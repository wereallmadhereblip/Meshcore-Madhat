# MeshCore AI Bot Dashboard

This project includes a local dashboard for a MeshCore device and an Ollama-powered mesh assistant. It can connect over Bluetooth or a serial port.

## Kali Linux setup

Use this one-liner to remove any previous clone, download the project, and run the setup script:

```bash
rm -rf ~/Meshcore-Ollama-bot && git clone https://github.com/wereallmadhereblip/Meshcore-Ollama-bot.git ~/Meshcore-Ollama-bot && cd ~/Meshcore-Ollama-bot && bash setup.sh
```

The installation also creates a desktop launcher named `MeshCore AI Bot Dashboard` on your desktop so you can start the app with a single click.

## Run the dashboard

From the project directory:

```bash
cd Meshcore-Ollama-bot
.venv/bin/python meshcore_ai_bot.py
```

You can also launch the app from the desktop shortcut created during setup.

The script starts the web server on port `8080` and attempts to open the dashboard automatically at http://127.0.0.1:8080.

## Customize the bot

Send the bot a direct message with a request such as:

- `Call yourself Nova.`
- `Change your personality to concise and curious.`
- `Be more playful.`

The bot confirms each change and saves its name and personality under
`~/.config/meshcore-ollama-bot/bot_settings.json` so they persist across restarts.

Use the dashboard to choose Bluetooth or Serial, enter the Device Bluetooth MAC address or serial port, and connect. The default values are `/dev/ttyACM0` and the MAC address defined near the top of the script.

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
- The Settings page has an Ollama server toggle to stop/start it on demand, which lowers power draw when you don't need AI replies.

## Stop the dashboard

Press `Ctrl+C` in the terminal running the script.
