# MeshCore AI Bot Dashboard

This project includes Jupyter notebooks and `mesh_ai_bot_dashboard.py`, a local web dashboard for a MeshCore device and an Ollama-powered mesh message assistant.

## Kali Linux setup

Install the system packages:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl bluez
```

For a serial-connected device, add your user to the serial-device group and then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

Create a virtual environment and install the Python dependencies:

```bash
git clone <YOUR_REPOSITORY_URL>
cd codespaces-jupyter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Install and start Ollama, then download the default model:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve
```

Open a second terminal, activate the environment again, and pull the model:

```bash
cd codespaces-jupyter
source .venv/bin/activate
ollama pull llama3.2:1b
```

## Run the dashboard

With the virtual environment active:

```bash
python mesh_ai_bot_dashboard.py
```

The script starts the web server on port `8080` and attempts to open the dashboard automatically at http://127.0.0.1:8080.

Use the dashboard to choose Bluetooth or Serial, enter the Heltec Bluetooth MAC address or serial port, select the Ollama model, and connect. The default values are `/dev/ttyACM0` and the MAC address defined near the top of the script.

## Hardware notes

- Bluetooth must be enabled and the device must be discoverable.
- Serial connections commonly use `/dev/ttyACM0` or `/dev/ttyUSB0`.
- If the browser does not open automatically, visit http://127.0.0.1:8080 manually.
- Gateway battery telemetry requires firmware support and telemetry enabled on the connected Heltec device.
- Channel discovery and telemetry depend on the MeshCore firmware and Python package version.

## Stop the dashboard

Press `Ctrl+C` in the terminal running the script.
