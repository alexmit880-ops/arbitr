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
import time
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


# ────────────��── уборка БД: пакетность и потолок времени ───────────────────

def _fresh_db(tmp_path, old_n=0, new_n=0):
    """Мини-база opportunities как в проде (с индексом по ts)."""
    import sqlite3, time
    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE opportunities (id INTEGER PRIMARY KEY, ts INTEGER)")
    c.execute("CREATE INDEX idx_opportunities_ts ON opportunities(ts)")
    now = int(time.time())
    # ВАЖНО: ts должен быть заведомо старше cutoff (now - 24ч).
    # Раньше здесь было "now - 90000 + i", и при i > 3600 строка внутри
    # блока "старых" оказывалась СВЕЖЕЙ - тест проверял не то и падал
    # с "осталось 197400". Отсчитываем вниз от заведомо старой точки.
    rows = [(now - 200000 - i,) for i in range(old_n)] + \
           [(now - i,) for i in range(new_n)]
    c.executemany("INSERT INTO opportunities (ts) VALUES (?)", rows)
    c.commit()
    return c


def test_cleanup_is_time_capped(tmp_path):
    """Уборка не должна блокировать запись дольше потолка.

    Замер (2026-09-30): массовый DELETE 13.8 млн строк занимал 21 с, а
    цикл бота - около 3 с, то есть эти секунды съедали торговые окна.
    """
    import asyncio, engine
    c = _fresh_db(tmp_path, old_n=400000, new_n=1000)
    db = engine.Database.__new__(engine.Database)
    db.conn = c
    db._writes_since_cleanup = 0
    t = time.time()
    asyncio.run(db._cleanup_old_opportunities())
    elapsed = time.time() - t
    assert elapsed <= engine.DB_CLEANUP_MAX_SEC + 2.0, (
        "уборка заняла %.1f с, потолок %.1f" % (elapsed,
                                                 engine.DB_CLEANUP_MAX_SEC))
    c.close()


def test_cleanup_does_not_blow_up_wal(tmp_path):
    """Пакетами удаление не раздувает WAL.

    Замер: массовый DELETE дал WAL 271 МБ, пакетами по 50 тыс. - 0 МБ.
    """
    import asyncio, engine, os
    c = _fresh_db(tmp_path, old_n=300000, new_n=1000)
    db = engine.Database.__new__(engine.Database)
    db.conn = c
    db._writes_since_cleanup = 0
    asyncio.run(db._cleanup_old_opportunities())
    wal = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    wal_path = str(tmp_path / "t.db") + "-wal"
    wal_mb = os.path.getsize(wal_path) / 1048576 if os.path.exists(wal_path) else 0.0
    assert wal_mb < 20, "WAL раздулся до %.1f МБ" % wal_mb
    c.close()


def test_cleanup_removes_all_old_rows(tmp_path):
    """Пакетное удаление сходится: старых строк не остаётся."""
    import asyncio, engine
    c = _fresh_db(tmp_path, old_n=200000, new_n=1000)
    db = engine.Database.__new__(engine.Database)
    db.conn = c
    db._writes_since_cleanup = 0
    cutoff = int(time.time()) - engine.DB_OPPORTUNITIES_KEEP_SEC
    for _ in range(30):          # запас итераций: срабатывает потолок времени
        asyncio.run(db._cleanup_old_opportunities())
        left = c.execute("SELECT COUNT(*) FROM opportunities WHERE ts < ?",
                         (cutoff,)).fetchone()[0]
        if left == 0:
            break
    assert left == 0, "осталось %d старых строк" % left
    kept = c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    assert kept == 1000, "свежие строки затронуты: осталось %d" % kept
    c.close()


def test_vacuum_only_when_base_is_bloated(tmp_path):
    """VACUUM не гоняется впустую - только когда база распухла.

    Замер: с VACUUM на каждой уборке бот терял 44 СЕКУНДЫ в сутки.
    """
    import inspect, engine
    src = inspect.getsource(engine.Database._cleanup_old_opportunities)
    assert "freelist_count" in src, "нет проверки freelist перед VACUUM"
    assert "DB_VACUUM_FREE_RATIO" in src, "VACUUM без порога"
    # VACUUM должен идти ПОСЛЕ commit, иначе sqlite отвечает
    # "cannot VACUUM from within a transaction"
    i_commit = src.index("self.conn.commit()")
    i_vacuum = src.index('"VACUUM"')
    assert i_commit < i_vacuum, "VACUUM до commit - упадёт с ошибкой"


