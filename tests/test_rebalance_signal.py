# -*- coding: utf-8 -*-
"""
Сигнал ребалансировки балансов между биржами (Шаг 0.6).

Сигнал, а не перевод: автоматический перевод в live опасен (комиссия,
время заморозки, риск остаться без хеджа). Задача - показать откуда и
куда перелить, решение принимает человек.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import BalanceManager


def test_no_signal_when_balances_even():
    bm = BalanceManager({e: {"USDT": 800.0}
                         for e in ("mexc", "bybit", "kucoin", "okx", "gate")})
    assert bm.rebalance_signal(target_per_exchange=800.0) == []


def test_measured_drift_is_not_alarming():
    """
    Измеренный дрейф после 10 сделок: mexc 792.82 при цели 800 (99%),
    bybit 841.04. Это НЕ повод для перевода - сигнала быть не должно.
    """
    bm = BalanceManager({"mexc": {"USDT": 792.82}, "bybit": {"USDT": 841.04},
                         "kucoin": {"USDT": 800.0}, "okx": {"USDT": 800.0},
                         "gate": {"USDT": 800.0}})
    assert bm.rebalance_signal(target_per_exchange=800.0) == []


def test_signal_points_from_rich_to_poor():
    """Главное: сигнал показывает ОТКУДА и КУДА, а не только факт перекоса."""
    bm = BalanceManager({"mexc": {"USDT": 150.0}, "bybit": {"USDT": 1600.0},
                         "kucoin": {"USDT": 800.0}, "okx": {"USDT": 800.0},
                         "gate": {"USDT": 800.0}})
    sig = bm.rebalance_signal(target_per_exchange=800.0)
    assert len(sig) == 1
    s = sig[0]
    assert s["from"] == "bybit", f"донор неверный: {s['from']}"
    assert s["to"] == "mexc", f"получатель неверен: {s['to']}"
    assert s["amount_usdt"] > 0
    assert s["urgency"] == "critical"
    assert "800" in s["reason"]


def test_signal_never_promises_reserved_money():
    """
    Ключевое ограничение: сигнал не может предложить перевести деньги,
    которые лежат в резерве под открытые позиции.
    """
    bm = BalanceManager({"mexc": {"USDT": 100.0}, "bybit": {"USDT": 3000.0}})
    bm.reserve("bybit", "USDT", 2500.0)          # free = 500
    sig = bm.rebalance_signal(target_per_exchange=1550.0, min_amount=10.0)
    for s in sig:
        assert s["amount_usdt"] <= 500.0, (
            f"сигнал обещает {s['amount_usdt']}, свободно только 500")


def test_small_transfers_suppressed():
    """
    Перевод меньше min_amount не сигналится: комиссия и время заморозки
    съедают больше самой суммы.
    """
    bm = BalanceManager({"mexc": {"USDT": 395.0}, "bybit": {"USDT": 805.0}})
    assert bm.rebalance_signal(target_per_exchange=600.0, min_amount=10.0) == []


def test_urgency_depends_on_severity():
    """25% от нормы - critical, 40% - warning."""
    bm = BalanceManager({"mexc": {"USDT": 200.0}, "bybit": {"USDT": 1400.0}})
    crit = bm.rebalance_signal(target_per_exchange=800.0, trigger_ratio=0.5)
    assert crit and crit[0]["urgency"] == "critical"

    bm2 = BalanceManager({"mexc": {"USDT": 320.0}, "bybit": {"USDT": 1280.0}})
    warn = bm2.rebalance_signal(target_per_exchange=800.0, trigger_ratio=0.5)
    assert warn and warn[0]["urgency"] == "warning"


def test_no_signal_without_donors():
    """Некому переводить - сигнала нет, даже при дефиците."""
    bm = BalanceManager({"mexc": {"USDT": 100.0}, "bybit": {"USDT": 110.0}})
    assert bm.rebalance_signal(target_per_exchange=800.0) == []


def test_target_defaults_to_equally_split():
    """Если цель не задана, делится поровну по всем биржам."""
    # a=150 ниже порога (400*0.5=200), b=600 выше цели 400
    bm = BalanceManager({"a": {"USDT": 150.0}, "b": {"USDT": 650.0}})
    sig = bm.rebalance_signal()
    assert sig, "при явном перекосе сигнала быть не должно быть"
    assert sig[0]["to"] == "a" and sig[0]["from"] == "b"
