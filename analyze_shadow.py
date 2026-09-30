"""📊 Shadow Logger Analyzer — сравнивает expected vs actual, строит ROC, heatmap.

Запуск:
    python analyze_shadow.py                 # стандартный анализ
    python analyze_shadow.py --hours 48      # анализ за последние N часов
    python analyze_shadow.py --show          # показать графики (интерактив)

Требует: pip install pandas matplotlib seaborn
"""
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HAS_PLOTS = False
try:
    import matplotlib
    matplotlib.use("Agg")  # headless для сервера
    import matplotlib.pyplot as plt
    import seaborn as sns
    HAS_PLOTS = True
except ImportError:
    pass


# ════════════════════════════════════════════════
# 1. ЗАГРУЗКА ДАННЫХ
# ════════════════════════════════════════════════

def load_shadow_log(db_path: str = "hunter.db", max_hours: float = 0) -> list:
    """Загружает записи shadow_log из SQLite.
    
    Args:
        db_path: путь к БД
        max_hours: если >0, только последние N часов
    
    Returns:
        список словарей
    """
    if not Path(db_path).exists():
        print(f"⚠️  Файл {db_path} не найден")
        return []
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    where = ""
    params = []
    if max_hours > 0:
        cutoff = int(time.time()) - int(max_hours * 3600)
        where = "WHERE ts >= ?"
        params = [cutoff]
    
    rows = conn.execute(
        f"SELECT * FROM shadow_log {where} ORDER BY ts DESC LIMIT 10000",
        params
    ).fetchall()
    conn.close()
    
    records = [dict(r) for r in rows]
    print(f"📥 Загружено {len(records)} записей из shadow_log"
          f"{f' (последние {max_hours:.0f}ч)' if max_hours > 0 else ' (все)'}")
    return records


# ════════════════════════════════════════════════
# 2. РАСЧЁТ МЕТРИК
# ════════════════════════════════════════════════

def calc_success_rate(records: list, min_conf: float = 0) -> dict:
    """Win rate при заданном пороге confidence."""
    filtered = [r for r in records if r["conf"] >= min_conf and r["net_actual"] != 0]
    if not filtered:
        return {"total": 0, "wins": 0, "losses": 0, "winrate": 0, "avg_pnl": 0}
    wins = [r for r in filtered if r["net_actual"] > 0]
    losses = [r for r in filtered if r["net_actual"] <= 0]
    avg_pnl = np.mean([r["net_actual"] for r in filtered]) if filtered else 0
    return {
        "total": len(filtered),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": len(wins) / len(filtered) * 100 if filtered else 0,
        "avg_pnl": avg_pnl,
    }


def calc_roc_curve(records: list) -> list:
    """ROC-кривая: процент успешных сделок при разных порогах confidence."""
    executed = [r for r in records if r["net_actual"] != 0]
    if not executed:
        return []
    
    confs = sorted(set(r["conf"] for r in executed))
    points = []
    for conf in confs:
        sr = calc_success_rate(executed, min_conf=conf)
        if sr["total"] >= 3:  # минимум 3 сделки для статистики
            points.append({
                "conf_threshold": conf,
                "total": sr["total"],
                "winrate": sr["winrate"],
                "avg_pnl": sr["avg_pnl"],
            })
    return points


def calc_reject_reasons(records: list) -> Counter:
    """Распределение причин отказов."""
    rejected = [r for r in records if r["reject_reason"] and r["reject_reason"] != ""]
    reasons = Counter(r["reject_reason"] for r in rejected)
    return reasons


def calc_best_thresholds(roc_points: list, min_trades: int = 5) -> dict:
    """Находит оптимальные пороги confidence для разных целевых winrate."""
    if not roc_points:
        return {}
    
    result = {}
    for target_wr in [90, 95, 99]:
        suitable = [p for p in roc_points 
                    if p["winrate"] >= target_wr and p["total"] >= min_trades]
        if suitable:
            best = min(suitable, key=lambda p: p["conf_threshold"])
            result[f"conf_for_{target_wr}pct_wr"] = {
                "confidence": best["conf_threshold"],
                "winrate": best["winrate"],
                "trades": best["total"],
            }
    
    # Порог с максимальным avg_pnl
    best_avg = max(roc_points, key=lambda p: p["avg_pnl"])
    result["max_avg_pnl"] = {
        "confidence": best_avg["conf_threshold"],
        "avg_pnl": best_avg["avg_pnl"],
        "trades": best_avg["total"],
        "winrate": best_avg["winrate"],
    }
    
    return result


