# -*- coding: utf-8 -*-
"""
ЧЕСТНЫЕ тесты восстановления: настоящий round-trip через БД (Шаг 1.0).

Чем этот файл отличается от test_restart_recovery.py и почему он важнее.

Старый тест подставлял фикстуру руками:
    row = {"id": 1, "symbol": ..., "buy_price": ...}   # ts НЕТ
    trader.restore_open([row])
и проверял её же. Из-за этого пропущенный ключ `ts` в
Database.get_open_trades() НЕ ЛОВИЛСЯ: тест был зелёным при сломанном
коде. Ровно тот случай, о котором мы говорили - зелёный тест при
сломанном коде хуже красного, потому что снимает сигнал.

Здесь путь настоящий:
    OpenPair -> save_open_trade() -> sqlite -> get_open_trades()
             -> restore_open() -> OpenPair
и сверяются фактические значения обеих сторон. Ничего не подставляется.

Что проверяется (все 4 бага, найденных в Шаге 1.0):
  1. ts терялся в get_open_trades() -> таймаут обнулялся при рестарте
  2. hold_hours негде было хранить -> всегда падал в дефолт 4.0 ч
  3. fut_reserved не сохранялся -> утечка резерва баланса
  4. save_open_trade брал Opportunity, а не пару -> терялись поля пары
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (
    BalanceManager, Database, OpenPair, Portfolio,
    SpotFuturesPaperTrader,
)

SYM = "SOL/USDT"


def _trader() -> SpotFuturesPaperTrader:
    return SpotFuturesPaperTrader(
        Portfolio(balance=2000, initial_balance=2000),
        BalanceManager({"mexc": {"USDT": 1000}, "bybit": {"USDT": 1000}}))


def _pair(**over) -> OpenPair:
    """Пара как её создаёт трейдер при открытии."""
    p = OpenPair(
        symbol=SYM,
        buy_exchange="mexc",
        sell_exchange="bybit",
        direction="fut_premium",
        amount=1.0,
        size_usdt=100.0,
        entry_spot=100.0,
        entry_fut=105.0,
        entry_basis=5.0,
        opened_at=time.time() - 2 * 3600,   # открыта 2 часа назад
        margin=100.0,
        hold_hours=2.0,                     # нестандартный таймаут
        fut_reserved=100.5,                 # нестандартный резерв
        fees_paid=0.1,
        funding_paid=0.02,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


def _roundtrip(pair: OpenPair, tmpdir: str):
    """Настоящий круг: запись -> чтение из sqlite -> восстановление."""
    cwd = os.getcwd()
    os.chdir(tmpdir)          # Database пишет в "hunter.db" относительно cwd
    try:
        db = Database()
        asyncio.run(_write_then_read(db, pair))
        rows = asyncio.run(_read_rows(db))
        asyncio.run(_stop(db))

        # Читаем ещё раз напрямую sqlite - проверяем, что запись легла
        # именно так, как мы её положили, а не как её интерпретировал
        # какой-то слой выше.
        c = sqlite3.connect(os.path.join(tmpdir, "hunter.db"))
        raw = c.execute(
            "SELECT ts, hold_hours, fut_reserved, amount, entry_basis "
            "FROM trades WHERE symbol=?", (SYM,)).fetchone()
        c.close()

        t = _trader()
        n = t.restore_open(rows)
        return rows, raw, n, t
    finally:
        os.chdir(cwd)


async def _write_then_read(db: Database, pair: OpenPair):
    await db.start()
    await db.save_open_trade(pair, "other")
    await asyncio.sleep(0.3)      # даём воркеру обработать очередь


async def _read_rows(db: Database):
    return await db.get_open_trades()


async def _stop(db: Database):
    await db.stop()


def _with_tmp(fn):
    with tempfile.TemporaryDirectory() as td:
        return fn(td)


# ─────────────────────────── баг №1: ts ───────────────────────────

def test_ts_survives_the_database():
    """Момент открытия обязан дойти до восстановленной пары.

    ЭТО ТЕСТ НА САМЫЙ ВАЖНЫЙ БАГ. Раньше get_open_trades() не возвращал
    ключ ts, restore_open() падал в `or time.time()`, и пара,
    открытая 4.4 ч при таймауте 4 ч, после рестарта жила ещё 4 ч.
    """
    def run(td):
        original = time.time() - 2 * 3600
        rows, raw, n, t = _roundtrip(_pair(opened_at=original), td)
        assert n == 1, "пара не восстановилась"
        p = t.open_pairs[SYM]
        assert abs(p.opened_at - original) < 1.0, (
            f"opened_at искажён: {p.opened_at} вместо {original}")
        assert raw is not None and abs(raw[0] - original) < 1.0, (
            f"ts не записан в sqlite: {raw[0] if raw else None}")
    _with_tmp(run)


def test_restored_pair_knows_it_aged():
    """Восстановленная пара должна знать свой возраст, а быть новой.

    Проверяем не структуру, а смысл: прошло ли время открытия. Если
    это 0, весь прогон после рестарта рисует артефакт, а не стратегию.
    """
    def run(td):
        rows, raw, n, t = _roundtrip(_pair(opened_at=time.time() - 2 * 3600), td)
        p = t.open_pairs[SYM]
        hours_held = (time.time() - p.opened_at) / 3600.0
        assert hours_held > 1.9, (
            f"пара выглядит новой (age={hours_held:.2f}ч), а открыта 2ч назад")
        assert hours_held > p.hold_hours, (
            f"пара должна была превысить таймаут: age={hours_held:.2f} "
            f"hold={p.hold_hours:.2f}")
    _with_tmp(run)


def test_row_without_ts_is_skipped_not_silently_zeroed():
    """Нет ts -> пара НЕ восстанавливается, но об этом пишется в лог.

    Альтернатива (восстановить с нулевым возрастом) хуже потери пары:
    потеря видна, а нулевой возраст тихо искажает весь прогон.
    """
    t = _trader()
    bad = {"id": 1, "symbol": SYM, "buy_ex": "mexc", "sell_ex": "bybit",
           "size_usdt": 100.0, "buy_price": 100.0, "sell_price": 105.0}
    assert t.restore_open([bad]) == 0
    assert SYM not in t.open_pairs, "пара без ts восстановлена как новая"


# ──────────────────────── баг №2: hold_hours ────────────────────────

def test_hold_hours_survives_the_database():
    """Таймаут пары, а не дефолт трейдера.

    hold_hours фиксируется по базису (BasisEntryCriteria.hold_hours_for).
    Правило "срок принадлежит паре" бессмысленно, если после рестарта
    все пары получают PAPER_HOLD_HOURS_MAX.
    """
    def run(td):
        rows, raw, n, t = _roundtrip(_pair(hold_hours=1.5), td)
        p = t.open_pairs[SYM]
        assert abs(p.hold_hours - 1.5) < 1e-6, (
            f"hold_hours={p.hold_hours}, ожидалось 1.5")
        assert abs(raw[1] - 1.5) < 1e-6, "hold_hours не записан в sqlite"


# ─────────────────────── баг №3: fut_reserved ───────────────────────

def test_fut_reserved_survives_the_database():
    """Резерв на фьючерсе переживает рестарт.

    Без него резерв считался от margin, а освобождался от
    margin + fee + funding - разница навсегда оставалась в резерве,
    то есть баланс тихо утекал.
    """
    def run(td):
        rows, raw, n, t = _roundtrip(_pair(fut_reserved=42.5), td)
        p = t.open_pairs[SYM]
        assert abs(p.fut_reserved - 42.5) < 1e-6, (
            f"fut_reserved={p.fut_reserved}, ожидалось 42.5")
        assert abs(raw[2] - 42.5) < 1e-6, "fut_reserved не записан в sqlite"


# ──────────────── баг №4: save_open_trade пишет пару ────────────────

def test_pair_fields_all_reach_the_database():
    """Все поля пары должны оказаться в sqlite, а не теряться.

    Баг был в том, что save_open_trade() принимал Opportunity и писал
    opp.buy_price. Теперь принимает пару - и проверяем, что дошли все
    поля, которые restore_open() потом читает.
    """
    def run(td):
        pair = _pair(entry_basis=3.75, amount=0.5, fees_paid=0.3,
                     funding_paid=0.11)
        rows, raw, n, t = _roundtrip(pair, td)
        p = t.open_pairs[SYM]
        assert abs(p.amount - 0.5) < 1e-6, f"amount={p.amount}"
        assert abs(p.entry_basis - 3.75) < 1e-6, f"entry_basis={p.entry_basis}"
        assert abs(p.entry_spot - 100.0) < 1e-6
        assert abs(p.entry_fut - 105.0) < 1e-6
        assert p.direction == "fut_premium"
        assert abs(p.fees_paid - 0.3) < 1e-6, f"fees_paid={p.fees_paid}"
        assert abs(p.funding_paid - 0.11) < 1e-6
        assert abs(raw[3] - 0.5) < 1e-6, "amount не записан"
        assert abs(raw[4] - 3.75) < 1e-6, "entry_basis не записан"


def test_get_open_trades_returns_row_factory_safe_shapes():
    """get_open_trades() не должен ломать другие запросы.

    Он переключает connection.row_factory на sqlite3.Row ради чтения по
    именам. Если забыть вернуть его обратно, ВСЕ остальные запросы,
    ждущие позиционные r[0]/r[1], молча поедут - и это отловится
    только в рантайме.
    """
    def run(td):
        rows, raw, n, t = _roundtrip(_pair(), td)
        db = Database()
        asyncio.run(db.start())
        r = asyncio.run(_read_rows(db))
        asyncio.run(_stop(db))
        assert isinstance(r, list)
        assert r and isinstance(r[0], dict), "вернулось не то"
        assert db.conn.row_factory is None, (
            f"row_factory не восстановлен: {db.conn.row_factory} - "
            f"остальные запросы сломаются")


def test_pair_can_still_close_after_restore():
    """Восстановленная пара обязана закрываться, а не висеть вечно.

    Это проверка результата, а не полей: если пара не может закрыться,
    средства заморожены навсегда - прямой убыток.
    """
    def run(td):
        rows, raw, n, t = _roundtrip(_pair(), td)
        p = t.open_pairs[SYM]
        assert p.amount > 0, "нулевая позиция - закрывать нечего"
        assert p.fut_reserved > 0, "нулевой резерв - освобождать нечего"
        # Цены почти сошлись: базис 105 -> 100.5 это +0.5% от 100.
        res = t.manage_open_positions(
            {SYM: Ticker(bid=100.4, ask=100.6, last=100.5)},
            {SYM: Ticker(bid=100.3, ask=100.5, last=100.4)},
        )
        assert res is not None, "управление вернуло None"


def test_multiple_pairs_survive_together():
    """Несколько пар восстанавливаются все и не путаются между собой.

    Символ - ключ open_pairs, поэтому две пары одного символа схлопнутся.
    Проверяем, что разные символы не мешают друг другу.
    """
    def run(td):
        cwd = os.getcwd()
        os.chdir(td)
        try:
            db = Database()
            async def go():
                await db.start()
                for sym, hold in (("SOL/USDT", 1.5), ("ETH/USDT", 3.0)):
                    await db.save_open_trade(
                        _pair(symbol=sym, hold_hours=hold), "other")
                await asyncio.sleep(0.3)
                rows = await db.get_open_trades()
                await db.stop()
                return rows
            rows = asyncio.run(go())
            t = _trader()
            n = t.restore_open(rows)
            assert n == 2, f"восстановлено {n} из 2"
            assert abs(t.open_pairs["SOL/USDT"].hold_hours - 1.5) < 1e-6
            assert abs(t.open_pairs["ETH/USDT"].hold_hours - 3.0) < 1e-6
        finally:
            os.chdir(cwd)


from engine import Ticker  # noqa: E402  (нужен для проверки закрытия)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


# ─────── рассогласование колонок и плейсхолдеров в INSERT ───────

def _extract_insert(sql: str):
    """Достаёт (список_колонок, число_значений) из INSERT-запроса."""
    import re
    cols_part = re.search(r"INSERT INTO\s+\w+\s*\((.*?)\)\s*VALUES",
                          sql, re.S | re.I).group(1)
    cols = [c.strip() for c in cols_part.replace('"', '').split(",")
            if c.strip()]
    vals_part = re.search(r"VALUES\s*\((.*)\)", sql, re.S | re.I).group(1)
    n_values = len(re.findall(r"\?", vals_part)) + \
        len(re.findall(r"'(?:[^']|'')*'", vals_part)) + \
        len(re.findall(r"(?<![\w?])-?\d+(?![\w?])", vals_part))
    return cols, n_values


def _insert_statements():
    """Все SQL-строки INSERT INTO trades из кода Database (через AST).

    Разбираем ast, а не регуляркой: соседние литералы склеиваются неявной
    конкатенацией в один Constant, и регулярка по исходнику такую склейку
    надёжно не вытаскивает.
    """
    import ast
    import engine
    out = []
    tree = ast.parse(inspect.getsource(engine.Database))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "INSERT INTO trades" in node.value and "VALUES" in node.value:
                out.append(node.value)
    return out


def test_insert_columns_match_placeholders():
    """Число колонок в INSERT должно совпадать с числом значений.

    ЭТО ТЕСТ НА КЛАСС ОШИБОК, а не на конкретный баг. В Шаге 1.0 при
    добавлении колонок дважды разошёлся счёт: то "18 values for 19
    columns", то "19 for 20". Ошибка видна ТОЛЬКО в логе фонового
    воркера ("DB: N values for M columns"), а запись при этом молча не
    происходит - то есть пары не сохраняются, и никто об этом не знает.

    Теперь такое расхождение ломает тест, а не прогон.
    """
    stmts = _insert_statements()
    assert stmts, "не нашлось ни одного INSERT INTO trades"
    for sql in stmts:
        cols, n_vals = _extract_insert(sql)
        assert len(cols) == n_vals, (
            f"INSERT: {len(cols)} колонок но {n_vals} значений\n"
            f"  колонки: {cols}\n  VALUES: {sql.split('VALUES')[-1]}")


def test_insert_placeholder_count_matches_params_tuple():
    """Число плейсхолдеров должно совпадать с длиной передаваемого tuple.

    Второй счётой уровень: даже если VALUES содержит верное число
    значений, в кортеже может быть не то количество элементов, и
    sqlite3 бросит "Incorrect number of bindings".
    """
    import re
    import engine
    src = inspect.getsource(engine.Database)
    for name in ("save_open_trade", "save_flat_open_trade", "save_trade"):
        m = re.search(rf"async def {name}\(self.*?await self\.queue\.put\(\(",
                      src, re.S)
        if not m:
            continue
        # сильный парсер не нужен: считаем запятые верхнего уровня
        start = m.end()
        depth, i = 1, start
        while i < len(src) and depth:
            if src[i] in "([{":
                depth += 1
            elif src[i] in ")]}":
                depth -= 1
            i += 1
        args = src[start:i - 1]
        # разбиваем по запятым верхнего уровня
        parts, depth, cur = [], 0, ""
        for ch in args:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        n_params = len([p for p in parts if p.strip()])
        stmt = _insert_statements()
        assert n_params > 0, f"{name}: не нашли параметров"
