# -*- coding: utf-8 -*-
"""
РЕГРЕССИИ хранения логов и разбора стакана.

Оба теста закрывают реальные находки, а не абстрактные пожелания.

1. log_volume. hunter.log разросся до 50 MB, потому что ротация стояла
   на 50 MB / 7 дней, а 88% строк составляли повторы одного и того же
   события (зомби по 800 символам за прогон). Закрепляем: ротация не
   выше 25 MB, повторы схлопываются, разные символы не склеиваются.

2. orderbook_levels. okx отдаёт уровни тройкой [price, amount, count],
   а код ждал пару. Падало "too many values to unpack (expected 2)"
   44 раза за прогон - весь L2-анализ okx не работал, сделки молча
   отклонялись как blocked.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import OrderBookExecutionAnalyzer as O


# ─────────────────────────── формат стакана ───────────────────────────

def test_vwap_accepts_okx_triples():
    """okx: [price, amount, num_orders] - третье поле лишнее."""
    triples = [[118.83, 70.436264, 0], [119.00, 50.0, 3]]
    vwap, filled = O._vwap(triples, 200.0, "buy")
    assert vwap > 0 and filled > 0, "тройки okx не разобрались"
    # VWAP должен лежать между ценами уровней, а не быть 0
    assert 118.0 < vwap < 120.0, f"VWAP вне диапазона стакана: {vwap}"


def test_vwap_accepts_pairs():
    """bybit/mexc/gate отдают пары - поведение не должно сломаться."""
    vwap, filled = O._vwap([[100.0, 2.0], [101.0, 2.0]], 150.0, "buy")
    assert vwap > 0 and filled > 0


def test_vwap_same_result_for_pair_and_triple():
    """Один и тот же стакан в двух форматах даёт ОДИНАковый VWAP.

    Это главное свойство: третье поле - служебное (число заявок) и не
    должно влиять на расчёт.
    """
    pair = [[100.0, 1.0], [101.0, 1.0]]
    triple = [[100.0, 1.0, 7], [101.0, 1.0, 9]]
    assert O._vwap(pair, 200.0, "buy") == O._vwap(triple, 200.0, "buy")


def test_depth_usdt_accepts_triples():
    """_depth_usdt имел ту же уязвимость, что и _vwap."""
    assert O._depth_usdt([[100.0, 2.0, 0]]) == 200.0


def test_garbage_levels_are_skipped_not_fatal():
    """Мусор в стакане не должен ронять анализ - как и раньше."""
    assert O._vwap([[None, None], ["a", "b"], [0, 0]], 100.0, "buy") == (0.0, 0.0)
    assert O._depth_usdt([[], [1], None, 5]) == 0.0


# ─────────────────────────── объём логов ───────────────────────────

def _record(msg, level="INFO"):
    """Минимальный loguru-совместимый record для прямого вызова фильтра."""
    return {"message": msg, "level": level, "time": 0.0, "extra": {}}


def test_repeat_filter_suppresses_duplicates():
    """Одинаковые сообщения в пределах окна схлопываются."""
    from app import RepeatFilter
    f = RepeatFilter(window_sec=60)
    assert f(_record("Trade failed on SOL")) is True, "первый должен пройти"
    for _ in range(50):
        assert f(_record("Trade failed on SOL")) is False
    assert f.total_suppressed == 50


def test_repeat_filter_keeps_distinct_messages():
    """Разные символы - разные сообщения. Склеивать их нельзя.

    Иначе потерялась бы сама диагностика: "упал SOL" и "упал ETH"
    должны оставаться разными строками.
    """
    from app import RepeatFilter
    f = RepeatFilter(window_sec=60)
    assert f(_record("Trade failed on SOL")) is True
    assert f(_record("Trade failed on ETH")) is True
    assert f(_record("Trade failed on BTC")) is True


def test_repeat_filter_ignores_changing_numbers():
    """Меняющиеся числа не должны обходить подавление.

    Именно этот случай и съедал диск: "ZOMBIE: SOL/USDT - cv:0.000000"
    и "cv:0.010000" - разные строки текста, но один и тот же шум.
    """
    from app import RepeatFilter
    f = RepeatFilter(window_sec=60)
    assert f(_record("ZOMBIE: SOL/USDT - cv:0.000000")) is True
    assert f(_record("ZOMBIE: SOL/USDT - cv:0.010000")) is False
    assert f(_record("ZOMBIE: SOL/USDT - cv:0.020000")) is False


def test_repeat_filter_counts_on_next_occurrence():
    """Подавленные повторы не теряются: счётчик всплывает позже."""
    from app import RepeatFilter
    f = RepeatFilter(window_sec=60)
    f(_record("ZOMBIE: SOL/USDT - cv:0.0"))
    for _ in range(9):
        f(_record("ZOMBIE: SOL/USDT - cv:0.1"))
    # окно истекло - сообщение проходит вместе с итогом
    f._last_seen = {k: 0.0 for k in f._last_seen}
    rec = _record("ZOMBIE: SOL/USDT - cv:0.2")
    assert f(rec) is True
    assert "ещё 9" in rec["message"], "счётчик подавленных потерян"


def test_flush_summary_reports_totals():
    """Итог при остановке: иначе непонятно, был шум или нет."""
    from app import RepeatFilter
    f = RepeatFilter(window_sec=60)
    f(_record("ORPHAN TRADE on startup: EGLD/USDT"))
    for _ in range(7):
        f(_record("ORPHAN TRADE on startup: EGLD/USDT"))
    summary = f.flush_summary()
    assert "Подавлено повторов" in summary
    assert "x7" in summary


def test_log_rotation_bounds_are_sane():
    """Ротация не должна разрешать разрастание файла до 50 MB.

    Зафиксировано на 25 MB / 3 дня: при наблюдаемом объёме это сутки
    работы вместо недели, а место под ботом ограничено.
    """
    import app
    assert app.LOG_MAX_MB <= 25, f"ротация слишком крупная: {app.LOG_MAX_MB} MB"
    assert app.LOG_KEEP_DAYS <= 3, f"слишком долго храним: {app.LOG_KEEP_DAYS} дней"


def test_zombie_messages_are_not_warning_level():
    """Зомби не должны идти на WARNING - это был источник шума.

    Раньше каждая новая зомби-пара логировалась как WARNING, и при 800
    символах это были сотни строк за прогон. Теперь событие видно на
    дашборде (панель ZOMBIES) и в агрегате zombies=N/M, а в файл
    попадает только по запросу на DEBUG.
    """
    import inspect
    import app
    src = inspect.getsource(app)
    assert 'logger.warning(f"⚰️ ZOMBIE' not in src, "зомби снова на WARNING"
    assert 'logger.info(f"✅ RECOVERED' not in src, "RECOVERED снова на INFO"
    assert 'logger.debug(f"ZOMBIE' in src, "зомби должны быть на DEBUG"

# ─────────────────────── размер БД (хранение) ───────────────────────

def test_db_uses_wal_and_indexes_ts():
    """WAL + индекс по ts обязательны, иначе чистка не работает.

    Без journal_mode=WAL удаление старых строк не возвращает место,
    а DELETE по ts без индекса сканирует всю таблицу целиком - на
    2.5 млн строк это секунды задержки в цикле записи.
    """
    import inspect
    import engine
    src = inspect.getsource(engine.Database._writer)
    assert "PRAGMA journal_mode=WAL" in src, "WAL не включён"
    assert "idx_opportunities_ts" in src, "нет индекса по ts"


def test_cleanup_keeps_recent_drops_old():
    """Старые строки удаляются, свежие остаются."""
    import asyncio, sqlite3, time, os
    import engine
    p = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "_cleanup_test.db")
    if os.path.exists(p):
        os.remove(p)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE opportunities (id INTEGER PRIMARY KEY, ts INTEGER)")
    now = int(time.time())
    conn.executemany("INSERT INTO opportunities (ts) VALUES (?)",
                     [(now - 90000,), (now - 90000,), (now - 10,)])
    conn.commit()

    db = engine.Database.__new__(engine.Database)
    db.conn = conn
    db._writes_since_cleanup = 0
    engine.DB_OPPORTUNITIES_KEEP_SEC = 86400
    asyncio.run(db._cleanup_old_opportunities())

    left = conn.execute("select count(*) from opportunities").fetchone()[0]
    conn.close()
    os.remove(p)
    assert left == 1, "осталось %d строк, ожидалась 1 (свежая)" % left
