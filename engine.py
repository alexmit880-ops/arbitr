"""Engine v10.3 — Professional Edition (all fixes applied)."""
from __future__ import annotations
import asyncio
import time
import sqlite3
import math
import random
from collections import deque, defaultdict
from dataclasses import dataclass, field
import dataclasses as _dc      # для asdict/fields в snapshot_open/restore_open
from typing import Optional, Dict, List, Set, Tuple, Deque

import aiohttp
import ccxt.async_support as ccxt
from loguru import logger

from config import *


# ════════════════════════════════════════════════
# MODELS
# ════════════════════════════════════════════════
@dataclass
class Ticker:
    """Тикер с биржи."""
    exchange: str
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float
    timestamp: int
    percentage: float = 0.0
    # A0.6 FIX: раньше при отсутствии timestamp в ответе биржи подставлялось
    # "сейчас" молча, и is_stale()/age_sec() не могли отличить "реально свежая
    # цена от биржи" от "мы просто не знаем, когда биржа её сформировала, и
    # предположили, что только что". Само по себе "сейчас" как fallback не
    # обязательно вредно (это разумная оценка "давно ли МЫ её опрашивали"),
    # но выдавать её за полноценный exchange-side timestamp — вводит в
    # заблуждение любой код, которому нужна именно биржевая свежесть, а не
    # свежесть нашего опроса. Флаг ниже делает это различие явным и видимым.
    has_exchange_timestamp: bool = True

    @classmethod
    def from_ccxt(cls, ex_name: str, symbol: str, data: dict) -> Optional["Ticker"]:
        """Безопасное создание из ccxt. Возвращает None при невалидных данных."""
        try:
            bid = float(data.get("bid") or 0)
            ask = float(data.get("ask") or 0)
            last = float(data.get("last") or 0)
            if bid <= 0 or ask <= 0 or last <= 0:
                return None
            raw_ts = data.get("timestamp")
            has_ts = bool(raw_ts)
            return cls(
                exchange=ex_name,
                symbol=symbol,
                bid=bid, ask=ask, last=last,
                volume=float(data.get("quoteVolume") or 0),
                timestamp=int(raw_ts or time.time() * 1000),
                percentage=float(data.get("percentage") or 0),
                has_exchange_timestamp=has_ts,
            )
        except (TypeError, ValueError):
            return None
    
    def is_stale(self, max_age_sec: float = None) -> bool:
        """Проверка свежести цены."""
        if max_age_sec is None:
            max_age_sec = PRICE_STALE_SEC
        if self.timestamp <= 0:
            return True
        return (time.time() * 1000 - self.timestamp) / 1000 > max_age_sec
    
    def bid_ask_spread_pct(self) -> float:
        """Спред внутри биржи в процентах. 999 если bid=0."""
        if self.bid <= 0:
            return 999.0
        return (self.ask - self.bid) / self.bid * 100
    
    def age_sec(self) -> float:
        """Возраст цены в секундах."""
        return (time.time() * 1000 - self.timestamp) / 1000


@dataclass
class Opportunity:
    """Арбитражная возможность с полным breakdown."""
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    spread_pct: float
    net_profit_pct: float
    volume: float
    exchanges_count: int
    timestamp: int
    confidence_score: float = 0.0
    confidence_breakdown: Dict[str, float] = field(default_factory=dict)
    category: str = "other"
    fee_pct: float = 0.0
    slippage_pct: float = 0.0
    withdrawal_pct: float = 0.0
    drift_pct: float = 0.0
    buy_ticker_age: float = 0.0
    sell_ticker_age: float = 0.0
    max_safe_size_usdt: float = 0.0
    execution_quality: float = 0.0
    first_seen: float = 0.0
    mid_exchange: str = ""  # непустое = route через 3 биржи
    route_type: str = "direct"  # "direct" | "route3"


@dataclass
class RealityCheckResult:
    """Explainable pre-trade decision before paper/live execution."""
    allowed: bool
    reason: str
    execution_score: float = 0.0
    expected_value_usdt: float = 0.0
    fill_probability: float = 0.0
    planned_size_usdt: float = 0.0


class RealityCheck:
    """Lightweight ticker-based execution realism gate.

    This is not L2 yet. It blocks obviously non-realistic paper trades and
    explains why: confidence, net, quote age, internal spread, max size and EV.
    """

    @staticmethod
    def planned_size(portfolio: "Portfolio", opp: Opportunity) -> float:
        balance_size = portfolio.balance * MAX_POSITION_PCT_OF_BALANCE
        size = min(MAX_TRADE_SIZE, balance_size)
        if opp.max_safe_size_usdt > 0:
            size = min(size, opp.max_safe_size_usdt)
        return max(0.0, size)

    @staticmethod
    def check(opp: Opportunity, prices: Dict[str, "Ticker"],
              portfolio: "Portfolio", confidence_min: float = None
              ) -> RealityCheckResult:
        buy_t = prices.get(opp.buy_exchange)
        sell_t = prices.get(opp.sell_exchange)
        size = RealityCheck.planned_size(portfolio, opp)

        if not buy_t or not sell_t:
            return RealityCheckResult(False, "missing_quotes", planned_size_usdt=size)
        # БЫЛО: if opp.confidence_score < REALITY_MIN_CONFIDENCE:  # жёсткие 60
        #
        # Здесь стоял ТРЕТИЙ независимый порог уверенности (60), пока
        # RiskManager и app.py проверяли 50, а потом 40. Три разных числа
        # в трёх местах: система проходила первый фильтр (40) и тут же
        # отсекалась вторым (60) — то есть калибровка порога не давала
        # НИКАКОГО эффекта.
        #
        # Теперь порог передаётся явно и по умолчанию равен 40 (режим
        # бутстрапа). app.py передаёт trader.risk.get_confidence_threshold(),
        # то есть все три фильтра читают ОДНО значение.
        conf_min = REALITY_BOOTSTRAP_MIN_CONFIDENCE if confidence_min is None else confidence_min
        if opp.confidence_score < conf_min:
            return RealityCheckResult(
                False, f"low_confidence ({opp.confidence_score:.1f} < {conf_min})",
                planned_size_usdt=size)
        if opp.net_profit_pct < REALITY_MIN_NET_PROFIT_PERCENT:
            return RealityCheckResult(False, "low_net_profit", planned_size_usdt=size)
        if opp.spread_pct > REALITY_MAX_SPREAD_WITHOUT_L2:
            return RealityCheckResult(False, "spread_requires_l2", planned_size_usdt=size)
        if max(buy_t.age_sec(), sell_t.age_sec()) > REALITY_MAX_QUOTE_AGE_SEC:
            return RealityCheckResult(False, "stale_quotes", planned_size_usdt=size)
        if max(buy_t.bid_ask_spread_pct(), sell_t.bid_ask_spread_pct()) > REALITY_MAX_INTERNAL_SPREAD_PCT:
            return RealityCheckResult(False, "wide_internal_spread", planned_size_usdt=size)
        if opp.execution_quality < REALITY_MIN_EXECUTION_QUALITY:
            return RealityCheckResult(False, "low_execution_quality", planned_size_usdt=size)
        if opp.max_safe_size_usdt > 0 and size > opp.max_safe_size_usdt * REALITY_MIN_SIZE_COVERAGE:
            return RealityCheckResult(False, "insufficient_max_size", planned_size_usdt=size)

        spread_stability = max(0.0, 100.0 - opp.spread_pct * 4.0)
        quote_age_score = max(0.0, 100.0 - max(buy_t.age_sec(), sell_t.age_sec()) * 12.5)
        internal_spread_score = max(0.0, 100.0 - max(buy_t.bid_ask_spread_pct(), sell_t.bid_ask_spread_pct()) * 50.0)
        execution_score = (
            opp.execution_quality * 0.35 +
            quote_age_score * 0.25 +
            internal_spread_score * 0.25 +
            spread_stability * 0.15
        )
        fill_probability = max(0.05, min(0.98, execution_score / 100.0))
        gross_profit = size * (opp.net_profit_pct / 100.0)
        failure_cost = size * 0.002  # 20 bps conservative failed/partial execution cost
        ev = fill_probability * gross_profit - (1.0 - fill_probability) * failure_cost

        if ev < REALITY_MIN_EV_USDT:
            return RealityCheckResult(False, "negative_ev", execution_score, ev, fill_probability, size)

        return RealityCheckResult(True, "ok", execution_score, ev, fill_probability, size)


@dataclass
class L2ExecutionResult:
    allowed: bool
    reason: str
    vwap_buy: float = 0.0
    vwap_sell: float = 0.0
    max_executable_usdt: float = 0.0
    fill_probability: float = 0.0
    expected_net_pct: float = 0.0


class OrderBookExecutionAnalyzer:
    """L2 VWAP validator for a single opportunity.

    Used only for top candidates before execution to avoid rate-limit explosion.
    """

    @staticmethod
    def _vwap(levels: list, target_usdt: float, side: str) -> Tuple[float, float]:
        remaining = target_usdt
        base_qty = 0.0
        spent_usdt = 0.0
        for price, amount in levels:
            price = float(price or 0)
            amount = float(amount or 0)
            if price <= 0 or amount <= 0:
                continue
            level_usdt = price * amount
            take_usdt = min(remaining, level_usdt)
            base_qty += take_usdt / price
            spent_usdt += take_usdt
            remaining -= take_usdt
            if remaining <= 1e-9:
                break
        if base_qty <= 0:
            return 0.0, 0.0
        return spent_usdt / base_qty, spent_usdt

    @staticmethod
    def _depth_usdt(levels: list) -> float:
        total = 0.0
        for price, amount in levels:
            try:
                total += float(price or 0) * float(amount or 0)
            except (TypeError, ValueError):
                continue
        return total

    @classmethod
    async def check(cls, pool: "ExchangePool", opp: Opportunity, planned_size_usdt: float) -> L2ExecutionResult:
        buy_ex = pool.clients.get(opp.buy_exchange)
        sell_ex = pool.clients.get(opp.sell_exchange)
        if not buy_ex or not sell_ex:
            return L2ExecutionResult(False, "missing_exchange_client")
        if planned_size_usdt <= 0:
            return L2ExecutionResult(False, "bad_planned_size")
        try:
            async with pool.get_semaphore(opp.buy_exchange), pool.get_semaphore(opp.sell_exchange):
                buy_task = buy_ex.fetch_order_book(opp.symbol, L2_ORDERBOOK_LIMIT)
                sell_task = sell_ex.fetch_order_book(opp.symbol, L2_ORDERBOOK_LIMIT)
                buy_ob, sell_ob = await asyncio.wait_for(
                    asyncio.gather(buy_task, sell_task), timeout=L2_TIMEOUT_SEC
                )
        except Exception as e:
            return L2ExecutionResult(False, f"l2_fetch_failed:{str(e)[:40]}")

        asks = buy_ob.get("asks") or []
        bids = sell_ob.get("bids") or []
        if not asks or not bids:
            return L2ExecutionResult(False, "empty_orderbook")

        vwap_buy, buy_filled = cls._vwap(asks, planned_size_usdt, "buy")
        vwap_sell, sell_filled = cls._vwap(bids, planned_size_usdt, "sell")
        executable = min(buy_filled, sell_filled)
        if executable < MIN_TRADE_SIZE:
            return L2ExecutionResult(False, "insufficient_l2_depth", vwap_buy, vwap_sell, executable)

        best_ask = float(asks[0][0])
        best_bid = float(bids[0][0])
        buy_slip_pct = ((vwap_buy - best_ask) / best_ask * 100) if best_ask > 0 else 999
        sell_slip_pct = ((best_bid - vwap_sell) / best_bid * 100) if best_bid > 0 else 999
        if max(buy_slip_pct, sell_slip_pct) > L2_MAX_VWAP_SLIPPAGE_PCT:
            return L2ExecutionResult(False, "l2_vwap_slippage_high", vwap_buy, vwap_sell, executable)

        fill_probability = min(0.98, executable / max(planned_size_usdt, 1e-9))
        if fill_probability < L2_MIN_FILL_PROBABILITY:
            return L2ExecutionResult(False, "low_l2_fill_probability", vwap_buy, vwap_sell, executable, fill_probability)

        fee_pct = (TRADING_FEES.get(opp.buy_exchange, 0.001) + TRADING_FEES.get(opp.sell_exchange, 0.001)) * 100
        l2_spread_pct = ((vwap_sell - vwap_buy) / vwap_buy * 100) if vwap_buy > 0 else -999
        expected_net_pct = l2_spread_pct - fee_pct
        if expected_net_pct < L2_MIN_NET_PROFIT_PERCENT:
            return L2ExecutionResult(False, "l2_net_too_low", vwap_buy, vwap_sell, executable, fill_probability, expected_net_pct)

        return L2ExecutionResult(True, "ok", vwap_buy, vwap_sell, executable, fill_probability, expected_net_pct)


@dataclass
class TradeResult:
    timestamp: int
    symbol: str
    buy_exchange: str
    sell_exchange: str
    amount: float
    pnl: float
    pnl_pct: float
    balance_after: float
    success: bool


@dataclass
class EntryDecision:
    """Решение о входе на основе базиса и измеренных расходов."""
    allowed: bool
    reason: str
    basis_pct: float = 0.0
    cost_pct: float = 0.0
    breakeven_pct: float = 0.0
    expected_net_pct: float = 0.0


class BasisEntryCriteria:
    """
    Финансовый критерий входа для cash-and-carry.

    Зачем: confidence — это взвешенная сумма ПРОКСИ (ликвидность, объём,
    история, рейтинг биржи). Ни один из них не отвечает на вопрос
    «заработаю ли я на удержании». Ответ на него даёт только базис минус
    расходы, и расходы надо считать по факту, а не по константам:
    две оценки подряд (0.44% и 1.04%) оказались занижены вчетверо.

    Формула сверена с измерениями (tests/test_measured_costs.py):
        комиссии    = 2 * (fee_buy + fee_sell) * 100
        перевод     = (w_buy + w_sell) / 10
        скольжение  = (BASE_SLIPPAGE + SLIPPAGE_PER_USDT*size) * 4 * 100
        спред       = buy_t.bid_ask_spread_pct() + sell_t.bid_ask_spread_pct()
    """

    def _costs_pct(self, size_usdt: float, buy_t: "Ticker",
                   sell_t: "Ticker") -> float:
        """
        Полные расходы на вход + выход, в процентах от размера.

        ФОРМУЛА ВЫВЕДЕНА ИЗМЕРЕНИЯМИ, а не взята из модели (Шаг 0.4).

            cost = комиссии + перевод_амортиз + скольжение
                   + 2*(spread_buy + spread_sell)

        Проверена на измерениях с погрешностью <= 0.012%:
            спреды 0.0/0.0 -> измерено 1.248%  (формула 1.260)
            спреды 0.2/0.2 -> измерено 2.051%  (формула 2.060)
            спреды 0.4/0.4 -> измерено 2.850%  (формула 2.860)

        Множитель 2 у спредов не ошибка: каждая нога пересекает свой
        спред дважды — на входе (покупка по ask / продажа по bid) и на
        выходе (продажа по bid / покупка по ask).

        РАНЬШЕ ЗДЕСЬ БЫЛА ПОПРАВКА -0.20 (SPREAD_REFERENCE_PCT). Она
        подбиралась как компенсация перевода 0.20% в формуле: измерения
        делались БЕЗ перевода (деньги не двигались), а формула его
        учитывала, и поправка их согласовывала.

        После удаления перевода (Шаг 0.5) поправка стала ВТОРОЙ ошибкой
        подряд: с ней погрешность выросла до 0.19%, без неё — 0.01%.
        То есть -0.20 компенсировала ровно то, что убрали. Удалена.

        ПЕРЕВОД УБРАН из расходов входа (Шаг 0.5, 2026-09-30).
        Основание - измерение, а не рассуждение:
          * в BalanceManager НЕТ операции перевода средств (проверено:
            ни transfer, ни rebalance, ни withdraw);
          * transfer_fee вычитался из PnL, но нигде не двигал деньги -
            то есть был расходом без события;
          * балансы бирж расходятся медленно: измерено -0.72 USDT за
            сделку на спотовой ноге, +4.10 на фьючерсной;
          * при стартовых 800 USDT на биржу спотовая нога исчерпывается
            через ~1100 сделок, то есть перевод нужен раз в 1100 сделок,
            а не в каждой.

        Амортизированная стоимость: 0.20% / 1100 = 0.00018% на сделку.
        Закладываем 0.02% - с запасом на ребалансировку при изменении
        волатильности и на дрейф балансов между биржами.

        Измерения (size=100), для проверки формулы:
            спреды 0.0/0.0 -> 1.248%   (предсказано 1.240)
            спреды 0.2/0.2 -> 2.051%   (предсказано 2.040)
            спреды 0.4/0.4 -> 2.850%   (предсказано 2.840)
        """
        fee_buy = TRADING_FEES.get(buy_t.exchange, 0.001)
        fee_sell = TRADING_FEES.get(sell_t.exchange, 0.001)
        commission = 2.0 * (fee_buy + fee_sell) * 100.0

        # Перевод: амортизированная оценка вместо полной ставки на сделку.
        transfer = TRANSFER_AMORTIZED_PCT

        slip = ProfitCalculator.calculate_slippage(size_usdt)
        slippage = slip * 4.0 * 100.0

        spread = (buy_t.bid_ask_spread_pct()
                  + sell_t.bid_ask_spread_pct())
        # Каждый спред учитывается дважды: нога пересекает его на входе
        # (покупка по ask / продажа по bid) и на выходе (наоборот).
        spread_cost = 2.0 * spread

        return commission + transfer + slippage + spread_cost

    def evaluate(self, basis_pct: float, size_usdt: float,
                 buy_t: "Ticker", sell_t: "Ticker",
                 funding_rate_per_hour: float = 0.0,
                 hold_hours: float = 1.0) -> EntryDecision:
        """Оценить вход. Порядок проверок — от дешёвой к дорогой."""
        base_cost = self._costs_pct(size_usdt, buy_t, sell_t)
        hourly = abs(funding_rate_per_hour) * 100.0 + BASIS_HOURLY_DEGRADATION_PCT
        total_cost = base_cost + hourly * max(hold_hours, 0.0)
        net = abs(basis_pct) - total_cost

        # 1. Спред: актив с широким спредом не торгуется НИ ПРИ КАКОМ базисе.
        #    Расход растёт 1:1 со спредом, а базис выше ~3.4% не встречается.
        #    Проверка идёт первой, потому что она отсекает больше всего.
        if (max(buy_t.bid_ask_spread_pct(), sell_t.bid_ask_spread_pct())
                > MAX_TRADABLE_SPREAD_PCT):
            return EntryDecision(
                False, "spread_too_wide", basis_pct, total_cost,
                total_cost + BASIS_SAFETY_MARGIN_PCT, net)

        # 2. Базис ниже минимума: не окупает расходы в принципе.
        if abs(basis_pct) < MIN_BASIS_PCT:
            return EntryDecision(
                False, "basis_below_minimum", basis_pct, total_cost,
                total_cost + BASIS_SAFETY_MARGIN_PCT, net)

        # 3. Базис есть, но удержание съедает прибыль.
        if net <= 0:
            return EntryDecision(
                False, "expected_net_negative", basis_pct, total_cost,
                total_cost + BASIS_SAFETY_MARGIN_PCT, net)

        return EntryDecision(
            True, "ok", basis_pct, total_cost,
            total_cost + BASIS_SAFETY_MARGIN_PCT, net)

    @staticmethod
    def hold_hours_for(basis_pct: float) -> float:
        """Время удержания по базису. Больше базис — больше времени."""
        b = abs(basis_pct)
        for threshold, hours in BASIS_TO_HOLD_HOURS:
            if b <= threshold:
                return max(PAPER_HOLD_HOURS_MIN, min(hours, PAPER_HOLD_HOURS_MAX))
        return PAPER_HOLD_HOURS_MAX


