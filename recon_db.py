# -*- coding: utf-8 -*-
"""
ГЛАВНЫЙ ВОПРОС К АРБИТРАЖНОМУ БОТУ: есть ли у него edge на самом деле?

Материал уже есть: 2.5 млн записанных «возможностей» и 2882 реальные сделки
в hunter.db. Поэтому начинаем не с кода, а с данных — иначе рискуем чинить
то, что не сломано, или ломать то, что работает.

ТРИ ВОПРОСА, на которые отвечаем:

  1. Сколько «возможностей» было в реальности, и сколько из них выжило
     после вычета комиссий?  (2.5 млн строк — это грубый спред, не edge)
  2. Что реально заработали 2882 сделки: PnL, PF, распределение по
     биржам, по времени, по символам.
  3. ГЛАВНОЕ: чем реальные сделки отличаются от потока. Если сделки
     прибыльны там, где «возможности» убыточны — это сигнал (что-то
     фильтрует). Если сделки прибыльны ровно настолько же, насколько
     убыточны возможности, — бот не отсекает шум, и edge иллюзорный.

Ничего не изменяется. Только чтение.
"""
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "hunter.db")


def load():
    con = sqlite3.connect(DB)
    opp = pd.read_sql("SELECT * FROM opportunities", con)
    tr = pd.read_sql("SELECT * FROM trades", con)
    con.close()
    for d in (opp, tr):
        if "ts" in d.columns:
            d["ts"] = pd.to_datetime(d["ts"], unit="ms", errors="coerce")
    return opp, tr


def q1_opportunities(opp):
    print(f"\n{'=' * 92}\n1. ПОТОК ВОЗМОЖНОСТЕЙ: {len(opp):,} записей\n{'=' * 92}")
    if opp.empty:
        print("  пусто")
        return
    print(f"  период: {opp['ts'].min()} .. {opp['ts'].max()}")
    print(f"  символов: {opp['symbol'].nunique()}   "
          f"маршрутов: {opp.groupby(['buy_ex','sell_ex']).ngroups}")
    print("\n  маршруты (биржа -> биржа):")
    g = opp.groupby(["buy_ex", "sell_ex"]).agg(
        n=("spread", "size"), med_spread=("spread", "median"),
        med_net=("net", "median"), max_net=("net", "max"))
    g = g.sort_values("n", ascending=False)
    print(f"    {'покупка':<10}{'продажа':<10}{'n':>12}"
          f"{'med spread%':>13}{'med net%':>11}{'max net%':>11}")
    for (b, s), r in g.iterrows():
        print(f"    {b:<10}{s:<10}{r['n']:>12,}{r['med_spread']:>13.3f}"
              f"{r['med_net']:>11.3f}{r['max_net']:>11.3f}")

    for col in ("spread", "net"):
        v = opp[col].dropna()
        if v.empty:
            continue
        print(f"\n  {col}:")
        print(f"    mean {v.mean():.4f}  median {v.median():.4f}  "
              f"max {v.max():.4f}  min {v.min():.4f}")
        print(f"    квантили: " + ", ".join(
            f"p{q}={np.percentile(v, q):.3f}" for q in (50, 75, 90, 99, 99.9)))
    if "net" in opp.columns:
        pos = (opp["net"] > 0).sum()
        print(f"\n  доля с ПОЛОЖИТЕЛЬНЫМ net: {pos / len(opp) * 100:.2f}%")
        print("  -> это доля грубого спреда, пережившего вычет комиссий.")


def q2_trades(tr):
    print(f"\n{'=' * 92}\n2. РЕАЛЬНЫЕ СДЕЛКИ: {len(tr):,}\n{'=' * 92}")
    if tr.empty:
        print("  пусто — сделок не было")
        return
    print(f"  период: {tr['ts'].min()} .. {tr['ts'].max()}")
    p = tr["pnl"]
    win, loss = p[p > 0], p[p < 0]
    gp, gl = float(win.sum()), float(abs(loss.sum()))
    print(f"\n  PnL всего: {p.sum():+,.2f} USDT")
    print(f"  win: {len(win):,} ({len(win)/len(tr)*100:.1f}%)  "
          f"loss: {len(loss):,} ({len(loss)/len(tr)*100:.1f}%)")
    print(f"  средний выигрыш: {win.mean() if len(win) else 0:+.2f}  "
          f"средний проигрыш: {loss.mean() if len(loss) else 0:+.2f}")
    print(f"  Profit Factor: {gp/gl if gl > 0 else float('inf'):.3f}")
    eq = tr["balance"].astype(float)
    if len(eq) > 1:
        peak = eq.cummax()
        dd = float(((eq - peak) / peak).min()) * 100
        print(f"  Max DD по балансу: {dd:.2f}%")
        print(f"  баланс: {eq.iloc[0]:,.2f} -> {eq.iloc[-1]:,.2f}")
    print(f"\n  {'категория':<22}{'n':>7}{'sum PnL':>12}{'WR%':>8}")
    for cat, g in tr.groupby("category"):
        print(f"  {str(cat):<22}{len(g):>7}{g['pnl'].sum():>12.2f}"
              f"{(g['pnl'] > 0).mean() * 100:>8.1f}")
    print(f"\n  ТОП-10 убыточных символов:")
    g = tr.groupby("symbol")["pnl"].agg(["size", "sum"]).sort_values("sum")
    for s, r in g.head(10).iterrows():
        print(f"    {s:<16} n={int(r['size']):<6} sum={r['sum']:+.2f}")
    print(f"\n  ТОП-10 прибыльных символов:")
    for s, r in g.tail(10).iloc[::-1].iterrows():
        print(f"    {s:<16} n={int(r['size']):<6} sum={r['sum']:+.2f}")


