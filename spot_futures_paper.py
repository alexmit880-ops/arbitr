# -*- coding: utf-8 -*-
"""
SPOT <-> FUTURES PAPER: сходимость базиса.

ЗАДАЧА: одна нога в фьючерсе, вторая на споте, ловим сходимость спреда
от 1% и выше, копим сделки для анализа. Только публичные данные ccxt.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ
В hunter SpotFuturesPaperTrader.execute() без close_prices закрывал обе
ноги по spot_entry, из-за чего PnL = ИСХОДНЫЙ СПРЕД всегда, независимо от
рынка (438% за 44 часа, WR 99.5%). Здесь позиция живёт до РЕАЛЬНОГО
закрытия по фактическим ценам.

ЧЕСТНОСТЬ
  * Сигнал на ЗАКРЫТОМ баре i, вход по open бара i+1. Без look-ahead.
  * Комиссии на всех 4 сторонах + проскальзывание в обе стороны.
  * Funding за всё удержание (платим и лонгу, и шорту — консервативно).
  * Выход: сходимость |базис| <= 0.10%, стоп по расширению базиса,
    либо таймаут HOLD_MAX_BARS баров.
"""
import sqlite3
from datetime import datetime, timezone

import ccxt

# ==========================================
# ПАРАМЕТРЫ (зафиксированы до прогона)
# ==========================================
# РАЗВЕДКА ПО ВСЕМ ПАРАМ (scan_basis_scan.py), 3 замера подряд, Bybit:
#   пар спот+своп            176
#   медиана |базиса|         0.089%
#   p90                      0.222%
#   p99                      0.483%
#   максимум                 0.94%
#   пар > 0.42% (порог)     3  (устойчиво, те же самые на каждом замере)
#
# ВЫВОД РАЗВЕДКИ: хвост существует, но он тонкий — верхние ~1.7% пар.
# Поэтому порог входа поднимаем до 0.60%: это и есть «мемкоины/тонкая
# ликвидность», о которых шла речь. Вход на медиане (0.09%) заведомо
# не окупает круговые издержки 0.42% — это показал прогон на 1057 сделок.
MIN_SPREAD_PCT = 0.60
MAX_SPREAD_PCT = 25.0
TARGET_CLOSE = 0.15
STOP_SPREAD_WIDEN = 1.5
HOLD_MAX_BARS = 96
NOTIONAL_USD = 1000.0
TAKER = 0.00055
SLIPPAGE = 0.0005
FUNDING_PER_8H = 0.0002
INTERVAL = "1h"
STORAGE = "sf_trades.db"
EXCHANGES = ("bybit", "binance")
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
           "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
           "LTC/USDT", "BCH/USDT", "ATOM/USDT", "NEAR/USDT"]


def basis_pct(spot_mid, fut_mid):
    """Базис в %: (фьючерс - спот)/спот*100."""
    if spot_mid <= 0:
        return 0.0
    return (fut_mid - spot_mid) / spot_mid * 100.0


def round_trip_pnl(direction, entry_spot, entry_fut, exit_spot, exit_fut,
                   notional, bars_held):
    """
    Cash-and-carry, все 4 стороны расходов.
    fut_premium (фьючерс дороже): шорт фьючерса + лонг спота.
    При сходимости exit_fut <= exit_spot оба плеча положительны.
    """
    amt = notional / entry_spot
    if direction == "fut_premium":
        gross = amt * ((entry_fut - exit_fut) + (exit_spot - entry_spot))
    else:
        gross = amt * ((exit_fut - entry_fut) + (entry_spot - exit_spot))
    # ФИКС ДВОЙНОГО УЧЁТА: проскальзывание уже заложено в цены входа и
    # выхода (см. process(): es = so*(1+SLIPPAGE), ef = fo*(1-SLIPPAGE)).
    # Учитывать его ещё раз в fees значило платить 0.2% дважды. Здесь только
    # биржевая комиссия taker на 4 стороны.
    fees = 4 * notional * TAKER
    funding = notional * FUNDING_PER_8H * (bars_held / 8.0)
    pnl = gross - fees - funding
    return pnl, (pnl / notional * 100 if notional > 0 else 0.0), fees, funding


