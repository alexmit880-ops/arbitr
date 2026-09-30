"""Tests for ShadowLogger and upgraded SpotFuturesPaperTrader."""
import asyncio
import time
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (
    BalanceManager, Opportunity, SpotFuturesPaperTrader, Portfolio, Ticker,
    ShadowLogger,
)


def make_opp(symbol="SOL/USDT", buy="binance", sell="bybit", conf=80, net=2.0):
    return Opportunity(
        symbol=symbol, buy_exchange=buy, sell_exchange=sell,
        buy_price=100, sell_price=105, spread_pct=5.0, net_profit_pct=net,
        volume=1_000_000, exchanges_count=2, timestamp=int(time.time()),
        confidence_score=conf, max_safe_size_usdt=500,
    )


def make_prices():
    """Только СПОТ: обе биржи на одном уровне, чтобы спред определялся
    исключительно фьючерсом (make_futures), а не спот-vs-спот."""
    now = int(time.time() * 1000)
    return {
        "binance": Ticker("binance", "SOL/USDT", bid=99, ask=100, last=100,
                          volume=1_000_000, timestamp=now),
        "bybit": Ticker("bybit", "SOL/USDT", bid=99, ask=100, last=100,
                        volume=1_000_000, timestamp=now),
    }


def make_futures(symbol="SOL/USDT", bid=105, ask=106, ex="bybit"):
    now = int(time.time() * 1000)
    return {symbol: {ex: Ticker(ex, symbol, bid=bid, ask=ask, last=bid,
                                volume=1_000_000, timestamp=now)}}


class TestShadowLogger:
    """
    Shadow-лог фиксирует и ОТКАЗЫ, и фактические результаты.

    Контракт execute() изменился: он ОТКРЫВАЕТ пару и возвращает None,
    а результат появляется только после manage_open_positions(). Поэтому
    успешный кейс проверяется через полный жизненный цикл: вход -> сходимость
    -> закрытие -> событие CLOSE в логе.

    Фьючерсные котировки передаются явно (make_futures): структура
    futures_prices — {symbol: {exchange: Ticker}}, и раньше фьючерсная нога
    молча бралась из спотового словаря, давая фантомный спред.
    """

    def _trader(self, sl, **kw):
        bm = BalanceManager({"binance": {"USDT": 1000}, "bybit": {"USDT": 1000}})
        pf = Portfolio(balance=2000, initial_balance=2000)
        args = dict(shadow_logger=sl, min_latency_ms=0, max_latency_ms=0,
                    latency_drift_bps_per_sec=0, target_close_pct=0.15)
        args.update(kw)
        return SpotFuturesPaperTrader(pf, bm, **args)

    def _close(self, trader, fut=(99.4995, 99.6995)):
        """Прогоняет один цикл управления с заданными ценами фьючерса."""
        now = int(time.time() * 1000)
        spot_map = {"SOL/USDT": {
            ex: Ticker(ex, "SOL/USDT", bid=99, ask=100, last=99.5,
                       volume=1_000_000, timestamp=now)
            for ex in ("binance", "bybit")}}
        return trader.manage_open_positions(spot_map, make_futures(
            bid=fut[0], ask=fut[1]))

    def test_logs_rejected_trades(self):
        """Отказ логируется с указанием причины (здесь — нехватка USDT)."""
        sl = ShadowLogger(enabled=True)
        bm = BalanceManager({"binance": {"USDT": 50}, "bybit": {"USDT": 1000}})
        pf = Portfolio(balance=2000, initial_balance=2000)
        trader = SpotFuturesPaperTrader(
            pf, bm, shadow_logger=sl,
            min_latency_ms=0, max_latency_ms=0, latency_drift_bps_per_sec=0,
        )

        assert asyncio.run(trader.execute(make_opp(), make_prices(),
                                          futures_prices=make_futures())) is None
        stats = sl.stats()
        assert stats["total"] > 0
        assert stats["rejected"] > 0
        reasons = sl.rejected_by_reason()
        assert len(reasons) > 0
        assert any("usdt" in r for r in reasons)

    def test_logs_successful_trades(self):
        """Успешный кейс = полный цикл: вход, затем сходимость и закрытие."""
        sl = ShadowLogger(enabled=True)
        trader = self._trader(sl)

        assert asyncio.run(trader.execute(make_opp(), make_prices(),
                                          futures_prices=make_futures())) is None
        assert len(trader.open_pairs) == 1, "пара должна открыться и ждать"
        closed = self._close(trader)
        assert len(closed) == 1, "сходимость не закрыла пару"

        stats = sl.stats()
        assert stats["total"] > 0
        assert stats["traded"] > 0
        assert stats["avg_synthetic_pnl"] != 0

    def test_logs_close_event_with_exit_reason(self):
        """
        Лог обязан содержать СОБЫТИЕ ЗАКРЫТИЯ с причиной выхода.

        Без этого нельзя отличить реальную сходимость от таймаута или стопа —
        а именно это главный вопрос при разборе доходности стратегии.
        """
        sl = ShadowLogger(enabled=True)
        trader = self._trader(sl)
        asyncio.run(trader.execute(make_opp(), make_prices(),
                                   futures_prices=make_futures()))
        self._close(trader)

        closes = [e for e in sl.entries if e.get("event") == "CLOSE"]
        assert len(closes) == 1
        assert closes[0]["exit_reason"] == "CONVERGED"
        assert "basis_exit" in closes[0]

    def test_synthetic_vs_latent_pnl_when_no_latency(self):
        """
        Synthetic (оценка по спреду на входе) и latent (фактический PnL)
        обязаны совпадать ПО ЗНАКУ: без задержки исполнения одно не может
        быть плюсом, а другое минусом. Расхождение знака означает, что
        shadow-оценка не соответствует реальности.
        """
        sl = ShadowLogger(enabled=True)
        trader = self._trader(sl)
        asyncio.run(trader.execute(make_opp(), make_prices(),
                                   futures_prices=make_futures()))
        self._close(trader)

        entries = [e for e in sl.entries
                   if not e["rejected"] and e.get("event") != "CLOSE"]
        assert len(entries) > 0, "вход не залогирован"
        e = entries[0]
        assert (e["synthetic_pnl"] > 0) == (e["latent_pnl"] > 0), \
            "synthetic и latent разошлись по знаку"

    def test_rejected_by_reason_details(self):
        sl = ShadowLogger(enabled=True)
        trader = self._trader(sl, api_failure_prob=1.0)
        assert asyncio.run(trader.execute(make_opp(), make_prices(),
                                          futures_prices=make_futures())) is None
        reasons = sl.rejected_by_reason()
        assert "api_failure" in reasons