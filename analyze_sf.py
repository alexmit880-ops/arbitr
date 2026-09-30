# -*- coding: utf-8 -*-
"""Проверка удержания пары: открытие -> переживание цикла -> закрытие."""
import asyncio
import sys
import time

import engine as E

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def T(bid, ask, sym="SOL/USDT"):
    return E.Ticker("x", sym, bid, ask, (bid + ask) / 2, 1e6,
                    int(time.time() * 1000))


def make_opp():
    return E.Opportunity(
        symbol="SOL/USDT", buy_exchange="binance",
        sell_exchange="bybit", buy_price=100, sell_price=102,
        spread_pct=2.0, net_profit_pct=1.5, volume=1e7, exchanges_count=2,
        timestamp=int(time.time()), max_safe_size_usdt=1000,
        confidence_score=80)


def main():
    bm = E.BalanceManager({"binance": {"USDT": 5000},
                           "bybit": {"USDT": 5000}})
    pf = E.Portfolio(balance=10000, initial_balance=10000)
    sl = E.ShadowLogger(enabled=True)
    t = E.SpotFuturesPaperTrader(pf, bm, shadow_logger=sl,
                                target_close_pct=0.15, stop_widen_pct=1.5,
                                max_hold_bars=96)
    fut_open = {"SOL/USDT": {"bybit": T(102, 102.2)}}
    r = asyncio.run(t.execute(make_opp(),
                              {"binance": T(100, 100.2), "bybit": T(102, 102.2)},
                              futures_prices=fut_open))
    print(f"execute -> {r}  (None = позиция открыта)")
    print(f"открытых пар: {len(t.open_pairs)}")
    p = list(t.open_pairs.values())[0]
    print(f"  basis входа {p.entry_basis:+.3f}%  amount {p.amount:.4f}  "
          f"перевод ${p.transfer_fee:.4f}  direction {p.direction}")

    pr = {"SOL/USDT": {"binance": T(100, 100.2), "bybit": T(101.8, 102.0)}}
    cl = t.manage_open_positions(pr, {"SOL/USDT": {"bybit": T(101.8, 102.0)}})
    print(f"\nцикл 1, базис ~1.8%: закрыто {len(cl)}, пар осталось "
          f"{len(t.open_pairs)}")

    pr = {"SOL/USDT": {"binance": T(100, 100.2), "bybit": T(100.05, 100.25)}}
    cl = t.manage_open_positions(pr, {"SOL/USDT": {"bybit": T(100.05, 100.25)}})
    print(f"цикл 2, базис ~0.05%: закрыто {len(cl)}, пар осталось "
          f"{len(t.open_pairs)}")
    if cl:
        print(f"  PnL {cl[0].pnl:+.4f} USDT ({cl[0].pnl_pct:+.4f}%), "
              f"held {p.bars_held} баров")
    print(f"\nбаланс портфеля: {pf.balance:.2f}")
    print(f"shadow stats: {sl.stats()}")
    print(f"отказы: {sl.rejected_by_reason()}")
    for e in sl.entries:
        print(f"  entry: {e['reason']:<12} synth {e['synthetic_pnl']:+.4f} "
              f"latent {e['latent_pnl']:+.4f}")


if __name__ == "__main__":
    main()
import os
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


