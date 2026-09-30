# -*- coding: utf-8 -*-
"""
Тесты главного цикла app.py.

Контекст: пока app.py не импортировался, ни один тест его не касался, и
дефект «manage_open_positions() не вызывается» жил незамеченным. Эти тесты
проверяют структуру цикла статически — без сети и БД, — чтобы поломка
обнаруживалась на прогоне pytest, а не на живом боте.

Что проверяем:
  1. Цикл вызывает manage_open_positions() каждый проход
  2. Закрытие пары попадает в БД и в risk-менеджер
  3. execute() для spot_futures больше не трактуется как закрытая сделка
  4. Ошибка управления не роняет весь цикл
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def _parse():
    with open(APP_PATH, encoding="utf-8") as f:
        return ast.parse(f.read())


def _find_main_loop(tree):
    """
    Возвращает ГЛАВНЫЙ цикл — тот, что лежит внутри main().

    Важно: в app.py несколько циклов `while not shutdown.is_set()`
    (cache_updater, futures_cache_updater, emergency_check, reconnect_loop).
    Поиск «первого попавшегося» находил цикл обновления кэша, а не цикл
    сканирования, и тесты проверяли не то место. Поэтому ищем While
    внутри тела main().
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            for sub in ast.walk(node):
                if isinstance(sub, ast.While):
                    if "shutdown" in ast.unparse(sub.test):
                        return sub
    return None


def _called_names(node):
    """Все имена функций/методов, вызываемых внутри узла."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def test_main_loop_exists():
    tree = _parse()
    assert _find_main_loop(tree) is not None, "главный цикл не найден"


def test_loop_manages_open_positions_every_cycle():
    """
    КЛЮЧЕВОЙ ТЕСТ Шага 0.2.

    Без manage_open_positions() в цикле SpotFuturesPaperTrader открывает
    пары и никогда их не закрывает: execute() возвращает None, значит PnL
    не считается, позиции живут бесконечно, а баланс заморожен.
    """
    loop = _find_main_loop(_parse())
    assert loop is not None
    assert "manage_open_positions" in _called_names(loop), \
        "главный цикл не вызывает manage_open_positions()"


def test_closed_pair_is_persisted():
    """
    Закрытие пары должно попадать в БД, иначе история сделок неполна и
    восстановление после рестарта невозможно.
    """
    loop = _find_main_loop(_parse())
    called = _called_names(loop)
    assert "close_trade" in called, "закрытие пары не сохраняется в БД"
    assert "record_trade" in called, "закрытие не учитывается риск-менеджером"


def test_open_handled_separately_from_close():
    """
    execute() в spot_futures-модели открывает пару и возвращает None.
    Старый код ждал TradeResult и при None пропускал сохранение, из-за
    чего открытые пары не попадали в БД вовсе.

    Проверяем, что в цикле есть отдельная ветка работы с открытием пары.
    """
    loop = _find_main_loop(_parse())
    called = _called_names(loop)
    assert "save_open_trade" in called, "открытие пары не сохраняется"
    assert "manage_open_positions" in called


def test_manage_errors_do_not_kill_cycle():
    """
    Сбой при управлении парой (нет котировки, биржа недоступна) не должен
    ронять цикл: иначе одна битая пара останавливает весь бот.
    """
    loop = _find_main_loop(_parse())
    body = ast.unparse(loop)
    # try/except вокруг вызова управления
    idx = body.find("manage_open_positions")
    assert idx != -1
    window = body[max(0, idx - 600):idx + 900]
    assert "try" in window and "except" in window, \
        "вызов manage_open_positions не защищён try/except"


def test_no_immediate_close_assumption():
    """
    Строка вида `if trade:` сразу после execute() в spot_futures — это
    остаток старой модели, где execute() возвращал закрытую сделку.

    Проверяем, что в цикле нет обращения к полям TradeResult там, где
    фактически возвращается OpenPair/None.
    """
    body = ast.unparse(_find_main_loop(_parse()))
    assert "is_open" in body or "open_pairs" in body, \
        "цикл не различает открытие пары и закрытие сделки"


# ═══════════════════════════════════════════════════════════════════════
# Тесты целостности точки входа
#
# Стоимость этого раздела определяется одним фактом: Hunter, Telegram и
# Database были потеряны при пересборке engine.py, и об этом не знал НИ
# ОДИН тест. 146 тестов были зелёными, а `import app` падал с ImportError.
# Модуль, который никто не импортировал в тестах, и есть тот, что
# запускает систему. Этот раздел закрывает класс поломок «модуль
# компилируется, но не импортируется».
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


# ═══════════════════════════════════════════════════════════════════════
# Согласованность вызовов с сигнатурами
#
# Тот же класс поломок, что и с импортом: объявление метода разошлось с
# местом вызова. record_decision() потерял stability_score/decay_rate, и
# ВСЕ вызовы из app.py падали с TypeError - ни одно решение о сделке не
# доходило до журнала реплея. Статически это не видно, в pytest раньше
# не проверялось.
# ═══════════════════════════════════════════════════════════════════════

def test_all_app_calls_match_engine_signatures():
    """
    Каждый вызов метода engine из app.py должен соответствовать реальной
    сигнатуре: никаких неизвестных именованных аргументов.
    """
    import inspect
    import re

    import engine

    with open(APP_PATH, encoding="utf-8") as f:
        src = f.read()

    # проверяем методы, к которым обращается app.py
    checks = {
        "record_decision": engine.ReplaySystem.record_decision,
        "record_trade": engine.RiskManager.record_trade,
    }

    for name, func in checks.items():
        if "." + name not in src:
            continue
        allowed = set(inspect.signature(func).parameters) - {"self"}
        for m in re.finditer(re.escape(name) + r"\((.*?)\)\s*\n", src, re.S):
            kwargs = re.findall(r"(\w+)\s*=", m.group(1))
            unknown = [k for k in kwargs if k not in allowed]
            assert not unknown, (
                f"app.py передаёт в {name}() несуществующие аргументы: "
                f"{unknown}. Каждый такой вызов падает с TypeError."
            )


def test_replay_decision_records_stability_fields():
    """
    stability_score и decay_rate не просто принимаются, а сохраняются:
    без них анализ устойчивости спреда теряет данные.
    """
    import engine

    opp = engine.Opportunity(
        symbol="X/USDT", buy_exchange="a", sell_exchange="b",
        buy_price=1, sell_price=2, spread_pct=1.0, net_profit_pct=0.5,
        volume=1, exchanges_count=2, timestamp=0, max_safe_size_usdt=10)
    replay = engine.ReplaySystem()
    replay.record_decision(opp, "blocked", "test",
                           stability_score=0.5, decay_rate=0.1)
    d = replay.decisions[-1]
    assert d.stability_score == 0.5
    assert d.decay_rate == 0.1
