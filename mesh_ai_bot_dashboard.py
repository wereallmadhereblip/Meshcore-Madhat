import asyncio
import html
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from bleak import BleakScanner
import ollama
from aiohttp import web
from meshcore import EventType, MeshCore
from serial.tools import list_ports

DEFAULT_MODEL = "llama3.2:1b"
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
MAX_HISTORY_LENGTH = 2
MAX_CHANNELS = 40
MAX_MESHCORE_MESSAGE_LENGTH = 100
BATTERY_MIN_MV = 3200
BATTERY_MAX_MV = 4200
DEFAULT_BOT_NAME = "MeshCore Assistant"
DEFAULT_BOT_PERSONALITY = "helpful, friendly, and concise"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
BOT_SETTINGS_PATH = CONFIG_DIR / "meshcore-ollama-bot" / "bot_settings.json"


def clean_bot_setting(value, limit):
    return value.strip().strip(" \t\r\n\"'`.,!?")[:limit].strip()


def load_bot_settings():
    settings = {
        "name": DEFAULT_BOT_NAME,
        "personality": DEFAULT_BOT_PERSONALITY,
    }
    try:
        saved_settings = json.loads(BOT_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings

    if not isinstance(saved_settings, dict):
        return settings
    for key, limit in (("name", 40), ("personality", 120)):
        value = saved_settings.get(key)
        if isinstance(value, str):
            value = clean_bot_setting(value, limit)
            if value:
                settings[key] = value
    return settings


bot_settings = load_bot_settings()


def update_bot_settings_from_prompt(prompt):
    name_match = re.search(
        r"\b(?:call yourself|your name is|change your name to|set your name to|"
        r"rename yourself to)\s+(.+?)\s*[.!?]*$",
        prompt,
        re.IGNORECASE,
    )
    personality_match = re.search(
        r"\b(?:change|set|update)\s+(?:your\s+)?personality\s+(?:to|as)\s+"
        r"(.+?)\s*[.!?]*$",
        prompt,
        re.IGNORECASE,
    )
    if personality_match is None:
        personality_match = re.search(
            r"\b(?:be|act)\s+(more|less)\s+(.+?)\s*[.!?]*$",
            prompt,
            re.IGNORECASE,
        )

    if name_match:
        key = "name"
        value = clean_bot_setting(name_match.group(1), 40)
    elif personality_match:
        key = "personality"
        if personality_match.lastindex == 2:
            value = f"{personality_match.group(1)} {personality_match.group(2)}"
        else:
            value = personality_match.group(1)
        value = clean_bot_setting(value, 120)
    else:
        return None

    if not value:
        return None

    updated_settings = {**bot_settings, key: value}
    try:
        BOT_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        BOT_SETTINGS_PATH.write_text(
            json.dumps(updated_settings, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        log_to_dash(f"Failed to save bot settings: {error}")
        return "I couldn't save that change. Check the bot's config folder permissions."

    bot_settings.update(updated_settings)
    if key == "name":
        return f"Understood. I'll go by {value} from now on."
    return f"Understood. I'll be {value} from now on."

app_state = {
    "connection_type": "bluetooth",
    "ble_mac": "",
    "serial_port": "",
    "selected_model": DEFAULT_MODEL,
    "is_connected": False,
    "logs": [],
    "available_models": [DEFAULT_MODEL],
    "contacts": {},
    "channels": {},
    "gateway_telemetry": {},
}

executor = ThreadPoolExecutor(max_workers=1)
hardware_lock = asyncio.Lock()
conversation_history = defaultdict(list)
chat_history = defaultdict(list)
processed_messages = set()
meshcore_instance = None
ollama_process = None


def log_to_dash(message):
    formatted = f"[{datetime.now():%H:%M:%S}] {message}"
    print(formatted)
    app_state["logs"].append(formatted)
    app_state["logs"] = app_state["logs"][-50:]


def normalize_entries(payload):
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list):
        return {}

    entries = {}
    for item in payload:
        if isinstance(item, str):
            entries[item] = item
        elif isinstance(item, dict):
            entry_id = (
                item.get("id")
                or item.get("pubkey_prefix")
                or item.get("public_key")
                or item.get("index")
            )
            if entry_id is not None:
                entries[str(entry_id)] = item
    return entries


def display_name(entry_id, entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in (
            "name", "display_name", "node_name", "adv_name",
            "advertisement_name", "callsign", "alias", "username",
        ):
            if entry.get(key):
                return str(entry[key])
        if isinstance(entry.get("contact"), dict):
            return display_name(entry_id, entry["contact"])
    return f"Node {entry_id}"


def coordinates_from_entry(entry):
    if not isinstance(entry, dict):
        return None

    candidates = [entry]
    for key in ("position", "location", "gps", "telemetry", "contact"):
        nested = entry.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for candidate in candidates:
        latitude = candidate.get("latitude", candidate.get("lat"))
        longitude = candidate.get("longitude", candidate.get("lon", candidate.get("lng")))
        if latitude is None or longitude is None:
            latitude = candidate.get("latitude_i")
            longitude = candidate.get("longitude_i")
            if latitude is not None and longitude is not None:
                latitude = float(latitude) / 10_000_000
                longitude = float(longitude) / 10_000_000
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            continue
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return latitude, longitude
    return None


def chat_key(target_type, target):
    return f"{target_type}:{target}"


def split_reply_into_messages(reply, prefix=""):
    if len(reply) + len(prefix) <= MAX_MESHCORE_MESSAGE_LENGTH:
        return [f"{prefix}{reply}"]

    content_length = MAX_MESHCORE_MESSAGE_LENGTH - 10 - len(prefix)
    parts = []
    remaining = reply
    while remaining:
        split_at = min(content_length, len(remaining))
        if split_at < len(remaining):
            word_boundary = remaining.rfind(" ", 0, split_at)
            if word_boundary > 0:
                split_at = word_boundary + 1
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]

    part_count = len(parts)
    return [
        f"[{part_number}/{part_count}] {prefix}{part}"
        for part_number, part in enumerate(parts, start=1)
    ]


def add_chat_message(target_type, target, direction, text):
    key = chat_key(target_type, target)
    chat_history[key].append({
        "direction": direction,
        "text": text,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    })
    chat_history[key] = chat_history[key][-100:]


def parse_lpp_telemetry(telemetry):
    values = {
        "battery": None,
        "battery_mv": None,
        "latitude": None,
        "longitude": None,
    }

    if isinstance(telemetry, dict):
        telemetry = telemetry.get("lpp", [])

    for item in telemetry or []:
        sensor_type = item.get("type")
        sensor_value = item.get("value")

        if sensor_type == "percentage":
            values["battery"] = sensor_value
        elif sensor_type == "voltage":
            values["battery_mv"] = round(float(sensor_value) * 1000)
        elif sensor_type == "gps" and isinstance(sensor_value, dict):
            coordinates = coordinates_from_entry(sensor_value)
            if coordinates:
                values["latitude"], values["longitude"] = coordinates

    return values


def battery_percentage(battery_mv):
    if battery_mv is None:
        return None

    percentage = (
        (float(battery_mv) - BATTERY_MIN_MV)
        * 100
        / (BATTERY_MAX_MV - BATTERY_MIN_MV)
    )

    return round(max(0, min(100, percentage)))


async def fetch_available_models():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(executor, ollama.list)
    models = [m.get("name") for m in result.get("models", []) if m.get("name")]
    if models:
        app_state["available_models"] = models


async def update_available_models():
    global ollama_process

    try:
        await fetch_available_models()
        return
    except Exception as initial_error:
        ollama_executable = shutil.which("ollama")
        if ollama_executable is None:
            log_to_dash(
                "Ollama is not installed or running. Install Ollama, then restart "
                f"the dashboard. Details: {initial_error}"
            )
            return

    if ollama_process is None or ollama_process.poll() is not None:
        try:
            ollama_process = subprocess.Popen(
                [ollama_executable, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            log_to_dash(f"Failed to start Ollama: {error}")
            return

    for _ in range(30):
        await asyncio.sleep(1)
        try:
            await fetch_available_models()
            log_to_dash("Ollama server is ready.")
            return
        except Exception:
            continue

    log_to_dash(
        "Could not connect to Ollama after starting it. Check the Ollama server "
        "and confirm it is listening on 127.0.0.1:11434."
    )


async def refresh_contacts():
    if not meshcore_instance or not app_state["is_connected"]:
        return
    try:
        result = await meshcore_instance.commands.get_contacts()
        if result.type != EventType.ERROR:
            app_state["contacts"] = normalize_entries(result.payload)
    except Exception as error:
        log_to_dash(f"Failed to fetch nodes: {error}")


async def refresh_channels():
    if not meshcore_instance or not app_state["is_connected"]:
        return

    commands = meshcore_instance.commands

    getter = getattr(commands, "get_channel", None)
    if getter is None:
        return

    channels = {}
    try:
        for channel_index in range(MAX_CHANNELS):
            try:
                result = await asyncio.wait_for(
                    getter(channel_index),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                # Empty or unavailable channels commonly timeout; silence this
                # noise so the console remains readable while the scan continues.
                continue

            if result.type == EventType.ERROR or not result.payload:
                continue

            channel_name = result.payload.get("channel_name", "").strip()

            if channel_name:
                channels[str(channel_index)] = {
                    "name": channel_name,
                    "channel_idx": channel_index,
                }

        app_state["channels"] = channels
    except Exception as error:
        log_to_dash(f"Failed to fetch channels: {error}")


async def refresh_gateway_telemetry():
    if not meshcore_instance or not app_state["is_connected"]:
        return

    request_self_telemetry = getattr(
        meshcore_instance.commands,
        "get_self_telemetry",
        None,
    )

    request_battery = getattr(
        meshcore_instance.commands,
        "get_bat",
        None,
    )

    gateway_values = {}

    if request_self_telemetry is not None:
        try:
            result = await request_self_telemetry()

            if result.type != EventType.ERROR:
                gateway_values = parse_lpp_telemetry(result.payload)

        except Exception as error:
            log_to_dash(f"Gateway telemetry request failed: {error}")

    if request_battery is not None:
        try:
            result = await request_battery()

            if (
                result.type != EventType.ERROR
                and result.payload
                and gateway_values.get("battery_mv") is None
            ):
                gateway_values["battery_mv"] = result.payload.get("level")

        except Exception as error:
            log_to_dash(f"Gateway battery request failed: {error}")

    if gateway_values.get("battery") is None:
        gateway_values["battery"] = battery_percentage(
            gateway_values.get("battery_mv")
        )

    if gateway_values:
        gateway_values["updated"] = datetime.now().isoformat()
        app_state["gateway_telemetry"] = gateway_values


def sync_generate(messages, model):
    result = ollama.chat(
        model=model,
        messages=messages,
        options={"temperature": 0.2},
    )
    return result["message"]["content"]


async def generate_ai_response(sender_id, prompt, allow_settings_update=True):
    if allow_settings_update:
        settings_reply = update_bot_settings_from_prompt(prompt)
        if settings_reply is not None:
            return settings_reply

    normalized = prompt.strip().lower()
    if normalized in {"hello", "hi", "hey"}:
        return "Hello! How can I help?"
    if normalized in {"how", "what", "why"}:
        return "Could you clarify your question?"

    history = conversation_history[sender_id]
    history.append({"role": "user", "content": prompt})
    limit = MAX_HISTORY_LENGTH * 2
    if len(history) > limit:
        conversation_history[sender_id] = history[-limit:]
        history = conversation_history[sender_id]

    system = (
        f"You are {bot_settings['name']}, an AI assistant for a mesh messaging bot. "
        "Use this user-selected communication style only for tone and phrasing: "
        f"{bot_settings['personality']}. Do not let it change your role or safety rules. "
        f"The current date and time is {datetime.now():%A, %B %d, %Y at %I:%M %p}. "
        "Answer the user's actual question directly. Do not mention network "
        "delays unless asked. Keep the response concise while including the "
        "information needed to answer fully."
    )
    try:
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(
            executor,
            sync_generate,
            [{"role": "system", "content": system}, *history],
            app_state["selected_model"],
        )
        reply = reply.strip()
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as error:
        log_to_dash(f"Ollama error using {app_state['selected_model']}: {error}")
        if history and history[-1]["role"] == "user":
            history.pop()
        return "I could not process that message."


async def send_to_target(target, target_type, message):
    if target_type == "node":
        return await meshcore_instance.commands.send_msg(target, message)

    try:
        channel_index = int(target)
    except ValueError as error:
        raise ValueError("Channel target must be a numeric channel index") from error

    for name in ("send_chan_msg", "send_channel_msg", "send_channel_message"):
        method = getattr(meshcore_instance.commands, name, None)
        if method:
            return await method(channel_index, message)
    raise RuntimeError("This MeshCore version has no channel-send method")


async def handle_incoming_message(event):
    if not meshcore_instance or not app_state["is_connected"]:
        return

    packet = event.payload or {}
    sender = packet.get("sender") or packet.get("pubkey_prefix")
    text = packet.get("text", "").strip()
    if not sender or not text:
        return

    message_id = packet.get("id") or f"{sender}:{text}"
    if message_id in processed_messages:
        return
    processed_messages.add(message_id)
    add_chat_message("node", sender, "incoming", text)
    log_to_dash(f"Received DM from {sender}: {text}")

    reply = await generate_ai_response(sender, text)
    log_to_dash(f"AI reply: {reply}")
    reply_parts = split_reply_into_messages(reply)

    async with hardware_lock:
        try:
            contacts = await meshcore_instance.commands.get_contacts()
            recipient = sender
            if contacts.type != EventType.ERROR and contacts.payload:
                recipient = contacts.payload.get(sender, sender)
            for part_number, part in enumerate(reply_parts, start=1):
                result = await meshcore_instance.commands.send_msg(recipient, part)
                if result.type == EventType.ERROR:
                    log_to_dash(
                        f"Hardware rejected reply part {part_number}/"
                        f"{len(reply_parts)}: {result.payload}"
                    )
                    return
                add_chat_message("node", sender, "outgoing", part)
            log_to_dash(
                f"Direct message reply sent in {len(reply_parts)} message(s)."
            )
        except Exception as error:
            log_to_dash(f"Message send error: {error}")


async def handle_incoming_channel_message(event):
    if not meshcore_instance or not app_state["is_connected"]:
        return

    packet = event.payload or {}
    channel_index = packet.get("channel_idx")
    text = packet.get("text", "").strip()
    if channel_index is None or not text:
        return

    bot_prefix = f"{bot_settings['name']}:"
    message_text = text
    if message_text.startswith("[") and "] " in message_text:
        message_text = message_text.split("] ", 1)[1]
    if message_text.casefold().startswith(bot_prefix.casefold()):
        return

    channel_target = str(channel_index)
    message_id = packet.get("id") or (
        f"channel:{channel_target}:{packet.get('sender_timestamp', '')}:"
        f"{text}"
    )
    if message_id in processed_messages:
        return
    processed_messages.add(message_id)
    add_chat_message("channel", channel_target, "incoming", text)
    log_to_dash(f"Received channel {channel_target} message: {text}")

    reply = await generate_ai_response(
        f"channel:{channel_target}",
        text,
        allow_settings_update=False,
    )
    log_to_dash(f"AI channel reply: {reply}")
    reply_parts = split_reply_into_messages(
        reply,
        prefix=f"{bot_settings['name']}: ",
    )

    async with hardware_lock:
        try:
            for part_number, part in enumerate(reply_parts, start=1):
                result = await send_to_target(channel_target, "channel", part)
                if result.type == EventType.ERROR:
                    log_to_dash(
                        f"Hardware rejected channel reply part {part_number}/"
                        f"{len(reply_parts)}: {result.payload}"
                    )
                    return
                add_chat_message("channel", channel_target, "outgoing", part)
            log_to_dash(
                f"Channel reply sent in {len(reply_parts)} message(s)."
            )
        except Exception as error:
            log_to_dash(f"Channel message send error: {error}")


async def disconnect_hardware():
    global meshcore_instance
    if meshcore_instance is None:
        app_state["is_connected"] = False
        return
    try:
        method = getattr(meshcore_instance, "disconnect", None)
        if method:
            result = method()
            if inspect.isawaitable(result):
                await result
    except Exception as error:
        log_to_dash(f"Disconnect error: {error}")
    finally:
        meshcore_instance = None
        app_state["is_connected"] = False
        app_state["contacts"] = {}
        app_state["channels"] = {}
        app_state["gateway_telemetry"] = {}


async def bluetooth_scan_handler(request):
    try:
        devices = await BleakScanner.discover(timeout=5.0)
    except Exception as error:
        log_to_dash(f"Bluetooth scan failed: {error}")
        return web.json_response({"error": str(error)}, status=503)

    return web.json_response({
        "devices": [
            {
                "name": device.name or "Unnamed Bluetooth device",
                "address": device.address,
            }
            for device in sorted(
                devices,
                key=lambda device: (device.name or device.address).casefold(),
            )
        ]
    })


async def serial_scan_handler(request):
    try:
        ports = list_ports.comports()
    except Exception as error:
        log_to_dash(f"Serial port scan failed: {error}")
        return web.json_response({"error": str(error)}, status=503)

    return web.json_response({
        "ports": [
            {
                "device": port.device,
                "description": port.description,
            }
            for port in sorted(ports, key=lambda port: port.device.casefold())
        ]
    })


async def connect_hardware():
    global meshcore_instance
    await disconnect_hardware()
    try:
        if app_state["connection_type"] == "bluetooth":
            log_to_dash(f"Connecting via Bluetooth to {app_state['ble_mac']}...")
            meshcore_instance = await MeshCore.create_ble(app_state["ble_mac"])
        else:
            log_to_dash(f"Connecting via serial to {app_state['serial_port']}...")
            meshcore_instance = await MeshCore.create_serial(app_state["serial_port"])

        await meshcore_instance.start_auto_message_fetching()
        meshcore_instance.subscribe(EventType.CONTACT_MSG_RECV, handle_incoming_message)
        meshcore_instance.subscribe(
            EventType.CHANNEL_MSG_RECV,
            handle_incoming_channel_message,
        )
        app_state["is_connected"] = True
        await refresh_contacts()
        await refresh_channels()
        log_to_dash("Hardware interface successfully linked and active.")
    except Exception as error:
        meshcore_instance = None
        app_state["is_connected"] = False
        log_to_dash(f"Hardware connection error: {error}")


async def telemetry_loop():
    while True:
        try:
            if app_state["is_connected"] and meshcore_instance:
                await refresh_contacts()
                await refresh_gateway_telemetry()
                await refresh_channels()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_to_dash(f"Telemetry error: {error}")
            await asyncio.sleep(30)


PAGE = r'''<!DOCTYPE html>
<html><head><title>MeshCore AI Bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script defer src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{color-scheme:dark;--page-bg:#0d1117;--panel-bg:#161b22;--panel-raised:#21262d;--text:#c9d1d9;--muted:#8b949e;--accent:#4ade80;--accent-dim:#1a3a25;--border:#30363d;--input-bg:#0d1117;--input-border:#3b444e;--button-text:#07130b;--log-bg:#0b1016;--danger:#f85149}
*{box-sizing:border-box}
body{min-height:100vh;margin:0;padding:10px;display:flex;flex-direction:column;background-color:var(--page-bg);background-image:linear-gradient(rgba(74,222,128,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(74,222,128,.025) 1px,transparent 1px);background-size:28px 28px;color:var(--text);font:13px/1.45 "Segoe UI",system-ui,sans-serif}
body[data-theme="light"]{color-scheme:light;--page-bg:#eef2f5;--panel-bg:#fff;--panel-raised:#f2f5f7;--text:#17202a;--muted:#607080;--accent:#087f5b;--accent-dim:#e2f2eb;--border:#d8e0e7;--input-bg:#f7f9fb;--input-border:#b8c4ce;--button-text:#fff;--log-bg:#17202a}
body[data-theme="ocean"]{--page-bg:#081a26;--panel-bg:#102737;--panel-raised:#183648;--accent:#36d1dc;--accent-dim:#123c43;--border:#284657;--input-bg:#0b202e;--input-border:#35596a;--button-text:#071a2b;--log-bg:#06131d}
body[data-theme="amber"]{--page-bg:#21180d;--panel-bg:#342311;--panel-raised:#443018;--accent:#ffb703;--accent-dim:#493611;--border:#795722;--input-bg:#24190c;--input-border:#80602c;--button-text:#21180d;--log-bg:#160f08}
body[data-theme="linux"]{--page-bg:#020702;--panel-bg:#081008;--panel-raised:#0d190d;--accent:#39ff14;--accent-dim:#10260d;--border:#245b24;--input-bg:#050b05;--input-border:#245b24;--button-text:#020502;--log-bg:#000}
body[data-theme="macos"]{color-scheme:light;--page-bg:#e7ebf0;--panel-bg:#fff;--panel-raised:#f3f5f8;--text:#1d1d1f;--muted:#6e6e73;--accent:#007aff;--accent-dim:#e5f1ff;--border:#c7c7cc;--input-bg:#f5f5f7;--input-border:#c7c7cc;--button-text:#fff;--log-bg:#1d1d1f}
body[data-theme="cyberpunk"]{--page-bg:#090511;--panel-bg:#160b24;--panel-raised:#211033;--accent:#ff2bd6;--accent-dim:#351040;--border:#74358c;--input-bg:#10081a;--input-border:#74358c;--button-text:#090511;--log-bg:#050208}
.dashboard-header,.grid{width:min(100%,1800px);margin-right:auto;margin-left:auto}
.dashboard-header{min-height:58px;margin-bottom:12px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;gap:18px;background:var(--panel-bg);border:1px solid var(--border);border-radius:8px}
.brand-lockup{display:flex;align-items:center;gap:10px;min-width:max-content}
.brand-mark{width:34px;height:34px;display:grid;place-items:center;border:1px solid color-mix(in srgb,var(--accent) 38%,var(--border));border-radius:7px;background:var(--accent-dim);color:var(--accent);font:700 12px/1 ui-monospace,monospace}
.brand-copy h1{margin:0;color:var(--text);font-size:16px;font-weight:600;line-height:1.2}
.brand-copy h1 span{color:var(--accent);font-weight:500}
.header-label,.eyebrow{display:block;margin-bottom:3px;color:var(--muted);font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase}
.header-meta{display:flex;align-items:center;gap:16px;min-width:0}
.header-metric{color:var(--text);font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.header-status,.live-tag{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border:1px solid var(--border);border-radius:5px;background:var(--panel-raised);color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.5px;white-space:nowrap}
.header-status::before,.live-tag::before{width:6px;height:6px;border-radius:50%;background:var(--danger);content:""}
.header-status.connected{border-color:color-mix(in srgb,var(--accent) 35%,var(--border));background:var(--accent-dim);color:var(--accent)}
.header-status.connected::before,.live-tag::before{background:var(--accent);box-shadow:0 0 8px color-mix(in srgb,var(--accent) 65%,transparent)}
.header-controls{display:flex;align-items:center;gap:8px}
.header-controls label{margin:0;color:var(--muted);font-size:10px}
.header-controls select{width:auto;min-width:108px;margin:0;padding:6px 24px 6px 8px}
.top-nav{display:flex;align-items:center;gap:4px;flex:1;min-width:max-content}
.nav-tab{display:inline-flex;align-items:center;gap:7px;min-height:32px;padding:6px 10px;border-color:transparent;background:transparent;color:var(--muted);font-size:11px}
.nav-tab:hover,.nav-tab[aria-pressed="true"]{border-color:var(--border);background:var(--accent-dim);color:var(--accent);transform:none}
.nav-count{min-width:18px;padding:1px 5px;border-radius:10px;background:var(--panel-raised);font:10px ui-monospace,monospace;text-align:center}
.view-panel[hidden]{display:none!important}
.page-view{width:min(100%,1800px);flex:1;margin:0 auto}
.connection-layout{width:min(100%,520px);display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:start}
.settings-layout{width:min(100%,760px);display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:start}
.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.settings-item{min-width:0;padding:12px;border:1px solid var(--border);border-radius:6px;background:var(--panel-raised)}
.settings-item select{margin-top:4px}
.settings-description{margin:8px 0 0;color:var(--muted);font-size:11px}
.card{min-width:0;margin-bottom:12px;padding:12px;background:var(--panel-bg);border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12)}
.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.panel-heading h2{margin:0;color:var(--text);font-size:14px;font-weight:600;line-height:1.25}
.panel-index{color:var(--muted);font:11px ui-monospace,monospace}
label{display:block;margin:9px 0 5px;color:var(--muted);font-size:11px;font-weight:600}
input,select,button{font:inherit}
input,select{width:100%;min-width:0;margin:0;padding:9px 10px;border:1px solid var(--input-border);border-radius:5px;background:var(--input-bg);color:var(--text);outline:none}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 18%,transparent)}
button{min-height:36px;padding:8px 12px;border:1px solid var(--border);border-radius:5px;background:var(--panel-raised);color:var(--text);font-size:11px;font-weight:650;cursor:pointer;transition:background .15s,border-color .15s,transform .15s}
button:hover{transform:translateY(-1px);border-color:var(--accent);background:var(--accent-dim)}
.connection-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
.connection-actions button:first-child{border-color:var(--accent);background:var(--accent);color:var(--button-text)}
.connection-actions button:first-child:hover{background:color-mix(in srgb,var(--accent) 85%,white)}
.scan-control{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}
.scan-control button{min-width:78px}
.scan-status{min-height:18px;margin:5px 0 0;color:var(--muted);font-size:10px}
.scan-status[data-state="error"]{color:var(--danger)}
.chat-panel{height:min(680px,calc(100vh - 270px));min-height:360px;display:flex;flex-direction:column}
.chat-target{margin-bottom:10px}
#node-chat-history,#channel-chat-history{flex:1;min-height:220px;overflow-y:auto;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--log-bg);white-space:pre-wrap;overflow-wrap:anywhere}
#node-chat-history:empty::before,#channel-chat-history:empty::before{display:block;padding:8px;color:var(--muted);font-size:11px;content:"No messages in this view yet"}
.chat-message{max-width:92%;width:fit-content;margin:6px 0;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--panel-raised);color:var(--text);text-align:left;white-space:pre-wrap;overflow-wrap:anywhere}
.chat-message.incoming{margin-right:auto}
.chat-message.outgoing{margin-left:auto;border-color:color-mix(in srgb,var(--accent) 36%,var(--border));background:var(--accent-dim);color:var(--text)}
.chat-panel form{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:10px}
.chat-panel form input{min-width:0}
.chat-panel form button{border-color:var(--accent);background:var(--accent);color:var(--button-text);white-space:nowrap}
.console-dock{position:sticky;bottom:0;z-index:800;width:min(100%,1800px);margin:12px auto 0;padding:8px 0 0;background:linear-gradient(transparent,var(--page-bg) 12px)}
.console-card{height:132px;min-height:132px;max-height:132px;margin:0;padding:10px;display:flex;flex-direction:column}
.console-card .panel-heading{margin-bottom:6px}
.console-card pre{flex:1;min-height:0;max-height:none;margin:0;padding:8px;overflow:auto;border:1px solid var(--border);border-radius:6px;background:var(--log-bg);color:var(--muted);font:11px/1.45 ui-monospace,"SFMono-Regular",monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.map-workspace{width:min(100%,1800px);margin:0 auto}
.map-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:12px;padding:12px 14px}
.map-toolbar h2{margin:0;color:var(--text);font-size:15px;font-weight:600}
.map-layout{display:grid;grid-template-columns:minmax(230px,300px) minmax(0,1fr);gap:12px}
.map-rail{min-height:520px;margin:0;display:flex;flex-direction:column}
.map-rail-summary{margin:0 0 12px;color:var(--muted);font-size:11px}
#map-node-list{flex:1;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--log-bg)}
.map-empty{padding:12px;color:var(--muted);font-size:11px}
.map-node-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px;border-bottom:1px solid var(--border)}
.map-node-row:last-child{border-bottom:0}
.map-node-row[role="button"]{cursor:pointer}
.map-node-row[role="button"]:hover,.map-node-row[role="button"]:focus{outline:0;background:var(--panel-raised)}
.map-node-row strong,.map-node-row div>span{display:block;overflow-wrap:anywhere}
.map-node-row strong{color:var(--text);font-size:11px}
.map-node-row div>span{margin-top:2px;color:var(--muted);font:10px ui-monospace,monospace}
.map-location{flex:none;font:9px ui-monospace,monospace;letter-spacing:.35px}
.map-location.located{color:var(--accent)}
.map-location.unlocated{color:var(--muted)}
.map-surface{position:relative;min-width:0;min-height:520px;margin:0;padding:0;overflow:hidden}
#map-canvas{width:100%;height:min(720px,calc(100vh - 170px));min-height:520px;background:#d9e2df}
.map-message{position:absolute;z-index:500;top:14px;left:50%;max-width:calc(100% - 28px);padding:8px 12px;transform:translateX(-50%);border:1px solid var(--border);border-radius:5px;background:var(--panel-bg);color:var(--muted);font-size:11px;text-align:center;box-shadow:0 4px 14px rgba(0,0,0,.2)}
.map-message[hidden]{display:none}
.leaflet-container{font:12px/1.4 "Segoe UI",system-ui,sans-serif}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:var(--panel-bg);color:var(--text)}
.leaflet-popup-content{margin:10px 12px}
.leaflet-control-attribution{font-size:9px!important}
@media(max-width:1050px){.header-meta{gap:10px}}
@media(max-width:1050px){.dashboard-header{flex-wrap:wrap}.top-nav{order:3;flex-basis:100%}.map-layout{grid-template-columns:minmax(210px,260px) minmax(0,1fr)}}
@media(max-width:720px){body{padding:8px}.dashboard-header{align-items:flex-start;flex-direction:column;gap:12px}.top-nav{order:0;max-width:100%;overflow-x:auto}.nav-tab{flex:none}.header-meta{width:100%;flex-wrap:wrap;justify-content:space-between}.settings-grid{grid-template-columns:minmax(0,1fr)}.chat-panel{height:calc(100vh - 250px);min-height:340px}.map-layout{grid-template-columns:minmax(0,1fr)}.map-rail{min-height:180px;max-height:230px}.map-surface{min-height:48vh}#map-canvas{height:50vh;min-height:320px}.map-toolbar{align-items:flex-start;flex-direction:column}.console-card{height:120px;min-height:120px;max-height:120px}}
</style>
<script>
let gatewayTelemetry={};
function applyTheme(theme){document.body.dataset.theme=theme;localStorage.setItem('meshcore-theme',theme);document.getElementById('theme-select').value=theme}
function loadTheme(){applyTheme(localStorage.getItem('meshcore-theme')||'midnight')}
function fields(){let t=connection_type.value;document.getElementById('ble-field').style.display=t==='bluetooth'?'block':'none';document.getElementById('serial-field').style.display=t==='serial'?'block':'none'}
async function scanDevices(kind){let bluetooth=kind==='bluetooth',select=document.getElementById(bluetooth?'ble_mac':'serial_port'),button=document.getElementById(bluetooth?'ble-scan':'serial-scan'),statusMessage=document.getElementById(bluetooth?'ble-scan-status':'serial-scan-status'),previous=select.value;button.disabled=true;button.textContent='Scanning';statusMessage.dataset.state='';statusMessage.textContent='Searching for available devices...';try{let response=await fetch('/api/scan/'+kind),data=await response.json();if(!response.ok)throw new Error(data.error||'Device scan failed');let devices=bluetooth?data.devices:data.ports;select.replaceChildren(new Option(bluetooth?'Select a Bluetooth device':'Select a serial port',''));for(let device of devices){let label=bluetooth?`${device.name} (${device.address})`:`${device.device} - ${device.description||'Serial port'}`;select.add(new Option(label,bluetooth?device.address:device.device))}if(previous&&[...select.options].some(option=>option.value===previous))select.value=previous;statusMessage.textContent=devices.length?`Found ${devices.length} device(s). Select one to connect.`:'No devices found. Check that the radio is powered and discoverable.'}catch(error){statusMessage.dataset.state='error';statusMessage.textContent=error.message}finally{button.disabled=false;button.textContent='Scan'}}
function scanBluetooth(){return scanDevices('bluetooth')}
function scanSerial(){return scanDevices('serial')}
function updateClock(){document.getElementById('current-datetime').textContent=new Date().toLocaleString()}
async function status(){let r=await fetch('/api/status'),d=await r.json();let b=document.getElementById('status');b.textContent=d.is_connected?'CONNECTED':'DISCONNECTED';b.className='header-status '+(d.is_connected?'connected':'disconnected');document.getElementById('console').innerText=d.logs.join('\n')}
let mapNodes=[];
let dashboardMap=null;
let mapMarkers=null;
let mapBoundsSignature='';
function showView(view){let target=document.getElementById(view+'-view');if(!target)return;document.querySelectorAll('.view-panel').forEach(panel=>panel.hidden=panel!==target);document.querySelectorAll('.nav-tab').forEach(tab=>tab.setAttribute('aria-pressed',String(tab.dataset.view===view)));if(view==='map')openMap()}
function openMap(){if(!window.L){document.getElementById('map-message').textContent='Map library unavailable. Check your internet connection and reload.';return}if(!dashboardMap){dashboardMap=L.map('map-canvas',{zoomControl:true}).setView([20,0],2);L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(dashboardMap);mapMarkers=L.layerGroup().addTo(dashboardMap)}setTimeout(()=>dashboardMap.invalidateSize(),80);renderMapMarkers()}
function popupContent(title,detail){let content=document.createElement('div');let heading=document.createElement('strong');heading.textContent=title;content.appendChild(heading);if(detail){let line=document.createElement('div');line.textContent=detail;content.appendChild(line)}return content}
function focusMapPoint(latitude,longitude){showView('map');if(dashboardMap)dashboardMap.setView([latitude,longitude],12)}
function renderMapMarkers(){if(!dashboardMap||!mapMarkers)return;mapMarkers.clearLayers();let bounds=[];for(let peer of mapNodes){if(!Number.isFinite(peer.latitude)||!Number.isFinite(peer.longitude))continue;let point=[peer.latitude,peer.longitude];L.circleMarker(point,{radius:7,color:'#0d1117',weight:2,fillColor:'#4ade80',fillOpacity:.95}).bindPopup(popupContent(peer.name,peer.id)).addTo(mapMarkers);bounds.push(point)}if(Number.isFinite(gatewayTelemetry.latitude)&&Number.isFinite(gatewayTelemetry.longitude)){let point=[gatewayTelemetry.latitude,gatewayTelemetry.longitude];L.circleMarker(point,{radius:9,color:'#0d1117',weight:2,fillColor:'#36d1dc',fillOpacity:1}).bindPopup(popupContent('This gateway','Current radio location')).addTo(mapMarkers);bounds.push(point)}let signature=JSON.stringify(bounds);if(bounds.length&&signature!==mapBoundsSignature){dashboardMap.fitBounds(bounds,{padding:[36,36],maxZoom:12});mapBoundsSignature=signature}else if(!bounds.length){mapBoundsSignature=''}document.getElementById('map-message').hidden=bounds.length>0;document.getElementById('map-message').textContent='No peer or gateway location data is available yet.'}
function renderMapNodes(){let list=document.getElementById('map-node-list');list.replaceChildren();let located=0;for(let peer of mapNodes){let row=document.createElement('div');row.className='map-node-row';let details=document.createElement('div');let name=document.createElement('strong');name.textContent=peer.name;let id=document.createElement('span');id.textContent=peer.id;details.append(name,id);let location=document.createElement('span');let hasLocation=Number.isFinite(peer.latitude)&&Number.isFinite(peer.longitude);location.className='map-location '+(hasLocation?'located':'unlocated');location.textContent=hasLocation?'LOCATED':'NO FIX';if(hasLocation){located++;row.tabIndex=0;row.setAttribute('role','button');row.addEventListener('click',()=>focusMapPoint(peer.latitude,peer.longitude));row.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();row.click()}})}row.append(details,location);list.appendChild(row)}let gatewayLocated=Number.isFinite(gatewayTelemetry.latitude)&&Number.isFinite(gatewayTelemetry.longitude);if(gatewayLocated){located++;let row=document.createElement('div');row.className='map-node-row';let details=document.createElement('div');let name=document.createElement('strong');name.textContent='This gateway';let id=document.createElement('span');id.textContent='Local radio';details.append(name,id);let location=document.createElement('span');location.className='map-location located';location.textContent='LOCATED';row.tabIndex=0;row.setAttribute('role','button');row.addEventListener('click',()=>focusMapPoint(gatewayTelemetry.latitude,gatewayTelemetry.longitude));row.append(details,location);list.appendChild(row)}if(!mapNodes.length&&!gatewayLocated){let empty=document.createElement('div');empty.className='map-empty';empty.textContent='No peers are available yet. Connect to a MeshCore radio to load contacts.';list.appendChild(empty)}document.getElementById('map-node-count').textContent=String(located);document.getElementById('map-peer-total').textContent=String(mapNodes.length);document.getElementById('map-node-summary').textContent=`${located} located / ${mapNodes.length} peers`;renderMapMarkers()}
async function peers(){let r=await fetch('/api/peers'),d=await r.json();gatewayTelemetry=d.gateway_telemetry||{};mapNodes=d.nodes||[];gateway_battery.textContent=gatewayTelemetry.battery!=null?gatewayTelemetry.battery+'%':'Unavailable';node.innerHTML='<option value="">Select node</option>';channel.innerHTML='<option value="">Select channel</option>';for(let n of mapNodes){node.add(new Option(n.name,n.id))}for(let c of d.channels||[]){channel.add(new Option(c.name,c.id))}renderMapNodes()}
async function history(t,id,boxId){let box=document.getElementById(boxId);if(!id){box.innerHTML='';return}let r=await fetch('/api/chat-history?target_type='+encodeURIComponent(t)+'&target='+encodeURIComponent(id)),d=await r.json();box.innerHTML='';for(let m of d.messages||[]){let e=document.createElement('div');e.className='chat-message '+m.direction;e.textContent='['+m.timestamp+'] '+m.text;box.appendChild(e)}box.scrollTop=box.scrollHeight}
function selectNode(){if(!node.value)return;history('node',node.value,'node-chat-history')}
function selectChannel(){if(!channel.value)return;history('channel',channel.value,'channel-chat-history')}
async function connect(){let r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({connection_type:connection_type.value,ble_mac:ble_mac.value,serial_port:serial_port.value,model:model.value})});let d=await r.json();if(!r.ok)alert(d.error);await status();await peers()}
async function disconnect(){await fetch('/api/disconnect',{method:'POST'});await status();await peers()}
async function sendMessage(e,targetId,type,messageId,historyId){e.preventDefault();let selected=document.getElementById(targetId).value;let input=document.getElementById(messageId);if(!selected)return alert('Select a '+type+' first');let r=await fetch('/api/transmit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:selected,target_type:type,text:input.value})});let d=await r.json();if(!r.ok)return alert(d.error);input.value='';await history(type,selected,historyId)}
window.addEventListener('DOMContentLoaded',()=>{loadTheme();fields();status();peers();updateClock();setInterval(updateClock,1000)});setInterval(status,2000);setInterval(peers,10000);
</script></head><body>
<header class="dashboard-header">
<div class="brand-lockup"><div class="brand-mark">MC</div><div class="brand-copy"><span class="header-label">LOCAL MESH / RADIO CONTROL</span><h1>MESHCORE <span>AI GATEWAY</span></h1></div></div>
<nav class="top-nav" aria-label="Dashboard pages"><button type="button" class="nav-tab" data-view="connection" aria-pressed="true" onclick="showView('connection')">Connection</button><button type="button" class="nav-tab" data-view="nodes" aria-pressed="false" onclick="showView('nodes')">Nodes</button><button type="button" class="nav-tab" data-view="channels" aria-pressed="false" onclick="showView('channels')">Channels</button><button type="button" class="nav-tab" data-view="map" aria-pressed="false" onclick="showView('map')">Map <span class="nav-count" id="map-node-count">0</span></button><button type="button" class="nav-tab" data-view="settings" aria-pressed="false" onclick="showView('settings')">Settings</button></nav>
<div class="header-meta">
<div><span class="header-label">LINK</span><span id="status" class="header-status disconnected">DISCONNECTED</span></div>
<div><span class="header-label">GATEWAY BATTERY</span><span id="gateway_battery" class="header-metric">Unavailable</span></div>
<div><span class="header-label">LOCAL TIME</span><span id="current-datetime" class="header-metric">--</span></div>
</div>
</header>
<main id="connection-view" class="view-panel page-view">
<div class="connection-layout">
<section class="card connection-card">
<div class="panel-heading"><div><span class="eyebrow">RADIO LINK</span><h2>Connection</h2></div><span class="panel-index">01</span></div>
<label for="connection_type">Connection type</label><select id="connection_type" onchange="fields()"><option value="bluetooth">Bluetooth</option><option value="serial">Serial</option></select>
<div id="ble-field"><label for="ble_mac">Bluetooth device</label><div class="scan-control"><select id="ble_mac"><option value="">Scan for Bluetooth devices</option></select><button id="ble-scan" type="button" onclick="scanBluetooth()">Scan</button></div><p id="ble-scan-status" class="scan-status" aria-live="polite"></p></div>
<div id="serial-field" style="display:none"><label for="serial_port">Serial port</label><div class="scan-control"><select id="serial_port"><option value="">Scan for serial ports</option></select><button id="serial-scan" type="button" onclick="scanSerial()">Scan</button></div><p id="serial-scan-status" class="scan-status" aria-live="polite"></p></div>
<div class="connection-actions"><button onclick="connect()">Connect</button><button onclick="disconnect()">Disconnect</button></div>
</section>
</div>
</main>
<main id="nodes-view" class="view-panel page-view" hidden>
<section class="card chat-panel"><div class="panel-heading"><div><span class="eyebrow">DIRECT MESSAGES</span><h2>Node Messages</h2></div><span class="panel-index">02</span></div><select class="chat-target" id="node" onchange="selectNode()"><option value="">Select node</option></select><div id="node-chat-history"></div><form onsubmit="sendMessage(event,'node','node','node-message','node-chat-history')"><input id="node-message" maxlength="200" placeholder="Message selected node" required><button>Send to Node</button></form></section>
</main>
<main id="channels-view" class="view-panel page-view" hidden>
<section class="card chat-panel"><div class="panel-heading"><div><span class="eyebrow">SHARED FREQUENCY</span><h2>Channel Messages</h2></div><span class="panel-index">03</span></div><select class="chat-target" id="channel" onchange="selectChannel()"><option value="">Select channel</option></select><div id="channel-chat-history"></div><form onsubmit="sendMessage(event,'channel','channel','channel-message','channel-chat-history')"><input id="channel-message" maxlength="200" placeholder="Message selected channel" required><button>Send to Channel</button></form></section>
</main>
<main id="settings-view" class="view-panel page-view" hidden>
<div class="settings-layout">
<section class="card"><div class="panel-heading"><div><span class="eyebrow">APPLICATION</span><h2>Settings</h2></div><span class="panel-index">04</span></div>
<div class="settings-grid">
<div class="settings-item"><label for="theme-select">Color theme</label><select id="theme-select" onchange="applyTheme(this.value)"><option value="midnight">Midnight</option><option value="light">Light</option><option value="ocean">Ocean</option><option value="amber">Amber</option><option value="linux">Linux Console</option><option value="macos">macOS</option><option value="cyberpunk">Hacker Cyberpunk</option></select><p class="settings-description">Changes the dashboard appearance and saves your choice in this browser.</p></div>
<div class="settings-item"><label for="model">Ollama model</label><select id="model">{{MODEL_OPTIONS}}</select><p class="settings-description">The selected model will be used when connecting to the MeshCore bot.</p></div>
</div>
</section>
</div>
</main>
<main id="map-view" class="map-workspace view-panel page-view" hidden>
<section class="card map-toolbar"><div><span class="eyebrow">LIVE MESH POSITIONS</span><h2>Network Map</h2></div><span class="live-tag" id="map-node-summary">0 of 0 locations</span></section>
<div class="map-layout">
<aside class="card map-rail"><div class="panel-heading"><div><span class="eyebrow">KNOWN PEERS</span><h2>Nodes</h2></div><span class="panel-index" id="map-peer-total">0</span></div><p class="map-rail-summary">Select a located node to center the map.</p><div id="map-node-list"><div class="map-empty">Waiting for nodes...</div></div></aside>
<section class="card map-surface" aria-label="Mesh node map"><div id="map-canvas"></div><div class="map-message" id="map-message">Waiting for map data...</div></section>
</div>
</main>
<footer id="console-dock" class="console-dock"><section class="card console-card"><div class="panel-heading"><div><span class="eyebrow">SYSTEM ACTIVITY</span><h2>Console</h2></div><span class="live-tag">LIVE</span></div><pre id="console"></pre></section></footer>
</body></html>'''


async def index_handler(request):
    options = "".join(
        f'<option value="{html.escape(model)}">{html.escape(model)}</option>'
        for model in app_state["available_models"]
    )
    return web.Response(
        text=PAGE.replace("{{MODEL_OPTIONS}}", options),
        content_type="text/html",
    )


async def status_handler(request):
    return web.json_response({
        "is_connected": app_state["is_connected"],
        "logs": app_state["logs"],
    })


async def peers_handler(request):
    await refresh_contacts()
    nodes = []
    for node_id, entry in app_state["contacts"].items():
        coordinates = coordinates_from_entry(entry)
        nodes.append({
            "id": str(node_id),
            "name": display_name(node_id, entry),
            "latitude": coordinates[0] if coordinates else None,
            "longitude": coordinates[1] if coordinates else None,
        })
    return web.json_response({
        "nodes": nodes,
        "channels": [
            {"id": str(i), "name": display_name(i, value)}
            for i, value in app_state["channels"].items()
        ],
        "gateway_telemetry": app_state["gateway_telemetry"],
    })


async def chat_history_handler(request):
    target_type = request.query.get("target_type", "node")
    target = request.query.get("target", "")
    return web.json_response({"messages": chat_history.get(chat_key(target_type, target), [])})


async def connect_handler(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    connection_type = data.get("connection_type", "bluetooth")
    ble_mac = str(data.get("ble_mac", "")).strip()
    serial_port = str(data.get("serial_port", "")).strip()

    if connection_type not in {"bluetooth", "serial"}:
        return web.json_response({"error": "Invalid connection type"}, status=400)
    if connection_type == "bluetooth" and not ble_mac:
        return web.json_response({"error": "Scan and select a Bluetooth device"}, status=400)
    if connection_type == "serial" and not serial_port:
        return web.json_response({"error": "Scan and select a serial port"}, status=400)

    app_state["connection_type"] = connection_type
    app_state["ble_mac"] = ble_mac
    app_state["serial_port"] = serial_port
    if data.get("model"):
        app_state["selected_model"] = str(data["model"])

    await connect_hardware()
    return web.json_response({"is_connected": app_state["is_connected"]})


async def disconnect_handler(request):
    await disconnect_hardware()
    return web.json_response({"is_connected": False})


async def transmit_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    target = str(data.get("target", "")).strip()
    target_type = str(data.get("target_type", "node")).strip()
    message = str(data.get("text", "")).strip()

    if target_type not in {"node", "channel"} or not target or not message:
        return web.json_response({"error": "Target and message are required"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    try:
        async with hardware_lock:
            result = await send_to_target(target, target_type, message)
        if result.type == EventType.ERROR:
            return web.json_response({"error": str(result.payload)}, status=500)
        add_chat_message(target_type, target, "outgoing", message)
        log_to_dash(f"{target_type.title()} message sent to {target}")
        return web.json_response({"success": True})
    except Exception as error:
        log_to_dash(f"Transmit error: {error}")
        return web.json_response({"error": str(error)}, status=500)


async def on_startup(app):
    app["telemetry_task"] = asyncio.create_task(telemetry_loop())
    app["ollama_task"] = asyncio.create_task(update_available_models())


async def on_cleanup(app):
    for task in (app["telemetry_task"], app["ollama_task"]):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await disconnect_hardware()
    executor.shutdown(wait=False)


def create_app():
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/peers", peers_handler)
    app.router.add_get("/api/scan/bluetooth", bluetooth_scan_handler)
    app.router.add_get("/api/scan/serial", serial_scan_handler)
    app.router.add_get("/api/chat-history", chat_history_handler)
    app.router.add_post("/api/connect", connect_handler)
    app.router.add_post("/api/disconnect", disconnect_handler)
    app.router.add_post("/api/transmit", transmit_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    threading.Timer(
        1.0,
        lambda: webbrowser.open(
            f"http://127.0.0.1:{WEB_PORT}"
        ),
    ).start()
    web.run_app(create_app(), host=WEB_HOST, port=WEB_PORT)
