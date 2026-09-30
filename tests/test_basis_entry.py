# -*- coding: utf-8 -*-
"""
Финансовый критерий входа и адаптивный таймаут (Шаг 0.4).

Критерий отвечает на вопрос, на который не отвечает confidence:
«заработаю ли я на удержании». Ответ даёт базис минус расходы, а
расходы измерены эмпирически, а не взяты из модели.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engine import (
    BasisEntryCriteria, EntryDecision, LifetimeTracker, OpenPair, Ticker,
    ProfitCalculator,
)


def tk(ex="mexc", mid=100.0, spread_pct=0.2, base="SOL", quote="USDT"):
    """Тикер с заданным ПОЛНЫМ спредом (bid_ask_spread_pct)."""
    h = spread_pct / 2.0 / 100.0
    bid, ask = mid * (1 - h), mid * (1 + h)
    return Ticker(ex, base + "/" + quote, bid, ask, (bid + ask) / 2,
                  1e6, int(time.time() * 1000))


# ── Формула расходов сверена с измерениями ──

def test_costs_match_measured_values():
    """
    Измерено (tests/test_measured_costs.py), size=100:
        спреды 0.0/0.0 -> 1.248%
        спреды 0.2/0.2 -> 2.051%
        спреды 0.4/0.4 -> 2.850%
    """
    c = BasisEntryCriteria()
    for hs, hf, measured in [(0.0, 0.0, 1.248), (0.2, 0.2, 2.051),
                             (0.4, 0.4, 2.850)]:
        got = c._costs_pct(100, tk(spread_pct=hs), tk("bybit", 102.0, hf))
        assert abs(got - measured) < 0.05, (
            f"спреды {hs}/{hf}: получено {got:.3f}%, измерено {measured:.3f}%")


def test_costs_grow_with_size():
    c = BasisEntryCriteria()
    small = c._costs_pct(100, tk(), tk("bybit", 102.0))
    large = c._costs_pct(1000, tk(), tk("bybit", 102.0))
    assert large > small, "расход обязан расти с размером (SLIPPAGE_PER_USDT)"


# ── Отказы ──

def test_thin_asset_rejected_by_spread():
    """Актив с широким спредом не торгуется НИ ПРИ КАКОМ базисе."""
    d = BasisEntryCriteria().evaluate(
        basis_pct=5.0, size_usdt=100,
        buy_t=tk(spread_pct=config.MAX_TRADABLE_SPREAD_PCT + 0.5),
        sell_t=tk("bybit", 102.0, 0.2))
    assert not d.allowed
    assert d.reason == "spread_too_wide"


def test_basis_below_minimum_rejected():
    d = BasisEntryCriteria().evaluate(
        basis_pct=-0.167, size_usdt=100, buy_t=tk(), sell_t=tk("bybit", 102.0))
    assert not d.allowed
    assert d.reason == "basis_below_minimum"


def test_holding_cost_can_turn_profit_into_loss():
    """
    Базис 2.2% проходит сам по себе, но при длинном удержании и высоком
    funding съедается. Это проверка того, что время удержания входит в расчёт.
    """
    c = BasisEntryCriteria()
    bt, st = tk(), tk("bybit", 102.0, 0.2)
    short_hold = c.evaluate(2.2, 100, bt, st, funding_rate_per_hour=0.0, hold_hours=0.5)
    long_hold = c.evaluate(2.2, 100, bt, st, funding_rate_per_hour=0.01, hold_hours=4.0)
    assert long_hold.cost_pct > short_hold.cost_pct
    assert long_hold.expected_net_pct < short_hold.expected_net_pct


def test_high_basis_with_narrow_spread_allowed():
    d = BasisEntryCriteria().evaluate(
        basis_pct=2.5, size_usdt=100, buy_t=tk(spread_pct=0.1),
        sell_t=tk("bybit", 102.0, 0.1), hold_hours=1.0)
    assert d.allowed
    assert d.expected_net_pct > 0


# ── Адаптивный таймаут ──

def test_hold_hours_grows_with_basis():
    assert (BasisEntryCriteria.hold_hours_for(2.0)
            < BasisEntryCriteria.hold_hours_for(3.0)
            < BasisEntryCriteria.hold_hours_for(6.0))


def test_hold_hours_capped_at_max():
    assert BasisEntryCriteria.hold_hours_for(99.0) == config.PAPER_HOLD_HOURS_MAX


def test_hold_hours_stored_in_pair_not_trader():
    """
    Ключевой дефект, который закрывает Шаг 0.4.

    Таймаут принадлежит паре. Общее значение в трейдере перезаписывалось бы
    при открытии каждой новой пары, и самая ценная (с наибольшим базисом)
    получила бы таймаут чужой пары.
    """
    p = OpenPair(symbol="SOL/USDT", buy_exchange="mexc",
                 sell_exchange="bybit", direction="fut_premium",
                 amount=1.0, size_usdt=100.0, entry_spot=100.0,
                 entry_fut=105.0, entry_basis=4.5, opened_at=time.time(),
                 hold_hours=3.5)
    assert p.hold_hours == 3.5
    assert p.hold_hours != config.PAPER_HOLD_HOURS_MAX


# ── history: обрезка снизу ──

def test_history_no_data_neutral():
    assert LifetimeTracker().get_repeatability("SOL/USDT", "mexc->bybit") == 50.0


def test_history_bad_experience_not_above_neutral():
    """
    Плохой опыт не должен цениться выше незнания.

    Без обрезки формула давала 41.9 при коротких жизнях — то есть система
    награждала отсутствие данных выше реального отрицательного опыта.
    """
    t = LifetimeTracker()
    t.history["SOL/USDT"]["mexc->bybit"] = [5] * 5
    assert t.get_repeatability("SOL/USDT", "mexc->bybit") == 50.0


def test_history_good_experience_above_neutral():
    t = LifetimeTracker()
    t.history["SOL/USDT"]["mexc->bybit"] = [120] * 20
    assert t.get_repeatability("SOL/USDT", "mexc->bybit") > 50.0


def test_history_monotonic_in_quality():
    t = LifetimeTracker()
    scores = []
    for life in [5, 30, 60, 120, 240]:
        t.history["T"]["mexc->bybit"] = [life] * 10
        scores.append(t.get_repeatability("T", "mexc->bybit"))
    assert scores == sorted(scores), f"оценка не монотонна: {scores}"
    assert scores[0] >= 50.0
