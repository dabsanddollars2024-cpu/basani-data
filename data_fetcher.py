"""
Market Data Fetcher
Pulls SPY, QQQ, and VIX data from Polygon.io API or Alpaca.
"""

import os
import sys
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen
import urllib.request
import urllib.error

# Try importing requests, fall back to urllib if not available
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Configuration
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY') or os.environ.get('POLYGON_KEY') or ''
ALPACA_API_KEY = os.environ.get('APCA_API_KEY_ID') or os.environ.get('ALPACA_API_KEY') or ''
ALPACA_SECRET_KEY = os.environ.get('APCA_API_SECRET_KEY') or os.environ.get('ALPACA_SECRET_KEY') or ''


class DataFetcher:
    """
    Fetches market data from Polygon.io or Alpaca API.
    """
    
    def __init__(self, api_key: str = None, provider: str = 'polygon'):
        self.api_key = api_key or POLYGON_API_KEY
        self.provider = provider.lower()
        
    def get_polygon_bars(self, symbol: str, days: int = 200) -> Optional[List[Dict]]:
        """Fetch daily bars from Polygon.io"""
        if not self.api_key:
            print("WARNING: No Polygon API key found")
            return None
            
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days+50)  # Extra days for MA calculations
        
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
            f"?adjusted=true&sort=asc&limit=5000&apiKey={self.api_key}"
        )
        
        try:
            if HAS_REQUESTS:
                resp = requests.get(url, timeout=30)
                data = resp.json()
            else:
                with urlopen(url, timeout=30) as response:
                    data = json.loads(response.read().decode())
                    
            if data.get('status') == 'OK' and 'results' in data:
                return data['results']
            else:
                print(f"Polygon API error for {symbol}: {data.get('error', 'Unknown error')}")
                return None
        except Exception as e:
            print(f"Error fetching Polygon data for {symbol}: {e}")
            return None
    
    def get_alpaca_bars(self, symbol: str, days: int = 200) -> Optional[List[Dict]]:
        """Fetch daily bars from Alpaca API"""
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            print("WARNING: No Alpaca API credentials found")
            return None
            
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days+50)
        
        url = (
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
            f"?start={start_date.isoformat()}&end={end_date.isoformat()}"
            f"&timeframe=1Day&limit=10000&adjustment=split"
        )
        
        headers = {
            'APCA-API-KEY-ID': ALPACA_API_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET_KEY
        }
        
        try:
            if HAS_REQUESTS:
                resp = requests.get(url, headers=headers, timeout=30)
                data = resp.json()
            else:
                req = urllib.request.Request(url, headers=headers)
                with urlopen(req, timeout=30) as response:
                    data = json.loads(response.read().decode())
                    
            if 'bars' in data:
                return data['bars']
            else:
                print(f"Alpaca API error for {symbol}: {data.get('error', 'Unknown error')}")
                return None
        except Exception as e:
            print(f"Error fetching Alpaca data for {symbol}: {e}")
            return None
    
    def get_vix_data(self) -> Optional[float]:
        """
        Get VIX data. Tries multiple approaches:
        1. Polygon ^VIX ticker
        2. Derived from SPX options
        3. Hardcoded estimate if unavailable
        """
        # Try Polygon first
        if self.api_key and self.provider == 'polygon':
            bars = self.get_polygon_bars('^VIX', days=5)
            if bars and len(bars) > 0:
                return bars[-1].get('c', bars[-1].get('close'))
        
        # Try Alpaca with VIX approximation (VX* indexes)
        if ALPACA_API_KEY:
            bars = self.get_alpaca_bars('VIXY', days=5)  # VIX ETF proxy
            if bars and len(bars) > 0:
                return bars[-1].get('close')
        
        # Return None to indicate VIX not available
        print("WARNING: Could not fetch VIX data")
        return None
    
    def calculate_ma(self, prices: List[float], period: int) -> float:
        """Calculate simple moving average"""
        if len(prices) < period:
            return 0
        return sum(prices[-period:]) / period
    
    def calculate_roc(self, prices: List[float], period: int) -> float:
        """Calculate rate of change percentage"""
        if len(prices) < period + 1:
            return 0
        current = prices[-1]
        past = prices[-(period + 1)]
        if past == 0:
            return 0
        return ((current / past) - 1) * 100
    
    def fetch_market_data(self, symbols: List[str] = None, days: int = 200) -> Dict:
        """
        Fetch all market data needed for regime calculation.
        
        Returns:
            Dict with 'spy', 'qqq', 'vix', 'error' keys
        """
        if symbols is None:
            symbols = ['SPY', 'QQQ']
            
        result = {
            'spy': {'prices': [], 'ma20': 0, 'ma50': 0, 'ma200': 0, 'roc_5d': 0, 'roc_20d': 0},
            'qqq': {'prices': [], 'ma20': 0, 'ma50': 0, 'ma200': 0},
            'vix': None,
            'errors': []
        }
        
        # Determine provider
        if self.api_key:
            provider = 'polygon'
        elif ALPACA_API_KEY:
            provider = 'alpaca'
        else:
            provider = None
            result['errors'].append("No API credentials configured")
            
        print(f"Using data provider: {provider or 'NONE'}")
        
        # Fetch SPY
        if 'SPY' in symbols:
            print(f"Fetching SPY data...")
            if provider == 'polygon':
                bars = self.get_polygon_bars('SPY', days)
            else:
                bars = self.get_alpaca_bars('SPY', days)
                
            if bars:
                prices = [b.get('c', b.get('close')) for b in bars]
                result['spy']['prices'] = prices
                result['spy']['ma20'] = self.calculate_ma(prices, 20)
                result['spy']['ma50'] = self.calculate_ma(prices, 50)
                result['spy']['ma200'] = self.calculate_ma(prices, 200)
                result['spy']['roc_5d'] = self.calculate_roc(prices, 5)
                result['spy']['roc_20d'] = self.calculate_roc(prices, 20)
                print(f"  SPY: {len(prices)} bars, price={prices[-1]:.2f}, ma200={result['spy']['ma200']:.2f}")
            else:
                result['errors'].append("Failed to fetch SPY data")
        
        # Fetch QQQ
        if 'QQQ' in symbols:
            print(f"Fetching QQQ data...")
            if provider == 'polygon':
                bars = self.get_polygon_bars('QQQ', days)
            else:
                bars = self.get_alpaca_bars('QQQ', days)
                
            if bars:
                prices = [b.get('c', b.get('close')) for b in bars]
                result['qqq']['prices'] = prices
                result['qqq']['ma20'] = self.calculate_ma(prices, 20)
                result['qqq']['ma50'] = self.calculate_ma(prices, 50)
                result['qqq']['ma200'] = self.calculate_ma(prices, 200)
                print(f"  QQQ: {len(prices)} bars, price={prices[-1]:.2f}, ma200={result['qqq']['ma200']:.2f}")
            else:
                result['errors'].append("Failed to fetch QQQ data")
        
        # Fetch VIX
        print(f"Fetching VIX data...")
        result['vix'] = self.get_vix_data()
        if result['vix']:
            print(f"  VIX: {result['vix']:.2f}")
        else:
            result['errors'].append("Failed to fetch VIX data")
            
        return result


