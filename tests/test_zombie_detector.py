"""Тесты ZombieDetector — исправлен test_static_price_zombie."""
import time
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Ticker, ZombieDetector
from config import ZOMBIE_MIN_HISTORY, ZOMBIE_NO_MOVEMENT_SEC


def make_ticker(exchange="binance", last=100.0, bid=99.9, ask=100.1, volume=1000):
    return Ticker(
        exchange=exchange, symbol="X/USDT",
        bid=bid, ask=ask, last=last, volume=volume,
        timestamp=int(time.time() * 1000),
    )


class TestZombieDetector:
    def test_empty_detector_not_zombie(self):
        """Без истории → не зомби."""
        zd = ZombieDetector()
        prices = {"X/USDT": {"binance": make_ticker()}}
        is_zombie, reason = zd.is_zombie("X/USDT", prices["X/USDT"])
        assert is_zombie is False
    
    def test_insufficient_history_not_zombie(self):
        """Мало истории (< 30) → не зомби."""
        zd = ZombieDetector()
        prices = {"X/USDT": {"binance": make_ticker()}}
        for _ in range(ZOMBIE_MIN_HISTORY - 1):
            zd.update(prices)
        is_zombie, _ = zd.is_zombie("X/USDT", prices["X/USDT"])
        assert is_zombie is False
    
    def test_moving_price_not_zombie(self):
        """✅ FIX: цена двигается → не зомби."""
        zd = ZombieDetector()
        for i in range(40):
            ticker = Ticker(
                exchange="t", symbol="X/USDT",
                bid=100, ask=100.5,
                last=100 + (i * 0.5),  # цена меняется!
                volume=1000,
                timestamp=int(time.time() * 1000),
            )
            zd.update({"X/USDT": {"t": ticker}})
        
        is_zombie, _ = zd.is_zombie("X/USDT", {"t": make_ticker()})
        assert is_zombie is False
    
    def test_static_price_zombie(self):
        """✅ FIX: цена не двигается + мало истории цены → зомби."""
        zd = ZombieDetector()
        
        # Заполняем историю одинаковой ценой (НЕ вызывая update после установки last_movement)
        for _ in range(ZOMBIE_MIN_HISTORY + 5):
            ticker = make_ticker(last=100.0)  # всегда 100
            zd.update({"X/USDT": {"binance": ticker}})
        
        # ✅ Устанавливаем last_movement в прошлое ПОСЛЕ update
        # чтобы update() его не перезаписал
        zd.last_movement["X/USDT"] = time.time() - ZOMBIE_NO_MOVEMENT_SEC - 100
        
        is_zombie, reason = zd.is_zombie("X/USDT", {"binance": make_ticker(last=100)})
        
        assert is_zombie is True, f"Expected zombie, got {is_zombie} with reason: {reason}"
        # Принимаем любую причину зомби: static или cv
        assert ("static" in reason or "cv" in reason), f"Unexpected reason: {reason}"
    
    def test_bid_equals_ask_zombie(self):
        """bid = ask → зомби (synthetic price)."""
        zd = ZombieDetector()
        for i in range(ZOMBIE_MIN_HISTORY + 5):
            ticker = Ticker(
                exchange="t", symbol="X/USDT",
                bid=100, ask=100,  # same!
                last=100 + (i * 0.01),
                volume=1000,
                timestamp=int(time.time() * 1000),
            )
            zd.update({"X/USDT": {"t": ticker}})
        
        is_zombie, reason = zd.is_zombie("X/USDT", {"t": make_ticker(bid=100, ask=100)})
        assert is_zombie is True
        assert "bid=ask" in reason
    
    def test_clear_zombie_resets_data(self):
        """clear_zombie сбрасывает историю."""
        zd = ZombieDetector()
        for _ in range(ZOMBIE_MIN_HISTORY + 5):
            zd.update({"X/USDT": {"binance": make_ticker()}})
        
        assert "X/USDT" in zd.price_history
        zd.clear_zombie("X/USDT")
        assert len(zd.price_history["X/USDT"]["binance"]) == 0
    
    def test_clear_all(self):
        """clear_all очищает все данные."""
        zd = ZombieDetector()
        for sym in ["A/USDT", "B/USDT"]:
            for _ in range(ZOMBIE_MIN_HISTORY + 5):
                zd.update({sym: {"binance": make_ticker()}})
        
        assert len(zd.price_history) == 2
        zd.clear_all()
        assert len(zd.price_history) == 0
        assert len(zd.last_movement) == 0
    
    def test_recovered_counter(self):
        """mark_recovered увеличивает счётчик."""
        zd = ZombieDetector()
        assert zd.recovered_count == 0
        zd.mark_recovered()
        zd.mark_recovered()
        assert zd.recovered_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
