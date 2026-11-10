#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbus-solis.py  -  Present ONE Solis (Ginlong) grid-tie inverter on the Venus OS
                  dbus as a native com.victronenergy.pvinverter device, with
                  optional active-power limiting over Modbus.

This script drives a single inverter on a single serial port. For automatic
USB discovery of one or more inverters, run solis_scan.py, which probes every
serial port and launches one instance of this script per Solis it finds.

Transport : Modbus RTU over a USB->RS485 adapter (CH340 / FTDI -> /dev/ttyUSBx)
Appears in: GX display, Device List, and VRM as a PV Inverter (AC output)
Control   : writable /Ac/PowerLimit (watts) -> Solis power-limit holding regs
            (DISABLED by default; pass --enable-control to allow register writes)

Register/control maps below are the commonly documented Solis S5/S6 layout and
MUST be verified against the Ginlong "RS485 Modbus-RTU protocol" doc for YOUR
model/firmware. Use --probe (FC04 input) and --probe-fc 3 (FC03 holding) to
match registers to the inverter's own display / SolisCloud.

Examples:
    ./dbus-solis.py --serial /dev/ttyUSB0 --slave 1 --rated 5000
    ./dbus-solis.py --serial /dev/ttyUSB0 --identify            # probe, exit 0 if Solis
    ./dbus-solis.py --serial /dev/ttyUSB0 --read-limit
    ./dbus-solis.py --serial /dev/ttyUSB0 --enable-control --set-limit 3000 --rated 5000
    ./dbus-solis.py --serial /dev/ttyUSB0 --probe-fc 3 --probe 3000 80