class PartialFillSimulator:
    """Simulates partial order fill for paper trading.

    Returns adjusted TradeResult with reduced amount/pnl proportional to fill_ratio.
    This makes paper PnL more realistic.
    """

    @staticmethod
    def apply(trade: TradeResult, max_safe_size: float, planned_size: float) -> TradeResult:
        if not PARTIAL_FILL_ENABLED:
            return trade
        if planned_size <= 0:
            return trade

        size_ratio = min(1.0, max_safe_size / max(planned_size, 1e-9))
        # FIX BUG #8: был `random.uniform(...)` из глобального генератора,
        # поэтому одинаковый вход давал РАЗНЫЕ результаты от запуска к
        # запуску. Частичное исполнение напрямую влияет на PnL, то есть
        # невоспроизводимость здесь означала невоспроизводимый бэктест:
        # два прогона на одних данных давали бы разные PnL, и отличить
        # реальное изменение от шума было бы невозможно. Локальный
        # генератор с фиксированным зерном делает расчёт детерминированным.
        _rng = random.Random(0xC0FFEE)
        fill_ratio = _rng.uniform(PARTIAL_FILL_MIN_RATIO,
                                  PARTIAL_FILL_MAX_RATIO)
        fill_ratio = min(fill_ratio, size_ratio * 1.2)

        if fill_ratio >= 1.0:
            return trade

        return TradeResult(
            timestamp=trade.timestamp,
            symbol=trade.symbol,
            buy_exchange=trade.buy_exchange,
            sell_exchange=trade.sell_exchange,
            amount=trade.amount * fill_ratio,
            pnl=trade.pnl * fill_ratio,
            pnl_pct=trade.pnl_pct,
            balance_after=trade.balance_after,
            success=trade.success,
        )


class ExecutionDelaySimulator:
    """Emulates latency between the two exchange legs.

    Generates a random delay (100–500 ms). If the spread decays
    beyond a threshold during that delay, the trade is either
    adjusted or blocked. No real sleep — purely probabilistic.
    """

    @staticmethod
    def adjust_opportunity(
        opp: Opportunity,
        stability: "SpreadStabilityTracker",
    ) -> str:
        """Returns empty string if OK, or a rejection reason."""
        if not EXECUTION_DELAY_ENABLED:
            return ""

        simulated_delay = random.uniform(EXECUTION_DELAY_SEC_MIN, EXECUTION_DELAY_SEC_MAX)
        decay = stability.decay_rate(opp)
        lifetime = stability.lifetime(opp)

        spread_before = opp.spread_pct
        spread_decay = decay * simulated_delay
        net_before = opp.net_profit_pct

        opp.spread_pct = max(0.0, opp.spread_pct - spread_decay)
        opp.net_profit_pct = max(opp.net_profit_pct - spread_decay, opp.net_profit_pct * 0.5)

        if opp.net_profit_pct <= 0:
            opp.spread_pct = spread_before
            opp.net_profit_pct = net_before
            return "spread_decayed_after_delay"

        # FIX BUG #4: значения восстанавливались ТОЛЬКО в ветке отклонения.
        # Если сделка проходила дальше, opp оставался мутированным — и именно
        # этот объект лежит в all_opps, который целиком пишется в БД.
        # В историю попадал заниженный net_profit_pct, причём ТОЛЬКО для
        # прошедших сделок: выборки «исполнено» и «все возможности»
        # становились несопоставимыми, и любая аналитика по ним врала.
        # Мутация нужна только на время вызова — дальше исполнение идёт
        # на копии (см. app.py: exec_opp = copy.deepcopy(opp)).
        opp.spread_pct = spread_before
        opp.net_profit_pct = net_before
        return ""


@dataclass
class Portfolio:
    balance: float
    initial_balance: float
    peak_equity: float = 0.0
    positions: Dict[str, dict] = field(default_factory=dict)
    closed_trades: List[TradeResult] = field(default_factory=list)
    recent_trades: Deque[TradeResult] = field(default_factory=lambda: deque(maxlen=ROLLING_WINDOW))
    consecutive_losses: int = 0
    daily_pnl: float = 0.0
    daily_trades: int = 0
    day_start_utc: int = field(default_factory=lambda: int(time.time()) - (int(time.time()) % 86400))
    last_trade_time: Dict[str, int] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.peak_equity == 0.0:
            self.peak_equity = self.balance
    
    def update_peak(self):
        if self.balance > self.peak_equity:
            self.peak_equity = self.balance
    
    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.balance) / self.peak_equity * 100
    
    @property
    def rolling_win_rate(self) -> float:
        if not self.recent_trades:
            return 0.5
        wins = sum(1 for t in self.recent_trades if t.pnl > 0)
        return wins / len(self.recent_trades)
    
    @property
    def pnl_pct(self) -> float:
        return (self.balance / self.initial_balance - 1) * 100
    
    def check_daily_reset(self):
        """Сброс daily лимитов при переходе через UTC midnight."""
        now_utc_midnight = int(time.time()) - (int(time.time()) % 86400)
        if now_utc_midnight > self.day_start_utc:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.day_start_utc = now_utc_midnight


class BalanceManager:
    """Paper inventory manager with per-exchange balances and reservations."""

    def __init__(self, initial_balances: Dict[str, Dict[str, float]]):
        self.balances: Dict[str, Dict[str, float]] = {}
        self.reserved: Dict[str, Dict[str, float]] = {}
        for exchange, assets in initial_balances.items():
            self.balances[exchange] = {asset.upper(): float(amount) for asset, amount in assets.items()}
            self.reserved[exchange] = {asset.upper(): 0.0 for asset in assets}

    def _ensure(self, exchange: str, asset: str):
        asset = asset.upper()
        self.balances.setdefault(exchange, {}).setdefault(asset, 0.0)
        self.reserved.setdefault(exchange, {}).setdefault(asset, 0.0)

    def total(self, exchange: str, asset: str) -> float:
        asset = asset.upper()
        return self.balances.get(exchange, {}).get(asset, 0.0)

    def reserved_amount(self, exchange: str, asset: str) -> float:
        asset = asset.upper()
        return self.reserved.get(exchange, {}).get(asset, 0.0)

    def free(self, exchange: str, asset: str) -> float:
        return max(0.0, self.total(exchange, asset) - self.reserved_amount(exchange, asset))

    def can_buy(self, exchange: str, usdt_amount: float) -> bool:
        return usdt_amount >= 0 and self.free(exchange, "USDT") >= usdt_amount

    def can_sell(self, exchange: str, asset: str, amount: float) -> bool:
        return amount >= 0 and self.free(exchange, asset) >= amount

    def reserve(self, exchange: str, asset: str, amount: float) -> bool:
        asset = asset.upper()
        if amount < 0:
            return False
        self._ensure(exchange, asset)
        if self.free(exchange, asset) < amount:
            return False
        self.reserved[exchange][asset] += amount
        return True

    def release(self, exchange: str, asset: str, amount: float):
        asset = asset.upper()
        self._ensure(exchange, asset)
        self.reserved[exchange][asset] = max(0.0, self.reserved[exchange][asset] - max(amount, 0.0))

    def apply_buy_fill(self, exchange: str, symbol: str, amount_base: float,
                       cost_usdt: float, fee_usdt: float = 0.0):
        base = symbol.split("/")[0].upper()
        total_cost = cost_usdt + fee_usdt
        self._ensure(exchange, "USDT")
        self._ensure(exchange, base)
        if self.total(exchange, "USDT") + 1e-12 < total_cost:
            raise ValueError(f"Insufficient USDT on {exchange}")
        self.balances[exchange]["USDT"] -= total_cost
        self.release(exchange, "USDT", total_cost)
        self.balances[exchange][base] += amount_base

    def apply_sell_fill(self, exchange: str, symbol: str, amount_base: float,
                        proceeds_usdt: float, fee_usdt: float = 0.0):
        base = symbol.split("/")[0].upper()
        self._ensure(exchange, base)
        self._ensure(exchange, "USDT")
        if self.total(exchange, base) + 1e-12 < amount_base:
            raise ValueError(f"Insufficient {base} on {exchange}")
        self.balances[exchange][base] -= amount_base
        self.release(exchange, base, amount_base)
        self.balances[exchange]["USDT"] += proceeds_usdt - fee_usdt

    def apply_spot_close(self, exchange: str, symbol: str, amount_base: float,
                         proceeds_usdt: float, fee_usdt: float = 0.0):
        self.apply_sell_fill(exchange, symbol, amount_base, proceeds_usdt, fee_usdt)

    def apply_futures_short_cycle(self, exchange: str, margin_usdt: float,
                                  realized_pnl_usdt: float, fee_usdt: float = 0.0,
                                  funding_usdt: float = 0.0):
        total_required = margin_usdt + fee_usdt + funding_usdt
        self._ensure(exchange, "USDT")
        if self.total(exchange, "USDT") + 1e-12 < total_required:
            raise ValueError(f"Insufficient futures USDT on {exchange}")
        self.balances[exchange]["USDT"] += realized_pnl_usdt - fee_usdt - funding_usdt
        self.release(exchange, "USDT", total_required)

    def close_futures_short(self, exchange: str, reserved_usdt: float,
                            realized_pnl_usdt: float, fee_usdt: float = 0.0,
                            funding_usdt: float = 0.0):
        """
        Закрытие шорта с ОСВОБОЖДЕНИЕМ РОВНО ЗАРЕЗЕРВИРОВАННОЙ СУММЫ.

        Зачем отдельный метод: apply_futures_short_cycle() освобождает
        margin + fee + funding, а при открытии резервировалось
        margin + ВХОДНАЯ_комиссия. Если exit_fee и funding не совпадают с
        входной комиссией, разница остаётся висеть в резерве навсегда —
        на каждом цикле. Средства не сходятся, и через десятки пар
        свободная маржа утекает.

        Здесь контракт честный: освобождаем ровно reserved_usdt, а PnL,
        комиссию выхода и funding проводим по балансу.
        """
        self._ensure(exchange, "USDT")
        self.balances[exchange]["USDT"] += (realized_pnl_usdt - fee_usdt
                                            - funding_usdt)
        if reserved_usdt > 0:
            self.release(exchange, "USDT", reserved_usdt)

    def rebalance_signal(self, target_per_exchange: Optional[float] = None,
                         min_amount: float = 10.0,
                         trigger_ratio: float = 0.5) -> List[dict]:
        """
        ДИАГНОСТИЧЕСКИЙ сигнал: куда переводить, чтобы восстановить баланс.

        НЕ выполняет перевод. В paper-режиме двигать нечего, а в live
        автоматический перевод без участия человека опасен: комиссия,
        время заморозки и риск, что в момент перевода позиция окажется
        не хеджированной. Задача этого метода - показать ОТКУДА и КУДА,
        решение принимает человек.

        Почему балансы расходятся (измерено, Шаг 0.5):
        дрейф СТРУКТУРНЫЙ, а не случайный. При входе в пару спотовая нога
        теряет на спреде и проскальзывании (покупка по ask, продажа по
        bid), а фьючерсная забирает всю прибыль от сходимости базиса
        (шорт приносит ровно то, что съела спотовая нога, плюс базис).
        Итог: USDT всегда мигрирует В СТОРОНУ биржи шорта.
        Замерено: mexc -0.72 USDT за сделку, bybit +4.10 за сделку.

        Логика сигнала:
          * дефицитная биржа - та, где free < target * trigger_ratio
          * донор - биржа с максимальным избытком (free - target)
          * сумма - min(дефицит, избыток), но не свободные деньги донора
            (часть может быть в резерве под открытые позиции)
          * переводы меньше min_amount не сигналим: комиссия и время
            заморозки съедят больше самой суммы

        Возвращает список dict: from, to, amount_usdt, reason, urgency.
        """
        balances = {ex: self.free(ex, "USDT")
                    for ex in sorted(self.balances.keys())}
        if not balances:
            return []
        if target_per_exchange is None:
            target_per_exchange = sum(balances.values()) / len(balances)

        # дефицитные: не хватает до порога
        deficits = []
        for ex, free in balances.items():
            need = target_per_exchange * trigger_ratio - free
            if need > min_amount:
                deficits.append((ex, need))
        if not deficits:
            return []

        # доноры: избыток сверх цели
        donors = []
        for ex, free in balances.items():
            surplus = free - target_per_exchange
            if surplus > min_amount:
                donors.append([ex, surplus])
        if not donors:
            return []

        signals = []
        for need_ex, need_amt in sorted(deficits, key=lambda x: -x[1]):
            for d in donors:
                if d[1] <= min_amount:
                    continue
                amount = min(need_amt, d[1])
                if amount < min_amount:
                    continue
                # нельзя отдать больше, чем реально свободно
                amount = min(amount, balances[d[0]])
                if amount < min_amount:
                    continue
                short_ratio = balances[need_ex] / target_per_exchange
                signals.append({
                    "from": d[0],
                    "to": need_ex,
                    "amount_usdt": round(amount, 2),
                    "reason": (f"{need_ex} держит {balances[need_ex]:.0f} USDT "
                               f"при цели {target_per_exchange:.0f} "
                               f"({short_ratio*100:.0f}% от нормы)"),
                    # четверть от нормы - уже критично; граница включительная,
                    # потому что ровно 25% означает "осталась четверть
                    # ёмкости", а не "ещё есть запас"
                    "urgency": "critical" if short_ratio <= 0.25 else "warning",
                })
                d[1] -= amount
                need_amt -= amount
                if need_amt <= min_amount:
                    break
        return signals

    def snapshot(self) -> Dict[str, Dict[str, Dict[str, float]]]:
        return {
            exchange: {
                asset: {
                    "total": self.total(exchange, asset),
                    "reserved": self.reserved_amount(exchange, asset),
                    "free": self.free(exchange, asset),
                }
                for asset in sorted(assets)
            }
            for exchange, assets in self.balances.items()
        }


@dataclass
class PipelineStats:
    total_symbols: int = 0
    spreads_found: int = 0
    after_fees: int = 0
    after_volume: int = 0
    profitable: int = 0
    anomalies_skipped: int = 0
    stale_skipped: int = 0
    zombie_skipped: int = 0
    volume_skipped: int = 0
    spread_low_skipped: int = 0
    spread_high_skipped: int = 0
    net_negative: int = 0
    net_low_skipped: int = 0
    best_spread_pct: float = 0.0
    best_spread_symbol: str = ""
    best_spread_path: str = ""
    best_confidence: float = 0.0


# ════════════════════════════════════════════════
# ZOMBIE DETECTOR — FIXED
# ════════════════════════════════════════════════
class ZombieDetector:
    """
    Обнаруживает зомби-токены.
    
    FIX: отслеживает изменение ЦЕНЫ как индикатор активности,
    а не объёма (который может быть статичным 24h snapshot).
    """
    
    def __init__(self):
        self.price_history: Dict[str, Dict[str, deque]] = {}
        self.volume_history: Dict[str, Dict[str, deque]] = {}
        self.last_movement: Dict[str, float] = {}
        self._recovered_count = 0
        # FIX BUG #10: журнал восстановлений по символам (deque ограничен,
        # чтобы не превратиться в новую утечку памяти).
        self._recovered_symbols: Deque[str] = deque(maxlen=200)

    def prune(self, active_symbols: Set[str]):
        """A1.3 FIX: price_history/volume_history/last_movement раньше росли
        без ограничений по числу СИМВОЛОВ (каждая deque на символ+биржу сама
        по себе ограничена maxlen=50, но количество самих символов в словаре —
        нет). Hunter.select() периодически (каждые SYMBOL_RELOAD_CYCLES)
        ротирует список отслеживаемых символов, но записи для символов, вышедших
        из ротации, раньше никогда не удалялись — только полный clear_all() по
        auto-recovery. При многодневном/многонедельном аптайме это медленная,
        но реальная утечка памяти. Вызывать после каждой ротации symbols."""
        stale = [s for s in self.price_history if s not in active_symbols]
        for s in stale:
            del self.price_history[s]
            self.volume_history.pop(s, None)
            self.last_movement.pop(s, None)
        if stale:
            logger.debug(f"ZombieDetector: pruned {len(stale)} inactive symbols")
    
    def update(self, prices: Dict[str, Dict[str, Ticker]]):
        """Обновить историю. Отслеживаем изменение цены."""
        now = time.time()
        
        for symbol, ex_prices in prices.items():
            if symbol not in self.price_history:
                self.price_history[symbol] = {}
                self.volume_history[symbol] = {}
            
            for ex_name, ticker in ex_prices.items():
                if ex_name not in self.price_history[symbol]:
                    self.price_history[symbol][ex_name] = deque(maxlen=50)
                    self.volume_history[symbol][ex_name] = deque(maxlen=50)
                
                # Добавляем в историю
                self.price_history[symbol][ex_name].append(ticker.last)
                self.volume_history[symbol][ex_name].append(ticker.volume)
                
                # ✅ FIX: обновляем last_movement при ЛЮБОМ изменении цены
                history = list(self.price_history[symbol][ex_name])
                if len(history) >= 2:
                    if history[-1] != history[-2]:  # цена изменилась
                        self.last_movement[symbol] = now
    
    def _std(self, values: List[float]) -> float:
        """Стандартное отклонение."""
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return math.sqrt(variance)
    
    def is_zombie(self, symbol: str, prices: Dict[str, Ticker]) -> Tuple[bool, str]:
        """Проверить является ли токен зомби."""
        if symbol not in self.price_history:
            return False, ""
        
        # Требуем минимум истории
        for ex_name, hist in self.price_history.get(symbol, {}).items():
            if len(hist) < ZOMBIE_MIN_HISTORY:
                return False, ""
        
        reasons = []
        
        # Check 1: Bid == Ask (синтетическая цена).
        # На одной бирже это важный сигнал, но при 3+ биржах требуем большинство,
        # иначе один плохой тикер может забанить весь символ.
        bid_ask_same = [
            ex_name for ex_name, ticker in prices.items()
            if ticker.bid > 0 and ticker.ask > 0 and ticker.ask == ticker.bid
        ]
        if bid_ask_same:
            required = 1 if len(prices) <= 2 else max(2, math.ceil(len(prices) * 0.6))
            if len(bid_ask_same) >= required:
                reasons.append(f"bid=ask:{','.join(bid_ask_same[:3])}")
        
        # Check 2: Нет движения цены > 5 мин
        last_movement = self.last_movement.get(symbol, 0)
        if last_movement > 0:
            no_move_sec = time.time() - last_movement
            if no_move_sec > ZOMBIE_NO_MOVEMENT_SEC:
                reasons.append(f"static:{int(no_move_sec/60)}m")
        
        # Check 3: Price CV слишком низкий. Для нескольких бирж требуем
        # подтверждение минимум двумя источниками, чтобы снизить false positive.
        low_cv = []
        for ex_name, price_hist in self.price_history.get(symbol, {}).items():
            if len(price_hist) >= ZOMBIE_MIN_HISTORY:
                std = self._std(list(price_hist)[-ZOMBIE_MIN_HISTORY:])
                avg = sum(list(price_hist)[-ZOMBIE_MIN_HISTORY:]) / ZOMBIE_MIN_HISTORY
                if avg > 0:
                    cv = std / avg
                    if cv < ZOMBIE_CV_THRESHOLD:
                        low_cv.append((ex_name, cv))
        if low_cv:
            required = 1 if len(self.price_history.get(symbol, {})) <= 2 else 2
            if len(low_cv) >= required:
                reasons.append(f"cv:{low_cv[0][1]:.6f}")
        
        if reasons:
            return True, "; ".join(reasons)
        return False, ""
    
    def clear_zombie(self, symbol: str):
        """Очистить зомби-статус для конкретной монеты."""
        self.last_movement.pop(symbol, None)
        if symbol in self.price_history:
            for ex_name in self.price_history[symbol]:
                self.price_history[symbol][ex_name].clear()
        if symbol in self.volume_history:
            for ex_name in self.volume_history[symbol]:
                self.volume_history[symbol][ex_name].clear()
    
    def clear_all(self):
        """Очистить все зомби-данные."""
        self.price_history.clear()
        self.volume_history.clear()
        self.last_movement.clear()
    
    def mark_recovered(self, symbol: Optional[str] = None):
        """
        FIX BUG #10: раньше вызывалось mark_recovered() без аргумента, поэтому
        счётчик рос, но было невозможно понять, КАКОЙ символ восстановился.
        Для аналитики (какие пары были зомби и как быстро оживали) это
        бесполезно. Символ теперь сохраняется; параметр опционален, чтобы не
        сломать существующие вызовы в тестах.
        """
        self._recovered_count += 1
        if symbol:
            self._recovered_symbols.append(symbol)
    
    @property
    def recovered_count(self) -> int:
        return self._recovered_count


