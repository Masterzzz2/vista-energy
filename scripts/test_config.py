#!/usr/bin/env python3
"""
Quick test script for Energy Optimizer
Run this to verify your configuration before starting the full app.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

def test_configuration():
    print("=== Energy Optimizer Configuration Test ===\n")
    
    from dotenv import load_dotenv
    load_dotenv()
    
    errors = []
    
    # Test 1: Check .env file exists
    env_path = Path(__file__).parent.parent / '.env'
    if not env_path.exists():
        print("⚠️  No .env file found. Copy .env.example to .env and fill in your tokens.")
    else:
        print("✅ .env file found")
    
    # Test 2: Tibber Token
    tibber_token = os.getenv('TIBBER_API_TOKEN')
    if tibber_token and tibber_token != 'your_tibber_token_here':
        print("✅ Tibber API token configured")
        
        # Test Tibber connection
        from services.tibber_service import TibberService
        tibber = TibberService(tibber_token)
        prices = tibber.get_current_prices()
        if prices:
            print(f"   Current price: {prices[0]['price']:.3f} €/kWh")
        else:
            print("⚠️  Tibber API returned no data")
    else:
        errors.append("TIBBER_API_TOKEN not set")
        print("❌ Tibber API token missing")
    
    # Test 3: EVCC/Fronius
    evcc_url = os.getenv('EVCC_API_URL')
    if evcc_url and evcc_url != 'http://192.168.1.x:7070':
        print("✅ EVCC API URL configured")
        
        from services.evcc_service import EVCCService
        evcc = EVCCService(evcc_url, os.getenv('EVCC_API_TOKEN'))
        state = evcc.get_current_state()
        print(f"   Battery SoC: {state.get('battery_soc', 0)*100:.0f}%")
        print(f"   House consumption: {state.get('house_consumption', 0)/1000:.1f} kW")
    else:
        print("⚠️  EVCC_API_URL not set (will use simulated data)")
    
    # Test 4: Forecast.Solar
    forecast_key = os.getenv('FORECAST_SOLAR_API_KEY')
    if forecast_key:
        print("✅ Forecast.Solar API key configured")
    else:
        print("⚠️  Forecast.Solar API key not set")
    
    # Test 5: Weather (Open-Meteo - no key needed)
    print("✅ Open-Meteo configured (no API key needed)")
    
    # Test 6: Battery settings
    battery_kwh = float(os.getenv('BATTERY_USABLE_KWH', '7.0'))
    print(f"✅ Battery capacity: {battery_kwh} kWh")
    
    # Test 7: Port
    port = int(os.getenv('WEB_PORT', '8080'))
    print(f"✅ Web UI port: {port}")
    
    print("\n=== Summary ===")
    if errors:
        print(f"❌ {len(errors)} configuration error(s):")
        for e in errors:
            print(f"   - {e}")
    else:
        print("✅ All critical configurations set!")
    
    print("\nNext steps:")
    print("  1. python scripts/init_db.py")
    print("  2. python app.py  (or: systemctl --user start energy-optimizer)")

if __name__ == '__main__':
    import os
    test_configuration()