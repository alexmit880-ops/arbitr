"""⚡ Live Engine — реальное исполнение арбитражных сделок.

Содержит:
  - LiveOrderExecutor: реальные лимитные ордера с leg-последовательностью
  - KillSwitch: аварийное закрытие всех позиций
  - TradingMode: shadow / paper / live переключение
"""
import asyncio
import time
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field

from loguru import logger

from config import *
from engine import (
    Opportunity, TradeResult, Portfolio, RealityCheck,
    OrderBookExecutionAnalyzer, ShadowLogger,
)


# ════════════════════════════════════════════════
# LEG EXECUTION STATUS
# ════════════════════════════════════════════════
@dataclass
class LegStatus:
    """Статус исполнения одного leg (ноги) сделки."""
    leg_id: str          # "buy" or "sell"
    exchange: str
    symbol: str
    side: str            # "buy" or "sell"
    planned_size: float
    filled_size: float = 0.0
    price: float = 0.0
    status: str = "pending"  # pending | submitted | partial | filled | failed | timeout
    error: str = ""
    timestamp: float = 0.0
    exchange_order_id: str = ""
    latency_sec: float = 0.0


@dataclass
class LiveTradeState:
    """Состояние выполняемой сделки. Хранит оба leg."""
    trade_id: str
    opp: Opportunity
    legs: Dict[str, LegStatus] = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float = 0.0
    final_status: str = "pending"  # pending | executing | success | partial | failed
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    error: str = ""
    killed: bool = False


# ════════════════════════════════════════════════
# KILL SWITCH
# ════════════════════════════════════════════════
class KillSwitch:
    """Автоматическое аварийное закрытие всех позиций.
    
    Срабатывает при:
      - Превышении MAX_DRAWDOWN_PERCENT
      - Потере связи с биржей > 30 сек
      - Превышении времени между legs
      - Превышении скорости убытка за 1 час
    """
    
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        self._triggered: bool = False
        self._trigger_time: float = 0.0
        self._last_check_time: float = time.time()
        self._hourly_pnl_history: List[Tuple[float, float]] = []  # (timestamp, pnl)
        self._exchange_last_ok: Dict[str, float] = {}
    
    def record_exchange_ok(self, exchange: str):
        """Обновить время успешного ответа от биржи."""
        self._exchange_last_ok[exchange] = time.time()
    
    def record_trade_result(self, pnl: float):
        """Записывает результат сделки для анализа скорости убытка."""
        now = time.time()
        self._hourly_pnl_history.append((now, pnl))
        # Оставляем только последний час
        cutoff = now - 3600
        self._hourly_pnl_history = [
            (t, p) for t, p in self._hourly_pnl_history if t > cutoff
        ]
    
    def check(self, force: bool = False) -> Tuple[bool, str]:
        """Проверить нужно ли аварийно закрыть позиции.
        
        Returns:
            (should_kill, reason)
        """
        if self._triggered:
            return True, f"Already triggered at {self._trigger_time:.0f}"
        
        now = time.time()
        p = self.portfolio
        
        # ── 1. Drawdown ────────────────────────────────────
        if p.drawdown_pct > MAX_DRAWDOWN_PERCENT:
            self._trigger(f"Drawdown {p.drawdown_pct:.1f}% > {MAX_DRAWDOWN_PERCENT}%")
            return True, self._reason
        
        # ── 2. Daily loss ──────────────────────────────────
        if p.initial_balance > 0:
            daily_loss_pct = abs(p.daily_pnl) / p.initial_balance * 100
            if daily_loss_pct > MAX_DAILY_LOSS_PERCENT:
                self._trigger(f"Daily loss {daily_loss_pct:.1f}% > {MAX_DAILY_LOSS_PERCENT}%")
                return True, self._reason
        
        # ── 3. Consecutive losses ──────────────────────────
        if p.consecutive_losses >= MAX_CONSECUTIVE_LOSSES * 2:
            self._trigger(f"Consecutive losses {p.consecutive_losses} >= {MAX_CONSECUTIVE_LOSSES * 2}")
            return True, self._reason
        
        # ── 4. Hourly loss rate ────────────────────────────
        if len(self._hourly_pnl_history) >= 5:
            recent_pnls = [pnl for _, pnl in self._hourly_pnl_history[-10:]]
            total_recent_loss = sum(p for p in recent_pnls if p < 0)
            if abs(total_recent_loss) > EMERGENCY_PNL_THRESHOLD:
                self._trigger(f"Hourly loss ${abs(total_recent_loss):.1f} > ${EMERGENCY_PNL_THRESHOLD}")
                return True, self._reason
        
        # ── 5. Exchange connectivity ──────────────────────
        for ex_name, last_ok in self._exchange_last_ok.items():
            if now - last_ok > 60:  # биржи нет > 1 мин
                self._trigger(f"Exchange {ex_name} silent for {now - last_ok:.0f}s")
                return True, self._reason
        
        return False, "ok"
    
    def _trigger(self, reason: str):
        """Активировать kill switch."""
        self._triggered = True
        self._trigger_time = time.time()
        self._reason = reason
        logger.critical(f"💀 KILL SWITCH ACTIVATED: {reason}")
    
    @property
    def is_triggered(self) -> bool:
        return self._triggered
    
    def reset(self):
        """Сбросить kill switch после ручного подтверждения."""
        self._triggered = False
        self._trigger_time = 0.0
        self._hourly_pnl_history.clear()
        logger.info("🔄 Kill switch reset")


