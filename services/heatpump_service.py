"""
NEXUS Heatpump-Service (vorbereitet, deaktiviert)
=================================================
Skeleton fuer die Anbindung der Bosch Compressor 5000 DW Warmwasser-Waermepumpe.

Aktivierung spaeter ueber .env:
    HEATPUMP_ENABLED=true
    HEATPUMP_TYPE=bosch_cs5000dw
    HEATPUMP_HOST=192.168.1.xx          (IPM-Modul der WP)
    HEATPUMP_PORT=502                   (Modbus TCP Standard)
    HEATPUMP_TANK_LITERS=270
    HEATPUMP_MAX_KW=2.0

Steuerungsmoeglichkeiten der Bosch CS5000 DW:
  a) Modbus TCP ueber das IP-Modul (IPM) - liest/setzt Soll-Temperatur, Modus
  b) SG-Ready Schnittstelle (potentialfreie Kontakte): 4 Stati
        1 = Sperrzeit         (WP aus, max 2h)
        2 = Normalbetrieb     (Default)
        3 = Erhoehter Betrieb (PV-Ueberschuss verwerten)
        4 = Anlauf-Befehl     (Sofort EIN, max 2h)

Strategie sobald angeschlossen:
  - viel PV-Ueberschuss + Akku >85%       -> SG-Ready 3 (Boost auf z.B. 65 C)
  - sehr guenstiger Tibber-Preis (< 5 ct) -> SG-Ready 4 (Sofort-Anlauf)
  - sehr teurer Strompreis (> 35 ct)      -> SG-Ready 1 (Sperre)
  - sonst                                 -> SG-Ready 2 (Normal)

Diese Datei ist aktuell ein Stub - die Methoden geben sinnvolle Defaults oder
"not_configured" zurueck. Sobald die Pumpe da ist, einfach .env aktivieren und
die echten Modbus-Calls in _read_modbus / _write_sg_ready einbauen.
"""
from __future__ import annotations
import os
import logging
from typing import Dict
from datetime import datetime

log = logging.getLogger('heatpump')

SG_READY_NAMES = {
    1: 'Sperrzeit (aus, max 2h)',
    2: 'Normalbetrieb',
    3: 'Erhoehter Betrieb (PV-Boost)',
    4: 'Sofort-Anlauf (max 2h)',
}


class HeatpumpService:
    """Stub-Service. Wird automatisch deaktiviert wenn HEATPUMP_ENABLED nicht true."""

    def __init__(self):
        self.enabled = os.getenv('HEATPUMP_ENABLED', 'false').lower() == 'true'
        self.host = os.getenv('HEATPUMP_HOST', '')
        self.port = int(os.getenv('HEATPUMP_PORT', 502))
        self.type = os.getenv('HEATPUMP_TYPE', 'bosch_cs5000dw')
        self.tank_l = float(os.getenv('HEATPUMP_TANK_LITERS', 270))
        self.max_kw = float(os.getenv('HEATPUMP_MAX_KW', 2.0))
        self._last_sg_ready = 2  # Normal
        self._last_set_at = None

    # -----------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------
    def status(self) -> Dict:
        if not self.enabled:
            return {
                'enabled': False,
                'reason': 'HEATPUMP_ENABLED=false in .env - Geraet noch nicht angeschlossen.',
                'type': self.type,
                'planned_features': [
                    'Modbus TCP Lesen (Tank-Temperatur, Stromaufnahme, Status)',
                    'SG-Ready Steuerung (4 Modi)',
                    'PV-Ueberschuss-Boost wenn Akku voll',
                    'Tibber-billig-Stunde Erhitzen',
                    'Sperre bei sehr teurem Strom',
                ],
            }
        try:
            return self._read_modbus()
        except Exception as e:
            log.warning(f'heatpump status: {e}')
            return {'enabled': True, 'error': str(e)}

    # -----------------------------------------------------------------
    # Steuerung (Stub)
    # -----------------------------------------------------------------
    def set_sg_ready(self, level: int) -> Dict:
        """level 1..4 - siehe SG_READY_NAMES."""
        if level not in (1, 2, 3, 4):
            return {'success': False, 'error': 'level muss 1..4 sein'}
        if not self.enabled:
            return {'success': False, 'error': 'Heatpump deaktiviert (HEATPUMP_ENABLED=false)'}
        try:
            self._write_sg_ready(level)
            self._last_sg_ready = level
            self._last_set_at = datetime.utcnow().isoformat() + 'Z'
            log.info(f'SG-Ready -> {level} ({SG_READY_NAMES[level]})')
            return {'success': True, 'level': level, 'name': SG_READY_NAMES[level]}
        except Exception as e:
            log.warning(f'set_sg_ready: {e}')
            return {'success': False, 'error': str(e)}

    def auto_decide(self, pv_w: float, surplus_w: float, soc: float,
                    price_ct: float | None) -> Dict:
        """Schlaegt den passenden SG-Ready Level vor (regelbasiert)."""
        if not self.enabled:
            return {'recommended': 2, 'reason': 'WP deaktiviert'}
        # Sehr teuer -> sperren
        if price_ct is not None and price_ct > 35:
            return {'recommended': 1, 'reason': f'Strom teuer ({price_ct:.1f} ct/kWh)'}
        # Sehr guenstig (oder negativ) -> Sofort-Anlauf
        if price_ct is not None and price_ct < 5:
            return {'recommended': 4, 'reason': f'Strom sehr guenstig ({price_ct:.1f} ct/kWh)'}
        # Viel Ueberschuss + Akku gut -> Boost
        if surplus_w > 1500 and soc > 80:
            return {'recommended': 3, 'reason': f'PV-Ueberschuss {surplus_w:.0f}W, Akku {soc:.0f}%'}
        # Default
        return {'recommended': 2, 'reason': 'Normalbetrieb'}

    # -----------------------------------------------------------------
    # Internal stubs - hier spaeter Modbus implementieren
    # -----------------------------------------------------------------
    def _read_modbus(self) -> Dict:
        """TODO: pymodbus Client zum IPM-Modul, lese:
            - Tank-Temperatur (oben/unten)
            - Soll-Temperatur
            - Aktuelle Stromaufnahme
            - Betriebsmodus
        """
        return {
            'enabled': True,
            'connected': False,
            'note': 'Modbus-Implementierung steht aus (Geraet noch nicht da).',
            'host': self.host,
            'port': self.port,
            'type': self.type,
            'tank_liters': self.tank_l,
            'max_kw': self.max_kw,
            'last_sg_ready': self._last_sg_ready,
            'last_sg_ready_name': SG_READY_NAMES.get(self._last_sg_ready),
            'last_set_at': self._last_set_at,
        }

    def _write_sg_ready(self, level: int):
        """TODO: SG-Ready ueber GPIO oder Modbus an die WP senden."""
        log.info(f'[STUB] SG-Ready Befehl gesendet: {level}')
