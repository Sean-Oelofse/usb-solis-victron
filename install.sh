#!/bin/sh
# ---------------------------------------------------------------------------
# install.sh  -  self-bootstrapping installer for the Solis USB driver on
#                Venus OS. Works two ways:
#
#   1) Straight off the net (nothing cloned yet):
#        bash <(curl -fsSL https://raw.githubusercontent.com/Sean-Oelofse/usb-solis-victron/main/install.sh)
#
#   2) From a local clone:
#        git clone https://github.com/Sean-Oelofse/usb-solis-victron /data/dbus-solis
#        /data/dbus-solis/install.sh
#
# In mode 1 it fetches the project into /data/dbus-solis (git, or a tarball if
# git is missing), then installs from there. Everything lives under /data so it
# survives Venus OS firmware updates.
# ---------------------------------------------------------------------------
set -e

REPO_USER=Sean-Oelofse
REPO_NAME=usb-solis-victron
BRANCH=main
TARGET=/data/dbus-solis

REPO_URL="https://github.com/$REPO_USER/$REPO_NAME"
TARBALL="https://codeload.github.com/$REPO_USER/$REPO_NAME/tar.gz/refs/heads/$BRANCH"
VELIB_URL="https://github.com/victronenergy/velib_python"
VELIB_TARBALL="https://codeload.github.com/victronenergy/velib_python/tar.gz/refs/heads/master"

have() { command -v "$1" >/dev/null 2>&1; }

download() {   # download <url> <outfile>
    if have curl; then curl -fsSL "$1" -o "$2"
    elif have wget; then wget -qO "$2" "$1"
    else echo "ERROR: need curl or wget to download $1" >&2; return 1
    fi
}

fetch_into() {   # fetch_into <giturl> <tarball> <branch> <dest>
    _git="$1"; _tar="$2"; _br="$3"; _dst="$4"
    if have git; then
        git clone --depth 1 -b "$_br" "$_git" "$_dst"
    else
        echo "git not found; downloading tarball..."
        _tmp=$(mktemp -d)
        download "$_tar" "$_tmp/src.tgz"
        mkdir -p "$_dst"
        tar -xzf "$_tmp/src.tgz" -C "$_dst" --strip-components=1
        rm -rf "$_tmp"
    fi
}

# --- 0. locate ourselves: running from a clone, or piped from the net? ------
SELF="${BASH_SOURCE:-$0}"
SRCDIR=""
case "$SELF" in
    /dev/fd/*|/proc/*|bash|sh|-*|"") : ;;          # piped: no real file on disk
    *)  _d=$(CDPATH= cd -- "$(dirname -- "$SELF")" 2>/dev/null && pwd || true)
        [ -n "$_d" ] && [ -f "$_d/solis_scan.py" ] && SRCDIR="$_d" ;;
esac

if [ -n "$SRCDIR" ]; then
    DIR="$SRCDIR"                       # installing in place from a clone
    echo "Installing from local checkout: $DIR"
elif [ -f "$TARGET/solis_scan.py" ]; then
    DIR="$TARGET"                       # already present; refresh if it's a git repo
    echo "Found existing install at $DIR"
    if [ -d "$DIR/.git" ] && have git; then
        echo "Updating to latest $BRANCH..."
        git -C "$DIR" fetch --depth 1 origin "$BRANCH" || true
        git -C "$DIR" reset --hard "origin/$BRANCH" || true
    fi
else
    echo "Fetching $REPO_NAME into $TARGET ..."
    mkdir -p "$(dirname "$TARGET")"
    fetch_into "$REPO_URL" "$TARBALL" "$BRANCH" "$TARGET"
    DIR="$TARGET"
fi

cd "$DIR"

# --- 1. velib_python (vedbus) ----------------------------------------------
if [ ! -f "$DIR/ext/velib_python/vedbus.py" ]; then
    echo "Fetching velib_python..."
    fetch_into "$VELIB_URL" "$VELIB_TARBALL" master "$DIR/ext/velib_python"
fi

# --- 2. minimalmodbus (pure python, pulls pyserial) ------------------------
if ! python3 -c 'import minimalmodbus' 2>/dev/null; then
    echo "Installing minimalmodbus..."
    pip3 install minimalmodbus 2>/dev/null \
        || python3 -m pip install minimalmodbus 2>/dev/null \
        || echo "WARN: could not auto-install minimalmodbus. Install it manually:
      pip3 install minimalmodbus
   (or vendor it into $DIR/ext so it is importable)."
fi

# --- 3. permissions + log dir ----------------------------------------------
chmod +x "$DIR/dbus-solis.py" "$DIR/solis_scan.py" \
         "$DIR/service/run" "$DIR/service/log/run" 2>/dev/null || true
mkdir -p /var/log/dbus-solis

# --- 4. register the daemontools service -----------------------------------
# /service is where Venus OS's svscan looks. A symlink is picked up in ~5s.
ln -sfn "$DIR/service" /service/dbus-solis

# --- 5. survive firmware updates -------------------------------------------
# /data persists across updates; /service does not. Re-link at every boot.
RCL=/data/rc.local
[ -f "$RCL" ] || printf '#!/bin/sh\n' > "$RCL"
chmod +x "$RCL"
if ! grep -q "dbus-solis/service" "$RCL" 2>/dev/null; then
    printf '\n# dbus-solis auto-start (added by install.sh)\nln -sfn %s/service /service/dbus-solis\n' "$DIR" >> "$RCL"
    echo "Added boot hook to $RCL"
fi

echo
echo "Installed to $DIR. The supervisor starts within ~5 seconds and will"
echo "auto-detect any Solis inverter on a USB-RS485 adapter."
echo "  Logs:    tail -F /var/log/dbus-solis/current"
echo "  Config:  $DIR/conf/solis.conf   (restart: svc -t /service/dbus-solis)"
echo "  Stop:    svc -d /service/dbus-solis"
echo "  Remove:  $DIR/uninstall.sh"
