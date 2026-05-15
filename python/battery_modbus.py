"""
NEXUS Battery-Modbus Modul
==========================
Direkte Steuerung des BYD HVS 7.68 ueber den Fronius GEN24 SunSpec Modell 124.

Modbus-Map auf Slave 1 ab Adresse 40343 (ID/Length) + 40345 (Payload).
Wichtige Register:
  Offset 0  WChaMax        Max Lade-/Entlade-Leistung in W
  Offset 3  StorCtl_Mod    Bitfeld: bit0=ChargeCtrl, bit1=DischargeCtrl
  Offset 5  MinRsvPct      Min-Reserve in %  (SF -2)
  Offset 6  ChaState       Aktueller SOC in %  (SF -2)
  Offset 9  ChaSt          Status: 1=OFF 2=EMPTY 3=DISCHARGING 4=CHARGING 5=FULL 6=HOLDING
  Offset 10 OutWRte        Entlade-Rate-Limit in %  (SF -2, also 10000 = 100.00%)
  Offset 11 InWRte         Lade-Rate-Limit in %     (SF -2)
  Offset 13 InOutWRte_RvrtTms  Sekunden bis Ruecksetzen auf default

Nutzung:
    bm = BatteryModbus()
    bm.read()                       # liefert dict mit allen Werten
    bm.set_charge_limit_pct(0)      # Akku darf NICHT mehr aus PV/Netz laden
    bm.set_charge_limit_pct(100)    # voll laden erlaubt
    bm.set_discharge_limit_pct(100) # voll entladen erlaubt
    bm.release()                    # alles zurueck auf no-control
"""
from __future__ import annotations
import logging
from typing import Optional, Dict, Any

from pymodbus.client import ModbusTcpClient

log = logging.getLogger('battery_modbus')

# SunSpec-Konstanten fuer Fronius GEN24
HOST = '192.168.1.80'
PORT = 502
SLAVE = 1
MODEL_124_ADDR = 40343          # ID/Length
MODEL_124_PAYLOAD = 40345       # Beginn Payload

# Offsets innerhalb des Payloads
OFF_WCHAMAX = 0
OFF_STORCTL_MOD = 3
OFF_MIN_RSV_PCT = 5
OFF_CHA_STATE = 6
OFF_CHA_ST = 9
OFF_OUT_WRTE = 10
OFF_IN_WRTE = 11
OFF_RVRT_TMS = 13
OFF_CHA_GRI_SET = 15   # 1 = PV_AC (nur aus PV), 2 = PV_AC_DC (auch aus Netz)

# Skalierungsfaktoren (SF) - aus den letzten Registern abgelesen, hardcoded
SF_INOUTWRTE = -2     # Werte in %, also 10000 = 100.00%
SF_CHASTATE = -2

CHA_ST_NAMES = {
    1: 'OFF', 2: 'EMPTY', 3: 'DISCHARGING', 4: 'CHARGING',
    5: 'FULL', 6: 'HOLDING', 7: 'TESTING'
}
STORCTL_NAMES = {
    0: 'no-control', 1: 'charge-only', 2: 'discharge-only', 3: 'both'
}


def _s16(v: int) -> int:
    return v - 0x10000 if v > 0x7FFF else v


def _u16(v: int) -> int:
    return v & 0xFFFF if v >= 0 else (v + 0x10000) & 0xFFFF