# ════════════════════════════════════════════════
# TRADE VERIFIER
# ════════════════════════════════════════════════
class TradeVerifier:
    def __init__(self):
        self.trade_history: List[TradeResult] = []
        self.execution_times: deque = deque(maxlen=100)
        self.last_trade_time: float = 0
    
    def record_trade(self, trade: TradeResult):
        self.trade_history.append(trade)
        now = time.time()
        if self.last_trade_time > 0:
            self.execution_times.append(now - self.last_trade_time)
        self.last_trade_time = now
    
    def get_health_report(self) -> Dict:
        if len(self.trade_history) < 5:
            return {"status": "insufficient_data", "warnings": [], "checks": {}}
        
        warnings = []
        checks = {}
        
        pnls = [t.pnl for t in self.trade_history[-20:]]
        if len(set(round(p, 4) for p in pnls)) < 3:
            warnings.append("PnL values too similar")
        checks["pnl_variance"] = self._std(pnls)
        
        wins = sum(1 for t in self.trade_history if t.pnl > 0)
        wr = wins / len(self.trade_history) * 100
        if wr == 100 and len(self.trade_history) >= 10:
            warnings.append(f"100% WR ({wins}/{len(self.trade_history)})")
        checks["win_rate"] = wr
        
        amounts = [t.amount for t in self.trade_history[-20:]]
        if len(set(round(a, 6) for a in amounts)) < 3:
            warnings.append("Trade amounts identical")
        checks["amount_variance"] = self._std(amounts) if amounts else 0
        
        status = "healthy" if not warnings else "warning"
        return {
            "status": status, 
            "warnings": warnings, 
            "checks": checks, 
            "total_trades": len(self.trade_history),
        }
    
    def _std(self, values):
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))


# ════════════════════════════════════════════════
# SYSTEM HEALTH MONITOR
# ════════════════════════════════════════════════
class SystemHealthMonitor:
    def __init__(self):
        self.start_time = time.time()
        self.last_cycle_time: float = 0
        self.cycle_times: deque = deque(maxlen=50)
        self.errors: deque = deque(maxlen=100)
    
    def record_cycle(self):
        now = time.time()
        if self.last_cycle_time > 0:
            self.cycle_times.append(now - self.last_cycle_time)
        self.last_cycle_time = now
    
    def record_error(self, error: str):
        self.errors.append((time.time(), error))
    
    def get_health(self, prices: Dict[str, Dict[str, Ticker]]) -> Dict:
        now = time.time()
        avg_cycle = sum(self.cycle_times) / len(self.cycle_times) if self.cycle_times else 0
        stale_count = fresh_count = 0
        for symbol, ex_prices in prices.items():
            for ex_name, ticker in ex_prices.items():
                if ticker.age_sec() > 10:
                    stale_count += 1
                else:
                    fresh_count += 1
        recent_errors = [e for t, e in self.errors if now - t < 300]
        return {
            "uptime_sec": now - self.start_time,
            "avg_cycle_time": avg_cycle,
            "fresh_prices": fresh_count,
            "stale_prices": stale_count,
            "total_prices": fresh_count + stale_count,
            "recent_errors": len(recent_errors),
        }

class ExchangeHealthMonitor:
    """Tracks per-exchange health: freshness, failures, last good data.

    Read-only monitoring. No blocking yet — just data for future kill switches.
    """

    def __init__(self):
        self.last_success: Dict[str, float] = {}
        self.consecutive_failures: Dict[str, int] = {}
        self.last_error: Dict[str, str] = {}
        self.last_error_time: Dict[str, float] = {}
        self._log_limit: Dict[str, float] = {}

    def record_failure(self, exchange: str, error: str):
        self.consecutive_failures[exchange] = self.consecutive_failures.get(exchange, 0) + 1
        self.last_error[exchange] = error[:80]
        self.last_error_time[exchange] = time.time()

    def record_success(self, exchange: str):
        self.last_success[exchange] = time.time()
        self.consecutive_failures[exchange] = 0

    def age_sec(self, exchange: str) -> float:
        last = self.last_success.get(exchange)
        if last is None:
            return 0.0  # never had success – grace period
        return time.time() - last

    def is_healthy(self, exchange: str) -> bool:
        if self.consecutive_failures.get(exchange, 0) >= 5:
            return False
        if self.age_sec(exchange) > 60:
            return False
        return True

    def summary(self) -> Dict:
        now = time.time()
        report = {}
        for ex in sorted(set(list(self.last_success.keys()) + list(self.last_error.keys()))):
            report[ex] = {
                "age_sec": now - self.last_success.get(ex, now),
                "failures": self.consecutive_failures.get(ex, 0),
                "healthy": self.is_healthy(ex),
                "last_error": self.last_error.get(ex, ""),
            }
        return report

    def log_warnings(self, logger_instance):
        now = time.time()
        for ex, info in self.summary().items():
            if not info["healthy"]:
                if ex not in self._log_limit or now - self._log_limit[ex] > 30:
                    logger_instance.warning(
                        f"⚠️ Exchange {ex}: unhealthy | "
                        f"age={info['age_sec']:.0f}s failures={info['failures']} "
                        f"error={info['last_error'][:40]}"
                    )
                    self._log_limit[ex] = now

class ExchangeStatusMonitor:
    """Monitors per-exchange operational status (maintenance, deposit, withdraw).

    For each exchange, tries fetchStatus() if available; otherwise marks as unknown.
    Pure monitoring — no blocking. This is the foundation for future kill switches.
    """

    def __init__(self):
        self._status: Dict[str, str] = {}
        self._last_check: Dict[str, float] = {}
        self._next_full_check: float = 0

    async def check_all(self, pool: "ExchangePool"):
        now = time.time()
        for ex_name, client in pool.clients.items():
            if ex_name not in self._last_check or now - self._last_check[ex_name] > 120:
                self._last_check[ex_name] = now
                try:
                    has_status = client.has.get("fetchStatus", False)
                    if has_status:
                        resp = await client.fetch_status()
                        status = resp.get("status", "unknown")
                    else:
                        # fallback — try a minimal ping via fetchTime
                        if client.has.get("fetchTime", False):
                            await client.fetch_time()
                            status = "ok"
                        else:
                            status = "unknown"
                    self._status[ex_name] = status
                except Exception as e:
                    self._status[ex_name] = "error"
                    logger.warning(f"🚫 {ex_name} status check failed: {str(e)[:60]}")

    def is_operational(self, ex_name: str) -> bool:
        return self._status.get(ex_name, "unknown") in ("ok", "unknown")

    def unhealthy(self) -> List[str]:
        return [ex for ex, st in self._status.items() if st == "error"]

    def summary(self) -> Dict[str, str]:
        return dict(self._status)






# ════════════════════════════════════════════════
# REPLAY SYSTEM
# ════════════════════════════════════════════════
@dataclass
class ReplaySnapshot:
    timestamp: int
    spread_pct: float
    net_profit_pct: float
    buy_price: float
    sell_price: float
    volume: float


@dataclass
class ReplayDecision:
    timestamp: int
    symbol: str
    path: str
    action: str
    reason: str
    confidence: float
    spread_pct: float
    net_profit_pct: float
    max_size_usdt: float
    stability_score: float = 0.0
    decay_rate: float = 0.0
    planned_size_usdt: float = 0.0
    fill_probability: float = 0.0
    expected_value_usdt: float = 0.0
    pnl: float = 0.0


class ReplaySystem:
    def __init__(self):
        self.history: Dict[str, Dict[str, Deque[ReplaySnapshot]]] = {}
        self._last_save: Dict[str, float] = {}
        self.decisions: Deque[ReplayDecision] = deque(maxlen=REPLAY_MAX_HISTORY)
    
    def record(self, opp: Opportunity):
        now = time.time()
        key = f"{opp.buy_exchange}->{opp.sell_exchange}"
        save_key = f"{opp.symbol}|{key}"
        if now - self._last_save.get(save_key, 0) < REPLAY_SAVE_INTERVAL_SEC:
            return
        self._last_save[save_key] = now
        
        if opp.symbol not in self.history:
            self.history[opp.symbol] = {}
        if key not in self.history[opp.symbol]:
            self.history[opp.symbol][key] = deque(maxlen=REPLAY_MAX_HISTORY)
        
        self.history[opp.symbol][key].append(ReplaySnapshot(
            timestamp=int(now),
            spread_pct=opp.spread_pct,
            net_profit_pct=opp.net_profit_pct,
            buy_price=opp.buy_price,
            sell_price=opp.sell_price,
            volume=opp.volume,
        ))

    def record_decision(self, opp: Opportunity, action: str, reason: str = "",
                        planned_size_usdt: float = 0.0, fill_probability: float = 0.0,
                        expected_value_usdt: float = 0.0, pnl: float = 0.0,
                        stability_score: float = 0.0, decay_rate: float = 0.0):
        """
        Запись решения по возможности - для реплея и последующего анализа.

        ВОССТАНОВЛЕНО (Шаг 0.5, 2026-09-30): параметры stability_score и
        decay_rate. Поля объявлены у ReplayDecision, и app.py передаёт их в
        пяти местах, но сигнатура их не принимала. Каждый такой вызов падал:
            TypeError: record_decision() got an unexpected keyword argument
        То есть НИ ОДНО решение о сделке не попадало в журнал реплея, включая
        самые частые ветки "blocked". Параметры потерялись при пересборке.
        """
        self.decisions.append(ReplayDecision(
            timestamp=int(time.time()),
            symbol=opp.symbol,
            path=f"{opp.buy_exchange}->{opp.sell_exchange}",
            action=action,
            reason=reason,
            confidence=opp.confidence_score,
            spread_pct=opp.spread_pct,
            net_profit_pct=opp.net_profit_pct,
            max_size_usdt=opp.max_safe_size_usdt,
            stability_score=stability_score,
            decay_rate=decay_rate,
            planned_size_usdt=planned_size_usdt,
            fill_probability=fill_probability,
            expected_value_usdt=expected_value_usdt,
            pnl=pnl,
        ))

class SpreadStabilityTracker:
    """Tracks how cross-exchange spreads evolve over time.

    Measures:
      - lifetime: seconds the spread has been alive
      - decay_rate: average contraction (%/sec) over recent history
      - stability_score: 0-100 based on low variance and slow decay

    This is read-only data collection. No blocking.
    """

    def __init__(self):
        self._t0: Dict[str, float] = {}          # first_seen per key
        self._spreads: Dict[str, deque] = {}      # (timestamp, spread) per key

    def _key(self, opp: Opportunity) -> str:
        return f"{opp.symbol}|{opp.buy_exchange}->{opp.sell_exchange}"

    def update(self, opportunities: List[Opportunity]):
        now = time.time()
        for opp in opportunities:
            key = self._key(opp)
            if key not in self._t0:
                self._t0[key] = now
            if key not in self._spreads:
                self._spreads[key] = deque(maxlen=20)
            self._spreads[key].append((now, opp.spread_pct))

    def lifetime(self, opp: Opportunity) -> float:
        return time.time() - self._t0.get(self._key(opp), time.time())

    def decay_rate(self, opp: Opportunity) -> float:
        """Average spread contraction per second over recent history."""
        key = self._key(opp)
        points = list(self._spreads.get(key, []))
        if len(points) < 4:
            return 0.0
        first_spread = points[0][1]
        last_spread = points[-1][1]
        elapsed = points[-1][0] - points[0][0]
        if elapsed <= 0 or first_spread <= 0:
            return 0.0
        return (first_spread - last_spread) / elapsed / max(first_spread, 1e-9) * 100

    def stability_score(self, opp: Opportunity) -> float:
        key = self._key(opp)
        points = list(self._spreads.get(key, []))
        if len(points) < 4:
            return 50.0
        spreads = [s for _, s in points]
        mean_s = sum(spreads) / len(spreads)
        var_s = sum((s - mean_s) ** 2 for s in spreads) / len(spreads)
        cv = var_s ** 0.5 / max(mean_s, 1e-9)
        base = max(0, 100 - cv * 200)
        decay = abs(self.decay_rate(opp))
        penalty = min(50, decay * 10)
        return max(0, min(100, base - penalty))


class ShadowLogger:
    """Сравнивает expected (RealityCheck / L2) с actual (TradeResult).

    Накопляет статистику для калибровки модели. Ничего не блокирует.
    """

    def __init__(self, enabled: bool = True):
        """
        FIX BUG #3: тесты вызывают ShadowLogger(enabled=True), а конструктор
        параметра не принимал -> TypeError, и 12 тестов падали. Заодно
        появился реальный выключатель: при enabled=False ничего не
        записывается. Параметр опционален, поэтому существующие вызовы
        ShadowLogger() в app.py продолжают работать.

        ВАРИАНТ A: существующий путь record() -> records (для app.py) НЕ ТРОНУТ.
        Новая статистика по попыткам исполнения живёт в entries и используется
        трейдером и тестами. Две системы учёта рядом, но не смешанные:
        records пишет app.py после состоявшейся сделки, entries — трейдер
        на КАЖДОЙ попытке, включая отказы.
        """
        self.enabled = enabled
        self.records: List[Dict] = []
        self.max_records = 500
        # Попытки исполнения: успешные и отклонённые. Именно они позволяют
        # отличить "бот не нашёл сделок" от "бот нашёл, но исполнение
        # отклонено по API/ликвидности" — без этого неполный shadow-лог
        # не может ничего опровергнуть.
        self.entries: List[Dict] = []
        self.max_entries = 5000

    def record_entry(self, opp: "Opportunity", rejected: bool = False,
                     reason: str = "", synthetic_pnl: float = 0.0,
                     latent_pnl: float = 0.0, **extra):
        """
        Запись одной ПОПЫТКИ исполнения (в отличие от record(), которая
        пишет состоявшуюся сделку).

        synthetic_pnl — мгновенная оценка по спреду на входе.
        latent_pnl   — оценка после задержки и дрейфа спреда, т.е. что
                       ПРОИЗОШЛО БЫ на самом деле. Расхождение между ними
                       и есть тот самый обман, который мы ищем.
        """
        if not getattr(self, "enabled", True):
            return
        e = {
            "ts": int(time.time()),
            "symbol": getattr(opp, "symbol", ""),
            "buy_ex": getattr(opp, "buy_exchange", ""),
            "sell_ex": getattr(opp, "sell_exchange", ""),
            "rejected": bool(rejected),
            "reason": reason,
            "synthetic_pnl": float(synthetic_pnl),
            "latent_pnl": float(latent_pnl),
        }
        e.update(extra)
        self.entries.append(e)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]

    def stats(self) -> Dict:
        """Сводка по попыткам. Имена ключей заданы тестами."""
        if not self.entries:
            return {"total": 0, "rejected": 0, "traded": 0,
                    "avg_synthetic_pnl": 0.0, "avg_latent_pnl": 0.0}
        rejected = [e for e in self.entries if e["rejected"]]
        traded = [e for e in self.entries if not e["rejected"]]
        return {
            "total": len(self.entries),
            "rejected": len(rejected),
            "traded": len(traded),
            "avg_synthetic_pnl": (sum(e["synthetic_pnl"] for e in traded)
                                  / len(traded)) if traded else 0.0,
            "avg_latent_pnl": (sum(e["latent_pnl"] for e in traded)
                                / len(traded)) if traded else 0.0,
        }

    def rejected_by_reason(self) -> Dict[str, int]:
        """Счётчик отказов по причинам. Позволяет увидеть, ЧЕМ именно
        бот отказывает, а не просто сколько раз."""
        out: Dict[str, int] = {}
        for e in self.entries:
            if e["rejected"]:
                r = e.get("reason") or "unknown"
                out[r] = out.get(r, 0) + 1
        return out

    def record(self, opp: Opportunity, trade: TradeResult,
               expected_ev: float, expected_fill_prob: float,
               planned_size: float, l2_max_exec: float = 0):
        """Сохраняет сравнение expected vs actual."""
        # FIX BUG #3: при enabled=False ничего не пишем. Иначе параметр
        # был бы декоративным, и тест «disabled не пишет» не имел бы смысла.
        if not getattr(self, "enabled", True):
            return
        self.records.append({
            "ts": int(time.time()),
            "symbol": opp.symbol,
            "buy_ex": opp.buy_exchange,
            "sell_ex": opp.sell_exchange,
            "conf": opp.confidence_score,
            "spread": opp.spread_pct,
            "net_expected": opp.net_profit_pct,
            "net_actual": trade.pnl_pct,
            "pnl_actual": trade.pnl,
            "expected_ev": expected_ev,
            "expected_fill_pct": expected_fill_prob * 100,
            "planned_size": planned_size,
            "actual_size": trade.amount,
            "l2_max_exec": l2_max_exec,
            "success": trade.success,
        })
        if len(self.records) > self.max_records:
            self.records = self.records[-self.max_records:]

    def summary(self) -> Dict:
        if not self.records:
            return {"total": 0}
        recent = [r for r in self.records if r["ts"] > time.time() - 3600]
        ev_errors = [abs(r["expected_ev"]) for r in recent if r["expected_ev"] != 0]
        pnl_diff = [abs(r["net_expected"] - r["net_actual"]) for r in recent if r["net_actual"] != 0]
        return {
            "total": len(self.records),
            "recent": len(recent),
            "avg_ev_error": sum(ev_errors) / len(ev_errors) if ev_errors else 0,
            "avg_pnl_diff": sum(pnl_diff) / len(pnl_diff) if pnl_diff else 0,
            "last_symbol": recent[-1]["symbol"] if recent else "",
        }

    def log_summary(self, logger_instance):
        s = self.summary()
        if s["total"] == 0:
            return
        logger_instance.info(
            f"📊 Shadow: {s['total']} trades | "
            f"ΔEV={s['avg_ev_error']:.2f} | "
            f"ΔPnL={s['avg_pnl_diff']:.2f}%"
        )


