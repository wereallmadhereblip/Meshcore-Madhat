#!/usr/bin/env bash
set -e

sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl bluez

if ! id -nG "$USER" | grep -qw "dialout"; then
  sudo usermod -aG dialout "$USER"
  echo "Added $USER to the dialout group. Log out and back in before using serial devices."
fi

sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

export PATH="$PATH:/usr/local/bin:/usr/local/sbin"

if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
  export PATH="$PATH:/usr/local/bin:/usr/local/sbin"
fi

source .venv/bin/activate
nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
ollama pull llama3.2:1b

echo "Setup complete."
echo "Run the following in a new terminal:"
echo "  cd ~/Meshcore-Ollama-bot"
echo "  source .venv/bin/activate"
echo "  python mesh_ai_bot_dashboard.py"