"""

import sys
import os
import argparse
import logging

# ---------------------------------------------------------------------------
# velib_python (vedbus): clone into ./ext/velib_python or point a path below.
#   git clone https://github.com/victronenergy/velib_python ext/velib_python
# ---------------------------------------------------------------------------
for _p in (
    os.path.join(os.path.dirname(__file__), "ext", "velib_python"),
    "/data/dbus-solis/ext/velib_python",
    "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
):
    if os.path.isdir(_p):
        sys.path.insert(0, _p)
        break

import minimalmodbus  # pip3 install minimalmodbus  (pulls in pyserial)


# ===========================================================================
# SOLIS INPUT-REGISTER MAP  (read, function code 0x04)
# Verified against Ginlong "RS485 MODBUS Solis PV Grid-Connected Inverter".
#   kind: 'u16'|'s16'|'u32'|'s32'  (u32/s32 = this reg + next, MSW first)
# Phase mapping: 3033/3036 = AB line / A phase; 3034/3037 = BC / B; 3035/3038 = CA / C
# ===========================================================================
REG = {
    "ac_power":     {"addr": 3004, "kind": "s32", "scale": 1.0,  "unit": "W"},    # 3004-3005 S32
    "energy_total": {"addr": 3008, "kind": "u32", "scale": 1.0,  "unit": "kWh"},  # 3008-3009 U32
    "energy_today": {"addr": 3014, "kind": "u16", "scale": 0.1,  "unit": "kWh"},
    "temperature":  {"addr": 3041, "kind": "s16", "scale": 0.1,  "unit": "degC"},
    "frequency":    {"addr": 3042, "kind": "u16", "scale": 0.01, "unit": "Hz"},
    "rated_power":  {"addr": 3109, "kind": "u16", "scale": 10.0, "unit": "W"},     # factory-rated
    "v_l1":         {"addr": 3033, "kind": "u16", "scale": 0.1,  "unit": "V"},
    "v_l2":         {"addr": 3034, "kind": "u16", "scale": 0.1,  "unit": "V"},
    "v_l3":         {"addr": 3035, "kind": "u16", "scale": 0.1,  "unit": "V"},
    "i_l1":         {"addr": 3036, "kind": "u16", "scale": 0.1,  "unit": "A"},
    "i_l2":         {"addr": 3037, "kind": "u16", "scale": 0.1,  "unit": "A"},
    "i_l3":         {"addr": 3038, "kind": "u16", "scale": 0.1,  "unit": "A"},
}

# ===========================================================================
# SOLIS ACTIVE-POWER-LIMIT CONTROL   (per Ginlong RS485 MODBUS doc)
#   switch 3069  (R/WS, 0xAA ON / 0x55 OFF, default 0xAA) enables regs 3051+3080.
#     -> FLASH-backed: doc warns against frequent writes, so we set it ONCE.
#   value  3051  (R/W,  raw = percent*100 ; 10000=100%, range 0..110%) <- set here
#   getpct 3049  (FC04 input image of the active-power %) <- read back here
# Absolute-W alternative (not used): 3080 "Active power set-point 1" S16, 10W/LSB.
# ===========================================================================
CTRL = {
    "limit_switch": {"addr": 3069, "on": 0xAA, "off": 0x55},  # FC03 read / FC06 write
    "limit_value":  {"addr": 3051, "value_scale": 0.01},      # FC06 write, raw=pct/0.01
    "limit_getpct": {"addr": 3049, "value_scale": 0.01},      # FC04 read-back
}

PRODUCT_ID   = 0xB0FE
PRODUCT_NAME = "Solis PV Inverter"
POLL_MS      = 2000


def resolve_rated(modbus, spec, fallback):
    """Turn a --rated value into a number of watts. 'auto' (or 0/blank) reads
    the factory nameplate from register 3109; anything else is used verbatim."""
    if str(spec).strip().lower() in ("auto", "", "0"):
        try:
            rated = modbus.read("rated_power")
            if rated and rated > 0:
                logging.info("Auto-detected rated power = %.0f W (reg 3109)", rated)
                return float(rated)
            logging.warning("Nameplate reg 3109 read back %.0f W; using "
                            "fallback %d W", rated, fallback)
        except Exception as e:
            logging.warning("Could not auto-detect rated power (%s); using "
                            "fallback %d W", e, fallback)
        return float(fallback)
    return float(spec)


class SolisModbus:
    """Single Solis RTU slave: read telemetry, write power limit."""

    def __init__(self, port, slave, baud):
        self.io = minimalmodbus.Instrument(port, slave)
        self.io.serial.baudrate = baud
        self.io.serial.bytesize = 8
        self.io.serial.parity   = minimalmodbus.serial.PARITY_NONE
        self.io.serial.stopbits = 1
        self.io.serial.timeout  = 1.0
        self.io.clear_buffers_before_each_transaction = True

    def close(self):
        try:
            self.io.serial.close()
        except Exception:
            pass

    # ---- read (FC04 input registers) ----
    def read(self, name):
        r = REG[name]
        k = r["kind"]
        if k in ("u16", "s16"):
            raw = self.io.read_register(r["addr"], functioncode=4, signed=(k == "s16"))
        else:
            raw = self.io.read_long(r["addr"], functioncode=4, signed=(k == "s32"))
        return raw * r["scale"]

    def identify(self, freq_range=(45.0, 65.0), power_range=(300.0, 200000.0)):
        """Heuristic 'is this a Solis at this slave?' check.
        Reads rated power + grid frequency over FC04 and sanity-checks them.
        Returns (ok, rated_w, freq_hz). Raises on comms failure (no response)."""
        freq = self.read("frequency")
        rated = self.read("rated_power")
        ok = (freq_range[0] <= freq <= freq_range[1] and
              power_range[0] <= rated <= power_range[1])
        return ok, rated, freq

    # ---- power-limit control ----
    def ensure_control_enabled(self):
        """Set the flash-backed switch (3069) to ON once. Default is already
        0xAA, so only write if it isn't, to spare the EEPROM."""
        sw = CTRL["limit_switch"]
        state = self.io.read_register(sw["addr"], functioncode=3)
        if state != sw["on"]:
            self.io.write_register(sw["addr"], sw["on"], functioncode=6)
            logging.info("Active-power switch 3069 set ON (was 0x%02X)", state)
        else:
            logging.info("Active-power switch 3069 already ON (0xAA)")

    def set_limit_percent(self, percent, max_percent=110.0):
        """Write the active-power limit to 3051 only (plain R/W, frequent-safe).
        percent None -> open up to max_percent (no EEPROM switch toggling)."""
        val = CTRL["limit_value"]
        if percent is None:
            percent = max_percent
        percent = max(0.0, min(max_percent, percent))
        raw = int(round(percent / val["value_scale"]))   # 110% -> 11000
        self.io.write_register(val["addr"], raw, functioncode=6)
        logging.info("Active-power limit set to %.1f%% (reg 3051 = %d)", percent, raw)

    def read_limit(self):
        sw, get = CTRL["limit_switch"], CTRL["limit_getpct"]
        state = self.io.read_register(sw["addr"], functioncode=3)
        raw = self.io.read_register(get["addr"], functioncode=4)   # 3049 input image
        return (state == sw["on"]), raw * get["value_scale"], state, raw

    def probe(self, start, count, fc):
        print(f"\nProbe registers {start}..{start + count - 1} (FC{fc:02d})\n")
        print(f"{'addr':>6} {'u16':>8} {'s16':>8} {'u32(this,next)':>16}")
        block, a = [], start
        while a < start + count:
            n = min(120, start + count - a)
            block += self.io.read_registers(a, n, functioncode=fc)
            a += n
        for i, v in enumerate(block):
            s16 = v - 0x10000 if v >= 0x8000 else v
            u32 = (v << 16) | block[i + 1] if i + 1 < len(block) else ""
            print(f"{start + i:>6} {v:>8} {s16:>8} {str(u32):>16}")