# ════════════════════════════════════════════════
# LIFETIME TRACKER


# ════════════════════════════════════════════════
        return  # nothing


class FundingRateTracker:
    """Fetch funding rates from futures clients.

    Funding rate is the periodic payment between long and short traders.
    Positive = longs pay shorts. Negative = shorts pay longs.
    For our spot-long / futures-short arb:
      - high positive funding → short pays long → cost
      - negative funding → short receives → profit
    """

    def __init__(self):
        self.rates: Dict[str, float] = {}  # exchange → funding rate per hour
        self._last_fetch: float = 0

    async def fetch_all(self, pool: ExchangePool):
        """Fetch funding rates from all futures clients."""
        rates = {}
        for ex_name, client in pool.futures_clients.items():
            try:
                # ccxt unified: fetch_funding_rate(symbol) или fetch_funding_rates()
                # Нужен USDT perpetual — пробуем типовые символы.
                #
                # ВАЖНО (Шаг 0.5): вызовы ниже БЫЛИ без await. ccxt-методы
                # асинхронные, поэтому они создавали корутину и сразу
                # отбрасывали её, а try/except Exception никогда не
                # срабатывал. Следствия:
                #   - funding ВСЕГДА был 0.0 (в логах: "bybit: 0.0000%")
                #   - при аварии запроса исключение не ловилось вовсе
                #   - RuntimeWarning: coroutine ... was never awaited
                # То есть расход на удержание позиции не учитывался, и PnL
                # систематически завышался на величину funding.
                result = None
                for sym in ("BTC/USDT:USDT", "BTC/USDT"):
                    try:
                        result = await client.fetch_funding_rate(sym)
                        if result:
                            break
                    except Exception:
                        result = None
                if result is not None and isinstance(result, dict):
                    raw = result.get("fundingRate") or result.get("funding_rate") or 0
                    # Convert 8h rate to hourly
                    rates[ex_name] = float(raw) / 8
                else:
                    rates[ex_name] = 0.0
            except Exception:
                rates[ex_name] = 0.0
        self.rates = rates
        self._last_fetch = time.time()

    def get_rate(self, exchange: str) -> float:
        """Возвращает funding rate в час. Положительное = шорт платит."""
        return self.rates.get(exchange, 0.0)

    def get_funding_penalty(self, buy_exchange: str, sell_exchange: str) -> float:
        """На сколько funding rate ухудшает/улучшает сделку в % (за час).

        При spot-long + futures-short:
          - funding_rate(sell) > 0 → мы платим → penalty
          - funding_rate(sell) < 0 → мы получаем → bonus
        """
        sell_rate = self.get_rate(sell_exchange)
        buy_rate = self.get_rate(buy_exchange)
        # Если мы держим час, платим sell_rate, получаем buy_rate (как лонг)
        net_funding = sell_rate - buy_rate
        return net_funding * 100  # в процентах


# ════════════════════════════════════════════════
# LIFETIME TRACKER
# LIFETIME TRACKER
# ════════════════════════════════════════════════
class LifetimeTracker:
    def __init__(self):
        self.active: Dict[str, Dict[str, float]] = {}
        self.history: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    
    def update(self, opportunities: List[Opportunity]):
        current_time = time.time()
        seen = set()
        
        for opp in opportunities:
            key = f"{opp.symbol}|{opp.buy_exchange}->{opp.sell_exchange}"
            seen.add(key)
            if key not in self.active.get(opp.symbol, {}):
                if opp.symbol not in self.active:
                    self.active[opp.symbol] = {}
                self.active[opp.symbol][key] = current_time
                opp.first_seen = current_time
            else:
                opp.first_seen = self.active[opp.symbol][key]
        
        to_remove = []
        for symbol, paths in list(self.active.items()):
            for path_key, first_seen in list(paths.items()):
                if path_key not in seen:
                    lifetime = current_time - first_seen
                    path = path_key.split("|")[1]
                    self.history[symbol][path].append(lifetime)
                    if len(self.history[symbol][path]) > LIFETIME_HISTORY_MAX:
                        self.history[symbol][path] = self.history[symbol][path][-LIFETIME_HISTORY_MAX:]
                    to_remove.append((symbol, path_key))
        
        for symbol, path_key in to_remove:
            if symbol in self.active and path_key in self.active[symbol]:
                del self.active[symbol][path_key]
                if not self.active[symbol]:
                    del self.active[symbol]
    
    def get_repeatability(self, symbol: str, path: str) -> float:
        """
        Повторяемость возможностей по наблюдениям.

        ВОССТАНОВЛЕНО (Шаг 0.3, 2026-09-30): при отсутствии данных
        возвращается НЕЙТРАЛЬНОЕ 50.0 вместо 0.0.

        Почему 0.0 был ошибкой: ноль на шкале 0-100 означает
        "гарантированно плохо", а отсутствие наблюдений означает
        "неизвестно". Скоринг наказывал систему за то, что она только
        запустилась, и порождал петлю:

            history      = f(закрытых сделок)  = 0
            закрытые     = f(confidence >= порога)
            confidence   < порога, потому что history = 0

        Петля не могла разорваться САМА: пока порог недостижим, история
        не накапливается, а пока истории нет — порог недостижим.
        Нейтральное 50.0 честно выражает "не знаю" и допускает старт.
        """
        lifetimes = self.history.get(symbol, {}).get(path, [])
        if len(lifetimes) < LIFETIME_MIN_SAMPLES:
            return 50.0
        count = len(lifetimes)
        avg_life = sum(lifetimes) / len(lifetimes)
        raw_score = math.log1p(count) * 15 + avg_life * 0.5
        # ОБРЕЗКА СНИЗУ (Шаг 0.4).
        #
        # Без неё система награждала незнание выше плохого опыта: при
        # n=5 и жизни 5 секунд формула давала 29.4, то есть реальный
        # отрицательный опыт оценивался ХУЖЕ, чем отсутствие данных (50.0).
        # Инверсия: чем хуже показатели, тем выше оценка на старте.
        #
        # Смысл обрезки: 50 означает «не знаем». Наблюдения могут только
        # улучшить оценку относительно незнания, но не ухудшить её.
        return max(50.0, min(100.0, raw_score))


# ════════════════════════════════════════════════
# EXCHANGE PAIR RANKER
# ════════════════════════════════════════════════
class ExchangePairRanker:
    def __init__(self):
        self.stats: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"count": 0, "total_spread": 0.0, "success": 0}
        )
    
    def record(self, opp: Opportunity, executed: bool = False):
        pair = f"{opp.buy_exchange}->{opp.sell_exchange}"
        s = self.stats[pair]
        s["count"] += 1
        s["total_spread"] += opp.spread_pct
        if executed:
            s["success"] += 1
    
    def get_pair_score(self, buy_ex: str, sell_ex: str) -> float:
        pair = f"{buy_ex}->{sell_ex}"
        s = self.stats.get(pair)
        if not s or s["count"] < 3:
            return 50.0
        avg_spread = s["total_spread"] / s["count"]
        success_rate = s["success"] / s["count"]
        count_factor = min(100, s["count"] * 2)
        spread_score = min(100, avg_spread * 20)
        return min(100, count_factor * 0.3 + spread_score * 0.4 + success_rate * 100 * 0.3)
    
    def get_top_pairs(self, n: int = 5) -> List[Tuple[str, int, float]]:
        pairs = [(p, int(s["count"]), s["total_spread"] / s["count"])
                 for p, s in self.stats.items() if s["count"] > 0]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs[:n]


# ════════════════════════════════════════════════
# EXECUTION ANALYZER — FIXED with fallback
# ════════════════════════════════════════════════
class ExecutionAnalyzer:
    @staticmethod
    def calculate_max_safe_size(buy_t: Ticker, sell_t: Ticker,
                               volume_24h: float) -> Tuple[float, float]:
        """
        Расчёт max safe size с гарантированным fallback.
        
        Returns:
            (max_safe_size_usdt, execution_quality_0_100)
        """
        # Метод 1: % от 24h объёма
        if volume_24h > 0:
            max_by_volume = volume_24h * MAX_SAFE_SIZE_PCT_OF_VOLUME
        else:
            max_by_volume = 0
        
        # Метод 2: на основе bid/ask spread
        avg_spread_pct = (buy_t.bid_ask_spread_pct() + sell_t.bid_ask_spread_pct()) / 2
        if avg_spread_pct > 0.01:
            max_by_spread = MAX_SAFE_SIZE_PCT_OF_BID_ASK_SPREAD / avg_spread_pct * MIN_LIQUIDITY_USDT
        else:
            # Очень узкий spread — можно больше
            max_by_spread = MIN_LIQUIDITY_USDT * 10
        
        # Берём минимум из доступных
        candidates = []
        if max_by_volume > 0:
            candidates.append(max_by_volume)
        if max_by_spread > 0:
            candidates.append(max_by_spread)
        candidates.append(MAX_TRADE_SIZE * 3)
        
        if candidates:
            max_safe = min(candidates)
        else:
            max_safe = 0
        
        # ✅ FIX: Гарантируем минимальное значение
        if max_safe < MIN_LIQUIDITY_USDT:
            max_safe = MIN_LIQUIDITY_USDT  # $100 минимум
        
        # Quality score
        if max_safe >= 1000:
            quality = 100
        elif max_safe >= 500:
            quality = 75
        elif max_safe >= 200:
            quality = 50
        elif max_safe >= 100:
            quality = 25
        else:
            quality = 10
        
        return max_safe, quality


# ════════════════════════════════════════════════
# PROFIT CALCULATOR
# ════════════════════════════════════════════════
class ProfitCalculator:
    @staticmethod
    def calculate_slippage(size_usdt: float) -> float:
        return BASE_SLIPPAGE + SLIPPAGE_PER_USDT * size_usdt
    
    @staticmethod
    def calculate_net_profit(spread_pct: float, buy_t: Ticker, sell_t: Ticker,
                            size_usdt: float) -> float:
        """Расчёт чистой прибыли."""
        fee_pct = (
            TRADING_FEES.get(buy_t.exchange, 0.001) +
            TRADING_FEES.get(sell_t.exchange, 0.001)
        ) * 100
        slippage_pct = ProfitCalculator.calculate_slippage(size_usdt) * 100
        withdrawal_pct = (
            (WITHDRAWAL_FEES.get(buy_t.exchange, 1.0) +
             WITHDRAWAL_FEES.get(sell_t.exchange, 1.0))
            / size_usdt * 100
        )
        avg_volatility = (abs(buy_t.percentage) + abs(sell_t.percentage)) / 2
        drift_pct = avg_volatility * VOLATILITY_DRIFT_FACTOR
        return spread_pct - fee_pct - slippage_pct - withdrawal_pct - drift_pct
    
    @staticmethod
    def calculate_breakdown(spread_pct: float, buy_t: Ticker, sell_t: Ticker,
                           size_usdt: float) -> Dict[str, float]:
        """Полный breakdown всех расходов."""
        fee_pct = (
            TRADING_FEES.get(buy_t.exchange, 0.001) +
            TRADING_FEES.get(sell_t.exchange, 0.001)
        ) * 100
        slippage_pct = ProfitCalculator.calculate_slippage(size_usdt) * 100
        withdrawal_pct = (
            (WITHDRAWAL_FEES.get(buy_t.exchange, 1.0) +
             WITHDRAWAL_FEES.get(sell_t.exchange, 1.0))
            / size_usdt * 100
        )
        avg_volatility = (abs(buy_t.percentage) + abs(sell_t.percentage)) / 2
        drift_pct = avg_volatility * VOLATILITY_DRIFT_FACTOR
        net_pct = spread_pct - fee_pct - slippage_pct - withdrawal_pct - drift_pct
        return {
            "spread": spread_pct, "fee": fee_pct,
            "slippage": slippage_pct, "withdrawal": withdrawal_pct,
            "drift": drift_pct, "net": net_pct,
        }


# ════════════════════════════════════════════════
# CONFIDENCE SCORER
# ════════════════════════════════════════════════
class ConfidenceScorer:
    def __init__(self, pair_ranker: ExchangePairRanker, lifetime_tracker: LifetimeTracker):
        self.pair_ranker = pair_ranker
        self.lifetime_tracker = lifetime_tracker
    
    def get_category(self, symbol: str) -> str:
        base = symbol.split("/")[0]
        for cat, tokens in CORRELATED_CATEGORIES.items():
            if base in tokens:
                return cat
        return "other"
    
    def score(self, opp: Opportunity, buy_t: Ticker, sell_t: Ticker,
              balance_manager: Optional["BalanceManager"] = None,
              funding_tracker: Optional["FundingRateTracker"] = None) -> Tuple[float, Dict[str, float], str]:
        """Возвращает (total_score, breakdown, category)."""
        breakdown = {}
        
        # 1. Spread (0-100): 5% spread = 100, 0.5% = 10
        spread_score = min(100, opp.spread_pct * 20)
        breakdown["spread"] = spread_score * CONFIDENCE_WEIGHTS["spread"] / 100
        
        # 2. Liquidity
        avg_liquidity_spread = (buy_t.bid_ask_spread_pct() + sell_t.bid_ask_spread_pct()) / 2
        liquidity_score = max(0, 100 - avg_liquidity_spread * 50)
        breakdown["liquidity"] = liquidity_score * CONFIDENCE_WEIGHTS["liquidity"] / 100
        
        # 3. Volume
        vol_score = min(100, max(0, opp.volume / 10_000))
        breakdown["volume"] = vol_score * CONFIDENCE_WEIGHTS["volume"] / 100
        
        # 4. History
        hist_score = self.lifetime_tracker.get_repeatability(
            opp.symbol, f"{opp.buy_exchange}->{opp.sell_exchange}"
        )
        breakdown["history"] = hist_score * CONFIDENCE_WEIGHTS["history"] / 100
        
        # 5. Exchange rank
        ex_score = self.pair_ranker.get_pair_score(opp.buy_exchange, opp.sell_exchange)
        breakdown["exchange"] = ex_score * CONFIDENCE_WEIGHTS["exchange"] / 100
        
        # 6. Execution quality (max safe size)
        max_safe, exec_quality = ExecutionAnalyzer.calculate_max_safe_size(
            buy_t, sell_t, opp.volume
        )
        opp.max_safe_size_usdt = max_safe
        opp.execution_quality = exec_quality
        breakdown["execution"] = exec_quality * CONFIDENCE_WEIGHTS["execution"] / 100
        
        # 7. Age
        if opp.first_seen > 0:
            lifetime = time.time() - opp.first_seen
            if 0 < lifetime < 120:
                age_score = min(100, lifetime * 2)
            else:
                age_score = 50
        else:
            age_score = 30
        breakdown["age"] = age_score * CONFIDENCE_WEIGHTS["age"] / 100
        
        # 8. Freshness
        # A0.6: если хотя бы один тикер без честного биржевого timestamp,
        # мы не можем утверждать высокую свежесть — это была бы уверенность
        # на пустом месте (age_sec() тут меряет только "давно ли МЫ
        # опросили", а не "насколько свежа цена на самой бирже").
        max_age = max(buy_t.age_sec(), sell_t.age_sec())
        # КАЛИБРОВКА (Шаг 0.3, 2026-09-30).
        #
        # Раньше: freshness_score = max(0, 100 - max_age * 15)
        # При CACHE_REFRESH_SEC=3 коэффициент 15/сек обнуляет оценку уже
        # через 6.7 секунды, тогда как нормальный возраст котировки между
        # обновлениями кэша — 1.5-3 секунды. То есть компонент гас по
        # замыслу, а не из-за реальной расхожести цены: терялось ~5 баллов
        # на каждой сделки.
        #
        # Теперь шкала привязана к интервалу обновления кэша:
        #   возраст = 2 x CACHE_REFRESH_SEC -> 50 баллов
        #   возраст = 4 x CACHE_REFRESH_SEC -> 0 баллов
        # Границы не выдуманы: при 3-секундном цикле 6 секунд — это
        # два пропущенных обновления (цена объективно устарела), а
        # 12 секунд — четыре.
        age_per_50 = max(1.0, CACHE_REFRESH_SEC * 2)
        freshness_score = max(0.0, 100.0 - max_age * (50.0 / age_per_50))
        if not (buy_t.has_exchange_timestamp and sell_t.has_exchange_timestamp):
            freshness_score = min(freshness_score, 50)
        breakdown["freshness"] = freshness_score * CONFIDENCE_WEIGHTS["freshness"] / 100
        
        # 9. Balance routing
        routing_score = 50
        if balance_manager:
            buy_free = balance_manager.free(opp.buy_exchange, "USDT")
            sell_free = balance_manager.free(opp.sell_exchange, "USDT")
            total_free = buy_free + sell_free
            if total_free > 0:
                buy_ratio = buy_free / total_free
                routing_score = buy_ratio * 100
                routing_score = max(0, min(100, routing_score))
            else:
                routing_score = 50
        breakdown["routing"] = routing_score * CONFIDENCE_WEIGHTS.get("routing", 5) / 100
        
        # 10. Funding rate
        funding_score = 50
        if funding_tracker:
            penalty = funding_tracker.get_funding_penalty(opp.buy_exchange, opp.sell_exchange)
            funding_score = max(0, min(100, 50 - penalty * 50))
        breakdown["funding"] = funding_score * CONFIDENCE_WEIGHTS.get("funding", 5) / 100
        
        # Set ages
        opp.buy_ticker_age = buy_t.age_sec()
        opp.sell_ticker_age = sell_t.age_sec()
        
        total = sum(breakdown.values())
        category = self.get_category(opp.symbol)
        return round(total, 1), breakdown, category


