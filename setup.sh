#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR"
DESKTOP_DIR="$HOME/Desktop"

sudo apt update
sudo apt install -y git python3 python3-venv python3-pip python3-dev build-essential \
  libffi-dev libssl-dev pkg-config curl bluez rfkill fastfetch whiptail

if ! id -nG "$USER" | grep -qw "dialout"; then
  sudo usermod -aG dialout "$USER"
  echo "Added $USER to the dialout group. Log out and back in before using serial devices."
fi

sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth || true

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

model_choice="1"
if [[ "$(dpkg --print-architecture)" == "arm64" ]]; then
  model_choice="2"
fi
if [[ -n "${OLLAMA_MODEL:-}" ]]; then
  case "$OLLAMA_MODEL" in
    llama3.2:1b) model_choice="1" ;;
    qwen2.5:0.5b) model_choice="2" ;;
    *) echo "Unsupported OLLAMA_MODEL: $OLLAMA_MODEL" >&2; exit 1 ;;
  esac
elif [ -t 0 ] || [ -e /dev/tty ]; then
  if command -v whiptail >/dev/null 2>&1; then
    model_choice=$(whiptail --title "MeshCore AI Bot - Model Selection" \
      --default-item "$model_choice" \
      --menu "Choose an Ollama model to install:\n(Option 2 is recommended on low-memory ARM boards)" \
      13 72 2 \
      "1" "llama3.2:1b  (~1.3 GB)" \
      "2" "qwen2.5:0.5b (~400 MB - low memory)" \
      3>&1 1>&2 2>&3 < /dev/tty) || model_choice="$model_choice"
  elif command -v dialog >/dev/null 2>&1; then
    model_choice=$(dialog --clear --title "MeshCore AI Bot - Model Selection" \
      --default-item "$model_choice" \
      --menu "Choose an Ollama model to install:\n(Option 2 is recommended on low-memory ARM boards)" \
      13 72 2 \
      "1" "llama3.2:1b  (~1.3 GB)" \
      "2" "qwen2.5:0.5b (~400 MB - low memory)" \
      3>&1 1>&2 2>&3 < /dev/tty) || model_choice="$model_choice"
  else
    echo ""
    echo "Select the Ollama model to download:"
    echo "  1) llama3.2:1b  (~1.3 GB)"
    echo "  2) qwen2.5:0.5b (~400 MB - low memory)"
    read -rp "Enter choice [1-2] (default: $model_choice): " selected_choice < /dev/tty || true
    model_choice="${selected_choice:-$model_choice}"
  fi
fi

SELECTED_MODEL="llama3.2:1b"
case "$model_choice" in
  2)
    echo "Pulling qwen2.5:0.5b..."
    ollama pull qwen2.5:0.5b
    SELECTED_MODEL="qwen2.5:0.5b"
    ;;
  *)
    echo "Pulling llama3.2:1b..."
    ollama pull llama3.2:1b
    SELECTED_MODEL="llama3.2:1b"
    ;;
esac

browser_choice="2"
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  browser_choice="1"
fi
if [ -t 0 ] || [ -e /dev/tty ]; then
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
fi

if [[ "$browser_choice" == "1" ]]; then
  AUTO_OPEN_BROWSER="true"
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
if [[ -f "$SCRIPT_DIR/config.json" && "$SELECTED_MODEL" != "llama3.2:1b" ]]; then
  APP_DIR="$APP_DIR" SELECTED_MODEL="$SELECTED_MODEL" python3 -c 'import json, os; from pathlib import Path; p=Path(os.environ["APP_DIR"])/"config.json"; c=json.loads(p.read_text()); c["model"] = os.environ["SELECTED_MODEL"] if c.get("model") == "llama3.2:1b" else c.get("model", "llama3.2:1b"); p.write_text(json.dumps(c, indent=2) + "\n")'
fi

echo "Setup complete."
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
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

LOGO_SCRIPT="$SCRIPT_DIR/assets/fastfetch-logo.sh"
if [[ -f "$LOGO_SCRIPT" ]]; then
  # Explicit width/height keep fastfetch from mis-measuring the ANSI-colored
  # logo (which otherwise makes the info text interleave with the art).
  fastfetch --logo-type data-raw --logo "$(bash "$LOGO_SCRIPT")" --logo-width 70 --logo-height 50
else
  fastfetch
fi

echo "Starting the MeshCore AI Bot Dashboard..."
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/madhat.py"
