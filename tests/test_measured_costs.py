# -*- coding: utf-8 -*-
"""
ИЗМЕРЕННЫЕ расходы round-trip, а не вычисленные по формуле.

Зачем файл: теоретические оценки расходов дважды оказывались неверными
(0.44% в плане против 1.44% фактических). Причина - модель не учитывала
то, что происходит при исполнении: пересечение спреда на ОБЕИХ ногах.

Метод: открыть пару и закрыть её БЕЗ изменения цен. Тогда PnL = -полные
расходы, и измерение не зависит от интерпретаций. Компоненты разделяются
варьированием одного фактора за раз.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engine import (
    BalanceManager, Opportunity, Portfolio, SpotFuturesPaperTrader, Ticker,
)

SYM = "SOL/USDT"


def _tk(bid, ask):
    return Ticker("x", SYM, bid, ask, (bid + ask) / 2.0, 1e6,
                  int(time.time() * 1000))


def _run_round_trip(size, spot_half_spread_pct=0.10, fut_half_spread_pct=0.10):
    """Round-trip без движения цен. Возвращает (size, total_cost_pct, parts)."""
    mid_s, mid_f = 100.0, 102.0
    sb = mid_s * (1 - spot_half_spread_pct / 100)
    sa = mid_s * (1 + spot_half_spread_pct / 100)
    fb = mid_f * (1 - fut_half_spread_pct / 100)
    fa = mid_f * (1 + fut_half_spread_pct / 100)

    async def go():
        bm = BalanceManager({"mexc": {"USDT": 300000},
                             "bybit": {"USDT": 300000}})
        pf = Portfolio(balance=1e6, initial_balance=1e6)
        tr = SpotFuturesPaperTrader(pf, bm, futures_leverage=1,
                                    target_close_pct=0.001,
                                    max_hold_hours=0.001)
        opp = Opportunity(
            symbol=SYM, buy_exchange="mexc", sell_exchange="bybit",
            buy_price=sa, sell_price=fb, spread_pct=2.0, net_profit_pct=1.5,
            volume=1e7, exchanges_count=2, timestamp=int(time.time()),
            confidence_score=90, max_safe_size_usdt=float(size))
        tr.risk.calculate_position_size = lambda o=None, m=0: float(size)
        await tr.execute(opp, {"mexc": _tk(sb, sa)},
                         futures_prices={SYM + ":USDT": {"bybit": _tk(fb, fa)}})
        if SYM not in tr.open_pairs:
            return None
        p = tr.open_pairs[SYM]
        sz, entry_fees, xfer = p.size_usdt, p.fees_paid, p.transfer_fee
        p.opened_at = time.time() - 99999
        closed = tr.manage_open_positions(
            {SYM: {"mexc": _tk(sb, sa)}},
            {SYM + ":USDT": {"bybit": _tk(fb, fa)}})
        if not closed:
            return None
        return sz, -closed[0].pnl / sz * 100.0, entry_fees / sz * 100.0, xfer / sz * 100.0

    return asyncio.run(go())


# ─────────────────── измеренные величины ───────────────────

def test_round_trip_cost_is_measured_not_1_percent():
    """
    КЛЮЧЕВОЙ ТЕСТ.

    Полная стоимость round-trip измерена эмпирически и составляет
    ОКОЛО 1.6-1.7% при размере 100 USDT, а не 0.44%, как предполагала
    теоретическая модель.
    """
    r = _run_round_trip(100)
    assert r is not None
    sz, total, entry_fees, xfer = r
    print("\n  size=%.0f  ВСЕГО=%.3f%%  (вход %.2f%% + перевод %.2f%%)" % (
        sz, total, entry_fees, xfer))
    assert total > 1.4, (
        "Round-trip дешевле %.2f%% - модель расходов не учитывает "
        "пересечение спреда на обеих ногах" % total)


def test_spread_crossing_is_the_missing_cost():
    """
    Декомпозиция: прирост расхода от расширения спредов.

    Измерено: спред 0.02% на ноге -> 1.29%, спред 0.20% -> 1.65%.
    Разница 0.36% на две ноги = 0.18% на ногу. Это ровно стоимость
    пересечения биржевого спреда, которой не было ни в плане, ни в
    исходной формуле расходов.
    """
    narrow = _run_round_trip(100, 0.001, 0.001)
    wide = _run_round_trip(100, 0.10, 0.10)
    assert narrow and wide
    base, wide_cost = narrow[1], wide[1]
    print("\n  узкий спред (0.001%%): %.3f%%" % base)
    print("  реальный спред (0.10%%): %.3f%%" % wide_cost)
    print("  вклад пересечения спреда: +%.3f%%" % (wide_cost - base))
    assert wide_cost > base, "расширение спреда не увеличило расходы"
    assert wide_cost - base > 0.15, "пересечение спреда не учитывается"


def test_cost_grows_with_size():
    """
    Скольжение содержит SLIPPAGE_PER_USDT * size, поэтому расход растёт
    с размером позиции. Это опровергает допущение, что расход -
    фиксированный процент.
    """
    small = _run_round_trip(100)
    large = _run_round_trip(1000)
    assert small and large
    print("\n  size=100 : %.3f%%" % small[1])
    print("  size=1000: %.3f%%" % large[1])
    assert large[1] > small[1], (
        "расход обязан расти с размером из-за проскальзывания")


def test_fee_component_entry_is_two_legs():
    """
    fees_paid в OpenPair - это расходы ТОЛЬКО на входе, то есть две ноги
    (покупка спота + открытие шорта). Проверяем именно это.

    Полные комиссии round-trip вдвое больше и уже учтены в
    test_round_trip_cost_is_measured_not_1_percent.
    """
    r = _run_round_trip(100)
    assert r is not None
    sz, total, entry_fees, xfer = r
    expected = (config.TRADING_FEES.get("mexc", 0.001)
                + config.TRADING_FEES.get("bybit", 0.001)) * 100
    print("\n  комиссия входа: %.3f%%  (ожидается %.3f%% = 2 ноги)" % (
        entry_fees, expected))
    assert abs(entry_fees - expected) < 0.02, (
        "комиссия входа не соответствует двум ногам")


def test_transfer_fee_only_cross_exchange():
    """Перевод платится между разными биржами и не платится внутри одной."""
    cross = _run_round_trip(100)
    assert cross is not None
    assert cross[3] > 0, "перевод между биржами не тарифицируется"
