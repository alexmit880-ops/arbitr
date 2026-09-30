"""Tests for paper inventory BalanceManager and inventory-aware PaperTrader."""
import asyncio
import time
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (
    BalanceManager, Opportunity, PaperTrader, Portfolio, SpotFuturesPaperTrader, Ticker,
)


def make_opp(symbol="SOL/USDT", buy="binance", sell="bybit", conf=80, net=2.0):
    return Opportunity(
        symbol=symbol,
        buy_exchange=buy,
        sell_exchange=sell,
        buy_price=100,
        sell_price=105,
        spread_pct=5.0,
        net_profit_pct=net,
        volume=1_000_000,
        exchanges_count=2,
        timestamp=int(time.time()),
        confidence_score=conf,
        max_safe_size_usdt=500,
    )


def make_prices():
    """Только СПОТ. Фьючерс задаётся отдельным make_futures(): структуры
    словарей разные, и их смешение подставляло спот вместо фьючерса —
    фантомный спред без фьючерсной ноги."""
    now = int(time.time() * 1000)
    return {
        "binance": Ticker("binance", "SOL/USDT", bid=99, ask=100, last=100, volume=1_000_000, timestamp=now),
        "bybit": Ticker("bybit", "SOL/USDT", bid=99, ask=100, last=100, volume=1_000_000, timestamp=now),
    }


def make_spot_spread_prices():
    """
    Спот с РАЗНЫМИ ценами по биржам.

    Нужен PaperTrader (инвентарному): он торгует спот против спота, поэтому
    разъезжаться должны спотовые котировки. Для SpotFuturesPaperTrader
    наоборот — обе биржи на одном уровне (make_prices), чтобы весь спред
    давал фьючерс, как в реальной связке спот/перпетуал.
    """
    now = int(time.time() * 1000)
    return {
        "binance": Ticker("binance", "SOL/USDT", bid=99, ask=100, last=100,
                          volume=1_000_000, timestamp=now),
        "bybit": Ticker("bybit", "SOL/USDT", bid=105, ask=106, last=105,
                        volume=1_000_000, timestamp=now),
    }


def make_futures(symbol="SOL/USDT", bid=105, ask=106, ex="bybit"):
    """Фьючерс/перп: по СИМВОЛУ внутри лежит биржа (в отличие от спота)."""
    now = int(time.time() * 1000)
    return {symbol: {ex: Ticker(ex, symbol, bid=bid, ask=ask, last=bid, volume=1_000_000, timestamp=now)}}


class TestBalanceManager:
    def test_free_total_reserved_initial_state(self):
        bm = BalanceManager({"binance": {"USDT": 1000, "SOL": 5}})
        assert bm.total("binance", "USDT") == 1000
        assert bm.reserved_amount("binance", "USDT") == 0
        assert bm.free("binance", "USDT") == 1000

    def test_can_buy_and_reserve_usdt(self):
        bm = BalanceManager({"binance": {"USDT": 1000}})
        assert bm.can_buy("binance", 500)
        assert bm.reserve("binance", "USDT", 600)
        assert bm.free("binance", "USDT") == 400
        assert not bm.can_buy("binance", 500)

    def test_can_sell_and_reserve_base_asset(self):
        bm = BalanceManager({"bybit": {"SOL": 10}})
        assert bm.can_sell("bybit", "SOL", 3)
        assert bm.reserve("bybit", "SOL", 4)
        assert bm.free("bybit", "SOL") == 6
        assert not bm.can_sell("bybit", "SOL", 7)

    def test_release_never_makes_reserved_negative(self):
        bm = BalanceManager({"binance": {"USDT": 1000}})
        assert bm.reserve("binance", "USDT", 100)
        bm.release("binance", "USDT", 500)
        assert bm.reserved_amount("binance", "USDT") == 0
        assert bm.free("binance", "USDT") == 1000

    def test_apply_buy_fill_updates_usdt_and_base(self):
        bm = BalanceManager({"binance": {"USDT": 1000, "SOL": 0}})
        assert bm.reserve("binance", "USDT", 101)
        bm.apply_buy_fill("binance", "SOL/USDT", amount_base=1, cost_usdt=100, fee_usdt=1)
        assert bm.total("binance", "USDT") == pytest.approx(899)
        assert bm.total("binance", "SOL") == pytest.approx(1)
        assert bm.reserved_amount("binance", "USDT") == 0

    def test_apply_sell_fill_updates_base_and_usdt(self):
        bm = BalanceManager({"bybit": {"USDT": 100, "SOL": 2}})
        assert bm.reserve("bybit", "SOL", 1)
        bm.apply_sell_fill("bybit", "SOL/USDT", amount_base=1, proceeds_usdt=110, fee_usdt=0.5)
        assert bm.total("bybit", "SOL") == pytest.approx(1)
        assert bm.total("bybit", "USDT") == pytest.approx(209.5)
        assert bm.reserved_amount("bybit", "SOL") == 0

    def test_apply_fill_rejects_insufficient_balance(self):
        bm = BalanceManager({"binance": {"USDT": 50}})
        with pytest.raises(ValueError):
            bm.apply_buy_fill("binance", "SOL/USDT", amount_base=1, cost_usdt=100, fee_usdt=1)


