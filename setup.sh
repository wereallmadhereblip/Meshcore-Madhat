#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR"
DESKTOP_DIR="$HOME/Desktop"

# Detect if running on a headless SBC (Single Board Computer) or server OS
detect_headless_sbc() {
  # Check for common SBC indicators
  local is_sbc=false
  local has_display=false
  local is_server_os=false

  # Detect common SBC boards
  if [ -f /proc/device-tree/model ]; then
    local board=$(cat /proc/device-tree/model 2>/dev/null || echo "")
    if [[ "$board" == *"Pi"* ]] || [[ "$board" == *"Orange Pi"* ]] || [[ "$board" == *"Banana Pi"* ]] || \
       [[ "$board" == *"NanoPI"* ]] || [[ "$board" == *"Raspberry"* ]] || [[ "$board" == *"BeagleBone"* ]]; then
      is_sbc=true
    fi
  fi

  # Check for display/desktop environment
  if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    has_display=true
  fi

  # Detect server OS (no desktop environment installed)
  if ! command -v startx >/dev/null 2>&1 && ! command -v startxfce4 >/dev/null 2>&1; then
    is_server_os=true
  fi

  # Return 0 (true) if it's headless (SBC without display or server OS)
  if ([[ "$is_sbc" == true ]] && [[ "$has_display" == false ]]) || [[ "$is_server_os" == true ]]; then
    return 0
  fi
  return 1
}

# Detect OS and board type
detect_board_and_os() {
  local board_name=""
  local os_name=""
  local os_version=""

  # Try to detect Orange Pi Zero 4
  if [ -f /proc/device-tree/model ]; then
    board_name=$(cat /proc/device-tree/model 2>/dev/null || echo "")
  fi

  # Detect Debian version
  if [ -f /etc/os-release ]; then
    os_name=$(grep '^NAME=' /etc/os-release | cut -d'"' -f2)
    os_version=$(grep '^VERSION_CODENAME=' /etc/os-release | cut -d'=' -f2)
  fi

  echo "Board: $board_name | OS: $os_name | Version: $os_version"

  # Check if Orange Pi Zero 4 with Debian Trixie
  if [[ "$board_name" == *"Orange Pi Zero 4"* ]] && [[ "$os_version" == "trixie" ]]; then
    return 0
  fi
  return 1
}

# AIC8800 Bluetooth fix for Orange Pi Zero 4
fix_aic8800_bluetooth() {
  echo "=========================================================="
  echo "  AIC8800 Bluetooth Permanent Fix for Orange Pi Zero 4   "
  echo "=========================================================="

  FW_DIR="/lib/firmware/aic8800d80"
  COMBO_FW="fmacfwbt_8800d80_u02.bin"
  TARGET_FW="fmacfw_8800d80_u02.bin"
  USER_CONF="aic_userconfig_8800d80.txt"
  HW_CONF="aichw.conf"
  ENV_FILE="/etc/environment"
  SERVICE_PATH="/etc/systemd/system/aic-bluetooth.service"

  # 1. Firmware & Hardware Config
  echo "[1/6] Setting up AIC8800 firmware & configs..."
  if [ -d "$FW_DIR" ]; then
    cd "$FW_DIR"
    if [ -f "$TARGET_FW" ] && [ ! -f "${TARGET_FW}.bak" ]; then
      cp -f "$TARGET_FW" "${TARGET_FW}.bak"
    fi
    if [ -f "$COMBO_FW" ]; then
      cp -f "$COMBO_FW" "$TARGET_FW"
      echo "  [+] Active firmware set to combo BT+Wi-Fi binary."
    fi
    if [ -f "$USER_CONF" ]; then
      cp -f "$USER_CONF" "$HW_CONF"
      echo "  [+] Generated $HW_CONF from user config."
    fi
  fi

  # 2. Kernel Module Configuration
  echo "[2/6] Writing module parameters to /etc/modprobe.d/aic8800.conf..."
  sudo cat << 'EOF' > /etc/modprobe.d/aic8800.conf
options aic8800_fdrv testmode=0
options aic8800_bsp btmode=1
EOF

  # 3. Systemd Auto-Attach Service
  echo "[3/6] Installing persistent aic-bluetooth.service..."
  sudo cat << 'EOF' > "$SERVICE_PATH"
[Unit]
Description=AIC8800 Bluetooth UART Initialization & Wake Service
After=syslog.target network.target bluetooth.service
Before=bluetooth.target

[Service]
Type=forking
ExecStartPre=/usr/sbin/rfkill unblock bluetooth
ExecStartPre=/bin/sh -c 'echo 1 > /proc/bluetooth/sleep/btwrite 2>/dev/null || true'
ExecStart=/usr/bin/hciattach -s 1500000 /dev/ttyS1 any 1500000 flow nosleep
ExecStartPost=/bin/sleep 2
ExecStartPost=/bin/sh -c '/usr/bin/hciconfig hci1 up 2>/dev/null || /usr/bin/hciconfig hci0 up 2>/dev/null || true'
ExecStartPost=/bin/sh -c '/usr/bin/bluetoothctl power on || true'
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  sudo chmod 644 "$SERVICE_PATH"
  sudo systemctl daemon-reload
  sudo systemctl enable aic-bluetooth.service
  echo "  [+] Enabled aic-bluetooth.service."

  # 4. Global Environment Variable for Bleak
  echo "[4/6] Exporting BLEAK_ADAPTER=hci1 to /etc/environment..."
  if ! sudo grep -q "BLEAK_ADAPTER" "$ENV_FILE" 2>/dev/null; then
    echo 'BLEAK_ADAPTER="hci1"' | sudo tee -a "$ENV_FILE" > /dev/null
  else
    sudo sed -i 's/BLEAK_ADAPTER=.*/BLEAK_ADAPTER="hci1"/' "$ENV_FILE"
  fi

  # 5. Inject into Application Services
  echo "[5/6] Patching systemd services with BLEAK_ADAPTER..."
  for s in $(find /etc/systemd/system -name "*meshcore*" -o -name "*madhat*" 2>/dev/null); do
    if [ -f "$s" ] && ! sudo grep -q "BLEAK_ADAPTER" "$s"; then
      sudo sed -i '/\[Service\]/a Environment="BLEAK_ADAPTER=hci1"' "$s"
      echo "  [+] Patched $s"
    fi
  done

  # 6. Apply Live Settings
  echo "[6/6] Initializing Bluetooth interfaces..."
  sudo systemctl stop aic-bluetooth.service 2>/dev/null || true
  sudo killall -9 hciattach 2>/dev/null || true
  sudo rfkill unblock bluetooth 2>/dev/null || true
  echo 1 | sudo tee /proc/bluetooth/sleep/btwrite 2>/dev/null || true

  sudo systemctl start aic-bluetooth.service || true
  sleep 2

  sudo hciconfig hci1 up 2>/dev/null || true
  sudo bluetoothctl select hci1 2>/dev/null || true
  sudo bluetoothctl power on 2>/dev/null || true

  echo "=========================================================="
  echo "Done! Run 'hciconfig -a' or 'bluetoothctl show' to verify."
  echo "=========================================================="
}

