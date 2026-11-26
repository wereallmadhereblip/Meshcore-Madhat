#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(eval echo "~$TARGET_USER")"

sudo apt-get update
sudo apt-get install -y zsh zsh-autosuggestions zsh-syntax-highlighting

cp "$SCRIPT_DIR/zshrc" "$TARGET_HOME/.zshrc"
chown "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.zshrc"

if [ "$(id -u)" -eq 0 ] && [ "$TARGET_HOME" != "/root" ]; then
    cp "$SCRIPT_DIR/zshrc" /root/.zshrc
fi

ZSH_BIN="$(command -v zsh)"
if [ -n "$ZSH_BIN" ]; then
    sudo chsh -s "$ZSH_BIN" "$TARGET_USER" || true
fi

echo "Kali-style zsh installed for $TARGET_USER."