def show(db):
    con = sqlite3.connect(os.path.join(HERE, db))
    cur = con.cursor()
    n = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    if not n:
        print(f"{db}: пусто")
        return
    tot, pos, neg = cur.execute(
        "SELECT SUM(pnl_usdt), SUM(pnl_usdt>0), SUM(pnl_usdt<0) "
        "FROM trades").fetchone()
    print(f"\n{'=' * 92}\n{db}: {n} сделок | PnL {tot:+.2f} | "
          f"win {pos} loss {neg} | WR {pos / n * 100:.1f}%\n{'=' * 92}")
    print(f"{'выход':<12}{'n':>6}{'sumPnL':>11}{'WR%':>7}{'basis вх':>10}"
          f"{'basis вых':>11}{'held':>7}")
    for r in cur.execute(
            "SELECT exit_reason,COUNT(*),SUM(pnl_usdt),"
            "AVG(pnl_usdt>0)*100,AVG(entry_spread),AVG(close_spread),"
            "AVG(bars_held) FROM trades GROUP BY exit_reason"):
        print(f"{r[0]:<12}{r[1]:>6}{r[2]:>11.2f}{r[3]:>7.1f}{r[4]:>10.3f}"
              f"{r[5]:>11.3f}{r[6]:>7.1f}")
    print("\nГЛАВНОЕ — куда двигался базис после входа:")
    for r in cur.execute(
            "SELECT exit_reason,COUNT(*),AVG(close_spread-entry_spread) "
            "FROM trades GROUP BY exit_reason"):
        print(f"  {r[0]:<12} n={r[1]:<4} среднее изменение базиса "
              f"{r[2]:+.3f} п.п.")
    print("\nРасходы на сделку:")
    f, h = cur.execute("SELECT AVG(fees), AVG(funding) FROM trades").fetchone()
    a = cur.execute("SELECT AVG(pnl_usdt) FROM trades").fetchone()[0]
    print(f"  комиссии {f:.2f} | funding {h:.2f} | вместе {f + h:.2f} USDT "
          f"({(f + h) / 10:.3f}% от 1000)")
    print(f"  средний PnL {a:+.2f} USDT")
    con.close()


def main():
    for db in ("sf_probe.db", "sf_probe2.db"):
        if os.path.exists(os.path.join(HERE, db)):
            show(db)


if __name__ == "__main__":
    main()

import os
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sf_probe.db")


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    n = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print("=" * 92)
    print(f"СДЕЛОК: {n}")
    print("=" * 92)
    tot, pos, neg = cur.execute(
        "SELECT SUM(pnl_usdt), SUM(pnl_usdt>0), SUM(pnl_usdt<0) "
        "FROM trades").fetchone()
    print(f"PnL: {tot:+.2f} USDT   win={pos}  loss={neg}  "
          f"WR={pos / n * 100:.1f}%")
    fees, fund, held, avgpct = cur.execute(
        "SELECT SUM(fees), SUM(funding), AVG(bars_held), AVG(pnl_pct) "
        "FROM trades").fetchone()
    print(f"комиссии {fees:.2f} | funding {fund:.2f} | "
          f"ср. удержание {held:.1f} баров | средний PnL {avgpct:.3f}%")
    print(f"\n{'выход':<12}{'n':>7}{'sumPnL':>12}{'WR%':>8}{'basis вх':>11}"
          f"{'basis вых':>12}")
    for r in cur.execute(
            "SELECT exit_reason,COUNT(*),SUM(pnl_usdt),"
            "AVG(pnl_usdt>0)*100,AVG(entry_spread),AVG(close_spread) "
            "FROM trades GROUP BY exit_reason"):
        print(f"{r[0]:<12}{r[1]:>7}{r[2]:>12.2f}{r[3]:>8.1f}"
              f"{r[4]:>11.3f}{r[5]:>12.3f}")
    print("\nПОЛОЖИТЕЛЬНЫХ СДЕЛОК:", pos)
    print("\nпримеры с наибольшим PnL:")
    for r in cur.execute("SELECT symbol,entry_spread,close_spread,pnl_usdt,"
                         "bars_held,exit_reason FROM trades "
                         "ORDER BY pnl_usdt DESC LIMIT 5"):
        print(f"  {r[0]:<12} вх {r[1]:+.3f}%  вых {r[2]:+.3f}%  "
              f"PnL {r[3]:+.2f}  {r[5]} за {r[4]} баров")
    print("\nРАЗБОР ПО НАПРАВЛЕНИЮ:")
    for r in cur.execute(
            "SELECT direction,COUNT(*),SUM(pnl_usdt),AVG(pnl_usdt>0)*100,"
            "AVG(entry_spread),AVG(close_spread) FROM trades "
            "GROUP BY direction"):
        print(f"  {r[0]:<14} n={r[1]:<5} PnL={r[2]:+9.2f} WR={r[3]:5.1f}% "
              f"basis вх {r[4]:+.3f}% -> вых {r[5]:+.3f}%")
    con.close()


if __name__ == "__main__":
    main()
