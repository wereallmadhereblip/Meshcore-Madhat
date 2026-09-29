#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/Meshcore-Ollama-bot"
DESKTOP_DIR="$HOME/Desktop"

mkdir -p "$DESKTOP_DIR"

sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl bluez fastfetch

LOGO_SCRIPT="$SCRIPT_DIR/assets/fastfetch-logo.sh"
if [[ -f "$LOGO_SCRIPT" ]]; then
  # Explicit width/height keep fastfetch from mis-measuring the ANSI-colored
  # logo (which otherwise makes the info text interleave with the art).
  fastfetch --logo-type data-raw --logo "$(bash "$LOGO_SCRIPT")" --logo-width 70 --logo-height 50
else
  fastfetch
fi

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

ollama pull llama3.2:1b
ollama pull qwen2.5:0.5b

echo "Setup complete."
echo "Desktop shortcut created: $DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop"

cat > "$DESKTOP_DIR/MeshCore AI Bot Dashboard.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=MeshCore AI Bot Dashboard
Comment=Launch the MeshCore AI Bot Dashboard
Exec=/bin/bash -lc 'cd "$HOME/Meshcore-Ollama-bot" && .venv/bin/python meshcore_ai_bot.py'
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

echo "Starting the MeshCore AI Bot Dashboard..."
cd "$APP_DIR"
exec .venv/bin/python meshcore_ai_bot.py