def test_cleanup_batch_and_cap_constants_are_sane():
    """Константы уборки не должны быть абсурдными."""
    import engine
    assert 1000 <= engine.DB_CLEANUP_BATCH <= 500000, engine.DB_CLEANUP_BATCH
    assert 0.1 <= engine.DB_CLEANUP_MAX_SEC <= 10.0, engine.DB_CLEANUP_MAX_SEC
    assert 0.05 <= engine.DB_VACUUM_FREE_RATIO <= 1.0, engine.DB_VACUUM_FREE_RATIO


# ─────────────── полнота requirements.txt ───────────────

def _third_party_imports() -> set:
    """Сторонние импорты верхнего уровня в app.py/engine.py/config.py."""
    import ast
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    std = set(sys.stdlib_module_names) | {"config", "engine", "__future__"}
    found = set()
    for fn in ("app.py", "engine.py", "config.py"):
        path = os.path.join(root, fn)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    found.add(node.module.split(".")[0])
    return found - std


def test_requirements_covers_all_imports():
    """Каждый сторонний импорт обязан быть в requirements.txt.

    Шаг 1.3: rich использовался в app.py пятью строками, но в
    requirements.txt его не было. На риге бот упал при первом запуске:
        ModuleNotFoundError: No module named 'rich'
    Тест ловит это ДО выкатки, а не на боевой машине.
    """
    req = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "requirements.txt")
    # Имя пакета в pip не обязано совпадать с именем модуля при импорте.
    # python-dotenv ставится как "python-dotenv", а импортируется как
    # "dotenv". Без этого соответствия тест ругается на пакет, который
    # в requirements.txt есть - ложное срабатывание.
    IMPORT_TO_PACKAGE = {"dotenv": "python-dotenv"}
    declared = set()
    for line in open(req, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            # "pkg==1.0", "pkg>=1.0", "pkg[extra]==1.0"
            name = line.split("==")[0].split(">=")[0].split("<=")[0]
            name = name.split(">")[0].split("<")[0].split("~=")[0]
            name = name.split("[")[0].strip()
            if name:
                declared.add(name.lower().replace("_", "-"))
    missing = sorted(
        m for m in _third_party_imports()
        if IMPORT_TO_PACKAGE.get(m, m).lower().replace("_", "-") not in declared
    )
    assert not missing, (
        f"импортируется в коде, но нет в requirements.txt: {missing}. "
        f"Бот упадёт на чистой машине с ModuleNotFoundError.")


def test_rich_is_declared():
    """Точечная проверка: rich нужен дашборду и был потерян."""
    req = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "requirements.txt")
    text = open(req, encoding="utf-8").read()
    assert "rich" in text, "rich не зафиксирован в requirements.txt"
    # Проверяем, что он реально доступен, а не только записан строкой.
    import importlib
    importlib.import_module("rich")