sudo apt update

# Determine if headless/server installation
if detect_headless_sbc; then
  echo "[!] Headless/Server OS detected. Skipping desktop and VNC packages."
  sudo apt install -y git python3 python3-venv python3-pip python3-dev build-essential \
    libffi-dev libssl-dev pkg-config curl bluez rfkill fastfetch whiptail zstd openssl
  IS_HEADLESS=true
else
  echo "[+] Desktop environment detected. Installing desktop and VNC packages."
  sudo apt install -y git python3 python3-venv python3-pip python3-dev build-essential \
    libffi-dev libssl-dev pkg-config curl bluez rfkill fastfetch whiptail zstd \
    tightvncserver novnc websockify xfce4 dbus-x11 openssl
  IS_HEADLESS=false
fi

# Only configure VNC on non-headless systems
if [[ "$IS_HEADLESS" != true ]]; then
  mkdir -p "$HOME/.vnc"
  if [[ ! -f "$HOME/.vnc/xstartup" ]]; then
    cat > "$HOME/.vnc/xstartup" <<'EOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec startxfce4
EOF
  fi
  chmod +x "$HOME/.vnc/xstartup"

  if [[ ! -f "$HOME/novnc.pem" ]]; then
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
      -keyout "$HOME/novnc.pem" -out "$HOME/novnc.pem" \
      -subj "/CN=$(hostname)"
  fi
  chmod 600 "$HOME/novnc.pem"

  if [[ ! -f "$HOME/.vnc/passwd" ]]; then
    if [[ -t 0 ]]; then
      echo "Set the password used to connect to TightVNC:"
      vncpasswd
    elif ( : < /dev/tty > /dev/tty ) 2>/dev/null; then
      echo "Set the password used to connect to TightVNC:"
      vncpasswd < /dev/tty > /dev/tty
    else
      echo "No VNC password is configured. Run 'vncpasswd' before using /tightvnc on." >&2
    fi
  fi
fi

if ! id -nG "$USER" | grep -qw "dialout"; then
  sudo usermod -aG dialout "$USER"
  echo "Added $USER to the dialout group. Log out and back in before using serial devices."
fi

sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth || true

# Detect and apply OS-specific fixes
echo ""
echo "Detecting board and OS configuration..."
if detect_board_and_os; then
  echo "[+] Detected Orange Pi Zero 4 with Debian Trixie. Applying AIC8800 Bluetooth fix..."
  fix_aic8800_bluetooth
  echo ""
else
  echo "[-] Standard board configuration detected."
fi
echo ""

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

export PATH="$PATH:/usr/local/bin:/usr/local/sbin"

if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
  export PATH="$PATH:/usr/local/bin:/usr/local/sbin"
fi

if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
fi

ollama_ready=false
for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    ollama_ready=true
    break
  fi
  sleep 1
done

if [[ "$ollama_ready" != true ]]; then
  echo "Ollama server did not become ready. Check /tmp/ollama-serve.log or run 'ollama serve'." >&2
  exit 1
fi