def check_api_configuration() -> Dict:
    """Check which APIs are configured"""
    status = {
        'polygon': bool(POLYGON_API_KEY),
        'alpaca': bool(ALPACA_API_KEY and ALPACA_SECRET_KEY),
        'has_requests': HAS_REQUESTS
    }
    
    print("\n" + "="*50)
    print("API CONFIGURATION STATUS")
    print("="*50)
    print(f"  Polygon API Key: {'YES' if status['polygon'] else 'NO'}")
    print(f"  Alpaca API Key: {'YES' if status['alpaca'] else 'NO'}")
    print(f"  requests library: {'YES' if status['has_requests'] else 'NO'}")
    print("="*50 + "\n")
    
    return status


def run_regime_analysis():
    """Main function to run regime analysis with current market data"""
    from regime_engine import RegimeEngine
    
    print("="*60)
    print("MARKET REGIME ANALYSIS")
    print("="*60)
    
    # Check API configuration
    check_api_configuration()
    
    # Initialize fetcher
    fetcher = DataFetcher()
    
    # Fetch market data
    print("\nFetching market data...")
    market_data = fetcher.fetch_market_data(days=200)
    
    if market_data['errors']:
        print("\nWARNING: Some data fetch errors occurred:")
        for err in market_data['errors']:
            print(f"  - {err}")
    
    # Check if we have enough data
    if len(market_data['spy']['prices']) < 200:
        print(f"\nERROR: Insufficient SPY data ({len(market_data['spy']['prices'])})")
        print("Need at least 200 days of price history.")
        return None
        
    if len(market_data['qqq']['prices']) < 200:
        print(f"\nERROR: Insufficient QQQ data ({len(market_data['qqq']['prices'])})")
        return None
    
    # Initialize regime engine
    engine = RegimeEngine()
    
    # Calculate regime
    # For breadth, we'll estimate A/D at highs based on MA200 position
    ad_at_highs = (
        market_data['spy']['prices'][-1] > market_data['spy']['ma200'] and
        market_data['spy']['prices'][-1] > market_data['spy']['ma50'] and
        market_data['spy']['prices'][-1] > market_data['spy']['ma20']
    )
    
    regime_input = {
        'spy': market_data['spy'],
        'qqq': market_data['qqq'],
        'vix': market_data['vix'] if market_data['vix'] else 20,  # Default to 20 if no VIX
        'ad_line_at_highs': ad_at_highs
    }
    
    results = engine.calculate_regime(regime_input)
    engine.print_report(results)
    
    return results


if __name__ == "__main__":
    print("Data Fetcher Module v1.0")
    print("Usage: python data_fetcher.py to run analysis")
    print("   or: from data_fetcher import DataFetcher, check_api_configuration")
    
    # Check configuration
    check_api_configuration()
