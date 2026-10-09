#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOTD_SCRIPT="$SCRIPT_DIR/motd-fastfetch.sh"

if [[ ! -f "$MOTD_SCRIPT" ]]; then
  echo "MOTD script not found: $MOTD_SCRIPT" >&2
  exit 1
fi

install_motd="${MESHCORE_MOTD:-}"
if [[ -z "$install_motd" ]]; then
  board_model=""
  if [[ -r /proc/device-tree/model ]]; then
    board_model="$(tr -d '\0' < /proc/device-tree/model)"
  fi
  if [[ "${board_model,,}" == *orange*pi* ]]; then
    install_motd=1
  else
    install_motd=0
  fi
fi

case "$install_motd" in
  0)
    echo "Skipping the MeshCore login banner (set MESHCORE_MOTD=1 to force install)."
    exit 0
    ;;
  1) ;;
  *)
    echo "Invalid MESHCORE_MOTD value: $install_motd (use 0 or 1)." >&2
    exit 1
    ;;
esac

sudo install -D -m 0755 "$MOTD_SCRIPT" /usr/local/share/meshcore/motd.sh
sudo tee /etc/profile.d/meshcore-motd.sh >/dev/null <<'EOF'
# Show the MeshCore banner once per interactive login shell.
case "$-" in
  *i*) [ -x /usr/local/share/meshcore/motd.sh ] && bash /usr/local/share/meshcore/motd.sh ;;
esac
EOF
sudo chmod 0644 /etc/profile.d/meshcore-motd.sh

# Disable the stock static and dynamic banners so only the MeshCore banner shows.
sudo truncate -s 0 /etc/motd 2>/dev/null || true
if [[ -d /etc/update-motd.d ]]; then
  sudo find /etc/update-motd.d -maxdepth 1 -type f -exec chmod a-x {} +
fi

echo "Installed the MeshCore login banner. Log out and back in to see it."