def fetch_bars(ex, symbol, limit):
    """
    Публичные OHLCV. Спот и фьючерс различаются ('BTC/USDT' vs
    'BTC/USDT:USDT'). Склеиваются ТОЛЬКО по совпавшим меткам времени —
    иначе базис считался бы на несогласованных данных (именно так рождались
    спреды в 26% в hunter.db)."""
    def one(sym):
        try:
            rows = ex.fetch_ohlcv(sym, INTERVAL, limit=limit)
        except Exception:
            return {}
        return {int(r[0]): {"o": float(r[1]), "h": float(r[2]),
                            "l": float(r[3]), "c": float(r[4])}
                for r in (rows or [])}
    spot = one(symbol)
    fut = one(f"{symbol}:USDT")
    if not spot or not fut:
        return []
    bars = []
    for ts in sorted(set(spot) & set(fut)):
        s, f = spot[ts], fut[ts]
        smid = (s["h"] + s["l"]) / 2.0
        fmid = (f["h"] + f["l"]) / 2.0
        if smid <= 0 or fmid <= 0:
            continue
        bars.append({"ts": ts, "so": s["o"], "sc": s["c"],
                     "fo": f["o"], "fc": f["c"],
                     "basis": basis_pct(smid, fmid)})
    return bars



def simulate(direction, bars, i, entry_spot, entry_fut):
    """
    Ищем РЕАЛЬНУЮ сходимость. Вход на i+1 (open следующего бара).
    Приоритет: стоп -> сходимость -> таймаут (пессимизм: стоп важнее цели).
    """
    entry_basis = basis_pct(entry_spot, entry_fut)
    start = i + 1
    for j in range(start, len(bars)):
        b = bars[j]
        if b["sc"] <= 0 or b["fc"] <= 0:
            continue
        cur = basis_pct(b["sc"], b["fc"])
        worse = (cur - entry_basis) if direction == "fut_premium" \
            else (entry_basis - cur)
        if worse >= STOP_SPREAD_WIDEN:
            return j, b["sc"], b["fc"], "STOP"
        if abs(cur) <= TARGET_CLOSE:
            return j, b["sc"], b["fc"], "CONVERGED"
    j = min(len(bars) - 1, start + HOLD_MAX_BARS)
    b = bars[j]
    return j, b["sc"], b["fc"], "TIMEOUT"


def process(ex_name, symbol, bars, notional=NOTIONAL_USD):
    """Ищет точки входа и прогоняет каждую до реального закрытия."""
    trades = []
    if len(bars) < 30:
        return trades
    busy_until = 0
    for i in range(5, len(bars) - 2):
        if i <= busy_until:
            continue
        bps = bars[i]["basis"]
        if not (MIN_SPREAD_PCT <= abs(bps) <= MAX_SPREAD_PCT):
            continue
        e = bars[i + 1]                      # вход по open СЛЕДУЮЩЕГО бара
        if e["so"] <= 0 or e["fo"] <= 0:
            continue
        es = e["so"] * (1 + SLIPPAGE)        # проскальзывание против нас
        ef = e["fo"] * (1 - SLIPPAGE)
        d = "fut_premium" if bps > 0 else "fut_discount"
        j, xs, xf, reason = simulate(d, bars, i, es, ef)
        if xs <= 0 or xf <= 0:
            continue
        held = max(j - (i + 1), 0)
        pnl, pnl_pct, fees, funding = round_trip_pnl(d, es, ef, xs, xf,
                                                      notional, held)
        trades.append({
            "ts_open": datetime.fromtimestamp(bars[i + 1]["ts"] / 1000,
                                               timezone.utc).isoformat(),
            "ts_close": datetime.fromtimestamp(bars[j]["ts"] / 1000,
                                                timezone.utc).isoformat(),
            "exchange": ex_name, "symbol": symbol, "direction": d,
            "entry_spread": basis_pct(es, ef), "close_spread": basis_pct(xs, xf),
            "entry_px_spot": es, "entry_px_fut": ef,
            "close_px_spot": xs, "close_px_fut": xf,
            "notional": notional, "pnl_usdt": pnl, "pnl_pct": pnl_pct,
            "fees": fees, "funding": funding, "bars_held": held,
            "exit_reason": reason})
        busy_until = j
    return trades



