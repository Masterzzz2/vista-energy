"""
Vista-Energy — KEBA KeContact Plugin

Kommunikation via UDP-Protokoll (KEBA KeContact P30).
Unterstuetzt: KEBA KeContact P30 c-series, x-series, GREEN EDITION.

KEBA verwendet ein einfaches UDP-Protokoll auf Port 7090.
Befehle werden als ASCII-Text gesendet, Antworten kommen als JSON.

UDP-Befehle:
  - "report 1"  → Geraete-Info (Product, Serial, Firmware)
  - "report 2"  → Status (State, Plug, MaxCurr, Power, etc.)
  - "report 3"  → Energie-Zaehler (E total, E pres)
  - "currtime X" → Ladestrom setzen (X in mA, 0 oder 6000-63000)
  - "ena 1"     → Laden freigeben
  - "ena 0"     → Laden sperren

Zustaende (State):
  0 = Startend, 1 = Bereit (kein Auto), 2 = Auto angesteckt,
  3 = Laedt, 4 = Fehler, 5 = Auth.

Konfiguration (config.yaml):
  wallbox:
    type: keba_kecontact
    ip: 192.168.1.120
"""

import json
import logging
import socket
from datetime import datetime
from typing import Dict, Optional

from services.wallboxes.base import WallboxBase

logger = logging.getLogger(__name__)


class KEBAKeContactWallbox(WallboxBase):
    """KEBA KeContact P30 via UDP-Protokoll."""

    UDP_PORT = 7090
    BUFFER_SIZE = 4096

    def __init__(self, ip: str = '192.168.1.120',
                 timeout: int = 3,
                 **kwargs):
        self.ip = ip
        self.timeout = timeout

    # ==================================================================
    # WallboxBase — Pflicht-Methoden
    # ==================================================================

    def get_status(self) -> Dict:
        """Aktueller KEBA-Status via UDP report 2 + report 3."""
        r2 = self._udp_report(2)
        r3 = self._udp_report(3)

        if not r2:
            return self._fallback_status()

        state = r2.get('State', 0)
        plug = r2.get('Plug', 0)

        # Plug: Bit 0 = Stecker Wallbox, Bit 1 = Stecker verriegelt,
        #        Bit 2 = Stecker Fahrzeug, Bit 3 = Stecker Fahrzeug verriegelt
        is_connected = (plug & 0x04) > 0  # Fahrzeug angesteckt
        is_charging = state == 3

        # Leistung in mW → W
        power_mw = r2.get('P', 0)
        power_w = power_mw / 1000.0

        # Strom in mA → A (3 Phasen)
        i1 = r2.get('I1', 0) / 1000.0
        i2 = r2.get('I2', 0) / 1000.0
        i3 = r2.get('I3', 0) / 1000.0
        current_a = max(i1, i2, i3)

        # Aktive Phasen zaehlen
        phases = sum(1 for i in [i1, i2, i3] if i > 0.5)
        if phases == 0:
            phases = 3  # Default

        # Max-Strom in mA
        max_current_ma = r2.get('Curr user', r2.get('MaxCurr', 32000))
        max_current_a = max_current_ma / 1000.0

        # Energie dieser Session (report 3)
        energy_kwh = 0
        if r3:
            energy_kwh = r3.get('E pres', 0) / 10000.0  # 0.1 Wh → kWh

        return {
            'connected': is_connected,
            'charging': is_charging,
            'power_w': power_w,
            'energy_kwh': energy_kwh,
            'current_a': current_a,
            'max_current_a': max_current_a,
            'phases': phases,
            'timestamp': datetime.now(),
            'state': state,
            'plug': plug,
            'u1': r2.get('U1', 0) / 1000.0,  # Spannung Phase 1
            'u2': r2.get('U2', 0) / 1000.0,
            'u3': r2.get('U3', 0) / 1000.0,
            'energy_total_kwh': r3.get('E total', 0) / 10000.0 if r3 else 0,
        }

    def set_charge_current(self, amps: float) -> bool:
        """Ladestrom setzen (0 oder 6-63A, in mA gesendet).

        KEBA akzeptiert: 0 (Stop) oder 6000-63000 mA in 1mA-Schritten.
        """
        if amps <= 0:
            return self._udp_send('currtime 0 1')

        milliamps = int(min(63000, max(6000, amps * 1000)))
        # currtime [mA] [Timeout in Sekunden]
        # Timeout 0 = unbegrenzt
        return self._udp_send(f'currtime {milliamps} 0')

    # ==================================================================
    # Optionale Methoden
    # ==================================================================

    def start_charging(self) -> bool:
        """Laden freigeben."""
        return self._udp_send('ena 1')

    def stop_charging(self) -> bool:
        """Laden sperren."""
        return self._udp_send('ena 0')

    def get_info(self) -> Dict:
        """KEBA Wallbox-Info via report 1."""
        r1 = self._udp_report(1)
        if not r1:
            return {
                'manufacturer': 'KEBA',
                'model': 'KeContact P30',
                'serial': '',
                'firmware': '',
                'connected': False,
            }

        return {
            'manufacturer': 'KEBA',
            'model': r1.get('Product', 'KeContact P30'),
            'serial': r1.get('Serial', ''),
            'firmware': r1.get('Firmware', ''),
            'connected': True,
        }

    def is_connected(self) -> bool:
        """Pruefen ob KEBA erreichbar ist."""
        r1 = self._udp_report(1)
        return bool(r1)

    # ==================================================================
    # Plugin-Registrierung
    # ==================================================================

    @staticmethod
    def plugin_id() -> str:
        return 'keba_kecontact'

    @staticmethod
    def plugin_name() -> str:
        return 'KEBA KeContact P30'

    # ==================================================================
    # Interne Methoden — UDP
    # ==================================================================

    def _udp_send(self, command: str) -> bool:
        """UDP-Befehl an KEBA senden."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            sock.sendto(
                command.encode('ascii'),
                (self.ip, self.UDP_PORT)
            )
            # Auf Bestaetigung warten (optional, KEBA antwortet nicht immer)
            try:
                data, addr = sock.recvfrom(self.BUFFER_SIZE)
                logger.debug(f"KEBA Antwort auf '{command}': {data.decode()}")
            except socket.timeout:
                pass  # Kein Fehler, manche Befehle haben keine Antwort
            sock.close()
            logger.info(f"KEBA Befehl gesendet: {command}")
            return True
        except Exception as e:
            logger.error(f"KEBA UDP '{command}': {e}")
            return False

    def _udp_report(self, report_id: int) -> Optional[dict]:
        """UDP report-Befehl senden und JSON-Antwort parsen."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            sock.sendto(
                f'report {report_id}'.encode('ascii'),
                (self.ip, self.UDP_PORT)
            )
            data, addr = sock.recvfrom(self.BUFFER_SIZE)
            sock.close()

            response = data.decode('ascii').strip()
            return json.loads(response)
        except socket.timeout:
            logger.warning(f"KEBA report {report_id}: Timeout")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"KEBA report {report_id}: JSON-Fehler: {e}")
            return None
        except Exception as e:
            logger.error(f"KEBA report {report_id}: {e}")
            return None

    @staticmethod
    def _fallback_status() -> Dict:
        return {
            'connected': False,
            'charging': False,
            'power_w': 0,
            'energy_kwh': 0,
            'current_a': 0,
            'max_current_a': 32,
            'phases': 3,
            'timestamp': datetime.now(),
        }