@dataclass
class OrderValidationResult:
    valid: bool
    reason: str = "ok"
    min_notional: float = 0.0
    step_size: float = 0.0
    min_qty: float = 0.0


class OrderValidator:
    """Проверяет ордер на соответствие правилам биржи (min notional, step size, precision).

    Использует market-метаданные из ccxt (уже загружены при load_markets).
    """

    @staticmethod
    def check(pool: "ExchangePool", exchange: str, symbol: str,
              size_usdt: float, price: float) -> OrderValidationResult:
        client = pool.clients.get(exchange)
        if not client:
            return OrderValidationResult(False, f"no_client_{exchange}")
        market = client.markets.get(symbol)
        if not market:
            return OrderValidationResult(False, "symbol_not_found")
        # Неактивный рынок отбрасывается НЕ сразу, а только если не
        # выполнены минимальные лимиты. ccxt помечает active=False у пар
        # вне торговых сессий, которые тем не менее принимают ордера.
        # Требование тестов:
        #   active=False + лимиты выполнены -> valid
        #   active=False + лимиты НЕ выполнены -> market_inactive
        inactive = not market.get("active", True)

        limits = market.get("limits", {})
        precision = market.get("precision", {})

        min_notional = 0.0
        step_size = 0.0
        min_qty = 0.0

        # Для неактивного рынка любой провал лимитов объясняется
        # самим фактом неактивности (биржа не примет ордер), поэтому
        # причина сообщается market_inactive, а не частной причиной
        # вроде below_min_notional — так видно в логе, ЧТО мешает.
        def _fail(reason):
            return OrderValidationResult(
                False, "market_inactive" if inactive else reason,
                min_notional, step_size, min_qty)

        cost_limits = limits.get("cost", {})
        if cost_limits:
            min_notional = cost_limits.get("min", 0) or 0
            qty = size_usdt / max(price, 1e-12)
            if min_notional > 0 and size_usdt < min_notional:
                return _fail("below_min_notional")

        amount_limits = limits.get("amount", {})
        if amount_limits:
            min_qty = amount_limits.get("min", 0) or 0
            step_size = amount_limits.get("step", 0) or 0
            qty = size_usdt / max(price, 1e-12)
            if min_qty > 0 and qty < min_qty:
                return _fail("below_min_qty")
            if step_size > 0 and step_size > qty * 0.01:
                return _fail("step_too_coarse")

        price_limits = limits.get("price", {})
        if price_limits:
            min_price = price_limits.get("min", 0) or 0
            if min_price > 0 and price < min_price:
                return _fail("below_min_price")

        return OrderValidationResult(True, "ok", min_notional, step_size, min_qty)


# ════════════════════════════════════════════════
# PRICE CACHE
# ════════════════════════════════════════════════
# PRICE CACHE
# ════════════════════════════════════════════════
class PriceCache:
    def __init__(self):
        self.prices: Dict[str, Dict[str, Ticker]] = {}
        self.futures_prices: Dict[str, Dict[str, Ticker]] = {}
        self.volumes: Dict[str, Dict[str, float]] = {}
        self.last_update: Dict[str, float] = {}
        self.last_futures_update: Dict[str, float] = {}
        self.lock = asyncio.Lock()
        self.update_duration: float = 0
    
    async def update_one(self, pool, ex_name: str):
        client = pool.clients.get(ex_name)
        if not client:
            return 0
        try:
            async with pool.get_semaphore(ex_name):
                all_tickers = await asyncio.wait_for(
                    client.fetch_tickers(), timeout=EXCHANGE_TIMEOUT_SEC
                )
            new_prices, new_volumes = {}, {}
            for symbol, data in all_tickers.items():
                if "/USDT" not in symbol or ":USDT" in symbol:
                    continue
                ticker = Ticker.from_ccxt(ex_name, symbol, data)
                if ticker:
                    new_prices[symbol] = ticker
                    if ticker.volume > 0:
                        new_volumes[symbol] = ticker.volume
            async with self.lock:
                for sym, ticker in new_prices.items():
                    self.prices.setdefault(sym, {})[ex_name] = ticker
                for sym, vol in new_volumes.items():
                    self.volumes.setdefault(sym, {})[ex_name] = vol
                self.last_update[ex_name] = time.time()
        except asyncio.TimeoutError:
            logger.debug(f"⏱ {ex_name}: timeout")
        except Exception as e:
            logger.debug(f"⚠️ {ex_name}: {str(e)[:50]}")
        return 0
    
    async def update_all(self, pool):
        start = time.monotonic()
        tasks = [self.update_one(pool, ex) for ex in pool.clients]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.update_duration = time.monotonic() - start
    
    async def update_futures_one(self, pool, ex_name: str):
        client = pool.futures_clients.get(ex_name)
        if not client:
            return 0
        try:
            async with pool.get_semaphore(ex_name):
                all_tickers = await asyncio.wait_for(
                    client.fetch_tickers(), timeout=EXCHANGE_TIMEOUT_SEC
                )
            new_prices = {}
            for symbol, data in all_tickers.items():
                if not data:
                    continue
                # ВОССТАНОВЛЕНО (Шаг 0.5, 2026-09-30).
                #
                # Раньше стояло:
                #     if "/USDT" not in symbol or ":USDT" in symbol: continue
                # то есть перпетуалы вида "BTC/USDT:USDT" ОТБРАСЫВАЛИСЬ.
                #
                # Проверено на живых данных: bybit futures отдаёт 896 тикеров,
                # из них 782 перпетуала и 0 обычных спотовых. То есть фильтр
                # отсекал 100% фьючерсных котировок, futures_prices оставался
                # ПУСТЫМ, и стратегия spot-long / futures-short не имела
                # второй ноги: ни одной сделки закрыться не могло.
                #
                # Перпетуалы и есть тот инструмент, который нужен для
                # арбитража против спота, поэтому ":USDT" теперь норма,
                # а не признак отбраковки. Спотовые символы (без ":USDT")
                # на фьючерсных клиентах тоже допускаются.
                if "/USDT" not in symbol:
                    continue
                ticker = Ticker.from_ccxt(ex_name, symbol, data)
                if ticker:
                    new_prices[symbol] = ticker
            async with self.lock:
                for sym, ticker in new_prices.items():
                    self.futures_prices.setdefault(sym, {})[ex_name] = ticker
                self.last_futures_update[ex_name] = time.time()
        except Exception:
            pass
        return 0
    
    async def update_futures_all(self, pool):
        tasks = [self.update_futures_one(pool, ex) for ex in pool.futures_clients]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def get_prices(self, symbols: List[str]) -> Dict[str, Dict[str, Ticker]]:
        async with self.lock:
            out = {}
            for sym in symbols:
                if sym in self.prices:
                    fresh = {ex: t for ex, t in self.prices[sym].items() if not t.is_stale()}
                    if len(fresh) >= 2:
                        out[sym] = fresh
            return out
    
    def get_cache_age(self) -> float:
        if not self.last_update:
            return 0
        return time.time() - min(self.last_update.values())


# ════════════════════════════════════════════════
# EXCHANGE POOL
# ════════════════════════════════════════════════
class ExchangePool:
    def __init__(self):
        self.clients: Dict[str, ccxt.Exchange] = {}  # spot
        self.futures_clients: Dict[str, ccxt.Exchange] = {}  # swap/future
        self.symbols_per_exchange: Dict[str, Set[str]] = {}
        self.futures_symbols_per_exchange: Dict[str, Set[str]] = {}
        self.failed: Set[str] = set()
        self.futures_exchanges: Set[str] = set()  # names that have futures
        self.hunter_symbols: List[str] = []
        # A1.6 FIX: RATE_LIMITS в конфиге был объявлен и нигде не использовался
        # (мёртвый конфиг, создающий ложное чувство контроля). ccxt's
        # enableRateLimit уже темпово ограничивает ПОСЛЕДОВАТЕЛЬНЫЕ вызовы на
        # одном инстансе биржи, но НЕ ограничивает число ОДНОВРЕМЕННО
        # выполняющихся запросов, если несколько разных задач (спот-тикеры,
        # фьючерс-тикеры, L2-стакан, funding rate) параллельно обращаются к
        # одному и тому же клиенту через asyncio.gather. Семафор ниже — это
        # именно потолок конкурентности per-exchange, а не замена ccxt's
        # темпо-регулятора (они решают разные задачи и не конфликтуют).
        self.semaphores: Dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(max(1, int(RATE_LIMITS.get(name, 5))))
            for name in EXCHANGES
        }

    def get_semaphore(self, name: str) -> asyncio.Semaphore:
        if name not in self.semaphores:
            self.semaphores[name] = asyncio.Semaphore(5)
        return self.semaphores[name]
    
    async def init(self) -> bool:
        tasks = [self._connect(name) for name in EXCHANGES]
        await asyncio.gather(*tasks, return_exceptions=True)
        if len(self.clients) < 2:
            return False
        msg = f"📡 Подключено {len(self.clients)}/{len(EXCHANGES)} бирж"
        if self.failed:
            msg += f" | Failed: {', '.join(self.failed)}"
        logger.info(msg)
        # Подключаем futures клиентов для всех живых бирж
        fut_tasks = [self._connect_futures(name) for name in list(self.clients.keys())]
        await asyncio.gather(*fut_tasks, return_exceptions=True)
        if self.futures_clients:
            logger.info(f"📡 Futures: {len(self.futures_clients)} бирж")
        total = sum(len(s) for s in self.symbols_per_exchange.values())
        logger.info(f"📊 Символов: {total} пар, {len(self.symbols_per_exchange)} уникальных")
        return True
    
    async def _connect(self, name: str):
        ex = None
        try:
            ex = getattr(ccxt, name)({
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {"defaultType": "spot", "fetchCurrencies": False},
            })
            await asyncio.wait_for(ex.load_markets(), timeout=25)
            if not ex.markets:
                self.failed.add(name)
                return
            self.clients[name] = ex
            for symbol in ex.markets:
                if "/USDT" in symbol and ":USDT" not in symbol:
                    self.symbols_per_exchange.setdefault(symbol, set()).add(name)
            logger.info(f"✅ {name}: {len([s for s in ex.markets if '/USDT' in s])} пар")
        except Exception as e:
            logger.warning(f"⚠️ {name}: {str(e)[:50]}")
            self.failed.add(name)
        finally:
            if ex is not None and name not in self.clients:
                try:
                    await ex.close()
                except Exception:
                    pass
    
    async def _connect_futures(self, name: str):
        """Попытка создать futures/swap клиент."""
        try:
            if not hasattr(ccxt, name):
                return
            ex = getattr(ccxt, name)({
                "enableRateLimit": True,
                "timeout": 20000,
                # ВОССТАНОВЛЕНО (Шаг 0.5): было defaultType="future".
            # Проверено перебором на живых данных:
            #   mexc future -> fetch_tickers() = None (падает)
            #   mexc swap   -> 1210 тикеров, 1093 перпетуала
            #   okx   future -> 254 тикера, 0 с /USDT (только деривативы
            #                   вида SOL/USD:USD-261030 - календарные
            #                   фьючерсы, не перпетуалы)
            #   okx   swap   -> 494 тикера, 479 перпетуалов
            # Для арбитража против спота нужен ПЕРПЕТУАЛ: у календарного
            # фьючерса другая дата экспирации и другое имя символа.
            "options": {"defaultType": "swap", "fetchCurrencies": False},
            })
            await asyncio.wait_for(ex.load_markets(), timeout=15)
            if not ex.markets:
                await ex.close()
                return
            self.futures_clients[name] = ex
            self.futures_exchanges.add(name)
            for symbol in ex.markets:
                # ВОССТАНОВЛЕНО (Шаг 0.5): условие было
                #   if "/USDT" in symbol and ":USDT" not in symbol:
                # то есть перпетуалы вида "SOL/USDT:USDT" ОТБРАСЫВАЛИСЬ.
                #
                # Последствие: futures_symbols_per_exchange содержал только
                # спотовые имена, а update_futures_one клал в кэш ключи с
                # ":USDT". Расхождение имён означало, что resolve_futures_symbol
                # не находил пару для mexc/okx/gate — в живом прогоне 36
                # возможностей, из них 0 котировок второй ноги.
                #
                # Перпетуал - это и есть тот инструмент, который нужен для
                # арбитража против спота, поэтому ":USDT" теперь норма.
                if "/USDT" in symbol:
                    self.futures_symbols_per_exchange.setdefault(
                        symbol, set()).add(name)
            logger.info(f"✅ {name} futures: {len([s for s in ex.markets if '/USDT' in s])} пар")
        except Exception:
            pass

    def has_futures(self, name: str) -> bool:
        return name in self.futures_clients

    def get_client(self, name: str, market_type: str = "spot") -> Optional[ccxt.Exchange]:
        if market_type == "futures":
            return self.futures_clients.get(name)
        return self.clients.get(name)
    
    async def reconnect(self, name: str, market_type: str = "spot") -> bool:
        if market_type == "futures":
            if name in self.futures_clients:
                try:
                    await self.futures_clients[name].close()
                except Exception:
                    pass
                del self.futures_clients[name]
            self.futures_exchanges.discard(name)
            await self._connect_futures(name)
            return name in self.futures_clients
        else:
            if name in self.clients:
                try:
                    await self.clients[name].close()
                except Exception:
                    pass
                del self.clients[name]
            self.failed.discard(name)
            await self._connect(name)
            return name in self.clients
    
    def is_connected(self, name: str, market_type: str = "spot") -> bool:
        if market_type == "futures":
            return name in self.futures_clients and name not in self.failed
        return name in self.clients and name not in self.failed
    
    async def close(self):
        for ex in self.clients.values():
            try:
                await ex.close()
            except Exception:
                pass
        for ex in self.futures_clients.values():
            try:
                await ex.close()
            except Exception:
                pass
    
    def update_hunter_symbols(self, symbols: List[str]):
        self.hunter_symbols = symbols


