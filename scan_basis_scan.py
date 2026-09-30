# -*- coding: utf-8 -*-
"""
РАЗВЕДКА: где вообще живёт базис.

Пользователь прав: на BTC/ETH/SOL базис средают арбитражники, на тонкой
ликвидности такого быть не может. Но «мемкоины» — слишком широкое
слово, и большинство из них не имеют фьючерса вообще. Поэтому проверяем
не выборочно, а ВСЕ пары сразу:

  1. берём ВСЕ спот-пары площадки (1 запрос)
  2. берём ВСЕ свопы площадки (1 запрос)
  3. оставляем пересечение — спот И фьючерс на ОДНОЙ бирже
  4. считаем базис по mid из тикера (не по барам) + оборот

Ключевой вопрос: сколько пар реально дают |базис| > 0.42%?
"""
import sys

import ccxt

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TAKER = 0.00055
SLIPPAGE = 0.0005
BREAKEVEN_PCT = 4 * (TAKER + SLIPPAGE) * 100      # 0.42%


def scan(ex_name, min_quote_vol=200_000, max_quote_vol=200_000_000):
    """
    ВАЖНО: fetch_tickers() по умолчанию отдаёт РАЗНОЕ на разных биржах:
      - Bybit   -> только свопы (нужен params={'type':'spot'})
      - Binance -> только спот (нужен отдельный вызов по свопам)
    Плюс у Bybit в списке есть ДАТИРОВАННЫЕ контракты
    ('BTC/USDT:USDT-261002') — их надо отбрасывать, иначе они засорят
    пересечение.
    """
    ex = getattr(ccxt, ex_name)({"enableRateLimit": True})
    if ex_name == "bybit":
        # Bybit принимает type='spot'; type='linear' НЕ поддерживается
        # (retCode 10001 "Illegal category"), а дефолтный вызов и так
        # отдаёт линейные свопы.
        spot = ex.fetch_tickers(params={"type": "spot"})
        swap = ex.fetch_tickers()
    else:
        spot = ex.fetch_tickers()
        try:
            swap = ex.fetch_tickers(params={"type": "swap"})
        except Exception:
            swap = {}
    rows = []
    for sym, s in spot.items():
        if "/USDT" not in sym or ":USDT" in sym:
            continue
        # датированные контракты вида BTC/USDT:USDT-261002 -> пропуск
        f = swap.get(f"{sym}:USDT")
        if not f or ":" in f.get("symbol", "") and "-" in f.get("symbol", ""):
            continue
        sb, sa = s.get("bid"), s.get("ask")
        fb, fa = f.get("bid"), f.get("ask")
        if not all(v and v > 0 for v in (sb, sa, fb, fa)):
            continue
        smid = (sb + sa) / 2.0
        fmid = (fb + fa) / 2.0
        basis = (fmid - smid) / smid * 100.0
        vol = s.get("quoteVolume") or 0.0
        if not (min_quote_vol <= vol <= max_quote_vol):
            continue
        rows.append((sym, basis, vol,
                     (sa - sb) / smid * 100.0, (fa - fb) / fmid * 100.0))
    return rows


def main():
    print(f"порог окупаемости: |базис| > {BREAKEVEN_PCT:.2f}% "
          f"(4 x {(TAKER + SLIPPAGE) * 100:.3f}%)")
    for name in ("bybit", "binance"):
        print(f"\n{'=' * 96}\n{name.upper()}\n{'=' * 96}")
        try:
            rows = scan(name)
        except Exception as e:
            print(f"  ошибка: {str(e)[:90]}")
            continue
        if not rows:
            print("  нет пар спот+своп в диапазоне объёма")
            continue
        print(f"  пар спот+своп в диапазоне: {len(rows)}")
        big = [r for r in rows if abs(r[1]) > BREAKEVEN_PCT]
        print(f"  с |базисом| > {BREAKEVEN_PCT:.2f}%: {len(big)} "
              f"({len(big) / len(rows) * 100:.1f}%)")
        if big:
            big.sort(key=lambda r: -abs(r[1]))
            print(f"\n  {'символ':<16}{'базис%':>9}{'оборот $':>14}"
                  f"{'спот spr%':>11}{'фьюч spr%':>11}")
            for sym, b, v, ss, fs in big[:25]:
                print(f"  {sym:<16}{b:>+9.3f}{v:>14,.0f}{ss:>11.3f}{fs:>11.3f}")
        else:
            print("  НИ ОДНОЙ пары не проходит порог. Калибровать нечего.")


if __name__ == "__main__":
    main()
