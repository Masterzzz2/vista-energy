#!/usr/bin/env python3
"""Quick test script for Energy Optimizer services."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

print("=== Energy Optimizer Service Test ===\n")

# Test 1: Open-Meteo
print("1. Open-Meteo Weather API:")
try:
    from services.weather_service import WeatherService
    weather = WeatherService(lat=48.07, lon=11.03)
    result = weather.get_current_weather()
    print(f"   Temperature: {result.get('temperature', 'N/A')}°C")
    print(f"   Condition: {result.get('condition', 'N/A')}")
    print("   ✅ WORKING\n")
except Exception as e:
    print(f"   ❌ ERROR: {e}\n")

# Test 2: Tibber API
print("2. Tibber API:")
try:
    from services.tibber_service import TibberService
    tibber = TibberService()
    prices = tibber.get_current_prices()
    if prices:
        print(f"   Current price: {prices[0]['price']:.4f} €/kWh")
        print(f"   Price level: {prices[0]['level']}")
        print("   ✅ WORKING\n")
    else:
        print("   ⚠️  No prices returned\n")
except Exception as e:
    print(f"   ❌ ERROR: {e}\n")

# Test 3: Fronius Solar API
print("3. Fronius Solar API (192.168.1.80):")
try:
    import requests
    url = "http://192.168.1.80/solar_api/v1/GetPowerFlowRealtimeData.fcgi?Scope=System"
    resp = requests.get(url, timeout=5)
    if resp.status_code == 200:
        data = resp.json()
        site = data.get('Body', {}).get('Data', {}).get('Site', {})
        print(f"   PV: {site.get('P_PV', 'N/A')} W")
        print(f"   Grid: {site.get('P_Grid', 'N/A')} W")
        print(f"   Load: {site.get('P_Load', 'N/A')} W")
        print("   ✅ WORKING\n")
    else:
        print(f"   ⚠️  HTTP {resp.status_code}\n")
except Exception as e:
    print(f"   ❌ Not reachable: {e}\n")

print("=== Test Complete ===")