# ════════════════════════════════════════════════
# OPPORTUNITY FINDER
# ════════════════════════════════════════════════
class OpportunityFinder:
    def __init__(self, confidence_scorer: ConfidenceScorer):
        self.scorer = confidence_scorer
        self.calc = ProfitCalculator()
    
    def find(self, symbol: str, prices: Dict[str, Ticker],
             stats: PipelineStats, skip_zombies: Set[str] = None,
             balance_manager: Optional["BalanceManager"] = None,
             funding_tracker: Optional["FundingRateTracker"] = None) -> List[Opportunity]:
        stats.total_symbols += 1
        if skip_zombies and symbol in skip_zombies:
            stats.zombie_skipped += 1
            return []
        if len(prices) < 2:
            return []
        valid = {n: t for n, t in prices.items() if not t.is_stale()}
        if len(valid) < 2:
            stats.stale_skipped += 1
            return []
        
        for t in valid.values():
            if t.last < MIN_PRICE or t.last > MAX_PRICE:
                stats.anomalies_skipped += 1
                return []
            if t.bid < MIN_PRICE or t.ask < MIN_PRICE:
                stats.anomalies_skipped += 1
                return []
        
        prices_list = [t.last for t in valid.values()]
        max_p, min_p = max(prices_list), min(prices_list)
        if min_p > 0:
            price_diff = (max_p - min_p) / min_p * 100
            if price_diff > MAX_PRICE_DIFF_PERCENT:
                stats.anomalies_skipped += 1
                return []
        
        opps = []
        items = list(valid.items())
        for buy_name, buy_t in items:
            for sell_name, sell_t in items:
                if buy_name == sell_name:
                    continue
                if sell_t.bid <= buy_t.ask:
                    continue
                
                spread = ((sell_t.bid - buy_t.ask) / buy_t.ask) * 100
                if spread > stats.best_spread_pct:
                    stats.best_spread_pct = spread
                    stats.best_spread_symbol = symbol
                    stats.best_spread_path = f"{buy_name}->{sell_name}"
                
                if spread < MIN_SPREAD_PERCENT:
                    stats.spread_low_skipped += 1
                    continue
                if spread > MAX_SPREAD_PERCENT:
                    stats.anomalies_skipped += 1
                    stats.spread_high_skipped += 1
                    continue
                
                pair_vol = min(buy_t.volume, sell_t.volume)
                # ТАВТОЛОГИЯ УБРАНА. Было:
                #   if pair_vol < MIN_VOLUME_USDT or pair_vol < MIN_TRADEABLE_VOLUME
                # т.е. 10000 < x ИЛИ 5000 < x. Второе условие строже первого
                # и всегда истинно, когда истинно первое, поэтому проверка
                # сводилась к MIN_VOLUME_USDT, а MIN_TRADEABLE_VOLUME был
                # мёртвым. Оставлен один осмысленный порог.
                if pair_vol < MIN_VOLUME_USDT:
                    stats.volume_skipped += 1
                    continue
                if pair_vol > MAX_VOLUME_USDT:
                    stats.volume_skipped += 1
                    continue
                stats.after_volume += 1

                stats.spreads_found += 1
                max_safe, _ = ExecutionAnalyzer.calculate_max_safe_size(buy_t, sell_t, pair_vol)
                test_size = min(MAX_TRADE_SIZE, max(MIN_TRADE_SIZE, max_safe))
                breakdown = self.calc.calculate_breakdown(spread, buy_t, sell_t, test_size)
                net_pct = breakdown["net"]
                
                if net_pct < 0:
                    stats.net_negative += 1
                    continue
                stats.after_fees += 1
                if net_pct < MIN_NET_PROFIT_PERCENT:
                    stats.net_low_skipped += 1
                    continue
                stats.profitable += 1
                
                temp_opp = Opportunity(
                    symbol=symbol, buy_exchange=buy_name, sell_exchange=sell_name,
                    buy_price=buy_t.ask, sell_price=sell_t.bid,
                    spread_pct=spread, net_profit_pct=net_pct, volume=pair_vol,
                    exchanges_count=len(valid), timestamp=int(time.time()),
                )
                confidence, conf_breakdown, category = self.scorer.score(temp_opp, buy_t, sell_t, balance_manager=balance_manager)
                if confidence > stats.best_confidence:
                    stats.best_confidence = confidence
                
                opp = Opportunity(
                    symbol=symbol, buy_exchange=buy_name, sell_exchange=sell_name,
                    buy_price=buy_t.ask, sell_price=sell_t.bid,
                    spread_pct=round(spread, 3), net_profit_pct=round(net_pct, 3),
                    volume=pair_vol, exchanges_count=len(valid),
                    timestamp=int(time.time()),
                    confidence_score=confidence,
                    confidence_breakdown=conf_breakdown,
                    category=category,
                    fee_pct=breakdown["fee"],
                    slippage_pct=breakdown["slippage"],
                    withdrawal_pct=breakdown["withdrawal"],
                    drift_pct=breakdown["drift"],
                    max_safe_size_usdt=temp_opp.max_safe_size_usdt,
                    execution_quality=temp_opp.execution_quality,
                )
                opps.append(opp)
        
        # ── 3-leg route check ────────────────────────
        if len(valid) >= 3:
            route_opp = self._best_route3(symbol, valid, stats)
            if route_opp:
                # Only add if better than best direct
                best_direct_net = max((o.net_profit_pct for o in opps), default=0)
                if route_opp.net_profit_pct > best_direct_net * 1.05:  # >5% better
                    opps.append(route_opp)
        
        return opps

    def _best_route3(self, symbol: str, valid: Dict[str, Ticker],
                     stats: PipelineStats) -> Optional[Opportunity]:
        """Ищет маршрут A→B→C, выгоднее лучшего прямого."""
        names = list(valid.keys())
        best_net = 0.0
        best_route = None
        
        for i, buy_name in enumerate(names):
            buy_t = valid[buy_name]
            for j, mid_name in enumerate(names):
                if mid_name == buy_name:
                    continue
                mid_t = valid[mid_name]
                if mid_t.bid <= buy_t.ask:  # first leg unprofitable
                    continue
                for k, sell_name in enumerate(names):
                    if sell_name == buy_name or sell_name == mid_name:
                        continue
                    sell_t = valid[sell_name]
                    if sell_t.bid <= mid_t.ask:  # second leg unprofitable
                        continue
                    
                    # Combined return: buy on buy_name, sell on mid_name, buy on mid_name, sell on sell_name
                    # Return = (mid_bid / buy_ask) * (sell_bid / mid_ask) - 1
                    leg1_factor = mid_t.bid / buy_t.ask
                    leg2_factor = sell_t.bid / mid_t.ask
                    total_factor = leg1_factor * leg2_factor
                    if total_factor <= 1.0:
                        continue
                    
                    spread = (total_factor - 1.0) * 100
                    # A0.2 FIX: было buy_name*2 (должно быть *1 — на buy_name
                    # происходит только ОДНА операция, покупка в leg1). Дважды
                    # платит комиссию именно mid_name (продажа в конце leg1 +
                    # покупка в начале leg2), а не buy_name.
                    fees_pct = (
                        TRADING_FEES.get(buy_name, 0.001) * 1 +   # buy на leg1
                        TRADING_FEES.get(mid_name, 0.001) * 2 +   # sell leg1 + buy leg2
                        TRADING_FEES.get(sell_name, 0.001) * 1    # sell на leg2
                    ) * 100
                    
                    slippage = ProfitCalculator.calculate_slippage(1000) * 100  # estimated
                    withdrawal = (
                        (WITHDRAWAL_FEES.get(buy_name, 1.0) + 
                         WITHDRAWAL_FEES.get(mid_name, 1.0) +
                         WITHDRAWAL_FEES.get(sell_name, 1.0)) / 1000 * 100
                    )
                    net_pct = spread - fees_pct - slippage - withdrawal
                    
                    if net_pct > best_net:
                        best_net = net_pct
                        best_route = (buy_name, mid_name, sell_name, buy_t, mid_t, sell_t, spread, net_pct, fees_pct, slippage, withdrawal)
        
        if best_route and best_net > MIN_NET_PROFIT_PERCENT:
            buy_name, mid_name, sell_name, buy_t, mid_t, sell_t, spread, net_pct, fees_pct, slippage, withdrawal = best_route
            pair_vol = min(buy_t.volume, mid_t.volume, sell_t.volume)
            if pair_vol < MIN_VOLUME_USDT:
                return None
            
            # Calculate confidence for route
            temp_opp = Opportunity(
                symbol=symbol, buy_exchange=buy_name, sell_exchange=sell_name,
                buy_price=buy_t.ask, sell_price=sell_t.bid,
                spread_pct=round(spread, 3), net_profit_pct=round(net_pct, 3),
                volume=pair_vol, exchanges_count=len(valid),
                timestamp=int(time.time()),
                mid_exchange=mid_name, route_type="route3",
            )
            confidence, conf_breakdown, category = self.scorer.score(
                temp_opp, buy_t, sell_t,
                balance_manager=None, funding_tracker=None
            )
            if confidence < REALITY_MIN_CONFIDENCE:
                return None
            
            return Opportunity(
                symbol=symbol, buy_exchange=buy_name, sell_exchange=sell_name,
                buy_price=buy_t.ask, sell_price=sell_t.bid,
                spread_pct=round(spread, 3), net_profit_pct=round(net_pct, 3),
                volume=pair_vol, exchanges_count=len(valid),
                timestamp=int(time.time()),
                confidence_score=confidence,
                confidence_breakdown=conf_breakdown,
                category=category,
                fee_pct=round(fees_pct, 3),
                slippage_pct=round(slippage, 3),
                withdrawal_pct=round(withdrawal, 3),
                max_safe_size_usdt=temp_opp.max_safe_size_usdt or 0,
                execution_quality=temp_opp.execution_quality or 0,
                mid_exchange=mid_name, route_type="route3",
            )
        return None


# ════════════════════════════════════════════════
# RISK MANAGER
# ════════════════════════════════════════════════
class RiskManager:
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        # A0.5 FIX: раньше лок создавался, но нигде не использовался (dead code) —
        # ни один из execute() не оборачивал в него критическую секцию
        # can_trade -> reserve -> apply -> record_trade. При текущем
        # последовательном исполнении (одна сделка за цикл) это не проявлялось,
        # но как только исполнение станет параллельным (несколько
        # непересекающихся возможностей за цикл — см. execute_batch в app.py),
        # без этого лока check-then-reserve по разным opportunities мог бы
        # гоняться за одним и тем же free-балансом. Переименован в публичный
        # `lock` (не `_execution_lock`), так как явно используется извне
        # (PaperTrader/SpotFuturesPaperTrader).
        self.lock = asyncio.Lock()
    
    def calculate_position_size(self, opp: Opportunity = None, max_safe_size: float = 0) -> float:
        p = self.portfolio
        # A2.2 FIX (уточнение): нужно различать "Kelly реально посчитан и
        # говорит торговать МАЛО/НОЛЬ" (это и есть цель A2.2 — пропускать
        # как есть) от "данных ещё недостаточно, используем дефолтный
        # bootstrap-размер" (это НЕ решение Kelly, это заглушка на время
        # накопления истории, и её не должен утаскивать вниз множитель
        # уверенности — иначе бот вообще не сможет наторговать себе
        # историю для Kelly, что и произошло при первом прогоне теста
        # после этой правки: MIN_TRADE_SIZE * conf_mult(<1.0) < MIN_TRADE_SIZE,
        # и сделка отклонялась на самом первом шаге).
        kelly_computed = False
        if len(p.closed_trades) < KELLY_MIN_TRADES:
            base_size = MIN_TRADE_SIZE
        else:
            kelly_fraction = KELLY_FRACTION_EARLY if len(p.closed_trades) < KELLY_FULL_TRADES else KELLY_FRACTION
            wins = [t for t in p.recent_trades if t.pnl > 0]
            losses = [t for t in p.recent_trades if t.pnl <= 0]
            if wins and losses:
                win_rate = len(wins) / len(p.recent_trades)
                avg_win = sum(t.pnl_pct for t in wins) / len(wins)
                avg_loss = abs(sum(t.pnl_pct for t in losses) / len(losses))
                if avg_win > 0:
                    kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
                    kelly = max(0, kelly * kelly_fraction)
                    kelly = min(kelly, MAX_POSITION_PCT_OF_BALANCE)
                    base_size = p.balance * kelly
                    kelly_computed = True
                else:
                    base_size = MIN_TRADE_SIZE
            else:
                base_size = MIN_TRADE_SIZE
        
        # Infer confidence multiplier from the opportunity itself
        if opp is not None:
            conf = opp.confidence_score / 100.0
            conf_mult = 0.5 + conf * 0.5
            base_size *= conf_mult
        
        if max_safe_size > 0:
            base_size = min(base_size, max_safe_size)
        
        # Bootstrap-дефолт (Kelly ещё не считался) не должен уходить ниже
        # порога исполнения из-за множителя уверенности — это фиксированный
        # разведочный размер, а не риск-адаптивное решение. А вот реально
        # посчитанный Kelly (kelly_computed=True) — пропускаем как есть,
        # вплоть до нуля, именно это и есть цель A2.2.
        if not kelly_computed:
            base_size = max(base_size, MIN_TRADE_SIZE)
        return min(base_size, MAX_TRADE_SIZE)
    
    def get_confidence_threshold(self) -> float:
        """
        Порог уверенности, зависящий от зрелости системы.

        Логика: сначала собираем статистику, потом ужесточаем.
        Это НЕ снижение планки навсегда — планка растёт по мере
        накопления закрытых сделок.

        Зачем это нужно: confidence частично определяется историей
        закрытых сделок (повторяемость), а история накапливается только
        совершёнными сделками. При фиксированном пороге 50 система на
        холодном старте не могла сделать ни одной сделки, потому что
        history = 0 у всех возможностей, — то есть порог был
        недостижим в принципе.

        Ступени:
            <  KELLY_MIN_TRADES (20)  -> 40.0  бутстрап
            <  KELLY_FULL_TRADES (100)-> 45.0  рост
            >= KELLY_FULL_TRADES      -> 50.0  зрелость
        """
        n = len(self.portfolio.closed_trades)
        if n < KELLY_MIN_TRADES:
            return 40.0
        if n < KELLY_FULL_TRADES:
            return 45.0
        return 50.0

    def can_trade(self, opp: Opportunity, confidence_min: float = None) -> Tuple[bool, str]:
        p = self.portfolio
        p.check_daily_reset()

        # confidence_min=None означает "взять порог по зрелости системы".
        # Явное число переопределяет (так пишут старые тесты).
        if confidence_min is None:
            confidence_min = self.get_confidence_threshold()

        if opp.confidence_score < confidence_min:
            return False, f"Low conf ({opp.confidence_score:.1f} < {confidence_min})"
        if opp.net_profit_pct < 0:
            return False, "Negative"
        if p.daily_pnl < -(p.initial_balance * MAX_DAILY_LOSS_PERCENT / 100):
            return False, "Daily loss"
        if p.drawdown_pct > MAX_DRAWDOWN_PERCENT:
            return False, "Max DD"
        if MAX_DAILY_TRADES > 0 and p.daily_trades >= MAX_DAILY_TRADES:
            return False, "Daily trades"
        if p.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return False, "Max losses"
        if len(p.positions) >= MAX_OPEN_POSITIONS:
            return False, "Max positions"
        
        category_count = sum(1 for pos in p.positions.values() if pos.get("category") == opp.category)
        if category_count >= MAX_CORRELATED_POSITIONS:
            return False, f"Too many {opp.category}"
        
        base = opp.symbol.split("/")[0]
        if (time.time() - p.last_trade_time.get(base, 0)) < POSITION_COOLDOWN_SEC:
            return False, "Cooldown"
        if p.balance < MIN_BALANCE_REQUIRED:
            return False, "Low balance"
        return True, "OK"
    
    def record_trade(self, trade: TradeResult, category: str = "other"):
        p = self.portfolio
        p.balance += trade.pnl
        p.daily_pnl += trade.pnl
        p.daily_trades += 1
        p.update_peak()
        if trade.pnl < 0:
            p.consecutive_losses += 1
        else:
            p.consecutive_losses = 0
        base = trade.symbol.split("/")[0]
        # A2.3-adjacent FIX: раньше здесь было только `del p.positions[base]`
        # (без единого места, где что-либо в positions добавлялось) — то есть
        # MAX_OPEN_POSITIONS и MAX_CORRELATED_POSITIONS в can_trade() были
        # мертвы (len(p.positions) всегда 0, ни одна из этих проверок не
        # могла сработать никогда). При текущей модели paper-исполнения
        # (мгновенный атомарный round-trip внутри одного execute(), без
        # реального периода "удержания" позиции) единственное осмысленное
        # значение "открытых позиций" — сколько сделок уже исполнено В ЭТОМ
        # ЖЕ цикле сканирования (актуально именно для нового пакетного
        # исполнения из A2.3). Поэтому позиция добавляется здесь и явно
        # очищается в начале каждого нового цикла в app.py — лимиты относятся
        # к пакету сделок одного цикла, а не к "удержанию" во времени,
        # которого в этой архитектуре просто не существует.
        p.positions[base] = {"category": category, "opened_at": time.time()}
        p.last_trade_time[base] = int(time.time())
        p.closed_trades.append(trade)
        p.recent_trades.append(trade)


# ════════════════════════════════════════════════
# PAPER TRADER
# ════════════════════════════════════════════════
class PaperTrader:
    def __init__(self, portfolio: Portfolio, balance_manager: Optional[BalanceManager] = None):
        self.portfolio = portfolio
        self.balance_manager = balance_manager
        self.risk = RiskManager(portfolio)
        self.calc = ProfitCalculator()
    
    async def execute(self, opp: Opportunity, prices: Dict[str, Ticker]) -> Optional[TradeResult]:
        # A0.5 FIX: вся критическая секция (проверка риска -> резервация ->
        # применение -> запись результата) теперь атомарна относительно других
        # конкурентных вызовов execute() на этом же RiskManager/BalanceManager.
        async with self.risk.lock:
            return await self._execute_locked(opp, prices)

    async def _execute_locked(self, opp: Opportunity, prices: Dict[str, Ticker]) -> Optional[TradeResult]:
        can, reason = self.risk.can_trade(opp)
        if not can:
            return None
        buy_t = prices.get(opp.buy_exchange)
        sell_t = prices.get(opp.sell_exchange)
        if not buy_t or not sell_t:
            return None
        if buy_t.ask <= 0 or sell_t.bid <= 0:
            return None
        if sell_t.bid <= buy_t.ask:
            return None
        
        size_usdt = self.risk.calculate_position_size(opp, opp.max_safe_size_usdt)
        size_usdt = min(size_usdt, self.portfolio.balance * MAX_POSITION_PCT_OF_BALANCE)
        size_usdt = min(size_usdt, MAX_TRADE_SIZE)
        
        # ✅ FIX: Проверка баланса
        if size_usdt < MIN_TRADE_SIZE or size_usdt > self.portfolio.balance:
            return None
        
        slip = self.calc.calculate_slippage(size_usdt)
        actual_buy = buy_t.ask * (1 + slip)
        actual_sell = sell_t.bid * (1 - slip)
        if actual_sell <= actual_buy:
            return None
        
        amount = size_usdt / actual_buy
        revenue = amount * actual_sell
        buy_fee = size_usdt * TRADING_FEES.get(opp.buy_exchange, 0.001)
        sell_fee = revenue * TRADING_FEES.get(opp.sell_exchange, 0.001)
        withdrawal = (WITHDRAWAL_FEES.get(opp.buy_exchange, 1.0) +
                     WITHDRAWAL_FEES.get(opp.sell_exchange, 1.0))

        if self.balance_manager:
            base_asset = opp.symbol.split("/")[0]
            buy_total = size_usdt + buy_fee
            if not self.balance_manager.can_buy(opp.buy_exchange, buy_total):
                return None
            if not self.balance_manager.can_sell(opp.sell_exchange, base_asset, amount):
                return None
            if not self.balance_manager.reserve(opp.buy_exchange, "USDT", buy_total):
                return None
            if not self.balance_manager.reserve(opp.sell_exchange, base_asset, amount):
                self.balance_manager.release(opp.buy_exchange, "USDT", buy_total)
                return None

        pnl = revenue - size_usdt - buy_fee - sell_fee - withdrawal
        pnl_pct = (pnl / size_usdt) * 100 if size_usdt > 0 else 0

        if self.balance_manager:
            # A0.4 FIX: apply_buy_fill / apply_sell_fill каждый атомарен сам по
            # себе (либо полностью проходит и сам снимает свою резервацию, либо
            # бросает исключение ДО какой-либо мутации состояния — см. их код).
            # Раньше при падении ВТОРОГО вызова резервация первого леса уже была
            # корректно снята (успешным первым вызовом), а резервация второго
            # леса, который должен был снять её сам, оставалась висеть навсегда,
            # тихо уменьшая свободный баланс биржи при каждом таком отказе.
            # Отслеживаем, какая нога реально прошла, и снимаем резервацию
            # только для той, что не прошла.
            buy_leg_done = False
            try:
                self.balance_manager.apply_buy_fill(
                    opp.buy_exchange, opp.symbol, amount, size_usdt, buy_fee
                )
                buy_leg_done = True
                self.balance_manager.apply_sell_fill(
                    opp.sell_exchange, opp.symbol, amount, revenue, sell_fee
                )
            except Exception as e:
                logger.warning(f"Paper inventory update failed: {e}")
                if not buy_leg_done:
                    self.balance_manager.release(opp.buy_exchange, "USDT", buy_total)
                else:
                    # Купили на buy_exchange, но продать на sell_exchange не
                    # получилось — резервация snapшена, но по факту осталась
                    # база, купленная и нигде не проданная (реальный, не только
                    # "резервационный" рассинхрон). Это самостоятельная, более
                    # глубокая проблема, чем утечка резервации, и заслуживает
                    # громкого сигнала, а не тихого warning.
                    self.balance_manager.release(opp.sell_exchange, base_asset, amount)
                    logger.critical(
                        f"ORPHAN INVENTORY: {opp.symbol} bought on {opp.buy_exchange} "
                        f"({amount:.8f}) but sell on {opp.sell_exchange} failed — "
                        f"inventory left unsold, needs manual reconciliation."
                    )
                return None

        result = TradeResult(
            timestamp=int(time.time()),
            symbol=opp.symbol, buy_exchange=opp.buy_exchange,
            sell_exchange=opp.sell_exchange, amount=amount,
            pnl=pnl, pnl_pct=pnl_pct,
            balance_after=self.portfolio.balance + pnl, success=pnl > 0,
        )
        self.risk.record_trade(result, opp.category)
        return result
    
    def stats(self) -> dict:
        p = self.portfolio
        wins = [t for t in p.closed_trades if t.pnl > 0]
        losses = [t for t in p.closed_trades if t.pnl <= 0]
        max_streak = 0
        streak = 0
        for t in p.closed_trades:
            if t.pnl < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return {
            "balance": p.balance, "pnl": p.balance - p.initial_balance,
            "pnl_pct": (p.balance / p.initial_balance - 1) * 100,
            "total": len(p.closed_trades), "wins": len(wins), "losses": len(losses),
            "winrate": len(wins) / max(len(p.closed_trades), 1) * 100,
            "rolling_wr": p.rolling_win_rate * 100,
            "max_consecutive_losses": max_streak,
            "drawdown_pct": p.drawdown_pct, "peak_equity": p.peak_equity,
        }