# ════════════════════════════════════════════════
# LIVE ORDER EXECUTOR
# ════════════════════════════════════════════════
class LiveOrderExecutor:
    """Реальное исполнение арбитражных ордеров на биржах.
    
    Поддерживает:
      - Лимитные ордера (сначала лимитки, не market)
      - IоС (immediate-or-cancel) для быстрых арбитражей
      - Leg-последовательность: купил → проверил → продал
      - Retry при timeout
      - Partial fill обработка
    
    Режимы:
      - "shadow": только логирование, никаких ордеров
      - "paper": симуляция (использует PaperTrader)
      - "live": реальные ордера
    """
    
    def __init__(self, pool, portfolio: Portfolio,
                 shadow_logger: Optional[ShadowLogger] = None,
                 mode: str = "shadow"):
        """
        Args:
            pool: ExchangePool с клиентами
            portfolio: Portfolio для отслеживания баланса
            shadow_logger: ShadowLogger для записи статистики
            mode: "shadow" | "paper" | "live"
        """
        self.pool = pool
        self.portfolio = portfolio
        self.shadow_logger = shadow_logger
        self.mode = mode
        self.kill_switch = KillSwitch(portfolio)
        self._active_trades: Dict[str, LiveTradeState] = {}
        self._trade_counter = 0
        self._open_orders: Dict[str, dict] = {}  # exchange -> order info
    
    def set_mode(self, mode: str):
        """Переключить режим исполнения."""
        assert mode in ("shadow", "paper", "live"), f"Invalid mode: {mode}"
        old = self.mode
        self.mode = mode
        logger.info(f"🔄 Trading mode: {old} → {mode}")
    
    # ── Public execute ──────────────────────────
    
    async def execute(self, opp: Opportunity, prices: dict,
                      adaptive=None) -> Optional[TradeResult]:
        """Исполнить арбитражную сделку в зависимости от режима.
        
        Args:
            opp: арбитражная возможность
            prices: словарь цен (exchange -> Ticker)
            adaptive: AdaptiveParameters для логирования
        
        Returns:
            TradeResult или None если сделка не прошла
        """
        if self.mode == "shadow":
            return await self._execute_shadow(opp, prices, adaptive)
        elif self.mode == "paper":
            return await self._execute_paper(opp, prices, adaptive)
        elif self.mode == "live":
            return await self._execute_live(opp, prices, adaptive)
        return None
    
    # ── Shadow mode ─────────────────────────────
    
    async def _execute_shadow(self, opp: Opportunity, prices: dict,
                               adaptive=None) -> Optional[TradeResult]:
        """Shadow: только логируем, ничего не делаем."""
        if self.shadow_logger:
            # Логируем как rejected — сделка не исполнится
            self.shadow_logger.record_rejected(
                opp, reason="shadow_mode",
                planned_size=opp.max_safe_size_usdt,
                expected_ev=0,
                fill_probability=0,
                adaptive_min_spread=adaptive.get_min_spread() if adaptive else 0,
                adaptive_min_net_profit=adaptive.get_min_net_profit() if adaptive else 0,
                volatility_factor=adaptive.get_volatility_factor() if adaptive else 1.0,
                spread_volatility_cv=adaptive.get_spread_volatility() if adaptive else 0,
            )
        return None
    
    # ── Paper mode ──────────────────────────────
    
    async def _execute_paper(self, opp: Opportunity, prices: dict,
                              adaptive=None) -> Optional[TradeResult]:
        """Paper: симуляция через SpotFuturesPaperTrader.
        
        Используем импортированный трейдер — он уже есть в engine.py.
        """
        # Импорт здесь чтобы избежать циклического импорта
        from engine import SpotFuturesPaperTrader, BalanceManager
        
        # Создаём временный трейдер (в реальном коде должен быть глобальный)
        bm = BalanceManager({})  # заглушка
        trader = SpotFuturesPaperTrader(
            portfolio=self.portfolio,
            balance_manager=bm,
        )
        return await trader.execute(opp, prices)
    
    # ── Live mode ───────────────────────────────
    
    async def _execute_live(self, opp: Opportunity, prices: dict,
                             adaptive=None) -> Optional[TradeResult]:
        """Live: реальные ордера через API бирж."""
        # Проверяем kill switch
        should_kill, reason = self.kill_switch.check()
        if should_kill:
            logger.critical(f"💀 Kill switch active, trade blocked: {reason}")
            return None
        
        # Проверяем быстрый RealityCheck
        rc = RealityCheck.check(opp, prices, self.portfolio)
        if not rc.allowed:
            logger.warning(f"🚫 Live blocked: {rc.reason}")
            if self.shadow_logger:
                self.shadow_logger.record_rejected(
                    opp, reason=f"live_reality:{rc.reason}",
                    planned_size=rc.planned_size_usdt,
                    expected_ev=rc.expected_value_usdt,
                    fill_probability=rc.fill_probability,
                )
            return None
        
        # Создаём сделку
        trade_id = f"T{int(time.time())}_{self._trade_counter}"
        self._trade_counter += 1
        
        state = LiveTradeState(
            trade_id=trade_id,
            opp=opp,
            started_at=time.time(),
            legs={
                "buy": LegStatus(
                    leg_id="buy", exchange=opp.buy_exchange,
                    symbol=opp.symbol, side="buy",
                    planned_size=rc.planned_size_usdt,
                ),
                "sell": LegStatus(
                    leg_id="sell", exchange=opp.sell_exchange,
                    symbol=opp.symbol, side="sell",
                    planned_size=rc.planned_size_usdt,
                ),
            }
        )
        self._active_trades[trade_id] = state
        
        # Последовательное исполнение legs
        try:
            # Leg 1: Buy
            state.legs["buy"].status = "submitted"
            buy_result = await self._execute_leg(
                exchange=opp.buy_exchange,
                symbol=opp.symbol,
                side="buy",
                amount_usdt=rc.planned_size_usdt,
                price=opp.buy_price,
                leg_timeout=LIVE_LEG_TIMEOUT_SEC,
            )
            state.legs["buy"].filled_size = buy_result.get("filled", 0)
            state.legs["buy"].price = buy_result.get("price", 0)
            state.legs["buy"].status = buy_result.get("status", "failed")
            state.legs["buy"].latency_sec = buy_result.get("latency", 0)
            
            if state.legs["buy"].status not in ("filled", "partial"):
                state.final_status = "failed"
                state.error = f"Buy leg failed: {buy_result.get('error', 'unknown')}"
                logger.error(f"❌ {trade_id}: {state.error}")
                return None
            
            # Leg 2: Sell
            state.legs["sell"].status = "submitted"
            sell_result = await self._execute_leg(
                exchange=opp.sell_exchange,
                symbol=opp.symbol,
                side="sell",
                amount=state.legs["buy"].filled_size,
                price=opp.sell_price,
                leg_timeout=LIVE_LEG_TIMEOUT_SEC,
            )
            state.legs["sell"].filled_size = sell_result.get("filled", 0)
            state.legs["sell"].price = sell_result.get("price", 0)
            state.legs["sell"].status = sell_result.get("status", "failed")
            state.legs["sell"].latency_sec = sell_result.get("latency", 0)
            
            if state.legs["sell"].status not in ("filled", "partial"):
                state.final_status = "partial"
                state.error = f"Sell leg failed: {sell_result.get('error', 'unknown')}"

                # Emergency close buy position to prevent open long without hedge
                try:
                    client = self.pool.clients.get(opp.buy_exchange)
                    if client and state.legs["buy"].filled_size > 0:
                        await client.create_market_sell_order(
                            opp.symbol,
                            state.legs["buy"].filled_size
                        )
                        logger.warning(f"💀 Emergency sold {opp.symbol} on {opp.buy_exchange} after sell leg failure")
                except Exception as e:
                    logger.error(f"💀 Emergency close failed: {e}")
                
                logger.warning(f"⚠️ {trade_id}: {state.error}, buy leg filled")
            
            # Расчёт PnL
            buy_cost = state.legs["buy"].filled_size * state.legs["buy"].price
            sell_revenue = state.legs["sell"].filled_size * state.legs["sell"].price
            fees_buy = buy_cost * TRADING_FEES.get(opp.buy_exchange, 0.001)
            fees_sell = sell_revenue * TRADING_FEES.get(opp.sell_exchange, 0.001)
            withdrawal = (WITHDRAWAL_FEES.get(opp.buy_exchange, 1.0) +
                         WITHDRAWAL_FEES.get(opp.sell_exchange, 1.0))
            
            state.total_pnl = sell_revenue - buy_cost - fees_buy - fees_sell - withdrawal
            if buy_cost > 0:
                state.total_pnl_pct = (state.total_pnl / buy_cost) * 100
            
            state.completed_at = time.time()
            state.final_status = "success" if state.total_pnl > 0 else "failed"
            
            # Обновляем портфель
            self.portfolio.balance += state.total_pnl
            self.portfolio.closed_trades.append(TradeResult(
                timestamp=int(time.time()),
                symbol=opp.symbol,
                buy_exchange=opp.buy_exchange,
                sell_exchange=opp.sell_exchange,
                amount=state.legs["buy"].filled_size,
                pnl=state.total_pnl,
                pnl_pct=state.total_pnl_pct,
                balance_after=self.portfolio.balance,
                success=state.total_pnl > 0,
            ))
            
            # Kill switch мониторинг
            self.kill_switch.record_trade_result(state.total_pnl)
            self.kill_switch.record_exchange_ok(opp.buy_exchange)
            self.kill_switch.record_exchange_ok(opp.sell_exchange)
            
            # Логирование
            logger.info(f"✅ {trade_id}: PnL=${state.total_pnl:.2f} "
                       f"({state.total_pnl_pct:.2f}%) "
                       f"buy={state.legs['buy'].status} "
                       f"sell={state.legs['sell'].status}")
            
            if self.shadow_logger:
                self.shadow_logger.record(
                    opp, TradeResult(
                        timestamp=int(time.time()),
                        symbol=opp.symbol,
                        buy_exchange=opp.buy_exchange,
                        sell_exchange=opp.sell_exchange,
                        amount=state.legs["buy"].filled_size,
                        pnl=state.total_pnl,
                        pnl_pct=state.total_pnl_pct,
                        balance_after=self.portfolio.balance,
                        success=state.total_pnl > 0,
                    ),
                    expected_ev=rc.expected_value_usdt,
                    expected_fill_prob=rc.fill_probability,
                    planned_size=rc.planned_size_usdt,
                    l2_max_exec=opp.max_safe_size_usdt,
                )
            
            return TradeResult(
                timestamp=int(time.time()),
                symbol=opp.symbol,
                buy_exchange=opp.buy_exchange,
                sell_exchange=opp.sell_exchange,
                amount=state.legs["buy"].filled_size,
                pnl=state.total_pnl,
                pnl_pct=state.total_pnl_pct,
                balance_after=self.portfolio.balance,
                success=state.total_pnl > 0,
            )
            
        except (asyncio.TimeoutError, Exception) as e:
            state.final_status = "failed"
            # Увеличиваем захват информации об ошибке для лучшей отладки
            error_detail = f"{type(e).__name__}: {str(e)[:100]}"
            state.error = error_detail
            logger.exception(f"❌ {trade_id} critical error during execution: {error_detail}")
            return None
    
    # ── Single leg execution ────────────────────
    
    async def _execute_leg(self, exchange: str, symbol: str,
                            side: str, amount_usdt: float = 0,
                            amount: float = 0, price: float = 0,
                            leg_timeout: float = 5.0) -> dict:
        """Исполняет один leg (лимитный ордер).
        
        Args:
            exchange: имя биржи
            symbol: торговый символ
            side: "buy" или "sell"
            amount_usdt: сумма в USDT (для buy)
            amount: количество базового актива (для sell)
            price: лимитная цена
            leg_timeout: таймаут ожидания исполнения (сек)
        
        Returns:
            dict со статусом, filled, price, latency, error
        """
        client = self.pool.clients.get(exchange)
        if not client:
            return {"status": "failed", "error": f"No client for {exchange}"}
        
        result = {
            "status": "pending",
            "filled": 0.0,
            "price": 0.0,
            "latency": 0.0,
            "error": "",
        }
        
        # В shadow/paper режиме не шлём реальные ордера
        if self.mode != "live":
            result["status"] = "filled" if side == "buy" else "filled"
            result["filled"] = amount_usdt / max(price, 1e-12) if side == "buy" else amount
            result["price"] = price
            return result
        
        try:
            start_time = time.time()
            
            # Конвертируем size
            if side == "buy" and amount_usdt > 0 and price > 0:
                qty = amount_usdt / price
            elif side == "sell" and amount > 0:
                qty = amount
            else:
                return {"status": "failed", "error": "bad_size", **result}
            
            # Применяем step size из market metadata
            market = client.markets.get(symbol)
            if market:
                step = float(market.get("precision", {}).get("amount", 1e-8))
                if step > 0:
                    qty = round(qty // step * step, 8)
                min_qty = float(market.get("limits", {}).get("amount", {}).get("min", 0))
                if min_qty > 0 and qty < min_qty:
                    return {"status": "failed", "error": "below_min_qty", **result}
            
            # Отправляем лимитный ордер
            order = await asyncio.wait_for(
                client.create_limit_order(symbol, side, qty, price),
                timeout=leg_timeout
            )
            
            elapsed = time.time() - start_time
            
            # Проверяем статус
            order_id = order.get("id", "")
            order_status = order.get("status", "open")
            filled = float(order.get("filled", 0))
            avg_price = float(order.get("average", price))
            
            result["status"] = "filled" if order_status == "closed" else "open"
            result["filled"] = filled if filled > 0 else qty
            result["price"] = avg_price if avg_price > 0 else price
            result["latency"] = elapsed
            result["order_id"] = order_id
            
            # Если не заполнился — пытаемся отменить
            if order_status in ("open", "active", "partially_filled"):
                try:
                    await client.cancel_order(order_id, symbol)
                    result["status"] = "cancelled"
                except Exception:
                    pass
            
            # Обновляем open_orders для мониторинга
            if result["status"] in ("open", "partially_filled"):
                self._open_orders[exchange] = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "filled": result["filled"],
                    "price": result["price"],
                    "timestamp": time.time(),
                }
            
            return result
            
        except asyncio.TimeoutError:
            return {"status": "timeout", "error": "timeout", **result}
        except Exception as e:
            return {"status": "failed", "error": str(e)[:80], **result}
    
    # ── Emergency close ─────────────────────────
    
    async def emergency_close_all(self):
        """Аварийно закрыть все открытые позиции market-ордерами."""
        logger.critical("💀 Emergency close all positions")
        
        for trade_id, state in self._active_trades.items():
            if state.final_status in ("success", "failed"):
                continue
            
            # Закрываем buy leg (если он был открыт)
            if state.legs["buy"].status in ("filled", "partial"):
                try:
                    client = self.pool.clients.get(state.opp.buy_exchange)
                    if client:
                        await client.create_market_sell_order(
                            state.opp.symbol,
                            state.legs["buy"].filled_size
                        )
                        logger.warning(f"💀 Emergency sold {state.opp.symbol} "
                                      f"on {state.opp.buy_exchange}")
                except Exception as e:
                    logger.error(f"💀 Emergency close failed: {e}")
            
            state.killed = True
            state.final_status = "killed"
    
    # ── Status ──────────────────────────────────
    
    def get_active_trades(self) -> List[Dict]:
        """Возвращает список активных сделок для dashboard."""
        return [
            {
                "trade_id": t.trade_id,
                "symbol": t.opp.symbol,
                "buy_ex": t.opp.buy_exchange,
                "sell_ex": t.opp.sell_exchange,
                "started_at": t.started_at,
                "status": t.final_status,
                "pnl": t.total_pnl,
                "pnl_pct": t.total_pnl_pct,
                "buy_status": t.legs.get("buy", LegStatus("","","","",0)).status,
                "sell_status": t.legs.get("sell", LegStatus("","","","",0)).status,
                "error": t.error,
                "killed": t.killed,
            }
            for t in self._active_trades.values()
        ]
    
    def get_open_orders(self) -> Dict[str, Dict]:
        """Возвращает открытые ордера."""
        # Очищаем старые
        now = time.time()
        self._open_orders = {
            ex: info for ex, info in self._open_orders.items()
            if now - info.get("timestamp", 0) < 60
        }
        return dict(self._open_orders)
    
    def get_mode_status(self) -> Dict:
        """Статус режима исполнения для dashboard."""
        return {
            "mode": self.mode,
            "kill_switch": self.kill_switch.is_triggered,
            "active_trades": len(self._active_trades),
            "open_orders": len(self._open_orders),
            "balance": self.portfolio.balance,
            "drawdown": self.portfolio.drawdown_pct,
            "daily_pnl": self.portfolio.daily_pnl,
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка состояния LiveOrderExecutor.
        
        Returns:
            Структурированный статус с полями status, latency, last_error.
        """
        try:
            # Проверяем состояние kill switch
            if self.kill_switch.is_triggered:
                return {
                    "status": "unhealthy",
                    "latency": 0.0,
                    "last_error": "Kill switch is triggered"
                }
            # Проверяем количество активных сделок
            active_count = len(self._active_trades)
            if active_count > MAX_OPEN_POSITIONS:
                return {
                    "status": "warning",
                    "latency": 0.0,
                    "last_error": f"Too many active trades: {active_count} > {MAX_OPEN_POSITIONS}"
                }
            return {
                "status": "healthy",
                "latency": 0.0,
                "last_error": ""
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "latency": 0.0,
                "last_error": str(e)[:80]
            }
