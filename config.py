"""🎯 ARBITRAGE SCANNER v10.2 — Auto-Recovery Edition."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
Path("logs").mkdir(exist_ok=True)

TRADING_MODE = os.getenv("TRADING_MODE", "paper")
PAPER_BALANCE = float(os.getenv("PAPER_BALANCE", "10000"))
PAPER_EXECUTION_MODEL = os.getenv("PAPER_EXECUTION_MODEL", "spot_futures")

EXCHANGES = [
    "bybit", "mexc"
]

RATE_LIMITS = {
    "bybit": 10, 
    "mexc": 15
}

TRADING_FEES = {
    "binance": 0.0010, "bybit": 0.0010, "okx": 0.0010,
    "gate": 0.0015, "mexc": 0.0010, "htx": 0.0020,
    "kucoin": 0.0010, "bitget": 0.0010,
}
# БЫЛО: "mexc": 0.0000 — нулевая комиссия. Это не «комиссия», а её
# отсутствие, и последствия были измеримыми:
#   bybit <-> mexc -> fees 0.1%   (одна сторона бесплатна)
#   bybit <-> bybit -> fees 0.2%
# Любой маршрут с MEXC стоил вдвое дешевле, поэтому `net > 0` выполнялось
# почти для всего: в hunter.db 100.00% из 2 539 782 возможностей имели
# положительный net. Бот предпочитал MEXC не из-за спреда, а из-за
# несуществующей комиссии.
# Ставим РЕАЛИСТИЧНУЮ комиссию. Реально: MEXC spot taker ~0.10%,
# фьючерс ~0.05%. Берём 0.001 — это консервативно для спота и для
# фьючерса (завышает расходы, а не занижает прибыль).

WITHDRAWAL_FEES = {
    "binance": 1.0, "bybit": 1.0, "okx": 1.0,
    "gate": 1.0, "mexc": 1.0, "htx": 1.0,
    "kucoin": 1.0, "bitget": 1.0,
}

EXCHANGE_TIMEOUT_SEC = 10

MIN_VOLUME_USDT = 10_000
MAX_VOLUME_USDT = 10_000_000_000
MIN_EXCHANGES = 2
MAX_EXCHANGES = len(EXCHANGES)
MIN_SPREAD_PERCENT = 0.3
MIN_NET_PROFIT_PERCENT = 0.05
MAX_SYMBOLS = 800

MAX_SPREAD_PERCENT = 50.0
MAX_PRICE_DIFF_PERCENT = 30.0
MIN_PRICE = 0.0001
MAX_PRICE = 1_000_000
MIN_TRADEABLE_VOLUME = 5000

BASE_SLIPPAGE = 0.002
SLIPPAGE_PER_USDT = 0.000001
VOLATILITY_DRIFT_FACTOR = 0.1

MIN_TRADE_SIZE = 100
MAX_TRADE_SIZE = 1000
MAX_POSITION_PCT_OF_BALANCE = 0.05

# Reality check before paper execution. Conservative filters reduce fake paper PnL.
REALITY_MIN_CONFIDENCE = float(os.getenv("REALITY_MIN_CONFIDENCE", "60"))
# Порог уверенности для RealityCheck.check() на холодном старте.
#
# Зачем отдельная константа (Шаг 0.3, 2026-09-30): RealityCheck стоял на
# жестких 60, тогда как RiskManager и app.py использовали 50, а затем 40.
# Три разных порога в трех местах: система проходила первый фильтр и тут
# же отсекалась вторым, из-за чего калибровка порога не давала НИКАКОГО
# эффекта - сделок по-прежнему не было ни одной.
#
# Значение 40 совпадает с порогом бутстрапа в
# RiskManager.get_confidence_threshold(). Фактическое значение в рантайме
# берется оттуда (app.py передает его явно), а 40 - безопасный дефолт.
REALITY_BOOTSTRAP_MIN_CONFIDENCE = float(
    os.getenv("REALITY_BOOTSTRAP_MIN_CONFIDENCE", "40"))
REALITY_MIN_NET_PROFIT_PERCENT = float(os.getenv("REALITY_MIN_NET_PROFIT_PERCENT", "0.5"))
REALITY_MAX_SPREAD_WITHOUT_L2 = float(os.getenv("REALITY_MAX_SPREAD_WITHOUT_L2", "12"))
REALITY_MAX_INTERNAL_SPREAD_PCT = float(os.getenv("REALITY_MAX_INTERNAL_SPREAD_PCT", "1.5"))
REALITY_MIN_EXECUTION_QUALITY = float(os.getenv("REALITY_MIN_EXECUTION_QUALITY", "50"))
REALITY_MAX_QUOTE_AGE_SEC = float(os.getenv("REALITY_MAX_QUOTE_AGE_SEC", "8"))
REALITY_MIN_SIZE_COVERAGE = float(os.getenv("REALITY_MIN_SIZE_COVERAGE", "1.0"))
REALITY_MIN_EV_USDT = float(os.getenv("REALITY_MIN_EV_USDT", "0"))

# L2 order book validation for top candidates only.
L2_REALITY_CHECK_ENABLED = os.getenv("L2_REALITY_CHECK_ENABLED", "true").lower() == "true"
L2_ORDERBOOK_LIMIT = int(os.getenv("L2_ORDERBOOK_LIMIT", "20"))
L2_TIMEOUT_SEC = float(os.getenv("L2_TIMEOUT_SEC", "3"))
L2_MAX_VWAP_SLIPPAGE_PCT = float(os.getenv("L2_MAX_VWAP_SLIPPAGE_PCT", "1.5"))
L2_MIN_FILL_PROBABILITY = float(os.getenv("L2_MIN_FILL_PROBABILITY", "0.60"))
L2_MIN_NET_PROFIT_PERCENT = float(os.getenv("L2_MIN_NET_PROFIT_PERCENT", "0.3"))

# Execution delay simulation — emulates latency between exchange legs.
EXECUTION_DELAY_ENABLED = os.getenv("EXECUTION_DELAY_ENABLED", "true").lower() == "true"
EXECUTION_DELAY_SEC_MIN = float(os.getenv("EXECUTION_DELAY_SEC_MIN", "0.1"))
EXECUTION_DELAY_SEC_MAX = float(os.getenv("EXECUTION_DELAY_SEC_MAX", "0.5"))
# Partial fill simulation — makes paper PnL less optimistic.
PARTIAL_FILL_ENABLED = os.getenv("PARTIAL_FILL_ENABLED", "true").lower() == "true"
PARTIAL_FILL_MIN_RATIO = float(os.getenv("PARTIAL_FILL_MIN_RATIO", "0.4"))
PARTIAL_FILL_MAX_RATIO = float(os.getenv("PARTIAL_FILL_MAX_RATIO", "1.0"))
# A1.4: было продублировано 3 раза подряд (явный след merge-конфликта) —
# опасно тем, что при правке только одной из копий следующая копия молча
# перезаписывала бы изменение обратно на дефолт.

FUTURES_LEVERAGE = float(os.getenv("FUTURES_LEVERAGE", "1"))
PAPER_HOLD_HOURS = float(os.getenv("PAPER_HOLD_HOURS", "0"))
FUTURES_FUNDING_RATE_PER_HOUR = float(os.getenv("FUTURES_FUNDING_RATE_PER_HOUR", "0"))
KELLY_FRACTION = 0.25
KELLY_FRACTION_EARLY = 0.10
KELLY_MIN_TRADES = 20
KELLY_FULL_TRADES = 100
ROLLING_WINDOW = 20

MAX_OPEN_POSITIONS = 3
MAX_DAILY_TRADES = 0
MAX_DAILY_LOSS_PERCENT = 10.0
MAX_CONSECUTIVE_LOSSES = 5
MAX_DRAWDOWN_PERCENT = 15.0
POSITION_COOLDOWN_SEC = 60
MIN_BALANCE_REQUIRED = 100

# FIX BUG #7: периодичность служебной проверки бирж. При SCAN_INTERVAL_SEC=2
# значение 30 даёт раз в минуту вместо 30 раз в минуту.
EXCHANGE_STATUS_INTERVAL_CYCLES = 30

# FIX BUG #5: чистка last_alert раз в N циклов. Словарь рос бесконечно.
ALERT_CLEANUP_INTERVAL_CYCLES = 100


# ==========================================
# ВАЛИДАЦИЯ КОНФИГУРАЦИИ
# ==========================================
def _validate():
    """
    Проверка инвариантов при старте. Без неё ошибка в .env всплывает как
    `ValueError: could not convert string to float` без указания, ЧТО именно
    сломано, либо — что хуже — молча портит расчёты (как с нулевой
    комиссией MEXC, из-за которой 100% net были положительными).

    Все проверки — на НЕРАВЕНСТВА, а не на "логичность": исправляя конфиг,
    не должно быть возможности случайно сделать систему неработоспособной.
    """
    errs = []

    for name, val in (("REALITY_MIN_CONFIDENCE", REALITY_MIN_CONFIDENCE),
                      ("REALITY_MIN_EXECUTION_QUALITY",
                       REALITY_MIN_EXECUTION_QUALITY)):
        if not 0 <= val <= 100:
            errs.append(f"{name}={val} вне диапазона 0..100")

    if PARTIAL_FILL_MIN_RATIO > PARTIAL_FILL_MAX_RATIO:
        errs.append(
            f"PARTIAL_FILL_MIN_RATIO={PARTIAL_FILL_MIN_RATIO} > "
            f"MAX={PARTIAL_FILL_MAX_RATIO}: частичное исполнение невозможно")
    if not 0 < PARTIAL_FILL_MIN_RATIO <= 1.0:
        errs.append(f"PARTIAL_FILL_MIN_RATIO={PARTIAL_FILL_MIN_RATIO} "
                    f"должен быть в (0, 1]")

    if FUTURES_LEVERAGE <= 0:
        errs.append(f"FUTURES_LEVERAGE={FUTURES_LEVERAGE} должен быть > 0")

    if MIN_EXCHANGES < 2:
        errs.append(f"MIN_EXCHANGES={MIN_EXCHANGES}: арбитраж требует "
                    f"минимум две площадки")
    if MAX_EXCHANGES < MIN_EXCHANGES:
        errs.append(f"MAX_EXCHANGES={MAX_EXCHANGES} < "
                    f"MIN_EXCHANGES={MIN_EXCHANGES}")

    for ex in EXCHANGES:
        if ex not in TRADING_FEES:
            errs.append(f"биржа {ex} есть в EXCHANGES, но нет в TRADING_FEES")
        f = TRADING_FEES.get(ex, -1)
        if f < 0:
            errs.append(f"комиссия {ex}={f} отрицательна")
        elif f == 0:
            # Ровно тот случай, что портил hunter.db
            errs.append(f"комиссия {ex}=0: это не комиссия, а её отсутствие. "
                        f"Любой маршрут с этой биржей будет выглядеть "
                        f"искусственно выгодным.")

    if CONFIDENCE_WEIGHTS_SUM != 100:
        errs.append(f"CONFIDENCE_WEIGHTS дают {CONFIDENCE_WEIGHTS_SUM}, "
                    f"а шкала рассчитана на 100")

    if MIN_VOLUME_USDT <= 0:
        errs.append(f"MIN_VOLUME_USDT={MIN_VOLUME_USDT} должен быть > 0")
    if MAX_VOLUME_USDT <= MIN_VOLUME_USDT:
        errs.append("MAX_VOLUME_USDT должен быть > MIN_VOLUME_USDT")

    if SCAN_INTERVAL_SEC <= 0:
        errs.append(f"SCAN_INTERVAL_SEC={SCAN_INTERVAL_SEC} должен быть > 0")

    if errs:
        raise ValueError("Некорректная конфигурация:\n  - "
                         + "\n  - ".join(errs))

# A2.1 FIX: раньше был разрыв политик без внятной логики —
# MAX_DRAWDOWN_PERCENT=15% блокировал НОВЫЕ сделки (RiskManager.can_trade,
# считается от пикового баланса — drawdown_pct), а единственная следующая
# граница, EMERGENCY_PNL_THRESHOLD, была 50% (считается от НАЧАЛЬНОГО
# депозита — pnl_pct) и полностью останавливала процесс. Между "перестали
# открывать новые сделки" и "убили процесс" был gap в 35 процентных
# пунктов без единого сигнала оператору. Это не обязательно баг (это
# два разных, законных измерения риска — просадка от пика и убыток от
# исходного капитала), но разрыв между уровнями реагирования — да.
# Введён промежуточный уровень: WARN на 25% убытка от исходного депозита
# (алерт, без остановки), полная остановка теперь на 40% вместо 50%
# (более консервативно — 50% потери капитала это слишком поздно для
# полной остановки, даже как последний рубеж).
EMERGENCY_WARN_PNL_THRESHOLD = float(os.getenv("EMERGENCY_WARN_PNL_THRESHOLD", "25.0"))
EMERGENCY_PNL_THRESHOLD = float(os.getenv("EMERGENCY_PNL_THRESHOLD", "40.0"))
EMERGENCY_CHECK_INTERVAL = 60

SCAN_INTERVAL_SEC = 3
CACHE_REFRESH_SEC = 3
SYMBOL_RELOAD_CYCLES = 720
PRICE_STALE_SEC = 25
ALERT_COOLDOWN_SEC = 60
STATS_INTERVAL_CYCLES = 30

BLACKLIST = {
    "LUNA", "USTC", "FTT", "BUSD",
    "USDT", "USDC", "DAI", "TUSD", "FRAX", "USDP",
    "DOWN", "UP", "BULL", "BEAR", "3L", "3S",
}

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# WATCHLIST удалён: он был объявлен, но не использовался НИГДЕ в app.py и
# engine.py (проверено поиском по всем модулям). Мёртвая конфигурация вводит
# в заблуждение при чтении — кажется, что список где-то применяется.
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
ALERT_MIN_NET_PROFIT = 1.0

DASHBOARD_TOP_N = 10
LOG_PANEL_HEIGHT = 8

CONFIDENCE_WEIGHTS = {
    "spread": 15, "liquidity": 15, "volume": 15,
    "history": 10, "exchange": 10, "execution": 10,
    "age": 8, "freshness": 7, "routing": 5, "funding": 5,
}
# БЫЛО: spread 25, остальное без изменений -> сумма 110.
# Движок считает `total = sum(breakdown.values())`, где каждый вклад =
# score * weight / 100. Значит итоговый скоринг лежал в 0..110, а не 0..100,
# и шкала «100 = идеальная сделка» была недостижима в принципе.
# Пороги 50/60/70 при этом работали (они ниже максимума), поэтому сдвиг
# касается прежде всего калибровки, а не работоспособности.
# Сумма приведена к 100: шкала 0..100 становится честной, пороги сохраняют
# смысл «процент от максимума».
CONFIDENCE_WEIGHTS_SUM = sum(CONFIDENCE_WEIGHTS.values())
assert CONFIDENCE_WEIGHTS_SUM == 100, (
    f"CONFIDENCE_WEIGHTS должны давать шкалу 0..100, получено "
    f"{CONFIDENCE_WEIGHTS_SUM}. Иначе пороги фильтров теряют смысл."
)

MAX_SAFE_SIZE_PCT_OF_VOLUME = 0.02
MAX_SAFE_SIZE_PCT_OF_BID_ASK_SPREAD = 10
MIN_LIQUIDITY_USDT = 100

REPLAY_MAX_HISTORY = 500
REPLAY_SAVE_INTERVAL_SEC = 2

CORRELATED_CATEGORIES = {
    "meme": {"DOGE", "SHIB", "PEPE", "FLOKI", "WIF", "BONK", "MEME", "BOME", "MEW", "BRETT"},
    "defi": {"UNI", "AAVE", "MKR", "CRV", "SNX", "LDO", "DYDX", "GMX", "CAKE", "BAL"},
    "l1_l2": {"APT", "SUI", "SEI", "TIA", "INJ", "ATOM", "DOT", "AVAX", "ARB", "OP"},
    "ai": {"FET", "RENDER", "TAO", "NEAR", "ICP", "GRT", "AGIX", "OCEAN"},
}
MAX_CORRELATED_POSITIONS = 2

SIMULATOR_DEPTH_LEVELS = 20
SIMULATOR_CACHE_SIZE = 500

LIFETIME_HISTORY_MAX = 1000
LIFETIME_MIN_SAMPLES = 5

# ════════════════════════════════════════════════
# ZOMBIE DETECTION — CALIBRATED v10.2
# ════════════════════════════════════════════════
ZOMBIE_MIN_HISTORY = 30          # Минимум истории (было 10)
ZOMBIE_NO_MOVEMENT_SEC = 300     # 5 мин без движения
ZOMBIE_CV_THRESHOLD = 0.00001    # Мягче (было 0.0001)
ZOMBIE_VOLUME_STATIC_THRESHOLD = 20  # Проверять последние 20 точек

# ════════════════════════════════════════════════
# AUTO-RECOVERY
# ════════════════════════════════════════════════
ZOMBIE_AUTO_RECOVERY_PCT = 50     # Если > 50% зомби — перезагрузка
ZOMBIE_AUTO_RECOVERY_INTERVAL = 300  # Не чаще чем раз в 5 мин
WARMUP_PERIOD_SEC = 60           # Skip zombie check первые 60 сек
ZOMBIE_TTL_SEC = 300             # Через 5 мин повторно проверять zombie-символ


# Валидация выполняется ПОСЛЕ определения всех констант: иначе _validate()
# обращается к ещё не объявленным именам (CONFIDENT_WEIGHTS_SUM и др.)
# и падает с NameError вместо понятной ошибки конфигурации.
_validate()
