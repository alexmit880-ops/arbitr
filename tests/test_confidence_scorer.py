"""Тесты Confidence Scorer."""
import time
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (
    Ticker, Opportunity, ConfidenceScorer,
    ExchangePairRanker, LifetimeTracker
)


def make_ticker(exchange="binance", bid=100, ask=101, last=100.5, volume=1000000, pct=5.0):
    return Ticker(
        exchange=exchange, symbol="X/USDT",
        bid=bid, ask=ask, last=last, volume=volume,
        timestamp=int(time.time() * 1000),
        percentage=pct,
    )


class TestConfidenceScorer:
    
    def test_high_spread_high_score(self):
        """Высокий spread → высокий score."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        buy = make_ticker("binance")
        sell = make_ticker("bybit")
        opp = Opportunity(
            symbol="X/USDT", buy_exchange="binance", sell_exchange="bybit",
            buy_price=100, sell_price=105, spread_pct=5.0,
            net_profit_pct=3.0, volume=1000000, exchanges_count=2,
            timestamp=int(time.time()),
        )
        score, breakdown, category = scorer.score(opp, buy, sell)
        
        assert score > 50
        assert "spread" in breakdown
    
    def test_low_spread_low_score(self):
        """Низкий spread → score ниже чем у высокого spread."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        # Низкий spread
        buy = make_ticker("binance")
        sell = make_ticker("bybit")
        opp_low = Opportunity(
            symbol="X/USDT", buy_exchange="binance", sell_exchange="bybit",
            buy_price=100, sell_price=100.3, spread_pct=0.3,
            net_profit_pct=-0.2, volume=100000, exchanges_count=2,
            timestamp=int(time.time()),
        )
        score_low, breakdown_low, _ = scorer.score(opp_low, buy, sell)
        
        # Высокий spread
        opp_high = Opportunity(
            symbol="Y/USDT", buy_exchange="binance", sell_exchange="bybit",
            buy_price=100, sell_price=105, spread_pct=5.0,
            net_profit_pct=3.0, volume=100000, exchanges_count=2,
            timestamp=int(time.time()),
        )
        score_high, breakdown_high, _ = scorer.score(opp_high, buy, sell)
        
        # ✅ Низкий spread → score должен быть ниже
        assert score_low < score_high, (
            f"Low spread score ({score_low}) should be < high spread score ({score_high})"
        )
        
        # ✅ Spread компонент у низкого должен быть маленьким
        assert breakdown_low["spread"] < breakdown_high["spread"]
        assert breakdown_low["spread"] < 10  # 0.3% * 20 = 6
    
    def test_zero_spread_minimal_score(self):
        """Spread = 0 → spread компонент = 0."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        buy = make_ticker("binance")
        sell = make_ticker("bybit")
        opp = Opportunity(
            symbol="X/USDT", buy_exchange="binance", sell_exchange="bybit",
            buy_price=100, sell_price=100, spread_pct=0,
            net_profit_pct=-2, volume=100000, exchanges_count=2,
            timestamp=int(time.time()),
        )
        score, breakdown, _ = scorer.score(opp, buy, sell)
        assert breakdown["spread"] == 0
        # Score может быть > 0 за счёт других компонентов
        assert score >= 0

    
    def test_max_safe_size_set_on_opp(self):
        """max_safe_size_usdt устанавливается на opportunity."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        buy = make_ticker()
        sell = make_ticker()
        opp = Opportunity(
            symbol="X/USDT", buy_exchange="binance", sell_exchange="bybit",
            buy_price=100, sell_price=102, spread_pct=2.0,
            net_profit_pct=1.0, volume=100000, exchanges_count=2,
            timestamp=int(time.time()),
        )
        scorer.score(opp, buy, sell)
        assert opp.max_safe_size_usdt > 0
    
    def test_execution_quality_set_on_opp(self):
        """execution_quality устанавливается."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        buy = make_ticker()
        sell = make_ticker()
        opp = Opportunity(
            symbol="X/USDT", buy_exchange="binance", sell_exchange="bybit",
            buy_price=100, sell_price=102, spread_pct=2.0,
            net_profit_pct=1.0, volume=100000, exchanges_count=2,
            timestamp=int(time.time()),
        )
        scorer.score(opp, buy, sell)
        assert 0 <= opp.execution_quality <= 100
    
    def test_category_detection_meme(self):
        """PEPE → meme category."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        assert scorer.get_category("PEPE/USDT") == "meme"
        assert scorer.get_category("DOGE/USDT") == "meme"
    
    def test_category_detection_defi(self):
        """UNI → defi category."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        assert scorer.get_category("UNI/USDT") == "defi"
        assert scorer.get_category("AAVE/USDT") == "defi"
    
    def test_category_detection_unknown(self):
        """Unknown → other category."""
        ranker = ExchangePairRanker()
        tracker = LifetimeTracker()
        scorer = ConfidenceScorer(ranker, tracker)
        
        assert scorer.get_category("RANDOM/USDT") == "other"


class TestExchangePairRanker:
    
    def test_new_pair_neutral_score(self):
        """Новая пара → 50 (нейтральный)."""
        ranker = ExchangePairRanker()
        score = ranker.get_pair_score("new_a", "new_b")
        assert score == 50.0
    
    def test_frequent_pair_high_score(self):
        """Частая пара → высокий score."""
        ranker = ExchangePairRanker()
        opp = Opportunity(
            symbol="X/USDT", buy_exchange="a", sell_exchange="b",
            buy_price=100, sell_price=102, spread_pct=2.0,
            net_profit_pct=1.0, volume=100000, exchanges_count=2,
            timestamp=int(time.time()),
        )
        for _ in range(10):
            ranker.record(opp, executed=True)
        
        score = ranker.get_pair_score("a", "b")
        assert score > 50
    
    def test_top_pairs_sorting(self):
        """Top pairs сортируются по count."""
        ranker = ExchangePairRanker()
        
        opp1 = Opportunity(symbol="A/USDT", buy_exchange="a", sell_exchange="b",
                          buy_price=100, sell_price=102, spread_pct=2.0,
                          net_profit_pct=1.0, volume=100000, exchanges_count=2,
                          timestamp=int(time.time()))
        opp2 = Opportunity(symbol="B/USDT", buy_exchange="c", sell_exchange="d",
                          buy_price=100, sell_price=102, spread_pct=2.0,
                          net_profit_pct=1.0, volume=100000, exchanges_count=2,
                          timestamp=int(time.time()))
        
        # 5 записей для пары a->b, 20 для c->d
        for _ in range(5):
            ranker.record(opp1)
        for _ in range(20):
            ranker.record(opp2)
        
        top = ranker.get_top_pairs(5)
        assert top[0][0] == "c->d"  # больше записей
        assert top[1][0] == "a->b"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