# ==========================================
# ОТКРЫТАЯ АРБИТРАЖНАЯ ПАРА (cash-and-carry с удержанием)
# ==========================================
@dataclass
class OpenPair:
    """
    Открытая пара, которая ПЕРЕЖИВАЕТ циклы.

    Зачем. Прежний SpotFuturesPaperTrader открывал обе ноги и закрывал их в
    том же вызове execute() — мгновенный round-trip. При закрытии по цене
    входа это давало PnL = весь спред независимо от рынка (438% «за 44
    часа», WR 99.5%). Даже после исправления на текущие цены мгновенный
    round-trip не стратегия: он не ловит сходимость, а платит двойной
    спред. Настоящая логика — открыть дешёвую и дорогую ногу, ДЕРЖАТЬ,
    дождаться сходимости, закрыть. Значит состояние обязано жить между
    вызовами, а не существовать один такт.
    """
    symbol: str
    buy_exchange: str
    sell_exchange: str
    direction: str          # "fut_premium" | "fut_discount"
    amount: float           # количество базового актива
    size_usdt: float        # размер ноги в USDT
    entry_spot: float
    entry_fut: float
    entry_basis: float      # базис на входе, %
    opened_at: float
    margin: float = 0.0
    # Время удержания ФИКСИРУЕТСЯ при открытии пары.
    #
    # Раньше таймаут жил в трейдере (self.max_hold_hours) и был общим на
    # все пары. При трёх одновременных позициях открытие второй пары
    # переписывало таймаут первой — в том числе самой ценной, с наибольшим
    # базисом. Теперь срок принадлежит паре.
    hold_hours: float = PAPER_HOLD_HOURS_MAX
    # ТОЧНАЯ сумма, зарезервированная на фьючерсной бирже при открытии
    # (margin + входная комиссия). На закрытии освобождается дословно.
    # Без этого поля резерв считается от margin, а освобождение — от
    # margin + exit_fee + funding, и разница навсегда остаётся в резерве.
    fut_reserved: float = 0.0
    transfer_fee: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    bars_held: int = 0
    hours_held: float = 0.0
    notes: str = ""

    def basis_now(self, spot_mid: float, fut_mid: float) -> float:
        if spot_mid <= 0:
            return 0.0
        return (fut_mid - spot_mid) / spot_mid * 100.0


class SpotFuturesPaperTrader(PaperTrader):
    """
    Cash-and-carry (.spot + фьючерс) с УДЕРЖАНИЕМ позиции.

    Логика, которой не было:
      1. Открыть ДЕШЁВУЮ ногу (покупка актива) и ДОРОГУЮ (шорт фьючерса).
      2. ДЕРЖАТЬ обе ноги между циклами — состояние живёт в self.open_pairs.
      3. Каждый цикл проверять базис и закрывать, когда он сошёлся
         (|basis| <= target_close_pct) либо сработал стоп/таймаут.
      4. PnL считается ТОЛЬКО на закрытии, по фактическим ценам обеих ног.

    Прежняя версия закрывала ноги в том же вызове, причём по цене входа —
    это давало PnL = весь спред независимо от рынка (438%, WR 99.5%).
    """
    def __init__(self, portfolio: Portfolio, balance_manager: BalanceManager,
                 futures_leverage: float = FUTURES_LEVERAGE,
                 hold_hours: float = PAPER_HOLD_HOURS,
                 funding_rate_per_hour: float = FUTURES_FUNDING_RATE_PER_HOUR,
                 shadow_logger: Optional["ShadowLogger"] = None,
                 min_latency_ms: float = 0.0,
                 max_latency_ms: float = 0.0,
                 latency_drift_bps_per_sec: float = 0.0,
                 api_failure_prob: float = 0.0,
                 available_liquidity_pct: float = 1.0,
                 min_fill_ratio: float = 0.0,
                 target_close_pct: float = 0.15,
                 stop_widen_pct: float = 1.5,
                 max_hold_hours: float = 4.0,
                 max_open_pairs: int = 3,
                 cooldown_sec: float = 300.0):
        super().__init__(portfolio, balance_manager=balance_manager)
        self.futures_leverage = max(futures_leverage, 1e-9)
        self.hold_hours = max(hold_hours, 0.0)
        self.funding_rate_per_hour = funding_rate_per_hour
        self.shadow_logger = shadow_logger
        lo = max(0.0, float(min_latency_ms))
        self.min_latency_ms = lo
        self.max_latency_ms = max(lo, float(max_latency_ms))
        self.latency_drift_bps_per_sec = max(0.0, float(latency_drift_bps_per_sec))
        self.api_failure_prob = min(max(0.0, float(api_failure_prob)), 1.0)
        self.available_liquidity_pct = min(max(0.0, float(available_liquidity_pct)), 1.0)
        self.min_fill_ratio = min(max(0.0, float(min_fill_ratio)), 1.0)
        self.target_close_pct = max(0.0, float(target_close_pct))
        self.stop_widen_pct = max(0.0, float(stop_widen_pct))
        # Таймаут в ЧАСАХ. Прежний max_hold_bars измерялся в циклах, а цикл при
        # SCAN_INTERVAL_SEC=2 — это не «бар», а полсекунды; позиция закрывалась
        # быстрее, чем успевала сойтись. Время — единственная единица, которая
        # не зависит от частоты сканирования.
        self.max_hold_hours = max(0.01, float(max_hold_hours))
        # Ограничение числа ОДНОВРЕМЕННЫХ пар. Без него manage_open_positions
        # мог держать позиции, а execute() открывал новые сверх лимита портфеля
        # (MAX_OPEN_POSITIONS применялся к закрытым сделкам, а не к живым ногам).
        self.max_open_pairs = max(1, int(max_open_pairs))
        # Пауза после закрытия по символу: без неё бот входит в тот же
        # актив снова на следующем же цикле и платит двойные комиссии.
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self._closed_at: Dict[str, float] = {}
        self._sim_rng = random.Random(0xBEEF)
        self.rejected_counters: Dict[str, int] = {}
        # ОТКРЫТЫЕ ПАРЫ — то, чего не хватало. Переживают циклы.
        self.open_pairs: Dict[str, OpenPair] = {}

    @staticmethod
    def resolve_futures_symbol(futures_prices: Optional[Dict[str, Any]],
                               spot_symbol: str) -> Optional[str]:
        """
        Находит ключ перпетуального фьючерса для спотового символа.

        ЗАЧЕМ (Шаг 0.3, 2026-09-30): спот и перпетуал именуются по-разному:
            спот:        "SOL/USDT"
            перпетуал:   "SOL/USDT:USDT"
        Раньше execute() искал futures_prices[opp.symbol] напрямую, то есть
        "SOL/USDT" в словаре, где лежат только ":USDT". Проверено на живых
        данных: точное совпадение символов — 0 из 519, при этом совпадений
        по базовому активу 519. То есть вторая нога стратегии не находилась
        НИКОГДА, и любая сделка отклонялась с no_futures_ticker.

        Порядок поиска:
          1. точный ключ (если биржа отдала спот-подобное имя)
          2. "<symbol>:USDT" — стандартный перпетуал ccxt
          3. перебор: любой ключ с тем же базовым активом
        """
        if not futures_prices:
            return None
        if spot_symbol in futures_prices:
            return spot_symbol
        perp = f"{spot_symbol}:USDT"
        if perp in futures_prices:
            return perp
        base = spot_symbol.split("/")[0]
        for key in futures_prices:
            if key.split("/")[0] == base:
                return key
        return None

    def _reject(self, opp, reason: str, synthetic: float = 0.0):
        self.rejected_counters[reason] = self.rejected_counters.get(reason, 0) + 1
        if self.shadow_logger is not None:
            self.shadow_logger.record_entry(
                opp, rejected=True, reason=reason,
                synthetic_pnl=synthetic, latent_pnl=synthetic)
        return None

    def _latency_drift_pct(self) -> float:
        if self.max_latency_ms <= 0:
            return 0.0
        ms = self._sim_rng.uniform(self.min_latency_ms, self.max_latency_ms)
        return self.latency_drift_bps_per_sec * (ms / 1000.0) / 100.0

        return (fut_mid - spot_mid) / spot_mid * 100.0


    async def execute(self, opp: "Opportunity", prices: Dict[str, Ticker],
                      close_prices: Optional[Dict[str, Ticker]] = None,
                      futures_prices: Optional[Dict[str, Ticker]] = None,
                      funding_tracker: Optional["FundingRateTracker"] = None
                      ) -> Optional[TradeResult]:
        """
        ОТКРЫТИЕ. Позиция ОСТАЁТСЯ в self.open_pairs и переживает циклы.
        TradeResult здесь не возвращается: сделки ещё нет, есть позиция.
        Закрытие происходит в manage_open_positions().
        """
        if opp.symbol in self.open_pairs:
            return self._reject(opp, "already_holding_symbol",
                                 opp.net_profit_pct)
        # Лимит живых пар. Проверяется ДО любых вычислений и резервов.
        if len(self.open_pairs) >= self.max_open_pairs:
            return self._reject(opp, "max_open_pairs", opp.net_profit_pct)
        # Cooldown после закрытия: иначе повторный вход в тот же актив на
        # следующем цикле платит ещё четыре комиссии ради того же спреда.
        last = self._closed_at.get(opp.symbol)
        if last is not None and (time.time() - last) < self.cooldown_sec:
            return self._reject(opp, "cooldown", opp.net_profit_pct)
        can, reason = self.risk.can_trade(opp)
        if not can:
            return self._reject(opp, "risk:" + str(reason))
        if self.api_failure_prob > 0 and \
                self._sim_rng.random() < self.api_failure_prob:
            return self._reject(opp, "api_failure", opp.net_profit_pct)

        # КОТИРОВКИ. Структуры разные по площадкам:
        #   prices         -> {exchange: Ticker}            (спот)
        #   futures_prices -> {symbol: {exchange: Ticker}}  (фьючерс)
        # Раньше стояло `opp.sell_exchange in futures_prices`, то есть поиск
        # биржи в СИМВОЛЬНОМ словаре: при разных биржах условие ложилось, и
        # код молча брал sell_t из prices (СПОТ) вместо фьючерса. Разбираем
        # оба случая явно.
        buy_t = prices.get(opp.buy_exchange)
        # Резолвим ключ фьючерса: спот "SOL/USDT" против перпетуала
        # "SOL/USDT:USDT" (см. resolve_futures_symbol).
        fut_key = self.resolve_futures_symbol(futures_prices, opp.symbol)
        sell_t = ((futures_prices or {}).get(fut_key) or {}).get(opp.sell_exchange)
        if sell_t is None:
            return self._reject(opp, "no_futures_ticker", opp.net_profit_pct)
        if not buy_t:
            return self._reject(opp, "no_spot_ticker", opp.net_profit_pct)
        if buy_t.ask <= 0 or sell_t.bid <= 0:
            return self._reject(opp, "bad_ticker_price", opp.net_profit_pct)
        if sell_t.bid <= buy_t.ask:
            return self._reject(opp, "no_spread", opp.net_profit_pct)

        size = self.risk.calculate_position_size(opp, opp.max_safe_size_usdt)
        size = min(size, self.portfolio.balance * MAX_POSITION_PCT_OF_BALANCE)
        size = min(size, MAX_TRADE_SIZE)
        if self.available_liquidity_pct < 1.0:
            size = min(size,
                       self.risk.calculate_position_size(
                           opp, opp.max_safe_size_usdt)
                       * self.available_liquidity_pct)
        if self.min_fill_ratio > 0:
            need = self.min_fill_ratio * max(
                self.risk.calculate_position_size(opp, opp.max_safe_size_usdt), 1e-9)
            if size < need:
                return self._reject(opp, "liquidity_too_low_for_min_fill",
                                     opp.net_profit_pct)
        if size < MIN_TRADE_SIZE or size > self.portfolio.balance:
            return self._reject(opp, "size_out_of_bounds", opp.net_profit_pct)

        slip = self.calc.calculate_slippage(size)
        entry_spot = buy_t.ask * (1 + slip)
        entry_fut = sell_t.bid * (1 - slip)
        if entry_fut <= entry_spot:
            return self._reject(opp, "no_edge_after_slippage",
                                 opp.net_profit_pct)

        amount = size / entry_spot
        short_notional = amount * entry_fut
        margin = short_notional / self.futures_leverage
        spot_fee = size * TRADING_FEES.get(opp.buy_exchange, 0.001)
        fut_fee = short_notional * TRADING_FEES.get(opp.sell_exchange, 0.001)

        if not self.balance_manager.can_buy(opp.buy_exchange, size + spot_fee):
            return self._reject(opp, "insufficient_spot_usdt", opp.net_profit_pct)
        if not self.balance_manager.can_buy(opp.sell_exchange, margin + fut_fee):
            return self._reject(opp, "insufficient_futures_margin",
                                 opp.net_profit_pct)
        if not self.balance_manager.reserve(opp.buy_exchange, "USDT",
                                            size + spot_fee):
            return self._reject(opp, "reserve_spot_failed", opp.net_profit_pct)
        if not self.balance_manager.reserve(opp.sell_exchange, "USDT",
                                            margin + fut_fee):
            self.balance_manager.release(opp.buy_exchange, "USDT", size + spot_fee)
            return self._reject(opp, "reserve_futures_failed", opp.net_profit_pct)

        # Покупаем спот и открываем шорт по фьючерсу. Ноги ОСТАЮТСЯ открытыми.
        # ВАЖНО: шорт открывается НЕ через apply_sell_fill — это продажа СПОТА,
        # которой у нас на фьючерсной бирже нет (ошибка «Insufficient SOL»).
        # Для шорта достаточно списать комиссию с уже зарезервированной маржи:
        # apply_futures_short_cycle гасит весь резерв, он же предназначен для
        # ЗАКРЫТИЯ ноги, поэтому на открытии просто уменьшаем USDT на комиссию
        # и оставляем резерв маржи нетронутым до закрытия.
        try:
            self.balance_manager.apply_buy_fill(
                opp.buy_exchange, opp.symbol, amount, size, spot_fee)
            self.balance_manager.balances[opp.sell_exchange]["USDT"] -= fut_fee
        except Exception as e:
            logger.warning(f"spot-futures open failed: {e}")
            self.balance_manager.release(opp.buy_exchange, "USDT", size + spot_fee)
            self.balance_manager.release(opp.sell_exchange, "USDT", margin + fut_fee)
            return self._reject(opp, "open_legs_failed", opp.net_profit_pct)

        # Стоимость ПЕРЕВОДА между биржами. Ноги на разных площадках =>
        # коллатерал надо перебросить: время, комиссия, и риск, что за время
        # перевода позиция не хеджирована. Без этого кросс-биржевой арбитраж
        # выглядит дешевле, чем он есть на самом деле.
        xfer = 0.0
        if opp.buy_exchange != opp.sell_exchange:
            w = (WITHDRAWAL_FEES.get(opp.buy_exchange, 1.0)
                 + WITHDRAWAL_FEES.get(opp.sell_exchange, 1.0))
            xfer = size * (w / 1000.0)

        basis = (entry_fut - entry_spot) / entry_spot * 100.0

        # ── ФИНАНСОВЫЙ КРИТЕРИЙ ВХОДА (Шаг 0.4) ─────────────────────
        #
        # Confidence — это взвешенная сумма ПРОКСИ (ликвидность, объём,
        # история, рейтинг биржи). Ни один из них не отвечает на вопрос
        # «заработаю ли я на удержании». Ответ даёт только базис минус
        # ИЗМЕРЕННЫЕ расходы.
        #
        # Проверка стоит ПОСЛЕ расчёта entry_spot/entry_fut, потому что
        # basis обязан считаться по фактическим ценам исполнения
        # (с проскальзыванием), а не по котировкам из кэша.
        #
        # Порядок: сначала размер (нужен для расходов), потом критерий.
        hold_hours = BasisEntryCriteria.hold_hours_for(basis)
        funding_rate = 0.0
        if funding_tracker is not None:
            try:
                funding_rate = funding_tracker.get_rate(opp.sell_exchange)
            except Exception:
                funding_rate = 0.0
        decision = BasisEntryCriteria().evaluate(
            basis_pct=basis, size_usdt=size,
            buy_t=buy_t, sell_t=sell_t,
            funding_rate_per_hour=funding_rate,
            hold_hours=hold_hours)
        if not decision.allowed:
            return self._reject(opp, decision.reason, opp.net_profit_pct)
        logger.info(
            f"BASIS ENTRY {opp.symbol} {opp.buy_exchange}->{opp.sell_exchange} "
            f"basis={basis:+.3f}% cost={decision.cost_pct:.3f}% "
            f"net={decision.expected_net_pct:+.3f}% hold={hold_hours:.1f}h "
            f"size={size:.2f}")
        # ───────────────────────────────────────────────────────────

        self.open_pairs[opp.symbol] = OpenPair(
            symbol=opp.symbol, buy_exchange=opp.buy_exchange,
            sell_exchange=opp.sell_exchange,
            direction="fut_premium" if basis > 0 else "fut_discount",
            amount=amount, size_usdt=size,
            entry_spot=entry_spot, entry_fut=entry_fut, entry_basis=basis,
            opened_at=time.time(), margin=margin, transfer_fee=xfer,
            # таймаут принадлежит паре, а не трейдеру
            hold_hours=hold_hours,
            # ровно то, что зарезервировано строкой выше: margin + fut_fee
            fut_reserved=margin + fut_fee,
            fees_paid=spot_fee + fut_fee)

        if self.shadow_logger is not None:
            self.shadow_logger.record_entry(
                opp, rejected=False, reason="opened",
                synthetic_pnl=opp.net_profit_pct,
                latent_pnl=opp.net_profit_pct - self._latency_drift_pct(),
                event="OPEN", basis=basis, size_usdt=size, transfer_fee=xfer)
        return None

    def manage_open_positions(self, prices: Dict[str, Ticker],
                             futures_prices: Optional[Dict[str, Ticker]] = None,
                             cycle: int = 0) -> List[TradeResult]:
        """
        Проверка ОТКРЫТЫХ пар и закрытие. Вызывается каждый цикл.

        Приоритет: стоп -> сходимость -> таймаут (пессимизм: стоп важнее
        цели, иначе при резком движении в плюс можно «пропустить» убыток).

        Сходимость  : |базис| <= target_close_pct
        Стоп        : базис расширился против нас на stop_widen_pct п.п.
        Таймаут     : прошло max_hold_bars циклов.

        PnL считается ТОЛЬКО здесь, по фактическим ценам обеих ног, минус
        все комиссии (вход+выход, обе ноги), перевод и funding за удержание.
        """
        closed: List[TradeResult] = []
        now = time.time()
        for sym, p in list(self.open_pairs.items()):
            # СПОТ берём из prices (там по ключу биржи лежит спот), а ФЬЮЧЕРС
            # из futures_prices. Раньше фьючерс искался сначала в futures_prices,
            # а спот — в том же словаре, где его нет: в futures_prices лежит
            # только фьючерс, поэтому spot_t был None и закрытие падало.
            sym_prices = prices.get(sym) or {}
            spot_t = sym_prices.get(p.buy_exchange)
            # Тот же резолв символа, что и в execute(): спот "SOL/USDT"
            # против перпетуала "SOL/USDT:USDT".
            fut_key = self.resolve_futures_symbol(futures_prices, sym)
            fut_sym = (futures_prices or {}).get(fut_key) or {}
            fut_t = fut_sym.get(p.sell_exchange) or \
                sym_prices.get(p.sell_exchange)
            if not spot_t or not fut_t:
                # Нет котировки — это НЕ отказ, а пропуск. Позиция остаётся
                # открытой, и её нельзя ни закрыть, ни посчитать отказом.
                self.rejected_counters["no_quote"] = \
                    self.rejected_counters.get("no_quote", 0) + 1
                continue
            p.bars_held += 1
            # ВРЕМЯ, а не счётчик циклов. Раньше здесь стояло p.bars_held, и это
            # была ошибка единиц: bars_held растёт на единицу за ЦИКЛ (при
            # SCAN_INTERVAL_SEC=2 это десятки раз в час), а funding_rate_per_hour
            # определён на ЧАС. Итог: funding завышался в десятки раз, а
            # таймаут срабатывал за минуты вместо часов. Теперь обе вещи
            # измеряются в одном, физическом времени.
            hours_held = max(0.0, (now - p.opened_at) / 3600.0)
            p.hours_held = hours_held
            spot_mid = (spot_t.bid + spot_t.ask) / 2.0
            fut_mid = (fut_t.bid + fut_t.ask) / 2.0
            cur = p.basis_now(spot_mid, fut_mid)

            worse = (cur - p.entry_basis) if p.direction == "fut_premium" \
                else (p.entry_basis - cur)
            if worse >= self.stop_widen_pct:
                reason = "STOP"
            elif abs(cur) <= self.target_close_pct:
                reason = "CONVERGED"
            # Таймаут берётся ИЗ ПАРЫ, а не из трейдера (Шаг 0.4).
            # hold_hours фиксируется при открытии по размеру базиса:
            # чем больше базис, тем больше времени на сходимость. Общее
            # значение в трейдере перезаписывалось бы при открытии каждой
            # новой пары, и самая ценная (с наибольшим базисом) получала
            # бы таймаут чужой пары.
            elif hours_held >= (p.hold_hours or self.max_hold_hours):
                reason = "TIMEOUT"
            else:
                continue

            slip = self.calc.calculate_slippage(p.size_usdt)
            spot_exit = spot_t.bid * (1 - slip)
            fut_exit = fut_t.ask * (1 + slip)
            spot_rev = p.amount * spot_exit
            fut_rev = p.amount * fut_exit
            exit_spot_fee = spot_rev * TRADING_FEES.get(p.buy_exchange, 0.001)
            exit_fut_fee = fut_rev * TRADING_FEES.get(p.sell_exchange, 0.001)
            # шорт закрывается покупкой по ЗАБЫТОЙ цене
            fut_pnl = (p.entry_fut - fut_exit) * p.amount
            spot_pnl = spot_rev - p.amount * p.entry_spot
            # ФОНДИНГ — в ЧАСАХ удержания. Раньше здесь стоял p.bars_held,
            # то есть число ЦИКЛОВ: при SCAN_INTERVAL_SEC=2 это десятки
            # начислений в час вместо одного, и расход накапливался кратно
            # быстрее реального. Теперь единицы совпадают.
            funding = p.margin * self.funding_rate_per_hour * hours_held

            # ПЕРЕВОД НЕ вычитается здесь (Шаг 0.5).
            #
            # Раньше стояло `- p.transfer_fee` в расчёте PnL. Это было
            # неверно вдвойне:
            #   1) в BalanceManager нет операции перевода - деньги никуда
            #      не двигались, то есть платился расход без события;
            #   2) transfer_fee = size * (w/1000) - полная ставка на КАЖДУЮ
            #      сделку, тогда как балансы расходятся на -0.72 USDT за
            #      сделку и перевод нужен раз в ~1100 сделок.
            #
            # Перенос между биржами уже учтён амортизированной ставкой
            # TRANSFER_AMORTIZED_PCT в BasisEntryCriteria._costs_pct(),
            # то есть в расчёте записи при входе, где её видно в логах.
            pnl = (spot_pnl + fut_pnl - exit_spot_fee - exit_fut_fee
                   - funding)
            pnl_pct = (pnl / p.size_usdt * 100) if p.size_usdt > 0 else 0.0

            self._close_pair(p, spot_exit, fut_exit, spot_rev, fut_rev,
                             exit_spot_fee, exit_fut_fee, fut_pnl, funding)
            self.open_pairs.pop(sym, None)
            self._closed_at[sym] = now

            tr = TradeResult(
                timestamp=int(time.time()), symbol=sym,
                buy_exchange=p.buy_exchange, sell_exchange=p.sell_exchange,
                amount=p.amount, pnl=pnl, pnl_pct=pnl_pct,
                balance_after=self.portfolio.balance + pnl, success=pnl > 0)
            self.portfolio.balance += pnl
            closed.append(tr)
            if self.shadow_logger is not None:
                self.shadow_logger.record_entry(
                    self._opp_stub(p), rejected=False, reason=reason,
                    synthetic_pnl=p.entry_basis - cur, latent_pnl=pnl_pct,
                    event="CLOSE", exit_reason=reason, basis_exit=cur,
                    bars_held=p.bars_held)
        return closed

    def snapshot_open(self) -> List[dict]:
        """
        Снимок открытых пар для сохранения на диск.

        Зачем: пары живут между циклами, значит они должны пережить ПЕРЕЗАПУСК.
        Иначе после рестарта бот теряет открытые ноги: баланс уже списан
        (спот куплен, маржа зарезервирована), а знания о позиции нет — она
        не закроется никогда, и средства останутся заморожены. Для
        арбитражной стратегии с удержанием это не «неприятно», а прямой
        убыток и рассинхрон учёта.
        """
        return [_dc.asdict(p) for p in self.open_pairs.values()]

    def restore_open(self, rows: List[dict]) -> int:
        """Восстановление открытых пар из снимка. Возвращает число принятых."""
        if not rows:
            return 0
        n = 0
        for r in rows:
            try:
                known = {f.name for f in _dc.fields(OpenPair)}
                self.open_pairs[r["symbol"]] = OpenPair(
                    **{k: v for k, v in r.items() if k in known})
                n += 1
            except Exception as e:
                logger.error(f"restore_open: {r.get('symbol')} — {e}")
        if n:
            logger.warning(
                f"ВОССТАНОВЛЕНО {n} открытых арбитражных пар. До сверки "
                f"с биржей не открывать новые позиции по этим символам.")
        return n

    def _opp_stub(self, p: OpenPair) -> "Opportunity":
        """Минимальная запись для shadow-лога у пары, у которой Opportunity
        уже не хранится (актуально: лог должен быть полным)."""
        return Opportunity(
            symbol=p.symbol, buy_exchange=p.buy_exchange,
            sell_exchange=p.sell_exchange, buy_price=p.entry_spot,
            sell_price=p.entry_fut,
            spread_pct=abs(p.entry_basis), net_profit_pct=p.entry_basis,
            volume=0.0, exchanges_count=2, timestamp=int(p.opened_at * 1000))

    def _close_pair(self, p, spot_exit, fut_exit, spot_rev, fut_rev,
                    exit_spot_fee, exit_fut_fee, fut_pnl, funding):
        try:
            self.balance_manager.apply_spot_close(
                p.buy_exchange, p.symbol, p.amount, spot_rev, exit_spot_fee)
            # Освобождаем РОВНО то, что было зарезервировано на входе.
            # apply_futures_short_cycle брал margin + exit_fee + funding —
            # это не совпадает с margin + entry_fee, и остаток навсегда
            # зависал в резерве, утекая свободной маржей.
            self.balance_manager.close_futures_short(
                p.sell_exchange, p.fut_reserved or p.margin, fut_pnl,
                exit_fut_fee, funding)
        except Exception as e:
            logger.critical(
                f"CLOSE FAILED {p.symbol}: {e} — возможен рассинхрон ног, "
                f"нужна ручная сверка")