def test_requirements_pins_versions():
    """Зависимости зафиксированы - иначе риг и машина разъедутся."""
    req = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "requirements.txt")
    unpinned = []
    for line in open(req, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "==" not in line:
            unpinned.append(line)
    assert not unpinned, f"зафиксируйте версии: {unpinned}"


# ─────────────── дашборд и не-TTY вывод ───────────────

def test_dashboard_does_not_grab_screen_when_not_tty():
    """Без TTY дашборд не рисует и не пишет escape-коды в stdout.

    Шаг 1.4: было Live(..., screen=True) безусловно. При запуске через
    run_bot.sh вывод идёт в пайп (| tee), stdout не TTY - и rich всё
    равно рисовал, ЗАХВАТЫВАЯ экран. В терминале было пусто, создавая
    впечатление зависания, хотя бот работал. Для journalctl это тем
    более плохо: escape-последовательности забивают лог.
    """
    import inspect
    import app
    src = inspect.getsource(app)
    assert "screen=_interactive" in src, "screen не зависит от TTY"
    assert "isatty" in src, "нет проверки TTY"
    # Голое screen=True безусловно - это и был баг
    # Проверяем ИСПОЛНЯЕМЫЕ строки, а не весь исходник: screen=True
    # законно упоминается в комментарии, где описана причина бага.
    _code = "\n".join(ln for ln in src.splitlines()
                    if not ln.strip().startswith("#"))
    assert "screen=True" not in _code, "screen=True остался безусловным в коде"
    # console задавать НЕ нужно: screen=False достаточно, чтобы rich
    # ничего не рисовал. А вот console=False - ошибка типов (bool вместо
    # Console), которая роняет live.update(). Это проверяет отдельный
    # тест test_no_boolean_console_argument.


def test_loguru_sink_writes_utf8_and_rotates():
    """Файл лога должен открываться в UTF-8, иначе кириллица в логе
    превращается в мусор при просмотре через less/journalctl."""
    import inspect
    import app
    src = inspect.getsource(app.setup_file_logging)
    assert 'encoding="utf-8"' in src, "лог не в UTF-8"
    assert "rotation=" in src, "нет ротации"


# ──────── падение бота не теряет причину (Шаг 1.5) ────────

def test_crashes_are_logged_with_traceback():
    """Падение верхнего уровня обязано попасть в hunter.log.

    Шаг 1.5: __main__ ловил только KeyboardInterrupt, поэтому любое
    другое исключение уходило в stderr Python-трейсбеком. На риге это
    давало 40 строк "Unclosed client session" - шум сборки мусора
    УМИРАЮЩЕГО процесса, - и настоящая причина падения в этом шуме
    терялась. Разобрать падение было нечем.
    """
    import inspect
    import app
    src = inspect.getsource(app)
    assert "Бот упал" in src, "нет записи о падении"
    assert "format_exc" in src, "нет трейсбека"
    # Код выхода обязан быть ненулевым при падении, иначе systemd
    # с Restart=always решит, что это успех (см. Шаг 1.1).
    assert "sys.exit(_exit)" in src, "нет явного кода выхода"
    assert "_exit = 1" in src, "падение не даёт ненулевой код"


def test_log_buffer_is_flushed_before_exit():
    """logger.complete() обязателен: enqueue=True буферизует записи.

    Без него при аварийном выходе очередь не успевает дописаться, и
    трейсбек - ровно та строка, ради которой мы его пишем, - теряется.
    Проверено экспериментально: без complete() в логе ничего не было.
    """
    import inspect
    import app
    src = inspect.getsource(app)
    assert "logger.complete()" in src, "буфер логгера не сбрасывается"
    assert "enqueue=True" in inspect.getsource(app.setup_file_logging), (
        "если enqueue снят - complete() не обязателен, но проверь "
        "намерение: в комментарии должно быть сказано почему")


def test_no_boolean_console_argument():
    """console=bool - ошибка типов, которая роняет весь бот.

    Шаг 1.4 -> 1.5: стояло console=None if _interactive else False.
    rich ждёт объект Console, а получал bool, и следом
    live.update() падал с
        AttributeError: 'bool' object has no attribute 'set_live'
    То есть ломалось ВСЁ, а не только рисование дашборда. На риге это
    выглядело как падение через 42 секунды после подключения бирж.
    """
    import inspect
    import app
    src = inspect.getsource(app)
    _code = "\n".join(ln for ln in src.splitlines()
                      if not ln.strip().startswith("#"))
    assert "console=False" not in _code, (
        "console=False - bool вместо Console, бот падает с set_live")
    assert "else False" not in _code, "console получает bool"
    # screen всё ещё должен зависеть от TTY (это было полезное изменение)
    assert "screen=_interactive" in _code, "screen должен зависеть от TTY"


# ──────── запуск сервиса на Linux (Шаги 1.6-1.7) ────────

def test_entrypoint_is_executable_in_git():
    """run_bot.sh обязан лежать в git с битом исполнения (100755).

    Шаг 1.7: файл был 100644, поэтому на риге systemd давал
        status=203/EXEC
    Право выставлялось вручную chmod +x на КОПИИ в ~/scripts/, а
    ExecStart запускал deploy/run_bot.sh, где прав не было. На
    Windows chmod не работает и core.fileMode=false, поэтому бит
    обязан быть проставлен в индексе git явно.
    """
    import subprocess
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        ["git", "ls-files", "-s", "deploy/run_bot.sh"],
        cwd=root, capture_output=True, text=True).stdout.strip()
    assert out, "run_bot.sh не отслеживается git"
    mode = out.split()[0]
    assert mode == "100755", (
        f"режим {mode}, а нужен 100755 - на Linux будет 203/EXEC")


def test_service_runs_script_via_bash():
    """ExecStart должен идти через bash - тогда бит исполнения не важен.

    Даже при 100644 в репозитории (или после scp/распаковки, или на
    ФС с noexec) сервис запустится. Экономит разбор 203/EXEC.
    """
    import app  # noqa: F401 - не нужен, импорт ради проверки окружения
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "deploy", "arb-bot.service"),
                encoding="utf-8").read()
    _code = "\n".join(ln for ln in text.splitlines()
                      if not ln.strip().startswith("#"))
    assert "ExecStart=/bin/bash " in _code, (
        "ExecStart должен вызывать /bin/bash, а не запускать файл "
        "напрямую: иначе 203/EXEC при любом потерянном бите +x")
    assert "%h/arbitr" in _code, "путь должен быть через %h, а не жёсткий"


