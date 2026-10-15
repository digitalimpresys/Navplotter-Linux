#!/bin/bash
# Installs or updates the NavPlotter node on Raspberry Pi OS (Zero 2 W, Pi 4, Pi 5):
#   curl -sSL https://raw.githubusercontent.com/digitalimpresys/Navplotter-Linux/main/install.sh | sudo bash
# or, from a copy of the repository:  sudo ./install.sh
set -euo pipefail

REPO=https://github.com/digitalimpresys/Navplotter-Linux.git
BRANCH=${NAVPLOTTER_BRANCH:-main}
DEST=/opt/navplotter
DATA=/var/lib/navplotter

if [ "$(id -u)" != 0 ]; then echo "Run it with sudo"; exit 1; fi

echo "== Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-serial git >/dev/null

echo "== Program in $DEST"
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd); fi
if [ -n "$HERE" ] && [ -d "$HERE/navplotter" ] && [ "$HERE" != "$DEST" ]; then
  mkdir -p "$DEST"
  cp -r "$HERE/navplotter" "$HERE/web" "$HERE/systemd" "$DEST/"
elif [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch -q origin "$BRANCH" && git -C "$DEST" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  rm -rf "$DEST"
  git clone -q -b "$BRANCH" "$REPO" "$DEST"
fi

echo "== User navplotter (serial port access)"
id navplotter >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin navplotter
usermod -aG dialout navplotter
mkdir -p "$DATA"
chown -R navplotter:navplotter "$DATA"

echo "== Fixed name for the Heltec V2 modem: /dev/navplotter-modem"
cat > /etc/udev/rules.d/99-navplotter-modem.rules <<'RULE'
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="navplotter-modem", GROUP="dialout", MODE="0660"
RULE
udevadm control --reload-rules && udevadm trigger || true

if [ ! -f "$DATA/settings.json" ]; then
  echo '{"modem": "/dev/navplotter-modem"}' > "$DATA/settings.json"
  chown navplotter:navplotter "$DATA/settings.json"
fi

echo "== Service"
install -m 644 "$DEST/systemd/navplotter.service" /etc/systemd/system/navplotter.service
systemctl daemon-reload
systemctl enable -q navplotter
systemctl restart navplotter

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "NavPlotter is running. Web app: http://${IP:-$(hostname).local}/"
echo "Settings: $DATA/settings.json   Log: journalctl -u navplotter -f"
