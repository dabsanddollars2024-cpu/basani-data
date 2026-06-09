"""
Test script for Regime Engine
Tests with simulated market data since no API key is available
"""

import sys
sys.path.insert(0, r'C:\Users\dabsa\Desktop\QuantLab\Basani')

from regime_engine import RegimeEngine

def test_regime_engine():
    """Test regime engine with simulated data"""
    print("="*60)
    print("TESTING REGIME ENGINE")
    print("="*60)
    
    # Simulated market data (realistic values)
    # SPY: Slightly bullish scenario
    spy_prices = [400 + i * 0.5 for i in range(200)]  # Upward trend
    qqq_prices = [300 + i * 0.6 for i in range(200)]  # Tech slightly leading
    
    current_spy = spy_prices[-1]
    current_qqq = qqq_prices[-1]
    
    # Moving averages
    spy_ma20 = sum(spy_prices[-20:]) / 20
    spy_ma50 = sum(spy_prices[-50:]) / 50
    spy_ma200 = sum(spy_prices[-200:]) / 200
    
    qqq_ma20 = sum(qqq_prices[-20:]) / 20
    qqq_ma50 = sum(qqq_prices[-50:]) / 50
    qqq_ma200 = sum(qqq_prices[-200:]) / 200
    
    # ROC calculations
    spy_roc_5d = ((current_spy / spy_prices[-6]) - 1) * 100
    spy_roc_20d = ((current_spy / spy_prices[-21]) - 1) * 100
    
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
            'ma20': qqq_ma20,
            'ma50': qqq_ma50,
            'ma200': qqq_ma200
        },
        'vix': 18.5,  # Low VIX - bullish regime
        'ad_line_at_highs': True
    }
    
    print(f"\nInput Data:")
    print(f"  SPY: ${current_spy:.2f} (MA20: ${spy_ma20:.2f}, MA50: ${spy_ma50:.2f}, MA200: ${spy_ma200:.2f})")
    print(f"  QQQ: ${current_qqq:.2f}")
    print(f"  SPY ROC 5d: {spy_roc_5d:.2f}%, 20d: {spy_roc_20d:.2f}%")
    print(f"  VIX: {market_data['vix']}")
    
    engine = RegimeEngine()
    results = engine.calculate_regime(market_data)
    engine.print_report(results)
    
    # Test with bearish scenario
    print("\n" + "="*60)
    print("BEARISH SCENARIO TEST")
    print("="*60)
    
    spy_prices_bear = [500 - i * 2 for i in range(200)]  # Downward trend
    qqq_prices_bear = [400 - i * 2.5 for i in range(200)]
    
    current_spy_bear = spy_prices_bear[-1]
    spy_ma20_bear = sum(spy_prices_bear[-20:]) / 20
    spy_ma50_bear = sum(spy_prices_bear[-50:]) / 50
    spy_ma200_bear = sum(spy_prices_bear[-200:]) / 200
    spy_roc_5d_bear = ((current_spy_bear / spy_prices_bear[-6]) - 1) * 100
    spy_roc_20d_bear = ((current_spy_bear / spy_prices_bear[-21]) - 1) * 100
    
    bear_market_data = {
        'spy': {
            'prices': spy_prices_bear,
            'ma20': spy_ma20_bear,
            'ma50': spy_ma50_bear,
            'ma200': spy_ma200_bear,
            'roc_5d': spy_roc_5d_bear,
            'roc_20d': spy_roc_20d_bear
        },
        'qqq': {
            'prices': qqq_prices_bear
        },
        'vix': 32,  # High VIX - bearish regime
        'ad_line_at_highs': False
    }
    
    engine2 = RegimeEngine()
    results2 = engine2.calculate_regime(bear_market_data)
    engine2.print_report(results2)
    
    return results, results2

if __name__ == "__main__":
    test_regime_engine()