class SolisService:
    def __init__(self, args, modbus):
        self.args = args
        self.modbus = modbus
        self.phases = ["L1", "L2", "L3"][: args.phases]
        self.last_limit_w = None          # last value actually written
        self.svc = self._build()

    def _build(self):
        # Imports deferred so --identify / --probe / --set-limit don't need dbus.
        from dbus.mainloop.glib import DBusGMainLoop
        from vedbus import VeDbusService
        DBusGMainLoop(set_as_default=True)

        base = os.path.basename(self.args.serial)
        name = self.args.service_name or f"com.victronenergy.pvinverter.solis_{base}"
        try:
            svc = VeDbusService(name, register=False)
            deferred = True
        except TypeError:
            svc = VeDbusService(name)
            deferred = False

        serial_str = self.args.serial_number or f"SOLIS{self.args.slave:03d}"

        svc.add_path("/Mgmt/ProcessName", os.path.basename(__file__))
        svc.add_path("/Mgmt/ProcessVersion", "1.2")
        svc.add_path("/Mgmt/Connection", f"Modbus RTU {self.args.serial}:{self.args.slave}")
        svc.add_path("/DeviceInstance", self.args.instance)
        svc.add_path("/ProductId", PRODUCT_ID)
        svc.add_path("/ProductName", self.args.device_name or PRODUCT_NAME)
        svc.add_path("/FirmwareVersion", 0)
        svc.add_path("/Serial", serial_str)
        svc.add_path("/Connected", 1)
        svc.add_path("/Position", self.args.position)   # 1 = AC output
        svc.add_path("/StatusCode", 0)
        svc.add_path("/ErrorCode", 0)

        paths = ["/Ac/Power", "/Ac/Energy/Forward"]
        for ph in self.phases:
            paths += [f"/Ac/{ph}/Voltage", f"/Ac/{ph}/Current",
                      f"/Ac/{ph}/Power", f"/Ac/{ph}/Energy/Forward"]
        for p in paths:
            svc.add_path(p, None)

        # Writable power limit in watts. Node-RED / ESS writes here.
        # -1 (or >= rated) means "no limit". Only honoured with --enable-control.
        svc.add_path("/Ac/MaxPower", self.args.rated)
        svc.add_path("/Ac/PowerLimit", self.args.initial_limit, writeable=True,
                     onchangecallback=self._on_limit_change)

        if deferred:
            svc.register()
        return svc

    def _on_limit_change(self, path, value):
        if not self.args.enable_control:
            logging.warning("Ignoring /Ac/PowerLimit write %s: control disabled "
                            "(start with --enable-control to allow)", value)
            return False
        try:
            watts = None if value is None or value < 0 else float(value)
            pct = None if watts is None else 100.0 * watts / self.args.rated
            self.modbus.set_limit_percent(pct, self.args.max_percent)
            self.last_limit_w = watts
            return True            # accept the new dbus value
        except Exception as e:
            logging.warning("Failed to apply power limit %s: %s", value, e)
            return False           # reject -> dbus keeps old value

    def poll(self):
        try:
            power = self.modbus.read("ac_power")
            total = self.modbus.read("energy_total")
            self.svc["/Ac/Power"] = round(power, 1)
            self.svc["/Ac/Energy/Forward"] = round(total, 2)

            v = {"L1": self.modbus.read("v_l1")}
            i = {"L1": self.modbus.read("i_l1")}
            if "L2" in self.phases:
                v["L2"], i["L2"] = self.modbus.read("v_l2"), self.modbus.read("i_l2")
            if "L3" in self.phases:
                v["L3"], i["L3"] = self.modbus.read("v_l3"), self.modbus.read("i_l3")

            share = total / len(self.phases)
            for ph in self.phases:
                self.svc[f"/Ac/{ph}/Voltage"] = round(v[ph], 1)
                self.svc[f"/Ac/{ph}/Current"] = round(i[ph], 2)
                self.svc[f"/Ac/{ph}/Power"] = round(v[ph] * i[ph], 1)
                self.svc[f"/Ac/{ph}/Energy/Forward"] = round(share, 2)

            self.svc["/StatusCode"] = 7 if power > 5 else 8
            self.svc["/Connected"] = 1
            self.svc["/ErrorCode"] = 0

            # Optional watchdog re-assert for models that drop the limit.
            if (self.args.enable_control and self.args.reassert
                    and self.last_limit_w is not None):
                pct = 100.0 * self.last_limit_w / self.args.rated
                self.modbus.set_limit_percent(pct, self.args.max_percent)
        except Exception as e:
            logging.warning("Solis read failed: %s", e)
            self.svc["/Connected"] = 0
        return True

    def run(self):
        from gi.repository import GLib
        logging.info("Registered pvinverter instance %d (Position %d) on %s",  # noqa
                     self.args.instance, self.args.position, self.args.serial)
        try:
            rated = self.modbus.read("rated_power")
            logging.info("Factory-rated output power (reg 3109) = %.0f W; "
                         "using --rated %d W", rated, self.args.rated)
            if abs(rated - self.args.rated) > 0.1 * self.args.rated:
                logging.warning("--rated %d W differs >10%% from nameplate %.0f W; "
                                "check your %%<->W conversion", self.args.rated, rated)
        except Exception as e:
            logging.warning("Could not read factory-rated power: %s", e)

        if self.args.enable_control:
            try:
                self.modbus.ensure_control_enabled()
            except Exception as e:
                logging.warning("Could not enable active-power control: %s", e)

            if self.args.initial_limit is not None and self.args.initial_limit >= 0:
                try:
                    pct = 100.0 * self.args.initial_limit / self.args.rated
                    self.modbus.set_limit_percent(pct, self.args.max_percent)
                    self.last_limit_w = float(self.args.initial_limit)
                except Exception as e:
                    logging.warning("Could not apply initial limit: %s", e)

        self.poll()
        GLib.timeout_add(POLL_MS, self.poll)
        GLib.MainLoop().run()