def test_gitattributes_forces_lf():
    """Для .sh и .service обязателен eol=lf (Шаг 1.6).

    С core.autocrlf=true на Windows скрипты приходили на Linux с CRLF,
    и shebang читался как "/bin/bash\r" - файл не существует.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".gitattributes")
    assert os.path.exists(path), "нет .gitattributes - CRLF вернётся"
    text = open(path, encoding="utf-8").read()
    assert "*.sh text eol=lf" in text, "нет правила для .sh"
    assert "*.service text eol=lf" in text, "нет правила для .service"


# ──────── изоляция systemd не должна ломать запуск (Шаг 1.8) ────────

def _service_executable_lines():
    """Исполняемые строки юнита (без комментариев)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "deploy", "arb-bot.service"),
                encoding="utf-8").read()
    return "\n".join(ln for ln in text.splitlines()
                      if ln.strip() and not ln.strip().startswith("#"))


def test_no_protect_home_readonly():
    """ProtectHome=read-only ломает запуск, потому что сервис идёт от root.

    Шаг 1.8: на Hive OS %h = /root, а ProtectHome делает /root доступным
    только для чтения. Бот при старте пишет на диск ДО включения лога:
        config.py:7  Path("logs").mkdir(exist_ok=True)
    и mkdir падал с PermissionError. Сообщение уходило только в stderr
    systemd, потому что hunter.log к тому моменту ещё не был настроен, -
    отсюда "тишина" в логе при status=1/FAILURE.

    ReadWritePaths не помогает: по документации systemd ProtectHome
    применяется ПОСЛЕ ReadWritePaths и закрывает домашний каталог целиком.
    """
    code = _service_executable_lines()
    assert "ProtectHome=read-only" not in code, (
        "ProtectHome=read-only запретит запись в ~/arbitr, и бот упадёт "
        "на mkdir logs/ до того, как лог вообще настроится")
    assert "ProtectHome=true" not in code, "ProtectHome=true тоже закрывает"
    # ReadWritePaths должен остаться - системные каталоги всё ещё полезно
    # закрыть, а рабочий каталог открыть.
    assert "ReadWritePaths=" in code, "нужен ReadWritePaths вместо ProtectHome"
    assert "ProtectSystem=full" in code, "системные каталоги стоит закрыть"


def test_config_creates_logs_before_logging_starts():
    """config.py пишет на диск при ИМПОРТЕ - это ловушка для диагностики.

    mkdir происходит до setup_file_logging(), поэтому любая ошибка записи
    не попадает в hunter.log. Тест фиксирует сам факт, чтобы при
    следующем рефакторинге не переставить порядок случайно.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = open(os.path.join(root, "config.py"), encoding="utf-8").read()
    app_src = open(os.path.join(root, "app.py"), encoding="utf-8").read()
    assert 'Path("logs").mkdir' in cfg, "config.py больше не создаёт logs/"
    i_import = app_src.index("from config import *")
    i_log = app_src.index("setup_file_logging()")
    assert i_import < i_log, (
        "config импортируется раньше настройки лога - значит ошибки "
        "записи в logs/ не попадут в hunter.log и будут видны только "
        "в stderr. Это ровно тот случай, из-за которого падение на риге "
        "выглядело как тишина.")


# ──────── скрипт запуска переживает отсутствие HOME (Шаг 1.9) ────────

def test_run_script_has_no_unbound_variable_risks():
    """Ни одна переменная не должна читаться без ":-" при set -u.

    Шаг 1.9: на риге под systemd скрипт падал с
        run_bot.sh: line 46: HOME: unbound variable
    systemd НЕ задаёт HOME, если у сервиса нет User=/Environment=.
    В интерактивном терминале HOME всегда есть, поэтому ошибка
    проявлялась ТОЛЬКО под сервисом - там, где её нельзя отладить
    обычным запуском скрипта руками.

    Конкретно $HOME: $1 я закрыл ещё в Шаге 1.2, а $HOME пропустил -
    и повторил ту же ошибку ровно в том же месте.
    """
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lines = open(os.path.join(root, "deploy", "run_bot.sh"),
                 encoding="utf-8").read().splitlines()
    # Переменные, объявленные через local= или присвоенные выше, безопасны
    assigned = set()
    unsafe = []
    for i, line in enumerate(lines, 1):
        # local объявления ВСЕ сразу, а не только первое: строка вида
        #   local cand script_dir home="${HOME:-}"
        # объявляет три переменные сразу, и разбор по одному имени
        # давал ложное срабатывание на $home.
        for nm in re.findall(r"local\s+([^;]+)", line):
            for part in nm.replace("=", " ").split():
                if re.match(r"^\w+$", part):
                    assigned.add(part)
        m = re.match(r"\s*(\w+)=\S", line)
        if m:
            assigned.add(m.group(1))
        if line.lstrip().startswith("#"):
            continue
        for var in re.findall(r"\$(?!\{)(\w+)", line):
            if var in ("0", "1", "2", "SECONDS", "PIPESTATUS", "BASH_SOURCE",
                       "RANDOM", "LINENO", "FUNCNAME", "SEC", "PPID", "IFS",
                       "PATH", "EUID", "UID", "HOSTNAME", "PWD"):
                continue        # всегда заданы самим bash
            if var in assigned:
                continue
            unsafe.append((i, var, line.strip()[:60]))
    assert not unsafe, (
        f"переменные без защиты и без присваивания: {unsafe}. "
        f"Под systemd (где HOME не задан) это 'unbound variable'.")


def test_home_is_guarded_explicitly():
    """$HOME обязан читаться как ${HOME:-} - это и есть суть фикса."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "deploy", "run_bot.sh"),
                encoding="utf-8").read()
    _code = "\n".join(ln for ln in text.splitlines()
                      if not ln.strip().startswith("#"))
    assert '"$HOME/arbitr"' not in _code, (
        "сырое $HOME/arbitr упадёт под systemd - используй ${HOME:-}")
    assert "${HOME:-}" in _code, "нет защиты ${HOME:-}"


