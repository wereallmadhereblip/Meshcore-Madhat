# MeshCore Ollama chatbot

This project includes a local dashboard for a MeshCore device and an Ollama-powered mesh assistant. It can connect over Bluetooth or a serial port.

## Orange Pi Zero 4 / Debian 13 setup

These steps target Debian GNU/Linux 13 (Trixie) arm64. Check that the board is running a 64-bit OS:

```bash
uname -m
dpkg --print-architecture
```

Expected results are `aarch64` and `arm64`. Install and start the app:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/wereallmadhereblip/Meshcore-Ollama-bot.git ~/Meshcore-Ollama-bot
cd ~/Meshcore-Ollama-bot
bash setup.sh
```

The setup script installs the Python, Bluetooth/serial, TightVNC, noVNC, websockify, XFCE and `zstd` dependencies (needed by the Ollama installer), installs Ollama, and prompts you to choose `llama3.2:1b`, `qwen2.5:0.5b` or `llama3.2:3b`. It preselects the smaller model on arm64 to reduce memory use. For unattended installs, set `OLLAMA_MODEL` to one of those model names before running `bash setup.sh`. A board with at least 2 GB RAM is recommended. Model inference runs on the CPU and may be slow.

Setup creates a self-signed TLS certificate at `~/novnc.pem`, configures XFCE for the VNC desktop, and prompts you to set a TightVNC password. You can manage the remote desktop from **Settings → Remote Desktop** or send `/tightvnc start`, `/tightvnc off`, `/tightvnc restart`, and `/tightvnc status` to the bot as an administrator. The browser desktop is available at `https://<host-ip>:6080/vnc.html` while enabled. The browser will warn about the self-signed certificate; the VNC password is still required to log in.

Setup also asks whether to open the dashboard automatically in a browser on the Orange Pi. This preference is saved under `~/.config/meshcore-ollama-bot/preferences.json`. Automatic opening requires a graphical desktop; a headless board can still be opened from a browser on the LAN.

On Orange Pi boards, setup installs the MeshCore gradient login banner automatically. To install it on another Debian-based device, run `MESHCORE_MOTD=1 bash assets/install-motd.sh` from the project directory. You can also run that command on an already-installed device without rerunning the full app setup. Log out and back in to display the banner; run `bash /usr/local/share/meshcore/motd.sh` to preview it in the current terminal.

The script starts the dashboard in the foreground. On a desktop session it also creates a desktop launcher. For a headless board, open `http://<board-ip>:8080` from a browser on the same network; find the board address with:

```bash
hostname -I
```

Setup also enables the dashboard to start automatically at boot through a systemd user service (`meshcore-madhat.service`, with lingering enabled so it runs without a login). Turn it off under **Settings → Update → Start the dashboard automatically when the system boots**, or run `systemctl --user disable meshcore-madhat.service`. Because the service owns port `8080`, use `systemctl --user stop meshcore-madhat.service` before running the dashboard manually.

The dashboard listens on the network so another device can access it. Keep it on a trusted LAN and do not expose ports `8080` or `6080` to the public internet.

If setup added your account to the `dialout` group, log out and back in before using serial connections.

## Other Linux distributions

The same `bash setup.sh` installer can also be used on Kali Linux and other Debian-based desktop systems. A desktop launcher is created when a graphical session is detected.

## Run the dashboard

From the project directory, start the dashboard with:

```bash
cd Meshcore-Ollama-bot
.venv/bin/python madhat.py
```

On desktop installations, you can also launch the app from the shortcut created during setup.

The script starts the web server on port `8080`. It opens the dashboard automatically when a graphical session is available; otherwise, open http://127.0.0.1:8080 locally or use the board's IP address from another device on the LAN.

## Customize the bot

Send the bot a direct message with a request such as:

- `Call yourself Nova.`
- `Change your personality to concise and curious.`
- `Be more playful.`

The bot confirms each change and saves its name and personality under
`~/.config/meshcore-ollama-bot/bot_settings.json` so they persist across restarts.

Use the dashboard to choose Bluetooth or Serial, enter the Device Bluetooth MAC address or serial port, and connect. The default values are `/dev/ttyACM0` and the MAC address defined near the top of the script.

Administrators can DM `/fastfetch` to receive the host's Fastfetch system summary. Fastfetch must be installed on the host.

## Hardware notes

- Bluetooth must be enabled and the device must be discoverable.
- Serial connections commonly use `/dev/ttyACM0` or `/dev/ttyUSB0`.
- If the browser does not open automatically, visit http://127.0.0.1:8080 manually.
- Gateway battery telemetry requires firmware support and telemetry enabled on the connected Heltec device.
- Channel discovery and telemetry depend on the MeshCore firmware and Python package version.

## Troubleshooting

### Bluetooth connection fails

- Make sure Bluetooth is enabled with:

```bash
sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth
bluetoothctl power on
```

- Confirm the MeshCore device is powered on and discoverable.
- Check that the MAC address entered in the app is correct.
- If the device is paired but still not connecting, try removing it from the OS Bluetooth list and reconnecting.

### Serial connection fails

- Verify the device port with:

```bash
ls /dev/ttyACM* /dev/ttyUSB*
```

- Common device ports are `/dev/ttyACM0` and `/dev/ttyUSB0`.
- Ensure your user is in the `dialout` group:

```bash
groups "$USER"
```

- Log out and back in after adding yourself to the group.

### Dashboard does not open in the browser

- Open http://127.0.0.1:8080 manually in a browser.
- Make sure the script is still running in the terminal and that no errors were printed.
- Check whether another process is already listening on port `8080`.

### Ollama model fails to load or respond

- The dashboard starts the local Ollama server automatically when Ollama is installed but not already running.
- If the Ollama command is missing, install Ollama:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

- To start the server manually, run `ollama serve` in a separate terminal.
- Pull the model again if needed:

```bash
ollama pull llama3.2:1b
ollama pull qwen2.5:0.5b
```

- Confirm your virtual environment is active before running the dashboard.
- The Settings page has an Ollama tab where you can:
  - Download new models with live download and installation progress (with quick presets for `qwen2.5:0.5b`, `llama3.2:1b`, `llama3.2:3b`, etc.)
  - Delete unused models to free up disk/RAM space (especially useful on Live Kali Linux)
  - Switch the active bot model
  - Stop/start the Ollama server on demand to save power
  - Set an automatic on/off schedule

## Stop the dashboard

Press `Ctrl+C` in the terminal running the script.