class BatteryModbus:
    def __init__(self, host: str = HOST, port: int = PORT, slave: int = SLAVE,
                 timeout: float = 4.0):
        self.host = host
        self.port = port
        self.slave = slave
        self.timeout = timeout
        self._client: Optional[ModbusTcpClient] = None

    # ---------------------------------------------------------------
    # Connection-Handling
    # ---------------------------------------------------------------
    def _connect(self):
        if self._client and self._client.connected:
            return
        self._client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)
        if not self._client.connect():
            raise ConnectionError(f'cannot connect to modbus {self.host}:{self.port}')

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ---------------------------------------------------------------
    # Low-level
    # ---------------------------------------------------------------
    def _read_payload(self) -> list[int]:
        self._connect()
        r = self._client.read_holding_registers(
            address=MODEL_124_PAYLOAD, count=24, device_id=self.slave
        )
        if r.isError():
            raise IOError(f'modbus read error: {r}')
        return r.registers

    def _write(self, addr: int, val: int):
        self._connect()
        r = self._client.write_register(address=addr, value=val, device_id=self.slave)
        if r.isError():
            raise IOError(f'modbus write error: {r}')

    # ---------------------------------------------------------------
    # High-level
    # ---------------------------------------------------------------
    def read(self) -> Dict[str, Any]:
        """Gesamtzustand auslesen."""
        try:
            p = self._read_payload()
        except Exception as e:
            log.warning(f'read failed: {e}')
            return {'error': str(e)}

        return {
            'wchamax_w': p[OFF_WCHAMAX],
            'storctl_mod': p[OFF_STORCTL_MOD],
            'storctl_mod_name': STORCTL_NAMES.get(p[OFF_STORCTL_MOD], '?'),
            'min_rsv_pct': p[OFF_MIN_RSV_PCT] * (10 ** SF_CHASTATE),
            'soc': round(p[OFF_CHA_STATE] * (10 ** SF_CHASTATE), 1),
            'cha_st': p[OFF_CHA_ST],
            'cha_st_name': CHA_ST_NAMES.get(p[OFF_CHA_ST], '?'),
            'out_wrte_pct': round(_s16(p[OFF_OUT_WRTE]) * (10 ** SF_INOUTWRTE), 2),
            'in_wrte_pct': round(_s16(p[OFF_IN_WRTE]) * (10 ** SF_INOUTWRTE), 2),
            'rvrt_tms': p[OFF_RVRT_TMS] if p[OFF_RVRT_TMS] != 0xFFFF else None,
        }

    def set_charge_limit_pct(self, pct: float, revert_seconds: int = 0):
        """Setzt Lade-Limit in % (0 = kein Laden, 100 = voll erlaubt).

        revert_seconds = 0 bedeutet \"permanent bis ueberschrieben\".
        Wenn > 0, faellt der Wert nach dieser Zeit wieder auf 100% zurueck.
        """
        pct = max(0.0, min(100.0, float(pct)))
        raw = int(round(pct * 100))                      # SF -2
        # InOutWRte_RvrtTms: 0 = kein revert
        self._write(MODEL_124_PAYLOAD + OFF_RVRT_TMS, revert_seconds & 0xFFFF)
        self._write(MODEL_124_PAYLOAD + OFF_IN_WRTE, _u16(raw))
        # StorCtl_Mod muss mind. Bit0 (Charge) gesetzt haben damit InWRte greift
        self._enable_storctl(charge=True)
        log.info(f'set_charge_limit {pct}% (raw={raw}, revert={revert_seconds}s)')

    def set_discharge_limit_pct(self, pct: float, revert_seconds: int = 0):
        pct = max(0.0, min(100.0, float(pct)))
        raw = int(round(pct * 100))
        self._write(MODEL_124_PAYLOAD + OFF_RVRT_TMS, revert_seconds & 0xFFFF)
        self._write(MODEL_124_PAYLOAD + OFF_OUT_WRTE, _u16(raw))
        self._enable_storctl(discharge=True)
        log.info(f'set_discharge_limit {pct}% (raw={raw}, revert={revert_seconds}s)')

    def _enable_storctl(self, charge: bool = False, discharge: bool = False):
        """Setzt StorCtl_Mod-Bits ohne bestehende zu loeschen."""
        try:
            cur = self._read_payload()[OFF_STORCTL_MOD]
        except Exception:
            cur = 0
        new = cur
        if charge:
            new |= 0x01
        if discharge:
            new |= 0x02
        if new != cur:
            self._write(MODEL_124_PAYLOAD + OFF_STORCTL_MOD, new)
            log.info(f'StorCtl_Mod {cur} -> {new}')

    def release(self):
        """Limits zuruecksetzen, aber Lade-Kontrolle behalten.

        StorCtl_Mod = 1 (charge-control) statt 0 (no-control), damit der
        GEN24 nicht eigenmaechtiq die PV-Ladung drosselt und Ueberschuss
        ins Netz leitet, obwohl der Akku noch nicht voll ist.
        InWRte = 100% sorgt dafuer, dass der volle PV-Ueberschuss in den
        Akku fliesst. Revert-Timer (5 min) als Sicherheitsnetz falls
        Vista-PV abstuerzt — danach faellt der GEN24 auf no-control zurueck.
        """
        try:
            self._write(MODEL_124_PAYLOAD + OFF_IN_WRTE, _u16(10000))     # 100%
            self._write(MODEL_124_PAYLOAD + OFF_OUT_WRTE, _u16(10000))    # 100%
            # MinRsvPct vorsichtig auf 0 (kein erzwungenes Laden mehr)
            self._write(MODEL_124_PAYLOAD + OFF_MIN_RSV_PCT, 0)
            # ChaGriSet zurueck auf PV-only (1)
            try:
                self._write(MODEL_124_PAYLOAD + OFF_CHA_GRI_SET, 1)
            except Exception:
                pass
            # Revert-Timer: 5 Minuten Sicherheitsnetz
            self._write(MODEL_124_PAYLOAD + OFF_RVRT_TMS, 300)
            # charge-control statt no-control: GEN24 darf PV-Ladung nicht drosseln
            self._write(MODEL_124_PAYLOAD + OFF_STORCTL_MOD, 1)
            log.info('release: charge-control, Limits 100%, MinRsvPct=0, revert=300s')
        except Exception as e:
            log.warning(f'release failed: {e}')

    # ---------------------------------------------------------------
    # Force-Charge: Akku aktiv aus dem Netz nachladen bis target_soc
    # ---------------------------------------------------------------
    def force_charge(self, target_soc_pct: float = 80.0,
                     max_charge_w: int = 5000):
        """Erzwingt Akku-Nachladung aus dem Netz bis target_soc_pct.

        Mechanik (SunSpec/Fronius):
          - ChaGriSet = 2  (PV_AC_DC: Akku darf aus AC-Netz geladen werden)
          - InWRte    = 100% (Lade-Limit hoch)
          - OutWRte   = 0%   (Entladen sperren)
          - MinRsvPct = target_soc_pct (Inverter laedt aktiv bis dahin)
          - StorCtl_Mod = 3 (Charge + Discharge Control aktiv)
        Sicherheits-Begrenzung: max_charge_w wird in WChaMax geschrieben.
        """
        target_soc_pct = max(5.0, min(100.0, float(target_soc_pct)))
        max_charge_w = max(500, min(7680, int(max_charge_w)))
        try:
            # 1. ChaGriSet = 2 (Netz-Laden erlauben)
            try:
                self._write(MODEL_124_PAYLOAD + OFF_CHA_GRI_SET, 2)
            except Exception as e:
                log.warning(f'ChaGriSet write failed: {e}')
            # 2. WChaMax setzen (max Power)
            try:
                self._write(MODEL_124_PAYLOAD + OFF_WCHAMAX, max_charge_w)
            except Exception as e:
                log.warning(f'WChaMax write failed: {e}')
            # 3. Lade-Limit hoch
            self._write(MODEL_124_PAYLOAD + OFF_IN_WRTE, _u16(10000))
            # 4. Entlade-Limit auf 0
            self._write(MODEL_124_PAYLOAD + OFF_OUT_WRTE, 0)
            # 5. MinRsvPct auf Ziel-SOC (* 100 wegen SF -2)
            self._write(MODEL_124_PAYLOAD + OFF_MIN_RSV_PCT, int(target_soc_pct * 100))
            # 6. StorCtl_Mod = 3 (charge + discharge control)
            self._write(MODEL_124_PAYLOAD + OFF_STORCTL_MOD, 3)
            log.info(f'force_charge: target={target_soc_pct}% max={max_charge_w}W')
        except Exception as e:
            log.error(f'force_charge failed: {e}')
            raise


# ---------------------------------------------------------------------------
# CLI fuer schnellen Test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    import json
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    bm = BatteryModbus()
    if len(sys.argv) == 1 or sys.argv[1] == 'read':
        print(json.dumps(bm.read(), indent=2))
    elif sys.argv[1] == 'charge':
        bm.set_charge_limit_pct(float(sys.argv[2]))
        print(json.dumps(bm.read(), indent=2))
    elif sys.argv[1] == 'discharge':
        bm.set_discharge_limit_pct(float(sys.argv[2]))
        print(json.dumps(bm.read(), indent=2))
    elif sys.argv[1] == 'release':
        bm.release()
        print(json.dumps(bm.read(), indent=2))
    else:
        print('usage: battery_modbus.py [read|charge PCT|discharge PCT|release]')
    bm.close()
