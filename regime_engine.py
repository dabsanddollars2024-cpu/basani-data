"""
Market Regime Engine
Calculates market bias using multiple technical indicators.
Score 0-100: >75=BULLISH, <25=BEARISH, 25-75=NEUTRAL
"""

import os
import sys
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
import json


class RegimeEngine:
    """
    Market Regime Score Calculator
    
    Components:
    - SPY + QQQ Technical (30 pts max)
    - Momentum (25 pts max)
    - VIX Regime (20 pts max)
    - Breadth (25 pts max)
    Total: 100 pts max
    """
    
    def __init__(self):
        self.spy_above_20dma = 0
        self.spy_above_50dma = 0
        self.spy_above_200dma = 0
        self.spy_roc_5d = 0
        self.spy_roc_20d = 0
        self.qqq_vs_spy = 0
        self.vix_level = 0
        self.vix_score = 0
        self.breadth_score = 0
        self.total_score = 0
        self.regime_bias = "NEUTRAL"
        self.breakdown = {}
        
    def calculate_regime(self, market_data: Dict) -> Dict:
        """
        Calculate regime score from market data.
        
        Expected market_data structure:
        {
            'spy': {'prices': [...], 'ma20': float, 'ma50': float, 'ma200': float, 'roc_5d': float, 'roc_20d': float},
            'qqq': {'prices': [...], 'ma20': float, 'ma50': float, 'ma200': float},
            'vix': float,
            'ad_line_at_highs': bool
        }
        """
        self.breakdown = {
            'timestamp': datetime.now().isoformat(),
            'components': {},
            'bias': None,
            'score': 0
        }
        
        spy = market_data.get('spy', {})
        qqq = market_data.get('qqq', {})
        self.vix_level = market_data.get('vix', 20)
        ad_at_highs = market_data.get('ad_line_at_highs', False)
        
        # SPY Technical (30 pts)
        tech_score = 0
        prices = spy.get('prices', [])
        if len(prices) >= 2:
            current_price = prices[-1]
            ma20 = spy.get('ma20', 0)
            ma50 = spy.get('ma50', 0)
            ma200 = spy.get('ma200', 0)
            
            if ma20 > 0 and current_price > ma20:
                tech_score += 10
                self.breakdown['components']['spy_above_20dma'] = {'score': 10, 'value': True}
            else:
                self.breakdown['components']['spy_above_20dma'] = {'score': 0, 'value': False}
                
            if ma50 > 0 and current_price > ma50:
                tech_score += 10
                self.breakdown['components']['spy_above_50dma'] = {'score': 10, 'value': True}
            else:
                self.breakdown['components']['spy_above_50dma'] = {'score': 0, 'value': False}
                
            if ma200 > 0 and current_price > ma200:
                tech_score += 10
                self.breakdown['components']['spy_above_200dma'] = {'score': 10, 'value': True}
            else:
                self.breakdown['components']['spy_above_200dma'] = {'score': 0, 'value': False}
        else:
            self.breakdown['components']['spy_technical'] = {'score': 0, 'error': 'Insufficient price data'}
            
        self.breakdown['components']['spy_technical'] = {'score': tech_score, 'max': 30}
        
        # Momentum (25 pts)
        momentum_score = 0
        self.spy_roc_5d = spy.get('roc_5d', 0)
        self.spy_roc_20d = spy.get('roc_20d', 0)
        
        if self.spy_roc_5d > 1:
            momentum_score += 12
            self.breakdown['components']['spy_roc_5d'] = {'score': 12, 'value': self.spy_roc_5d}
        else:
            self.breakdown['components']['spy_roc_5d'] = {'score': 0, 'value': self.spy_roc_5d}
            
        if self.spy_roc_20d > 3:
            momentum_score += 13
            self.breakdown['components']['spy_roc_20d'] = {'score': 13, 'value': self.spy_roc_20d}
        else:
            self.breakdown['components']['spy_roc_20d'] = {'score': 0, 'value': self.spy_roc_20d}
        
        # QQQ vs SPY relative strength
        qqq_prices = qqq.get('prices', [])
        if len(prices) >= 20 and len(qqq_prices) >= 20:
            spy_20d_return = (prices[-1] / prices[-20] - 1) * 100 if prices[-20] > 0 else 0
            qqq_20d_return = (qqq_prices[-1] / qqq_prices[-20] - 1) * 100 if qqq_prices[-20] > 0 else 0
            
            if qqq_20d_return > spy_20d_return:
                momentum_score += 10  # Tech leading
                self.breakdown['components']['qqq_vs_spy'] = {'score': 10, 'value': 'QQQ_LEADING'}
            else:
                momentum_score += 5  # Broad market
                self.breakdown['components']['qqq_vs_spy'] = {'score': 5, 'value': 'SPY_LEADING'}
        else:
            self.breakdown['components']['qqq_vs_spy'] = {'score': 0, 'error': 'Insufficient data'}
            
        self.breakdown['components']['momentum'] = {'score': momentum_score, 'max': 25}
        
        # VIX Regime (20 pts)
        vix_score = 0
        if self.vix_level < 15:
            vix_score = 20
        elif self.vix_level <= 25:
            vix_score = 10
        else:
            vix_score = 0
            
        self.breakdown['components']['vix'] = {'score': vix_score, 'max': 20, 'value': self.vix_level}
        
        # Breadth (25 pts)
        breadth_score = 0
        if tech_score == 30:  # All 3 SPY MAs above
            breadth_score += 15
            self.breakdown['components']['all_spy_mas_above'] = {'score': 15, 'value': True}
        else:
            self.breakdown['components']['all_spy_mas_above'] = {'score': 0, 'value': False}
            
        if ad_at_highs:
            breadth_score += 10
            self.breakdown['components']['ad_at_highs'] = {'score': 10, 'value': True}
        else:
            self.breakdown['components']['ad_at_highs'] = {'score': 0, 'value': False}
            
        self.breakdown['components']['breadth'] = {'score': breadth_score, 'max': 25}
        
        # Total Score
        self.total_score = tech_score + momentum_score + vix_score + breadth_score
        self.breakdown['score'] = self.total_score
        
        # Determine Bias
        if self.total_score >= 75:
            self.regime_bias = "BULLISH BIAS"
        elif self.total_score <= 25:
            self.regime_bias = "BEARISH BIAS"
        else:
            self.regime_bias = "NEUTRAL"
            
        self.breakdown['bias'] = self.regime_bias
        
        return {
            'score': self.total_score,
            'bias': self.regime_bias,
            'breakdown': self.breakdown
        }
    
    def print_report(self, results: Dict = None):
        """Print formatted regime report."""
        if results is None:
            results = {
                'score': self.total_score,
                'bias': self.regime_bias,
                'breakdown': self.breakdown
            }
            
        print("\n" + "="*60)
        print("MARKET REGIME ANALYSIS")
        print("="*60)
        print(f"\nOVERALL SCORE: {results['score']}/100")
        print(f"BIAS: {results['bias']}")
        print("\n" + "-"*40)
        print("COMPONENT BREAKDOWN:")
        print("-"*40)
        
        breakdown = results.get('breakdown', {})
        components = breakdown.get('components', {})
        
        for comp_name, comp_data in components.items():
            if isinstance(comp_data, dict):
                if 'max' in comp_data:
                    print(f"  {comp_name.upper()}: {comp_data['score']}/{comp_data['max']}")
                elif 'score' in comp_data and 'value' in comp_data:
                    print(f"  {comp_name.upper()}: {comp_data['score']} pts (value: {comp_data['value']})")
                elif 'value' in comp_data:
                    print(f"  {comp_name.upper()}: {comp_data['score']} pts (value: {comp_data['value']})")
        
        print("\n" + "="*60)
        print(f"Generated: {breakdown.get('timestamp', 'N/A')}")
        print("="*60 + "\n")


