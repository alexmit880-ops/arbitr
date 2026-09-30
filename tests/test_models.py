"""Тесты моделей данных."""
import time
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Ticker, Opportunity, Portfolio, TradeResult


class TestTicker:
    """Тесты класса Ticker."""
    
    def test_from_ccxt_valid_data(self):
        """Корректные данные → Ticker создаётся."""
        data = {
            "bid": 100.0,
            "ask": 100.5,
            "last": 100.2,
            "quoteVolume": 1000000,
            "timestamp": 1700000000000,
            "percentage": 5.5,
        }
        ticker = Ticker.from_ccxt("binance", "BTC/USDT", data)
        
        assert ticker is not None
        assert ticker.bid == 100.0
        assert ticker.ask == 100.5
        assert ticker.last == 100.2
        assert ticker.volume == 1000000
        assert ticker.percentage == 5.5
        assert ticker.timestamp == 1700000000000
    
    def test_from_ccxt_zero_bid_returns_none(self):
        """bid = 0 → None (невалидный тикер)."""
        data = {"bid": 0, "ask": 100, "last": 100}
        assert Ticker.from_ccxt("binance", "BTC/USDT", data) is None
    
    def test_from_ccxt_zero_ask_returns_none(self):
        """ask = 0 → None."""
        data = {"bid": 100, "ask": 0, "last": 100}
        assert Ticker.from_ccxt("binance", "BTC/USDT", data) is None
    
    def test_from_ccxt_zero_last_returns_none(self):
        """last = 0 → None."""
        data = {"bid": 100, "ask": 100.5, "last": 0}
        assert Ticker.from_ccxt("binance", "BTC/USDT", data) is None
    
    def test_from_ccxt_none_values_returns_none(self):
        """None значения → None."""
        data = {"bid": None, "ask": None, "last": None}
        assert Ticker.from_ccxt("binance", "BTC/USDT", data) is None
    
    def test_from_ccxt_missing_keys_returns_none(self):
        """Отсутствующие ключи → None."""
        data = {}
        assert Ticker.from_ccxt("binance", "BTC/USDT", data) is None
    
    def test_from_ccxt_string_values_handled(self):
        """Строковые значения корректно парсятся."""
        data = {"bid": "100.5", "ask": "101.0", "last": "100.8"}
        ticker = Ticker.from_ccxt("bybit", "ETH/USDT", data)
        assert ticker is not None
        assert ticker.bid == 100.5
    
    def test_from_ccxt_exception_returns_none(self):
        """Исключение при парсинге → None."""
        data = {"bid": "invalid", "ask": "data"}  # ValueError при float()
        assert Ticker.from_ccxt("gate", "SOL/USDT", data) is None
    
    def test_is_stale_fresh(self):
        """Свежий тикер → not stale."""
        data = {"bid": 100, "ask": 101, "last": 100.5, "timestamp": int(time.time() * 1000)}
        ticker = Ticker.from_ccxt("mexc", "XRP/USDT", data)
        assert not ticker.is_stale()
    
    def test_is_stale_old(self):
        """Старый тикер → stale."""
        data = {"bid": 100, "ask": 101, "last": 100.5, "timestamp": int(time.time() * 1000) - 60000}
        ticker = Ticker.from_ccxt("mexc", "XRP/USDT", data)
        assert ticker.is_stale()
    
    def test_is_stale_zero_timestamp(self):
        """timestamp = 0 → stale."""
        ticker = Ticker(exchange="test", symbol="X/USDT", bid=1, ask=1.1, last=1.05, 
                       volume=1000, timestamp=0)
        assert ticker.is_stale()
    
    def test_bid_ask_spread_pct_normal(self):
        """bid=100, ask=101 → spread = 1%."""
        ticker = Ticker(exchange="t", symbol="X", bid=100, ask=101, last=100.5,
                       volume=1000, timestamp=int(time.time() * 1000))
        assert ticker.bid_ask_spread_pct() == pytest.approx(1.0, 0.01)
    
    def test_bid_ask_spread_pct_zero_bid(self):
        """bid = 0 → spread = 999 (sentinel)."""
        ticker = Ticker(exchange="t", symbol="X", bid=0, ask=100, last=50,
                       volume=1000, timestamp=int(time.time() * 1000))
        assert ticker.bid_ask_spread_pct() == 999.0