class TestInventoryPaperTrader:
    def test_inventory_trade_happy_path_updates_balances(self):
        bm = BalanceManager({
            "binance": {"USDT": 1000, "SOL": 0},
            "bybit": {"USDT": 1000, "SOL": 5},
        })
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = PaperTrader(portfolio, balance_manager=bm)

        trade = asyncio.run(trader.execute(make_opp(), make_spot_spread_prices()))

        assert trade is not None
        assert trade.pnl > 0
        assert bm.total("binance", "USDT") < 1000
        assert bm.total("binance", "SOL") > 0
        assert bm.total("bybit", "USDT") > 1000
        assert bm.total("bybit", "SOL") < 5
        assert bm.reserved_amount("binance", "USDT") == 0
        assert bm.reserved_amount("bybit", "SOL") == 0

    def test_inventory_trade_rejected_without_sell_inventory(self):
        bm = BalanceManager({
            "binance": {"USDT": 1000, "SOL": 0},
            "bybit": {"USDT": 1000, "SOL": 0},
        })
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = PaperTrader(portfolio, balance_manager=bm)

        trade = asyncio.run(trader.execute(make_opp(), make_spot_spread_prices()))

        assert trade is None
        assert bm.total("binance", "USDT") == 1000
        assert bm.total("bybit", "SOL") == 0

    def test_inventory_trade_rejected_without_buy_usdt(self):
        bm = BalanceManager({
            "binance": {"USDT": 50, "SOL": 0},
            "bybit": {"USDT": 1000, "SOL": 5},
        })
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = PaperTrader(portfolio, balance_manager=bm)

        trade = asyncio.run(trader.execute(make_opp(), make_spot_spread_prices()))

        assert trade is None
        assert bm.total("binance", "USDT") == 50
        assert bm.total("bybit", "SOL") == 5





