"""Unit-тесты для OrderValidator и причин блокировки ордеров."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import OrderValidator


class FakeClient:
    def __init__(self, markets=None):
        self.markets = markets or {}


class FakePool:
    def __init__(self, clients=None):
        self.clients = clients or {}


def make_pool(exchange="binance", market=None):
    return FakePool(
        {
            exchange: FakeClient(
                {"RE/USDT": market} if market is not None else {}
            )
        }
    )


class TestOrderValidator:
    def test_no_client(self):
        pool = FakePool({})
        result = OrderValidator.check(pool, "binance", "RE/USDT", 100.0, 1.0)
        assert result.valid is False
        assert result.reason == "no_client_binance"

    def test_symbol_not_found(self):
        pool = make_pool(market=None)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 100.0, 1.0)
        assert result.valid is False
        assert result.reason == "symbol_not_found"

    def test_market_inactive_when_minimums_not_met(self):
        market = {
            "active": False,
            "limits": {
                "cost": {"min": 100.0},
                "amount": {"min": 50.0},
            },
        }
        pool = make_pool(market=market)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 10.0, 1.0)
        assert result.valid is False
        assert result.reason == "market_inactive"

    def test_inactive_market_allowed_when_minimums_are_met(self):
        market = {
            "active": False,
            "limits": {
                "cost": {"min": 10.0},
                "amount": {"min": 5.0},
            },
        }
        pool = make_pool(market=market)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 100.0, 10.0)
        assert result.valid is True
        assert result.reason == "ok"

    def test_below_min_notional(self):
        market = {
            "active": True,
            "limits": {
                "cost": {"min": 200.0},
            },
        }
        pool = make_pool(market=market)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 100.0, 2.0)
        assert result.valid is False
        assert result.reason == "below_min_notional"
        assert result.min_notional == 200.0

    def test_below_min_qty(self):
        market = {
            "active": True,
            "limits": {
                "amount": {"min": 10.0, "step": 0.1},
            },
        }
        pool = make_pool(market=market)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 20.0, 5.0)
        assert result.valid is False
        assert result.reason == "below_min_qty"
        assert result.min_qty == 10.0

    def test_step_too_coarse(self):
        market = {
            "active": True,
            "limits": {
                "amount": {"min": 0.1, "step": 1.0},
            },
        }
        pool = make_pool(market=market)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 100.0, 100.0)
        assert result.valid is False
        assert result.reason == "step_too_coarse"
        assert result.step_size == 1.0

    def test_below_min_price(self):
        market = {
            "active": True,
            "limits": {
                "price": {"min": 5.0},
            },
        }
        pool = make_pool(market=market)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 100.0, 4.0)
        assert result.valid is False
        assert result.reason == "below_min_price"

    def test_ok_when_all_constraints_pass(self):
        market = {
            "active": True,
            "limits": {
                "cost": {"min": 10.0},
                "amount": {"min": 1.0, "step": 0.01},
                "price": {"min": 1.0},
            },
        }
        pool = make_pool(market=market)
        result = OrderValidator.check(pool, "binance", "RE/USDT", 100.0, 10.0)
        assert result.valid is True
        assert result.reason == "ok"
        assert result.min_notional == 10.0
        assert result.min_qty == 1.0
        assert result.step_size == 0.01


if __name__ == "__main__":
    pytest.main([__file__, "-v"])