def open_db(path=STORAGE):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_open TEXT, ts_close TEXT,
        exchange TEXT, symbol TEXT, direction TEXT, entry_spread REAL,
        close_spread REAL, entry_px_spot REAL, entry_px_fut REAL,
        close_px_spot REAL, close_px_fut REAL, notional REAL, pnl_usdt REAL,
        pnl_pct REAL, fees REAL, funding REAL, bars_held INTEGER,
        exit_reason TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, exchange TEXT,
        symbol TEXT, spot_mid REAL, fut_mid REAL, basis_pct REAL)""")
    con.commit()
    return con


def save(con, trades):
    con.executemany(
        "INSERT INTO trades (ts_open,ts_close,exchange,symbol,direction,"
        "entry_spread,close_spread,entry_px_spot,entry_px_fut,close_px_spot,"
        "close_px_fut,notional,pnl_usdt,pnl_pct,fees,funding,bars_held,"
        "exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(t.values()) for t in trades])
    con.commit()


def report(con):
    cur = con.cursor()
    n = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"\n{'=' * 92}\nСДЕЛКИ: {n}\n{'=' * 92}")
    if not n:
        print("  нет сделок")
        return
    tot, pos, neg = cur.execute(
        "SELECT SUM(pnl_usdt), SUM(pnl_usdt>0), SUM(pnl_usdt<0) "
        "FROM trades").fetchone()
    print(f"  PnL: {tot:+.2f} USDT   win={pos} loss={neg}   WR={pos / n * 100:.1f}%")
    print(f"\n  {'направление':<14}{'n':>5}{'sumPnL':>10}{'WR%':>7}"
          f"{'basis вх':>10}{'basis вых':>11}")
    for d, k, s, wr, eb, cb in cur.execute(
            "SELECT direction,COUNT(*),SUM(pnl_usdt),AVG(pnl_usdt>0)*100,"
            "AVG(entry_spread),AVG(close_spread) FROM trades GROUP BY direction"):
        print(f"  {d:<14}{k:>5}{s:>10.2f}{wr:>7.1f}{eb:>10.3f}{cb:>11.3f}")
    print(f"\n  {'выход':<12}{'n':>5}{'sumPnL':>10}{'WR%':>7}")
    for d, k, s, wr in cur.execute(
            "SELECT exit_reason,COUNT(*),SUM(pnl_usdt),AVG(pnl_usdt>0)*100 "
            "FROM trades GROUP BY exit_reason"):
        print(f"  {d:<12}{k:>5}{s:>10.2f}{wr:>7.1f}")
    print("\n  7 худших:")
    for r in cur.execute("SELECT symbol,pnl_usdt,exit_reason,bars_held "
                         "FROM trades ORDER BY pnl_usdt LIMIT 7"):
        print(f"    {r[0]:<12}{r[1]:>9.2f}  {r[2]:<11} held={r[3]}")


def main():
    print(f"SPOT<->FUTURES PAPER | вход при |базисе| >= {MIN_SPREAD_PCT}% | "
          f"{INTERVAL} | биржи {', '.join(EXCHANGES)}")
    con = open_db()
    total = 0
    for name in EXCHANGES:
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
        except Exception as e:
            print(f"  {name}: не инициализирован ({str(e)[:60]})")
            continue
        for sym in SYMBOLS:
            try:
                bars = fetch_bars(ex, sym, 500)
            except Exception as e:
                print(f"  {name} {sym}: {str(e)[:50]}")
                continue
            if len(bars) < 30:
                print(f"  {name:<8} {sym:<12} общих баров: {len(bars)} - пропуск")
                continue
            bs = [b["basis"] for b in bars]
            con.executemany(
                "INSERT INTO observations (ts,exchange,symbol,spot_mid,"
                "fut_mid,basis_pct) VALUES (?,?,?,?,?,?)",
                [(datetime.now(timezone.utc).isoformat(), name, sym,
                  b["sc"], b["fc"], b["basis"]) for b in bars[-200:]])
            con.commit()
            tr = process(name, sym, bars)
            if tr:
                save(con, tr)
                total += len(tr)
            print(f"  {name:<8} {sym:<12} баров {len(bars):>4}  "
                  f"basis [{min(bs):+.2f}%; {max(bs):+.2f}%]  сделок {len(tr)}")
    print(f"\nвсего сделок: {total}")
    report(con)


if __name__ == "__main__":
    main()