echo "Installing llama3.2:1b-instruct-q4_K_M..."
ollama pull llama3.2:1b-instruct-q4_K_M
SELECTED_MODEL="llama3.2:1b-instruct-q4_K_M"

browser_choice="2"
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  browser_choice="1"
fi

# Skip browser prompt on headless SBCs
if detect_headless_sbc; then
  echo "[!] Headless SBC detected. Skipping browser auto-open prompt."
  AUTO_OPEN_BROWSER="false"
elif [ -t 0 ] || [ -e /dev/tty ]; then
  if command -v whiptail >/dev/null 2>&1; then
    browser_choice=$(whiptail --title "MeshCore AI Bot - Browser" \
      --default-item "$browser_choice" \
      --menu "Automatically open the dashboard in a browser on this device?\n(Requires a graphical desktop session)" \
      10 72 2 \
      "1" "Yes, open the browser" \
      "2" "No, keep browser closed" \
      3>&1 1>&2 2>&3 < /dev/tty) || browser_choice="$browser_choice"
  else
    echo ""
    if [[ "$browser_choice" == "1" ]]; then
      browser_prompt="[Y/n]"
    else
      browser_prompt="[y/N]"
    fi
    read -rp "Automatically open the dashboard browser on this device? $browser_prompt: " browser_answer < /dev/tty || true
    if [[ "$browser_answer" =~ ^[Yy] ]]; then
      browser_choice="1"
    elif [[ "$browser_answer" =~ ^[Nn] ]]; then
      browser_choice="2"
    fi
  fi

  if [[ "$browser_choice" == "1" ]]; then
    AUTO_OPEN_BROWSER="true"
  else
    AUTO_OPEN_BROWSER="false"
  fi
else
  AUTO_OPEN_BROWSER="false"
fi
PREFERENCES_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/meshcore-ollama-bot"
mkdir -p "$PREFERENCES_DIR"
PREFERENCES_PATH="$PREFERENCES_DIR/preferences.json" \
  AUTO_OPEN_BROWSER="$AUTO_OPEN_BROWSER" python3 -c 'import json, os; from pathlib import Path; p=Path(os.environ["PREFERENCES_PATH"]); d={};
try: d=json.loads(p.read_text())
except (OSError, json.JSONDecodeError): pass
d["auto_open_browser"] = os.environ["AUTO_OPEN_BROWSER"] == "true"; p.write_text(json.dumps(d, indent=2) + "\n")'

# Keep an existing non-default model choice intact; update only the shipped default.
if [[ -f "$SCRIPT_DIR/config.json" && "$SELECTED_MODEL" != "llama3.2:1b-instruct-q4_K_M" ]]; then
  APP_DIR="$APP_DIR" SELECTED_MODEL="$SELECTED_MODEL" python3 -c 'import json, os; from pathlib import Path; p=Path(os.environ["APP_DIR"])/"config.json"; c=json.loads(p.read_text()); c["model"] = os.environ["SELECTED_MODEL"] if c.get("model") == "llama3.2:1b-instruct-q4_K_M" else c.get("model", "llama3.2:1b-instruct-q4_K_M"); p.write_text(json.dumps(c, indent=2) + "\n")'
fi

# Start the dashboard at boot via a systemd user service (can be turned off in app settings).
"$APP_DIR/.venv/bin/python" "$APP_DIR/madhat.py" --set-autostart on \
  || echo "Could not enable start at boot; enable it later in the dashboard settings." >&2

echo "Setup complete."
if [[ "$IS_HEADLESS" != true ]] && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  mkdir -p "$DESKTOP_DIR"
  cat > "$DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=MeshCore AI Bot Dashboard
Comment=Launch the MeshCore AI Bot Dashboard
Exec="$APP_DIR/.venv/bin/python" "$APP_DIR/madhat.py"
Path=$APP_DIR
Terminal=true
Icon=$APP_DIR/dashboard-logo.png
StartupNotify=true
Categories=Utility;
EOF

  chmod +x "$DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop"

  if command -v gio >/dev/null 2>&1; then
    gio set "$DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop" metadata::trusted true || true
  fi
  echo "Desktop shortcut created: $DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop"
fi

bash "$SCRIPT_DIR/assets/install-motd.sh"

LOGO_SCRIPT="$SCRIPT_DIR/assets/fastfetch-logo.sh"
if [[ -f "$LOGO_SCRIPT" ]]; then
  # Explicit width/height keep fastfetch from mis-measuring the ANSI-colored
  # logo (which otherwise makes the info text interleave with the art).
  fastfetch --logo-type data-raw --logo "$(bash "$LOGO_SCRIPT")" --logo-width 70 --logo-height 50
else
  fastfetch
fi

echo ""
echo "Starting the MeshCore AI Bot Dashboard..."
echo "======== Running on http://0.0.0.0:8080 ========"
echo ""

# Start the dashboard in the background
nohup "$APP_DIR/.venv/bin/python" "$APP_DIR/madhat.py" >/tmp/madhat.log 2>&1 &

echo "Dashboard started in background. Check logs with: tail -f /tmp/madhat.log"
exit 0
