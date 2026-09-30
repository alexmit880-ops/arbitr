# -*- coding: utf-8 -*-
"""
Восстановление открытых пар после перезапуска (Шаг 0.7).

Критично для прогона на сервере: любой перезапуск (деплой, OOM,
перезагрузка) иначе оставлял позиции без управления навсегда.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (
    BalanceManager, Portfolio, SpotFuturesPaperTrader, Ticker,
)

SYM = "SOL/USDT"


def _trader():
    return SpotFuturesPaperTrader(
        Portfolio(balance=2000, initial_balance=2000),
        BalanceManager({"mexc": {"USDT": 1000}, "bybit": {"USDT": 1000}}))


def _db_row(**over):
    """Ровно те поля, которые отдаёт Database.get_open_trades()."""
    row = {"id": 1, "symbol": SYM, "buy_ex": "mexc", "sell_ex": "bybit",
           "size_usdt": 100.0, "buy_price": 100.0, "sell_price": 105.0,
           "category": "other"}
    row.update(over)
    return row


def test_restore_returns_nonzero_for_db_row():
    """
    КЛЮЧЕВОЙ ТЕСТ.

    Раньше restore_open() возвращал 0 для реальной строки из БД: имена
    полей не совпадали (buy_ex против buy_exchange, buy_price против
    entry_spot), пара падала с TypeError, ошибка ловилась - и пары
    терялись МОЛЧА.
    """
    t = _trader()
    assert t.restore_open([_db_row()]) == 1
    assert len(t.open_pairs) == 1


def test_restored_pair_has_usable_size():
    """
    Пара без amount=0 не закроется: делить нечего, PnL всегда ноль.
    """
    t = _trader()
    t.restore_open([_db_row()])
    p = t.open_pairs[SYM]
    assert p.amount > 0, f"amount={p.amount} - позиция не закроется"
    assert p.size_usdt == 100.0
    assert p.entry_spot == 100.0
    assert p.entry_fut == 105.0
    assert abs(p.entry_basis - 5.0) < 0.01


def test_restored_pair_can_actually_close():
    """Восстановленная пара обязана закрываться, иначе она мёртвая."""
    t = _trader()
    t.restore_open([_db_row()])
    p = t.open_pairs[SYM]
    p.opened_at = time.time() - (p.hold_hours * 3600 + 60)

    def tk(b, a):
        return Ticker("mexc", SYM, b, a, (b + a) / 2, 1e6,
                      int(time.time() * 1000))

    closed = t.manage_open_positions(
        {SYM: {"mexc": tk(100, 100.2)}},
        {SYM + ":USDT": {"bybit": tk(105, 105.2)}})
    assert len(closed) == 1, "восстановленная пара не закрылась по таймауту"


def test_incomplete_row_is_rejected_loudly():
    """
    Запись без цены или размера БЕСПОЛЕЗНА и не должна молча
    восстанавливаться как нулевая позиция.
    """
    t = _trader()
    assert t.restore_open([_db_row(buy_price=0)]) == 0
    assert not t.open_pairs
    assert t.restore_open([_db_row(size_usdt=0)]) == 0
    assert not t.open_pairs


def test_empty_rows_are_noop():
    t = _trader()
    assert t.restore_open([]) == 0


def test_app_calls_restore_on_startup():
    """
    Метод восстановления без вызова бесполезен: раньше он был определён,
    но не вызывался нигде - пары терялись при каждом рестарте.
    """
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    called = any(
        isinstance(n, ast.Attribute) and n.attr == "restore_open"
        for n in ast.walk(tree))
    assert called, "app.py не вызывает restore_open() при старте"
