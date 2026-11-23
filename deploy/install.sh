#!/bin/sh
# Sets BlindBridge up on a Raspberry Pi: a Python environment with the
# Bluetooth library, and two system services (server and bridge).
#
# Run it as your normal user from anywhere:   sh deploy/install.sh
# It asks for sudo to write the two service files and one permission rule.
#
#   sh deploy/install.sh              both services (one Pi does everything)
#   sh deploy/install.sh bridge       bridge only (a second Pi in another room)
#   sh deploy/install.sh server       server only
set -eu

WHAT="${1:-both}"
case "$WHAT" in
  both|bridge|server) ;;
  *) echo "usage: sh deploy/install.sh [both|bridge|server]" >&2; exit 2 ;;
esac

DIR=$(cd "$(dirname "$0")/.." && pwd)
RUN_AS=$(id -un)
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

if [ "$RUN_AS" = "root" ]; then
  echo "Run this as your normal user, not root." >&2
  exit 1
fi

if [ "$WHAT" != "server" ]; then
  echo "Creating Python environment in $DIR/venv"
  if ! python3 -m venv "$DIR/venv" 2>/dev/null; then
    echo "python3-venv is missing. Install it with: sudo apt install python3-venv" >&2
    exit 1
  fi
  # The bridge is written against the 3.x line of bleak, the Bluetooth library.
  BLEAK='bleak>=3.1,<4'
  "$DIR/venv/bin/pip" install --quiet "$BLEAK" || PIP_CONFIG_FILE=/dev/null "$DIR/venv/bin/pip" install --quiet "$BLEAK"
  echo "Bluetooth library installed: $("$DIR/venv/bin/python" -c 'import importlib.metadata as m; print(m.version("bleak"))')"
fi

write_unit() {
  # $1 = unit name, stdin = contents
  sudo tee "$UNIT_DIR/$1" >/dev/null
  echo "Wrote $UNIT_DIR/$1"
}

if [ "$WHAT" != "bridge" ]; then
  write_unit blindbridge-server.service <<EOF
[Unit]
Description=BlindBridge server
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR/server
ExecStart=/usr/bin/python3 $DIR/server/server.py run
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
fi

if [ "$WHAT" != "server" ]; then
  write_unit blindbridge-bridge.service <<EOF
[Unit]
Description=BlindBridge bridge (Bluetooth to blinds)
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR/bridge
Environment=PYTHONUNBUFFERED=1
ExecStartPre=+/usr/sbin/rfkill unblock bluetooth
ExecStart=$DIR/venv/bin/python $DIR/bridge/bridge.py run
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
fi

if [ "$WHAT" != "bridge" ]; then
  # Let the web page's "Restart Pi" and "Shut down Pi" buttons work. This polkit
  # rule lets this one user ask the system to power off or reboot. It grants
  # nothing else, and it needs no sudo at run time.
  RULES_DIR="${POLKIT_RULES_DIR:-/etc/polkit-1/rules.d}"
  if sudo test -d "$RULES_DIR"; then
    sudo tee "$RULES_DIR/49-blindbridge-power.rules" >/dev/null <<RULE
// Installed by BlindBridge (deploy/install.sh). Delete this file to turn the
// web page's power buttons off again.
polkit.addRule(function(action, subject) {
    var allowed = [
        "org.freedesktop.login1.power-off",
        "org.freedesktop.login1.power-off-multiple-sessions",
        "org.freedesktop.login1.reboot",
        "org.freedesktop.login1.reboot-multiple-sessions"
    ];
    if (subject.user == "$RUN_AS" && allowed.indexOf(action.id) >= 0) {
        return polkit.Result.YES;
    }
});
RULE
    echo "Power buttons enabled for $RUN_AS"
  else
    echo "No polkit rules folder found, so the power buttons stay off." >&2
  fi
fi

if [ "$WHAT" != "server" ] && ! id -nG | tr ' ' '\n' | grep -qx netdev; then
  echo "Note: $RUN_AS is not in the netdev group, so the page's Wi-Fi setting will be unavailable."
  echo "      To allow it: sudo usermod -aG netdev $RUN_AS   (then restart the bridge)"
fi

sudo systemctl daemon-reload
echo
echo "Installed. The services are not started yet."
echo "Finish the setup in the README first, then start them with:"
[ "$WHAT" != "bridge" ] && echo "  sudo systemctl enable --now blindbridge-server"
[ "$WHAT" != "server" ] && echo "  sudo systemctl enable --now blindbridge-bridge"
exit 0
