# solis-usb-victron

Auto-discovering **Solis (Ginlong) PV inverter** driver for **Victron Venus OS**
(Cerbo GX, Raspberry Pi Venus, etc.), over a USB → RS485 adapter.

It presents each inverter on the Venus dbus as a native
`com.victronenergy.pvinverter` device, so it shows up in the GX display, the
Device List, and VRM as a PV Inverter — no extra configuration per unit.

- **Auto-discovery over USB** — a supervisor service scans every USB serial
  port, Modbus-probes it, and starts a driver for each Solis it finds. Plug an
  inverter in and it appears; unplug it and it goes away.
- **Installable service** — one command installs a daemontools service under
  `/data`, so it starts at boot and **survives Venus OS firmware updates**.
- **Read-only by default** — no register is ever written unless you explicitly
  enable power-limit control.
- **Multiple inverters** — one USB adapter each, or several on one RS485 bus by
  listing their slave ids. Each gets a stable VRM device instance tied to its
  physical USB socket.

---

## Architecture

```
            USB-RS485                         Venus OS dbus
 Solis  ───────────────►  /dev/ttyUSB0  ──►  com.victronenergy.pvinverter.*
 Solis  ───────────────►  /dev/ttyUSB1  ──►  (one dbus service per inverter)

 solis_scan.py  (one service: /service/dbus-solis)
     scans ports → probes for Solis → launches & supervises:
 dbus-solis.py  (one process per inverter: telemetry + optional limiting)
```

- `solis_scan.py` — discovery supervisor. Reads `conf/solis.conf`.
- `dbus-solis.py` — per-inverter driver. Also usable standalone for testing.

---

## Install (on the GX device)

SSH into the GX as `root`, then run one line:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/Sean-Oelofse/usb-solis-victron/main/install.sh)
```

That fetches the project into `/data/dbus-solis` (via git, or a tarball if git
isn't present), fetches Victron's `velib_python`, installs `minimalmodbus`,
links the service into `/service`, and adds a `/data/rc.local` boot hook so it
re-links after firmware updates.

Prefer to clone it yourself?

```sh
git clone https://github.com/Sean-Oelofse/usb-solis-victron /data/dbus-solis
/data/dbus-solis/install.sh
```

Re-running the one-liner later updates an existing install to the latest
version before re-installing.

Watch it work:

```sh
tail -F /var/log/dbus-solis/current
```

Within a few seconds you should see `Solis found on /dev/ttyUSB0 ...` and a
driver start. The inverter then appears in **Settings → Device List**.

### Requirements
- Venus OS (any GX device or a Raspberry Pi running Venus).
- A USB → RS485 adapter (CH340 / FTDI) wired to the inverter's RS485 / COM
  port (Solis "Logger"/COM terminal; A/B as per the inverter manual).
- Internet on the GX for the one-time `velib_python` / `minimalmodbus` fetch.

---

## Configure

Edit `/data/dbus-solis/conf/solis.conf`, then restart:

```sh
svc -t /service/dbus-solis
```

Key options:

| Option | Default | Meaning |
|---|---|---|
| `baud` | `9600` | RS485 baud rate |
| `slaves` | `1` | Modbus unit ids to try (comma list for a shared bus) |
| `phases` | `1` | `1` single-phase, `3` three-phase |
| `rated` | `auto` | rated AC power — `auto` reads the nameplate (reg 3109), or set W |
| `ports` | *(empty)* | empty = auto-scan; or pin `/dev/ttyUSB0,/dev/ttyUSB1` |
| `enable_control` | `0` | `1` allows **writes** (power limiting) |
| `instance_base` | `40` | DeviceInstance numbering start |
| `scan_interval` | `20` | seconds between USB rescans |

Full documentation is inline in `conf/solis.conf`.

---

## Power limiting (optional, writes to the inverter)

Disabled by default. The register map for the active-power limit (`CTRL` in
`dbus-solis.py`) is the common Solis S5/S6 layout but **must be verified against
the Ginlong "RS485 Modbus-RTU protocol" document for your exact model/firmware**
before you enable writes.

To enable: set `enable_control = 1` in `solis.conf`. A writable
`/Ac/PowerLimit` (watts; `-1` = unlimited) then appears on each inverter's dbus
service, which Node-RED / ESS logic can drive.

---

## Verify a single inverter by hand

Before trusting auto-discovery you can poke one port directly:

```sh
cd /data/dbus-solis

# Confirm a Solis answers (exit 0 = yes):
./dbus-solis.py --serial /dev/ttyUSB0 --slave 1 --identify

# Dump input registers around the power/energy block:
./dbus-solis.py --serial /dev/ttyUSB0 --probe 3000 60

# Dump holding registers (control block):
./dbus-solis.py --serial /dev/ttyUSB0 --probe-fc 3 --probe 3040 40

# Read the current power limit:
./dbus-solis.py --serial /dev/ttyUSB0 --read-limit

# One-shot limit test (requires explicit opt-in to writes):
./dbus-solis.py --serial /dev/ttyUSB0 --enable-control --rated 5000 --set-limit 3000
```

Use `--probe` to line the register values up against the inverter's own display
or SolisCloud, then correct `REG` / `CTRL` at the top of `dbus-solis.py` if your
model differs.

---

## Service management

```sh
svc -d /service/dbus-solis      # stop
svc -u /service/dbus-solis      # start
svc -t /service/dbus-solis      # restart (after editing config)
tail -F /var/log/dbus-solis/current
```

## Uninstall

```sh
/data/dbus-solis/uninstall.sh
```

---

## Notes & limitations

- **Auto-detection is heuristic.** Generic CH340/FTDI adapters share a USB
  VID:PID, so the inverter can't be identified from the adapter alone — the
  supervisor instead probes each port over Modbus (grid frequency + rated power
  sanity-checked against the `freq_*` / `power_*` bounds). If you run *other*
  Modbus-RTU devices (e.g. an energy meter) on their own USB adapters, pin the
  Solis ports explicitly with `ports =` in the config to avoid a mis-probe.
- Ports already claimed by Victron's own `serial-starter` (VE.Direct, GPS,
  meters) are skipped while `respect_serial_starter = 1`.
- **Device identity across reboots.** `/DeviceInstance` is keyed to the physical
  USB socket (persisted in `conf/instances.json`), *not* the `ttyUSBx` number —
  so a reboot, even one that renumbers `ttyUSB0`↔`ttyUSB1`, keeps the same VRM
  device. Moving the adapter to a different USB port or changing a hub makes it
  look like a new device. To make identity fully port-independent, set
  `serial_reg` in the config to key on the inverter's own serial number instead.
- The `REG` / `CTRL` maps are the documented Solis S5/S6 layout. **Verify them
  against the Ginlong RS485 Modbus doc for your model before relying on the
  telemetry or enabling control.**