# ════════════════════════════════════════════════
# 3. ВЫВОД В КОНСОЛЬ
# ════════════════════════════════════════════════

def print_results(records: list, roc_points: list, reasons: Counter,
                  thresholds: dict, best_pairs: list):
    """Печатает результаты анализа в консоль."""
    executed = [r for r in records if r["net_actual"] != 0]
    rejected = [r for r in records if r["reject_reason"] and r["reject_reason"] != ""]
    
    print("\n" + "═" * 60)
    print("📊 SHADOW LOGGER — ОТЧЁТ")
    print("═" * 60)
    
    print(f"\n📈 Всего записей:        {len(records)}")
    print(f"   ✅ Исполнено сделок:   {len(executed)}")
    print(f"   🚫 Заблокировано:      {len(rejected)}")
    
    if executed:
        sr = calc_success_rate(executed)
        print(f"\n🏆 Win rate:            {sr['winrate']:.1f}% ({sr['wins']}/{sr['total']})")
        print(f"   Средний PnL:          ${sr['avg_pnl']:.2f}")
        
        diffs = [abs(r["net_expected"] - r["net_actual"]) 
                 for r in executed if r["net_actual"] != 0]
        if diffs:
            print(f"   Среднее расхождение:  {np.mean(diffs):.3f}%")
            print(f"   Медианное расх.:      {np.median(diffs):.3f}%")
        
        evs = [r["expected_ev"] for r in executed if abs(r["expected_ev"]) > 0.01]
        if evs:
            print(f"   Средний Expected EV:  ${np.mean(evs):.2f}")
    
    if rejected:
        print(f"\n🚫 ТОП-10 причин блокировки:")
        for reason, count in reasons.most_common(10):
            pct = count / len(rejected) * 100
            print(f"   {reason:<30} {count:>4} ({pct:>5.1f}%)")
    
    if thresholds:
        print(f"\n🎯 РЕКОМЕНДАЦИИ ПО ПОРОГАМ:")
        for key, info in thresholds.items():
            if key.startswith("conf_for"):
                wr = key.split("_")[-2]
                print(f"   Для winrate >{wr}%:    confidence ≥ {info['confidence']:.0f}"
                      f"  (trades={info['trades']}, wr={info['winrate']:.1f}%)")
            elif key == "max_avg_pnl":
                print(f"   Макс. средний PnL:    confidence ≥ {info['confidence']:.0f}"
                      f"  (avg_pnl=${info['avg_pnl']:.2f}, trades={info['trades']})")
        
        # Конкретная рекомендация
        current_conf = 60  # REALITY_MIN_CONFIDENCE
        best = thresholds.get("conf_for_90pct_wr", {})
        if best and best["confidence"] != current_conf:
            delta = best["confidence"] - current_conf
            direction = "↑ повысь" if delta > 0 else "↓ снизь"
            print(f"\n💡 Рекомендация: {direction} REALITY_MIN_CONFIDENCE"
                  f" до {best['confidence']:.0f} (было {current_conf})")
    
    if best_pairs:
        print(f"\n🔗 ТОП-5 торговых пар (биржи):")
        for pair, count, avg_spread in best_pairs[:5]:
            print(f"   {pair:<30} {count:>4} сделок  спред={avg_spread:.2f}%")
    
    print("═" * 60)


# ════════════════════════════════════════════════
# 4. ГРАФИКИ
# ════════════════════════════════════════════════