def main():
    ap = argparse.ArgumentParser(description="Solis -> Venus OS dbus pvinverter + power limit")
    ap.add_argument("--serial", default="/dev/ttyUSB0")
    ap.add_argument("--slave", type=int, default=1)
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--instance", type=int, default=50)
    ap.add_argument("--phases", type=int, choices=(1, 3), default=1)
    ap.add_argument("--position", type=int, choices=(0, 1, 2), default=1,
                    help="0=AC input, 1=AC output (default), 2=AC input 2")
    ap.add_argument("--rated", default="auto",
                    help="inverter rated AC power in W (for %%<->W conversion). "
                         "'auto' (default) reads the nameplate from reg 3109.")
    ap.add_argument("--rated-fallback", type=int, default=5000,
                    help="rated W to use if --rated auto cannot read the inverter")
    ap.add_argument("--max-percent", type=float, default=100.0,
                    help="max allowed limit as %% of rated (default 100)")
    ap.add_argument("--initial-limit", type=float, default=-1,
                    help="limit in W to apply at startup (<0 = unlimited). "
                         "Only applied with --enable-control.")
    ap.add_argument("--enable-control", action="store_true",
                    help="allow Modbus WRITES (power limiting). Off by default: "
                         "the driver is read-only unless you pass this.")
    ap.add_argument("--reassert", type=int, default=0,
                    help="re-send the active limit every poll (0=off). Only if "
                         "your model drops the limit; wears EEPROM if the switch "
                         "reg is flash-backed. Requires --enable-control.")
    # Identity / labelling (normally supplied by solis_scan.py)
    ap.add_argument("--service-name", help="override dbus service name")
    ap.add_argument("--device-name", help="override /ProductName shown in GUI")
    ap.add_argument("--serial-number", help="override /Serial (VRM device id)")
    # One-shot / diagnostic modes
    ap.add_argument("--identify", action="store_true",
                    help="probe the port; exit 0 if a Solis answers, 1 otherwise")
    ap.add_argument("--set-limit", type=float, metavar="WATTS",
                    help="one-shot: write this limit (W, <0 = unlimited) and exit "
                         "(requires --enable-control)")
    ap.add_argument("--read-limit", action="store_true",
                    help="read current limit switch+value and exit")
    ap.add_argument("--probe", nargs=2, type=int, metavar=("START", "COUNT"),
                    help="dump registers and exit")
    ap.add_argument("--probe-fc", type=int, choices=(3, 4), default=4,
                    help="probe function code: 4=input (default), 3=holding")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    modbus = SolisModbus(args.serial, args.slave, args.baud)

    if args.identify:
        try:
            ok, rated, freq = modbus.identify()
            if ok:
                print(f"Solis detected on {args.serial} slave {args.slave}: "
                      f"rated={rated:.0f}W freq={freq:.2f}Hz")
                sys.exit(0)
            print(f"Device answered on {args.serial} slave {args.slave} but values "
                  f"look wrong (rated={rated:.0f}W freq={freq:.2f}Hz)")
            sys.exit(1)
        except Exception as e:
            print(f"No Solis on {args.serial} slave {args.slave}: {e}")
            sys.exit(1)

    if args.probe:
        modbus.probe(args.probe[0], args.probe[1], args.probe_fc)
        return
    if args.read_limit:
        en, pct, raw_sw, raw_v = modbus.read_limit()
        print(f"limiting={'ON' if en else 'OFF'}  limit={pct:.1f}%  "
              f"(switch raw=0x{raw_sw:02X}, value raw={raw_v})")
        return
    # Resolve rated power (nameplate auto-detect) for the modes that need it.
    args.rated = resolve_rated(modbus, args.rated, args.rated_fallback)

    if args.set_limit is not None:
        if not args.enable_control:
            sys.stderr.write("Refusing to write: pass --enable-control to allow "
                             "Modbus writes.\n")
            sys.exit(2)
        pct = None if args.set_limit < 0 else 100.0 * args.set_limit / args.rated
        modbus.set_limit_percent(pct, args.max_percent)
        return

    SolisService(args, modbus).run()


if __name__ == "__main__":
    main()