class TestOpportunity:
    """Тесты класса Opportunity."""
    
    def test_create_opportunity(self):
        opp = Opportunity(
            symbol="BTC/USDT",
            buy_exchange="binance",
            sell_exchange="bybit",
            buy_price=50000,
            sell_price=50100,
            spread_pct=0.2,
            net_profit_pct=0.15,
            volume=1000000,
            exchanges_count=2,
            timestamp=int(time.time()),
        )
        assert opp.symbol == "BTC/USDT"
        assert opp.buy_price == 50000
        assert opp.sell_price == 50100
        assert opp.confidence_score == 0.0  # default
        assert opp.max_safe_size_usdt == 0.0  # default
    
    def test_opportunity_with_all_fields(self):
        """Opportunity со всеми полями."""
        opp = Opportunity(
            symbol="ETH/USDT", buy_exchange="a", sell_exchange="b",
            buy_price=3000, sell_price=3050, spread_pct=1.67, net_profit_pct=1.2,
            volume=500000, exchanges_count=3, timestamp=12345,
            confidence_score=85.5,
            confidence_breakdown={"spread": 25, "volume": 20},
            category="defi",
            fee_pct=0.2, slippage_pct=0.5, withdrawal_pct=0.4, drift_pct=0.1,
            buy_ticker_age=0.5, sell_ticker_age=1.2,
            max_safe_size_usdt=850.0, execution_quality=75,
            first_seen=12340.0,
        )
        assert opp.confidence_score == 85.5
        assert opp.category == "defi"
        assert opp.max_safe_size_usdt == 850.0
        assert opp.execution_quality == 75


class TestPortfolio:
    """Тесты класса Portfolio."""
    
    def test_initial_state(self):
        p = Portfolio(balance=10000, initial_balance=10000)
        assert p.balance == 10000
        assert p.initial_balance == 10000
        assert p.peak_equity == 10000
        assert p.drawdown_pct == 0.0
        assert p.pnl_pct == 0.0
    
    def test_drawdown_calculation(self):
        """После убытка — правильный drawdown."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.balance = 9000  # -10%
        p.peak_equity = 10000  # peak не обновляется автоматически
        assert p.drawdown_pct == pytest.approx(10.0, 0.01)
    
    def test_peak_update(self):
        """Peak обновляется когда balance растёт."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.balance = 11000
        p.update_peak()
        assert p.peak_equity == 11000
        assert p.drawdown_pct == 0.0
    
    def test_pnl_pct(self):
        """P&L percentage."""
        p = Portfolio(balance=10500, initial_balance=10000)
        assert p.pnl_pct == pytest.approx(5.0, 0.01)
    
    def test_rolling_win_rate_empty(self):
        """Без сделок → 50% (нейтральный)."""
        p = Portfolio(balance=10000, initial_balance=10000)
        assert p.rolling_win_rate == 0.5
    
    def test_rolling_win_rate_all_wins(self):
        """Все выигрыши → 100%."""
        p = Portfolio(balance=10000, initial_balance=10000)
        for i in range(5):
            t = TradeResult(timestamp=i, symbol="X/USDT", buy_exchange="a",
                           sell_exchange="b", amount=1.0, pnl=10, pnl_pct=2,
                           balance_after=10000, success=True)
            p.recent_trades.append(t)
        assert p.rolling_win_rate == 1.0
    
    def test_rolling_win_rate_mixed(self):
        """3 из 5 выигрышей → 60%."""
        p = Portfolio(balance=10000, initial_balance=10000)
        pnls = [10, -5, 10, 10, -5]
        for i, pnl in enumerate(pnls):
            t = TradeResult(timestamp=i, symbol="X/USDT", buy_exchange="a",
                           sell_exchange="b", amount=1.0, pnl=pnl, pnl_pct=2,
                           balance_after=10000, success=pnl > 0)
            p.recent_trades.append(t)
        assert p.rolling_win_rate == pytest.approx(0.6, 0.01)
    
    def test_check_daily_reset_first_day(self):
        """В первый день daily_pnl не сбрасывается."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.daily_pnl = 500
        p.daily_trades = 10
        original_day_start = p.day_start_utc
        p.check_daily_reset()
        assert p.daily_pnl == 500  # не сбросился
        assert p.daily_trades == 10
    
    def test_check_daily_reset_new_day(self):
        """На следующий день daily_pnl сбрасывается."""
        p = Portfolio(balance=10000, initial_balance=10000)
        p.daily_pnl = 500
        p.daily_trades = 10
        # Симулируем что прошло > 24 часов
        p.day_start_utc = int(time.time()) - 90000
        p.check_daily_reset()
        assert p.daily_pnl == 0.0
        assert p.daily_trades == 0


class TestTradeResult:
    """Тесты класса TradeResult."""
    
    def test_create_trade(self):
        t = TradeResult(
            timestamp=12345, symbol="BTC/USDT",
            buy_exchange="binance", sell_exchange="bybit",
            amount=0.01, pnl=5.0, pnl_pct=1.0,
            balance_after=10005, success=True,
        )
        assert t.pnl == 5.0
        assert t.success is True
    
    def test_losing_trade(self):
        t = TradeResult(
            timestamp=12345, symbol="BTC/USDT",
            buy_exchange="binance", sell_exchange="bybit",
            amount=0.01, pnl=-3.0, pnl_pct=-0.6,
            balance_after=9997, success=False,
        )
        assert t.pnl < 0
        assert t.success is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
