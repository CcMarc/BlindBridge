#!/bin/sh
# One-time tightening for a Pi whose only job is BlindBridge. Safe to run again.
#
#   sh deploy/harden.sh             the three steps below
#   sh deploy/harden.sh --console   also turn the graphical desktop off (from the next restart)
#
# It asks for sudo. It does not touch SSH or the BlindBridge services.
set -eu

CONSOLE=no
[ "${1:-}" = "--console" ] && CONSOLE=yes

echo "1. Turning off network services BlindBridge does not use"
for unit in rpcbind.socket rpcbind.service nfs-blkmap.service; do
  if systemctl list-unit-files "$unit" 2>/dev/null | grep -q "^$unit"; then
    sudo systemctl disable --now "$unit" >/dev/null 2>&1 || true
    echo "   off: $unit"
  fi
done

echo "2. Automatic security updates"
if dpkg -s unattended-upgrades >/dev/null 2>&1; then
  echo "   already installed"
else
  sudo apt-get update -qq
  sudo apt-get install -y -qq unattended-upgrades
  echo "   installed"
fi

echo "3. Bluetooth: not discoverable, and refuse pairing requests"
bluetoothctl discoverable off >/dev/null 2>&1 || true
bluetoothctl pairable off >/dev/null 2>&1 || true
bluetoothctl show 2>/dev/null | grep -E "Discoverable:|Pairable:" | sed 's/^[[:space:]]*/   /'

if [ "$CONSOLE" = "yes" ]; then
  echo "4. Graphical desktop off from the next restart"
  sudo systemctl set-default multi-user.target >/dev/null
fi

echo
echo "Still listening for connections (expected: 22 = SSH, 8787 = blinds page, 5353 = the .local name):"
ss -tulnH | awk '{print "   " $1, $5}' | sort -u
