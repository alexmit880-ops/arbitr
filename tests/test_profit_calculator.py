"""Тесты ProfitCalculator — обновлённые под новый API."""
import time
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Ticker, ProfitCalculator, ExecutionAnalyzer
from config import (
    BASE_SLIPPAGE, SLIPPAGE_PER_USDT, MIN_TRADE_SIZE,
    TRADING_FEES, WITHDRAWAL_FEES
)


def make_ticker(exchange="binance", bid=100, ask=101, last=100.5, vol=1000000, pct=5.0):
    return Ticker(
        exchange=exchange, symbol="X/USDT",
        bid=bid, ask=ask, last=last, volume=vol,
        timestamp=int(time.time() * 1000),
        percentage=pct,
    )


class TestCalculateSlippage:
    def test_slippage_base(self):
        assert ProfitCalculator.calculate_slippage(0) == pytest.approx(BASE_SLIPPAGE, 0.0001)
    
    def test_slippage_100_usdt(self):
        expected = BASE_SLIPPAGE + SLIPPAGE_PER_USDT * 100
        assert ProfitCalculator.calculate_slippage(100) == pytest.approx(expected, 0.0001)
    
    def test_slippage_1000_usdt(self):
        expected = BASE_SLIPPAGE + SLIPPAGE_PER_USDT * 1000
        assert ProfitCalculator.calculate_slippage(1000) == pytest.approx(expected, 0.0001)
    
    def test_slippage_linear(self):
        diff = ProfitCalculator.calculate_slippage(200) - ProfitCalculator.calculate_slippage(100)
        assert diff == pytest.approx(SLIPPAGE_PER_USDT * 100, 0.0001)


class TestCalculateNetProfit:
    def test_simple_profitable_trade(self):
        """Простая прибыльная сделка."""
        buy = make_ticker("binance", bid=100, ask=100.1, last=100.05)
        sell = make_ticker("bybit", bid=105, ask=105.1, last=105.05)
        net = ProfitCalculator.calculate_net_profit(5.0, buy, sell, 500)
        assert net > 0
        assert net < 5.0  # после расходов
    
    def test_zero_spread_negative(self):
        """spread = 0 → net < 0."""
        buy = make_ticker("binance")
        sell = make_ticker("bybit")
        net = ProfitCalculator.calculate_net_profit(0.0, buy, sell, 500)
        assert net < 0
    
    def test_high_fees_lower_profit(self):
        """HTX дороже → меньше net profit."""
        buy_binance = make_ticker("binance")
        sell_mexc = make_ticker("mexc")
        buy_htx = make_ticker("htx")
        sell_mexc2 = make_ticker("mexc")
        
        net_low = ProfitCalculator.calculate_net_profit(3.0, buy_binance, sell_mexc, 500)
        net_high = ProfitCalculator.calculate_net_profit(3.0, buy_htx, sell_mexc2, 500)
        assert net_high < net_low
    
    def test_size_affects_withdrawal(self):
        """Меньший ордер → withdrawal в % больше."""
        buy = make_ticker("binance")
        sell = make_ticker("bybit")
        net_small = ProfitCalculator.calculate_net_profit(3.0, buy, sell, 100)
        net_large = ProfitCalculator.calculate_net_profit(3.0, buy, sell, 1000)
        assert net_large > net_small
    
    def test_volatility_increases_drift(self):
        """Высокая volatility → больше drift → меньше net."""
        buy_low = make_ticker("binance", pct=2.0)
        sell_low = make_ticker("bybit", pct=2.0)
        buy_high = make_ticker("binance", pct=20.0)
        sell_high = make_ticker("bybit", pct=20.0)
        
        net_low = ProfitCalculator.calculate_net_profit(5.0, buy_low, sell_low, 500)
        net_high = ProfitCalculator.calculate_net_profit(5.0, buy_high, sell_high, 500)
        assert net_high < net_low


class TestExecutionAnalyzer:
    def test_max_safe_size_basic(self):
        """Базовый расчёт max safe size."""
        buy = make_ticker("binance", bid=100, ask=100.1)
        sell = make_ticker("bybit", bid=100, ask=100.1)
        max_safe, quality = ExecutionAnalyzer.calculate_max_safe_size(buy, sell, 1000000)
        assert max_safe > 0
        assert quality > 0
        assert quality <= 100
    
    def test_max_safe_size_zero_volume_fallback(self):
        """✅ FIX: volume=0 → fallback на MIN_TRADE_SIZE."""
        buy = make_ticker("binance")
        sell = make_ticker("bybit")
        max_safe, quality = ExecutionAnalyzer.calculate_max_safe_size(buy, sell, 0)
        assert max_safe >= MIN_TRADE_SIZE  # fallback работает
        assert max_safe > 0
    
    def test_quality_increases_with_size(self):
        buy = make_ticker("binance")
        sell = make_ticker("bybit")
        small_safe, small_quality = ExecutionAnalyzer.calculate_max_safe_size(buy, sell, 1000)
        large_safe, large_quality = ExecutionAnalyzer.calculate_max_safe_size(buy, sell, 10000000)
        assert large_quality > small_quality


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