def calculate_simple_regime(spy_prices: list, qqq_prices: list, vix: float, 
                           spy_ma20: float, spy_ma50: float, spy_ma200: float,
                           ad_at_highs: bool = False) -> Dict:
    """
    Simple function to calculate regime score given price data.
    """
    if len(spy_prices) < 200 or len(qqq_prices) < 200:
        return {'error': 'Need at least 200 days of price data'}
    
    engine = RegimeEngine()
    
    current_spy = spy_prices[-1]
    current_qqq = qqq_prices[-1]
    
    # Calculate ROC
    spy_roc_5d = ((current_spy / spy_prices[-6] - 1) * 100) if len(spy_prices) >= 6 else 0
    spy_roc_20d = ((current_spy / spy_prices[-21] - 1) * 100) if len(spy_prices) >= 21 else 0
    
    market_data = {
        'spy': {
            'prices': spy_prices,
            'ma20': spy_ma20,
            'ma50': spy_ma50,
            'ma200': spy_ma200,
            'roc_5d': spy_roc_5d,
            'roc_20d': spy_roc_20d
        },
        'qqq': {
            'prices': qqq_prices,
        },
        'vix': vix,
        'ad_line_at_highs': ad_at_highs
    }
    
    results = engine.calculate_regime(market_data)
    return results


if __name__ == "__main__":
    # Test with sample data structure
    print("Market Regime Engine v1.0")
    print("Import this module and use calculate_simple_regime() or RegimeEngine class")