def q3_edge(opp, tr):
    print(f"\n{'=' * 92}\n3. ОТЛИЧАЕТСЯ ЛИ ИСПОЛНЕНОЕ ОТ ПОТОКА?\n{'=' * 92}")
    if opp.empty or tr.empty:
        print("  нехватка данных")
        return
    print(f"  поток:      n={len(opp):,}  доля net>0 = "
          f"{(opp['net'] > 0).mean() * 100:.2f}%  "
          f"median net = {opp['net'].median():.3f}%")
    print(f"  исполнено:  n={len(tr):,}  доля PnL>0 = "
          f"{(tr['pnl'] > 0).mean() * 100:.2f}%  "
          f"median pnl_pct = {tr['pnl_pct'].median():.3f}%")
    ratio = (tr["pnl"] > 0).mean() / max((opp["net"] > 0).mean(), 1e-9)
    print(f"\n  ВЫБОР: исполненные сделки успешны в {ratio:.2f}x чаще, "
          f"чем «сырые» возможности")
    print("  -> это и есть вклад фильтров. Если ratio близко к 1,")
    print("     бот не отсекает шум, и прибыльность сделок — иллюзия.")


def main():
    opp, tr = load()
    q1_opportunities(opp)
    q2_trades(tr)
    q3_edge(opp, tr)


if __name__ == "__main__":
    main()




def anomaly():
    """
    ПОЧЕМУ 438% ЗА 2 МИНУТЫ — РАЗБОР АНОМАЛИИ.

    Три версии, которые надо различить ДО любых выводов:

      1. ОШИБКА ЧТЕНИЯ: в прошлом прогоне ts был прочитан как миллисекунды,
         отсюда 1970 год. В БД лежит ~1 790 000 — это СЕКУНДЫ (~20 дней).
         На экономику не влияет, но показывает: с такими данными нельзя
         работать «на глаз».

      2. АРТЕФАКТ РАСЧЁТА: 100.00% из 2 539 782 возможностей имеют net > 0
         при медианном спреде 5.59%. Реальный кросс-биржевой спред между
         ликвидными площадками — доли процента. Медиана 5.6% означает, что
         сопоставляются НЕ те инструменты (разный квотируемый актив,
         спот против фьючерса) или берётся несинхронная цена.

      3. РЕАЛЬНЫЙ EDGE: исключён по построению. 438% за 2 минуты — это
         лучшая стратегия в истории финансов; WR 99.5% и PF 4150 на
         межбиржевом арбитраже не возникают, потому что две ноги
         исполняются на разных площадках и лаг гарантированно даёт
         убытки на части сделок.
    """
    con = sqlite3.connect(DB)
    opp = pd.read_sql("SELECT * FROM opportunities LIMIT 400000", con)
    tr = pd.read_sql("SELECT * FROM trades", con)
    con.close()

    print("=" * 92)
    print("1. ЕДИНИЦА ВРЕМЕНИ")
    print("=" * 92)
    t0 = float(opp["ts"].iloc[0])
    print(f"  ts = {t0:.0f}")
    print(f"  как СЕКУНДЫ -> {pd.to_datetime(t0, unit='s')}")
    d = (float(opp["ts"].max()) - float(opp["ts"].min())) / 86400.0
    print(f"  охват данных: {d:.2f} суток <- реальная длительность")

    print("\n" + "=" * 92)
    print("2. АНОМАЛИЯ СПРЕДА")
    print("=" * 92)
    print(f"  медиана спреда:   {opp['spread'].median():.3f}%")
    print(f"  доля спред > 1%:  {(opp['spread'] > 1).mean() * 100:.2f}%")
    print(f"  доля net > 0:     {(opp['net'] > 0).mean() * 100:.2f}%")
    print("  Реальный кросс-биржевой спред на ликвидных парах: 0.05-0.5%.")

    g = opp.groupby(["buy_ex", "sell_ex"])["spread"].agg(
        ["size", "median"]).sort_values("size", ascending=False)
    print(f"\n  {'маршрут':<24}{'n':>10}{'median%':>10}")
    for (b, s), r in g.iterrows():
        print(f"    {b + ' -> ' + s:<24}{int(r['size']):>10,}{r['median']:>10.3f}")

    gs = opp.groupby("symbol")["spread"].agg(["size", "median"]).sort_values(
        "median", ascending=False)
    print(f"\n  ТОП-12 символов по медианному спреду:")
    for s, r in gs.head(12).iterrows():
        print(f"    {s:<18}{int(r['size']):>10,}{r['median']:>10.3f}")
    print("\n  ТОП-12 символов по МИНИМАЛЬНОМУ спреду (похожи на реальные):")
    for s, r in gs.tail(12).iloc[::-1].iterrows():
        print(f"    {s:<18}{int(r['size']):>10,}{r['median']:>10.3f}")

    print("\n" + "=" * 92)
    print("3. РЕАЛЬНОСТЬ РЕЗУЛЬТАТА")
    print("=" * 92)
    d_tr = (float(tr["ts"].max()) - float(tr["ts"].min())) / 3600.0
    pnl = float(tr["pnl"].sum())
    print(f"  сделок:  {len(tr):,}")
    print(f"  период:  {d_tr:.2f} ЧАСА")
    print(f"  PnL:     {pnl:+,.2f} USDT на стартовых ~10 000")
    print(f"  WR:      {(tr['pnl'] > 0).mean() * 100:.2f}%")
    print(f"  темп:    {pnl / max(d_tr, 1e-9):,.0f} USDT/час")
    print("\n  -> это не результат стратегии, это ошибка модели или данных.")


if __name__ == "__main__":
    main()
    anomaly()