class TestSpotFuturesPaperTrader:
    """
    Контракт изменился: execute() ОТКРЫВАЕТ пару и возвращает None.
    Закрытие происходит в manage_open_positions() по ценам выхода.

    Раньше эти тесты передавали execute() только спотовые котировки, а
    фьючерсная нога молча подставлялась из того же словаря. Спред считался
    «спот против спота» — фантомный, без второй ноги. Теперь фьючерс
    передаётся явно через make_futures().
    """

    def _open(self, trader, opp=None, spot=(99, 100), fut=(105, 106)):
        """Открыть пару: спот на binance, шорт фьючерса на bybit."""
        return asyncio.run(trader.execute(
            opp or make_opp(), make_prices(),
            futures_prices=make_futures(bid=fut[0], ask=fut[1])))

    def _manage(self, trader, spot=(99, 100), fut=(105, 106)):
        """
        Структуры для manage_open_positions() отличаются от execute():
        здесь оба словаря ключуются СИМВОЛОМ, а не биржей.

        execute():   prices = {exchange: Ticker}
                     futures_prices = {symbol: {exchange: Ticker}}
        manage():    prices = {symbol: {exchange: Ticker}}
                     futures_prices = {symbol: {exchange: Ticker}}

        Несогласованность подписей — источник ошибок, поэтому она
        зафиксирована здесь явно.
        """
        now = int(time.time() * 1000)
        spot_map = {"SOL/USDT": {
            "binance": Ticker("binance", "SOL/USDT", bid=spot[0], ask=spot[1],
                              last=spot[0], volume=1_000_000, timestamp=now),
            "bybit": Ticker("bybit", "SOL/USDT", bid=spot[0], ask=spot[1],
                            last=spot[0], volume=1_000_000, timestamp=now),
        }}
        fut_map = make_futures(bid=fut[0], ask=fut[1])
        return trader.manage_open_positions(spot_map, fut_map)

    def test_open_buys_spot_and_reserves_futures_margin(self):
        """execute() открывает пару: спот куплен, маржа зарезервирована."""
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, futures_leverage=1,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )

        assert self._open(trader) is None, "execute() на открытии возвращает None"
        assert len(trader.open_pairs) == 1
        p = next(iter(trader.open_pairs.values()))
        assert p.buy_exchange == "binance" and p.sell_exchange == "bybit"
        assert p.amount > 0
        # спот куплен, маржа фьючерса зарезервирована
        assert bm.total("binance", "SOL") > 0 or bm.reserved_amount("binance", "SOL") > 0
        assert bm.reserved_amount("bybit", "USDT") > 0, "маржа не зарезервирована"

    def test_pair_survives_until_manage_is_called(self):
        """Ключевое отличие от старого бага: пара НЕ закрывается сама."""
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, futures_leverage=1,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        self._open(trader)
        for _ in range(10):
            assert self._manage(trader) == []
            assert len(trader.open_pairs) == 1

    def test_close_releases_reserves_and_settles_both_legs(self):
        """Закрытие освобождает резервы и возвращает средства."""
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, futures_leverage=1, target_close_pct=0.15,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        self._open(trader)
        # фьючерс падает к споту: basis был +5%, станет ~+0.1% — сходимость
        closed = self._manage(trader, fut=(99.4995, 99.6995))

        assert len(closed) == 1
        assert not trader.open_pairs
        assert bm.reserved_amount("bybit", "USDT") == 0, "маржа не освобождена"
        assert bm.reserved_amount("binance", "SOL") == 0, "спот не освобождён"
        assert closed[0].success is True
        assert closed[0].pnl > 0, f"PnL {closed[0].pnl:+.4f} не положителен"

    def test_funding_in_hours_reduces_pnl(self):
        """
        Регрессия на единицы: funding_rate_per_hour тарифицируется за ЧАС.
        8 часов удержания обязаны стоить в 8 раз дороже часа, а не зависеть
        от числа вызовов manage_open_positions().
        """
        def pnl_after(hours):
            bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
            pf = Portfolio(balance=2000, initial_balance=2000)
            t = SpotFuturesPaperTrader(
                pf, bm, futures_leverage=1, target_close_pct=0.15,
                funding_rate_per_hour=0.001, min_latency_ms=0, max_latency_ms=0,
                latency_drift_bps_per_sec=0)
            self._open(t)
            p = next(iter(t.open_pairs.values()))
            p.opened_at = time.time() - hours * 3600.0
            c = self._manage(t, fut=(99.4995, 99.6995))
            assert len(c) == 1
            return c[0].pnl

        assert pnl_after(8.0) < pnl_after(0.0), "funding не влияет на PnL"

    def test_rejected_without_spot_usdt(self):
        bm = BalanceManager({"binance": {"USDT": 50}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, futures_leverage=1,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        assert self._open(trader) is None
        assert not trader.open_pairs
        assert bm.total("binance", "USDT") == 50
        assert bm.total("bybit", "USDT") == 1000

    def test_rejected_without_futures_margin(self):
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 50}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, futures_leverage=1,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        assert self._open(trader) is None
        assert not trader.open_pairs
        assert bm.total("binance", "USDT") == 1000
        assert bm.total("bybit", "USDT") == 50

    def test_rejected_without_futures_ticker(self):
        """
        Нет фьючерсной котировки => отказ, а не подстановка спота.
        Прежний код молча брал спотовый тикер и рисовал фантомный спред.
        """
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, futures_leverage=1,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        assert asyncio.run(trader.execute(make_opp(), make_prices(),
                                          futures_prices={})) is None
        assert not trader.open_pairs
        assert trader.rejected_counters.get("no_futures_ticker", 0) == 1

    def test_rejected_when_api_failure_occurs(self):
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, api_failure_prob=1.0,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        assert self._open(trader) is None
        assert not trader.open_pairs
        assert bm.total("binance", "USDT") == 1000
        assert bm.total("bybit", "USDT") == 1000

    def test_rejected_when_liquidity_below_min_fill(self):
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, available_liquidity_pct=0.000001, min_fill_ratio=0.9,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        assert self._open(trader) is None
        assert not trader.open_pairs
        assert bm.total("binance", "USDT") == 1000
        assert bm.total("bybit", "USDT") == 1000

    def test_partial_fill_reduces_position_size(self):
        """
        Частичное исполнение уменьшает размер позиции.

        Размер изолирован от Kelly: на bootstrap-этапе calculate_position_size
        возвращает ровно MIN_TRADE_SIZE (100), поэтому ЛЮБОЙ haircut уводит
        size ниже порога и сделка отклоняется как size_out_of_bounds. Это не
        баг fill-логики, а свойство bootstrap-размера, поэтому расчёт размера
        подменён — проверяется именно срез по ликвидности.

        Окно частичного филла: min_fill_ratio < liquidity < 1.
        """
        bm = BalanceManager({"binance": {"USDT": 10000},
                             "bybit": {"USDT": 10000}})
        portfolio = Portfolio(balance=20000, initial_balance=20000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, available_liquidity_pct=0.85, min_fill_ratio=0.8,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        trader.risk.calculate_position_size = lambda opp=None, mss=0: 500.0
        self._open(trader)
        assert trader.open_pairs, "филл выше min_fill_ratio должен проходить"
        p = next(iter(trader.open_pairs.values()))
        assert p.amount < 5.0, f"частичный филл не урезан: {p.amount}"
        assert p.amount > 4.0, f"срез слишком сильный: {p.amount}"
        assert p.size_usdt == pytest.approx(500.0 * 0.85, rel=0.02)
    def test_rejected_when_liquidity_below_min_fill_ratio(self):
        """Ниже порога вход запрещён: забирать меньше min_fill_ratio бессмысленно."""
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        portfolio = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            portfolio, bm, available_liquidity_pct=0.5, min_fill_ratio=0.8,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )
        assert self._open(trader) is None
        assert not trader.open_pairs
        assert trader.rejected_counters.get("liquidity_too_low_for_min_fill", 0) == 1