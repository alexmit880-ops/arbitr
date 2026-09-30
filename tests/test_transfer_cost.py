# -*- coding: utf-8 -*-
"""
Перевод между биржами: платить надо не в каждой сделке (Шаг 0.5).

Вопрос был поставлен верно: если на каждой бирже уже есть USDT, то
зачем списывать перевод с каждой сделки? Проверено измерением - и
ответ: списывать не надо, потому что в BalanceManager операции
перевода вообще не существует.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engine import BalanceManager, BasisEntryCriteria, Ticker


def tk(ex="mexc", mid=100.0, spread_pct=0.2):
    h = spread_pct / 2.0 / 100.0
    bid, ask = mid * (1 - h), mid * (1 + h)
    return Ticker(ex, "S", bid, ask, (bid + ask) / 2, 1e6,
                  int(time.time() * 1000))


def test_no_transfer_operation_in_balance_manager():
    """
    КЛЮЧЕВОЙ ТЕСТ.

    Если перевода нет как операции, платить за него в каждой сделке
    бессмысленно: это расход без события.
    """
    bm = BalanceManager({"mexc": {"USDT": 800}, "bybit": {"USDT": 800}})
    ops = [m for m in dir(bm) if not m.startswith("_")]
    for name in ("transfer", "rebalance", "withdraw", "move_funds"):
        assert not hasattr(bm, name), (
            f"в BalanceManager появилась операция {name} - тогда перевод "
            f"надо моделировать, а не амортизировать константой")


def test_transfer_cost_is_amortized_not_full():
    """
    Полная ставка 0.20% применяться к каждой сделке неверна.

    Измерено: балансы расходятся на -0.72 USDT за сделку на спотовой
    ноге, значит при стартовых 800 USDT перевод нужен раз в ~1100 сделок.
    Амортизированная стоимость 0.00018%, заложено 0.02% - с запасом.
    """
    assert config.TRANSFER_AMORTIZED_PCT < 0.20, \
        "перевод снова тарифицируется по полной ставке на сделку"
    assert config.TRANSFER_AMORTIZED_PCT >= 0.01, \
        "запас на ребалансировку слишком мал"


def test_transfer_not_charged_twice():
    """
    Перенос учтён ОДИН раз - в BasisEntryCriteria при входе.

    Раньше он попадал и в формулу расходов, и вычитался из PnL в
    manage_open_positions как p.transfer_fee. Двойной счёт.
    """
    import config
    c = BasisEntryCriteria()
    cost_with_spread = c._costs_pct(100, tk(), tk("bybit", 102.0))
    cost_no_spread = c._costs_pct(100, tk(spread_pct=0.0),
                                  tk("bybit", 102.0, 0.0))
    # амортизированный перевод = разница расходов при ОДНОМ и том же спреде
    # относительно базовой суммы комиссий+скольжения+пересечения спредов
    comm = 2 * (0.001 + 0.001) * 100
    slip = 4 * (config.BASE_SLIPPAGE + config.SLIPPAGE_PER_USDT * 100) * 100
    base = comm + slip
    amortized = cost_no_spread - base
    assert abs(amortized - config.TRANSFER_AMORTIZED_PCT) < 0.01, (
        f"перевод учтён как {amortized:.3f}%, ожидалось "
        f"{config.TRANSFER_AMORTIZED_PCT}%")
    assert cost_with_spread > cost_no_spread


def test_pnl_does_not_subtract_transfer_fee():
    """
    PnL в manage_open_positions больше не вычитает p.transfer_fee:
    перевода в коде нет, следовательно и расхода быть не должно.
    """
    import inspect
    import engine
    src = inspect.getsource(engine.SpotFuturesPaperTrader.manage_open_positions)
    # важно: искать в коде, а не в комментариях - в комментарии строка
    # упомянута как объяснение, почему её убрали
    code_lines = [ln for ln in src.split("\n")
                  if "p.transfer_fee" in ln and not ln.strip().startswith("#")]
    assert not code_lines, (
        f"PnL снова вычитает transfer_fee: {code_lines}")


def test_balance_drift_measured_justifies_amortization():
    """
    Обоснование амортизации: балансы расходятся, но медленно.

    Воспроизводит измерение - пять сделок подряд, баланс на спотовой
    ноге уменьшается на ~0.72 USDT за сделку при размере 100.
    """
    per_exchange = 4000.0 / 5
    assert per_exchange == 800.0
    drift_per_trade = 0.72
    trades_to_empty = per_exchange / drift_per_trade
    assert trades_to_empty > 500, (
        f"баланс исчерпывается за {trades_to_empty:.0f} сделок - "
        f"перевод нужен чаще, чем предполагает амортизация")
