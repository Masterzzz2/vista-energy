#!/bin/bash
# EVCC Battery Charging Decision Script
# Runs daily to decide if battery should charge from grid

LOG="/tmp/evcc-decision.log"
EVCC_API="http://localhost:7070/api/state"

echo "=== EVCC Battery Decision $(date) ===" >> $LOG

# Get current state
STATE=$(curl -s $EVCC_API)
if [ $? -ne 0 ]; then
    echo "ERROR: Cannot reach EVCC" >> $LOG
    exit 1
fi

# Extract key values with Python
python3 << PYEOF >> $LOG
import json, sys
from datetime import datetime, timezone, timedelta

d = json.loads('$STATE' if '$STATE' else '{}')

tariff = d.get('tariffGrid', 0)
battery_soc = d.get('battery', {}).get('soc', 0)
pv_power = d.get('pvPower', 0)

print(f"Battery SOC: {battery_soc:.1f}%")
print(f"PV Power: {pv_power:.0f} W")
print(f"Current Tibber Price: {tariff:.3f} €/kWh")
PYEOF

# Get tomorrow's weather
WEATHER=$(curl -s "https://api.open-meteo.com/v1/forecast?latitude=52.52&longitude=13.405&hourly=shortwave_radiation&forecast_days=2&timezone=Europe/Berlin")
TOMORROW_KWH=$(echo "$WEATHER" | python3 -c "
import json,sys
d=json.load(sys.stdin)
rad=d['hourly']['shortwave_radiation']
total=sum(r for i,r in enumerate(rad[24:48]) if r > 0)
print(round(total * 7.695 * 0.86 / 1000, 1))
")

echo "Tomorrow PV estimate: $TOMORROW_KWH kWh" >> $LOG

# Decision with Python
python3 << PYEOF >> $LOG
tariff = $tariff if '$tariff' else 0
battery_soc = $battery_soc if '$battery_soc' else 0
threshold = 0.15
min_pv_kwh = 15

if battery_soc >= 50:
    print(f"\nDECISION: No grid charging needed")
    print(f"REASON: Battery is {battery_soc:.0f}% (>= 50%)")
elif tariff < threshold:
    print(f"\nDECISION: Should charge battery from grid")
    print(f"REASON: Price is low ({tariff:.3f}€ < {threshold}€)")
elif float('$TOMORROW_KWH') < min_pv_kwh:
    print(f"\nDECISION: Should charge battery from grid")
    print(f"REASON: Bad weather tomorrow ({'$TOMORROW_KWH'} kWh < {min_pv_kwh} kWh)")
else:
    print(f"\nDECISION: No grid charging needed")
    print(f"REASON: Battery ok ({battery_soc:.0f}%), good PV day tomorrow ({'$TOMORROW_KWH'} kWh)")
PYEOF

echo ""
echo "=== Recommendation ==="
tail -5 $LOG
