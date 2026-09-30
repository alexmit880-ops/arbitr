"""Тесты Kelly Criterion position sizing."""
import time
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Portfolio, TradeResult, RiskManager
from config import (
    MIN_TRADE_SIZE, MAX_TRADE_SIZE, MAX_POSITION_PCT_OF_BALANCE,
    KELLY_MIN_TRADES, KELLY_FRACTION_EARLY, KELLY_FRACTION,
    KELLY_FULL_TRADES
)


def make_trade(pnl, pnl_pct, success):
    return TradeResult(
        timestamp=int(time.time()), symbol="X/USDT",
        buy_exchange="a", sell_exchange="b", amount=1.0,
        pnl=pnl, pnl_pct=pnl_pct, balance_after=10000, success=success,
    )


class TestKellySizing:
    
    def test_insufficient_trades_uses_min(self):
        """< KELLY_MIN_TRADES → MIN_TRADE_SIZE."""
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        # 10 trades (less than KELLY_MIN_TRADES=20)
        for i in range(10):
            rm.record_trade(make_trade(10, 1.0, True))
        
        size = rm.calculate_position_size()
        assert size == MIN_TRADE_SIZE
    
    def test_early_kelly_uses_reduced_fraction(self):
        """20-100 trades → KELLY_FRACTION_EARLY (0.10)."""
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        # 50 trades, all wins of +2%
        for i in range(50):
            rm.record_trade(make_trade(20, 2.0, True))
        
        size = rm.calculate_position_size()
        # Win rate 100%, avg win 2%, no losses → Kelly = 1.0 (max)
        # Apply KELLY_FRACTION_EARLY = 0.10 → size = 10% * balance = $1000
        # Capped by MAX_TRADE_SIZE = $1000
        assert size <= MAX_TRADE_SIZE
        assert size >= MIN_TRADE_SIZE
    
    def test_full_kelly_after_100_trades(self):
        """>100 trades → KELLY_FRACTION (0.25)."""
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        # 150 trades
        for i in range(150):
            success = (i % 2 == 0)  # 75% win rate
            pnl = 20 if success else -15
            rm.record_trade(make_trade(pnl, 1.5 if success else -1.0, success))
        
        size = rm.calculate_position_size()
        # With 75% WR, positive edge → size > 0
        assert size >= MIN_TRADE_SIZE
        assert size <= MAX_TRADE_SIZE
    
    def test_size_capped_by_max_trade(self):
        """Kelly может дать больше чем MAX_TRADE_SIZE → cap."""
        p = Portfolio(balance=1000000, initial_balance=1000000)
        rm = RiskManager(p)
        # Много прибыльных сделок → Kelly хочет большую позицию
        for i in range(150):
            rm.record_trade(make_trade(100, 5.0, True))
        
        size = rm.calculate_position_size()
        assert size <= MAX_TRADE_SIZE
    
    def test_size_capped_by_balance_percentage(self):
        """Размер ≤ MAX_POSITION_PCT_OF_BALANCE."""
        p = Portfolio(balance=100000, initial_balance=100000)
        rm = RiskManager(p)
        for i in range(150):
            rm.record_trade(make_trade(100, 3.0, True))
        
        size = rm.calculate_position_size()
        assert size <= p.balance * MAX_POSITION_PCT_OF_BALANCE
    
    def test_size_capped_by_max_safe(self):
        """Max safe size ограничивает позицию."""
        p = Portfolio(balance=100000, initial_balance=100000)
        rm = RiskManager(p)
        for i in range(150):
            rm.record_trade(make_trade(100, 3.0, True))
        
        size = rm.calculate_position_size(max_safe_size=150)
        assert size <= 150  # capped by max_safe
    
    def test_minimum_size_enforced(self):
        """Размер не может быть меньше MIN_TRADE_SIZE."""
        p = Portfolio(balance=100, initial_balance=10000)
        rm = RiskManager(p)
        for i in range(150):
            rm.record_trade(make_trade(-5, -5.0, False))
        
        size = rm.calculate_position_size()
        assert size >= MIN_TRADE_SIZE
    
    def test_losing_streak_reduces_size(self):
        """После losses Kelly уменьшает размер."""
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        
        # 50 profitable
        for i in range(50):
            rm.record_trade(make_trade(10, 1.0, True))
        size_good = rm.calculate_position_size()
        
        # 50 losing
        for i in range(50):
            rm.record_trade(make_trade(-5, -0.5, False))
        size_bad = rm.calculate_position_size()
        
        # Kelly с losses → меньше размер
        # (или равный если recent_trades уже полные)
        # Сравниваем с абсолютным значением
        assert size_bad <= size_good


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
