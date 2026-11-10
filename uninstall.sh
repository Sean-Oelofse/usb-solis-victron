#!/bin/sh
# ---------------------------------------------------------------------------
# uninstall.sh  -  stop and remove the Solis USB service.
# Leaves the files in place; delete the directory yourself if you want them gone.
# ---------------------------------------------------------------------------
DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

echo "Stopping service..."
svc -d /service/dbus-solis 2>/dev/null
svc -d /service/dbus-solis/log 2>/dev/null
rm -f /service/dbus-solis

# Remove the boot hook from /data/rc.local
if [ -f /data/rc.local ]; then
    sed -i '/dbus-solis auto-start/d; /dbus-solis\/service/d' /data/rc.local
fi

# Make sure any driver children are gone.
pkill -f "$DIR/solis_scan.py" 2>/dev/null
pkill -f "$DIR/dbus-solis.py" 2>/dev/null

echo "Uninstalled. Files remain in $DIR"