def plot_results(records: list, roc_points: list, reasons: Counter,
                 best_pairs: list, thresholds: dict,
                 output_dir: str = "reports"):
    """Строит и сохраняет графики."""
    if not HAS_PLOTS:
        print("⚠️  Установите matplotlib, seaborn: pip install matplotlib seaborn")
        return
    
    Path(output_dir).mkdir(exist_ok=True)
    executed = [r for r in records if r["net_actual"] != 0]
    
    # --- 1. Scatter: expected vs actual net profit ---
    if executed:
        fig, ax = plt.subplots(figsize=(10, 6))
        expected = [r["net_expected"] for r in executed]
        actual = [r["net_actual"] for r in executed]
        colors = ["green" if r["net_actual"] > 0 else "red" for r in executed]
        ax.scatter(expected, actual, c=colors, alpha=0.6, s=30)
        
        # Линия регрессии
        if len(expected) > 3:
            z = np.polyfit(expected, actual, 1)
            p = np.poly1d(z)
            x_line = np.linspace(min(expected), max(expected), 100)
            ax.plot(x_line, p(x_line), "b--", alpha=0.7, label=f"y={z[0]:.2f}x+{z[1]:.2f}")
        
        # Идеальная линия y=x
        lims = [min(expected + actual), max(expected + actual)]
        ax.plot(lims, lims, "k--", alpha=0.3, label="y=x (идеал)")
        
        ax.set_xlabel("Expected Net Profit (%)")
        ax.set_ylabel("Actual Net Profit (%)")
        ax.set_title("📊 Expected vs Actual PnL")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.savefig(f"{output_dir}/expected_vs_actual.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"   ✅ График: {output_dir}/expected_vs_actual.png")
    
    # --- 2. ROC: confidence vs success rate ---
    if roc_points:
        fig, ax = plt.subplots(figsize=(10, 6))
        confs = [p["conf_threshold"] for p in roc_points]
        wrs = [p["winrate"] for p in roc_points]
        trades = [p["total"] for p in roc_points]
        
        ax.plot(confs, wrs, "b-", linewidth=2, label="Winrate")
        ax.fill_between(confs, wrs, alpha=0.1)
        
        # Размер точек пропорционален числу сделок
        sizes = [max(20, min(200, t * 2)) for t in trades]
        ax.scatter(confs, wrs, s=sizes, c="blue", alpha=0.4, zorder=5)
        
        ax.axhline(90, color="green", linestyle="--", alpha=0.5, label="90% target")
        ax.axhline(95, color="orange", linestyle="--", alpha=0.5, label="95% target")
        ax.axhline(99, color="red", linestyle="--", alpha=0.5, label="99% target")
        
        # Отметить оптимальный порог из рекомендаций
        if "conf_for_90pct_wr" in thresholds:
            opt = thresholds["conf_for_90pct_wr"]
            ax.axvline(opt["confidence"], color="purple", linestyle=":", alpha=0.7)
            ax.annotate(f"opt={opt['confidence']:.0f}", 
                       xy=(opt["confidence"], 90),
                       xytext=(opt["confidence"] + 5, 85),
                       arrowprops=dict(arrowstyle="->", color="purple"))
        
        ax.set_xlabel("Confidence Threshold")
        ax.set_ylabel("Winrate (%)")
        ax.set_title("🎯 ROC-кривая: Confidence vs Success Rate")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.savefig(f"{output_dir}/roc_curve.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"   ✅ График: {output_dir}/roc_curve.png")
    
    # --- 3. Heatmap: биржевые пары ---
    if executed:
        fig, ax = plt.subplots(figsize=(12, 8))
        pairs_data = defaultdict(lambda: {"count": 0, "avg_pnl": 0, "avg_conf": 0})
        for r in executed:
            pair = f"{r['buy_ex']}→{r['sell_ex']}"
            p = pairs_data[pair]
            p["count"] += 1
            p["avg_pnl"] += r["net_actual"]
            p["avg_conf"] += r["conf"]
        
        for pair in pairs_data:
            p = pairs_data[pair]
            if p["count"] > 0:
                p["avg_pnl"] /= p["count"]
                p["avg_conf"] /= p["count"]
        
        # Топ-15 пар по числу сделок
        top_pairs = sorted(pairs_data.items(), key=lambda x: x[1]["count"], reverse=True)[:15]
        if top_pairs:
            names = [p[0] for p in top_pairs]
            counts = [p[1]["count"] for p in top_pairs]
            avg_pnls = [p[1]["avg_pnl"] for p in top_pairs]
            
            # Матрица: биржи-строки vs биржи-столбцы
            ex_names = sorted(set(
                ex for pair in names 
                for ex in pair.split("→")
            ))
            n = len(ex_names)
            heat = np.zeros((n, n))
            for i, buy_ex in enumerate(ex_names):
                for j, sell_ex in enumerate(ex_names):
                    if buy_ex == sell_ex:
                        continue
                    pair_key = f"{buy_ex}→{sell_ex}"
                    if pair_key in pairs_data:
                        heat[i, j] = pairs_data[pair_key]["avg_pnl"]
            
            mask = heat == 0
            sns.heatmap(heat, annot=True, fmt=".3f", cmap="RdYlGn",
                       xticklabels=ex_names, yticklabels=ex_names,
                       mask=mask, center=0, ax=ax,
                       cbar_kws={"label": "Avg PnL (%)"})
            ax.set_title("🔥 Heatmap: Средний PnL по парам бирж")
            fig.savefig(f"{output_dir}/exchange_heatmap.png", dpi=150, bbox_inches="tight")
            plt.close(fig)
            print(f"   ✅ График: {output_dir}/exchange_heatmap.png")
    
    # --- 4. Pie: причины отказов ---
    if reasons:
        fig, ax = plt.subplots(figsize=(10, 8))
        top_reasons = reasons.most_common(8)
        labels = [f"{r[0]}\n({r[1]})" for r in top_reasons]
        values = [r[1] for r in top_reasons]
        others = sum(r[1] for r in reasons.most_common()[8:])
        if others > 0:
            labels.append(f"other\n({others})")
            values.append(others)
        
        colors = plt.cm.Set3(np.linspace(0, 1, len(values)))
        wedges, texts, autotexts = ax.pie(
            values, labels=labels, autopct="%1.1f%%",
            colors=colors, startangle=90, pctdistance=0.85
        )
        ax.set_title("🚫 Причины блокировки сделок")
        fig.savefig(f"{output_dir}/reject_reasons.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"   ✅ График: {output_dir}/reject_reasons.png")
    
    # --- 5. Distribution of expected vs actual error ---
    if executed:
        fig, ax = plt.subplots(figsize=(10, 6))
        errors = [r["net_expected"] - r["net_actual"] for r in executed]
        ax.hist(errors, bins=30, edgecolor="black", alpha=0.7, color="steelblue")
        ax.axvline(0, color="red", linestyle="--", linewidth=2)
        ax.set_xlabel("Expected - Actual (%)")
        ax.set_ylabel("Frequency")
        ax.set_title("📉 Распределение ошибки Expected vs Actual")
        ax.grid(True, alpha=0.3)
        fig.savefig(f"{output_dir}/error_distribution.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"   ✅ График: {output_dir}/error_distribution.png")


# ════════════════════════════════════════════════
# 5. MAIN
# ════════════════════════════════════════════════

def main():
    # Парсинг аргументов
    max_hours = 0
    show = False
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--hours" and i + 1 < len(args):
            max_hours = float(args[i + 1])
        elif arg == "--show":
            show = True
    
    print("🔍 Shadow Logger Analyzer v1.0")
    print("─" * 50)
    
    records = load_shadow_log(max_hours=max_hours)
    if not records:
        print("Нет данных для анализа. Запустите бота и подождите сделок.")
        return
    
    # Анализ
    roc_points = calc_roc_curve(records)
    reasons = calc_reject_reasons(records)
    thresholds = calc_best_thresholds(roc_points)
    
    # Топ пар
    executed = [r for r in records if r["net_actual"] != 0]
    pair_stats = Counter(f"{r['buy_ex']}→{r['sell_ex']}" for r in executed)
    pair_spreads = defaultdict(list)
    for r in executed:
        pair = f"{r['buy_ex']}→{r['sell_ex']}"
        pair_spreads[pair].append(r["spread"])
    best_pairs = [
        (p, c, np.mean(pair_spreads[p]) if pair_spreads[p] else 0)
        for p, c in pair_stats.most_common(10)
    ]
    
    # Вывод
    print_results(records, roc_points, reasons, thresholds, best_pairs)
    
    # Графики
    if HAS_PLOTS:
        print("\n📈 Построение графиков...")
        plot_results(records, roc_points, reasons, best_pairs, thresholds)
        print("   Все графики сохранены в reports/")
    
    if show and HAS_PLOTS:
        print("\n💡 Откройте папку reports/ для просмотра графиков")
    
    # Итоговая рекомендация
    if thresholds:
        print("\n" + "─" * 50)
        print("ИТОГО:")
        current_conf = 60  # REALITY_MIN_CONFIDENCE
        best = thresholds.get("conf_for_90pct_wr", {})
        if best:
            print(f"→ Рекомендуемый REALITY_MIN_CONFIDENCE: {best['confidence']:.0f}"
                  f" (сейчас {current_conf})")
            if best["confidence"] > current_conf:
                print(f"  ↑ Повышение на {best['confidence'] - current_conf:.0f} ед.")
            elif best["confidence"] < current_conf:
                print(f"  ↓ Снижение на {current_conf - best['confidence']:.0f} ед.")
        
        max_pnl = thresholds.get("max_avg_pnl", {})
        if max_pnl and max_pnl.get("confidence"):
            print(f"→ Макс. средний PnL при confidence ≥ {max_pnl['confidence']:.0f}:"
                  f" ${max_pnl['avg_pnl']:.2f}/сделку")
        
        total_rejected = len(reasons)
        if total_rejected > 0:
            top_reason = reasons.most_common(1)[0]
            print(f"→ Главная причина блокировки: {top_reason[0]} ({top_reason[1]} раз)")


if __name__ == "__main__":
    main()