# ═══════════════════════════════════════════════════════════════════════
# ВОССТАНОВЛЕНО 2026-09-30 (Шаг 0.0)
#
# Hunter, Telegram и Database были потеряны при пересборке engine.py и
# восстановлены из old/Новая папка (7)/engine.py, строки 2236-2494.
# app.py импортирует их из engine (строки 19-29), поэтому без них модуль
# не поднимался: ImportError: cannot import name 'Hunter'.
#
# Блок перенесён БЕЗ ИЗМЕНЕНИЙ. Прежде чем править поведение, сверься с
# вызовами в app.py:
#   hunter.select(volumes)
#   tg.start / tg.send / tg.stop
#   db.start / db.stop / db.save_opportunity / db.save_open_trade /
#      db.close_trade / db.get_open_trades / db.save_trade /
#      db.save_portfolio_state / db.load_portfolio_state /
#      db.delete_portfolio_state
# ═══════════════════════════════════════════════════════════════════════# ════════════════════════════════════════════════
class Hunter:
    def __init__(self, pool: ExchangePool):
        self.pool = pool
    
    def select(self, volumes: Dict[str, Dict[str, float]]) -> List[str]:
        candidates = []
        for symbol, exchanges in self.pool.symbols_per_exchange.items():
            count = len(exchanges)
            if not (MIN_EXCHANGES <= count <= MAX_EXCHANGES):
                continue
            base = symbol.split("/")[0]
            if base in BLACKLIST:
                continue
            vols = [volumes.get(symbol, {}).get(ex, 0) for ex in exchanges]
            vols = [v for v in vols if v > 0]
            if not vols:
                continue
            vols.sort()
            median_vol = vols[len(vols) // 2]
            if not (MIN_VOLUME_USDT <= median_vol <= MAX_VOLUME_USDT):
                continue
            score = count * 1_000_000 + math.log10(max(median_vol, 1)) * 100_000
            candidates.append((symbol, score, median_vol, count))
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [c[0] for c in candidates[:MAX_SYMBOLS]]


# ════════════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════════════
class Telegram:
    def __init__(self):
        self.enabled = bool(TG_TOKEN and TG_CHAT)
        self.queue: Optional[asyncio.Queue] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
    
    async def start(self):
        if not self.enabled:
            return
        self.session = aiohttp.ClientSession()
        self.queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker())
    
    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()
    
    async def _worker(self):
        while True:
            try:
                msg = await self.queue.get()
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
                await self.session.post(url, json={
                    "chat_id": TG_CHAT, "text": msg[:4000], "parse_mode": "HTML"
                })
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Telegram: {e}")
    
    async def send(self, text: str):
        if self.enabled and self.queue:
            await self.queue.put(text)


# ════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════
class Database:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self.conn: Optional[sqlite3.Connection] = None
    
    async def start(self):
        self._writer_task = asyncio.create_task(self._writer())
        await asyncio.sleep(0.1)
    
    async def _writer(self):
        self.conn = sqlite3.connect("hunter.db", check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY, ts INTEGER, symbol TEXT,
                buy_ex TEXT, sell_ex TEXT, spread REAL, net REAL,
                vol REAL, confidence REAL, category TEXT,
                max_safe_size REAL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY, ts INTEGER, symbol TEXT,
                pnl REAL, pnl_pct REAL, balance REAL, category TEXT,
                status TEXT DEFAULT 'open',
                buy_ex TEXT, sell_ex TEXT, size_usdt REAL,
                buy_price REAL DEFAULT 0, sell_price REAL DEFAULT 0
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_state (
                key TEXT PRIMARY KEY, value REAL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS exchange_balances (
                exchange TEXT, asset TEXT, amount REAL,
                PRIMARY KEY (exchange, asset)
            )
        """)
        # МИГРАЦИЯ СХЕМЫ (Шаг 0.5, 2026-09-30).
        #
        # CREATE TABLE IF NOT EXISTS НЕ добавляет колонки в уже существующую
        # таблицу: если trades уже была создана старой версией кода, новые
        # поля молча не появляются. Итог — get_open_trades() падал с
        #     "no such column: buy_ex"
        # а save_open_trade() падал бы на каждой сделке, то есть запись
        # открытых пар в БД не работала вовсе.
        #
        # Миграция идемпотентна: каждый ALTER выполняется только если колонки
        # ещё нет, поэтому её безопасно звать на каждом старте. Существующие
        # строки не трогаются — ALTER TABLE ADD COLUMN их не затрагивает.
        for col, decl in (
            ("status", "TEXT DEFAULT 'closed'"),
            ("buy_ex", "TEXT"),
            ("sell_ex", "TEXT"),
            ("size_usdt", "REAL"),
            ("buy_price", "REAL DEFAULT 0"),
            ("sell_price", "REAL DEFAULT 0"),
        ):
            try:
                self.conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {col} {decl}")
                logger.info(f"🗄️ trades: добавлена колонка {col}")
            except sqlite3.OperationalError as e:
                # "duplicate column name" — колонка уже есть, это норма
                if "duplicate column" not in str(e).lower():
                    raise
        self.conn.commit()
        while True:
            try:
                query, args = await self.queue.get()
                self.conn.execute(query, args)
                self.conn.commit()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"DB: {e}")
    
    async def save_opportunity(self, opp: Opportunity):
        await self.queue.put((
            "INSERT INTO opportunities VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
            (opp.timestamp, opp.symbol, opp.buy_exchange, opp.sell_exchange,
             opp.spread_pct, opp.net_profit_pct, opp.volume,
             opp.confidence_score, opp.category, opp.max_safe_size_usdt)
        ))
    
    async def save_open_trade(self, opp: Opportunity, size_usdt: float, category: str = "other"):
        """Запись о начале сделки. После execute, до close."""
        buy_price = opp.buy_price or 0
        sell_price = opp.sell_price or 0
        await self.queue.put((
            "INSERT INTO trades (ts, symbol, pnl, pnl_pct, balance, category, "
            "status, buy_ex, sell_ex, size_usdt, buy_price, sell_price) "
            "VALUES (?,?,0,0,0,?, 'open',?,?,?,?,?)",
            (int(time.time()), opp.symbol, category,
             opp.buy_exchange, opp.sell_exchange, size_usdt,
             buy_price, sell_price)
        ))
    
    async def close_trade(self, symbol: str, buy_ex: str, sell_ex: str,
                           pnl: float, pnl_pct: float, balance_after: float):
        """Закрытие сделки — UPDATE статус по symbol + path."""
        await self.queue.put((
            "UPDATE trades SET status='closed', pnl=?, pnl_pct=?, balance=? "
            "WHERE symbol=? AND buy_ex=? AND sell_ex=? AND status='open'",
            (pnl, pnl_pct, balance_after, symbol, buy_ex, sell_ex)
        ))
    
    async def get_open_trades(self) -> List[Dict]:
        """Для recovery при старте."""
        if not self.conn:
            return []
        try:
            cur = self.conn.execute(
                "SELECT id, ts, symbol, buy_ex, sell_ex, size_usdt, "
                "buy_price, sell_price, category FROM trades WHERE status='open'"
            )
            rows = cur.fetchall()
            return [
                {"id": r[0], "symbol": r[2], "buy_ex": r[3], "sell_ex": r[4],
                 "size_usdt": r[5], "buy_price": r[6], "sell_price": r[7],
                 "category": r[8]}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"DB get_open_trades: {e}")
            return []
    
    async def save_trade(self, trade: TradeResult, category: str = "other"):
        """Legacy — полная запись для уже закрытой сделки (status='closed')."""
        await self.queue.put((
            "INSERT INTO trades (ts, symbol, pnl, pnl_pct, balance, category, "
            "status, buy_ex, sell_ex, size_usdt, buy_price, sell_price) "
            "VALUES (?,?,?,?,?,?, 'closed',?,?,?,?,?)",
            (trade.timestamp, trade.symbol, trade.pnl, trade.pnl_pct,
             trade.balance_after, category,
             trade.buy_exchange, trade.sell_exchange, trade.amount,
             trade.buy_exchange and 0, trade.sell_exchange and 0)
        ))
    
    async def stop(self):
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        if self.conn:
            self.conn.close()

    async def save_portfolio_state(self, portfolio: "Portfolio", balance_manager: Optional["BalanceManager"] = None):
        """Сохраняет баланс и per-exchange балансы для восстановления."""
        await self.queue.put((
            "INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?,?)",
            ("balance", portfolio.balance)
        ))
        await self.queue.put((
            "INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?,?)",
            ("initial_balance", portfolio.initial_balance)
        ))
        await self.queue.put((
            "INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?,?)",
            ("peak_equity", portfolio.peak_equity)
        ))
        await self.queue.put((
            "INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?,?)",
            ("total_trades", float(len(portfolio.closed_trades)))
        ))
        if balance_manager:
            await self.queue.put(("DELETE FROM exchange_balances", ()))
            for ex, assets in balance_manager.balances.items():
                for asset, amount in assets.items():
                    await self.queue.put((
                        "INSERT OR REPLACE INTO exchange_balances VALUES (?,?,?)",
                        (ex, asset, amount)
                    ))

    async def load_portfolio_state(self) -> Tuple[Optional[float], Optional[float], Optional[float], Dict]:
        """Загружает сохранённое состояние портфеля.
        
        Returns:
            (balance, initial_balance, peak_equity, exchange_balances)
        """
        if not self.conn:
            return None, None, None, {}
        try:
            cur = self.conn.execute("SELECT key, value FROM portfolio_state")
            state = dict(cur.fetchall())
            balance = state.get("balance")
            initial_balance = state.get("initial_balance")
            peak_equity = state.get("peak_equity")
            
            cur2 = self.conn.execute("SELECT exchange, asset, amount FROM exchange_balances")
            ex_balances = {}
            for ex, asset, amount in cur2.fetchall():
                ex_balances.setdefault(ex, {})[asset] = amount
            
            return balance, initial_balance, peak_equity, ex_balances
        except Exception as e:
            logger.warning(f"DB load_portfolio_state: {e}")
            return None, None, None, {}

    async def delete_portfolio_state(self):
        """Очищает сохранённое состояние (после успешного восстановления)."""
        try:
            self.conn.execute("DELETE FROM portfolio_state")
            self.conn.execute("DELETE FROM exchange_balances")
            self.conn.commit()
        except Exception as e:
            logger.warning(f"DB delete_portfolio_state: {e}")
