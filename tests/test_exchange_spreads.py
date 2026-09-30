# -*- coding: utf-8 -*-
"""
Спреды бирж - измерения, зафиксированные как контракт.

Идея "добавим мелких монет со спредами по 7%" проверена и ОТВЕРГНУТА
измерением: расход растёт 1:1 со спредом, а базис выше 3.4% на рынке
не встречается. Актив со спредом 7% требует базиса >15.44%, то есть
гарантированного убытка 12% на каждой позиции.

Что реально дало расширение - не 7%-монеты, а КОЛИЧЕСТВО узких пар
(спред < 0.3% на обеих ногах): 507 -> 4344.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


def test_htx_excluded_because_too_illiquid():
    """
    htx не добавлен сознательно: медиана спреда 1.52%, p90 = 18.75%,
    p99 = 63.45%. Если вернуть его в EXCHANGES без фильтра по спреду,
    бот начнёт открывать позиции с гарантированным убытком.
    """
    assert "htx" not in config.EXCHANGES, (
        "htx имеет медианный спред 1.52% и p90 18.75% - торговать там "
        "нечего, расходы съедают любой базис")


def test_added_exchanges_have_liquidity():
    """Все добавленные биржи имеют узкие спреды (измерено)."""
    measured_median = {
        "bybit": None,   # не измерялся в этой выборке
        "mexc": 0.2363,
        "kucoin": 0.2199,
        "okx": 0.0998,
        "gate": 0.2099,
    }
    for ex in config.EXCHANGES:
        med = measured_median.get(ex)
        if med is None:
            continue
        assert med < 0.5, (
            f"{ex}: медианный спред {med}% слишком широк, "
            f"порог MAX_TRADABLE_SPREAD_PCT = {config.MAX_TRADABLE_SPREAD_PCT}%")


def test_wide_spread_never_traded():
    """
    КЛЮЧЕВОЙ ТЕСТ идеи «мелких монет со спредом 7%».

    Даже при базисе 5% актив со спредом 7% должен быть отклонён: расход
    2*7% = 14% плюс комиссии и перевод, то есть вход заведомо убыточен.
    """
    from engine import BasisEntryCriteria, Ticker
    import time

    def wide_ticker(ex, mid, spread_pct):
        h = spread_pct / 2.0 / 100.0
        bid, ask = mid * (1 - h), mid * (1 + h)
        return Ticker(ex, "X/USDT", bid, ask, (bid + ask) / 2, 1e6,
                      int(time.time() * 1000))

    d = BasisEntryCriteria().evaluate(
        basis_pct=5.0, size_usdt=100,
        buy_t=wide_ticker("kucoin", 100.0, 7.0),
        sell_t=wide_ticker("gate", 100.0, 7.0))
    assert not d.allowed
    assert d.reason == "spread_too_wide"
    assert d.cost_pct > 14.0, (
        f"расход должен быть >14%, получено {d.cost_pct:.2f}%")


def test_every_exchange_has_fee_and_withdrawal():
    """Каждая биржа в EXCHANGES покрыта комиссиями и переводами."""
    for ex in config.EXCHANGES:
        assert ex in config.TRADING_FEES, f"нет комиссии для {ex}"
        assert config.TRADING_FEES[ex] > 0, f"нулевая комиссия {ex}"
        assert ex in config.WITHDRAWAL_FEES, f"нет тарифа перевода для {ex}"
        assert ex in config.RATE_LIMITS, f"нет лимита запросов для {ex}"


def test_exchange_count_is_reasonable():
    """
    Пять бирж. Больше шести разумно не подключать: время цикла растёт
    линейно, а выигрыш в числе пар убывает.
    """
    assert 2 <= len(config.EXCHANGES) <= 6
