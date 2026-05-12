#!/usr/bin/env python3
"""EVCC Battery Charging Decision Script"""
import json
import urllib.request
from datetime import datetime, timezone, timedelta

LOG = "/tmp/evcc-decision.log"

def log(msg):
    print(msg)
    with open(LOG, "a") as f:
        f.write(msg + "\n")

log(f"=== EVCC Battery Decision {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

# Get current state from EVCC
try:
    with urllib.request.urlopen("http://localhost:7070/api/state", timeout=5) as resp:
        evcc_data = json.loads(resp.read())
except Exception as e:
    log(f"ERROR: Cannot reach EVCC: {e}")
    exit(1)

# Extract key values
tariff = evcc_data.get('tariffGrid', 0)
battery_soc = evcc_data.get('battery', {}).get('soc', 0)
pv_power = evcc_data.get('pvPower', 0)

log(f"Battery SOC: {battery_soc:.1f}%")
log(f"PV Power: {pv_power:.0f} W")
log(f"Current Tibber Price: {tariff:.3f} €/kWh")

# Get tomorrow's weather from Open-Meteo
try:
    url = "https://api.open-meteo.com/v1/forecast?latitude=52.52&longitude=13.405&hourly=shortwave_radiation&forecast_days=2&timezone=Europe/Berlin"
    with urllib.request.urlopen(url, timeout=10) as resp:
        weather = json.loads(resp.read())
    
    rad = weather['hourly']['shortwave_radiation']
    # Tomorrow is hours 24-47 (indices 24-47)
    total_rad = sum(r for r in rad[24:48] if r > 0)
    tomorrow_kwh = round(total_rad * 7.695 * 0.86 / 1000, 1)
except Exception as e:
    log(f"Weather API error: {e}")
    tomorrow_kwh = 999  # Assume good weather on error

log(f"Tomorrow PV estimate: {tomorrow_kwh} kWh")

# Decision logic
THRESHOLD_PRICE = 0.15  # €/kWh
THRESHOLD_PV = 15       # kWh - below this = bad day
THRESHOLD_SOC = 50     # % - below this = need to charge

log("")

if battery_soc >= THRESHOLD_SOC:
    log(f"DECISION: No grid charging needed")
    log(f"REASON: Battery is {battery_soc:.0f}% (ok)")
    recommendation = "Battery full genug, kein Laden nötig"
elif tariff < THRESHOLD_PRICE:
    log(f"DECISION: Should charge battery from grid!")
    log(f"REASON: Price is low ({tariff:.3f}€ < {THRESHOLD_PRICE}€)")
    recommendation = f"⚡ Batterie laden! Strom ist günstig ({tariff:.3f}€/kWh)"
elif tomorrow_kwh < THRESHOLD_PV:
    log(f"DECISION: Should charge battery from grid!")
    log(f"REASON: Bad weather tomorrow ({tomorrow_kwh} kWh < {THRESHOLD_PV} kWh)")
    recommendation = f"🌧️ Batterie laden! Morgen schlechtes Wetter ({tomorrow_kwh} kWh)"
else:
    log(f"DECISION: No grid charging needed")
    log(f"REASON: Battery ok ({battery_soc:.0f}%), good PV tomorrow ({tomorrow_kwh} kWh)")
    recommendation = f"✅ Alles gut - Battery {battery_soc:.0f}%, Morgen {tomorrow_kwh} kWh PV"

print(f"\n=== NEXUS Empfehlung ===")
print(recommendation)
