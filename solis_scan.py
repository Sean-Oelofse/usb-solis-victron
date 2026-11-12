#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
solis_scan.py  -  USB auto-discovery supervisor for Solis inverters on Venus OS.

Runs as a single daemontools service. It:
  * scans /dev/ttyUSB* (+ /dev/ttyACM*) for serial ports,
  * skips ports already claimed by Victron's serial-starter or that are busy,
  * Modbus-probes each remaining port for a Solis inverter,
  * launches ONE dbus-solis.py process per inverter it finds, with a stable
    /DeviceInstance derived from the physical USB socket (so VRM keeps the same
    device across reboots and replugs),
  * keeps rescanning: starts drivers for newly plugged inverters and restarts
    any that exit.

Config: conf/solis.conf (key=value). See that file for every option.

No serial-starter edits, no rootfs changes. Everything lives under /data, so it
survives Venus OS firmware updates (re-linked at boot by /data/rc.local).
"""

import os
import re
import sys
import glob
import json
import time
import signal
import logging
import argparse
import subprocess

try:
    import serial  # pyserial, shipped with minimalmodbus
except ImportError:
    serial = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import minimalmodbus  # noqa: E402  (fail loudly if missing; install.sh handles it)

DRIVER = os.path.join(HERE, "dbus-solis.py")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULTS = {
    "baud":           "9600",
    "slaves":         "1",          # comma list of Modbus slave ids to try
    "phases":         "1",          # 1 or 3
    "rated":          "auto",       # 'auto' = read nameplate (reg 3109); or set W
    "rated_fallback": "5000",       # W to use if auto-detect fails
    "position":       "1",          # 0=AC in, 1=AC out, 2=AC in 2
    "enable_control": "0",          # 1 = allow power-limit WRITES
    "initial_limit":  "-1",         # W at startup (<0 unlimited), control only
    "max_percent":    "100",
    "reassert":       "0",
    "scan_interval":  "20",         # seconds between rescans
    "ports":          "",           # explicit list disables auto-scan, e.g.
                                    #   /dev/ttyUSB0,/dev/ttyUSB1
    "instance_base":  "40",         # DeviceInstance numbering starts here
    "freq_min":       "45",
    "freq_max":       "65",
    "power_min":      "300",
    "power_max":      "200000",
    "respect_serial_starter": "1",  # skip ports Victron drivers already run on
    # Optional: identify inverters by their own Modbus serial number instead of
    # the USB socket, so DeviceInstance survives moving the adapter to any port.
    "serial_reg":     "",           # FC04 input reg where ASCII serial starts
    "serial_regs":    "4",          # number of 16-bit regs it spans (2 chars ea)
}


def decode_ascii(regs):
    """Decode a list of 16-bit registers as big-endian ASCII (Solis serials)."""
    out = bytearray()
    for r in regs:
        out.append((r >> 8) & 0xFF)
        out.append(r & 0xFF)
    text = "".join(chr(b) for b in out if 32 <= b < 127).strip()
    return text or None


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split("#", 1)[0].strip()   # drop inline comments
                cfg[k.strip()] = v
    return cfg


# ---------------------------------------------------------------------------
# Stable per-socket DeviceInstance mapping (persisted under conf/)
# ---------------------------------------------------------------------------
class InstanceMap:
    def __init__(self, path, base):
        self.path = path
        self.base = base
        self.map = {}
        try:
            with open(path) as fh:
                self.map = json.load(fh)
        except Exception:
            self.map = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.map, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as e:
            logging.warning("Could not persist instance map: %s", e)

    def get(self, usb_id):
        if usb_id in self.map:
            return self.map[usb_id]
        used = set(self.map.values())
        inst = self.base
        while inst in used:
            inst += 1
        self.map[usb_id] = inst
        self._save()
        return inst


def usb_socket_id(tty):
    """Stable id for the physical USB socket behind a tty, so the same inverter
    in the same port keeps its DeviceInstance. Falls back to the tty name."""
    base = os.path.basename(tty)
    try:
        link = os.path.realpath("/sys/class/tty/%s/device" % base)
        # e.g. .../usb1/1-1/1-1.2/1-1.2:1.0  ->  take "1-1.2"
        m = re.search(r"/(\d+-[\d.]+):\d", link)
        if m:
            return m.group(1)
        parts = [p for p in link.split("/") if re.match(r"^\d+-[\d.]+$", p)]
        if parts:
            return parts[-1]
    except Exception:
        pass
    return base


# ---------------------------------------------------------------------------
# Port filtering
# ---------------------------------------------------------------------------
def list_serial_ports():
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return ports


def owned_by_serial_starter(tty):
    """True if a Victron serial-starter service is already running on this tty
    (e.g. /service/vedirect.ttyUSB0, /service/gps.ttyUSB1)."""
    base = os.path.basename(tty)
    for svc in glob.glob("/service/*." + base):
        return True
    return False


def port_busy(tty):
    if serial is None:
        return False
    try:
        s = serial.Serial(tty)
        s.close()
        return False
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
class Supervisor:
    def __init__(self, cfg, conf_dir):
        self.cfg = cfg
        self.children = {}           # tty -> subprocess.Popen
        self.meta = {}               # tty -> dict(instance, slave, usb_id)
        self.stop = False
        self.imap = InstanceMap(
            os.path.join(conf_dir, "instances.json"),
            int(cfg["instance_base"]))
        self.slaves = [int(x) for x in cfg["slaves"].split(",") if x.strip()]
        self.explicit_ports = [p.strip() for p in cfg["ports"].split(",") if p.strip()]
        self.freq_range = (float(cfg["freq_min"]), float(cfg["freq_max"]))
        self.power_range = (float(cfg["power_min"]), float(cfg["power_max"]))
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, *_):
        logging.info("Shutting down, stopping %d driver(s)...", len(self.children))
        self.stop = True

    # ---- probe one port for a Solis; return (slave, rated_w, serial) or None ----
    def probe_port(self, tty):
        for slave in self.slaves:
            io = None
            try:
                io = minimalmodbus.Instrument(tty, slave)
                io.serial.baudrate = int(self.cfg["baud"])
                io.serial.timeout = 0.6
                io.clear_buffers_before_each_transaction = True
                freq = io.read_register(
                    3042, functioncode=4) * 0.01   # grid frequency (doc 3043)
                rated = io.read_long(
                    3699, functioncode=4)          # rated power W, U32 (doc 3700-3701)
                ok = (self.freq_range[0] <= freq <= self.freq_range[1] and
                      self.power_range[0] <= rated <= self.power_range[1])
                if ok:
                    serial = self._read_serial(io)
                    logging.info("Solis found on %s slave %d (rated=%.0fW, "
                                 "freq=%.2fHz, serial=%s)", tty, slave, rated,
                                 freq, serial or "n/a")
                    return slave, rated, serial
            except Exception:
                pass
            finally:
                if io is not None:
                    try:
                        io.serial.close()
                    except Exception:
                        pass
        return None

    def _read_serial(self, io):
        """Read the inverter's own serial number, if a register is configured."""
        reg = self.cfg["serial_reg"].strip()
        if not reg:
            return None
        try:
            regs = io.read_registers(
                int(reg), int(self.cfg["serial_regs"]), functioncode=4)
            return decode_ascii(regs)
        except Exception as e:
            logging.warning("Could not read serial from reg %s: %s", reg, e)
            return None

    def rated_for(self, detected):
        """Pick the rated power to hand the driver: config value if the user
        pinned one, otherwise the nameplate we just read off the inverter."""
        spec = self.cfg["rated"].strip().lower()
        if spec in ("auto", "", "0"):
            if detected and detected > 0:
                return int(round(detected))
            return int(self.cfg["rated_fallback"])
        return int(float(spec))

    # ---- launch a driver for a confirmed inverter ----
    def launch(self, tty, slave, rated, serial=None):
        usb_id = usb_socket_id(tty)
        base = os.path.basename(tty)
        rated_w = self.rated_for(rated)

        # Identity key for the stable DeviceInstance. Prefer the inverter's own
        # serial (port-independent); otherwise the physical USB socket.
        if serial:
            ident_key = "sn:" + serial
            serial_str = serial
        else:
            ident_key = "usb:" + usb_id
            serial_str = "SOLIS-%s" % usb_id.replace("/", "_")
        instance = self.imap.get(ident_key)

        cmd = [
            sys.executable, DRIVER,
            "--serial", tty,
            "--slave", str(slave),
            "--baud", self.cfg["baud"],
            "--instance", str(instance),
            "--phases", self.cfg["phases"],
            "--position", self.cfg["position"],
            "--rated", str(rated_w),
            "--max-percent", self.cfg["max_percent"],
            "--initial-limit", self.cfg["initial_limit"],
            "--reassert", self.cfg["reassert"],
            "--serial-number", serial_str,
            "--service-name", "com.victronenergy.pvinverter.solis_%s" % base,
        ]
        if self.cfg["enable_control"] == "1":
            cmd.append("--enable-control")
        logging.info("Starting driver for %s (instance %d, rated %dW): %s",
                     tty, instance, rated_w, " ".join(cmd))
        proc = subprocess.Popen(cmd)
        self.children[tty] = proc
        self.meta[tty] = {"instance": instance, "slave": slave, "usb_id": usb_id}

    def reap(self):
        for tty in list(self.children):
            proc = self.children[tty]
            if proc.poll() is not None:
                logging.warning("Driver for %s exited (code %s); will rescan",
                                tty, proc.returncode)
                del self.children[tty]
                self.meta.pop(tty, None)

    def scan(self):
        ports = self.explicit_ports or list_serial_ports()
        present = set(ports)

        # Drop drivers whose port vanished (adapter unplugged).
        for tty in list(self.children):
            if tty not in present:
                logging.info("%s disappeared; stopping its driver", tty)
                self._terminate(tty)

        for tty in ports:
            if tty in self.children:
                continue
            if (self.cfg["respect_serial_starter"] == "1"
                    and owned_by_serial_starter(tty)):
                continue
            if not self.explicit_ports and port_busy(tty):
                continue
            found = self.probe_port(tty)
            if found is not None:
                slave, rated, serial = found
                self.launch(tty, slave, rated, serial)

    def _terminate(self, tty):
        proc = self.children.pop(tty, None)
        self.meta.pop(tty, None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def shutdown(self):
        for tty in list(self.children):
            self._terminate(tty)

    def run(self):
        interval = max(5, int(self.cfg["scan_interval"]))
        logging.info("Solis USB supervisor started. slaves=%s baud=%s "
                     "scan_interval=%ds control=%s", self.slaves,
                     self.cfg["baud"], interval,
                     "ON" if self.cfg["enable_control"] == "1" else "off")
        while not self.stop:
            try:
                self.reap()
                self.scan()
            except Exception as e:
                logging.exception("scan loop error: %s", e)
            # sleep in small steps so SIGTERM is handled promptly
            for _ in range(interval * 2):
                if self.stop:
                    break
                time.sleep(0.5)
        self.shutdown()
        logging.info("Supervisor stopped.")


def main():
    ap = argparse.ArgumentParser(description="Auto-discover Solis inverters on USB")
    ap.add_argument("--config", default=os.path.join(HERE, "conf", "solis.conf"))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s [scan] %(message)s")

    cfg = load_config(args.config)
    conf_dir = os.path.dirname(os.path.abspath(args.config))
    Supervisor(cfg, conf_dir).run()


if __name__ == "__main__":
    main()
