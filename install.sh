#!/bin/sh
# ---------------------------------------------------------------------------
# install.sh  -  install the Solis USB driver as a Venus OS service.
#
# Recommended location: /data/dbus-solis  (so it survives firmware updates).
#   ssh root@venus
#   git clone <your-repo> /data/dbus-solis
#   /data/dbus-solis/install.sh
# ---------------------------------------------------------------------------
set -e

DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
echo "Installing dbus-solis from $DIR"

# 1. velib_python (vedbus) ---------------------------------------------------
if [ ! -f "$DIR/ext/velib_python/vedbus.py" ]; then
    echo "Cloning velib_python..."
    git clone https://github.com/victronenergy/velib_python "$DIR/ext/velib_python"
fi

# 2. minimalmodbus (pure python, pulls pyserial) -----------------------------
if ! python3 -c 'import minimalmodbus' 2>/dev/null; then
    echo "Installing minimalmodbus..."
    pip3 install minimalmodbus 2>/dev/null \
        || python3 -m pip install minimalmodbus 2>/dev/null \
        || echo "WARN: could not auto-install minimalmodbus. Install it manually:
      pip3 install minimalmodbus
   (or vendor it into $DIR/ext so it is importable)."
fi

# 3. permissions + log dir ---------------------------------------------------
chmod +x "$DIR/dbus-solis.py" "$DIR/solis_scan.py" \
         "$DIR/service/run" "$DIR/service/log/run"
mkdir -p /var/log/dbus-solis

# 4. register the daemontools service ---------------------------------------
# /service is where Venus OS's svscan looks. A symlink is picked up in ~5s.
ln -sfn "$DIR/service" /service/dbus-solis

# 5. survive firmware updates ------------------------------------------------
# /data persists across updates; /service does not. Re-link at every boot.
RCL=/data/rc.local
[ -f "$RCL" ] || { printf '#!/bin/sh\n' > "$RCL"; }
chmod +x "$RCL"
if ! grep -q "dbus-solis/service" "$RCL" 2>/dev/null; then
    printf '\n# dbus-solis auto-start (added by install.sh)\nln -sfn %s/service /service/dbus-solis\n' "$DIR" >> "$RCL"
    echo "Added boot hook to $RCL"
fi

echo
echo "Installed. The supervisor starts within ~5 seconds and will auto-detect"
echo "any Solis inverter on a USB-RS485 adapter."
echo "  Logs:    tail -F /var/log/dbus-solis/current"
echo "  Config:  $DIR/conf/solis.conf   (restart: svc -t /service/dbus-solis)"
echo "  Stop:    svc -d /service/dbus-solis"
