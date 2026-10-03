#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/Meshcore-Ollama-bot"
DESKTOP_DIR="$HOME/Desktop"

mkdir -p "$DESKTOP_DIR"

sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl bluez fastfetch whiptail

if ! id -nG "$USER" | grep -qw "dialout"; then
  sudo usermod -aG dialout "$USER"
  echo "Added $USER to the dialout group. Log out and back in before using serial devices."
fi

sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

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
if [ -t 0 ] || [ -e /dev/tty ]; then
  if command -v whiptail >/dev/null 2>&1; then
    model_choice=$(whiptail --title "MeshCore AI Bot - Model Selection" \
      --menu "Choose an Ollama model to install:\n(Option 2 is recommended for Live Kali to save RAM/space)" \
      13 72 2 \
      "1" "llama3.2:1b  (~1.3 GB - Default)" \
      "2" "qwen2.5:0.5b (~400 MB - Live Kali / Low RAM)" \
      3>&1 1>&2 2>&3 < /dev/tty) || model_choice="1"
  elif command -v dialog >/dev/null 2>&1; then
    model_choice=$(dialog --clear --title "MeshCore AI Bot - Model Selection" \
      --menu "Choose an Ollama model to install:\n(Option 2 is recommended for Live Kali to save RAM/space)" \
      13 72 2 \
      "1" "llama3.2:1b  (~1.3 GB - Default)" \
      "2" "qwen2.5:0.5b (~400 MB - Live Kali / Low RAM)" \
      3>&1 1>&2 2>&3 < /dev/tty) || model_choice="1"
  else
    echo ""
    echo "Select the Ollama model to download:"
    echo "  1) llama3.2:1b  (~1.3 GB - Default)"
    echo "  2) qwen2.5:0.5b (~400 MB - Live Kali / Low RAM)"
    read -rp "Enter choice [1-2] (default: 1): " model_choice < /dev/tty || model_choice="1"
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

# Update config.json default model if a specific model was selected
if [[ -f "$SCRIPT_DIR/config.json" && "$SELECTED_MODEL" != "llama3.2:1b" ]]; then
  sed -i 's/"model": "llama3.2:1b"/"model": "'"$SELECTED_MODEL"'"/' "$SCRIPT_DIR/config.json"
fi

echo "Setup complete."
echo "Desktop shortcut created: $DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop"

cat > "$DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=MeshCore AI Bot Dashboard
Comment=Launch the MeshCore AI Bot Dashboard
Exec=/bin/bash -lc 'cd "$HOME/Meshcore-Ollama-bot" && .venv/bin/python madhat.py'
Path=$HOME/Meshcore-Ollama-bot
Terminal=true
Icon=$HOME/Meshcore-Ollama-bot/dashboard-logo.png
StartupNotify=true
Categories=Utility;
EOF

chmod +x "$DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop"

if command -v gio >/dev/null 2>&1; then
  gio set "$DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop" metadata::trusted true || true
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
cd "$APP_DIR"
exec .venv/bin/python madhat.py
