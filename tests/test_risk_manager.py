"""Тесты Risk Manager."""
import time
import asyncio
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Portfolio, TradeResult, Opportunity, RiskManager
from config import (
    MAX_DAILY_LOSS_PERCENT, MAX_CONSECUTIVE_LOSSES,
    MAX_OPEN_POSITIONS, MAX_CORRELATED_POSITIONS,
    POSITION_COOLDOWN_SEC, MIN_BALANCE_REQUIRED,
    KELLY_MIN_TRADES, MIN_TRADE_SIZE, MAX_TRADE_SIZE,
    MAX_DRAWDOWN_PERCENT
)


def make_opp(conf=70, net=2.0, category="meme", symbol="X/USDT"):
    return Opportunity(
        symbol=symbol, buy_exchange="a", sell_exchange="b",
        buy_price=100, sell_price=102, spread_pct=2.0,
        net_profit_pct=net, volume=100000, exchanges_count=2,
        timestamp=int(time.time()),
        confidence_score=conf, category=category,
    )


class TestCanTrade:
    """Тесты проверки возможности торговли."""
    
    def test_profitable_high_confidence_allowed(self):
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        opp = make_opp(conf=70, net=2.0)
        can, reason = rm.can_trade(opp)
        assert can is True
    
    def test_low_confidence_blocked(self):
        """Confidence < 50 → blocked."""
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        opp = make_opp(conf=30)
        can, reason = rm.can_trade(opp)
        assert can is False
        assert "conf" in reason.lower()
    
    def test_negative_profit_blocked(self):
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        opp = make_opp(net=-1.0)
        can, reason = rm.can_trade(opp)
        assert can is False
        assert "negative" in reason.lower()
    
    def test_daily_loss_limit(self):
        """Daily loss > limit → blocked."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.daily_pnl = -1500  # -15% > 10% limit
        rm = RiskManager(p)
        opp = make_opp(conf=70)
        can, reason = rm.can_trade(opp)
        assert can is False
        assert "loss" in reason.lower()
    
    def test_max_drawdown_blocked(self):
        """Drawdown > 15% → blocked."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.peak_equity = 12000
        p.balance = 10000  # -16.67% drawdown
        rm = RiskManager(p)
        opp = make_opp(conf=70)
        can, reason = rm.can_trade(opp)
        assert can is False
    
    def test_consecutive_losses_blocked(self):
        """MAX_CONSECUTIVE_LOSSES losses подряд → blocked."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.consecutive_losses = MAX_CONSECUTIVE_LOSSES
        rm = RiskManager(p)
        opp = make_opp(conf=70)
        can, reason = rm.can_trade(opp)
        assert can is False
    
    def test_max_positions_blocked(self):
        """MAX_OPEN_POSITIONS открыто → blocked."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.positions = {f"SYM{i}": {} for i in range(MAX_OPEN_POSITIONS)}
        rm = RiskManager(p)
        opp = make_opp(conf=70, symbol="NEW/USDT")
        can, reason = rm.can_trade(opp)
        assert can is False
    
    def test_correlation_limit(self):
        """Слишком много мем-токенов → blocked."""
        p = Portfolio(balance=10000, initial_balance=10000)
        # Уже MAX_CORRELATED_POSITIONS мем-токенов
        for i in range(MAX_CORRELATED_POSITIONS):
            p.positions[f"MEME{i}/USDT"] = {"category": "meme"}
        rm = RiskManager(p)
        opp = make_opp(conf=70, category="meme", symbol="NEW_MEME/USDT")
        can, reason = rm.can_trade(opp)
        assert can is False
        assert "meme" in reason.lower()
    
    def test_position_cooldown(self):
        """Торговля по тому же символу в cooldown → blocked."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.last_trade_time["X"] = time.time()  # только что торговали
        rm = RiskManager(p)
        opp = make_opp(conf=70, symbol="X/USDT")
        can, reason = rm.can_trade(opp)
        assert can is False
        assert "cooldown" in reason.lower()
    
    def test_low_balance_blocked(self):
        """Баланс < MIN_BALANCE_REQUIRED → blocked."""
        p = Portfolio(balance=50, initial_balance=10000)
        rm = RiskManager(p)
        opp = make_opp(conf=70)
        can, reason = rm.can_trade(opp)
        assert can is False


class TestRecordTrade:
    """Тесты записи сделки."""
    
    def test_win_updates_balance(self):
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        trade = TradeResult(
            timestamp=int(time.time()), symbol="X/USDT",
            buy_exchange="a", sell_exchange="b", amount=1.0,
            pnl=50, pnl_pct=0.5, balance_after=10050, success=True,
        )
        rm.record_trade(trade)
        assert p.balance == 10050
        assert p.daily_pnl == 50
        assert p.consecutive_losses == 0
    
    def test_loss_updates_consecutive(self):
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        trade = TradeResult(
            timestamp=int(time.time()), symbol="X/USDT",
            buy_exchange="a", sell_exchange="b", amount=1.0,
            pnl=-20, pnl_pct=-0.2, balance_after=9980, success=False,
        )
        rm.record_trade(trade)
        assert p.balance == 9980
        assert p.consecutive_losses == 1
    
    def test_win_after_loss_resets_streak(self):
        """Выигрыш после проигрыша сбрасывает streak."""
        p = Portfolio(balance=10000, initial_balance=10000)
        rm = RiskManager(p)
        
        # 3 losses
        for i in range(3):
            rm.record_trade(TradeResult(
                timestamp=i, symbol="X/USDT", buy_exchange="a", sell_exchange="b",
                amount=1, pnl=-10, pnl_pct=-0.1, balance_after=9990-i*10, success=False,
            ))
        assert p.consecutive_losses == 3
        
        # Win
        rm.record_trade(TradeResult(
            timestamp=10, symbol="Y/USDT", buy_exchange="a", sell_exchange="b",
            amount=1, pnl=10, pnl_pct=0.1, balance_after=9990, success=True,
        ))
        assert p.consecutive_losses == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
