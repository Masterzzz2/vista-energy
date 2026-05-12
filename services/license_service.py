"""
Vista-Energy License Service
Lizenzpruefung gegen Vista-Energy Lizenzserver.

Ablauf:
  1. Erstinstallation → Trial startet (30 Tage, voll funktionsfaehig)
  2. Trial laeuft ab → Optimierungs-Features deaktiviert
  3. Kunde gibt Lizenzschluessel im Dashboard ein
  4. Key wird gegen Lizenzserver verifiziert + an Hardware-ID gebunden
  5. Taegliche Online-Pruefung (1x pro 24h)
  6. Bei Netzwerk-Ausfall: 7 Tage Offline-Gnadenfrist

WICHTIG: Bei abgelaufener Lizenz wird die Anlage NICHT lahmgelegt!
  - Dashboard + Datensammlung laufen weiter
  - Nur Optimierung/Steuerung wird deaktiviert
  - Keine Haftungsrisiken durch Lizenzpruefung
"""

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class LicenseService:
    """Verwaltet Trial, Lizenzschluessel und Online-Verifizierung."""

    LICENSE_SERVER = "https://license.vista-energy.de"
    TRIAL_DAYS = 30
    GRACE_PERIOD_DAYS = 7         # Offline-Gnadenfrist
    CHECK_INTERVAL_HOURS = 24     # Online-Pruefung alle 24h
    LICENSE_FILE = '.license.json'

    # Features die bei abgelaufener Lizenz deaktiviert werden
    LICENSED_FEATURES = {
        'battery_control',        # Batterie Lade/Entlade-Steuerung
        'tibber_optimization',    # Tibber-Preisoptimierung
        'wallbox_control',        # Wallbox-Steuerung
        'learning_profile',       # Lern-Profil (pausiert, nicht geloescht!)
        'auto_update',            # Automatische Updates
    }

    # Features die IMMER funktionieren (auch ohne Lizenz)
    FREE_FEATURES = {
        'dashboard',              # Dashboard anzeigen
        'data_collection',        # Daten sammeln
        'monitoring',             # PV/Batterie/Verbrauch anzeigen
        'manual_control',         # Manuelle Steuerung (Buttons)
    }

    def __init__(self, app_dir: Path):
        self.app_dir = Path(app_dir)
        self._license_path = self.app_dir / self.LICENSE_FILE
        self._license = self._load_license()
        self._hardware_id = None

    # ------------------------------------------------------------------
    # Oeffentliche API
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """True wenn Trial laeuft ODER Lizenz gueltig.

        Das ist die Haupt-Pruefung die der Optimizer abfragt.
        """
        status = self.get_status()
        return status['active']

    def is_feature_enabled(self, feature: str) -> bool:
        """Prueft ob ein bestimmtes Feature aktiviert ist.

        Args:
            feature: Name aus LICENSED_FEATURES oder FREE_FEATURES

        Returns:
            True wenn Feature nutzbar (aktive Lizenz oder Free-Feature)
        """
        if feature in self.FREE_FEATURES:
            return True
        if feature in self.LICENSED_FEATURES:
            return self.is_active()
        # Unbekanntes Feature → sicherheitshalber erlauben
        return True

    def get_status(self) -> Dict:
        """Aktueller Lizenz-Status fuer Dashboard und interne Pruefung.

        Returns:
            {
                'active': bool,
                'mode': 'trial' | 'licensed' | 'expired' | 'grace',
                'trial_days_left': int or None,
                'license_key': str or None (maskiert),
                'plan': str or None,
                'expires': str or None,
                'hardware_id': str,
                'last_verified': str or None,
                'message': str,
            }
        """
        hw_id = self.get_hardware_id()

        # Fall 1: Aktive Lizenz vorhanden
        if self._license.get('key') and self._license.get('verified'):
            expires = self._license.get('expires')
            if expires:
                try:
                    exp_dt = datetime.fromisoformat(expires)
                    if exp_dt > datetime.now():
                        # Lizenz gueltig
                        days_left = (exp_dt - datetime.now()).days
                        return {
                            'active': True,
                            'mode': 'licensed',
                            'trial_days_left': None,
                            'license_key': self._mask_key(
                                self._license['key']
                            ),
                            'plan': self._license.get('plan', 'basic'),
                            'expires': expires,
                            'days_left': days_left,
                            'hardware_id': hw_id,
                            'last_verified': self._license.get(
                                'last_verified'
                            ),
                            'message': (
                                f"Lizenz aktiv ({self._license.get('plan', 'Basic')}). "
                                f"Gueltig bis {exp_dt.strftime('%d.%m.%Y')}."
                            ),
                        }
                except (ValueError, TypeError):
                    pass

            # Lizenz ohne Ablaufdatum oder abgelaufen → Gnadenfrist pruefen
            last_verified = self._license.get('last_verified')
            if last_verified:
                try:
                    lv_dt = datetime.fromisoformat(last_verified)
                    grace_end = lv_dt + timedelta(days=self.GRACE_PERIOD_DAYS)
                    if datetime.now() < grace_end:
                        days_grace = (grace_end - datetime.now()).days
                        return {
                            'active': True,
                            'mode': 'grace',
                            'trial_days_left': None,
                            'license_key': self._mask_key(
                                self._license['key']
                            ),
                            'plan': self._license.get('plan'),
                            'expires': None,
                            'days_left': days_grace,
                            'hardware_id': hw_id,
                            'last_verified': last_verified,
                            'message': (
                                f"Lizenz-Pruefung fehlgeschlagen. "
                                f"Offline-Gnadenfrist: noch {days_grace} Tage."
                            ),
                        }
                except (ValueError, TypeError):
                    pass

        # Fall 2: Trial-Modus
        installed = self._license.get('installed_at')
        if installed:
            try:
                inst_dt = datetime.fromisoformat(installed)
                trial_end = inst_dt + timedelta(days=self.TRIAL_DAYS)
                days_left = (trial_end - datetime.now()).days

                if days_left > 0:
                    return {
                        'active': True,
                        'mode': 'trial',
                        'trial_days_left': days_left,
                        'license_key': None,
                        'plan': 'trial',
                        'expires': trial_end.isoformat(),
                        'days_left': days_left,
                        'hardware_id': hw_id,
                        'last_verified': None,
                        'message': (
                            f"Testphase: noch {days_left} Tage. "
                            f"Alle Features freigeschaltet."
                        ),
                    }
            except (ValueError, TypeError):
                pass

        # Fall 3: Abgelaufen
        return {
            'active': False,
            'mode': 'expired',
            'trial_days_left': 0,
            'license_key': self._mask_key(self._license.get('key')),
            'plan': None,
            'expires': None,
            'days_left': 0,
            'hardware_id': hw_id,
            'last_verified': self._license.get('last_verified'),
            'message': (
                "Lizenz abgelaufen. Optimierung deaktiviert. "
                "Bitte Lizenzschluessel eingeben oder Abo verlaengern."
            ),
        }

    def activate_key(self, key: str) -> Dict:
        """Lizenzschluessel aktivieren (gegen Server pruefen).

        Args:
            key: Lizenzschluessel im Format VE-XXXX-XXXX-XXXX

        Returns:
            {'success': bool, 'message': str}
        """
        key = key.strip().upper()
        if not self._validate_key_format(key):
            return {
                'success': False,
                'message': 'Ungueltiges Format. Erwartet: VE-XXXX-XXXX-XXXX'
            }

        hw_id = self.get_hardware_id()

        try:
            resp = requests.post(
                f"{self.LICENSE_SERVER}/api/activate",
                json={'key': key, 'hardware_id': hw_id},
                timeout=15,
                allow_redirects=False,
            )
            data = resp.json()

            if resp.status_code == 200 and data.get('success'):
                self._license['key'] = key
                self._license['hardware_id'] = hw_id
                self._license['verified'] = True
                self._license['plan'] = data.get('plan', 'basic')
                self._license['expires'] = data.get('expires')
                self._license['last_verified'] = datetime.now().isoformat()
                self._license['activated_at'] = datetime.now().isoformat()
                self._save_license()

                logger.info(
                    f"Lizenz aktiviert: {self._mask_key(key)}, "
                    f"Plan: {data.get('plan')}"
                )
                return {
                    'success': True,
                    'message': (
                        f"Lizenz erfolgreich aktiviert! "
                        f"Plan: {data.get('plan', 'Basic')}"
                    ),
                    'plan': data.get('plan'),
                }
            else:
                msg = data.get('message', 'Aktivierung fehlgeschlagen')
                logger.warning(f"Lizenz-Aktivierung abgelehnt: {msg}")
                return {'success': False, 'message': msg}

        except requests.RequestException as e:
            msg = f"Verbindung zum Lizenzserver fehlgeschlagen: {e}"
            logger.error(msg)
            return {'success': False, 'message': msg}

    def verify_online(self) -> bool:
        """Lizenz online verifizieren (taeglicher Check).

        Wird vom Scheduler aufgerufen (1x pro 24h).
        Returns True wenn Lizenz gueltig.
        """
        key = self._license.get('key')
        if not key:
            return False

        try:
            hw_id = self.get_hardware_id()
            resp = requests.post(
                f"{self.LICENSE_SERVER}/api/verify",
                json={'key': key, 'hardware_id': hw_id},
                timeout=15,
                allow_redirects=False,
            )
            data = resp.json()

            if resp.status_code == 200 and data.get('valid'):
                self._license['verified'] = True
                self._license['last_verified'] = datetime.now().isoformat()
                self._license['expires'] = data.get('expires')
                self._license['plan'] = data.get('plan', self._license.get('plan'))
                self._save_license()
                logger.debug("Lizenz online verifiziert: OK")
                return True
            else:
                logger.warning(
                    f"Lizenz online ungueltig: {data.get('message')}"
                )
                self._license['verified'] = False
                self._save_license()
                return False

        except requests.RequestException as e:
            # Netzwerk-Fehler: NICHT sofort deaktivieren → Gnadenfrist
            logger.warning(
                f"Lizenz-Verifizierung fehlgeschlagen (Netzwerk): {e}. "
                f"Gnadenfrist: {self.GRACE_PERIOD_DAYS} Tage."
            )
            return self._license.get('verified', False)

    def should_verify(self) -> bool:
        """True wenn eine Online-Pruefung faellig ist."""
        if not self._license.get('key'):
            return False
        last = self._license.get('last_verified')
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            elapsed_h = (datetime.now() - last_dt).total_seconds() / 3600
            return elapsed_h >= self.CHECK_INTERVAL_HOURS
        except (ValueError, TypeError):
            return True

    def deactivate(self) -> Dict:
        """Lizenz deaktivieren (z.B. bei Geraetewechsel).

        Informiert den Server, dass die Hardware-ID freigegeben wird.
        """
        key = self._license.get('key')
        if not key:
            return {'success': False, 'message': 'Keine Lizenz aktiv'}

        try:
            hw_id = self.get_hardware_id()
            requests.post(
                f"{self.LICENSE_SERVER}/api/deactivate",
                json={'key': key, 'hardware_id': hw_id},
                timeout=15,
            )
        except requests.RequestException:
            pass  # Server-Fehler ist OK, lokal trotzdem deaktivieren

        self._license['key'] = None
        self._license['verified'] = False
        self._license['plan'] = None
        self._save_license()

        logger.info("Lizenz deaktiviert")
        return {'success': True, 'message': 'Lizenz deaktiviert'}

    def get_hardware_id(self) -> str:
        """Eindeutige Hardware-ID aus CPU + MAC generieren.

        Kombiniert mehrere Quellen fuer Eindeutigkeit:
        - CPU-Serial (falls verfuegbar, z.B. Raspberry Pi)
        - Erste Netzwerk-MAC-Adresse
        - Machine-ID (Linux)

        Ergebnis ist ein SHA256-Hash → nicht rueckverfolgbar.
        """
        if self._hardware_id:
            return self._hardware_id

        parts = []

        # 1. Machine-ID (Linux, stabil ueber Reboots)
        machine_id_path = Path('/etc/machine-id')
        if machine_id_path.exists():
            parts.append(machine_id_path.read_text().strip())

        # 2. CPU-Serial (Raspberry Pi, manche SBCs)
        try:
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('Serial'):
                        parts.append(line.split(':')[1].strip())
                        break
        except (FileNotFoundError, IOError):
            pass

        # 3. MAC-Adresse (erstes Interface)
        mac = ':'.join(
            f'{(uuid.getnode() >> i) & 0xff:02x}'
            for i in range(0, 48, 8)
        )
        parts.append(mac)

        # 4. Hostname als Salt
        parts.append(platform.node())

        # Hash aller Teile → 12 Zeichen Hardware-ID
        combined = '|'.join(parts)
        full_hash = hashlib.sha256(combined.encode()).hexdigest()
        self._hardware_id = full_hash[:12].upper()

        return self._hardware_id

    # ------------------------------------------------------------------
    # Interne Methoden
    # ------------------------------------------------------------------

    def _load_license(self) -> dict:
        """Lizenzdaten aus JSON laden, bei Erstinstallation Trial starten."""
        try:
            if self._license_path.exists():
                data = json.loads(self._license_path.read_text())
                return data
        except Exception as e:
            logger.debug(f"License file read error: {e}")

        # Erstinstallation: Trial starten
        initial = {
            'installed_at': datetime.now().isoformat(),
            'key': None,
            'verified': False,
            'plan': None,
            'hardware_id': None,
        }
        self._license = initial
        self._save_license()
        logger.info(
            f"Vista-Energy Trial gestartet ({self.TRIAL_DAYS} Tage)"
        )
        return initial

    def _save_license(self):
        """Lizenzdaten als JSON speichern."""
        try:
            self._license_path.write_text(
                json.dumps(self._license, indent=2, default=str)
            )
        except Exception as e:
            logger.error(f"License save failed: {e}")

    @staticmethod
    def _validate_key_format(key: str) -> bool:
        """Prueft ob Key dem Format VE-XXXX-XXXX-XXXX entspricht."""
        pattern = r'^VE-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$'
        return bool(re.match(pattern, key))

    @staticmethod
    def _mask_key(key: Optional[str]) -> Optional[str]:
        """Lizenzschluessel maskieren fuer Anzeige: VE-XXXX-****-****"""
        if not key or len(key) < 7:
            return key
        return key[:7] + '-****-****'
