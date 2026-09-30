# -*- coding: utf-8 -*-
"""
Тест целостности точки входа.

Стоимость этого файла определяется одним фактом: Hunter, Telegram и Database
были потеряны при пересборке engine.py, и об этом не знал НИ ОДИН тест.

146 тестов были зелёными, `python -c "import app"` падал с
ImportError. Модуль, который никто не импортировал в тестах, не был
проверен — а именно он запускает систему.

Этот файл закрывает класс поломок «модуль компилируется, но не импортируется».
"""
import importlib
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_engine_imports():
    """Базовый engine импортируется."""
    assert importlib.import_module("engine") is not None


def test_app_imports():
    """
    КЛЮЧЕВОЙ ТЕСТ: app.py обязан импортироваться.

    Именно эта проверка отсутствовала, и из-за этого поломка
    `ImportError: cannot import name 'Hunter'` жила незамеченной.
    """
    app = importlib.import_module("app")
    assert app is not None
    assert hasattr(app, "main"), "в app.py должна быть точка входа main()"


def test_app_import_does_not_execute_trading():
    """
    Импорт app.py не должен запускать торговлю.

    Если импорт начнёт что-то делать (подключаться к биржам, открывать файл
    БД, слать сообщения), тестовый прогон станет опасным. Сейчас защита
    условная: проверяем, что main() — функция, а не вызов на верхнем уровне.
    """
    app = importlib.import_module("app")
    assert callable(app.main), "main() должен быть функцией"
    src = inspect.getsource(app)
    # на верхнем уровне (без отступа) вызовов быть не должно
    top_level_calls = [
        line for line in src.splitlines()
        if line and not line[0].isspace()
        and line.rstrip().endswith("()")
        and not line.lstrip().startswith(("def ", "class ", "async def ", "#", "if __name__"))
    ]
    assert not top_level_calls, \
        f"на верхнем уровне app.py выполняются вызовы: {top_level_calls}"


def test_every_name_app_imports_from_engine_exists():
    """
    Каждое имя из блока `from engine import (...)` в app.py должно
    существовать в engine.

    Именно так проявляется потеря класса: движок компилируется, тесты
    модуля зелёные, а точка входа падает на импорте.
    """
    import engine

    with open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()

    marker = "from engine import ("
    assert marker in src, "не найден блок импорта engine в app.py"
    block = src.split(marker, 1)[1].split(")", 1)[0]

    names = [n.strip() for n in block.replace("\n", " ").split(",") if n.strip()]
    assert names, "блок импорта пуст — разбор не удался"

    missing = [n for n in names if not hasattr(engine, n)]
    assert not missing, f"engine не содержит импортируемых имён: {missing}"


@pytest.mark.parametrize("cls_name,required", [
    ("Hunter", ["select"]),
    ("Telegram", ["start", "stop", "send"]),
    ("Database", [
        "start", "stop",
        "save_opportunity", "save_open_trade", "close_trade",
        "get_open_trades", "save_trade",
        "save_portfolio_state", "load_portfolio_state",
        "delete_portfolio_state",
    ]),
])
def test_recovered_classes_have_methods_app_calls(cls_name, required):
    """
    Восстановленные из old/ классы должны содержать все методы,
    которые вызывает app.py. Иначе ImportError сменится AttributeError
    в рантайме — то есть на живом прогоне, а не на тестах.
    """
    import engine

    cls = getattr(engine, cls_name, None)
    assert cls is not None, f"engine.{cls_name} отсутствует"

    for method in required:
        assert hasattr(cls, method), f"{cls_name}.{method} отсутствует"
        assert callable(getattr(cls, method)), \
            f"{cls_name}.{method} не является вызываемым"


def test_database_and_managing_are_reachable():
    """
    Связка бота: трейдер умеет управлять открытыми парами, а БД умеет
    их сохранять и отдавать при старте.

    Без этого звена бот открывает пары и забывает их — именно тот дефект,
    ради которого затевался Шаг 0.
    """
    import engine

    trader = engine.SpotFuturesPaperTrader(
        engine.Portfolio(balance=1000, initial_balance=1000),
        engine.BalanceManager({"binance": {"USDT": 500}, "bybit": {"USDT": 500}}))
    assert hasattr(trader, "manage_open_positions"), \
        "нет manage_open_positions: пары не будут закрываться"
    assert hasattr(trader, "snapshot_open"), \
        "нет snapshot_open: состояние пар не переживёт перезапуск"
    assert hasattr(trader, "restore_open"), \
        "нет restore_open: пары потеряются при рестарте"

    assert hasattr(engine.Database, "save_open_trade")
    assert hasattr(engine.Database, "get_open_trades")
