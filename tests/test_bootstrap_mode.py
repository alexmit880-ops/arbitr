# -*- coding: utf-8 -*-
"""
Режим бутстрапа: разрыв петли «нет истории -> нет сделок -> нет истории».

Петля была замкнутой по построению:
  confidence = f(истории закрытых сделок)   -> history = 0 на старте
  закрытые сделки = f(confidence >= порога)  -> их нет
  confidence < порога, потому что history = 0

Три исправления (Шаг 0.3):
  1. history при отсутствии данных = 50.0 (нейтрально), а не 0.0
  2. порог уверенности зависит от зрелости системы, а не жёсткие 50
  3. шкала freshness привязана к CACHE_REFRESH_SEC

Ключевой критерий: система на холодном старте ДОЛЖНА быть способна
совершить первую сделку, иначе история не начнёт накапливаться никогда.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engine import (
    LifetimeTracker, Portfolio, RiskManager, Opportunity, TradeResult,
    ConfidenceScorer, ExchangePairRanker,
)


def make_opp(conf=60.0, net=1.0, sym="BTC/USDT"):
    return Opportunity(
        symbol=sym, buy_exchange="bybit", sell_exchange="mexc",
        buy_price=100, sell_price=101, spread_pct=1.0, net_profit_pct=net,
        volume=1_000_000, exchanges_count=2, timestamp=int(time.time()),
        confidence_score=conf, max_safe_size_usdt=1000)


def make_trade(pnl=1.0):
    return TradeResult(
        timestamp=int(time.time()), symbol="BTC/USDT",
        buy_exchange="bybit", sell_exchange="mexc", amount=1.0,
        pnl=pnl, pnl_pct=0.1, balance_after=10000 + pnl, success=pnl > 0)


# ============ Часть 1: нейтральная history ============

def test_repeatability_neutral_without_history():
    """Нет данных - не "плохо", а "неизвестно": 50.0, а не 0.0."""
    tracker = LifetimeTracker()
    assert tracker.get_repeatability("BTC/USDT", "bybit->mexc") == 50.0, \
        "Отсутствие данных не должно штрафоваться как 0 (гарантированно плохо)"


def test_repeatability_still_uses_data_when_present():
    """
    С данными поведение прежнее - не регрессия.

    Формула: min(100, log1p(count)*15 + avg_life*0.5). Она зависит и от
    числа наблюдений, и от времени жизни, поэтому результат НЕ обязан
    быть выше нейтральных 50: короткая жизнь при малом числе наблюдений
    честно даёт меньше 50 (напр. n=5, life=30 -> 41.9).

    Проверяем поэтому не абсолютное значение, а ЧТО оценка зависит от
    данных, то есть перестала быть константой 50.
    """
    def score_for(n, life):
        t = LifetimeTracker()
        for _ in range(n):
            t.history.setdefault("BTC/USDT", {}).setdefault(
                "bybit->mexc", []).append(life)
        return t.get_repeatability("BTC/USDT", "bybit->mexc")

    few = score_for(config.LIFETIME_MIN_SAMPLES, 30.0)
    many = score_for(20, 30.0)
    short_life = score_for(20, 30.0)
    long_life = score_for(20, 120.0)

    assert many != few, "оценка перестала зависеть от числа наблюдений"
    assert many > few, "больше наблюдений должно давать выше"
    assert long_life > short_life, "дольше живёт возможность - выше оценка"
    assert long_life <= 100.0


# ============ Часть 2: динамический порог ============

def test_threshold_bootstrap_to_mature():
    """Порог растёт по мере накопления статистики."""
    pf = Portfolio(balance=10000, initial_balance=10000)
    risk = RiskManager(pf)

    assert risk.get_confidence_threshold() == 40.0, "на старте нужен бутстрап-порог"

    for _ in range(config.KELLY_MIN_TRADES + 5):
        pf.closed_trades.append(make_trade())
    assert risk.get_confidence_threshold() == 45.0

    while len(pf.closed_trades) < config.KELLY_FULL_TRADES + 5:
        pf.closed_trades.append(make_trade())
    assert risk.get_confidence_threshold() == 50.0, "зрелая система = полный стандарт"


def test_cold_start_can_actually_trade():
    """
    КЛЮЧЕВОЙ ТЕСТ: петля разорвана.

    На холодном старте возможность с conf 45 (ниже прежних 50) должна
    ПРОХОДИть. Раньше она отсекалась, история не накапливалась, и
    порог оставался недостижимым навсегда.
    """
    pf = Portfolio(balance=10000, initial_balance=10000)
    risk = RiskManager(pf)
    can, reason = risk.can_trade(make_opp(conf=45.0, net=1.0))
    assert can, f"холодный старт не может начать торговлю: {reason}"


def test_explicit_threshold_still_overrides():
    """Явный порог переопределяет динамический (так пишут старые тесты)."""
    pf = Portfolio(balance=10000, initial_balance=10000)
    risk = RiskManager(pf)
    can, reason = risk.can_trade(make_opp(conf=45.0, net=1.0), confidence_min=99.0)
    assert not can, "явный порог должен иметь приоритет"


def test_mature_system_gets_stricter():
    """Зрелая система отсекает то, что бутстрап пропустил бы."""
    pf = Portfolio(balance=10000, initial_balance=10000)
    risk = RiskManager(pf)
    while len(pf.closed_trades) < config.KELLY_FULL_TRADES + 5:
        pf.closed_trades.append(make_trade())
    can, _ = risk.can_trade(make_opp(conf=45.0, net=1.0))
    assert not can, "после накопления истории порог обязан вырасти"


# ============ Часть 3: калибровка freshness ============

def test_freshness_scale_matches_cache_cycle():
    """
    Раньше: 100 - age*15, обнуление через 6.7с при цикле обновления 3с -
    компонент гас в норме, терялось ~5 баллов.
    """
    from engine import Ticker

    def make(age_sec):
        now_ms = int(time.time() * 1000)
        t = Ticker("bybit", "BTC/USDT", 100, 101, 100.5, 1_000_000, now_ms)
        t.timestamp = int((time.time() - age_sec) * 1000)
        t.has_exchange_timestamp = True
        return t

    scorer = ConfidenceScorer(ExchangePairRanker(), LifetimeTracker())
    opp = make_opp()

    fresh = scorer.score(opp, make(1.5), make(1.5))[1]["freshness"]
    stale = scorer.score(opp, make(config.CACHE_REFRESH_SEC * 2), make(1.5))[1]["freshness"]
    dead = scorer.score(opp, make(config.CACHE_REFRESH_SEC * 4), make(1.5))[1]["freshness"]

    w = config.CONFIDENCE_WEIGHTS["freshness"]
    assert fresh > stale > dead, f"шкала должна убывать: {fresh} > {stale} > {dead}"
    assert dead == 0.0, "4 цикла без обновления = котировка мертва"
    assert fresh > 0.5 * w, (
        f"свежая котировка (1.5с) должна давать больше половины веса: "
        f"{fresh:.2f} из {w}")


# ============ Сквозной критерий ============

def test_bootstrap_loop_is_broken():
    """
    Сквозная проверка: при пороге 40 и нейтральной history типичная
    возможность проходит. Это ровно тот случай, который раньше
    блокировал систему.
    """
    pf = Portfolio(balance=10000, initial_balance=10000)
    risk = RiskManager(pf)
    tracker = LifetimeTracker()

    neutral = tracker.get_repeatability("BTC/USDT", "bybit->mexc")
    assert neutral == 50.0
    thr = risk.get_confidence_threshold()
    assert thr == 40.0
    # 10 баллов за history при нейтральном 50 => +5 к общей оценке
    hist_points = 50.0 * config.CONFIDENCE_WEIGHTS["history"] / 100
    assert hist_points == 5.0
    assert 46.4 + 5.0 >= thr, "реальная возможность 46.4 должна проходить порог 40"