def test_script_runs_without_home_env(tmp_path):
    """Скрипт обязан запускаться с ПУСТЫМ окружением HOME.

    Именно так он работает под systemd. Проверяем на настоящем bash,
    а не только чтением исходника: ошибка "unbound variable" -
    исполняемая, её видно лишь при запуске.
    """
    import os
    import shutil
    import subprocess
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Git Bash, а НЕ WSL: WSL на этой машине поднимает свой rootfs и
 # сообщает об ошибке ДО запуска скрипта, из-за чего тест врал бы.
    bash = None
    for cand in (r"C:\\Program Files\\Git\\bin\\bash.exe",
                 r"C:\\Program Files (x86)\\Git\\bin\\bash.exe",
                 "/bin/bash", "/usr/bin/bash"):
        if os.path.exists(cand):
            bash = cand
            break
    if not bash:
        pytest.skip("bash недоступен")
    home = tmp_path / "home"
    proj = home / "arbitr"
    (proj / "venv" / "bin").mkdir(parents=True)
    (proj / "logs").mkdir()
    (proj / "app.py").write_text("", encoding="utf-8")
    (proj / ".env").write_text("TRADING_MODE=paper\n", encoding="utf-8")
    py = proj / "venv" / "bin" / "python"
    py.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    os.chmod(py, 0o755)
    scripts = home / "scripts"
    scripts.mkdir()
    shutil.copy(os.path.join(root, "deploy", "run_bot.sh"),
                str(scripts / "run_bot.sh"))
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    # encoding="utf-8" обязателен: по умолчанию на Windows используется
    # cp1251, и русские строки скрипта роняют декодирование с
    # UnicodeDecodeError - тест падал бы из-за кодировки, а не из-за
    # проверяемой логики.
    # ВАЖНО: запускаем через `env -u HOME`. Простое удаление HOME из
    # словаря НЕ работает: на Windows subprocess подставляет HOME
    # заново из USERPROFILE, и скрипт видел её как всегда заданную.
    # Тест проходил бы на сломанном коде - проверено: с сырым $HOME
    # он оставался зелёным.
    # C:\Users\... -> /c/Users/... : слеши и двоеточие убираем
    # регуляркой. Через replace("\\\\","/") слеши съедались, и путь
    # получался вида "cUsers..." без разделителей.
    posix = re.sub(r"[\\:]+", "/", str(scripts / "run_bot.sh"))
    posix = "/" + posix[0].lower() + posix[1:]
    r = subprocess.run([bash, "-c", f"env -u HOME bash {posix}"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    out = r.stdout + r.stderr
    assert "unbound variable" not in out, (
        f"скрипт падает без HOME: {out[:300]}")
    assert "Запуск бота" in out, (
        f"бот не стартовал без HOME: {out[:300]}")
