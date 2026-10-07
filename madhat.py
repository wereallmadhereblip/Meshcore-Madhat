import asyncio
import getpass
import hashlib
import hmac
import html
import inspect
import json
import math
import os
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from bleak import BleakScanner
import ollama
from aiohttp import ClientSession, ClientTimeout, web
from meshcore import EventType, MeshCore
from serial.tools import list_ports

DEFAULT_MODEL = "llama3.2:1b"
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
AUTO_RECONNECT_INTERVAL_SECONDS = 10
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
MAX_CHANNELS = 40
MAX_MESHCORE_MESSAGE_LENGTH = 125
# Bytes; below the firmware's 160-byte text limit to leave room for the sender-name prefix.
MAX_SINGLE_MESSAGE_LENGTH = 125
MAX_AI_REPLY_PACKETS = 12
RESPONSE_LENGTH_PACKET_LIMITS = {"short": 3, "medium": 6, "long": MAX_AI_REPLY_PACKETS}
DEFAULT_RESPONSE_LENGTH = "medium"
BATTERY_MIN_MV = 3200
BATTERY_MAX_MV = 4200
DEFAULT_BOT_NAME = "MeshCore Assistant"
DEFAULT_BOT_PERSONALITY = "helpful, friendly, and concise"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
BOT_SETTINGS_PATH = CONFIG_DIR / "meshcore-ollama-bot" / "bot_settings.json"
CHAT_HISTORY_PATH = CONFIG_DIR / "meshcore-ollama-bot" / "chat_history.json"
PREFERENCES_PATH = CONFIG_DIR / "meshcore-ollama-bot" / "preferences.json"
LOG_FILE_PATH = CONFIG_DIR / "meshcore-ollama-bot" / "app.log"
LOG_FILE_MAX_BYTES = 2 * 1024 * 1024
LOG_VIEW_MAX_LINES = 2000
CONFIG_FILE_PATH = Path(__file__).resolve().with_name("config.json")
AUTOSTART_UNIT_NAME = "meshcore-madhat.service"
AUTOSTART_UNIT_PATH = CONFIG_DIR / "systemd" / "user" / AUTOSTART_UNIT_NAME
TIGHTVNC_CERT_PATH = Path.home() / "novnc.pem"
TIGHTVNC_PID_PATH = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "meshcore-madhat" / "websockify.pid"
TIGHTVNC_LOG_PATH = TIGHTVNC_PID_PATH.with_name("websockify.log")
SSH_TERMINAL_PORT = 7681
SSH_TERMINAL_PID_PATH = TIGHTVNC_PID_PATH.with_name("ttyd.pid")
SSH_TERMINAL_LOG_PATH = TIGHTVNC_PID_PATH.with_name("ttyd.log")
NOVNC_WEB_PATH = Path("/usr/share/novnc")
AVAILABLE_THEMES = {"midnight", "light", "ocean", "amber", "linux", "macos", "cyberpunk", "tron"}
ADMIN_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{6,64}$")
TIME_OF_DAY_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def should_auto_open_browser():
    default = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    try:
        preferences = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if isinstance(preferences, dict) and isinstance(preferences.get("auto_open_browser"), bool):
        return preferences["auto_open_browser"]
    return default


def clean_bot_setting(value, limit):
    return value.strip().strip(" \t\r\n\"'`.,!?")[:limit].strip()


def load_bot_settings():
    settings = {
        "name": DEFAULT_BOT_NAME,
        "personality": DEFAULT_BOT_PERSONALITY,
        "response_length": DEFAULT_RESPONSE_LENGTH,
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
    response_length = saved_settings.get("response_length")
    if response_length in RESPONSE_LENGTH_PACKET_LIMITS:
        settings["response_length"] = response_length
    return settings


def load_app_config():
    config = {
        "model": DEFAULT_MODEL,
        "theme": "midnight",
        "connection": {"type": "serial", "ble_mac": "", "serial_port": DEFAULT_SERIAL_PORT},
        "weather": {"city": "", "state": ""},
        "bot": {**load_bot_settings(), "greet_new_users": False, "greet_channel": "", "admins": []},
        "ollama": {"schedule_enabled": False, "start_time": "07:00", "end_time": "17:00"},
        "auto_update": {"enabled": True},
    }
    try:
        saved_config = json.loads(CONFIG_FILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config

    if not isinstance(saved_config, dict):
        return config
    model = saved_config.get("model")
    if isinstance(model, str) and model.strip():
        config["model"] = model.strip()[:120]
    theme = saved_config.get("theme")
    if theme in AVAILABLE_THEMES:
        config["theme"] = theme
    saved_connection = saved_config.get("connection")
    if isinstance(saved_connection, dict):
        connection_type = saved_connection.get("type")
        if connection_type in {"bluetooth", "serial"}:
            config["connection"]["type"] = connection_type
        for key in ("ble_mac", "serial_port"):
            value = saved_connection.get(key)
            if isinstance(value, str):
                config["connection"][key] = value.strip()
    # Serial on the default port unless the user chose a device.
    if config["connection"]["type"] == "bluetooth" and not config["connection"]["ble_mac"]:
        config["connection"]["type"] = "serial"
    if config["connection"]["type"] == "serial" and not config["connection"]["serial_port"]:
        config["connection"]["serial_port"] = DEFAULT_SERIAL_PORT
    saved_weather = saved_config.get("weather")
    if isinstance(saved_weather, dict):
        for key in ("city", "state"):
            value = saved_weather.get(key)
            if isinstance(value, str):
                config["weather"][key] = value.strip()[:80]
    saved_bot = saved_config.get("bot")
    if isinstance(saved_bot, dict):
        for key, limit in (("name", 40), ("personality", 120)):
            value = saved_bot.get(key)
            if isinstance(value, str):
                value = clean_bot_setting(value, limit)
                if value:
                    config["bot"][key] = value
        response_length = saved_bot.get("response_length")
        if response_length in RESPONSE_LENGTH_PACKET_LIMITS:
            config["bot"]["response_length"] = response_length
        if isinstance(saved_bot.get("greet_new_users"), bool):
            config["bot"]["greet_new_users"] = saved_bot["greet_new_users"]
        greet_channel = saved_bot.get("greet_channel")
        if isinstance(greet_channel, str) and greet_channel.isdigit() and int(greet_channel) < MAX_CHANNELS:
            config["bot"]["greet_channel"] = greet_channel
        saved_admins = saved_bot.get("admins")
        if isinstance(saved_admins, list):
            config["bot"]["admins"] = [
                item.strip().lower() for item in saved_admins
                if isinstance(item, str) and ADMIN_KEY_PATTERN.match(item.strip())
            ]
    saved_ollama = saved_config.get("ollama")
    if isinstance(saved_ollama, dict):
        if isinstance(saved_ollama.get("schedule_enabled"), bool):
            config["ollama"]["schedule_enabled"] = saved_ollama["schedule_enabled"]
        if (
            isinstance(saved_ollama.get("greet_new_users"), bool)
            and not (
                isinstance(saved_bot, dict)
                and isinstance(saved_bot.get("greet_new_users"), bool)
            )
        ):
            config["bot"]["greet_new_users"] = saved_ollama["greet_new_users"]
        for key in ("start_time", "end_time"):
            value = saved_ollama.get(key)
            if isinstance(value, str) and TIME_OF_DAY_PATTERN.match(value):
                config["ollama"][key] = value
    greeting_disabled_without_channel = (
        config["bot"]["greet_new_users"] and not config["bot"]["greet_channel"]
    )
    if greeting_disabled_without_channel:
        config["bot"]["greet_new_users"] = False
    saved_auto_update = saved_config.get("auto_update")
    if isinstance(saved_auto_update, dict) and isinstance(saved_auto_update.get("enabled"), bool):
        config["auto_update"]["enabled"] = saved_auto_update["enabled"]
    if (
        not {"model", "theme", "connection", "weather", "bot", "ollama", "auto_update"}.issubset(saved_config)
        or isinstance(saved_ollama, dict) and "greet_new_users" in saved_ollama
        or greeting_disabled_without_channel
    ):
        try:
            write_app_config(config)
        except OSError:
            pass
    return config


def validate_app_config(value):
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object")
    unsupported = set(value) - {"model", "theme", "connection", "weather", "bot", "ollama", "auto_update"}
    if unsupported:
        raise ValueError(f"Unsupported configuration keys: {', '.join(sorted(unsupported))}")

    model = value.get("model", app_config["model"])
    if not isinstance(model, str) or not model.strip() or len(model.strip()) > 120:
        raise ValueError("Model must contain 1 to 120 characters")
    theme = value.get("theme", app_config["theme"])
    if theme not in AVAILABLE_THEMES:
        raise ValueError("Choose a supported dashboard theme")

    connection = value.get("connection", app_config["connection"])
    if not isinstance(connection, dict) or set(connection) - {"type", "ble_mac", "serial_port"}:
        raise ValueError("Connection settings must contain type, ble_mac, and serial_port")
    connection_type = connection.get("type", app_config["connection"]["type"])
    if connection_type not in {"bluetooth", "serial"}:
        raise ValueError("Connection type must be bluetooth or serial")
    connection_values = {"type": connection_type}
    for key in ("ble_mac", "serial_port"):
        setting = connection.get(key, app_config["connection"][key])
        if not isinstance(setting, str):
            raise ValueError(f"Connection {key} must be text")
        connection_values[key] = setting.strip()

    weather = value.get("weather", app_config["weather"])
    if not isinstance(weather, dict) or set(weather) - {"city", "state"}:
        raise ValueError("Weather location must contain only city and state")
    weather_values = {}
    for key in ("city", "state"):
        setting = weather.get(key, app_config["weather"][key])
        if not isinstance(setting, str) or len(setting.strip()) > 80:
            raise ValueError(f"Weather {key} must be text up to 80 characters")
        weather_values[key] = setting.strip()
    if weather_values["state"] and not weather_values["city"]:
        raise ValueError("Enter a city when specifying a state")

    bot = value.get("bot", app_config["bot"])
    if not isinstance(bot, dict) or set(bot) - {
        "name", "personality", "response_length", "greet_new_users", "greet_channel", "admins",
    }:
        raise ValueError("Bot settings contain an unsupported option")
    validated_bot = {}
    for key, limit in (("name", 40), ("personality", 120)):
        setting = bot.get(key, app_config["bot"][key])
        if not isinstance(setting, str):
            raise ValueError(f"Bot {key} must be text")
        setting = clean_bot_setting(setting, limit)
        if not setting:
            raise ValueError(f"Bot {key} cannot be empty")
        validated_bot[key] = setting
    response_length = bot.get("response_length", app_config["bot"]["response_length"])
    if response_length not in RESPONSE_LENGTH_PACKET_LIMITS:
        raise ValueError("Bot response_length must be short, medium, or long")
    validated_bot["response_length"] = response_length
    greet_new_users = bot.get("greet_new_users", app_config["bot"]["greet_new_users"])
    if not isinstance(greet_new_users, bool):
        raise ValueError("Bot greet_new_users must be true or false")
    validated_bot["greet_new_users"] = greet_new_users
    greet_channel = bot.get("greet_channel", app_config["bot"]["greet_channel"])
    if not isinstance(greet_channel, str) or (
        greet_channel and (not greet_channel.isdigit() or int(greet_channel) >= MAX_CHANNELS)
    ):
        raise ValueError("Bot greet_channel must be a configured channel index")
    if greet_new_users and not greet_channel:
        raise ValueError("Send /greet on in the channel where you want greetings sent")
    validated_bot["greet_channel"] = greet_channel
    admins = bot.get("admins", app_config["bot"].get("admins", []))
    if not isinstance(admins, list) or not all(
        isinstance(item, str) and ADMIN_KEY_PATTERN.match(item.strip()) for item in admins
    ):
        raise ValueError("Bot admins must be a list of hex public-key prefixes (6+ characters)")
    validated_bot["admins"] = [item.strip().lower() for item in admins]

    ollama_schedule = value.get("ollama", app_config["ollama"])
    if not isinstance(ollama_schedule, dict) or set(ollama_schedule) - {
        "schedule_enabled", "start_time", "end_time",
    }:
        raise ValueError("Ollama schedule must contain only schedule_enabled, start_time, and end_time")
    schedule_enabled = ollama_schedule.get("schedule_enabled", app_config["ollama"]["schedule_enabled"])
    if not isinstance(schedule_enabled, bool):
        raise ValueError("Ollama schedule_enabled must be true or false")
    ollama_values = {"schedule_enabled": schedule_enabled}
    for key in ("start_time", "end_time"):
        setting = ollama_schedule.get(key, app_config["ollama"][key])
        if not isinstance(setting, str) or not TIME_OF_DAY_PATTERN.match(setting):
            raise ValueError(f"Ollama {key} must be in HH:MM 24-hour format")
        ollama_values[key] = setting

    auto_update = value.get("auto_update", app_config["auto_update"])
    if not isinstance(auto_update, dict) or set(auto_update) - {"enabled"}:
        raise ValueError("Auto-update settings must contain only enabled")
    auto_update_enabled = auto_update.get("enabled", app_config["auto_update"]["enabled"])
    if not isinstance(auto_update_enabled, bool):
        raise ValueError("Auto-update enabled must be true or false")

    return {
        "model": model.strip(),
        "theme": theme,
        "connection": connection_values,
        "weather": weather_values,
        "bot": validated_bot,
        "ollama": ollama_values,
        "auto_update": {"enabled": auto_update_enabled},
    }


def write_app_config(config):
    temporary_path = CONFIG_FILE_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(CONFIG_FILE_PATH)


app_config = load_app_config()
bot_settings = app_config["bot"]


def current_reply_packet_limit():
    return RESPONSE_LENGTH_PACKET_LIMITS.get(
        bot_settings.get("response_length", DEFAULT_RESPONSE_LENGTH),
        RESPONSE_LENGTH_PACKET_LIMITS[DEFAULT_RESPONSE_LENGTH],
    )


def update_bot_settings_from_prompt(prompt):
    global app_config, bot_settings

    name_match = re.search(
        r"\b(?:call yourself|your name is|change your name to|set your name to|"
        r"rename yourself to)\s+(.+?)\s*[.!?]*$",
        prompt,
        re.IGNORECASE,
    )
    response_length_match = re.search(
        r"\b(?:make|keep|set)\s+(?:your\s+)?(?:responses?|replies)\s+"
        r"(short|medium|mid|long)\b",
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
    elif response_length_match:
        key = "response_length"
        value = response_length_match.group(1).lower()
        if value == "mid":
            value = "medium"
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

    updated_config = {
        **app_config,
        "bot": {**bot_settings, key: value},
    }
    try:
        write_app_config(updated_config)
    except OSError as error:
        log_to_dash(f"Failed to save bot settings: {error}")
        return "I couldn't save that change to config.json. Check the file permissions."

    app_config = updated_config
    bot_settings = app_config["bot"]
    if key == "name":
        return f"Understood. I'll go by {value} from now on."
    if key == "response_length":
        return f"Understood. I'll keep my responses {value} from now on."
    return f"Understood. I'll be {value} from now on."

app_state = {
    "connection_type": app_config["connection"]["type"],
    "ble_mac": app_config["connection"]["ble_mac"],
    "serial_port": app_config["connection"]["serial_port"],
    "selected_model": app_config["model"],
    "is_connected": False,
    "auto_reconnect": True,
    "logs": [],
    "trace_events": [],
    "available_models": [DEFAULT_MODEL, "qwen2.5:0.5b"],
    "models_info": [],
    "model_action_busy": False,
    "model_progress": None,
    "contacts": {},
    "channels": {},
    "limits": {"max_contacts": None, "max_channels": MAX_CHANNELS},
    "gateway_telemetry": {},
    "ollama_running": False,
    "incoming_message_count": 0,
    "latest_incoming_message": None,
}

executor = ThreadPoolExecutor(max_workers=1)
hardware_lock = asyncio.Lock()
# BlueZ rejects overlapping D-Bus operations (scan/connect/disconnect) with
# "Operation already in progress", so serialize them with their own lock
# instead of hardware_lock (which only guards post-connect commands).
connection_lock = asyncio.Lock()
# Sending back-to-back before the radio finishes the previous transmit
# trips firmware's ERR_CODE_BAD_STATE, so pace multi-part replies.
HARDWARE_SEND_INTERVAL = 1.5
# The firmware also returns ERR_CODE_BAD_STATE (or drops the response
# entirely) when a new command lands too soon after the previous one, even
# for unrelated commands like get_contacts/get_channel, so every command
# needs to wait out this gap since the last one finished.
HARDWARE_COMMAND_MIN_INTERVAL = 0.4
_last_hardware_command_at = 0.0


@asynccontextmanager
async def paced_hardware_lock():
    global _last_hardware_command_at
    async with hardware_lock:
        wait = HARDWARE_COMMAND_MIN_INTERVAL - (time.monotonic() - _last_hardware_command_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            yield
        finally:
            _last_hardware_command_at = time.monotonic()


conversation_history = defaultdict(list)
MAX_HISTORY_MESSAGES = 6
chat_history = defaultdict(list)
chat_metadata = {}
blocked_senders = set()
processed_messages = set()
announced_contact_adverts = {}
advert_seen_at = {}
meshcore_instance = None
ollama_process = None


def load_chat_store():
    try:
        saved_store = json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(saved_store, dict):
        return

    saved_chats = saved_store.get("chats", {})
    if isinstance(saved_chats, dict):
        for key, messages in saved_chats.items():
            if not isinstance(key, str) or not key.startswith(("node:", "channel:")):
                continue
            if not isinstance(messages, list):
                continue
            valid_messages = [
                message for message in messages[-100:]
                if isinstance(message, dict)
                and message.get("direction") in {"incoming", "outgoing"}
                and isinstance(message.get("text"), str)
            ]
            for message in valid_messages:
                if not isinstance(message.get("id"), str):
                    message["id"] = secrets.token_hex(6)
            if valid_messages:
                chat_history[key] = valid_messages

    saved_blocked = saved_store.get("blocked", [])
    if isinstance(saved_blocked, list):
        blocked_senders.update(
            name.casefold() for name in saved_blocked if isinstance(name, str) and name.strip()
        )

    saved_metadata = saved_store.get("metadata", {})
    if isinstance(saved_metadata, dict):
        for key, metadata in saved_metadata.items():
            if (
                isinstance(key, str)
                and key.startswith(("node:", "channel:"))
                and isinstance(metadata, dict)
            ):
                chat_metadata[key] = {
                    "archived": metadata.get("archived") is True,
                    "pinned": metadata.get("pinned") is True,
                }


def save_chat_store():
    temporary_path = CHAT_HISTORY_PATH.with_suffix(".json.tmp")
    store = {
        "chats": dict(chat_history),
        "metadata": chat_metadata,
        "blocked": sorted(blocked_senders),
    }
    try:
        CHAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(CHAT_HISTORY_PATH)
    except OSError as error:
        log_to_dash(f"Failed to save chat history: {error}")
        return False
    return True


load_chat_store()


def write_log_file(line):
    try:
        LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE_PATH.exists() and LOG_FILE_PATH.stat().st_size > LOG_FILE_MAX_BYTES:
            LOG_FILE_PATH.replace(LOG_FILE_PATH.with_name("app.log.1"))
        with LOG_FILE_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(line.replace("\n", " | ") + "\n")
    except OSError:
        pass


def read_log_file():
    lines = []
    for path in (LOG_FILE_PATH.with_name("app.log.1"), LOG_FILE_PATH):
        try:
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return lines[-LOG_VIEW_MAX_LINES:]


def log_to_dash(message):
    formatted = f"[{datetime.now():%I:%M:%S %p}] {message}"
    print(formatted)
    app_state["logs"].append(formatted)
    app_state["logs"] = app_state["logs"][-50:]
    write_log_file(f"[{datetime.now():%Y-%m-%d %I:%M:%S %p}] {message}")


def record_trace_event(kind, direction, target_id):
    target_id = str(target_id)
    collection = app_state["contacts"] if kind == "direct" else app_state["channels"]
    entry = next(
        (
            value
            for key, value in collection.items()
            if str(key) == target_id
            or (
                kind == "direct"
                and (
                    str(key).startswith(target_id)
                    or target_id.startswith(str(key))
                )
            )
        ),
        {},
    )
    trace_events = app_state["trace_events"]
    event_id = trace_events[-1]["id"] + 1 if trace_events else 1
    trace_events.append({
        "id": event_id,
        "timestamp": datetime.now().strftime("%I:%M:%S %p"),
        "kind": kind,
        "direction": direction,
        "target_id": target_id,
        "target_name": display_name(target_id, entry),
    })
    app_state["trace_events"] = trace_events[-60:]


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
        latitude = candidate.get(
            "latitude",
            candidate.get("lat", candidate.get("adv_lat")),
        )
        longitude = candidate.get(
            "longitude",
            candidate.get("lon", candidate.get("lng", candidate.get("adv_lon"))),
        )
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


def resolve_contact_id(candidate):
    """Map a raw sender id/pubkey prefix to the same contact key used by /api/peers.

    Incoming packets identify the sender by a short pubkey prefix, while the
    dashboard's node list keys chats by the full contact id, so without this
    the two never line up and dashboard-sent replies vanish from the thread.
    """
    candidate_str = str(candidate)
    if candidate_str in app_state["contacts"]:
        return candidate_str
    for contact_id, entry in app_state["contacts"].items():
        contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
        public_key = contact.get("public_key") if isinstance(contact, dict) else None
        if public_key and (
            str(public_key).startswith(candidate_str)
            or candidate_str.startswith(str(public_key))
        ):
            return str(contact_id)
    return candidate_str


class SingleMessage(str):
    """A reply sent as one packet with no [n/m] part markers, trimmed to fit."""


def split_reply_into_messages(reply, prefix="", max_parts=None):
    if isinstance(reply, SingleMessage):
        budget = MAX_SINGLE_MESSAGE_LENGTH - len(prefix.encode("utf-8"))
        text = reply.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" ,;:.")
        return [f"{prefix}{text}"]
    if len(reply) + len(prefix) <= MAX_MESHCORE_MESSAGE_LENGTH:
        return [f"{prefix}{reply}"]

    content_length = MAX_MESHCORE_MESSAGE_LENGTH - 10 - len(prefix)
    parts = []
    remaining = reply
    while remaining:
        split_at = min(content_length, len(remaining))
        if split_at < len(remaining):
            word_boundary = remaining.rfind("\n", 0, split_at)
            if word_boundary <= 0:
                word_boundary = remaining.rfind(" ", 0, split_at)
            if word_boundary > 0:
                boundary_split = word_boundary + 1
                parts_after_boundary = math.ceil(
                    (len(remaining) - boundary_split) / content_length
                )
                available_parts = None if max_parts is None else max_parts - len(parts) - 1
                if available_parts is None or parts_after_boundary <= available_parts:
                    split_at = boundary_split
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].lstrip()

    part_count = len(parts)
    return [
        f"[{part_number}/{part_count}] {prefix}{part}"
        for part_number, part in enumerate(parts, start=1)
    ]


def limit_ai_reply(reply, max_length):
    if len(reply) <= max_length:
        return reply

    shortened = reply[:max_length - 3]
    sentence_end = max(shortened.rfind(". "), shortened.rfind("! "), shortened.rfind("? "))
    if sentence_end >= max_length // 2:
        shortened = shortened[:sentence_end + 1]
    else:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:") + "..."


def parse_channel_sender(text):
    match = re.match(r"^\s*(?:\[([^\]]{1,40})\]|([^:\r\n\[]{1,40}):)\s", text)
    return (match.group(1) or match.group(2)).strip() if match else ""


def reception_info(packet):
    info = {}
    path_len = packet.get("path_len")
    if isinstance(path_len, int) and 0 <= path_len < 64:
        info["path_len"] = path_len
    snr = packet.get("SNR")
    if isinstance(snr, (int, float)):
        info["snr"] = snr
    if "path_len" in info:
        nodes = path_nodes(
            packet.get("path"),
            info["path_len"],
            packet.get("path_hash_mode") + 1 if isinstance(packet.get("path_hash_mode"), int) else None,
        )
        if nodes:
            info["path_nodes"] = nodes
    return info


def add_chat_message(target_type, target, direction, text, info=None):
    key = chat_key(target_type, target)
    now = datetime.now()
    message = {
        "id": secrets.token_hex(6),
        **(info or {}),
        "direction": direction,
        "text": text,
        "status": "sent" if direction == "outgoing" else "received",
        "timestamp": now.strftime("%I:%M:%S %p"),
        "sort_timestamp": now.timestamp(),
    }
    chat_history[key].append(message)
    chat_history[key] = chat_history[key][-100:]
    save_chat_store()
    if direction == "incoming":
        app_state["incoming_message_count"] += 1
        sender_name = ""
        channel_name = ""
        body = text
        if target_type == "channel":
            entry = next(
                (value for key, value in app_state["channels"].items() if str(key) == str(target)),
                None,
            )
            channel_name = display_name(target, entry) if entry is not None else f"Channel {target}"
            sender_match = re.match(r"^\s*(?:\[([^\]]{1,40})\]|([^:\r\n\[]{1,40}):)\s*(.*)$", text, re.DOTALL)
            if sender_match:
                sender_name = (sender_match.group(1) or sender_match.group(2)).strip()
                body = sender_match.group(3)
        else:
            entry = app_state["contacts"].get(str(target))
            sender_name = display_name(target, entry) if entry is not None else f"Node {str(target)[:8]}"
        app_state["latest_incoming_message"] = {
            "type": target_type,
            "target": str(target),
            "sender": sender_name,
            "channel": channel_name,
            "text": body[:120],
        }
    return message


def update_chat_message_status(message, status):
    message["status"] = status
    save_chat_store()


def get_chat_metadata(target_type, target):
    metadata = chat_metadata.get(chat_key(target_type, target), {})
    return {
        "archived": metadata.get("archived", False),
        "pinned": metadata.get("pinned", False),
    }


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


def system_battery_percentage():
    for supply_path in Path("/sys/class/power_supply").glob("*"):
        try:
            if (supply_path / "type").read_text(encoding="utf-8").strip() != "Battery":
                continue
            percentage = int((supply_path / "capacity").read_text(encoding="utf-8").strip())
            return max(0, min(100, percentage))
        except (OSError, ValueError):
            continue
    return None


def format_model_size(size_bytes):
    if not size_bytes or size_bytes <= 0:
        return ""
    try:
        b = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if b < 1024.0:
                return f"{b:.1f} {unit}" if unit != "B" else f"{int(b)} B"
            b /= 1024.0
        return f"{b:.1f} PB"
    except Exception:
        return ""


def get_model_name(m):
    name = getattr(m, "model", None) or getattr(m, "name", None)
    if not name and hasattr(m, "get"):
        name = m.get("model") or m.get("name")
    return str(name) if name else None


def get_model_size(m):
    size = getattr(m, "size", None)
    if size is None and hasattr(m, "get"):
        size = m.get("size")
    return size if isinstance(size, (int, float)) else None


async def fetch_available_models():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(executor, ollama.list)
    raw_models = getattr(result, "models", None)
    if raw_models is None and hasattr(result, "get"):
        raw_models = result.get("models", [])
    raw_models = raw_models or []

    models = []
    models_info = []
    for m in raw_models:
        name = get_model_name(m)
        if name:
            models.append(name)
            models_info.append({
                "name": name,
                "size": format_model_size(get_model_size(m)),
                "size_bytes": get_model_size(m),
            })
    if models:
        app_state["available_models"] = models
    app_state["models_info"] = models_info
    app_state["ollama_running"] = True


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


async def stop_ollama_server():
    """Stop the local Ollama server to save power, without crashing the dashboard.

    generate_ai_response() already treats any ollama.chat() failure as a soft
    error, so replies simply fall back to "I could not process that message."
    while the server is off instead of the dashboard crashing.
    """
    global ollama_process

    if ollama_process is not None and ollama_process.poll() is None:
        ollama_process.terminate()
        try:
            ollama_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ollama_process.kill()
        ollama_process = None
        app_state["ollama_running"] = False
        log_to_dash("Ollama server stopped to save power.")
        return

    # Not a process we spawned (e.g. started manually or by another session);
    # ask any running instance to exit gracefully rather than force-killing it.
    ollama_executable = shutil.which("ollama")
    if ollama_executable is None:
        app_state["ollama_running"] = False
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        executor,
        lambda: subprocess.run(
            ["pkill", "-f", "ollama serve"], capture_output=True, check=False
        ),
    )
    app_state["ollama_running"] = False
    log_to_dash("Ollama server stopped to save power.")


def _time_in_window(now, start_time, end_time):
    if start_time == end_time:
        return True
    if start_time < end_time:
        return start_time <= now < end_time
    # Window wraps past midnight (e.g. 22:00 to 06:00).
    return now >= start_time or now < end_time


async def ollama_schedule_loop():
    """Turn Ollama on/off automatically to match the configured time window."""
    while True:
        try:
            schedule = app_config["ollama"]
            if schedule["schedule_enabled"] and not system_sleep_active():
                start_time = datetime.strptime(schedule["start_time"], "%H:%M").time()
                end_time = datetime.strptime(schedule["end_time"], "%H:%M").time()
                should_run = _time_in_window(datetime.now().time(), start_time, end_time)
                if should_run and not app_state["ollama_running"]:
                    log_to_dash("Scheduled window started; turning Ollama on.")
                    await update_available_models()
                elif not should_run and app_state["ollama_running"]:
                    log_to_dash("Scheduled window ended; turning Ollama off.")
                    await stop_ollama_server()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_to_dash(f"Ollama schedule check failed: {error}")
        await asyncio.sleep(60)



async def refresh_contacts():
    if not meshcore_instance or not app_state["is_connected"]:
        return
    try:
        async with paced_hardware_lock():
            # Streaming a full contact list over BLE can take much longer
            # than the library's 5s default, especially with many peers.
            result = await meshcore_instance.commands.get_contacts(timeout=20)
        if result.type != EventType.ERROR:
            app_state["contacts"] = normalize_entries(result.payload)
        else:
            # Silent failures here previously left the dashboard showing an
            # empty node list with no indication the fetch ever ran.
            log_to_dash(f"Failed to fetch nodes: {result.payload}")
    except Exception as error:
        log_to_dash(f"Failed to fetch nodes: {error}")


def is_private_mesh_channel(channel_name, channel_secret):
    if not isinstance(channel_name, str) or not isinstance(channel_secret, (bytes, bytearray)):
        return False
    if len(channel_secret) != 16:
        return False
    public_secret = hashlib.sha256(channel_name.encode("utf-8")).digest()[:16]
    return bytes(channel_secret) != public_secret


async def refresh_device_limits():
    if not meshcore_instance or not app_state["is_connected"]:
        return
    query = getattr(meshcore_instance.commands, "send_device_query", None)
    if query is None:
        return
    try:
        async with paced_hardware_lock():
            result = await query()
        if result.type == EventType.ERROR or not isinstance(result.payload, dict):
            return
        for key in ("max_contacts", "max_channels"):
            value = result.payload.get(key)
            if isinstance(value, int) and value > 0:
                app_state["limits"][key] = value
    except Exception as error:
        log_to_dash(f"Could not read device limits: {error}")


async def refresh_channels():
    if not meshcore_instance or not app_state["is_connected"]:
        return

    commands = meshcore_instance.commands

    getter = getattr(commands, "get_channel", None)
    if getter is None:
        return

    channels = {}
    seen_channels = set()
    try:
        for channel_index in range(MAX_CHANNELS):
            try:
                # Lock per-channel (not the whole scan) so a long scan of
                # many empty channels doesn't block message sends for tens
                # of seconds. BLE round-trips can exceed a second right after
                # connecting, so allow more slack than the 1s used previously.
                async with paced_hardware_lock():
                    result = await asyncio.wait_for(
                        getter(channel_index),
                        timeout=3.0,
                    )
            except asyncio.TimeoutError:
                # Empty or unavailable channels commonly timeout; silence this
                # noise so the console remains readable while the scan continues.
                continue

            if result.type == EventType.ERROR or not result.payload:
                continue

            raw_channel_name = result.payload.get("channel_name", "")
            if not isinstance(raw_channel_name, str):
                continue
            channel_name = raw_channel_name.strip()
            channel_secret = result.payload.get("channel_secret")
            if not isinstance(channel_secret, (bytes, bytearray)):
                channel_secret = None
            else:
                channel_secret = bytes(channel_secret)
            channel_key = (channel_name.casefold(), channel_secret)

            # Some devices report the same channel (e.g. "Public") at more
            # than one index; keep only the first one so it isn't listed twice.
            if channel_name and channel_key not in seen_channels:
                seen_channels.add(channel_key)
                channels[str(channel_index)] = {
                    "name": channel_name,
                    "channel_idx": channel_index,
                    "is_private": is_private_mesh_channel(
                        raw_channel_name, channel_secret
                    ),
                }

        if not channels and app_state["channels"]:
            # Every channel timed out/errored this pass; keep the last known
            # good list instead of blanking the dashboard on a flaky scan.
            log_to_dash("Channel scan returned nothing; keeping previous channel list.")
        else:
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
    request_self_info = getattr(
        meshcore_instance.commands,
        "send_appstart",
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
            async with paced_hardware_lock():
                result = await request_self_telemetry()

            if result.type != EventType.ERROR:
                gateway_values = parse_lpp_telemetry(result.payload)

        except Exception as error:
            log_to_dash(f"Gateway telemetry request failed: {error}")

    if request_self_info is not None:
        try:
            async with paced_hardware_lock():
                result = await request_self_info()
            if result.type != EventType.ERROR and result.payload:
                latitude = result.payload.get("adv_lat")
                longitude = result.payload.get("adv_lon")
                coordinates = None
                if latitude not in (None, 0, 0.0) and longitude not in (None, 0, 0.0):
                    coordinates = coordinates_from_entry({
                        "latitude": latitude,
                        "longitude": longitude,
                    })
                if coordinates and gateway_values.get("latitude") is None:
                    gateway_values["latitude"], gateway_values["longitude"] = coordinates
        except Exception as error:
            log_to_dash(f"Gateway position request failed: {error}")

    if request_battery is not None:
        try:
            async with paced_hardware_lock():
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


WEATHER_CODES = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def wind_arrow(degrees_from):
    """Arrow pointing the way the wind is blowing (the API reports where it comes from)."""
    if degrees_from is None:
        return ""
    arrows = "↑↗→↘↓↙←↖"
    return arrows[round(((float(degrees_from) + 180) % 360) / 45) % 8]


async def geocode_location(session, location):
    # Open-Meteo's geocoding search wants just a place name, so a combined
    # "city state"/"city country" string (e.g. "hartford connecticut") often
    # returns no matches. Retry with trailing words dropped, but don't shrink
    # past 2 words unless the whole location was 1-2 words to begin with --
    # otherwise stray words from an unrelated sentence ("the", "for") can
    # fuzzy-match a real place and produce a bogus weather reply.
    words = location.split()
    minimum_words = 1 if len(words) <= 2 else 2
    for word_count in range(len(words), minimum_words - 1, -1):
        candidate = " ".join(words[:word_count])
        async with session.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": candidate, "count": 1, "language": "en", "format": "json"},
        ) as response:
            response.raise_for_status()
            places = (await response.json()).get("results", [])
        if places:
            return places[0]
    return None


# Words that can end up looking like a "location" after prompt parsing but
# are never actually place names, so treat them as no location found.
NON_LOCATION_WORDS = {
    "weather", "forecast", "temperature", "conditions", "today", "tonight",
    "tomorrow", "now", "here", "there", "it", "this", "that", "the", "a", "an",
}


def is_plausible_location(location):
    if not location or len(location.split()) > 5:
        return False
    words = {word.casefold() for word in re.findall(r"[a-z']+", location, re.IGNORECASE)}
    return bool(words - NON_LOCATION_WORDS)


async def fetch_weather_response(prompt, sender_id=None):
    command = re.fullmatch(r"\s*(?:wx|weather)\b[\s:,]*(.*?)\s*", prompt, re.IGNORECASE | re.DOTALL)
    if command is None:
        return None
    usage = "Usage: wx <zip code> or wx local. Add 'forecast' or 'c' for more."
    argument = re.sub(r"\b(?:celsius|metric|forecast|c)\b", "", command.group(1), flags=re.IGNORECASE).strip()
    if not argument:
        return usage
    local_requested = argument.lower() == "local"
    zip_match = re.fullmatch(r"\d{5}(?:-\d{4})?", argument)
    location = None if local_requested or zip_match else argument
    if location and not is_plausible_location(location):
        return usage
    use_metric = bool(re.search(
        r"\b(?:celsius|centigrade|metric|kmh|kph|kilometers? per hour|"
        r"kilometres? per hour|c)\b|°\s*c\b",
        prompt,
        re.IGNORECASE,
    ))
    forecast_requested = bool(
        re.search(r"\b(?:forecast|tomorrow|next few days|this week)\b", prompt, re.IGNORECASE)
    )
    temperature_unit = "celsius" if use_metric else "fahrenheit"
    wind_speed_unit = "kmh" if use_metric else "mph"

    try:
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            if zip_match:
                async with session.get(
                    f"https://api.zippopotam.us/us/{argument[:5]}"
                ) as response:
                    if response.status == 404:
                        return f"I couldn't find zip code {argument[:5]}."
                    response.raise_for_status()
                    zip_place = (await response.json())["places"][0]
                latitude = float(zip_place["latitude"])
                longitude = float(zip_place["longitude"])
                location_label = f"{zip_place['place name']}, {zip_place['state abbreviation']} {argument[:5]}"
            elif location:
                place = await geocode_location(session, location)
                if not place:
                    return f"I couldn't find {location}. Try wx <zip code>."
                latitude = place["latitude"]
                longitude = place["longitude"]
                location_label = ", ".join(
                    str(place[key]) for key in ("name", "admin1", "country") if place.get(key)
                )
            else:
                local_city = app_config["weather"]["city"]
                local_place = None
                if local_city:
                    local_place = await geocode_location(
                        session, f"{local_city} {app_config['weather']['state']}".strip()
                    )
                if local_place:
                    latitude = local_place["latitude"]
                    longitude = local_place["longitude"]
                    location_label = ", ".join(
                        str(local_place[key]) for key in ("name", "admin1") if local_place.get(key)
                    )
                else:
                    telemetry = app_state["gateway_telemetry"]
                    latitude = telemetry.get("latitude")
                    longitude = telemetry.get("longitude")
                    if latitude is None or longitude is None:
                        return "No local location set. Set a weather city in the app settings."
                    location_label = "the gateway location"

            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "forecast_days": 3,
                    "temperature_unit": temperature_unit,
                    "wind_speed_unit": wind_speed_unit,
                    "timezone": "auto",
                },
            ) as response:
                response.raise_for_status()
                weather = await response.json()

        current = weather["current"]
        temperature_label = "C" if use_metric else "F"
        wind_label = "km/h" if use_metric else "mph"
        code = int(current["weather_code"])
        answer = (
            f"{location_label}: {current['temperature_2m']:.0f}{temperature_label} "
            f"(feels {current['apparent_temperature']:.0f}), "
            f"{WEATHER_CODES.get(code, 'unknown')}, "
            f"humidity {current['relative_humidity_2m']}%, "
            f"wind {wind_arrow(current.get('wind_direction_10m'))}{current['wind_speed_10m']:.0f}{wind_label}"
        )
        if forecast_requested:
            daily = weather["daily"]
            for index, day_name in enumerate(("Today", "Tmrw")):
                answer += (
                    f". {day_name} {daily['temperature_2m_max'][index]:.0f}/"
                    f"{daily['temperature_2m_min'][index]:.0f}{temperature_label} "
                    f"{WEATHER_CODES.get(int(daily['weather_code'][index]), '')}"
                ).rstrip()
        return SingleMessage(answer)
    except Exception as error:
        log_to_dash(f"Live weather lookup failed: {error}")
        return "I couldn't retrieve live weather right now, so I don't want to guess. Please try again shortly."


def sync_generate(messages, model, max_chars=None):
    options = {"temperature": 0.2, "num_ctx": 4096, "repeat_penalty": 1.0}
    if max_chars:
        # Keep generation within Ollama's former default while avoiding excess output.
        options["num_predict"] = min(128, max(48, max_chars // 6 + 16))
    result = ollama.chat(
        model=model,
        messages=messages,
        options=options,
        keep_alive="30m",
    )
    return result["message"]["content"]


def looks_like_gibberish(text, prompt=""):
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 12:
        return False
    prompt_has_foreign = any(char.isalpha() and ord(char) > 0x24F for char in prompt)
    foreign = sum(1 for char in letters if ord(char) > 0x24F)
    if foreign and not prompt_has_foreign:
        return True
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) >= 8:
        counts = {}
        for word in words:
            counts[word] = counts.get(word, 0) + 1
        if len(counts) / len(words) < 0.5 or max(counts.values()) / len(words) > 0.25:
            return True
    raw_words = text.split()
    camel_mix = sum(1 for word in raw_words if re.search(r"[a-z][A-Z]|[A-Za-z][^\x00-\x7f]", word))
    return len(raw_words) > 8 and camel_mix / len(raw_words) > 0.25


def clear_chat_memory_reply(sender_id, prompt):
    if not re.search(
        r"\b(?:clear|forget|reset|wipe|erase)\b.*\b(?:chat|conversation)?\s*"
        r"(?:memory|history)\b",
        prompt,
        re.IGNORECASE,
    ):
        return None
    conversation_history.pop(sender_id, None)
    return "Done. I've cleared our chat memory."


def update_greeting_setting_from_message(prompt, channel_id=None):
    global app_config, bot_settings

    match = re.fullmatch(
        r"\s*(?:\[[^\]]+\]\s*|[^:\r\n]{1,40}:\s*)?/greet\s+(on|off)\s*",
        prompt,
        re.IGNORECASE,
    )
    if match is None:
        return None

    enabled = match.group(1).lower() == "on"
    greet_channel = app_config["bot"].get("greet_channel", "")
    if enabled and channel_id is not None:
        greet_channel = str(channel_id)
        if not greet_channel.isdigit() or int(greet_channel) >= MAX_CHANNELS:
            return "I couldn't identify that channel. Send /greet on in a configured channel."
    if enabled and not greet_channel:
        return "Send /greet on in the channel where you want new-user greetings sent."
    updated_config = {
        **app_config,
        "bot": {
            **app_config["bot"],
            "greet_new_users": enabled,
            "greet_channel": greet_channel,
        },
    }
    try:
        write_app_config(updated_config)
    except OSError as error:
        log_to_dash(f"Failed to save greeting setting: {error}")
        return "I couldn't save the greeting setting to config.json. Check the file permissions."

    app_config = updated_config
    bot_settings = app_config["bot"]
    if enabled:
        return f"New-user greetings are on in channel {greet_channel}."
    return "New-user greetings are off."


HELP_TEXT = (
    "Commands: /help, /help settings, /clearmemory, /settings (show), /settings <name> <value>, /restart, /update, /syssleep, /syswakeup, /syswifi status|on|off|scan|connect <SSID> [password]|disconnect|ap [off], /reboot, /sysreboot, /sysshutdown, /tightvnc start|off|restart, /power, /fastfetch, "
    "wx <zip>, wx local, "
    "/greet on|off (in a channel), /bot <question> (in a channel). "
    "/settings works in direct messages from admins only."
)
SETTINGS_HELP_TEXT = (
    "/settings names - App: name, personality, length (short|medium|long), model, "
    "theme, autoupdate on|off, city, state, schedule on|off, start HH:MM, end HH:MM. "
    "Device: devname, tx, freq, bw, sf, cr, repeat on|off, lat, lon, gps on|off, "
    "advert flood|zero, synctime, reboot confirm. System: startup on|off (start at boot)."
)
ON_VALUES = {"on", "true", "yes", "1"}
OFF_VALUES = {"off", "false", "no", "0"}


class InternalRequest:
    def __init__(self, payload, restart_delay=0.3):
        self._payload = payload
        self.restart_delay = restart_delay

    async def json(self):
        return self._payload


def is_settings_admin(sender_id):
    sender = str(sender_id).lower()
    return any(
        sender.startswith(admin) or admin.startswith(sender)
        for admin in app_config["bot"].get("admins", [])
    )


def parse_on_off(value):
    value = value.strip().lower()
    if value in ON_VALUES:
        return True
    if value in OFF_VALUES:
        return False
    raise ValueError("Use on or off")


def save_app_config_change(change):
    global app_config, bot_settings
    config = json.loads(json.dumps(app_config))
    change(config)
    config = validate_app_config(config)
    write_app_config(config)
    app_config = config
    bot_settings = app_config["bot"]
    app_state["selected_model"] = app_config["model"]


def app_settings_summary():
    bot = app_config["bot"]
    return (
        f"name={bot['name']}; length={bot['response_length']}; model={app_config['model']}; "
        f"theme={app_config['theme']}; autoupdate={'on' if app_config['auto_update']['enabled'] else 'off'}; "
        f"city={app_config['weather']['city'] or '-'}. See /help settings"
    )


async def run_device_handler(handler, payload):
    response = await handler(InternalRequest(payload))
    data = json.loads(response.text)
    if response.status >= 400:
        raise RuntimeError(data.get("error", "Device request failed"))
    return data


async def handle_settings_command(sender_id, args):
    parts = args.split(None, 1)
    key = parts[0].lower() if parts else ""
    value = parts[1].strip() if len(parts) > 1 else ""
    if not key:
        return app_settings_summary()
    if not is_settings_admin(sender_id):
        log_to_dash(f"Rejected /settings from non-admin {sender_id}")
        return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
    if not value and key not in {"advert", "synctime"}:
        return f"Usage: /settings {key} <value>. See /help settings"

    def set_bot(name, text=None):
        return lambda c: c["bot"].__setitem__(name, value if text is None else text)

    try:
        app_changes = {
            "name": set_bot("name"),
            "personality": set_bot("personality"),
            "length": set_bot("response_length", value.lower()),
            "model": lambda c: c.__setitem__("model", value),
            "theme": lambda c: c.__setitem__("theme", value.lower()),
            "city": lambda c: c["weather"].__setitem__("city", value),
            "state": lambda c: c["weather"].__setitem__("state", value),
            "start": lambda c: c["ollama"].__setitem__("start_time", value),
            "end": lambda c: c["ollama"].__setitem__("end_time", value),
        }
        if key == "autoupdate":
            enabled = parse_on_off(value)
            app_changes[key] = lambda c: c["auto_update"].__setitem__("enabled", enabled)
        elif key == "schedule":
            enabled = parse_on_off(value)
            app_changes[key] = lambda c: c["ollama"].__setitem__("schedule_enabled", enabled)
        if key in app_changes:
            save_app_config_change(app_changes[key])
            log_to_dash(f"Settings changed by {sender_id}: {key}")
            return f"Updated {key}."

        device_values = {
            "devname": lambda: {"name": value},
            "tx": lambda: {"tx_power": int(value)},
            "freq": lambda: {"radio_freq": float(value)},
            "bw": lambda: {"radio_bw": float(value)},
            "sf": lambda: {"radio_sf": int(value)},
            "cr": lambda: {"radio_cr": int(value)},
            "repeat": lambda: {"repeat": parse_on_off(value)},
            "lat": lambda: {"adv_lat": float(value)},
            "lon": lambda: {"adv_lon": float(value)},
            "gps": lambda: {"gps_enabled": parse_on_off(value)},
        }
        if key in device_values:
            await run_device_handler(update_device_settings_handler, {"values": device_values[key]()})
            log_to_dash(f"Device setting changed by {sender_id}: {key}")
            return f"Device {key} updated."
        if key == "startup":
            enabled = parse_on_off(value)
            await asyncio.to_thread(set_autostart, enabled)
            log_to_dash(f"Start at boot {'enabled' if enabled else 'disabled'} by {sender_id}")
            return f"Start at boot {'enabled' if enabled else 'disabled'}."
        if key == "advert":
            action = "advert_flood" if value.lower() == "flood" else "advert_zero_hop"
            await run_device_handler(device_action_handler, {"action": action})
            return "Advert sent."
        if key == "synctime":
            await run_device_handler(device_action_handler, {"action": "sync_time"})
            return "Device time synced."
        if key == "reboot":
            if value.lower() != "confirm":
                return "Send /settings reboot confirm to reboot the device."
            await run_device_handler(device_action_handler, {"action": "reboot"})
            return "Device rebooting."
    except ValueError as error:
        return f"Invalid value: {error}"
    except OSError:
        return "I couldn't save that change to config.json."
    except Exception as error:
        log_to_dash(f"/settings {key} failed: {error}")
        return f"Could not apply {key}: {error}"
    return "Unknown setting. See /help settings"


def run_wifi_command(action):
    commands = []
    if shutil.which("nmcli"):
        commands.append(["nmcli", "radio", "wifi", action] if action != "status" else ["nmcli", "radio", "wifi"])
    if shutil.which("rfkill"):
        commands.append({
            "on": ["rfkill", "unblock", "wifi"],
            "off": ["rfkill", "block", "wifi"],
            "status": ["rfkill", "list", "wifi"],
        }[action])
    if not commands:
        raise RuntimeError("nmcli and rfkill are not installed")
    last_error = ""
    for command in commands:
        for prefix in ([], ["sudo", "-n"]):
            if prefix and os.geteuid() == 0:
                continue
            result = subprocess.run(
                [*prefix, *command], capture_output=True, text=True, timeout=20, check=False,
            )
            if result.returncode == 0:
                return command[0], result.stdout
            last_error = result.stderr.strip() or result.stdout.strip()
    raise RuntimeError(last_error or "Wi-Fi command failed")


WIFI_SCAN_LIMIT = 10
wifi_scan_results = {}


def run_nmcli(args, timeout):
    if not shutil.which("nmcli"):
        raise RuntimeError("nmcli is not installed")
    last = None
    for prefix in ([], ["sudo", "-n"]):
        if prefix and os.geteuid() == 0:
            continue
        attempt = subprocess.run([*prefix, "nmcli", *args], capture_output=True, text=True, timeout=timeout, check=False)
        if attempt.returncode == 0:
            return attempt
        if last is None:
            last = attempt
    return last


def split_nmcli_fields(line):
    fields, current, escaped = [], "", False
    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(current)
            current = ""
        else:
            current += char
    fields.append(current)
    return fields


def scan_wifi_networks():
    result = run_nmcli(["-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes"], 30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "scan failed")
    best = {}
    for line in result.stdout.splitlines():
        fields = split_nmcli_fields(line)
        if len(fields) < 3 or not fields[0].strip():
            continue
        ssid = fields[0]
        try:
            signal_pct = int(fields[1])
        except ValueError:
            signal_pct = 0
        if ssid not in best or signal_pct > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": signal_pct, "secured": bool(fields[2].strip() and fields[2].strip() != "--")}
    return sorted(best.values(), key=lambda n: -n["signal"])[:WIFI_SCAN_LIMIT]


def saved_wifi_connections():
    result = run_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"], 15)
    if result is None or result.returncode != 0:
        return []
    names = []
    for line in result.stdout.splitlines():
        fields = split_nmcli_fields(line)
        if len(fields) >= 2 and fields[1] == "802-11-wireless" and fields[0] != WIFI_AP_CONNECTION:
            names.append(fields[0])
    return names


def connect_saved_wifi(name):
    result = run_nmcli(["-w", "30", "connection", "up", "id", name], 45)
    return result is not None and result.returncode == 0


def match_wifi_network(connection, networks):
    connection = connection.strip()
    matches = [
        (network, connection[len(network["ssid"]):].strip())
        for network in networks
        if connection == network["ssid"] or connection.startswith(network["ssid"] + " ")
    ]
    return max(matches, key=lambda match: len(match[0]["ssid"]), default=None)


def connect_wifi_network(ssid, password):
    command = ["-w", "20", "dev", "wifi", "connect", ssid]
    if password:
        command += ["password", password]
    result = run_nmcli(command, 40)
    return result.returncode == 0


WIFI_AP_SSID = "orangepizero"
WIFI_AP_PASSWORD = "orangepi"
WIFI_AP_CONNECTION = "madhat-hotspot"


def wifi_interface():
    result = run_nmcli(["-t", "-f", "DEVICE,TYPE", "dev"], 15)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not list network devices")
    for line in result.stdout.splitlines():
        fields = split_nmcli_fields(line)
        if len(fields) >= 2 and fields[1] == "wifi":
            return fields[0]
    raise RuntimeError("no Wi-Fi adapter found")


def nmcli_error(result, fallback):
    return (result.stderr.strip() or result.stdout.strip() or fallback)


def disconnect_wifi():
    interface = wifi_interface()
    result = run_nmcli(["dev", "disconnect", interface], 30)
    if result.returncode != 0:
        raise RuntimeError(nmcli_error(result, "disconnect failed"))


WIFI_AP_STATE_PATH = TIGHTVNC_PID_PATH.with_name("wifi-ap-paused.json")


def wifi_client_profiles():
    result = run_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"], 15)
    names = []
    for line in result.stdout.splitlines():
        fields = split_nmcli_fields(line)
        if len(fields) >= 2 and fields[1] == "802-11-wireless" and fields[0] != WIFI_AP_CONNECTION:
            names.append(fields[0])
    return names


def wifi_ap_running():
    result = run_nmcli(["-t", "-f", "NAME", "connection", "show", "--active"], 15)
    return WIFI_AP_CONNECTION in result.stdout.splitlines()


def wifi_ap_active(interface):
    result = run_nmcli(["-t", "-f", "GENERAL.CONNECTION,GENERAL.STATE", "dev", "show", interface], 15)
    return WIFI_AP_CONNECTION in result.stdout and "(connected)" in result.stdout


WIFI_AP_DNSMASQ_CONF = Path("/etc/NetworkManager/dnsmasq-shared.d/madhat-ap.conf")


def set_ap_dhcp_only(enabled):
    # Another service often owns port 53, which makes NetworkManager's dnsmasq fail; the AP only needs DHCP.
    try:
        if enabled:
            run_privileged("mkdir", "-p", str(WIFI_AP_DNSMASQ_CONF.parent))
            run_privileged("tee", str(WIFI_AP_DNSMASQ_CONF), input_text="port=0\n")
        else:
            run_privileged("rm", "-f", str(WIFI_AP_DNSMASQ_CONF))
    except (OSError, subprocess.SubprocessError):
        pass


def start_wifi_ap():
    interface = wifi_interface()
    run_nmcli(["radio", "wifi", "on"], 15)
    # Saved networks would otherwise auto-reconnect and take the adapter back from the AP.
    paused = [n for n in wifi_client_profiles() if run_nmcli(["-g", "connection.autoconnect", "connection", "show", n], 15).stdout.strip() != "no"]
    try:
        WIFI_AP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WIFI_AP_STATE_PATH.write_text(json.dumps(paused))
    except OSError:
        pass
    for name in paused:
        run_nmcli(["connection", "modify", name, "connection.autoconnect", "no"], 15)
    run_nmcli(["connection", "down", WIFI_AP_CONNECTION], 15)
    run_nmcli(["dev", "disconnect", interface], 30)
    run_nmcli(["connection", "delete", WIFI_AP_CONNECTION], 15)
    error = None
    ip_address = None
    set_ap_dhcp_only(True)
    if not (shutil.which("dnsmasq") or Path("/usr/sbin/dnsmasq").exists()):
        error = "dnsmasq is missing; run: sudo apt install dnsmasq-base"
    # The default shared subnet can clash with another connection, so retry on a different one.
    for address in ([] if error else [None, "192.168.77.1/24", "172.31.77.1/24"]):
        run_nmcli(["connection", "down", WIFI_AP_CONNECTION], 15)
        run_nmcli(["connection", "delete", WIFI_AP_CONNECTION], 15)
        add = [
            "connection", "add", "type", "wifi", "ifname", interface, "con-name", WIFI_AP_CONNECTION,
            "autoconnect", "no", "ssid", WIFI_AP_SSID,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv6.method", "ignore",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", WIFI_AP_PASSWORD,
        ]
        if address:
            add += ["ipv4.addresses", address]
        result = run_nmcli(add, 30)
        if result.returncode != 0:
            error = nmcli_error(result, "could not create the access point profile")
            break
        result = run_nmcli(["connection", "up", WIFI_AP_CONNECTION, "ifname", interface], 45)
        if result.returncode != 0:
            error = nmcli_error(result, "could not start the access point")
            continue
        time.sleep(4)
        if not wifi_ap_active(interface):
            error = "access point started but dropped (adapter may not support AP mode)"
            continue
        error = None
        info = run_nmcli(["-g", "IP4.ADDRESS", "dev", "show", interface], 15)
        ip_address = info.stdout.strip().split("\n")[0].split("/")[0] or None
        break
    if error:
        run_nmcli(["connection", "delete", WIFI_AP_CONNECTION], 15)
        set_ap_dhcp_only(False)
        restore_wifi_clients()
        raise RuntimeError(error)
    return ip_address or "10.42.0.1"


def restore_wifi_clients():
    try:
        names = json.loads(WIFI_AP_STATE_PATH.read_text())
        WIFI_AP_STATE_PATH.unlink()
    except (OSError, ValueError):
        return
    for name in names if isinstance(names, list) else []:
        run_nmcli(["connection", "modify", str(name), "connection.autoconnect", "yes"], 15)


def stop_wifi_ap():
    result = run_nmcli(["connection", "down", WIFI_AP_CONNECTION], 30)
    run_nmcli(["connection", "delete", WIFI_AP_CONNECTION], 15)
    set_ap_dhcp_only(False)
    restore_wifi_clients()
    if result.returncode != 0:
        raise RuntimeError(nmcli_error(result, "access point is not running"))


def local_ip_addresses():
    addresses = []
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5, check=False)
        addresses = [a for a in result.stdout.split() if "." in a]
    except (OSError, subprocess.SubprocessError):
        pass
    return addresses


def wifi_ap_instructions(ip_address="10.42.0.1"):
    user = getpass.getuser()
    return (
        f"Access point {WIFI_AP_SSID} started (password {WIFI_AP_PASSWORD}).\n"
        f"1. Join that Wi-Fi.\n"
        f"2. SSH: ssh {user}@{ip_address}\n"
        f"3. VNC: /tightvnc start, then https://{ip_address}:6080/vnc.html"
    )


SLEEP_STATE_PATH = TIGHTVNC_PID_PATH.with_name("sleep-state.json")
sleep_lock = threading.Lock()


def system_sleep_active():
    return SLEEP_STATE_PATH.exists()


def read_sleep_state():
    try:
        data = json.loads(SLEEP_STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_screen_power(on):
    for display in (os.environ.get("DISPLAY"), ":0"):
        if not display:
            continue
        env = {**os.environ, "DISPLAY": display}
        if shutil.which("xset"):
            args = ["xset", "dpms", "force", "on"] if on else ["xset", "dpms", "force", "off"]
            if subprocess.run(args, env=env, capture_output=True, timeout=10, check=False).returncode == 0:
                return True
    return False


def cpu_governor_paths():
    return sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor"))


def set_cpu_governor(governor):
    paths = [str(path) for path in cpu_governor_paths()]
    if not paths:
        return False
    result = run_privileged("tee", *paths, input_text=governor + "\n")
    return result.returncode == 0


def current_cpu_governor():
    paths = cpu_governor_paths()
    try:
        return paths[0].read_text().strip() if paths else None
    except OSError:
        return None


def wifi_radio_enabled():
    try:
        tool, output = run_wifi_command("status")
        return wifi_status_text(tool, output)
    except Exception:
        return None


def enter_system_sleep(ollama_was_running):
    """Low-power mode: the app keeps running so /syswakeup can still arrive over the mesh."""
    with sleep_lock:
        if system_sleep_active():
            return []
        state = {
            "ollama": bool(ollama_was_running),
            "governor": current_cpu_governor(),
            "wifi": wifi_radio_enabled(),
        }
        try:
            SLEEP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SLEEP_STATE_PATH.write_text(json.dumps(state))
        except OSError as error:
            raise RuntimeError(f"could not save sleep state: {error}")
        steps = []
        for name, action in (
            ("access point", lambda: wifi_ap_running() and stop_wifi_ap()),
            ("Wi-Fi", lambda: state["wifi"] is not False and run_wifi_command("off")),
            ("CPU governor", lambda: state["governor"] and set_cpu_governor("powersave")),
            ("screen", lambda: set_screen_power(False)),
        ):
            try:
                action()
            except Exception as error:
                steps.append(f"{name}: {error}")
        return steps


def exit_system_sleep():
    with sleep_lock:
        if not system_sleep_active():
            return None
        state = read_sleep_state()
        problems = []
        for name, action in (
            ("screen", lambda: set_screen_power(True)),
            ("CPU governor", lambda: state.get("governor") and set_cpu_governor(state["governor"])),
            ("Wi-Fi", lambda: state.get("wifi") is not False and run_wifi_command("on")),
        ):
            try:
                action()
            except Exception as error:
                problems.append(f"{name}: {error}")
        try:
            SLEEP_STATE_PATH.unlink()
        except OSError:
            pass
        return state, problems


def wifi_status_text(tool, output):
    if tool == "nmcli":
        return output.strip().lower() == "enabled"
    return "soft blocked: yes" not in output.lower() and "hard blocked: yes" not in output.lower()


def read_number(path):
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def cpu_temperature_f():
    temps = [read_number(p) for p in Path("/sys/class/thermal").glob("thermal_zone*/temp")]
    temps = [t / 1000 for t in temps if t and 0 < t / 1000 < 150]
    return round(max(temps) * 9 / 5 + 32) if temps else None


def system_power_summary():
    parts = [f"load {os.getloadavg()[0]:.2f}"]
    temps = [read_number(p) for p in Path("/sys/class/thermal").glob("thermal_zone*/temp")]
    temps = [t / 1000 for t in temps if t]
    if temps:
        parts.append(f"temp {max(temps) * 9 / 5 + 32:.0f}F")
    freq = read_number("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if freq:
        parts.append(f"cpu {freq / 1000:.0f}MHz")

    watts = []
    for supply in Path("/sys/class/power_supply").glob("*"):
        # tcpm-source-psy-* reports the negotiated USB-C contract, not measured draw.
        if supply.name.startswith("tcpm-source"):
            continue
        power = read_number(supply / "power_now")
        volts = read_number(supply / "voltage_now")
        amps = read_number(supply / "current_now")
        if power:
            watts.append(power / 1e6)
        elif volts and amps:
            watts.append(volts * amps / 1e12)
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        power = read_number(hwmon / "power1_input")
        millivolts = read_number(hwmon / "in0_input")
        milliamps = read_number(hwmon / "curr1_input")
        if power:
            watts.append(power / 1e6)
        elif millivolts and milliamps:
            watts.append(millivolts * milliamps / 1e6)
    parts.append(f"power {sum(watts):.1f}W" if watts else "no power sensor")
    return ", ".join(parts)


FASTFETCH_FIELDS = {
    "os": "OS", "host": "Host", "kernel": "Kernel", "uptime": "Up", "cpu": "CPU",
    "gpu": "GPU", "memory": "RAM", "swap": "Swap", "disk": "Disk",
    "local ip": "IP", "battery": "Bat",
}


def system_fastfetch_info():
    # --logo none drops the ASCII art, which can't be shown in the MeshCore app.
    result = subprocess.run(
        ["fastfetch", "--logo", "none", "--pipe"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.strip() or f"fastfetch exited with status {result.returncode}"
        raise RuntimeError(error)

    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout)
    items = []
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        value = re.sub(r"\s+", " ", value).strip()
        label = FASTFETCH_FIELDS.get(re.sub(r"\s*\(.*\)$", "", key.strip()).lower())
        if separator and label and value:
            items.append(f"{label}: {value}")
    if not items:
        raise RuntimeError("fastfetch returned no system information")
    return limit_ai_reply(" | ".join(items), MAX_AI_REPLY_PACKETS * 75)


async def handle_slash_command(sender_id, prompt, allow_settings):
    match = re.fullmatch(r"\s*/(help|clearmemory|settings|restart|update|syswifi|syssleep|syswakeup|reboot|sysreboot|sysshutdown|tightvnc|power|fastfetch)\b\s*(.*)", prompt, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    command, args = match.group(1).lower(), match.group(2).strip()
    if command == "help":
        return SETTINGS_HELP_TEXT if args.lower() == "settings" else HELP_TEXT
    if command == "clearmemory":
        conversation_history.pop(sender_id, None)
        return "Done. I've cleared our chat memory."
    if not allow_settings:
        return "Send /settings in a direct message to me."
    if command == "restart":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /restart from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        log_to_dash(f"Restart requested by {sender_id}")
        # Delay lets the reply go out before the process is replaced.
        schedule_restart(delay=10)
        return "Restarting the app now. Back in about a minute."
    if command == "power":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /power from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        return SingleMessage(await asyncio.to_thread(system_power_summary))
    if command == "fastfetch":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /fastfetch from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        try:
            info = await asyncio.to_thread(system_fastfetch_info)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            log_to_dash(f"/fastfetch failed: {error}")
            if isinstance(error, FileNotFoundError):
                return "Fastfetch is not installed on this computer."
            if isinstance(error, subprocess.TimeoutExpired):
                return "Fastfetch timed out while collecting system information."
            return f"Fastfetch failed: {error}"[:180]
        return info
    if command == "reboot":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /reboot from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        if not meshcore_instance or not app_state["is_connected"]:
            return "The device is not connected."
        log_to_dash(f"Node reboot requested by {sender_id}")

        async def reboot_node():
            # The reply is sent through the node, so wait for it to go out first.
            await asyncio.sleep(10)
            try:
                async with paced_hardware_lock():
                    await meshcore_instance.commands.reboot()
            except Exception as error:
                log_to_dash(f"Node reboot error: {error}")

        asyncio.create_task(reboot_node())
        return "Rebooting the node in a few seconds."
    if command == "sysreboot":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /sysreboot from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        log_to_dash(f"System reboot requested by {sender_id}")

        async def reboot_system():
            # Wait for the reply to be delivered before rebooting the host.
            await asyncio.sleep(10)
            try:
                result = await asyncio.to_thread(run_privileged, "systemctl", "reboot")
                if result.returncode != 0:
                    error = result.stderr.strip() or "systemctl reboot failed"
                    log_to_dash(f"/sysreboot failed: {error}")
            except Exception as error:
                log_to_dash(f"/sysreboot failed: {error}")

        asyncio.create_task(reboot_system())
        return "Rebooting the host in a few seconds."
    if command == "sysshutdown":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /sysshutdown from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        log_to_dash(f"System shutdown requested by {sender_id}")

        async def shutdown_system():
            # Wait for the reply to be delivered before powering off the host.
            await asyncio.sleep(10)
            try:
                result = await asyncio.to_thread(run_privileged, "systemctl", "poweroff")
                if result.returncode != 0:
                    error = result.stderr.strip() or "systemctl poweroff failed"
                    log_to_dash(f"/sysshutdown failed: {error}")
            except Exception as error:
                log_to_dash(f"/sysshutdown failed: {error}")

        asyncio.create_task(shutdown_system())
        return "Shutting down the host in a few seconds."
    if command == "tightvnc":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /tightvnc from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        action = args.lower() or "status"
        if action == "start":
            action = "on"
        if action not in {"status", "on", "off", "restart"}:
            return "Usage: /tightvnc status, /tightvnc start, /tightvnc off, or /tightvnc restart"
        try:
            status = await asyncio.to_thread(set_tightvnc, action)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            log_to_dash(f"/tightvnc {action} failed: {error}")
            return f"TightVNC {action} failed: {error}"[:180]
        log_to_dash(f"TightVNC {action} requested by {sender_id}")
        if action == "off":
            return "TightVNC and noVNC turned off."
        addresses = await asyncio.to_thread(local_ip_addresses)
        urls = "\n".join(f"https://{a}:6080/vnc.html" for a in addresses[:3])
        if action == "status":
            summary = (
                f"TightVNC is {'on' if status['vnc_running'] else 'off'}; "
                f"noVNC is {'on' if status['novnc_running'] else 'off'}."
            )
            return f"{summary}\n{urls}" if urls and status["novnc_running"] else summary
        verb = "restarted" if action == "restart" else "turned on"
        if not urls:
            return f"TightVNC and noVNC {verb}. Could not detect this device's IP; open https://<device-ip>:6080/vnc.html."
        return f"TightVNC and noVNC {verb}. Open in a browser (accept the certificate warning):\n{urls}"
    if command in {"syssleep", "syswakeup"}:
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /{command} from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        if command == "syssleep":
            if system_sleep_active():
                return "Already asleep. Send /syswakeup to wake up."
            log_to_dash(f"System sleep requested by {sender_id}")
            was_running = app_state["ollama_running"]

            async def go_to_sleep():
                # Let the reply go out before the network and screen are turned off.
                await asyncio.sleep(5)
                try:
                    if was_running:
                        await stop_ollama_server()
                    problems = await asyncio.to_thread(enter_system_sleep, was_running)
                    if problems:
                        log_to_dash("Sleep partly applied: " + "; ".join(problems)[:300])
                    else:
                        log_to_dash("System sleeping.")
                except Exception as error:
                    log_to_dash(f"/syssleep failed: {error}")

            asyncio.create_task(go_to_sleep())
            return "Going to sleep in a few seconds (screen, Wi-Fi, Ollama off, CPU low-power). Send /syswakeup to wake."
        if not system_sleep_active():
            return "Already awake."
        try:
            result = await asyncio.to_thread(exit_system_sleep)
        except Exception as error:
            log_to_dash(f"/syswakeup failed: {error}")
            return f"Wake up failed: {error}"[:120]
        state, problems = result if result else ({}, [])
        if state.get("ollama"):
            asyncio.create_task(update_available_models())
        log_to_dash(f"System woken by {sender_id}" + (f" ({'; '.join(problems)[:200]})" if problems else ""))
        return "Awake. Screen, Wi-Fi and CPU restored." + (" Some steps failed; see logs." if problems else "")
    if command == "syswifi":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /syswifi from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        parts = args.split(None, 1)
        action = parts[0].lower() if parts else ""
        if action == "scan":
            try:
                networks = await asyncio.to_thread(scan_wifi_networks)
            except Exception as error:
                log_to_dash(f"/syswifi scan failed: {error}")
                return f"Wi-Fi scan failed: {error}"[:120]
            wifi_scan_results[sender_id] = networks
            if not networks:
                return "No Wi-Fi access points found."
            lines = [f"{n['ssid'][:32]} ({n['signal']}%{'' if n['secured'] else ', open'})" for n in networks]
            return "Wi-Fi networks:\n" + "\n".join(lines) + "\nReply: /syswifi connect <SSID> [password] (no password needed for saved networks)"
        if action == "connect":
            if len(parts) >= 2:
                try:
                    saved = await asyncio.to_thread(saved_wifi_connections)
                except Exception:
                    saved = []
                request_text = parts[1].strip()
                saved_match = max(
                    (name for name in saved if request_text.lower() == name.lower()),
                    key=len, default=None,
                )
                if saved_match is not None:
                    log_to_dash(f"/syswifi connect to saved network {saved_match} requested by {sender_id}")
                    try:
                        connected = await asyncio.to_thread(connect_saved_wifi, saved_match)
                    except Exception as error:
                        log_to_dash(f"/syswifi saved connect failed: {error}")
                        return "Wi-Fi failed"
                    return "Wi-Fi connected" if connected else "Wi-Fi failed"
            networks = wifi_scan_results.get(sender_id)
            if not networks:
                return "Run /syswifi scan first."
            if len(parts) < 2:
                return "Usage: /syswifi connect <SSID> <password> (SSID from /syswifi scan)"
            match = match_wifi_network(parts[1], networks)
            if match is None:
                return "Use an SSID shown by /syswifi scan: /syswifi connect <SSID> <password>"
            network, password = match
            if network["secured"] and len(password) < 8:
                return "That network needs a password (8+ characters): /syswifi connect <SSID> <password>"
            log_to_dash(f"/syswifi connect to {network['ssid']} requested by {sender_id}")
            try:
                connected = await asyncio.to_thread(connect_wifi_network, network["ssid"], password)
            except Exception as error:
                log_to_dash(f"/syswifi connect failed: {error}")
                return "Wi-Fi failed"
            return "Wi-Fi connected" if connected else "Wi-Fi failed"
        if action == "disconnect":
            try:
                await asyncio.to_thread(disconnect_wifi)
            except Exception as error:
                log_to_dash(f"/syswifi disconnect failed: {error}")
                return f"Wi-Fi disconnect failed: {error}"[:120]
            log_to_dash(f"Computer Wi-Fi disconnected by {sender_id}")
            return "Wi-Fi disconnected."
        if action == "ap":
            stop = len(parts) > 1 and parts[1].lower() in {"off", "stop"}
            try:
                ap_ip = await asyncio.to_thread(stop_wifi_ap if stop else start_wifi_ap)
            except Exception as error:
                log_to_dash(f"/syswifi ap failed: {error}")
                return f"Wi-Fi access point failed: {error}"[:160]
            log_to_dash(f"Wi-Fi access point {'stopped' if stop else 'started'} by {sender_id}")
            if stop:
                return "Wi-Fi access point stopped."
            return wifi_ap_instructions(ap_ip or "10.42.0.1")
        if action not in {"status", "on", "off"}:
            return "Usage: /syswifi status, on, off, scan, connect <SSID> <password>, disconnect, or ap [off]"
        try:
            tool, output = await asyncio.to_thread(run_wifi_command, action)
            if action == "status":
                return f"Computer Wi-Fi is {'on' if wifi_status_text(tool, output) else 'off'}."
        except Exception as error:
            log_to_dash(f"/syswifi {action} failed: {error}")
            return f"Wi-Fi {action} failed: {error}"[:120]
        log_to_dash(f"Computer Wi-Fi turned {action} by {sender_id}")
        return f"Computer Wi-Fi turned {action}."
    if command == "update":
        if not is_settings_admin(sender_id):
            log_to_dash(f"Rejected /update from non-admin {sender_id}")
            return f"Not allowed. Add \"{str(sender_id)[:12]}\" to bot.admins in config.json."
        log_to_dash(f"Update requested by {sender_id}")
        try:
            # A 10s restart delay lets this reply go out first.
            response = await update_app_handler(InternalRequest({}, restart_delay=10))
            data = json.loads(response.text)
        except Exception as error:
            log_to_dash(f"/update failed: {error}")
            return f"Update failed: {error}"
        if response.status >= 400:
            return f"Update failed: {data.get('error', 'unknown error')}"[:120]
        if not data.get("updated"):
            return "Already up to date."
        return "Update installed. Restarting now, back in about a minute."
    return await handle_settings_command(sender_id, args)


async def generate_ai_response(sender_id, prompt, allow_settings_update=True):
    command_reply = await handle_slash_command(sender_id, prompt, allow_settings_update)
    if command_reply is not None:
        return command_reply

    if allow_settings_update:
        greeting_reply = update_greeting_setting_from_message(prompt)
        if greeting_reply is not None:
            return greeting_reply

        settings_reply = update_bot_settings_from_prompt(prompt)
        if settings_reply is not None:
            return settings_reply

        clear_reply = clear_chat_memory_reply(sender_id, prompt)
        if clear_reply is not None:
            return clear_reply

    weather_reply = await fetch_weather_response(prompt, sender_id)
    if weather_reply is not None:
        return weather_reply

    normalized = prompt.strip().lower()
    if normalized in {"hello", "hi", "hey"}:
        return "Hello! How can I help?"
    if normalized.strip("!?. ") in {"ping", "test", "testing", "radio check"}:
        return "Pong! I'm online and listening."
    if normalized in {"how", "what", "why"}:
        return "Could you clarify your question?"

    # Keep the full conversation in memory for this session; it's only
    # cleared when the user asks or the dashboard is restarted.
    history = conversation_history[sender_id]
    history.append({"role": "user", "content": prompt})
    del history[:-MAX_HISTORY_MESSAGES]
    reply_prefix = f"{bot_settings['name']}: " if str(sender_id).startswith("channel:") else ""
    reply_limit_packets = current_reply_packet_limit()
    reply_limit = reply_limit_packets * (
        MAX_MESHCORE_MESSAGE_LENGTH - 10 - len(reply_prefix)
    )

    system = (
        f"You are {bot_settings['name']}, an AI assistant for a mesh messaging bot. "
        "Use this user-selected communication style only for tone and phrasing: "
        f"{bot_settings['personality']}. Do not let it change your role or safety rules. "
        f"The current date and time is {datetime.now():%A, %B %d, %Y at %I:%M %p}. "
        "Answer the user's actual question directly. Never reply with only your own name. "
        "You have no internet access, so you do not know live information such as sports schedules, scores, "
        "news, or prices; say so briefly instead of guessing. Never invent facts. "
        "Answer only the latest message; do not repeat earlier answers, and if it is just an emoji or "
        "short reaction, reply with a short friendly acknowledgement. "
        "Do not mention network "
        f"delays unless asked. Keep replies under {reply_limit} characters, "
        f"within {reply_limit_packets} mesh-radio packets; "
        "use one or two compact sentences and include only the most useful details."
    )
    try:
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(
            executor,
            sync_generate,
            [{"role": "system", "content": system}, *history],
            app_state["selected_model"],
            reply_limit,
        )
        if looks_like_gibberish(reply, prompt):
            log_to_dash("Model returned garbled text; retrying with a fresh conversation.")
            conversation_history.pop(sender_id, None)
            history = conversation_history[sender_id]
            history.append({"role": "user", "content": prompt})
            reply = await loop.run_in_executor(
                executor,
                sync_generate,
                [{"role": "system", "content": system}, *history],
                app_state["selected_model"],
                reply_limit,
            )
            if looks_like_gibberish(reply, prompt):
                history.pop()
                return "Sorry, I couldn't generate a clear answer. Please try again."
        reply = limit_ai_reply(reply.strip(), reply_limit)
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as error:
        log_to_dash(f"Ollama error using {app_state['selected_model']}: {error}")
        if history and history[-1]["role"] == "user":
            history.pop()
        return "I could not process that message."


async def send_to_target(target, target_type, message, timestamp=None):
    if target_type == "node":
        return await meshcore_instance.commands.send_msg(target, message)

    try:
        channel_index = int(target)
    except ValueError as error:
        raise ValueError("Channel target must be a numeric channel index") from error

    for name in ("send_chan_msg", "send_channel_msg", "send_channel_message"):
        method = getattr(meshcore_instance.commands, name, None)
        if method:
            if timestamp is not None and name == "send_chan_msg":
                return await method(channel_index, message, timestamp)
            return await method(channel_index, message)
    raise RuntimeError("This MeshCore version has no channel-send method")


def echo_hop_key(log_data):
    path = str(log_data.get("path") or "")
    size = log_data.get("path_hash_size") or 1
    if log_data.get("path_len", 0) > 0 and path:
        return path[-size * 2:]
    return "direct"


def repeater_name_for_hash(hash_hex):
    if hash_hex == "direct":
        return "Direct"
    for contact_id, entry in app_state["contacts"].items():
        contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
        public_key = str(contact.get("public_key", "")) if isinstance(contact, dict) else ""
        if public_key.lower().startswith(hash_hex.lower()):
            return display_name(contact_id, contact)
    return ""


def path_nodes(path_hex, path_len, path_hash_size=None):
    if not isinstance(path_hex, str) or not isinstance(path_len, int) or path_len <= 0:
        return []
    size = path_hash_size or max(1, len(path_hex) // 2 // path_len)
    step = size * 2
    hashes = [path_hex[i:i + step] for i in range(0, path_len * step, step)]
    return [{"hash": h, "name": repeater_name_for_hash(h)} for h in hashes if h]


async def send_to_target_with_pending_confirmation(target, target_type, message):
    early_ack_codes = set()
    echoes = []
    subscriptions = []
    subscribe = getattr(meshcore_instance, "subscribe", None)
    ack_type = getattr(EventType, "ACK", None)
    if subscribe and ack_type is not None:
        subscriptions.append(
            subscribe(ack_type, lambda event: early_ack_codes.add(event.attributes.get("code")))
        )
    # Repeater re-broadcasts show up in the RX log; channel text (5) or the DM ACK (3).
    echo_payload_type = 5 if target_type == "channel" else 3
    if subscribe and hasattr(EventType, "RX_LOG_DATA"):
        subscriptions.append(
            subscribe(
                EventType.RX_LOG_DATA,
                lambda event: echoes.append(event.payload)
                if isinstance(event.payload, dict)
                and event.payload.get("payload_type") == echo_payload_type
                else None,
            )
        )
    send_timestamp = int(time.time()) if target_type == "channel" else None

    def cleanup():
        for subscription in subscriptions:
            subscription.unsubscribe()

    try:
        result = await send_to_target(target, target_type, message, send_timestamp)
    except Exception:
        cleanup()
        raise

    if result.type == EventType.ERROR:
        cleanup()
        return result, None, None

    ack_code = None
    if isinstance(result.payload, dict):
        expected_ack = result.payload.get("expected_ack")
        if expected_ack is not None:
            ack_code = expected_ack.hex() if hasattr(expected_ack, "hex") else str(expected_ack)

    def matching_echoes():
        if target_type == "channel":
            return [
                log for log in echoes
                if log.get("sender_timestamp") == send_timestamp
                and str(log.get("message", "")).endswith(message)
            ]
        if ack_code is None:
            return []
        return [
            log for log in echoes
            if isinstance(log.get("pkt_payload"), (bytes, bytearray))
            and bytes(log["pkt_payload"][:4]).hex() == ack_code
        ]

    async def wait_for_confirmation(chat_message):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        delivered = False
        try:
            while loop.time() < deadline:
                matched = matching_echoes()
                acked = target_type == "node" and ack_code in early_ack_codes
                if matched or acked:
                    if not delivered:
                        delivered = True
                        # Keep listening briefly so late repeaters are counted too.
                        deadline = loop.time() + 8.0
                    repeaters = {}
                    for log in matched:
                        repeaters.setdefault(echo_hop_key(log), log)
                    heard = max(len(repeaters), 1)
                    if chat_message.get("heard") != heard or chat_message.get("status") != "delivered":
                        chat_message["heard"] = heard
                        chat_message["heard_repeaters"] = [
                            {"hash": key, "name": repeater_name_for_hash(key)}
                            for key in repeaters
                        ]
                        update_chat_message_status(chat_message, "delivered")
                await asyncio.sleep(0.5)
            if not delivered:
                update_chat_message_status(chat_message, "failed")
        finally:
            cleanup()

    return result, wait_for_confirmation, None


def schedule_delivery_status_update(message, pending_confirmation, _unused=None):
    if pending_confirmation is None:
        update_chat_message_status(message, "failed")
        return

    async def run():
        try:
            await pending_confirmation(message)
        except Exception as error:
            log_to_dash(f"Delivery confirmation unavailable: {error}")
            update_chat_message_status(message, "failed")

    return asyncio.create_task(run())


async def handle_incoming_message(event):
    if not meshcore_instance or not app_state["is_connected"]:
        log_to_dash("Ignored incoming DM because the device is not connected.")
        return

    packet = event.payload or {}
    sender = packet.get("sender") or packet.get("pubkey_prefix")
    text = packet.get("text", "").strip()
    if not sender or not text:
        return

    # Replies to remote-management commands go to the waiting request, never to the AI bot.
    if packet.get("txt_type") == 1:
        for prefix, queue in list(cli_reply_waiters.items()):
            if str(sender).lower().startswith(prefix) or prefix.startswith(str(sender).lower()):
                queue.put_nowait(text)
        return

    message_id = packet.get("id") or f"{sender}:{packet.get('sender_timestamp', '')}:{text}"
    if message_id in processed_messages:
        return
    processed_messages.add(message_id)
    resolved_sender = resolve_contact_id(sender)
    info = reception_info(packet)
    if "path_nodes" not in info:
        # DM frames carry no repeater list, so fall back to the route stored for this contact.
        entry = app_state["contacts"].get(resolved_sender)
        contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
        if isinstance(contact, dict) and isinstance(contact.get("out_path_len"), int):
            nodes = path_nodes(contact.get("out_path"), contact["out_path_len"])
            if nodes:
                info["path_nodes"] = nodes
    add_chat_message("node", resolved_sender, "incoming", text, info)
    record_trace_event("direct", "inbound", resolved_sender)
    log_to_dash(f"Received DM from {sender}: {text}")

    reply = await generate_ai_response(sender, text)
    log_to_dash(f"AI reply: {reply}")
    reply_parts = split_reply_into_messages(reply, max_parts=current_reply_packet_limit())

    async with paced_hardware_lock():
        try:
            contacts = await meshcore_instance.commands.get_contacts()
            recipient = sender
            if contacts.type != EventType.ERROR and contacts.payload:
                recipient = contacts.payload.get(sender, sender)
            for part_number, part in enumerate(reply_parts, start=1):
                if part_number > 1:
                    await asyncio.sleep(HARDWARE_SEND_INTERVAL)
                result, pending_delivery, hops = await send_to_target_with_pending_confirmation(
                    recipient, "node", part
                )
                if result.type == EventType.ERROR:
                    log_to_dash(
                        f"Hardware rejected reply part {part_number}/"
                        f"{len(reply_parts)}: {result.payload}"
                    )
                    return
                sent_message = add_chat_message("node", resolved_sender, "outgoing", part)
                update_chat_message_status(sent_message, "sent")
                schedule_delivery_status_update(sent_message, pending_delivery, hops)
                record_trace_event("direct", "outbound", resolved_sender)
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
    if parse_channel_sender(text).casefold() in blocked_senders:
        return
    add_chat_message("channel", channel_target, "incoming", text, reception_info(packet))
    record_trace_event("channel", "inbound", channel_target)
    log_to_dash(f"Received channel {channel_target} message: {text}")

    # Some MeshCore clients prefix channel text with the sender's name
    # (e.g. "[Alice] /bot ..." or "Alice: /bot ...") before it reaches us,
    # so look for /bot anywhere after a word boundary rather than only at
    # the very start of the message.
    reply = update_greeting_setting_from_message(message_text, channel_target)
    if reply is None:
        bot_command = re.search(r"(?:^|\s)/bot(?:\s+|$)", message_text, re.IGNORECASE)
        if bot_command is None:
            return
        prompt = message_text[bot_command.end():].strip()
        if not prompt:
            return

        reply = await generate_ai_response(
            f"channel:{channel_target}",
            prompt,
            allow_settings_update=False,
        )
    # Weather answers are addressed to the asker so others in the channel can tell who it's for.
    sender_match = re.match(r"^\s*(?:\[([^\]]{1,40})\]|([^:\r\n\[]{1,40}):)\s", text)
    sender_name = (sender_match.group(1) or sender_match.group(2)).strip() if sender_match else ""
    if isinstance(reply, SingleMessage) and sender_name:
        reply = SingleMessage(f"@[{sender_name}] {reply}")
    log_to_dash(f"AI channel reply: {reply}")
    reply_parts = split_reply_into_messages(
        reply,
        prefix=f"{bot_settings['name']}: ",
        max_parts=current_reply_packet_limit(),
    )

    async with paced_hardware_lock():
        try:
            for part_number, part in enumerate(reply_parts, start=1):
                if part_number > 1:
                    await asyncio.sleep(HARDWARE_SEND_INTERVAL)
                result, pending_delivery, hops = await send_to_target_with_pending_confirmation(
                    channel_target, "channel", part
                )
                if result.type == EventType.ERROR:
                    log_to_dash(
                        f"Hardware rejected channel reply part {part_number}/"
                        f"{len(reply_parts)}: {result.payload}"
                    )
                    return
                sent_message = add_chat_message(
                    "channel", channel_target, "outgoing", part
                )
                update_chat_message_status(sent_message, "sent")
                schedule_delivery_status_update(sent_message, pending_delivery, hops)
                record_trace_event("channel", "outbound", channel_target)
            log_to_dash(
                f"Channel reply sent in {len(reply_parts)} message(s)."
            )
        except Exception as error:
            log_to_dash(f"Channel message send error: {error}")


async def handle_advertisement(event):
    payload = event.payload if isinstance(event.payload, dict) else {}
    public_key = payload.get("public_key")
    if not public_key:
        return
    advert_seen_at[str(public_key)] = int(time.time())


async def handle_new_contact(event):
    contact = event.payload or {}
    if contact.get("public_key"):
        advert_seen_at[str(contact["public_key"])] = int(time.time())
    if not meshcore_instance or not app_state["is_connected"]:
        return
    if not app_config["bot"]["greet_new_users"]:
        return

    public_key = contact.get("public_key")
    if not public_key:
        return
    advert_timestamp = contact.get("last_advert")
    if announced_contact_adverts.get(str(public_key)) == advert_timestamp:
        return

    channel = app_config["bot"].get("greet_channel", "")
    if not channel:
        return

    hops = contact.get("out_path_len")
    if isinstance(hops, int) and hops >= 0:
        hop_text = f"{hops} hop{'s' if hops != 1 else ''}"
        greeting = f"Welcome {display_name(public_key, contact)}; detected {hop_text} away."
    else:
        greeting = f"Welcome {display_name(public_key, contact)}; detected via flood route."

    prefix = f"{bot_settings['name']}: "
    greeting_parts = split_reply_into_messages(greeting, prefix=prefix)
    announced_contact_adverts[str(public_key)] = advert_timestamp
    async with paced_hardware_lock():
        for part_number, part in enumerate(greeting_parts, start=1):
            if part_number > 1:
                await asyncio.sleep(HARDWARE_SEND_INTERVAL)
            try:
                result, pending_delivery, hops = await send_to_target_with_pending_confirmation(
                    channel, "channel", part
                )
                if result.type == EventType.ERROR:
                    log_to_dash(f"Channel {channel} peer greeting rejected: {result.payload}")
                    return
                sent_message = add_chat_message("channel", channel, "outgoing", part)
                update_chat_message_status(sent_message, "sent")
                schedule_delivery_status_update(sent_message, pending_delivery, hops)
            except Exception as error:
                log_to_dash(f"Channel {channel} peer greeting failed: {error}")
                return
    log_to_dash(f"Announced new peer {display_name(public_key, contact)} in channel {channel}.")


async def disconnect_hardware():
    async with connection_lock:
        await _disconnect_hardware_locked()


async def _disconnect_hardware_locked():
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
        # Share connection_lock so a scan can't collide with an in-flight
        # connect/disconnect and trip BlueZ's "Operation already in progress".
        async with connection_lock:
            devices = await BleakScanner.discover(timeout=5.0, return_adv=True)
    except Exception as error:
        log_to_dash(f"Bluetooth scan failed: {error}")
        return web.json_response({"error": str(error)}, status=503)

    meshcore_devices = [
        device
        for device, adv in devices.values()
        if (adv.local_name or device.name or "").startswith("MeshCore")
    ]

    return web.json_response({
        "devices": [
            {
                "name": device.name or "Unnamed MeshCore device",
                "address": device.address,
            }
            for device in sorted(
                meshcore_devices,
                key=lambda device: (device.name or device.address).casefold(),
            )
        ]
    })


async def logs_handler(request):
    return web.json_response({"logs": read_log_file()})


async def clear_logs_handler(request):
    for path in (LOG_FILE_PATH, LOG_FILE_PATH.with_name("app.log.1")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            return web.json_response({"error": str(error)}, status=500)
    app_state["logs"] = []
    return web.json_response({"ok": True})


def run_systemctl_user(*args):
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


SYSTEM_UNIT_PATH = Path("/etc/systemd/system") / AUTOSTART_UNIT_NAME


def run_privileged(*args, input_text=None):
    """Runs a command as root, using passwordless sudo when not already root."""
    command = list(args) if os.geteuid() == 0 else ["sudo", "-n", *args]
    return subprocess.run(
        command, input=input_text, capture_output=True, text=True, timeout=30, check=False,
    )


def tightvnc_status():
    try:
        with socket.create_connection(("127.0.0.1", 5901), timeout=0.5):
            vnc_running = True
    except OSError:
        vnc_running = False
    return {
        "vnc_running": vnc_running,
        "novnc_running": _websockify_pid() is not None,
        "addresses": local_ip_addresses(),
    }


def _websockify_pid():
    try:
        pid = int(TIGHTVNC_PID_PATH.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
            process_state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split(") ", 1)[1][0]
        except OSError:
            command_line = b""
            process_state = ""
        if (
            process_state != "Z"
            and b"websockify" in command_line
            and b"6080" in command_line
            and b"5901" in command_line
        ):
            return pid
    try:
        TIGHTVNC_PID_PATH.unlink()
    except FileNotFoundError:
        pass
    return None


def _run_tightvnc_command(command, operation):
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"{operation} failed"
        raise RuntimeError(detail)
    return result


def _ensure_novnc_certificate():
    if not TIGHTVNC_CERT_PATH.is_file():
        result = subprocess.run(
            [
                "openssl", "req", "-x509", "-nodes", "-days", "365",
                "-newkey", "rsa:2048", "-keyout", str(TIGHTVNC_CERT_PATH),
                "-out", str(TIGHTVNC_CERT_PATH), "-subj", f"/CN={socket.gethostname()}",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or "Could not create the noVNC TLS certificate"
            raise RuntimeError(detail)
    TIGHTVNC_CERT_PATH.chmod(0o600)


def _tightvnc_start():
    if shutil.which("tightvncserver") is None:
        raise RuntimeError("TightVNC is not installed. Run setup.sh to install it.")
    if shutil.which("websockify") is None or not NOVNC_WEB_PATH.is_dir():
        raise RuntimeError("noVNC/websockify is not installed. Run setup.sh to install it.")
    if not (Path.home() / ".vnc" / "passwd").is_file():
        raise RuntimeError("Set a VNC password first by running: vncpasswd")
    _ensure_novnc_certificate()

    if not tightvnc_status()["vnc_running"]:
        _run_tightvnc_command(
            ["tightvncserver", ":1", "-localhost"],
            "Starting TightVNC",
        )
        for _ in range(20):
            if tightvnc_status()["vnc_running"]:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("TightVNC did not start listening on localhost:5901")

    if _websockify_pid() is None:
        TIGHTVNC_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TIGHTVNC_LOG_PATH.open("ab") as log_file:
            process = subprocess.Popen(
                [
                    "websockify", f"--web={NOVNC_WEB_PATH}/",
                    f"--cert={TIGHTVNC_CERT_PATH}", "6080", "localhost:5901",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        TIGHTVNC_PID_PATH.write_text(f"{process.pid}\n", encoding="ascii")
        for _ in range(20):
            if _websockify_pid() is not None:
                try:
                    with socket.create_connection(("127.0.0.1", 6080), timeout=0.2):
                        break
                except OSError:
                    pass
            if process.poll() is not None:
                try:
                    TIGHTVNC_PID_PATH.unlink()
                except FileNotFoundError:
                    pass
                log_tail = TIGHTVNC_LOG_PATH.read_text(encoding="utf-8", errors="replace")[-500:].strip()
                raise RuntimeError(log_tail or "noVNC/websockify exited during startup")
            time.sleep(0.25)
        else:
            pid = _websockify_pid()
            if pid is not None:
                os.kill(pid, signal.SIGTERM)
            try:
                TIGHTVNC_PID_PATH.unlink()
            except FileNotFoundError:
                pass
            raise RuntimeError("noVNC/websockify did not start on port 6080")
    return tightvnc_status()


def _tightvnc_stop():
    failures = []
    pid = _websockify_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if _websockify_pid() is None:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("noVNC/websockify did not stop after SIGTERM")
        except (OSError, RuntimeError) as error:
            failures.append(str(error))
    if tightvnc_status()["vnc_running"]:
        try:
            _run_tightvnc_command(
                ["tightvncserver", "-kill", ":1"],
                "Stopping TightVNC",
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            failures.append(str(error))
    if failures:
        raise RuntimeError("; ".join(failures))
    return tightvnc_status()


_tightvnc_lock = threading.Lock()


def set_tightvnc(action):
    if action not in {"status", "on", "off", "restart"}:
        raise ValueError("action must be status, on, off, or restart")
    with _tightvnc_lock:
        if action == "status":
            return tightvnc_status()
        if action == "off":
            return _tightvnc_stop()
        if action == "restart":
            _tightvnc_stop()
        return _tightvnc_start()


def _ssh_terminal_pid():
    try:
        pid = int(SSH_TERMINAL_PID_PATH.read_text(encoding="ascii").strip())
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split(") ", 1)[1][0]
        if state != "Z" and b"ttyd" in command_line:
            return pid
    except (OSError, ValueError, IndexError):
        pass
    SSH_TERMINAL_PID_PATH.unlink(missing_ok=True)
    return None


def ssh_terminal_status():
    return {
        "running": _ssh_terminal_pid() is not None,
        "installed": shutil.which("ttyd") is not None and shutil.which("ssh") is not None,
        "addresses": local_ip_addresses(),
        "user": getpass.getuser(),
    }


def _ssh_terminal_start():
    if shutil.which("ttyd") is None or shutil.which("ssh") is None:
        raise RuntimeError("ttyd or an SSH client is not installed. Run setup.sh to install them.")
    if _ssh_terminal_pid() is not None:
        return ssh_terminal_status()
    _ensure_novnc_certificate()
    help_text = subprocess.run(["ttyd", "--help"], capture_output=True, text=True, check=False)
    command = ["ttyd", "-p", str(SSH_TERMINAL_PORT), "--ssl",
               "--ssl-cert", str(TIGHTVNC_CERT_PATH), "--ssl-key", str(TIGHTVNC_CERT_PATH)]
    if "--writable" in help_text.stdout + help_text.stderr:
        command.append("--writable")
    # The session is an ordinary SSH login to this machine, so the Pi's own credentials gate access.
    command += ["ssh", "-o", "StrictHostKeyChecking=accept-new", f"{getpass.getuser()}@localhost"]
    SSH_TERMINAL_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SSH_TERMINAL_LOG_PATH.open("ab") as log_file:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log_file,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    SSH_TERMINAL_PID_PATH.write_text(f"{process.pid}\n", encoding="ascii")
    for _ in range(20):
        if process.poll() is not None:
            SSH_TERMINAL_PID_PATH.unlink(missing_ok=True)
            tail = SSH_TERMINAL_LOG_PATH.read_text(encoding="utf-8", errors="replace")[-500:].strip()
            raise RuntimeError(tail or "ttyd exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", SSH_TERMINAL_PORT), timeout=0.2):
                return ssh_terminal_status()
        except OSError:
            time.sleep(0.25)
    process.terminate()
    SSH_TERMINAL_PID_PATH.unlink(missing_ok=True)
    raise RuntimeError(f"ttyd did not start on port {SSH_TERMINAL_PORT}")


def _ssh_terminal_stop():
    pid = _ssh_terminal_pid()
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if _ssh_terminal_pid() is None:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("ttyd did not stop after SIGTERM")
    return ssh_terminal_status()


def set_ssh_terminal(action):
    if action not in {"on", "off", "restart"}:
        raise ValueError("action must be on, off, or restart")
    with _tightvnc_lock:
        if action == "off":
            return _ssh_terminal_stop()
        if action == "restart":
            _ssh_terminal_stop()
        return _ssh_terminal_start()


def can_use_system_service():
    if not Path("/run/systemd/system").is_dir():
        return False
    try:
        return os.geteuid() == 0 or run_privileged("true").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def autostart_enabled():
    try:
        for command in (["systemctl"], ["systemctl", "--user"]):
            result = subprocess.run(
                [*command, "is-enabled", AUTOSTART_UNIT_NAME],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if result.stdout.strip() == "enabled":
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def set_autostart(enabled):
    """Enable/disable launching the dashboard at boot (system service, else user service)."""
    if shutil.which("systemctl") is None:
        raise RuntimeError("systemd is not available on this system")
    app_path = Path(__file__).resolve()
    venv_python = app_path.parent / ".venv" / "bin" / "python"
    python_path = venv_python if venv_python.exists() else Path(sys.executable)
    user = getpass.getuser()

    if can_use_system_service():
        unit = None
        if enabled:
            unit = (
                "[Unit]\n"
                "Description=MeshCore AI Bot Dashboard\n"
                "After=network-online.target ollama.service\n"
                "Wants=network-online.target\n\n"
                "[Service]\n"
                f"User={user}\n"
                "SupplementaryGroups=dialout\n"
                f"WorkingDirectory={app_path.parent}\n"
                "Environment=MESHC_OPS_RESTARTING=1\n"
                f"ExecStart={python_path} {app_path}\n"
                "Restart=always\n"
                "RestartSec=5\n\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            steps = [
                ("tee", str(SYSTEM_UNIT_PATH)),
                ("systemctl", "daemon-reload"),
                ("systemctl", "enable", AUTOSTART_UNIT_NAME),
            ]
        else:
            steps = [("systemctl", "disable", AUTOSTART_UNIT_NAME)]
        for step in steps:
            result = run_privileged(*step, input_text=unit if step[0] == "tee" else None)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"{step[0]} failed")
        if not enabled:
            run_systemctl_user("disable", AUTOSTART_UNIT_NAME)
        return

    if enabled:
        AUTOSTART_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_UNIT_PATH.write_text(
            "[Unit]\n"
            "Description=MeshCore AI Bot Dashboard\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            f"WorkingDirectory={app_path.parent}\n"
            f"ExecStart={python_path} {app_path}\n"
            "Restart=always\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n",
            encoding="utf-8",
        )
        steps = [("daemon-reload",), ("enable", AUTOSTART_UNIT_NAME)]
    else:
        steps = [("disable", AUTOSTART_UNIT_NAME)]
    for step in steps:
        result = run_systemctl_user(*step)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"systemctl {step[0]} failed")
    if enabled:
        # Linger lets the user service start at boot without a login session.
        subprocess.run(
            ["loginctl", "enable-linger", getpass.getuser()],
            capture_output=True,
            check=False,
        )


async def autostart_handler(request):
    return web.json_response({"enabled": await asyncio.to_thread(autostart_enabled)})


async def update_autostart_handler(request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be true or false"}, status=400)
    try:
        await asyncio.to_thread(set_autostart, enabled)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        log_to_dash(f"Autostart change failed: {error}")
        return web.json_response({"error": str(error)}, status=500)
    log_to_dash(f"Start at boot {'enabled' if enabled else 'disabled'}")
    return web.json_response({"enabled": enabled})


async def tightvnc_handler(request):
    status = await asyncio.to_thread(tightvnc_status)
    return web.json_response(status)


async def update_tightvnc_handler(request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in {"on", "off", "restart"}:
        return web.json_response(
            {"error": "action must be on, off, or restart"},
            status=400,
        )
    try:
        status = await asyncio.to_thread(set_tightvnc, action)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        log_to_dash(f"TightVNC {action} failed: {error}")
        return web.json_response({"error": str(error)}, status=500)
    log_to_dash(f"TightVNC {action} completed")
    return web.json_response(status)


async def ssh_terminal_handler(request):
    return web.json_response(await asyncio.to_thread(ssh_terminal_status))


async def update_ssh_terminal_handler(request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in {"on", "off", "restart"}:
        return web.json_response({"error": "action must be on, off, or restart"}, status=400)
    try:
        status = await asyncio.to_thread(set_ssh_terminal, action)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        log_to_dash(f"SSH terminal {action} failed: {error}")
        return web.json_response({"error": str(error)}, status=500)
    log_to_dash(f"SSH terminal {action} completed")
    return web.json_response(status)


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


HARDWARE_CONNECT_ATTEMPTS = 3
HARDWARE_CONNECT_RETRY_DELAY_SECONDS = 3


async def connect_hardware():
    global meshcore_instance
    async with connection_lock:
        await _disconnect_hardware_locked()
        try:
            # A device that just rebooted may accept the transport connection but
            # not yet answer the firmware app-start handshake in time, so retry a
            # few times before giving up.
            for attempt in range(1, HARDWARE_CONNECT_ATTEMPTS + 1):
                if app_state["connection_type"] == "bluetooth":
                    log_to_dash(f"Connecting via Bluetooth to {app_state['ble_mac']}...")
                    meshcore_instance = await MeshCore.create_ble(app_state["ble_mac"])
                else:
                    log_to_dash(f"Connecting via serial to {app_state['serial_port']}...")
                    meshcore_instance = await MeshCore.create_serial(app_state["serial_port"])

                if meshcore_instance is not None:
                    break

                if attempt < HARDWARE_CONNECT_ATTEMPTS:
                    log_to_dash(
                        f"No response from device (attempt {attempt}/{HARDWARE_CONNECT_ATTEMPTS}); "
                        f"retrying in {HARDWARE_CONNECT_RETRY_DELAY_SECONDS}s..."
                    )
                    await asyncio.sleep(HARDWARE_CONNECT_RETRY_DELAY_SECONDS)

            if meshcore_instance is None:
                raise RuntimeError(
                    "MeshCore did not return a connection instance; check device address/port, "
                    "or wait for the device to finish booting and try connecting again."
                )

            # The library's auto-fetch loop calls commands.get_msg() on its own
            # background task, bypassing hardware_lock entirely; wrap it so
            # those calls queue behind (and pace with) our other commands
            # instead of racing them and confusing the firmware.
            unlocked_get_msg = meshcore_instance.commands.get_msg

            async def locked_get_msg(*args, _unlocked=unlocked_get_msg, **kwargs):
                async with paced_hardware_lock():
                    return await _unlocked(*args, **kwargs)

            meshcore_instance.commands.get_msg = locked_get_msg

            await meshcore_instance.start_auto_message_fetching()
            meshcore_instance.set_decrypt_channel_logs(True)
            meshcore_instance.subscribe(EventType.CONTACT_MSG_RECV, handle_incoming_message)
            meshcore_instance.subscribe(
                EventType.CHANNEL_MSG_RECV,
                handle_incoming_channel_message,
            )
            app_state["is_connected"] = True
            await refresh_device_limits()
            await refresh_contacts()
            await refresh_channels()
            meshcore_instance.subscribe(EventType.NEW_CONTACT, handle_new_contact)
            meshcore_instance.subscribe(EventType.ADVERTISEMENT, handle_advertisement)
            log_to_dash("Hardware interface successfully linked and active.")
        except Exception as error:
            meshcore_instance = None
            app_state["is_connected"] = False
            log_to_dash(f"Hardware connection error: {error}")


async def telemetry_loop():
    while True:
        try:
            if app_state["is_connected"] and meshcore_instance:
                # Each refresh_* call locks its own hardware commands, so
                # message sends can interleave instead of waiting out an
                # entire multi-second scan.
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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1117">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<script>
(function(){
const root=document.documentElement,ua=navigator.userAgent||'',uaData=navigator.userAgentData||null,platform=(uaData&&uaData.platform)||navigator.platform||'';
const touchMac=/Mac/.test(platform)&&navigator.maxTouchPoints>1;
let os='other';
if(/Android/i.test(ua))os='android';else if(/iPhone|iPad|iPod/i.test(ua)||touchMac)os='ios';else if(/Win/i.test(platform))os='windows';else if(/Mac/i.test(platform))os='macos';else if(/CrOS/i.test(ua))os='chromeos';else if(/Linux/i.test(platform+ua))os='linux';
let browser='other';
if(/Edg\//.test(ua))browser='edge';else if(/OPR\/|Opera/.test(ua))browser='opera';else if(/SamsungBrowser/.test(ua))browser='samsung';else if(/Firefox|FxiOS/.test(ua))browser='firefox';else if(/Chrome|CriOS/.test(ua))browser='chrome';else if(/Safari/.test(ua))browser='safari';
function classify(){
  const coarse=window.matchMedia&&matchMedia('(pointer:coarse)').matches,short=Math.min(screen.width,screen.height),width=window.innerWidth;
  const phoneUa=/Mobi|iPhone|iPod|Android.*Mobile/i.test(ua);
  const tabletUa=/iPad|Android(?!.*Mobile)|Tablet/i.test(ua)||touchMac;
  let device='desktop';
  if(phoneUa||(coarse&&short<600))device='mobile';else if(tabletUa||(coarse&&short<1100))device='tablet';
  if(device==='desktop'&&width<720)device='mobile';
  return device;
}
function apply(){
  const device=classify();
  root.dataset.device=device;root.dataset.os=os;root.dataset.browser=browser;
  root.dataset.orientation=window.innerHeight>window.innerWidth?'portrait':'landscape';
  window.isMobileDevice=()=>root.dataset.device==='mobile';
  window.isCompactDevice=()=>root.dataset.device!=='desktop';
}
apply();
window.addEventListener('resize',()=>{const before=root.dataset.device;apply();if(before!==root.dataset.device&&typeof showView==='function'&&typeof activeView!=='undefined')showView(activeView==='tron-overview'?'nodes':activeView)});
window.addEventListener('orientationchange',apply);
window.mobileDeviceInfo=()=>({device:root.dataset.device,os,browser});
})();
</script>
<link rel="stylesheet" href="/assets/leaflet/leaflet.css">
<script defer src="/assets/leaflet/leaflet.js"></script>
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
body[data-theme="tron"]{color-scheme:dark;--page-bg:#040b12;--panel-bg:#071521;--panel-raised:#0c2030;--text:#d4f4fa;--muted:#78a7bb;--accent:#00d8ff;--accent-dim:#082d40;--border:#14506b;--input-bg:#050f19;--input-border:#176486;--button-text:#031018;--log-bg:#030a11;--danger:#ff6d63}
.dashboard-header,.grid{width:min(100%,1800px);margin-right:auto;margin-left:auto}
.dashboard-header{min-height:58px;margin-bottom:12px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;gap:18px;background:var(--panel-bg);border:1px solid var(--border);border-radius:8px}
.brand-lockup{display:flex;align-items:center;gap:10px;min-width:max-content}
.brand-mark{width:34px;height:34px;display:grid;place-items:center;border:1px solid color-mix(in srgb,var(--accent) 38%,var(--border));border-radius:7px;background:var(--accent-dim);color:var(--accent);font:700 12px/1 ui-monospace,monospace}
.brand-copy h1{margin:0;color:var(--text);font-size:16px;font-weight:600;line-height:1.2}
.brand-copy h1 span{color:var(--accent);font-weight:500}
.header-label,.eyebrow{display:block;margin-bottom:3px;color:var(--muted);font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase}
.header-meta{display:flex;align-items:center;gap:16px;min-width:0}
.header-meta-item{display:flex;flex-direction:column;justify-content:center;gap:3px;min-width:0;padding:0;border:0;background:transparent}
.battery-readings{display:flex;align-items:baseline;gap:12px}.battery-reading{display:flex;align-items:baseline;gap:4px}
.header-meta-item.weather-widget{min-width:170px}
.weather-current{display:flex;align-items:center;gap:8px;white-space:nowrap}
.weather-icon{flex:none;width:40px;height:40px;display:grid;place-items:center;color:var(--accent)}
.weather-icon svg{width:36px;height:36px;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;stroke-width:1.7}
.weather-current strong{color:var(--text);font-size:14px;font-weight:700;font-variant-numeric:tabular-nums}
.weather-current span,.weather-location{color:var(--muted);font-size:10px}
.weather-current .weather-icon{color:var(--accent)}
.weather-extra{margin-left:2px}.weather-place{max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.weather-location{display:block;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
.nav-count{min-width:18px;padding:1px 5px;border-radius:10px;background:var(--panel-raised);font:10px ui-monospace,monospace;text-align:center}.nav-count[hidden]{display:none}
.view-panel[hidden]{display:none!important}
.page-view{width:min(100%,1800px);flex:1;margin:0 auto;padding-bottom:10px;display:flex;flex-direction:column;min-height:0}
.page-view>*:last-child{flex:1;min-height:0}
body:has(#nodes-view:not([hidden])){height:100vh;overflow:hidden}
.connection-layout{width:min(100%,520px);display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:stretch}
.settings-layout{width:min(100%,760px);display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:stretch}
.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.settings-tabs{display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid var(--border)}
.settings-tab{min-height:32px;padding:6px 10px;border-color:transparent;border-bottom:2px solid transparent;border-radius:0;background:transparent;color:var(--muted)}
.settings-tab[aria-pressed="true"]{border-bottom-color:var(--accent);color:var(--accent)}
.settings-tab-panel[hidden]{display:none}
.bot-terminal{grid-column:1/-1;width:100%;margin-top:16px;border:1px solid var(--border);border-radius:6px;background:#050a0d;color:#c8e6d0;font:12px/1.5 ui-monospace,"Cascadia Code",monospace;overflow:hidden}
.bot-terminal-bar{display:flex;align-items:center;gap:12px;padding:6px 10px;border-bottom:1px solid var(--border);color:#7fa;font-size:10px;font-weight:700;letter-spacing:.08em}
.bot-terminal-note{flex:1;color:var(--muted);font-weight:400;letter-spacing:0}
.bot-terminal-bar button{min-height:22px;padding:2px 8px;font-size:10px}
.bot-terminal-output{height:280px;overflow-y:auto;padding:10px;white-space:pre-wrap;word-break:break-word}
.bot-terminal-output .you{color:#8cf}.bot-terminal-output .bot{color:#c8e6d0;margin-bottom:8px}.bot-terminal-output .err{color:#f88}.bot-terminal-output .sys{color:#789}
.bot-terminal-input{display:flex;align-items:center;gap:8px;padding:8px 10px;border-top:1px solid var(--border)}
.bot-terminal-input span{color:#7fa}.bot-terminal-input input{flex:1;min-width:0;margin:0;background:transparent;border:0;color:inherit;font:inherit;outline:none}
.weather-settings-layout{max-width:560px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:end}
.weather-settings-layout label{margin:0}
.weather-settings-layout .settings-actions{grid-column:1/-1;display:flex;align-items:center;gap:10px}
.config-json-editor{min-height:360px;resize:vertical;font:12px/1.5 ui-monospace,"SFMono-Regular",monospace;tab-size:2}
.config-actions{display:flex;align-items:center;gap:8px;margin-top:10px}
.config-actions button:first-child{border-color:var(--accent);background:var(--accent);color:var(--button-text)}
.config-actions button:disabled{opacity:.55;cursor:not-allowed}
.config-status,.preferences-status{min-height:18px;margin:0;color:var(--muted);font-size:11px}
.config-status[data-state="error"],.preferences-status[data-state="error"]{color:var(--danger)}
.config-status[data-state="success"],.preferences-status[data-state="success"]{color:var(--accent)}
.settings-item{min-width:0;padding:12px;border:1px solid var(--border);border-radius:6px;background:var(--panel-raised)}
.settings-item select{margin-top:4px}
.theme-control-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:8px;margin-top:4px}.theme-control-row select{margin:0}.theme-control-row[hidden]{display:none!important}
.settings-item-full{grid-column:1/-1}
.model-manager-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.model-manager-header label{margin:0}
.refresh-btn{min-height:28px;padding:3px 10px;font-size:10px}
.installed-models-list{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.model-card{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 12px;border:1px solid var(--border);border-radius:6px;background:var(--panel-bg);flex-wrap:wrap}
.model-card-info{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.model-card-name{font-weight:600;font-family:ui-monospace,monospace;color:var(--text);font-size:12px}
.model-card-size{color:var(--muted);font-size:11px;font-family:ui-monospace,monospace}
.model-card-badge{display:inline-block;padding:2px 7px;border-radius:10px;background:var(--accent-dim);color:var(--accent);font-size:10px;font-weight:700;text-transform:uppercase;border:1px solid var(--accent)}
.model-card-actions{display:flex;gap:6px;align-items:center}
.model-card-actions button{min-height:28px;padding:4px 10px;font-size:11px}
.ollama-download-progress{margin-top:12px}
.ollama-download-progress[hidden]{display:none}
.ollama-download-progress progress{display:block;width:100%;height:12px;margin:6px 0}
.ollama-download-progress p{margin:0;color:var(--muted);font-size:11px}
.model-card-actions .delete-btn{color:var(--danger)!important;border-color:color-mix(in srgb,var(--danger) 45%,var(--border))!important}
.model-card-actions .delete-btn:hover{background:color-mix(in srgb,var(--danger) 15%,transparent)}
.download-control{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:6px}
.download-control button{min-width:90px;border-color:var(--accent);background:var(--accent);color:var(--button-text)}
.download-control button:hover{background:color-mix(in srgb,var(--accent) 85%,white)}
.model-preset-row{display:flex;align-items:center;gap:6px;margin-top:8px;flex-wrap:wrap;font-size:11px;color:var(--muted)}
.preset-btn{min-height:26px;padding:2px 8px;font-size:10px;border-radius:12px;background:var(--panel-bg);color:var(--muted);border:1px solid var(--border)}
.preset-btn:hover{color:var(--text);border-color:var(--accent)}
.settings-description{margin:8px 0 0;color:var(--muted);font-size:11px}
.card{min-width:0;margin-bottom:12px;padding:12px;background:var(--panel-bg);border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12)}
.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.panel-heading h2{margin:0;color:var(--text);font-size:14px;font-weight:600;line-height:1.25}
.panel-index{color:var(--muted);font:11px ui-monospace,monospace}
.nav-count,.panel-index,.incoming-adverts-count{display:none!important}
.map-rail .panel-index{display:inline!important}
label{display:block;margin:9px 0 5px;color:var(--muted);font-size:11px;font-weight:600}
input,select,textarea,button{font:inherit}
input,select,textarea{width:100%;min-width:0;margin:0;padding:9px 10px;border:1px solid var(--input-border);border-radius:5px;background:var(--input-bg);color:var(--text);outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 18%,transparent)}
button{min-height:36px;padding:8px 12px;border:1px solid var(--border);border-radius:5px;background:var(--panel-raised);color:var(--text);font-size:11px;font-weight:650;cursor:pointer;transition:background .15s,border-color .15s,transform .15s}
button:hover{transform:translateY(-1px);border-color:var(--accent);background:var(--accent-dim)}
.connection-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
.connection-actions button:first-child{border-color:var(--accent);background:var(--accent);color:var(--button-text)}
.connection-actions button:first-child:hover{background:color-mix(in srgb,var(--accent) 85%,white)}
.scan-control{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}
.scan-control button{min-width:78px}
.scan-status{min-height:18px;margin:5px 0 0;color:var(--muted);font-size:10px}
.scan-status[data-state="error"]{color:var(--danger)}
.chat-panel{min-height:360px;display:flex;flex-direction:column}
.messages-layout{display:grid;grid-template-columns:minmax(210px,280px) minmax(0,1fr);align-items:stretch;gap:12px}
.conversation-rail{min-height:360px;margin:0;display:flex;flex-direction:column}
#nodes-view .messages-layout,#channels-view .messages-layout{height:100%;min-height:0;overflow:hidden}
#nodes-view .conversation-rail,#nodes-view .chat-panel,#channels-view .conversation-rail,#channels-view .chat-panel{min-height:0;overflow:hidden}
.conversation-target-list{display:grid;grid-auto-rows:max-content;align-content:start;gap:6px;min-height:0;overflow:auto}
.conversation-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:8px}
.conversation-filters label{min-width:0;margin:0;font-size:10px}
.conversation-filters select{margin-top:4px;padding:7px 20px 7px 7px;font-size:10px}
.conversation-filters .node-search-label{grid-column:1/-1}
.conversation-filters .node-search-label input{margin-top:4px;padding:7px;font-size:10px;width:100%;box-sizing:border-box;background:var(--input-bg,var(--panel-bg));color:inherit;border:1px solid var(--border);border-radius:4px}
.search-input-row{position:relative;display:flex;align-items:center;gap:4px}.conversation-filters .search-input-row input,.map-peer-filters .search-input-row input{flex:1;min-width:0;width:auto;margin-top:0;padding-right:7px}.search-filter-menu{flex:none;position:static}.search-filter-menu>summary{display:grid;width:34px;height:34px;min-height:34px;place-items:center;border:1px solid var(--input-border,var(--border));border-radius:4px;background:var(--input-bg,var(--panel-bg));color:var(--muted);cursor:pointer;list-style:none}.search-filter-menu>summary::-webkit-details-marker{display:none}.search-filter-menu>summary:hover,.search-filter-menu[open]>summary{border-color:var(--accent);color:var(--accent)}.search-filter-menu>summary svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;stroke-width:1.8}.search-filter-panel{position:absolute;top:calc(100% + 5px);right:0;z-index:30;display:grid;width:min(220px,calc(100vw - 32px));box-sizing:border-box;gap:9px;padding:10px;border:1px solid var(--border);border-radius:4px;background:var(--panel-raised);box-shadow:0 8px 20px rgba(0,0,0,.28)}.search-filter-panel label{display:grid;gap:4px;min-width:0;margin:0;color:var(--muted);font-size:10px}.search-filter-panel select{width:100%;margin:0;padding:7px;font-size:10px}
.conversation-entry{display:grid;grid-template-columns:minmax(0,1fr) 34px;gap:4px;overflow:hidden;border:1px solid var(--border);border-radius:6px;background:var(--log-bg)}
.conversation-entry .conversation-target{border:0;border-radius:0;background:transparent}
.conversation-entry .conversation-target:hover{background:var(--panel-raised)}
.conversation-target{display:grid;width:100%;gap:3px;padding:9px;text-align:left}
.conversation-target strong{overflow-wrap:anywhere;color:var(--text);font-size:11px}
.conversation-target small{overflow-wrap:anywhere;color:var(--muted);font-size:10px}
.conversation-target.active{border-color:var(--accent);background:var(--accent-dim)}
.favorite-toggle{width:34px;min-height:36px;padding:4px;color:var(--muted);font-size:17px}
.favorite-toggle[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-dim);color:var(--accent)}
.chat-header{display:grid;gap:2px;min-height:38px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.chat-header-main{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.chat-header-copy{display:grid;min-width:0;gap:2px}
.chat-actions{position:relative;flex:none}
.chat-actions summary{list-style:none;cursor:pointer;padding:6px 9px;border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px}
.chat-actions summary::-webkit-details-marker{display:none}
.chat-action-menu{position:absolute;z-index:5;top:calc(100% + 4px);right:0;display:grid;min-width:145px;padding:4px;border:1px solid var(--border);border-radius:4px;background:var(--panel-bg);box-shadow:0 8px 20px #0004}
.chat-action-menu button{padding:7px;text-align:left;font-size:10px}
.chat-header strong{color:var(--text);font-size:13px}
.chat-header span{color:var(--muted);font-size:10px;overflow-wrap:anywhere}
.peer-inline-detail{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 10px;margin:4px 0 0;padding:0}
.rail-peer-detail{grid-template-columns:repeat(auto-fit,minmax(110px,1fr));margin:0;padding:0 9px 9px}
.peer-inline-detail[hidden]{display:none}
.peer-inline-detail div{min-width:0}
.peer-inline-detail dt{color:var(--muted);font-size:9px}
.peer-inline-detail dd{margin:1px 0 0;color:var(--text);font-size:10px;overflow-wrap:anywhere}
#node-chat-history,#channel-chat-history{flex:1;min-height:220px;overflow-y:auto;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--log-bg);white-space:pre-wrap;overflow-wrap:anywhere}
#nodes-view #node-chat-history,#channels-view #channel-chat-history{min-height:0}
#node-chat-history:empty::before,#channel-chat-history:empty::before{display:block;padding:8px;color:var(--muted);font-size:11px;content:"No messages in this view yet"}
.chat-message{max-width:92%;width:fit-content;margin:6px 0;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--panel-raised);color:var(--text);text-align:left;white-space:pre-wrap;overflow-wrap:anywhere}
.chat-message.incoming{margin-right:auto}
.chat-message.outgoing{margin-left:auto;border-color:color-mix(in srgb,var(--accent) 36%,var(--border));background:var(--accent-dim);color:var(--text)}
.chat-message-meta{display:flex;align-items:center;gap:6px;margin-bottom:4px;font-size:10px}
.chat-message.outgoing .chat-message-meta{flex-direction:row-reverse}
.chat-avatar{flex:none;width:18px;height:18px;display:grid;place-items:center;border-radius:50%;color:#fff;font:700 9px ui-monospace,monospace;text-transform:uppercase}
.chat-sender{color:var(--text);font-weight:700;overflow-wrap:anywhere}
.chat-time{color:var(--muted);font:9px ui-monospace,monospace}
.chat-status{color:var(--muted);font:9px ui-monospace,monospace;text-transform:uppercase}
.chat-message-body{white-space:pre-wrap;overflow-wrap:anywhere}
.conversation-target{display:grid;grid-template-columns:22px minmax(0,1fr);align-items:start;gap:3px 8px}
.conversation-target strong,.conversation-target small{grid-column:2}
.conversation-avatar{grid-row:1/3;width:22px;height:22px;display:grid;place-items:center;border-radius:50%;color:#fff;font:700 10px ui-monospace,monospace;text-transform:uppercase}
.chat-panel form{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:10px}
.chat-panel form input{min-width:0}
.chat-panel form button{border-color:var(--accent);background:var(--accent);color:var(--button-text);white-space:nowrap}
.chat-panel form button:disabled{opacity:.55;cursor:not-allowed}
.device-settings-layout{width:min(100%,1700px);column-width:340px;column-gap:12px}
.device-settings-layout form{display:contents}
.device-settings-layout label{margin:0}
.device-settings-layout label span{display:block;margin-bottom:5px}
.device-settings-layout .settings-description{grid-column:1/-1;margin:0}
.device-settings-layout .settings-actions{grid-column:1/-1;display:flex;align-items:center;gap:10px;break-inside:avoid;margin-bottom:12px}
.custom-radio-fields{grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.custom-radio-fields[hidden],#custom-power-field[hidden]{display:none}
.settings-status{min-height:18px;margin:0;color:var(--muted);font-size:11px}
.settings-status[data-state="error"]{color:var(--danger)}
.settings-status[data-state="success"]{color:var(--accent)}
.console-dock{position:fixed;top:88px;right:16px;bottom:16px;z-index:900;width:320px;min-width:240px;min-height:160px;max-width:calc(100vw - 60px);margin:0;padding:0;background:none;resize:both;overflow:hidden;transition:opacity .2s ease,transform .2s ease}
.console-dock.collapsed{opacity:0;pointer-events:none;transform:scale(.96)}
.console-dock.dragging{opacity:.85}
.console-toggle{position:static;order:2;flex:none;align-self:center;padding:6px 10px;border:1px solid var(--border);border-radius:4px;background:var(--panel-bg);color:var(--text);text-transform:uppercase;letter-spacing:.05em;font:10px ui-monospace,"SFMono-Regular",monospace;cursor:pointer}
.console-card{height:100%;margin:0;padding:10px;display:flex;flex-direction:column}
.console-card .panel-heading{cursor:move;user-select:none}
.console-card .panel-heading{margin-bottom:6px}
.console-card pre{flex:1;min-height:0;max-height:none;margin:0;padding:8px;overflow:auto;border:1px solid var(--border);border-radius:6px;background:var(--log-bg);color:var(--muted);font:11px/1.45 ui-monospace,"SFMono-Regular",monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.analyzer-shell{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:10px;min-height:calc(100vh - 150px)}
.analyzer-main,.analyzer-side{border:1px solid var(--border);background:var(--panel-bg)}
.analyzer-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-bottom:1px solid var(--border);background:linear-gradient(90deg,var(--panel-raised),var(--panel-bg))}
.analyzer-toolbar h2{margin:0;font-size:14px;letter-spacing:.02em}.analyzer-toolbar p{margin:3px 0 0;color:var(--muted);font-size:10px}
.analyzer-table-wrap{overflow:auto}.analyzer-table{width:100%;border-collapse:collapse;font:11px/1.4 ui-monospace,monospace}.analyzer-table th{position:sticky;top:0;padding:8px 10px;border-bottom:1px solid var(--border);background:var(--panel-raised);color:var(--muted);font-size:9px;font-weight:700;letter-spacing:.08em;text-align:left;text-transform:uppercase}.analyzer-table td{padding:9px 10px;border-bottom:1px solid color-mix(in srgb,var(--border) 72%,transparent);color:var(--text);white-space:nowrap}.analyzer-table tr:hover td{background:var(--accent-dim)}.packet-direction{color:var(--accent);font-weight:700}.packet-channel{color:#f5b94c}.packet-empty{padding:34px 16px;color:var(--muted);text-align:center}
.analyzer-side{display:flex;flex-direction:column}.analyzer-side-section{padding:12px;border-bottom:1px solid var(--border)}.analyzer-side h3{margin:0 0 10px;color:var(--text);font-size:11px;letter-spacing:.08em;text-transform:uppercase}.analyzer-stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}.analyzer-stat{padding:10px;background:var(--panel-raised)}.analyzer-stat strong{display:block;color:var(--accent);font:18px ui-monospace,monospace}.analyzer-stat span{color:var(--muted);font-size:9px;text-transform:uppercase}.analyzer-detail{color:var(--muted);font:10px/1.6 ui-monospace,monospace;white-space:pre-line;overflow-wrap:anywhere}.analyzer-detail strong{color:var(--text)}
@media(max-width:900px){.analyzer-shell{grid-template-columns:minmax(0,1fr)}.analyzer-table{min-width:650px}}
.map-workspace{width:min(100%,1800px);margin:0 auto}
.map-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:12px;padding:12px 14px}
.map-toolbar h2{margin:0;color:var(--text);font-size:15px;font-weight:600}
.map-layout{display:grid;grid-template-columns:minmax(230px,300px) minmax(0,1fr);align-items:stretch;gap:12px}
.map-layout{grid-template-columns:minmax(0,1fr)!important}.map-search-overlay{position:absolute;top:10px;left:10px;right:56px;z-index:1000;display:block;max-width:340px;margin:0}.map-search-overlay .search-input-row{background:var(--panel-bg,#fff);border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.35)}.map-search-overlay .search-filter-panel{z-index:1001}.map-search-overlay .node-search-label>label{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}.map-search-overlay .node-search-label input{margin-top:0!important}
.map-rail{min-height:0;margin:0;display:flex;flex-direction:column}
.map-workspace .map-layout{grid-template-columns:minmax(230px,300px) minmax(0,1fr)!important}
@media(max-width:720px){.map-workspace .map-layout{grid-template-columns:minmax(0,1fr)!important}}
.map-rail-summary{margin:0 0 12px;color:var(--muted);font-size:11px}
.map-peer-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:8px}
.map-peer-filters label{min-width:0;margin:0;font-size:10px}
.map-peer-filters select{margin-top:4px;padding:7px 20px 7px 7px;font-size:10px}
.map-peer-filters .node-search-label{grid-column:1/-1}
.map-peer-filters .node-search-label input{margin-top:4px;padding:7px;font-size:10px;width:100%;box-sizing:border-box;background:var(--input-bg,var(--panel-bg));color:inherit;border:1px solid var(--border);border-radius:4px}
#map-node-list{flex:1;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--log-bg)}
.map-empty{padding:12px;color:var(--muted);font-size:11px}
.map-node-row{display:flex;flex-direction:column;gap:4px;padding:10px;border-bottom:1px solid var(--border)}
.map-node-row:last-child{border-bottom:0}
.map-node-row-main{display:flex;align-items:center;justify-content:space-between;gap:10px}
.map-peer-target{flex:1;min-width:0;padding:4px;border:0;background:transparent;text-align:left}
.map-peer-target:hover{transform:none;border-color:transparent;background:var(--panel-raised)}
.map-node-row .favorite-toggle{flex:none}
.map-node-row[role="button"]{cursor:pointer}
.map-node-row[role="button"]:hover,.map-node-row[role="button"]:focus{outline:0;background:var(--panel-raised)}
.map-node-row strong,.map-node-row div>span{display:block;overflow-wrap:anywhere}
.map-node-row strong{color:var(--text);font-size:11px}
.map-node-row div>span{margin-top:2px;color:var(--muted);font:10px ui-monospace,monospace}
.map-location{flex:none;font:9px ui-monospace,monospace;letter-spacing:.35px}
.map-location.located{color:var(--accent)}
.map-location.unlocated{color:var(--muted)}
.map-peer-icon{display:grid;width:32px;height:32px;place-items:center;border:2px solid #fff;border-radius:50%;box-shadow:0 2px 7px rgba(0,0,0,.4)}
.map-peer-icon svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;stroke-width:2}
.map-peer-icon-users{background:#e0f2fe;color:#0369a1}
.map-peer-icon-repeaters{background:#fef3c7;color:#a16207}
.map-peer-icon-room-servers{background:#f3e8ff;color:#7e22ce}
.map-peer-icon-sensors{background:#ffe4e6;color:#be123c}
.map-peer-icon-unknown{background:#e2e8f0;color:#334155}
.map-surface{position:relative;min-width:0;min-height:0;margin:0;padding:0;overflow:hidden}
#map-canvas{width:100%;height:100%;min-height:max(260px,calc(100vh - 230px));background:#d9e2df}
.map-tiles-dark{filter:invert(1) hue-rotate(180deg) brightness(.85) contrast(.9) saturate(.6)}
.map-message{position:absolute;z-index:500;top:14px;left:50%;max-width:calc(100% - 28px);padding:8px 12px;transform:translateX(-50%);border:1px solid var(--border);border-radius:5px;background:var(--panel-bg);color:var(--muted);font-size:11px;text-align:center;box-shadow:0 4px 14px rgba(0,0,0,.2)}
.map-message[hidden]{display:none}
.leaflet-container{font:12px/1.4 "Segoe UI",system-ui,sans-serif}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:var(--panel-bg);color:var(--text)}
.leaflet-popup-content{margin:10px 12px}
.leaflet-control-attribution{font-size:9px!important}
.live-trace-workspace{position:relative;width:min(100%,1800px);height:min(760px,calc(100vh - 170px));min-height:520px;margin:0 auto;border:1px solid var(--border);border-radius:8px;overflow:hidden}
#live-trace-canvas{width:100%;height:100%;background:#0b1016}
.live-trace-overlay{position:absolute;z-index:500;display:flex;flex-direction:column;max-height:calc(100% - 24px);overflow:hidden;border:1px solid var(--border);border-radius:7px;background:color-mix(in srgb,var(--panel-bg) 90%,transparent);backdrop-filter:blur(6px);box-shadow:0 8px 24px rgba(0,0,0,.35)}
.live-trace-feed{top:12px;left:12px;width:280px}
.live-trace-legend{top:12px;right:12px;width:190px}
.live-trace-overlay .panel-heading{margin:0;padding:9px 10px;border-bottom:1px solid var(--border)}
.live-trace-overlay .panel-heading h2{font-size:12px}
.live-trace-feed-list{overflow-y:auto;padding:6px;display:flex;flex-direction:column;gap:4px}
.live-trace-feed-item{padding:6px 8px;border:1px solid var(--border);border-radius:5px;background:var(--panel-raised);color:var(--text);font-size:10.5px;line-height:1.4;overflow-wrap:anywhere}
.live-trace-feed-item time{display:block;margin-bottom:2px;color:var(--muted);font:9px ui-monospace,monospace}
.live-trace-legend-list{padding:8px 10px;display:flex;flex-direction:column;gap:6px}
.live-trace-legend-item{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:10px}
.live-trace-legend-dot{flex:none;width:10px;height:10px;border-radius:50%}
.trace-pulse-marker{animation:trace-pulse-ring 1.1s ease-out 2}
@keyframes trace-pulse-ring{0%{filter:drop-shadow(0 0 0 var(--accent))}50%{filter:drop-shadow(0 0 8px var(--accent))}100%{filter:drop-shadow(0 0 0 transparent)}}
.trace-pulse-dot{filter:drop-shadow(0 0 6px var(--accent))}
.analyzer-radio-status{display:grid;gap:10px;margin-top:12px}.analyzer-radio-group h4{margin:0 0 5px;color:var(--accent);font-size:9px;font-weight:700;text-transform:uppercase}.analyzer-radio-list{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 8px;margin:0}.analyzer-radio-list dt{color:var(--muted);font-size:10px}.analyzer-radio-list dd{margin:0;color:var(--text);font:10px ui-monospace,monospace;text-align:right}.analyzer-radio-updated{margin:0;color:var(--muted);font-size:9px}
.header-metric,.header-status,.weather-current{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:1050px){.header-meta{gap:10px}}
@media(max-width:1050px){.dashboard-header{flex-wrap:wrap}.top-nav{order:3;flex-basis:100%}.map-layout{grid-template-columns:minmax(210px,260px) minmax(0,1fr)}}
@media(max-width:720px){body{padding:8px}.dashboard-header{align-items:flex-start;flex-direction:column;gap:12px}.top-nav{order:0;max-width:100%;overflow-x:auto}.nav-tab{flex:none}.header-meta{width:100%;flex-wrap:wrap;justify-content:space-between}.settings-grid,.device-settings-layout form{grid-template-columns:minmax(0,1fr)}.messages-layout{grid-template-columns:minmax(0,1fr)}.conversation-rail{min-height:0;max-height:190px}.device-settings-layout .settings-description,.device-settings-layout .settings-actions{grid-column:1}.map-layout{grid-template-columns:minmax(0,1fr)}.map-rail{min-height:180px;max-height:230px}.map-surface{min-height:48vh}#map-canvas{height:50vh;min-height:320px}.map-toolbar{align-items:flex-start;flex-direction:column}.console-dock{top:auto!important;right:8px;bottom:8px;left:8px!important;width:auto!important;max-width:none;height:38vh!important}.console-toggle{right:0}.messages-layout{grid-template-rows:auto minmax(0,1fr)}.chat-panel{min-height:340px}.page-view{padding-bottom:8px}}
@media(max-width:720px){#nodes-view .messages-layout,#channels-view .messages-layout{grid-template-rows:minmax(0,min(24vh,190px)) minmax(0,1fr)}}
body[data-theme="midnight"],body[data-theme="ocean"]{--page-bg:#091117;--panel-bg:#0f1b22;--panel-raised:#14252d;--text:#d7e4e8;--muted:#78919a;--accent:#42d9c3;--accent-dim:#103c3d;--border:#23404a;--input-bg:#0a151b;--input-border:#315562;--log-bg:#071016}
body{padding:0;background-image:linear-gradient(rgba(66,217,195,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(66,217,195,.018) 1px,transparent 1px);background-size:24px 24px;font-family:ui-monospace,"SFMono-Regular",monospace}
.dashboard-header{width:100%;max-width:none;margin:0 0 10px;padding:10px 16px;border-width:0 0 1px;border-radius:0;background:#0b151b;box-shadow:0 4px 20px rgba(0,0,0,.22)}
.brand-mark{border-radius:2px}.brand-copy h1{font-family:ui-monospace,"SFMono-Regular",monospace;font-size:14px;letter-spacing:.08em}.top-nav{gap:0}.nav-tab{min-height:36px;border-width:0 0 2px;border-radius:0;text-transform:uppercase;font:10px ui-monospace,"SFMono-Regular",monospace;letter-spacing:.05em}.nav-tab:hover,.nav-tab[aria-pressed="true"]{border-color:var(--accent);background:rgba(66,217,195,.08);color:var(--accent);transform:none}.nav-count{border-radius:2px}.page-view{width:min(100% - 24px,1800px)}.card,.map-workspace,.live-trace-workspace,.analyzer-main,.analyzer-side{border-radius:2px}.panel-heading{border-bottom:1px solid var(--border)}.chat-panel,.conversation-rail,.map-rail,.map-surface,.map-toolbar{box-shadow:0 10px 30px rgba(0,0,0,.13)}
.dashboard-logo{width:120px;height:120px;flex:none;object-fit:contain}.brand-copy h1{font-size:20px;letter-spacing:.14em}.device-settings-layout form{gap:0}.device-settings-layout form>label,.device-settings-layout form>.custom-radio-fields{padding:12px 14px;border-bottom:1px solid var(--border)}.device-settings-layout form>.settings-section-label{padding:14px;color:var(--accent);background:var(--panel-raised);font:10px ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase}.device-settings-layout form>.settings-description{margin:0;padding:12px 14px;border-bottom:1px solid var(--border)}
.reference-device-settings{width:min(100%,1700px);column-width:340px;column-gap:12px;overflow-x:auto;margin-bottom:16px}.device-settings-card{padding:0;overflow:hidden;break-inside:avoid}.device-settings-card>.panel-heading{margin:0;padding:13px 16px}.device-settings-card>.panel-heading h2{font-size:14px}.device-setting-row{width:100%;min-height:56px;padding:11px 16px;display:flex;align-items:center;justify-content:space-between;gap:12px;border:0;border-bottom:1px solid var(--border);border-radius:0;background:transparent;color:var(--text);text-align:left}.device-setting-row:hover{background:var(--panel-raised);transform:none}.device-setting-row strong,.device-toggle-row strong{display:block;font-size:12px}.device-setting-row small,.device-toggle-row small{display:block;margin-top:3px;color:var(--muted);font-size:10px}.device-info-grid{margin:0;padding:8px 16px 14px;display:grid;grid-template-columns:minmax(100px,.35fr) minmax(0,1fr);gap:6px 12px;border-top:1px solid var(--border);font-size:10px}.device-info-grid[hidden]{display:none}.device-info-grid dt{color:var(--muted)}.device-info-grid dd{margin:0;overflow-wrap:anywhere;color:var(--text);font-family:ui-monospace,monospace}.device-settings-grid{padding:12px 16px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 14px}.device-settings-grid>label{margin:0;min-width:0}.device-settings-grid>label>span{display:block;margin-bottom:5px;color:var(--muted);font-size:10px}.device-settings-grid .device-toggle-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0}.device-toggle-row input{width:18px;min-width:18px;height:18px;margin:0;accent-color:var(--accent)}.device-action-grid{padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px}.device-action-grid button{flex:1 1 145px}.danger-action{color:var(--danger)!important;border-color:color-mix(in srgb,var(--danger) 45%,var(--border))!important}.device-debug-output{max-height:300px;margin:0 16px 12px;padding:10px;overflow:auto;border:1px solid var(--border);background:var(--log-bg);color:var(--muted);font:10px/1.5 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.local-region-list{padding:0 16px 12px}.local-region-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--border);font:11px ui-monospace,monospace}.local-region-item button{padding:4px 8px;color:var(--danger)}
@media(max-width:640px){.device-settings-grid{grid-template-columns:minmax(0,1fr)}.reference-device-settings{width:calc(100% - 12px);column-width:auto;column-count:1}}
body[data-theme="tron"]{padding:0;background-color:var(--page-bg);background-image:linear-gradient(rgba(76,232,207,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(76,232,207,.045) 1px,transparent 1px),repeating-linear-gradient(0deg,rgba(255,255,255,.012) 0,rgba(255,255,255,.012) 1px,transparent 1px,transparent 4px);background-size:32px 32px,32px 32px,100% 4px;font:12px/1.45 "IBM Plex Mono","Cascadia Code",ui-monospace,monospace}
body[data-theme="tron"] .dashboard-header{display:grid;grid-template-columns:minmax(160px,210px) minmax(0,1fr) auto;grid-template-areas:"brand nav console" "brand meta meta";align-items:center;gap:8px 18px;min-height:0;margin:0 0 12px;padding:10px 16px;border:0;border-bottom:1px solid var(--border);border-radius:0;background:linear-gradient(100deg,#091319,#0c171d 68%,#11221f);box-shadow:0 10px 28px rgba(0,0,0,.32)}
body[data-theme="tron"] .brand-lockup{grid-area:brand;min-width:0}
body[data-theme="tron"] .dashboard-logo{width:48px;height:48px;object-fit:contain;filter:drop-shadow(0 0 8px rgba(76,232,207,.45))}
body[data-theme="tron"] .brand-copy h1{font:600 18px/1.1 "IBM Plex Mono","Cascadia Code",ui-monospace,monospace;color:var(--text)}
body[data-theme="tron"] .brand-copy .header-label{color:var(--accent)}
body[data-theme="tron"] .top-nav{grid-area:nav;min-width:0;flex-wrap:wrap;gap:3px}
body[data-theme="tron"] .nav-tab{min-height:30px;padding:6px 8px;border:1px solid transparent;border-radius:2px;color:var(--muted);font:10px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace;text-transform:uppercase}
body[data-theme="tron"] .nav-tab:hover,body[data-theme="tron"] .nav-tab[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-dim);color:var(--accent);box-shadow:0 0 12px rgba(76,232,207,.12);transform:none}
body[data-theme="tron"] .header-meta{grid-area:meta;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;width:100%;max-width:none}
body[data-theme="tron"] .header-meta-item{min-height:48px;padding:6px 9px;border:1px solid var(--border);border-left:2px solid var(--accent);background:rgba(5,13,18,.76)}
body[data-theme="tron"] .header-label,body[data-theme="tron"] .eyebrow{margin-bottom:3px;color:var(--muted);font:700 9px/1.2 "IBM Plex Mono","Cascadia Code",ui-monospace,monospace;letter-spacing:0}
body[data-theme="tron"] .weather-current strong{font-size:13px}
body[data-theme="tron"] .weather-location{max-width:100%;font-size:9px}
body[data-theme="tron"] .console-toggle{grid-area:console;justify-self:end;align-self:start;border-color:var(--accent);border-radius:2px;background:var(--accent-dim);color:var(--accent);font:10px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace}
body[data-theme="tron"] .page-view{width:min(100% - 32px,1840px);padding-bottom:16px}
body[data-theme="tron"] .card,body[data-theme="tron"] .analyzer-main,body[data-theme="tron"] .analyzer-side,body[data-theme="tron"] .map-workspace,body[data-theme="tron"] .live-trace-workspace{border:1px solid var(--border);border-radius:3px;background:linear-gradient(145deg,rgba(15,29,35,.98),rgba(8,17,22,.98));box-shadow:0 12px 30px rgba(0,0,0,.28),inset 0 1px rgba(255,255,255,.025)}
body[data-theme="tron"] .panel-heading{padding-bottom:9px;border-bottom:1px solid rgba(76,232,207,.2)}
body[data-theme="tron"] h2,body[data-theme="tron"] h3{font-family:"IBM Plex Mono","Cascadia Code",ui-monospace,monospace;color:var(--text)}
body[data-theme="tron"] button{min-height:32px;border-radius:2px;font:10px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace;text-transform:uppercase;letter-spacing:0}
body[data-theme="tron"] button:hover{border-color:var(--accent);background:var(--accent-dim);color:var(--accent);box-shadow:0 0 14px rgba(76,232,207,.12);transform:none}
body[data-theme="tron"] input,body[data-theme="tron"] select,body[data-theme="tron"] textarea{border-radius:2px;background-color:var(--input-bg);font:11px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace}
body[data-theme="tron"] input:focus,body[data-theme="tron"] select:focus,body[data-theme="tron"] textarea:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(76,232,207,.12)}
body[data-theme="tron"] .connection-layout{width:min(100%,900px)}
body[data-theme="tron"] .connection-card{padding:18px;border-top:2px solid var(--accent)}
body[data-theme="tron"] .messages-layout{gap:10px}
body[data-theme="tron"] .conversation-rail,body[data-theme="tron"] .chat-panel{border-radius:3px}
body[data-theme="tron"] .conversation-target{border-radius:2px;background:#0b171d}
body[data-theme="tron"] .conversation-target.active{border-color:var(--accent);background:var(--accent-dim);box-shadow:inset 2px 0 var(--accent)}
body[data-theme="tron"] .chat-header{border-color:rgba(76,232,207,.25)}
body[data-theme="tron"] #node-chat-history,body[data-theme="tron"] #channel-chat-history{border-color:var(--border);border-radius:2px;background:var(--log-bg)}
body[data-theme="tron"] .chat-message{border-radius:2px;background:#101f25}
body[data-theme="tron"] .chat-message.outgoing{border-color:#28695f;background:#10312d}
body[data-theme="tron"] .chat-message.incoming{border-left:2px solid #e8b968}
body[data-theme="tron"] .analyzer-toolbar{border-color:var(--border);background:linear-gradient(90deg,#10221f,#0b151b)}
body[data-theme="tron"] .analyzer-table th{background:#10211f;color:var(--accent)}
body[data-theme="tron"] .analyzer-table td{border-color:rgba(76,232,207,.12)}
body[data-theme="tron"] .analyzer-table tr:hover td{background:var(--accent-dim)}
body[data-theme="tron"] .analyzer-stat{background:#0b171d}
body[data-theme="tron"] .analyzer-stat strong{color:var(--accent)}
body[data-theme="tron"] .settings-tabs{border-color:var(--border)}
body[data-theme="tron"] .settings-tab[aria-pressed="true"]{border-bottom-color:var(--accent);color:var(--accent)}
body[data-theme="tron"] .settings-item{border-radius:2px;background:rgba(14,29,35,.88)}
body[data-theme="tron"] .tron-mode-button{border-color:#47d9c4;background:#0c2928;color:#a1fff0}
body[data-theme="tron"] .console-dock .card{border-color:var(--accent);background:#081217}
body[data-theme="tron"] .header-status.connected{border-color:var(--accent);background:#123a35;color:var(--accent)}
body[data-theme="tron"] .packet-channel{color:#f0bd69}
@media(max-width:1050px){body[data-theme="tron"] .dashboard-header{grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"brand console" "nav nav" "meta meta"}.dashboard-logo{width:40px;height:40px}}
@media(max-width:640px){body[data-theme="tron"] .dashboard-header{gap:10px;padding:10px}.dashboard-logo{width:36px;height:36px}body[data-theme="tron"] .header-meta{grid-template-columns:repeat(2,minmax(0,1fr))}body[data-theme="tron"] .page-view{width:calc(100% - 16px)}body[data-theme="tron"] .theme-control-row{grid-template-columns:minmax(0,1fr)}}
body[data-theme="tron"].tron-overview{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));grid-template-rows:auto minmax(300px,1.15fr) minmax(220px,.8fr);grid-template-areas:"header header header header header header header header header header header header" "connection connection connection nodes nodes nodes nodes nodes channels channels channels channels" "map map map map map map map map analyzer analyzer analyzer analyzer";gap:10px;width:100%;height:100vh;min-height:100vh;padding:10px;overflow:hidden}
body[data-theme="tron"].tron-overview .dashboard-header{grid-area:header;width:100%;margin:0}
body[data-theme="tron"].tron-overview #nodes-view{grid-area:nodes}
body[data-theme="tron"].tron-overview #channels-view{grid-area:channels}
body[data-theme="tron"].tron-overview #map-view{grid-area:map}
body[data-theme="tron"].tron-overview #analyzer-view{grid-area:analyzer}
body[data-theme="tron"].tron-overview #nodes-view,body[data-theme="tron"].tron-overview #channels-view,body[data-theme="tron"].tron-overview #map-view,body[data-theme="tron"].tron-overview #analyzer-view{display:flex!important;width:auto;min-width:0;min-height:0;height:auto;margin:0;padding:0;overflow:hidden}
body[data-theme="tron"].tron-overview #nodes-view .messages-layout{grid-template-columns:minmax(260px,360px) minmax(0,1fr);width:100%;height:100%;min-height:0;gap:8px}
body[data-theme="tron"].tron-overview #channels-view .messages-layout{grid-template-columns:minmax(260px,360px) minmax(0,1fr);width:100%;height:100%;min-height:0;gap:8px}
body[data-theme="tron"].tron-overview #nodes-view .conversation-rail,body[data-theme="tron"].tron-overview #channels-view .conversation-rail,body[data-theme="tron"].tron-overview #nodes-view .chat-panel,body[data-theme="tron"].tron-overview #channels-view .chat-panel{min-width:0;min-height:0;height:100%;margin:0;overflow:hidden}
body[data-theme="tron"].tron-overview #node-chat-history,body[data-theme="tron"].tron-overview #channel-chat-history{min-height:0}
body[data-theme="tron"].tron-overview #map-view .map-layout{flex:1;min-height:0;grid-template-columns:minmax(260px,360px) minmax(0,1fr);gap:8px}
body[data-theme="tron"].tron-overview #map-view .map-rail{min-width:0;min-height:0;overflow:visible;padding:12px}
body[data-theme="tron"].tron-overview #map-view .map-rail-summary{display:none}
body[data-theme="tron"].tron-overview #map-view .map-peer-filters{gap:5px;margin-bottom:6px}
body[data-theme="tron"].tron-overview #map-view .map-peer-filters label{font-size:9px}
body[data-theme="tron"].tron-overview #map-view .map-peer-filters select,body[data-theme="tron"].tron-overview #map-view .map-peer-filters input{min-height:28px;padding:4px 16px 4px 6px;font-size:9px}
body[data-theme="tron"].tron-overview #map-view #map-node-list{width:310px;min-width:310px;flex:none;min-height:0;overflow:visible;border:0;border-radius:0;background:transparent;box-sizing:border-box}
body[data-theme="tron"].tron-overview #map-view #map-node-list>*{width:100%;box-sizing:border-box}
body[data-theme="tron"].tron-overview #map-view #map-node-list:has(>.map-empty){flex:none;width:310px;min-width:310px;height:auto;max-height:none;overflow:visible;padding:0}
body[data-theme="tron"].tron-overview #map-view #map-node-list>.map-empty{width:310px;height:76px;min-width:310px;max-width:none;box-sizing:border-box;margin:0;justify-self:start}
body[data-theme="tron"].tron-overview #map-view .map-peer-target{min-height:32px;padding:5px 7px;font-size:10px}
body[data-theme="tron"].tron-overview #map-view .map-surface{min-height:0}
body[data-theme="tron"] #map-view #map-node-list{display:grid;grid-template-columns:minmax(0,1fr);grid-auto-rows:max-content;align-content:start;gap:6px;padding:6px;box-sizing:border-box}
body[data-theme="tron"] #map-view #map-node-list:has(>.map-empty){height:auto;max-height:none;overflow:visible;padding:0}
body[data-theme="tron"] #map-view .map-node-row{padding:0;border:0;gap:0}
body[data-theme="tron"] #map-view .map-node-row-main{width:100%;gap:0;align-items:stretch;border:1px solid var(--border);border-radius:2px;background:#0b171d;box-sizing:border-box}
body[data-theme="tron"] #map-view .map-node-row-main:hover{border-color:var(--accent);background:var(--accent-dim)}
body[data-theme="tron"] #map-view .map-peer-target{width:100%;min-width:0;padding:9px}
body[data-theme="tron"] #map-view .map-node-row .favorite-toggle{align-self:center;margin-right:6px}
body[data-theme="tron"] #map-view .peer-inline-detail{margin:6px 0 0}
body[data-theme="tron"].tron-overview #map-view #map-canvas{min-height:0}
body[data-theme="tron"].tron-overview .analyzer-shell{grid-template-columns:minmax(0,1fr);grid-template-rows:repeat(2,minmax(0,1fr));height:100%;min-height:0;gap:8px}
body[data-theme="tron"].tron-overview .analyzer-main,body[data-theme="tron"].tron-overview .analyzer-side{min-width:0;min-height:0;overflow:hidden}
body[data-theme="tron"].tron-overview .analyzer-main{display:flex;flex-direction:column}
body[data-theme="tron"].tron-overview .analyzer-toolbar{flex:none;padding:7px 9px}
body[data-theme="tron"].tron-overview .analyzer-toolbar p{display:none}
body[data-theme="tron"].tron-overview .analyzer-table-wrap{flex:1;min-height:0;overflow:hidden}
body[data-theme="tron"].tron-overview .analyzer-table{min-width:0;table-layout:fixed}
body[data-theme="tron"].tron-overview .analyzer-table th,body[data-theme="tron"].tron-overview .analyzer-table td{padding:4px 5px;overflow:hidden;font-size:8px;text-overflow:ellipsis}
body[data-theme="tron"].tron-overview #analyzer-packet-list>tr:nth-child(n+6){display:none}
body[data-theme="tron"].tron-overview .analyzer-side>.analyzer-side-section:first-child{display:flex;flex:1;flex-direction:column;min-height:0}
body[data-theme="tron"].tron-overview .analyzer-radio-status{flex:1;align-content:space-between}
body[data-theme="tron"].tron-overview .view-panel[hidden]{display:none!important}
body[data-theme="tron"].tron-overview .view-panel:not([hidden]){animation:tron-panel-arrive .32s ease-out both}
@keyframes tron-panel-arrive{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
.tron-panel-bar,.tron-resize,#tron-layout-tray{display:none}
body[data-theme="tron"].tron-overview{grid-template-areas:none;grid-template-rows:auto repeat(12,minmax(0,1fr));grid-auto-rows:minmax(0,1fr);grid-auto-flow:row dense}
body[data-theme="tron"].tron-overview .dashboard-header{grid-area:auto;grid-column:1/-1;grid-row:1;order:-1}
body[data-theme="tron"].tron-overview main.view-panel{grid-area:auto;position:relative;flex-direction:column!important}
body[data-theme="tron"].tron-overview main.view-panel.tron-hidden:is(#nodes-view,#channels-view,#map-view,#analyzer-view){display:none!important}
body[data-theme="tron"].tron-overview main.view-panel.tron-dragging{opacity:.65;z-index:50;outline:1px dashed var(--accent);outline-offset:-1px}
body[data-theme="tron"].tron-overview main.view-panel>.messages-layout,body[data-theme="tron"].tron-overview main.view-panel>.analyzer-shell,body[data-theme="tron"].tron-overview main.view-panel>.map-layout{flex:1 1 0!important;height:auto!important;min-height:0!important}
body[data-theme="tron"].tron-overview .tron-panel-bar{display:flex;flex:none;align-items:center;gap:6px;height:22px;padding:0 4px 0 8px;background:var(--panel-raised);border-bottom:1px solid var(--border);color:var(--muted);font-size:9px;letter-spacing:.12em;text-transform:uppercase;cursor:grab;touch-action:none;user-select:none}
body[data-theme="tron"].tron-overview .tron-panel-bar:active{cursor:grabbing}
body[data-theme="tron"].tron-overview .tron-panel-bar .tron-grip{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
body[data-theme="tron"].tron-overview .tron-panel-bar button{min-height:0;width:20px;height:18px;padding:0;line-height:1;font-size:12px;cursor:pointer}
body[data-theme="tron"].tron-overview .tron-resize{display:block;position:absolute;right:0;bottom:0;width:16px;height:16px;z-index:5;cursor:nwse-resize;touch-action:none;background:linear-gradient(135deg,transparent 50%,var(--accent) 50%,var(--accent) 58%,transparent 58%,transparent 72%,var(--accent) 72%,var(--accent) 80%,transparent 80%)}
body[data-theme="tron"].tron-overview #tron-layout-tray{display:flex;position:fixed;left:12px;bottom:12px;z-index:800;align-items:center;gap:6px;padding:5px 8px;background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
body[data-theme="tron"].tron-overview #tron-layout-tray button{min-height:0;padding:3px 8px;font-size:9px;cursor:pointer}
body[data-theme="tron"].tron-overview #tron-layout-tray button[aria-pressed="false"]{opacity:.5;text-decoration:line-through}
@media(max-width:1050px){body[data-theme="tron"].tron-overview{height:auto;min-height:100vh;overflow:auto;grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:auto repeat(4,minmax(320px,auto));grid-template-areas:"header header" "connection nodes" "channels channels" "map map" "analyzer analyzer"}body[data-theme="tron"].tron-overview:has(#nodes-view:not([hidden])){height:auto;overflow:auto}body[data-theme="tron"].tron-overview #channels-view .messages-layout{grid-template-columns:minmax(150px,38%) minmax(0,1fr)}}
@media(max-width:900px){body[data-theme="tron"].tron-overview #map-view .map-layout{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(150px,.72fr) minmax(180px,1.28fr)}}
@media(max-width:640px){body[data-theme="tron"].tron-overview{grid-template-columns:minmax(0,1fr);grid-template-rows:auto repeat(4,minmax(300px,auto));grid-template-areas:"header" "nodes" "channels" "map" "analyzer";padding:8px}body[data-theme="tron"].tron-overview .analyzer-shell{grid-template-columns:minmax(0,1fr);grid-template-rows:repeat(2,minmax(150px,1fr));height:100%}body[data-theme="tron"].tron-overview #nodes-view .messages-layout,body[data-theme="tron"].tron-overview #channels-view .messages-layout{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(110px,.38fr) minmax(0,1fr)}body[data-theme="tron"].tron-overview #nodes-view .conversation-rail,body[data-theme="tron"].tron-overview #channels-view .conversation-rail{max-height:none}}
.tron-quick-tabs{display:none}
body[data-theme="tron"]{--page-bg:#000;--panel-bg:rgba(0,0,0,.62);--panel-raised:rgba(0,10,16,.58);--accent:#00d8ff;--accent-dim:rgba(0,92,122,.24);--border:rgba(0,190,235,.45);--input-bg:rgba(0,0,0,.76);--input-border:rgba(0,190,235,.55);--log-bg:rgba(0,0,0,.75);background-color:#000;background-image:linear-gradient(rgba(0,190,235,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(0,190,235,.018) 1px,transparent 1px);background-size:32px 32px}
body[data-theme="tron"] .dashboard-header{display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-start;gap:3px 8px;min-height:0;margin:0 0 8px;padding:4px 10px;border:0;border-bottom:1px solid rgba(0,190,235,.45);border-radius:0;background:rgba(0,0,0,.82);box-shadow:none;backdrop-filter:blur(5px)}
body[data-theme="tron"] .brand-lockup{display:none}
body[data-theme="tron"] .top-nav{display:none;order:2;flex:0 1 auto;min-width:0;flex-wrap:nowrap;gap:1px;overflow-x:auto}
body[data-theme="tron"] .nav-tab{flex:none;min-height:25px;padding:3px 6px;border-radius:1px;font-size:9px}
body[data-theme="tron"] .nav-tab[data-view="settings"],body[data-theme="tron"] .nav-tab[data-view="device-settings"]{display:none}
body[data-theme="tron"] .nav-tab:hover,body[data-theme="tron"] .nav-tab[aria-pressed="true"]{border-color:#00d8ff;background:rgba(0,105,140,.2);color:#4be7ff;box-shadow:none}
body[data-theme="tron"] .header-meta{order:1;flex:1 1 440px;display:flex;align-items:center;justify-content:flex-end;gap:0;width:auto;max-width:none;min-width:0;border:0;background:transparent;overflow:visible}
body[data-theme="tron"] .header-meta-item{display:flex;flex:0 1 auto;flex-direction:row;align-items:center;gap:5px;min-width:0;min-height:24px;padding:2px 8px;border:0;border-left:1px solid rgba(0,190,235,.3);background:transparent}
body[data-theme="tron"] .header-meta-item.weather-widget{min-width:0}
body[data-theme="tron"] .header-label{display:inline;margin:0;color:#70a8bc;font-size:8px;font-weight:600;letter-spacing:0}
body[data-theme="tron"] .header-metric{font-size:10px;color:#d4f4fa}
body[data-theme="tron"] .header-status{padding:2px 5px;border:0;background:transparent;color:#d4f4fa;font-size:9px}
body[data-theme="tron"] .header-status.connected{color:#72ffcb}
body[data-theme="tron"] .weather-current{gap:4px}
body[data-theme="tron"] .weather-icon{width:18px;height:18px}
body[data-theme="tron"] .weather-icon svg{width:16px;height:16px}
body[data-theme="tron"] .weather-current strong{font-size:10px}
body[data-theme="tron"] .weather-current span:not(.weather-icon){font-size:9px}
body[data-theme="tron"] .weather-place{max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
body[data-theme="tron"] .tron-quick-tabs{order:3;display:flex;flex:none;align-items:center;gap:3px}
body[data-theme="tron"] .tron-quick-tabs button,body[data-theme="tron"] .console-toggle{position:static;min-height:25px;padding:3px 7px;border:1px solid rgba(0,190,235,.42);border-radius:1px;background:rgba(0,45,62,.28);color:#78ddf4;font:9px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace}
body[data-theme="tron"] .console-toggle{order:4;align-self:center;border-color:rgba(0,216,255,.62);color:#4be7ff}
body[data-theme="tron"] .console-toggle:hover,body[data-theme="tron"] .tron-quick-tabs button:hover{border-color:#00d8ff;background:rgba(0,105,140,.2);color:#fff;box-shadow:none}
body[data-theme="tron"] .card,body[data-theme="tron"] .analyzer-main,body[data-theme="tron"] .analyzer-side,body[data-theme="tron"] .map-workspace,body[data-theme="tron"] .live-trace-workspace,body[data-theme="tron"] .settings-item{border-color:rgba(0,170,215,.35);border-radius:2px;background:rgba(0,5,9,.58);box-shadow:none;backdrop-filter:blur(3px)}
body[data-theme="tron"] .panel-heading,body[data-theme="tron"] .chat-header{border-color:rgba(0,190,235,.25)}
body[data-theme="tron"] .conversation-target,body[data-theme="tron"] .analyzer-stat,body[data-theme="tron"] .analyzer-table th{background:rgba(0,18,26,.56)}
body[data-theme="tron"] .chat-message,body[data-theme="tron"] .chat-message.outgoing{background:rgba(0,35,48,.48)}
body[data-theme="tron"] .chat-message.incoming{border-left-color:#f1bc69}
body[data-theme="tron"] .analyzer-toolbar{background:rgba(0,15,22,.5)}
body[data-theme="tron"] .analyzer-table tr:hover td{background:rgba(0,90,120,.18)}
body[data-theme="tron"] .settings-tabs{background:transparent}
body[data-theme="tron"] .tron-mode-button{background:rgba(0,80,100,.28)}
body[data-theme="tron"].tron-overview{background-color:#000}
.incoming-adverts{display:flex;flex:0 0 auto;width:min(33%,300px);flex-direction:row;align-items:center;gap:8px;min-width:180px;max-width:33%;height:30px;overflow:hidden;padding:2px 6px;border-left:2px solid var(--accent);background:transparent}
.incoming-adverts-heading{display:flex;flex:none;flex-direction:column;align-items:flex-start;gap:1px;margin:0}
.incoming-adverts-heading h3{margin:0;color:var(--muted);font-size:8px;font-weight:700;text-transform:uppercase;white-space:nowrap}
.incoming-adverts-count{color:var(--muted);font:8px ui-monospace,monospace}
.incoming-adverts-list{position:relative;display:block;flex:1;align-self:stretch;min-width:0;overflow-x:hidden;overflow-y:auto;scrollbar-width:none;overscroll-behavior:contain;-webkit-mask-image:linear-gradient(180deg,transparent,#000 18%,#000 82%,transparent);mask-image:linear-gradient(180deg,transparent,#000 18%,#000 82%,transparent)}
.incoming-advert-empty{margin:0;color:var(--muted);font-size:8px;line-height:1.3}
.incoming-advert-track{display:block}
.incoming-advert-group{display:flex;flex-direction:column;gap:1px;padding-bottom:6px}
.incoming-advert-row{display:flex;align-items:center;justify-content:flex-start;gap:6px;width:fit-content;max-width:100%;min-width:0;min-height:11px;padding:0 4px;border-left:1px solid var(--border);background:transparent}
.incoming-advert-name{min-width:0;overflow:hidden;color:var(--text);font:9px ui-monospace,monospace;text-overflow:ellipsis;white-space:nowrap}
.incoming-advert-meta{flex:none;color:var(--muted);font:8px ui-monospace,monospace;white-space:nowrap}
body[data-theme="tron"] .incoming-adverts-list::-webkit-scrollbar,.incoming-adverts-list::-webkit-scrollbar{display:none}
body[data-theme="tron"] .incoming-adverts{flex:0 0 auto;width:min(33%,300px);min-width:0;max-width:33%;height:30px;border-left-color:#00d8ff;background:rgba(0,0,0,.45)}
body[data-theme="tron"] .incoming-adverts-heading h3{color:#58dff7;font:600 9px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace}
body[data-theme="tron"] .incoming-adverts-count{font-size:9px}
body[data-theme="tron"] .incoming-advert-empty{font:9px/1.3 ui-monospace,monospace}
body[data-theme="tron"] .incoming-advert-row{border-left:2px solid rgba(0,190,235,.52);background:rgba(0,30,42,.36)}
body[data-theme="tron"] .incoming-advert-name{font-size:10px}
body[data-theme="tron"] .incoming-advert-meta{color:#75b4c8;font-size:9px}
#toast-stack{position:fixed;top:70px;right:16px;z-index:1100;display:flex;flex-direction:column;gap:8px;width:min(320px,calc(100vw - 32px));pointer-events:none}
.toast{padding:9px 12px;background:var(--panel-raised,var(--panel-bg));color:var(--text);border:1px solid var(--accent);border-left-width:4px;border-radius:6px;box-shadow:0 6px 24px rgba(0,0,0,.4);font-size:12px;line-height:1.35;opacity:0;transform:translateX(16px);transition:opacity .25s ease,transform .25s ease}
.toast.clickable{pointer-events:auto;cursor:pointer}
.toast.clickable:hover{filter:brightness(1.15)}
.toast.show{opacity:1;transform:translateX(0)}
.toast strong{display:block;margin-bottom:2px;color:var(--accent);font-size:10px;letter-spacing:.08em;text-transform:uppercase}
.toast span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.noise-floor{display:flex;flex:1 1 160px;align-items:center;gap:8px;min-width:140px;max-width:320px;height:30px;overflow:hidden;padding:2px 6px;border-left:2px solid var(--accent);background:transparent}
.noise-floor-heading{display:flex;flex:none;flex-direction:column;align-items:flex-start;gap:1px}
.noise-floor-heading h3{margin:0;color:var(--muted);font-size:8px;font-weight:700;text-transform:uppercase;white-space:nowrap}
.noise-floor-value{color:var(--accent);font:9px ui-monospace,monospace;white-space:nowrap}
.cpu-temp{display:flex;flex:none;flex-direction:column;align-items:flex-end;gap:1px}
.cpu-temp h3{margin:0;color:var(--muted);font-size:8px;font-weight:700;text-transform:uppercase}
.noise-floor-canvas{flex:1;min-width:0;width:100%;height:26px;display:block}
body[data-theme="tron"] .noise-floor{border-left-color:#00d8ff;background:rgba(0,0,0,.45)}
body[data-theme="tron"] .noise-floor-heading h3{color:#58dff7;font:600 9px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace}
@media(max-width:720px){.noise-floor{flex-basis:100%;max-width:none}}
.incoming-advert-new{animation:advert-arrival .24s ease-out}
@keyframes advert-arrival{from{opacity:0;transform:translateX(-5px)}to{opacity:1;transform:translateX(0)}}
body[data-theme="tron"] .analyzer-stat-grid{display:none}
body[data-theme="tron"] .analyzer-side>.analyzer-side-section:nth-child(2){display:none}
body[data-theme="tron"] .analyzer-side>.analyzer-side-section:last-child{display:none}
body[data-theme="tron"].tron-overview .analyzer-side{overflow:hidden}
body[data-theme="tron"].tron-overview .analyzer-side-section{padding:6px 8px}
body[data-theme="tron"].tron-overview .analyzer-side h3{margin:0 0 4px;font-size:9px}
body[data-theme="tron"].tron-overview .analyzer-radio-status{grid-template-columns:repeat(3,minmax(0,1fr));align-content:start;gap:3px 7px;margin-top:3px}
body[data-theme="tron"].tron-overview .analyzer-radio-state,body[data-theme="tron"].tron-overview .analyzer-radio-updated{grid-column:1/-1;margin:0;font-size:8px;line-height:1.2}
body[data-theme="tron"].tron-overview .analyzer-radio-group{min-width:0}
body[data-theme="tron"].tron-overview .analyzer-radio-group h4{margin:0 0 2px;font-size:8px}
body[data-theme="tron"].tron-overview .analyzer-radio-list{grid-template-columns:minmax(0,1fr) auto;gap:1px 3px}
body[data-theme="tron"].tron-overview .analyzer-radio-list dt,body[data-theme="tron"].tron-overview .analyzer-radio-list dd{overflow:hidden;font:8px/1.15 ui-monospace,monospace;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:1050px){body[data-theme="tron"] .dashboard-header{gap:4px 6px}body[data-theme="tron"] .header-meta{flex-basis:100%;order:1;justify-content:space-between}body[data-theme="tron"] .top-nav{order:2;flex:1 1 auto}body[data-theme="tron"] .tron-quick-tabs{order:3;margin-left:auto}body[data-theme="tron"] .console-toggle{order:4}}
@media(max-width:720px){.incoming-adverts{flex-basis:100%;max-width:none;height:30px}}
@media(max-width:640px){body[data-theme="tron"] .dashboard-header{gap:4px;padding:4px 6px}body[data-theme="tron"] .incoming-adverts{order:0;flex-basis:100%;max-width:none;height:30px}body[data-theme="tron"] .header-meta{flex-wrap:wrap;justify-content:flex-start;gap:0}body[data-theme="tron"] .header-meta-item{padding:2px 5px}body[data-theme="tron"] .top-nav{flex-basis:100%;order:2}body[data-theme="tron"] .tron-quick-tabs{order:3;margin-left:0}body[data-theme="tron"] .console-toggle{order:4}}
.tron-settings-back{display:none}
body[data-theme="tron"] #settings-view .tron-settings-back{display:inline-flex;align-self:flex-start;min-height:28px;margin:0 0 8px;padding:4px 8px;border:1px solid rgba(0,190,235,.42);border-radius:1px;background:rgba(0,45,62,.28);color:#78ddf4;font:9px "IBM Plex Mono","Cascadia Code",ui-monospace,monospace}
.add-btn{margin-left:8px;padding:3px 9px;border:1px solid var(--accent);border-radius:6px;background:var(--accent-dim);color:var(--accent);font:600 11px inherit;cursor:pointer;white-space:nowrap}
.rail-capacity{display:block;margin:2px 0 0;color:var(--muted);font:11px ui-monospace,monospace}
.rail-capacity[data-full="true"]{color:var(--danger)}
.panel-heading .heading-tools{display:flex;align-items:center;gap:4px}
#add-dialog{width:min(420px,94vw);padding:16px;border:1px solid var(--border);border-radius:10px;background:var(--panel-bg);color:var(--text)}
#add-dialog::backdrop{background:rgba(0,0,0,.6)}
#add-dialog h3{margin:0 0 4px;font-size:16px}
#add-dialog textarea{width:100%;min-height:70px}
#add-dialog .add-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}
#add-dialog .add-note{margin:6px 0 0;color:var(--muted);font-size:11px}
#add-dialog .add-status{min-height:1.3em;margin-top:8px;font-size:12px;word-break:break-all}
#add-dialog .add-status[data-state="error"]{color:var(--danger)}
#add-dialog .add-status[data-state="success"]{color:var(--accent)}
html[data-device="mobile"] body,html[data-device="tablet"] body{-webkit-text-size-adjust:100%;-webkit-tap-highlight-color:transparent;padding-left:max(8px,env(safe-area-inset-left));padding-right:max(8px,env(safe-area-inset-right));padding-bottom:max(8px,env(safe-area-inset-bottom))}
html[data-device="mobile"] input,html[data-device="mobile"] select,html[data-device="mobile"] textarea{font-size:16px}
html[data-device="mobile"] button,html[data-device="mobile"] select,html[data-device="mobile"] input:not([type=checkbox]):not([type=radio]){min-height:44px}
html[data-device="mobile"] .nav-tab{min-height:44px;padding:0 14px}
html[data-device="mobile"] .top-nav{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
html[data-device="mobile"] .dashboard-header{position:static}
html[data-device="mobile"] .header-meta{gap:8px}
html[data-device="mobile"] #nodes-view .messages-layout,html[data-device="mobile"] #channels-view .messages-layout{display:flex;flex-direction:column;height:auto;overflow:visible}
html[data-device="mobile"] #nodes-view .conversation-rail,html[data-device="mobile"] #channels-view .conversation-rail{flex:none;max-height:70dvh;min-height:180px}
html[data-device="mobile"] #nodes-view .chat-panel,html[data-device="mobile"] #channels-view .chat-panel{flex:none;min-height:60dvh;max-height:none;overflow:visible}
html[data-device="mobile"] .conversation-target-list{grid-auto-rows:max-content;min-height:120px;overflow-y:auto;-webkit-overflow-scrolling:touch}
html[data-device="mobile"] .chat-panel form{position:sticky;bottom:0;padding:8px 0;background:var(--panel-bg)}
html[data-device="mobile"] .map-layout{grid-template-columns:minmax(0,1fr)!important}
html[data-device="mobile"] #map-canvas{height:55dvh;min-height:300px}
html[data-device="mobile"] .map-rail{max-height:none;min-height:0}
html[data-device="mobile"] #map-node-list{max-height:32dvh;overflow-y:auto}
html[data-device="mobile"] .leaflet-control-zoom a{width:40px;height:40px;line-height:40px;font-size:20px}
html[data-device="mobile"] .console-dock{max-height:50dvh}
html[data-device="mobile"] #add-dialog{width:calc(100vw - 20px);max-height:90dvh;overflow:auto}
html[data-device="mobile"] .analyzer-table-wrap,html[data-device="mobile"] .analyzer-main{overflow-x:auto}
html[data-device="tablet"] .conversation-rail{min-height:300px}
html[data-device="tablet"] button,html[data-device="tablet"] select{min-height:40px}
html[data-device="tablet"] input,html[data-device="tablet"] select,html[data-device="tablet"] textarea{font-size:16px}
@media(hover:none){.conversation-entry button,.map-node-row,.nav-tab{min-height:44px}}
.chat-message{cursor:pointer}
#msg-actions{position:fixed;inset:0;z-index:1200;display:none;align-items:flex-end;justify-content:center;background:rgba(0,0,0,.55)}
#msg-actions.open{display:flex}
#msg-actions .sheet{width:min(520px,100%);max-height:80dvh;overflow:auto;border:1px solid var(--border);border-radius:12px 12px 0 0;background:var(--panel-bg);padding-bottom:max(12px,env(safe-area-inset-bottom))}
@media(min-width:721px){#msg-actions{align-items:center}#msg-actions .sheet{border-radius:12px}}
#msg-actions .sheet-head{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--panel-raised);font-weight:600}
#msg-actions .sheet-head button{min-width:0;padding:2px 8px;font-size:18px;line-height:1}
#msg-actions .sheet-preview{margin:10px 12px 0;padding:8px 10px;border-radius:8px;background:var(--input-bg);color:var(--muted);font-size:12px;overflow-wrap:anywhere;max-height:84px;overflow:hidden}
#msg-actions .sheet-action{display:flex;align-items:center;gap:12px;width:calc(100% - 16px);margin:8px;padding:13px 14px;border:1px solid var(--border);border-radius:10px;background:var(--panel-raised);color:var(--text);font-size:14px;text-align:left;cursor:pointer}
#msg-actions .sheet-action.danger{color:var(--danger)}
#msg-actions .sheet-action svg{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#msg-actions .sheet-action span{flex:1}
#msg-actions .sheet-paths{margin:8px 12px;padding:10px;border-radius:8px;background:var(--input-bg);font-size:12px;line-height:1.7}
#msg-actions .sheet-paths div{display:flex;justify-content:space-between;gap:12px}
#msg-actions .sheet-paths .muted{color:var(--muted);margin-top:6px;font-size:11px}
.map-search-overlay .map-peer-filters{display:block;margin:0}.map-search-overlay .search-input-row{display:flex;align-items:center;gap:6px;padding:4px}.map-search-overlay .search-input-row input{flex:1;min-width:0}
.map-search-overlay #map-node-list{display:none;margin-top:6px;max-height:min(50vh,360px);overflow-y:auto;grid-auto-rows:max-content;gap:6px;padding:6px;border:1px solid var(--border);border-radius:6px;background:var(--panel-bg,var(--log-bg))}
.map-search-overlay.open #map-node-list{display:grid}
body[data-theme="tron"] .map-search-overlay #map-node-list,body[data-theme="tron"].tron-overview #map-view .map-search-overlay #map-node-list{width:auto;min-width:0;flex:none;overflow-y:auto;overflow-x:hidden;background:rgba(0,18,26,.94);border:1px solid var(--border);border-radius:3px;padding:6px}
body[data-theme="tron"] .map-search-overlay .search-input-row{background:rgba(0,18,26,.94);border:1px solid var(--border)}
.map-search-overlay .add-btn{flex:none}
body[data-theme="tron"] #map-view .map-search-overlay:not(.open) #map-node-list{display:none}
#map-view .map-layout,body[data-theme="tron"].tron-overview #map-view .map-layout{grid-template-columns:minmax(0,1fr)!important}
#map-view .map-surface,#map-view #map-canvas{width:100%}
</style>
<script>
let gatewayTelemetry={};
let meshChannels=[];
let peerLimits={};
let selectedNodeId='';
let selectedChannelId='';
let activeView='nodes';
let deviceSettingsLoaded=false;
let loadedDeviceSettings=null;
let favoriteNodeIds=new Set();
let appConfig={model:'llama3.2:1b',theme:'midnight',connection:{type:'bluetooth',ble_mac:'',serial_port:''},bot:{name:'MeshCore Assistant',personality:'helpful, friendly, and concise',response_length:'medium'},ollama:{schedule_enabled:false,start_time:'07:00',end_time:'17:00'},auto_update:{enabled:true}};
let configEditorLoaded=false;
const commonRadioProfiles={balanced:{radio_bw:125,radio_sf:7,radio_cr:5},long_range:{radio_bw:125,radio_sf:10,radio_cr:5},high_throughput:{radio_bw:250,radio_sf:7,radio_cr:5}};
function applyTheme(theme,persist=true){let previousTheme=document.body.dataset.theme,wasOverview=document.body.classList.contains('tron-overview');document.body.dataset.theme=theme;let modeSelect=document.getElementById('theme-mode-select'),colorSelect=document.getElementById('theme-select'),colorControl=document.getElementById('classic-theme-control');if(modeSelect)modeSelect.value=theme==='tron'?'tron':'classic';if(colorSelect&&theme!=='tron')colorSelect.value=theme;if(colorControl)colorControl.hidden=theme==='tron';let sessionTitle=document.getElementById('analyzer-session-title');if(sessionTitle)sessionTitle.textContent=theme==='tron'?'MeshCore Live Statistics':'Session';if(theme==='tron'&&previousTheme!=='tron'||theme!=='tron'&&wasOverview)showView('nodes');if(persist)saveAppConfig({...appConfig,theme})}
function selectThemeMode(mode){if(mode==='tron'){applyTheme('tron');return}let colorTheme=document.getElementById('theme-select').value;applyTheme(colorTheme==='tron'?'midnight':colorTheme)}
function selectClassicTheme(theme){try{localStorage.setItem('meshcore-classic-theme',theme)}catch(error){}applyTheme(theme)}
function showSettingsTab(tab){document.querySelectorAll('.settings-tab').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.settingsTab===tab)));for(let panel of document.querySelectorAll('.settings-tab-panel'))panel.hidden=panel.id!=='settings-'+tab+'-panel';if(tab==='config'){if(!configEditorLoaded)loadConfigEditor();loadAutostart()}if(tab==='ollama')loadOllamaModels();if(tab==='tightvnc'){loadTightvnc();loadSshTerminal()}if(tab==='logs')loadAppLogs()}
async function loadAutostart(){let box=document.getElementById('autostart-enabled');try{let response=await fetch('/api/autostart'),data=await response.json();box.checked=!!data.enabled}catch(error){}}
async function saveAutostart(){let box=document.getElementById('autostart-enabled'),status=document.getElementById('autostart-status'),wanted=box.checked;try{let response=await fetch('/api/autostart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:wanted})}),data=await response.json();if(!response.ok)throw new Error(data.error||'Failed');status.textContent=wanted?'Enabled':'Disabled'}catch(error){box.checked=!wanted;status.textContent=error.message}}
async function loadTightvnc(){let status=document.getElementById('tightvnc-status');try{let response=await fetch('/api/tightvnc'),data=await response.json();if(!response.ok)throw new Error(data.error||'Status unavailable');serverAddresses=data.addresses||[];document.querySelectorAll('.live-host-url').forEach(el=>el.textContent=hostUrl(el.dataset.port,el.dataset.path));status.textContent=`TightVNC: ${data.vnc_running?'on':'off'} · noVNC: ${data.novnc_running?'on':'off'} · ${hostUrl(6080,'/vnc.html')}`;status.dataset.state=data.vnc_running&&data.novnc_running?'success':''}catch(error){status.textContent=error.message;status.dataset.state='error'}}
async function setTightvnc(action){let status=document.getElementById('tightvnc-status');status.textContent=`${action==='restart'?'Restarting':'Turning '+action} TightVNC...`;status.dataset.state='';for(let button of document.querySelectorAll('[data-tightvnc-action]'))button.disabled=true;try{let response=await fetch('/api/tightvnc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})}),data=await response.json();if(!response.ok)throw new Error(data.error||'TightVNC operation failed');status.textContent=`TightVNC: ${data.vnc_running?'on':'off'} · noVNC: ${data.novnc_running?'on':'off'} · ${hostUrl(6080,'/vnc.html')}`;status.dataset.state='success'}catch(error){status.textContent=error.message;status.dataset.state='error'}finally{for(let button of document.querySelectorAll('[data-tightvnc-action]'))button.disabled=false}}
let sshTerminalRunning=false;
function renderSshTerminal(data){let status=document.getElementById('ssh-terminal-status');sshTerminalRunning=!!data.running;document.getElementById('ssh-open-button').disabled=!sshTerminalRunning;status.textContent=data.installed===false?'ttyd/ssh not installed. Run setup.sh.':`SSH terminal: ${data.running?'on':'off'} · login as ${data.user}`;status.dataset.state=data.running?'success':''}
async function loadSshTerminal(){try{let response=await fetch('/api/ssh-terminal'),data=await response.json();if(!response.ok)throw new Error(data.error||'Status unavailable');renderSshTerminal(data)}catch(error){let status=document.getElementById('ssh-terminal-status');status.textContent=error.message;status.dataset.state='error'}}
async function setSshTerminal(action){let status=document.getElementById('ssh-terminal-status');status.textContent='Working...';status.dataset.state='';for(let button of document.querySelectorAll('[data-ssh-action]'))button.disabled=true;try{let response=await fetch('/api/ssh-terminal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})}),data=await response.json();if(!response.ok)throw new Error(data.error||'SSH terminal operation failed');renderSshTerminal(data)}catch(error){status.textContent=error.message;status.dataset.state='error'}finally{for(let button of document.querySelectorAll('[data-ssh-action]'))button.disabled=false}}
function openSshTerminal(){window.open(hostUrl(7681,'/'),'_blank','noopener')}
async function loadAppLogs(){let output=document.getElementById('app-log-output');try{let response=await fetch('/api/logs'),data=await response.json();output.textContent=(data.logs||[]).join('\n')||'No log entries.';output.scrollTop=output.scrollHeight}catch(error){output.textContent=error.message}}
async function clearAppLogs(){if(!confirm('Delete all stored logs?'))return;let status=document.getElementById('app-log-status');try{let response=await fetch('/api/logs',{method:'DELETE'});if(!response.ok)throw new Error((await response.json()).error||'Failed');status.textContent='Logs cleared.';loadAppLogs()}catch(error){status.textContent=error.message}}
function syncConfigControls(){let colorSelect=document.getElementById('theme-select'),savedClassicTheme=null;try{savedClassicTheme=localStorage.getItem('meshcore-classic-theme')}catch(error){}let validSavedClassicTheme=[...colorSelect.options].some(option=>option.value===savedClassicTheme),classicTheme=appConfig.theme==='tron'?(validSavedClassicTheme?savedClassicTheme:'midnight'):appConfig.theme;colorSelect.value=classicTheme;let modelSelect=document.getElementById('model');if(![...modelSelect.options].some(option=>option.value===appConfig.model))modelSelect.add(new Option(appConfig.model,appConfig.model));modelSelect.value=appConfig.model;document.getElementById('weather-city').value=appConfig.weather.city;document.getElementById('weather-state').value=appConfig.weather.state;document.getElementById('bot-name').value=appConfig.bot.name;document.getElementById('bot-personality').value=appConfig.bot.personality;document.getElementById('bot-response-length').value=appConfig.bot.response_length;document.getElementById('ollama-schedule-enabled').checked=appConfig.ollama.schedule_enabled;document.getElementById('ollama-schedule-start').value=appConfig.ollama.start_time;document.getElementById('ollama-schedule-end').value=appConfig.ollama.end_time;document.getElementById('auto-update-enabled').checked=appConfig.auto_update.enabled;applyTheme(appConfig.theme,false)}
const weatherIcons={sun:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></svg>',partly:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="16" cy="7" r="3"/><path d="M16 2v1m0 8v1m5-5h-1m-8 0h-1M5 19h12a3 3 0 0 0 .3-6A5 5 0 0 0 8 11.5 3.8 3.8 0 0 0 5 19Z"/></svg>',cloud:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 19h13a4 4 0 0 0 .4-8A6 6 0 0 0 7 9.5 4.8 4.8 0 0 0 5 19Z"/></svg>',fog:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 14h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 6 4 4 0 0 0 5 14Zm-2 4h14m-10 3h14"/></svg>',rain:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 15h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 7 4 4 0 0 0 5 15Zm2 3-1 2m7-2-1 2m7-2-1 2"/></svg>',snow:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 14h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 6 4 4 0 0 0 5 14Zm2 4h.01M12 19h.01M18 18h.01"/></svg>',storm:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 14h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 6 4 4 0 0 0 5 14Zm8 1-3 4h3l-1 3 4-5h-3l1-2"/></svg>'};
function weatherIconName(code){if(code===null||code===undefined||!Number.isFinite(Number(code)))return 'cloud';code=Number(code);if(code===0)return 'sun';if(code===1||code===2)return 'partly';if(code===45||code===48)return 'fog';if(code===51||code===53||code===55||code===56||code===57||code===61||code===63||code===65||code===66||code===67||code===80||code===81||code===82)return 'rain';if(code===71||code===73||code===75||code===77||code===85||code===86)return 'snow';if(code===95||code===96||code===99)return 'storm';return 'cloud'}
function renderWeatherIcon(code,condition='Weather condition unavailable'){let icon=document.getElementById('weather-icon');icon.innerHTML=weatherIcons[weatherIconName(code)]||weatherIcons.cloud;icon.setAttribute('aria-label',condition);icon.title=condition}
async function loadLocalWeather(){let temperature=document.getElementById('weather-temperature'),condition=document.getElementById('weather-condition'),location=document.getElementById('weather-location'),extra=document.getElementById('weather-extra'),widget=document.getElementById('banner-item-weather');condition.textContent='Loading';try{let response=await fetch('/api/local-weather'),data=await response.json();if(!response.ok)throw new Error(data.error||'Weather unavailable');temperature.textContent=Math.round(data.temperature_f)+'°F';condition.textContent=data.condition;location.textContent=data.short_location||data.location;if(extra)extra.textContent=[data.humidity!=null?'💧'+Math.round(data.humidity)+'%':'',data.wind_mph!=null?'💨'+Math.round(data.wind_mph)+' mph':''].filter(Boolean).join(' ');if(widget)widget.title=data.location;renderWeatherIcon(data.weather_code,data.condition)}catch(error){temperature.textContent='--°F';condition.textContent=error.message.includes('Enter a city')?'Set location':'Unavailable';if(extra)extra.textContent='';location.textContent=appConfig.weather.city?(appConfig.weather.state?appConfig.weather.city+', '+appConfig.weather.state:appConfig.weather.city):'Location not set';if(widget)widget.title=location.textContent;renderWeatherIcon(null,condition.textContent)}}
async function loadAppConfig(){try{let response=await fetch('/api/config'),data=await response.json();if(!response.ok)throw new Error(data.error||'Settings could not be loaded');appConfig=data;syncConfigControls();if(new URLSearchParams(window.location.search).get('preview')==='tron')applyTheme('tron',false);loadLocalWeather();maybeCheckForUpdates()}catch(error){let statusMessage=document.getElementById('preferences-status');statusMessage.dataset.state='error';statusMessage.textContent=error.message}}
async function saveAppConfig(config,statusId='preferences-status'){let statusMessage=document.getElementById(statusId);statusMessage.dataset.state='';statusMessage.textContent='Saving config.json...';try{let response=await fetch('/api/config',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)}),data=await response.json();if(!response.ok)throw new Error(data.error||'Settings could not be saved');appConfig=data;syncConfigControls();if(statusId==='config-status'){document.getElementById('config-json-editor').value=JSON.stringify(appConfig,null,2);configEditorLoaded=true}statusMessage.textContent='Saved to config.json.';statusMessage.dataset.state='success'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
async function savePreference(key,value){await saveAppConfig({...appConfig,[key]:value})}
function botTerminalLine(cls,text){let out=document.getElementById('bot-terminal-output'),line=document.createElement('div');line.className=cls;line.textContent=text;out.appendChild(line);out.scrollTop=out.scrollHeight;return line}
async function sendBotTerminal(event){event.preventDefault();let input=document.getElementById('bot-terminal-text'),send=document.getElementById('bot-terminal-send'),message=input.value.trim();if(!message)return;input.value='';botTerminalLine('you','> '+message);send.disabled=true;let pending=botTerminalLine('sys','thinking...');try{let response=await fetch('/api/bot-console',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message})}),data=await response.json();pending.remove();if(!response.ok)throw new Error(data.error||'Request failed');botTerminalLine('bot',data.reply||'(no reply)')}catch(error){pending.remove();botTerminalLine('err','Error: '+error.message)}finally{send.disabled=false;input.focus()}}
async function clearBotTerminal(){document.getElementById('bot-terminal-output').replaceChildren();try{await fetch('/api/bot-console',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reset:true})})}catch(error){}}
async function saveBotSettings(event){event.preventDefault();let name=document.getElementById('bot-name').value.trim(),personality=document.getElementById('bot-personality').value.trim(),responseLength=document.getElementById('bot-response-length').value,greetNewUsers=document.getElementById('bot-greet-new-users').checked,statusMessage=document.getElementById('bot-settings-status');if(!name||!personality){statusMessage.textContent='Enter a bot name and personality.';statusMessage.dataset.state='error';return}await saveAppConfig({...appConfig,bot:{name,personality,response_length:responseLength,greet_new_users:greetNewUsers}},'bot-settings-status')}
async function saveGreetingSetting(enabled){let checkbox=document.getElementById('bot-greet-new-users'),statusMessage=document.getElementById('bot-settings-status');if(enabled&&!appConfig.bot.greet_channel){checkbox.checked=false;statusMessage.textContent='Send /greet on in the channel where you want greetings sent.';statusMessage.dataset.state='error';return}await saveAppConfig({...appConfig,bot:{...appConfig.bot,greet_new_users:enabled}},'bot-settings-status')}
async function saveWeatherLocation(event){event.preventDefault();let city=document.getElementById('weather-city').value.trim(),state=document.getElementById('weather-state').value.trim();if(!city){let statusMessage=document.getElementById('weather-settings-status');statusMessage.textContent='Enter a city.';statusMessage.dataset.state='error';return}await saveAppConfig({...appConfig,weather:{city,state}},'weather-settings-status');if(document.getElementById('weather-settings-status').dataset.state==='success')loadLocalWeather()}
async function saveOllamaSettings(event){event.preventDefault();let scheduleEnabled=document.getElementById('ollama-schedule-enabled').checked,startTime=document.getElementById('ollama-schedule-start').value,endTime=document.getElementById('ollama-schedule-end').value,statusMessage=document.getElementById('ollama-settings-status');if(scheduleEnabled&&(!startTime||!endTime)){statusMessage.textContent='Set both a start and end time.';statusMessage.dataset.state='error';return}await saveAppConfig({...appConfig,ollama:{schedule_enabled:scheduleEnabled,start_time:startTime||'07:00',end_time:endTime||'17:00'}},'ollama-settings-status')}
async function loadConfigEditor(){let statusMessage=document.getElementById('config-status');statusMessage.dataset.state='';statusMessage.textContent='Loading config.json...';try{let response=await fetch('/api/config'),data=await response.json();if(!response.ok)throw new Error(data.error||'config.json could not be loaded');appConfig=data;syncConfigControls();document.getElementById('config-json-editor').value=JSON.stringify(appConfig,null,2);configEditorLoaded=true;statusMessage.textContent='Loaded config.json.'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
async function saveConfigFile(){let statusMessage=document.getElementById('config-status'),config;try{config=JSON.parse(document.getElementById('config-json-editor').value)}catch(error){statusMessage.textContent='Invalid JSON: '+error.message;statusMessage.dataset.state='error';return}await saveAppConfig(config,'config-status')}
async function restartDashboard(){let button=document.getElementById('restart-dashboard-button'),statusMessage=document.getElementById('config-status');button.disabled=true;statusMessage.dataset.state='';statusMessage.textContent='Restarting dashboard...';try{await fetch('/api/restart',{method:'POST'})}catch(error){}let attempts=0;async function waitForDashboard(){try{let response=await fetch('/api/status',{cache:'no-store'});if(response.ok){window.location.reload();return}}catch(error){}attempts++;if(attempts>=40){statusMessage.textContent='Dashboard did not restart. Start it again from the terminal.';statusMessage.dataset.state='error';button.disabled=false;return}setTimeout(waitForDashboard,500)}setTimeout(waitForDashboard,500)}
async function updateApp(){let button=document.getElementById('update-app-button'),statusMessage=document.getElementById('config-status');button.disabled=true;statusMessage.dataset.state='';statusMessage.textContent='Checking repository for updates...';try{let response=await fetch('/api/update',{method:'POST'}),data=await response.json();if(!response.ok)throw new Error(data.error||'App update failed');statusMessage.textContent=data.message||'Update complete.';if(!data.updated){button.disabled=false;return}let attempts=0;async function waitForUpdatedDashboard(){try{let health=await fetch('/api/status',{cache:'no-store'});if(health.ok){window.location.reload();return}}catch(error){}attempts++;if(attempts>=40){statusMessage.textContent='Update installed, but the dashboard did not restart. Start it again from the terminal.';statusMessage.dataset.state='error';button.disabled=false;return}setTimeout(waitForUpdatedDashboard,500)}setTimeout(waitForUpdatedDashboard,700)}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error';button.disabled=false}}
async function saveAutoUpdateSetting(){await saveAppConfig({...appConfig,auto_update:{enabled:document.getElementById('auto-update-enabled').checked}},'config-status')}
async function maybeCheckForUpdates(){if(!appConfig.auto_update||!appConfig.auto_update.enabled)return;try{let response=await fetch('/api/update/check'),data=await response.json();if(!response.ok)return;if(data.update_available&&confirm('An update is available for the MeshCore AI Bot. Update now? The dashboard will restart.'))await updateApp()}catch(error){}}
function loadFavoriteNodes(){try{let saved=JSON.parse(localStorage.getItem('meshcore-favorite-nodes')||'[]');if(Array.isArray(saved))favoriteNodeIds=new Set(saved.map(String))}catch(error){favoriteNodeIds=new Set()}}
function toggleNodeFavorite(id){let normalized=String(id);if(favoriteNodeIds.has(normalized))favoriteNodeIds.delete(normalized);else favoriteNodeIds.add(normalized);localStorage.setItem('meshcore-favorite-nodes',JSON.stringify([...favoriteNodeIds]));renderConversationTargets('node');renderKnownPeers();renderMapMarkers()}
function createNodeFavoriteButton(id){let favorite=document.createElement('button'),isFavorite=favoriteNodeIds.has(String(id));favorite.type='button';favorite.className='favorite-toggle';favorite.textContent=isFavorite?'★':'☆';favorite.title=isFavorite?'Remove from favorites':'Add to favorites';favorite.setAttribute('aria-label',favorite.title);favorite.setAttribute('aria-pressed',String(isFavorite));favorite.onclick=()=>toggleNodeFavorite(id);return favorite}
function fields(){let t=connection_type.value;document.getElementById('ble-field').style.display=t==='bluetooth'?'block':'none';document.getElementById('serial-field').style.display=t==='serial'?'block':'none'}
async function scanDevices(kind){let bluetooth=kind==='bluetooth',select=document.getElementById(bluetooth?'ble_mac':'serial_port'),button=document.getElementById(bluetooth?'ble-scan':'serial-scan'),statusMessage=document.getElementById(bluetooth?'ble-scan-status':'serial-scan-status'),previous=select.value;button.disabled=true;button.textContent='Scanning';statusMessage.dataset.state='';statusMessage.textContent='Searching for available devices...';try{let response=await fetch('/api/scan/'+kind),data=await response.json();if(!response.ok)throw new Error(data.error||'Device scan failed');let devices=bluetooth?data.devices:data.ports;select.replaceChildren(new Option(bluetooth?'Select a Bluetooth device':'Select a serial port',''));for(let device of devices){let label=bluetooth?`${device.name} (${device.address})`:`${device.device} - ${device.description||'Serial port'}`;select.add(new Option(label,bluetooth?device.address:device.device))}if(previous&&[...select.options].some(option=>option.value===previous))select.value=previous;statusMessage.textContent=devices.length?`Found ${devices.length} device(s). Select one to connect.`:'No devices found. Check that the radio is powered and discoverable.'}catch(error){statusMessage.dataset.state='error';statusMessage.textContent=error.message}finally{button.disabled=false;button.textContent='Scan'}}
function scanBluetooth(){return scanDevices('bluetooth')}
function scanSerial(){return scanDevices('serial')}
function updateClock(){document.getElementById('current-datetime').textContent=new Date().toLocaleDateString()+' '+fmtTime(new Date())}
async function status(){let r=await fetch('/api/status'),d=await r.json();let b=document.getElementById('status');b.textContent=d.is_connected?'CONNECTED':'DISCONNECTED';b.className='header-status '+(d.is_connected?'connected':'disconnected');document.getElementById('console').innerText=d.logs.join('\n');handleTraceEvents(d.trace_events||[]);renderOllamaPower(d.ollama_running);handleIncomingNotifications(d)}
let ollamaToggleBusy=false;
function renderOllamaPower(running){let button=document.getElementById('ollama-power-toggle'),status=document.getElementById('ollama-power-status');if(!button||ollamaToggleBusy)return;button.textContent=running?'Turn off':'Turn on';status.textContent=running?'Running. Turn it off between chats to save power.':'Stopped to save power. Replies will fail until it is turned back on.'}
async function toggleOllama(){let button=document.getElementById('ollama-power-toggle'),status=document.getElementById('ollama-power-status'),running=button.textContent.trim()==='Turn off';ollamaToggleBusy=true;button.disabled=true;status.textContent=running?'Stopping Ollama...':'Starting Ollama...';try{let r=await fetch('/api/ollama/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!running})}),d=await r.json();if(!r.ok)throw new Error(d.error||'Could not change Ollama state');ollamaToggleBusy=false;renderOllamaPower(d.ollama_running);loadOllamaModels()}catch(error){ollamaToggleBusy=false;status.textContent=error.message}finally{button.disabled=false}}
let ollamaModelsBusy=false;
async function loadOllamaModels(){let listEl=document.getElementById('installed-models-list');if(!listEl)return;try{let res=await fetch('/api/ollama/models');let data=await res.json();if(!res.ok)throw new Error(data.error||'Failed to load models');let models=data.models||[];let available=data.available_models||[];let currentModel=appConfig.model||data.selected_model;let modelSelect=document.getElementById('model');if(modelSelect){let prevVal=modelSelect.value||currentModel;modelSelect.replaceChildren();for(let name of available)modelSelect.add(new Option(name,name));if(prevVal&&[...modelSelect.options].some(o=>o.value===prevVal))modelSelect.value=prevVal;else if([...modelSelect.options].some(o=>o.value===currentModel))modelSelect.value=currentModel}if(!data.ollama_running){listEl.innerHTML='<p class="map-empty">Ollama server is currently stopped. Turn it on above to view or manage models.</p>';return}if(models.length===0){listEl.innerHTML='<p class="map-empty">No models installed in Ollama yet. Use the download section below to pull a model.</p>';return}listEl.replaceChildren();for(let m of models){let isCurrent=m.name===currentModel;let card=document.createElement('div');card.className='model-card';let info=document.createElement('div');info.className='model-card-info';let name=document.createElement('span');name.className='model-card-name';name.textContent=m.name;info.appendChild(name);if(m.size){let size=document.createElement('span');size.className='model-card-size';size.textContent=m.size;info.appendChild(size)}if(isCurrent){let badge=document.createElement('span');badge.className='model-card-badge';badge.textContent='Active';info.appendChild(badge)}card.appendChild(info);let actions=document.createElement('div');actions.className='model-card-actions';if(!isCurrent){let useBtn=document.createElement('button');useBtn.type='button';useBtn.className='secondary';useBtn.textContent='Use';useBtn.onclick=()=>setActiveModel(m.name);actions.appendChild(useBtn)}let delBtn=document.createElement('button');delBtn.type='button';delBtn.className='delete-btn';delBtn.textContent='Delete';delBtn.onclick=()=>deleteOllamaModel(m.name);actions.appendChild(delBtn);card.appendChild(actions);listEl.appendChild(card)}}catch(err){listEl.replaceChildren();let errP=document.createElement('p');errP.className='map-empty';errP.textContent='Could not load models: '+err.message;listEl.appendChild(errP)}}
async function setActiveModel(modelName){let statusEl=document.getElementById('ollama-model-status');statusEl.dataset.state='';statusEl.textContent='Activating model '+modelName+'...';try{await savePreference('model',modelName);statusEl.dataset.state='success';statusEl.textContent='Active model set to '+modelName+'.';loadOllamaModels()}catch(err){statusEl.dataset.state='error';statusEl.textContent=err.message}}
function setDownloadModel(modelName){let input=document.getElementById('ollama-download-input');if(input){input.value=modelName;input.focus()}}
async function pollOllamaModelProgress(model,stop){let panel=document.getElementById('ollama-download-progress'),phaseEl=document.getElementById('ollama-download-phase'),bar=document.getElementById('ollama-download-progress-bar'),detail=document.getElementById('ollama-download-detail');if(!panel||!phaseEl||!bar||!detail)return;panel.hidden=false;while(!stop.done){try{let res=await fetch('/api/ollama/models/progress'),data=await res.json();if(!res.ok)throw new Error(data.error||'Could not read model progress');let progress=data.progress;if(progress&&progress.model===model){let phase=progress.phase==='installing'?'Installing':progress.phase==='error'?'Download failed':'Downloading';phaseEl.textContent=phase+' "'+model+'": '+(progress.status||phase.toLowerCase());let completed=Number(progress.completed)||0,total=Number(progress.total)||0;if(total>0){bar.max=100;bar.value=progress.phase==='installing'?100:Math.min(100,completed/total*100);detail.textContent=progress.phase==='installing'?'Downloaded '+formatModelProgressSize(completed)+' of '+formatModelProgressSize(total):formatModelProgressSize(completed)+' of '+formatModelProgressSize(total)+' ('+Math.floor(completed/total*100)+'%)'}else if(progress.phase==='installing'){bar.max=100;bar.value=100;detail.textContent='Finishing model installation...'}else{bar.removeAttribute('value');detail.textContent='Waiting for download size...'}}}catch(error){phaseEl.textContent='Download progress unavailable: '+error.message;break}await new Promise(resolve=>setTimeout(resolve,700))}}
function formatModelProgressSize(bytes){if(bytes<1024*1024)return Math.round(bytes/1024)+' KB';return (bytes/(1024*1024*1024)>=1?(bytes/(1024*1024*1024)).toFixed(2)+' GB':(bytes/(1024*1024)).toFixed(0)+' MB')}
async function downloadOllamaModel(){let input=document.getElementById('ollama-download-input'),btn=document.getElementById('ollama-download-button'),statusEl=document.getElementById('ollama-model-status'),progressPanel=document.getElementById('ollama-download-progress'),model=(input?input.value:'').trim();if(!model){statusEl.dataset.state='error';statusEl.textContent='Enter a model name to download.';return}ollamaModelsBusy=true;if(btn)btn.disabled=true;if(input)input.disabled=true;statusEl.dataset.state='';statusEl.textContent='Starting download of "'+model+'"...';if(progressPanel)progressPanel.hidden=false;let stop={done:false},progressPoll=pollOllamaModelProgress(model,stop);try{let res=await fetch('/api/ollama/models/pull',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})}),data=await res.json();if(!res.ok)throw new Error(data.error||'Failed to download model');statusEl.dataset.state='success';statusEl.textContent='Model "'+model+'" downloaded and installed successfully!';if(input)input.value='';await loadOllamaModels()}catch(err){statusEl.dataset.state='error';statusEl.textContent=err.message}finally{stop.done=true;await progressPoll;ollamaModelsBusy=false;if(btn)btn.disabled=false;if(input)input.disabled=false}}
async function deleteOllamaModel(modelName){if(!window.confirm('Delete model "'+modelName+'"? This will permanently remove its files from disk.'))return;let statusEl=document.getElementById('ollama-model-status');statusEl.dataset.state='';statusEl.textContent='Deleting "'+modelName+'"...';try{let res=await fetch('/api/ollama/models/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:modelName})}),data=await res.json();if(!res.ok)throw new Error(data.error||'Failed to delete model');statusEl.dataset.state='success';statusEl.textContent='Model "'+modelName+'" deleted successfully.';if(data.selected_model&&data.selected_model!==appConfig.model){appConfig.model=data.selected_model;syncConfigControls()}await loadOllamaModels()}catch(err){statusEl.dataset.state='error';statusEl.textContent=err.message}}
let notificationsInitialized=false,lastIncomingCount=0,notificationAudio=null;
function notificationsEnabled(){try{return localStorage.getItem('meshcore-message-notifications')!=='off'}catch(error){return true}}
function playNotificationSound(){try{notificationAudio=notificationAudio||new(window.AudioContext||window.webkitAudioContext)();let ctx=notificationAudio;if(ctx.state==='suspended')ctx.resume();let now=ctx.currentTime;[880,1175].forEach((freq,i)=>{let osc=ctx.createOscillator(),gain=ctx.createGain();osc.type='sine';osc.frequency.value=freq;gain.gain.setValueAtTime(0.0001,now+i*0.15);gain.gain.exponentialRampToValueAtTime(0.2,now+i*0.15+0.02);gain.gain.exponentialRampToValueAtTime(0.0001,now+i*0.15+0.14);osc.connect(gain);gain.connect(ctx.destination);osc.start(now+i*0.15);osc.stop(now+i*0.15+0.15)})}catch(error){}}
function handleIncomingNotifications(d){let count=Number(d.incoming_message_count||0);if(!notificationsInitialized){lastIncomingCount=count;notificationsInitialized=true;return}if(count>lastIncomingCount){lastIncomingCount=count;if(notificationsEnabled()){playNotificationSound();let m=d.latest_incoming_message;if(m)showToast(m.type==='channel'?(m.sender?m.sender+' \u00b7 ':'')+'#'+(m.channel||m.target):(m.sender||'New direct message'),m.text,undefined,()=>openIncomingConversation(m));if(m&&'Notification'in window&&Notification.permission==='granted'&&document.hidden){try{let n=new Notification('New MeshCore message',{body:m.text});n.onclick=()=>{window.focus();openIncomingConversation(m);n.close()}}catch(error){}}}}else if(count<lastIncomingCount){lastIncomingCount=count}}
let serverAddresses=[];
function hostUrl(port,path){let host=location.hostname;if(['localhost','127.0.0.1','::1','[::1]'].includes(host)&&serverAddresses.length)host=serverAddresses[0];return 'https://'+host+':'+port+path}
document.addEventListener('DOMContentLoaded',()=>document.querySelectorAll('.live-host-url').forEach(el=>el.textContent=hostUrl(el.dataset.port,el.dataset.path)));
function fmtTime(value){if(value instanceof Date){let h=value.getHours(),m=String(value.getMinutes()).padStart(2,'0'),sec=String(value.getSeconds()).padStart(2,'0');return String(h%12||12).padStart(2,'0')+':'+m+':'+sec+' '+(h<12?'AM':'PM')}let match=/^(\d{1,2}):(\d{2}):(\d{2})$/.exec(String(value||''));if(!match)return value||'';let h=Number(match[1]);return String(h%12||12).padStart(2,'0')+':'+match[2]+':'+match[3]+' '+(h<12?'AM':'PM')}
function openIncomingConversation(m){let isChannel=m.type==='channel';showView(isChannel?'channels':'nodes');selectConversation(isChannel?'channel':'node',String(m.target))}
function showToast(title,body,duration=7000,onClick=null){let stack=document.getElementById('toast-stack');if(!stack){stack=document.createElement('div');stack.id='toast-stack';stack.setAttribute('aria-live','polite');document.body.append(stack)}let toast=document.createElement('div'),heading=document.createElement('strong'),text=document.createElement('span');toast.className='toast';heading.textContent=title;text.textContent=body;toast.append(heading,text);if(onClick){toast.classList.add('clickable');toast.setAttribute('role','button');toast.tabIndex=0;toast.onclick=()=>{onClick();toast.remove()};toast.onkeydown=event=>{if(event.key==='Enter')toast.click()}}stack.append(toast);while(stack.children.length>3)stack.firstChild.remove();requestAnimationFrame(()=>toast.classList.add('show'));setTimeout(()=>{toast.classList.remove('show');setTimeout(()=>toast.remove(),300)},duration)}
function syncNotificationControl(){let box=document.getElementById('notification-toggle');if(box)box.checked=notificationsEnabled()}
function saveNotificationSetting(){let enabled=document.getElementById('notification-toggle').checked;try{localStorage.setItem('meshcore-message-notifications',enabled?'on':'off')}catch(error){}if(enabled){playNotificationSound();if('Notification'in window&&Notification.permission==='default')Notification.requestPermission()}}
const NOISE_MAX_POINTS=90;
let noiseSamples=[],noiseLoading=false;
function drawNoiseScope(){let canvas=document.getElementById('noise-floor-canvas');if(!canvas)return;let w=canvas.clientWidth,h=canvas.clientHeight;if(!w||!h)return;let dpr=window.devicePixelRatio||1;if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr)}let ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);let style=getComputedStyle(document.body),accent=style.getPropertyValue('--accent').trim()||'#00d8ff',muted=style.getPropertyValue('--border').trim()||'#244';ctx.strokeStyle=muted;ctx.lineWidth=1;ctx.globalAlpha=.6;for(let i=1;i<3;i++){let y=Math.round(h*i/3)+.5;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}ctx.globalAlpha=1;if(noiseSamples.length<2)return;let values=noiseSamples.map(sample=>sample.v),lo=Math.min(...values),hi=Math.max(...values),span=Math.max(10,hi-lo),mid=(hi+lo)/2;lo=mid-span/2;hi=mid+span/2;let x=i=>w-(noiseSamples.length-1-i)*(w/(NOISE_MAX_POINTS-1)),y=v=>h-2-(v-lo)/(hi-lo)*(h-4);ctx.beginPath();noiseSamples.forEach((sample,i)=>{i?ctx.lineTo(x(i),y(sample.v)):ctx.moveTo(x(i),y(sample.v))});ctx.strokeStyle=accent;ctx.lineWidth=1.5;ctx.shadowColor=accent;ctx.shadowBlur=4;ctx.stroke();ctx.shadowBlur=0}
async function pollNoiseFloor(){let label=document.getElementById('noise-floor-value');if(!label||noiseLoading||document.hidden)return;noiseLoading=true;try{let response=await fetch('/api/noise-floor'),data=await response.json();if(!response.ok||typeof data.noise_floor!=='number'){label.textContent=data.connected===false?'offline':'-- dBm';return}if(!noiseSamples.length||noiseSamples[noiseSamples.length-1].t!==data.time){noiseSamples.push({t:data.time,v:data.noise_floor});if(noiseSamples.length>NOISE_MAX_POINTS)noiseSamples.shift()}label.textContent=data.noise_floor+' dBm';drawNoiseScope()}catch(error){label.textContent='-- dBm'}finally{noiseLoading=false}}
let cpuTempLoading=false;
async function pollCpuTemp(){let label=document.getElementById('cpu-temp-value');if(!label||cpuTempLoading||document.hidden)return;cpuTempLoading=true;try{let response=await fetch('/api/cpu-temp'),data=await response.json();label.textContent=response.ok&&typeof data.temperature_f==='number'?data.temperature_f+'°F':'--°F'}catch(error){label.textContent='--°F'}finally{cpuTempLoading=false}}
window.addEventListener('resize',drawNoiseScope);
function handleTraceEvents(events){if(!traceEventsInitialized){for(let event of events)addTraceActivity(event);lastSeenTraceEventId=events.length?Number(events[events.length-1].id):0;traceEventsInitialized=true;return}for(let event of events){let eventId=Number(event.id);if(eventId<=lastSeenTraceEventId)continue;addTraceActivity(event);if(event.kind==='direct')pulseTrace(event.target_id,event.direction);lastSeenTraceEventId=eventId}}
function addTraceActivity(event){let list=document.getElementById('live-trace-feed-list');if(list){document.getElementById('live-trace-feed-empty')?.remove();let target=event.target_name||event.target_id||'Unknown';let label=event.kind==='direct'?(event.direction==='inbound'?'Direct message from ':'Direct message to ')+target:(event.direction==='inbound'?'Message received on ':'Message sent to ')+'Channel '+target;let entry=document.createElement('div');entry.className='live-trace-feed-item';let timestamp=document.createElement('time');timestamp.textContent=fmtTime(event.timestamp)||'';let body=document.createElement('div');body.textContent=label;entry.append(timestamp,body);list.prepend(entry);while(list.children.length>60)list.lastElementChild.remove()}addAnalyzerPacket(event)}
function addAnalyzerPacket(event){let list=document.getElementById('analyzer-packet-list');if(!list)return;let empty=list.querySelector('.packet-empty');empty?.parentElement.remove();let row=document.createElement('tr');row.tabIndex=0;let direction=event.direction==='inbound'?'IN':'OUT';let transport=event.kind==='direct'?'DIRECT':'CHANNEL';let status=event.direction==='inbound'?'RECEIVED':'SENT';let values=[fmtTime(event.timestamp)||'--',direction,transport,event.target_name||event.target_id||'Unknown',status];values.forEach((value,index)=>{let cell=document.createElement('td');cell.textContent=String(value);if(index===1)cell.className='packet-direction';if(index===2&&transport==='CHANNEL')cell.className='packet-channel';row.appendChild(cell)});row.onclick=()=>showAnalyzerEvent(event);row.onkeydown=key=>{if(key.key==='Enter'||key.key===' '){key.preventDefault();showAnalyzerEvent(event)}};list.prepend(row);while(list.children.length>60)list.lastElementChild.remove();analyzerEventCount=Math.min(analyzerEventCount+1,60);document.getElementById('analyzer-total').textContent=String(analyzerEventCount)}
function showAnalyzerEvent(event){let detail=document.getElementById('analyzer-event-detail');if(!detail)return;detail.textContent=[event.target_name||event.target_id||'Unknown','Event '+(event.id||'--'),event.direction==='inbound'?'Received by gateway':'Sent by gateway','Transport: '+(event.kind==='direct'?'Direct message':'Channel message'),'Time: '+(fmtTime(event.timestamp)||'--')].join('\n')}
let analyzerEventCount=0;
function updateNavigationBadges(){for(let [badgeId,countId] of [['map-node-count','map-node-count'],['analyzer-count','analyzer-total']]){let badge=document.getElementById(badgeId),count=Number(document.getElementById(countId)?.textContent||0);if(badge){if(badgeId!==countId)badge.textContent=String(count);badge.hidden=count<=0}}}
window.addEventListener('DOMContentLoaded',()=>{let observer=new MutationObserver(updateNavigationBadges);for(let id of ['map-node-count','analyzer-total']){let count=document.getElementById(id);if(count)observer.observe(count,{childList:true,characterData:true,subtree:true})}updateNavigationBadges()});
let mapNodes=[];
let dashboardMap=null;
let mapMarkers=null;
let mapBoundsSignature='';
let liveTraceMap=null;
let liveTraceMarkers=null;
let liveTraceMarkerById=new Map();
let lastSeenTraceEventId=0;
let traceEventsInitialized=false;
function hashColor(id){let str=String(id),hash=0;for(let i=0;i<str.length;i++){hash=(hash*31+str.charCodeAt(i))|0}return 'hsl('+(Math.abs(hash)%360)+',65%,50%)'}
function showView(view){let target=document.getElementById(view+'-view');if(!target)return;let overviewViews=['nodes','channels','map','analyzer'];if(document.body.dataset.theme==='tron'&&!(window.isMobileDevice&&window.isMobileDevice())&&overviewViews.includes(view)){activeView='tron-overview';document.body.classList.add('tron-overview');document.querySelectorAll('.view-panel').forEach(panel=>panel.hidden=!overviewViews.includes(panel.id.replace(/-view$/,'')));document.querySelectorAll('.nav-tab').forEach(tab=>tab.setAttribute('aria-pressed',String(tab.dataset.view===view)));openMap();renderAnalyzerStats();loadTronAnalyzerRadioStatus();return}document.body.classList.remove('tron-overview');activeView=view;document.querySelectorAll('.view-panel').forEach(panel=>panel.hidden=panel!==target);document.querySelectorAll('.nav-tab').forEach(tab=>tab.setAttribute('aria-pressed',String(tab.dataset.view===view)));if(view==='map')openMap();if(view==='live-trace')openLiveTrace();if(view==='analyzer'){renderAnalyzerStats();loadAnalyzerRadioStatus()}if(view==='device-settings'&&!deviceSettingsLoaded)loadDeviceSettings()}
const MAP_STYLES={standard:['/tiles/osm/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}],dark:['/tiles/osm/{z}/{x}/{y}.png',{maxZoom:19,className:'map-tiles-dark',attribution:'&copy; OpenStreetMap contributors'}],terrain:['/tiles/topo/{z}/{x}/{y}.png',{maxZoom:17,attribution:'&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap (CC-BY-SA)'}]};
let mapTileLayer=null;
function applyMapStyle(){let select=document.getElementById('map-style-select');if(!select||!dashboardMap)return;let key=MAP_STYLES[select.value]?select.value:'standard';try{localStorage.setItem('meshcore-map-style',key)}catch(e){}if(mapTileLayer)dashboardMap.removeLayer(mapTileLayer);mapTileLayer=L.tileLayer(MAP_STYLES[key][0],MAP_STYLES[key][1]).addTo(dashboardMap);mapTileLayer.bringToBack()}
function initMapStyle(){let select=document.getElementById('map-style-select');if(!select)return;try{let saved=localStorage.getItem('meshcore-map-style');if(MAP_STYLES[saved])select.value=saved}catch(e){}}
function openMap(){initMapStyle();if(!window.L){document.getElementById('map-message').textContent='Map library unavailable. Check your internet connection and reload.';return}if(!dashboardMap){dashboardMap=L.map('map-canvas',{zoomControl:true}).setView([20,0],2);mapMarkers=L.layerGroup().addTo(dashboardMap);applyMapStyle()}setTimeout(()=>dashboardMap.invalidateSize(),80);renderMapMarkers()}
function openLiveTrace(){if(!window.L)return;if(!liveTraceMap){liveTraceMap=L.map('live-trace-canvas',{zoomControl:true}).setView([20,0],2);L.tileLayer('/tiles/osm/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(liveTraceMap);liveTraceMarkers=L.layerGroup().addTo(liveTraceMap)}setTimeout(()=>liveTraceMap.invalidateSize(),80);renderLiveTraceMarkers()}
function renderLiveTraceMarkers(){if(!liveTraceMap||!liveTraceMarkers)return;liveTraceMarkers.clearLayers();liveTraceMarkerById=new Map();let bounds=[],located=0;for(let peer of mapNodes){if(!Number.isFinite(peer.latitude)||!Number.isFinite(peer.longitude))continue;let point=[peer.latitude,peer.longitude],marker=L.marker(point,{icon:peerMarkerIcon(peer),title:peer.name}).bindPopup(peerPopupContent(peer)).addTo(liveTraceMarkers);liveTraceMarkerById.set(String(peer.id),{marker,point});bounds.push(point);located++}if(Number.isFinite(gatewayTelemetry.latitude)&&Number.isFinite(gatewayTelemetry.longitude)){let point=[gatewayTelemetry.latitude,gatewayTelemetry.longitude];L.circleMarker(point,{radius:9,color:'#0d1117',weight:2,fillColor:'#36d1dc',fillOpacity:1}).bindPopup(popupContent('This gateway','Current radio location')).addTo(liveTraceMarkers);liveTraceMarkerById.set('gateway',{marker:null,point});bounds.push(point);located++}if(bounds.length)liveTraceMap.fitBounds(bounds,{padding:[36,36],maxZoom:12});let countLabel=document.getElementById('live-trace-count');if(countLabel)countLabel.textContent=String(located)}
function pulseTrace(nodeId,direction){if(!liveTraceMap)return;let id=String(nodeId),target=liveTraceMarkerById.get(id);if(!target){let match=[...liveTraceMarkerById.entries()].find(([peerId])=>peerId!=='gateway'&&(peerId.startsWith(id)||id.startsWith(peerId)));if(match)target=match[1]}let gateway=liveTraceMarkerById.get('gateway');if(!target)return;if(target.marker){let element=target.marker.getElement();if(element){element.classList.remove('trace-pulse-marker');void element.offsetWidth;element.classList.add('trace-pulse-marker')}}if(!gateway)return;let points=direction==='inbound'?[target.point,gateway.point]:[gateway.point,target.point];let line=L.polyline(points,{color:'#4ade80',weight:2,opacity:.85,dashArray:'4 6'}).addTo(liveTraceMap);let dot=L.circleMarker(points[0],{radius:5,color:'#4ade80',weight:1,fillColor:'#4ade80',fillOpacity:1,className:'trace-pulse-dot'}).addTo(liveTraceMap);let start=performance.now(),duration=900;function animate(now){let t=Math.min(1,(now-start)/duration),lat=points[0][0]+(points[1][0]-points[0][0])*t,lng=points[0][1]+(points[1][1]-points[0][1])*t;dot.setLatLng([lat,lng]);if(t<1)requestAnimationFrame(animate);else setTimeout(()=>{liveTraceMap.removeLayer(line);liveTraceMap.removeLayer(dot)},400)}requestAnimationFrame(animate)}
function peerTypeLabel(type){return ({1:'User',2:'Repeater',3:'Room server',4:'Sensor'})[Number(type)]||'Unknown'}
function peerSummary(peer){if(!peer)return 'No peer selected';return peerTypeLabel(peer.type)}
function peerPopupContent(peer){return popupContent(peer.name,peerSummary(peer))}
function popupContent(title,detail){let content=document.createElement('div');let heading=document.createElement('strong');heading.textContent=title;content.appendChild(heading);if(detail){let line=document.createElement('div');line.textContent=detail;content.appendChild(line)}return content}
function focusMapPoint(latitude,longitude){showView('map');if(dashboardMap&&Number.isFinite(latitude)&&Number.isFinite(longitude))dashboardMap.setView([latitude,longitude],12)}
function appendPeerDetail(container,label,value){if(value===null||value===undefined||value==='')return;let field=document.createElement('div'),term=document.createElement('dt'),description=document.createElement('dd');term.textContent=label;description.textContent=value;field.append(term,description);container.appendChild(field)}
function usUnitTelemetry(type,value){let n=Number(value);if(value===null||value===''||!Number.isFinite(n))return value;let t=String(type).toLowerCase();if(t.includes('temp'))return (n*9/5+32).toFixed(1)+'°F';if(t==='altitude'||t==='distance')return (n*3.28084).toFixed(0)+' ft';if(t==='barometer'||t.includes('pressure'))return (n*0.02953).toFixed(2)+' inHg';if(t.includes('wind')||t==='speed')return (n*2.23694).toFixed(1)+' mph';return value}
async function renderPeerDetails(nodeId,container){if(!container)return;container.hidden=false;let knownPeer=mapNodes.find(peer=>String(peer.id)===String(nodeId))||{},appendContactDetails=node=>{let rawHops=node.hops,hops=Number(rawHops);appendPeerDetail(container,'Type',peerTypeLabel(node.type));appendPeerDetail(container,'Last heard',Number(node.last_heard)>0?(d=>d.toLocaleDateString()+' '+fmtTime(d))(new Date(Number(node.last_heard)*1000)):'Not available');appendPeerDetail(container,'# of hops',rawHops!==null&&rawHops!==undefined&&rawHops!==''&&Number.isInteger(hops)&&hops>=0?hops:'Unavailable')};container.replaceChildren();appendContactDetails(knownPeer);appendPeerDetail(container,'Telemetry','Requesting telemetry...');try{let response=await fetch('/api/peer-telemetry?node_id='+encodeURIComponent(nodeId)),data=await response.json();if(!response.ok)throw new Error(data.error||'Peer details could not be loaded');container.replaceChildren();appendContactDetails(data.node||knownPeer);let telemetry=data.telemetry||[];for(let item of telemetry){let label=String(item.type||'Telemetry');let value=item.value;if(value&&typeof value==='object')value=Object.entries(value).map(([key,entry])=>key+': '+entry).join(', ');else value=usUnitTelemetry(label,value);if(value!==null&&value!==undefined)appendPeerDetail(container,label,value)}if(!telemetry.length)appendPeerDetail(container,'Telemetry','No telemetry has been reported by this peer.')}catch(error){appendPeerDetail(container,'Telemetry error',error.message)}}
const peerMarkerSvgs={users:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 21a7 7 0 0 1 14 0"/></svg>',repeaters:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20V8M8 20h8M6 11a8 8 0 0 1 12 0M3 8a12 12 0 0 1 18 0"/><circle cx="12" cy="5" r="1"/></svg>','room-servers':'<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="7" rx="1.5"/><rect x="4" y="13" width="16" height="7" rx="1.5"/><path d="M8 7.5h.01M8 16.5h.01M12 7.5h5M12 16.5h5"/></svg>',sensors:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 14.76V5a3 3 0 0 0-6 0v9.76a5 5 0 1 0 6 0Z"/><path d="M11 11v6"/></svg>',unknown:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/></svg>'};
function peerMarkerIcon(peer){let category=nodeCategory(peer);return L.divIcon({className:'',html:`<span class="map-peer-icon map-peer-icon-${category}" aria-label="${category}">${peerMarkerSvgs[category]||peerMarkerSvgs.unknown}</span>`,iconSize:[32,32],iconAnchor:[16,16]})}
function renderMapMarkers(){if(!dashboardMap||!mapMarkers)return;mapMarkers.clearLayers();let bounds=[];for(let peer of sortedFilteredNodes('map-node-sort','map-node-type-filter','map-node-search')){if(!Number.isFinite(peer.latitude)||!Number.isFinite(peer.longitude))continue;let point=[peer.latitude,peer.longitude],marker=L.marker(point,{icon:peerMarkerIcon(peer),title:peer.name+' ('+nodeCategory(peer)+')'}).bindPopup(peerPopupContent(peer)).addTo(mapMarkers);marker.on('click',()=>expandKnownPeerRow(peer.id));bounds.push(point)}if(Number.isFinite(gatewayTelemetry.latitude)&&Number.isFinite(gatewayTelemetry.longitude)){let point=[gatewayTelemetry.latitude,gatewayTelemetry.longitude];L.circleMarker(point,{radius:9,color:'#0d1117',weight:2,fillColor:'#36d1dc',fillOpacity:1}).bindPopup(popupContent('This gateway','Current radio location')).addTo(mapMarkers);bounds.push(point)}let signature=JSON.stringify(bounds);if(bounds.length&&signature!==mapBoundsSignature){dashboardMap.fitBounds(bounds,{padding:[36,36],maxZoom:12});mapBoundsSignature=signature}else if(!bounds.length){mapBoundsSignature=''}document.getElementById('map-message').hidden=bounds.length>0;document.getElementById('map-message').textContent='No peer or gateway location data is available yet.'}
function nodeCategory(node){let type=String(node.type??'').toLowerCase();if(type==='1'||type==='client')return 'users';if(type==='2'||type==='repeater')return 'repeaters';if(type==='3'||type==='room server'||type==='room_server')return 'room-servers';if(type==='4'||type==='sensor')return 'sensors';return 'unknown'}
function sortedFilteredNodes(sortId='node-sort',typeId='node-type-filter',searchId='node-search'){let category=document.getElementById(typeId).value,query=document.getElementById(searchId)?.value.trim().toLowerCase()||'',nodes=mapNodes.filter(node=>(category==='all'||(category==='favorites'?favoriteNodeIds.has(String(node.id)):nodeCategory(node)===category))&&(!query||String(node.name||'').toLowerCase().includes(query)||String(node.id).toLowerCase().includes(query))),sort=document.getElementById(sortId).value;nodes.sort((left,right)=>{if(sort==='heard')return Number(right.last_heard||0)-Number(left.last_heard||0)||left.name.localeCompare(right.name);if(sort==='messages')return Number(right.last_message_at||0)-Number(left.last_message_at||0)||left.name.localeCompare(right.name);return left.name.localeCompare(right.name)})
return nodes}
function updateMapPeerFilters(){renderKnownPeers();renderMapMarkers()}
function expandKnownPeerRow(peerId){let row=document.querySelector('#map-node-list .conversation-entry[data-peer-id="'+String(peerId).replace(/"/g,'')+'"]');if(!row)return;let overlay=document.getElementById('map-search-overlay');if(overlay)overlay.classList.add('open');let target=row.querySelector('.map-peer-target');if(target)target.click()}
function isRepeaterNode(nodeId){let peer=mapNodes.find(item=>String(item.id)===String(nodeId));return Number(peer?.type)===2}
function renderKnownPeers(){let list=document.getElementById('map-node-list');if(!list)return;let peers=sortedFilteredNodes('map-node-sort','map-node-type-filter','map-node-search'),count=document.getElementById('map-peer-total'),summary=document.getElementById('map-node-summary'),badge=document.getElementById('map-node-count');if(count)count.textContent=String(peers.length);if(summary)summary.textContent=peers.length+' of '+mapNodes.length+' peers';if(badge){badge.textContent=String(peers.length);badge.hidden=peers.length===0}list.replaceChildren();if(!peers.length){let empty=document.createElement('p');empty.className='map-empty';empty.textContent=mapNodes.length?'No peers match this filter.':'No nodes found. Connect to a MeshCore radio to load contacts.';list.appendChild(empty);return}for(let peer of peers){let row=document.createElement('div');row.className='conversation-entry';row.dataset.peerId=String(peer.id);let target=document.createElement('button');target.type='button';target.className='conversation-target map-peer-target';target.title=Number.isFinite(peer.latitude)&&Number.isFinite(peer.longitude)?'Center map and view peer details':'View peer details';let avatar=document.createElement('span');avatar.className='conversation-avatar';avatar.style.background=hashColor(peer.id);avatar.textContent=String(peer.name||'?').trim().charAt(0)||'?';let name=document.createElement('strong');name.textContent=peer.name;target.append(avatar,name);let detailBox=document.createElement('dl');detailBox.className='peer-inline-detail rail-peer-detail';detailBox.hidden=true;detailBox.style.gridColumn='1 / -1';target.onclick=()=>{if(Number.isFinite(peer.latitude)&&Number.isFinite(peer.longitude))focusMapPoint(peer.latitude,peer.longitude);let willOpen=detailBox.hidden;document.querySelectorAll('#map-node-list .peer-inline-detail').forEach(el=>{if(el!==detailBox)el.hidden=true});if(willOpen)renderPeerDetails(peer.id,detailBox);else detailBox.hidden=true};row.append(target,createNodeFavoriteButton(peer.id),detailBox);list.appendChild(row)}}
function updateConversationMenu(type,metadata={}){let prefix=type==='node'?'node':'channel',clear=document.getElementById(prefix+'-clear-action'),archive=document.getElementById(prefix+'-archive-action'),pin=document.getElementById(prefix+'-pin-action'),enabled=Boolean(type==='node'?selectedNodeId:selectedChannelId);clear.disabled=!enabled;archive.disabled=!enabled;pin.disabled=!enabled;document.getElementById(prefix+'-delete-action').disabled=!enabled;archive.textContent=metadata.archived?'Unarchive chat':'Archive chat';pin.textContent=metadata.pinned?'Unpin chat':'Pin chat'}
function renderConversationTargets(type){let isNode=type==='node',filter=document.getElementById(isNode?'node-chat-filter':'channel-chat-filter').value,query=isNode?'':document.getElementById('channel-search').value.trim().toLowerCase(),items=[...(isNode?sortedFilteredNodes():meshChannels)].filter(item=>(!query||String(item.name||'').toLowerCase().includes(query)||String(item.id).toLowerCase().includes(query))&&(filter==='all'||(filter==='archived'?Boolean(item.archived):!item.archived))),list=document.getElementById(isNode?'node-target-list':'channel-target-list'),selected=isNode?selectedNodeId:selectedChannelId;items.sort((left,right)=>Number(Boolean(left.archived))-Number(Boolean(right.archived))||Number(Boolean(right.pinned))-Number(Boolean(left.pinned)));list.replaceChildren();if(!items.length){let empty=document.createElement('p');empty.className='map-empty';empty.textContent=filter==='archived'?'No archived chats.':(isNode?(mapNodes.length?'No nodes match this filter.':'No nodes found. Connect to a MeshCore radio to load contacts.'):(query?'No channels match this search.':'No channels found on this device.'));list.appendChild(empty);return}for(let item of items){let button=document.createElement('button');button.type='button';button.className='conversation-target'+(selected===String(item.id)?' active':'');button.onclick=()=>selectConversation(type,item.id);let avatar=document.createElement('span');avatar.className='conversation-avatar';avatar.style.background=hashColor(item.id);avatar.textContent=String(item.name||'?').trim().charAt(0)||'?';let title=document.createElement('strong');title.textContent=item.name;let detail=document.createElement('small');detail.textContent=[item.pinned?'Pinned':'',item.archived?'Archived':''].filter(Boolean).join(' · ');button.append(avatar,title);if(detail.textContent)button.append(detail);if(isNode){let entry=document.createElement('div');entry.className='conversation-entry';entry.addEventListener('contextmenu',event=>{event.preventDefault();window.openNodeActions(item)});entry.append(button,createNodeFavoriteButton(item.id));list.appendChild(entry);}else list.appendChild(button)}}
function selectConversation(type,id){let normalized=String(id);if(type==='node'){let peer=mapNodes.find(item=>String(item.id)===normalized)||{id:normalized},repeater=Number(peer.type)===2,form=document.querySelector('#nodes-view .chat-panel form'),input=document.getElementById('node-message');selectedNodeId=normalized;document.getElementById('node-chat-title').textContent=peer.name||normalized;document.getElementById('node-chat-detail').textContent=repeater?'Direct messages are not supported for repeater nodes.':peerSummary(peer);document.getElementById('node-send').disabled=repeater;input.disabled=repeater;form.hidden=repeater;updateConversationMenu('node',peer);renderConversationTargets('node');history('node',normalized,'node-chat-history',true)}else{let channel=meshChannels.find(item=>String(item.id)===normalized)||{id:normalized};selectedChannelId=normalized;document.getElementById('channel-chat-title').textContent=channel.name||'Channel '+normalized;document.getElementById('channel-chat-detail').textContent='Channel '+normalized;document.getElementById('channel-send').disabled=false;updateConversationMenu('channel',channel);renderConversationTargets('channel');history('channel',normalized,'channel-chat-history',true)}}
let incomingAdvertEvents=[];
let incomingAdvertSnapshot=new Map();
let incomingAdvertsInitialized=false;
let incomingAdvertSignature='';
function renderAnalyzerStats(){let located=mapNodes.filter(peer=>Number.isFinite(peer.latitude)&&Number.isFinite(peer.longitude)).length;document.getElementById('analyzer-nodes').textContent=String(mapNodes.length);document.getElementById('analyzer-channels').textContent=String(meshChannels.length);document.getElementById('analyzer-located').textContent=String(located)}
function advertAgeLabel(timestamp){let advertTime=Number(timestamp);if(!Number.isFinite(advertTime)||advertTime<=0)return 'Time unavailable';let elapsed=Math.max(0,Math.floor(Date.now()/1000-advertTime));if(elapsed<60)return elapsed+'s ago';if(elapsed<3600)return Math.floor(elapsed/60)+'m ago';if(elapsed<86400)return Math.floor(elapsed/3600)+'h ago';return Math.floor(elapsed/86400)+'d ago'}
function renderIncomingAdverts(){let list=document.getElementById('incoming-adverts-list'),count=document.getElementById('incoming-adverts-count');if(!list)return;let receivedNewAdvert=false;for(let peer of mapNodes){let timestamp=Number(peer.last_heard);if(!Number.isFinite(timestamp)||timestamp<=0)continue;let id=String(peer.id),previous=incomingAdvertSnapshot.get(id);if(previous===undefined||timestamp>previous){if(incomingAdvertsInitialized)receivedNewAdvert=true;incomingAdvertSnapshot.set(id,timestamp);incomingAdvertEvents.unshift({id,name:peer.name||'Unknown node',last_heard:timestamp,hops:peer.hops})}else{let existing=incomingAdvertEvents.find(event=>event.id===id&&event.last_heard===timestamp);if(existing){existing.name=peer.name||existing.name;existing.hops=peer.hops}}}incomingAdvertsInitialized=true;incomingAdvertEvents.sort((left,right)=>left.last_heard-right.last_heard);incomingAdvertEvents=incomingAdvertEvents.slice(-5);if(count)count.textContent=String(incomingAdvertEvents.length);let signature=incomingAdvertEvents.map(e=>[e.id,e.last_heard,e.name,e.hops].join('|')).join(';');if(!incomingAdvertEvents.length){list.replaceChildren();incomingAdvertSignature='';let empty=document.createElement('p');empty.className='incoming-advert-empty';empty.textContent=document.getElementById('status')?.classList.contains('connected')?'Waiting for incoming adverts...':'Connect to a radio to receive adverts.';list.appendChild(empty);return}let describe=advert=>{let hopCount=advert.hops===null||advert.hops===undefined?null:Number(advert.hops),route=hopCount===-1?'? hops':(Number.isInteger(hopCount)&&hopCount>=0?hopCount+' hop'+(hopCount===1?'':'s'):'Hops unavailable');return advertAgeLabel(advert.last_heard)+' · '+route};if(signature===incomingAdvertSignature){list.querySelectorAll('.incoming-advert-meta').forEach(meta=>{let advert=incomingAdvertEvents[Number(meta.dataset.index)];if(advert)meta.textContent=describe(advert)});return}incomingAdvertSignature=signature;let previousScroll=list.scrollTop;list.replaceChildren();let track=document.createElement('div');track.className='incoming-advert-track';let buildGroup=()=>{let group=document.createElement('div');group.className='incoming-advert-group';for(let index=0;index<incomingAdvertEvents.length;index++){let advert=incomingAdvertEvents[index],row=document.createElement('div'),name=document.createElement('span'),meta=document.createElement('span');row.className='incoming-advert-row'+(receivedNewAdvert&&index===incomingAdvertEvents.length-1?' incoming-advert-new':'');name.className='incoming-advert-name';name.textContent=advert.name;meta.className='incoming-advert-meta';meta.dataset.index=String(index);meta.textContent=describe(advert);row.append(name,meta);group.appendChild(row)}return group};track.appendChild(buildGroup());list.appendChild(track);list.scrollTop=receivedNewAdvert?previousScroll:list.scrollHeight;if(receivedNewAdvert)list.scrollTo({top:list.scrollHeight,behavior:'smooth'})}
let analyzerRadioStatusLoading=false;
async function loadAnalyzerRadioStatus(){let panel=document.getElementById('analyzer-radio-status');if(!panel||!['analyzer','tron-overview'].includes(activeView)||analyzerRadioStatusLoading)return;analyzerRadioStatusLoading=true;panel.textContent='Reading live radio status...';let groups=[['Radio','radio'],['Core','core'],['Packets','packets']],labels={noise_floor:['Noise floor','dBm'],last_rssi:['Last RSSI','dBm'],last_snr:['Last SNR','dB'],tx_air_secs:['TX airtime','s'],rx_air_secs:['RX airtime','s'],battery_mv:['Battery voltage','mV'],uptime_secs:['Uptime','s'],errors:['Errors',''],queue_len:['Transmit queue',''],recv:['Received packets',''],sent:['Sent packets',''],flood_tx:['Flood TX',''],direct_tx:['Direct TX',''],flood_rx:['Flood RX',''],direct_rx:['Direct RX',''],recv_errors:['Receive errors','']};let content=document.createDocumentFragment();try{for(let [title,statsType] of groups){let response;try{response=await fetch('/api/device-settings/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'stats',stats_type:statsType})});let data=await response.json();if(!response.ok){if(response.status===503){panel.textContent='Connect to a MeshCore radio to view live statistics.';return}throw new Error(data.error||'Statistics unavailable')}let values=data.result&&typeof data.result==='object'&&!Array.isArray(data.result)?data.result:{};let section=document.createElement('section'),heading=document.createElement('h4'),list=document.createElement('dl');section.className='analyzer-radio-group';heading.textContent=title;list.className='analyzer-radio-list';for(let [key,value] of Object.entries(values)){let [label,unit]=labels[key]||[key.replaceAll('_',' ').replace(/\b\w/g,char=>char.toUpperCase()),''];let term=document.createElement('dt'),detail=document.createElement('dd');term.textContent=label;detail.textContent=value===null||value===undefined?'Unavailable':String(value)+(unit?' '+unit:'');list.append(term,detail)}if(!list.children.length){let term=document.createElement('dt'),detail=document.createElement('dd');term.textContent='Status';detail.textContent='No fields reported';list.append(term,detail)}section.append(heading,list);content.append(section)}catch(error){let section=document.createElement('section'),heading=document.createElement('h4'),detail=document.createElement('p');section.className='analyzer-radio-group';heading.textContent=title;detail.className='analyzer-detail';detail.textContent=error.message||'Statistics unavailable';section.append(heading,detail);content.append(section)}}let updated=document.createElement('p');updated.className='analyzer-radio-updated';updated.textContent='Updated '+fmtTime(new Date());content.append(updated);panel.replaceChildren(content)}finally{analyzerRadioStatusLoading=false}}
async function loadTronAnalyzerRadioStatus(){let panel=document.getElementById('analyzer-radio-status');if(!panel||activeView!=='tron-overview'||analyzerRadioStatusLoading)return;analyzerRadioStatusLoading=true;let groups=[['Radio','radio'],['Core','core'],['Packets','packets']],labels={noise_floor:['Noise floor','dBm'],last_rssi:['Last RSSI','dBm'],last_snr:['Last SNR','dB'],tx_air_secs:['TX airtime','s'],rx_air_secs:['RX airtime','s'],battery_mv:['Battery voltage','mV'],uptime_secs:['Uptime','s'],errors:['Errors',''],queue_len:['Transmit queue',''],recv:['Received packets',''],sent:['Sent packets',''],flood_tx:['Flood TX',''],direct_tx:['Direct TX',''],flood_rx:['Flood RX',''],direct_rx:['Direct RX',''],recv_errors:['Receive errors','']};let state=panel.querySelector('.analyzer-radio-state');if(!state){state=panel.querySelector('.analyzer-detail');if(state)state.className='analyzer-radio-state analyzer-detail'}let contentReady=Boolean(panel.querySelector('[data-stat-key]')),issues=[];try{for(let [title,statsType] of groups){try{let response=await fetch('/api/device-settings/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'stats',stats_type:statsType})}),data=await response.json();if(!response.ok){if(response.status===503){if(state)state.textContent='Connect to a MeshCore radio to view live statistics.';return}throw new Error(data.error||'Statistics unavailable')}let values=data.result&&typeof data.result==='object'&&!Array.isArray(data.result)?data.result:{};let section=panel.querySelector('[data-radio-group="'+statsType+'"]'),list;if(!section){if(!contentReady){panel.replaceChildren();contentReady=true;state=document.createElement('p');state.className='analyzer-radio-state';panel.appendChild(state)}section=document.createElement('section');section.className='analyzer-radio-group';section.dataset.radioGroup=statsType;let heading=document.createElement('h4');heading.textContent=title;list=document.createElement('dl');list.className='analyzer-radio-list';list.dataset.radioGroup=statsType;section.append(heading,list);panel.appendChild(section)}else list=section.querySelector('.analyzer-radio-list');for(let [key,value] of Object.entries(values)){let detail=[...list.querySelectorAll('[data-stat-key]')].find(item=>item.dataset.statKey===key);if(!detail){let term=document.createElement('dt');term.textContent=(labels[key]||[key.replaceAll('_',' ').replace(/\b\w/g,char=>char.toUpperCase()),''])[0];detail=document.createElement('dd');detail.dataset.statKey=key;list.append(term,detail)}let unit=(labels[key]||['',''])[1];detail.textContent=value===null||value===undefined?'Unavailable':String(value)+(unit?' '+unit:'')}}catch(error){issues.push(title)}}let timestamp=panel.querySelector('.analyzer-radio-updated');if(!timestamp){timestamp=document.createElement('p');timestamp.className='analyzer-radio-updated';panel.appendChild(timestamp)}timestamp.textContent='Updated '+fmtTime(new Date());if(state)state.textContent=issues.length?'Some readings did not refresh; previous values are retained.':'LIVE'}finally{analyzerRadioStatusLoading=false}}
setInterval(()=>{if(activeView==='analyzer')loadAnalyzerRadioStatus();else if(activeView==='tron-overview')loadTronAnalyzerRadioStatus()},15000);
function updateSearchPlaceholders(){let contactPlaceholder='Search '+mapNodes.length+' contacts';for(let id of ['node-search','map-node-search']){let input=document.getElementById(id);if(input)input.placeholder=contactPlaceholder}let channelSearch=document.getElementById('channel-search');if(channelSearch)channelSearch.placeholder='Search '+meshChannels.length+' channels'}
async function peers(){let r=await fetch('/api/peers'),d=await r.json();gatewayTelemetry=d.gateway_telemetry||{};mapNodes=d.nodes||[];meshChannels=d.channels||[];peerLimits=d.limits||{};updateAddCapacity();updateSearchPlaceholders();gateway_battery.textContent=gatewayTelemetry.battery!=null?gatewayTelemetry.battery+'%':'Unavailable';system_battery.textContent=d.system_battery!=null?d.system_battery+'%':'Unavailable';if(!mapNodes.some(item=>String(item.id)===selectedNodeId))selectedNodeId='';if(!meshChannels.some(item=>String(item.id)===selectedChannelId))selectedChannelId='';renderConversationTargets('node');renderConversationTargets('channel');renderKnownPeers();renderMapMarkers();renderAnalyzerStats();renderIncomingAdverts();if(liveTraceMap)renderLiveTraceMarkers()}
function parseChannelSender(text){let bracket=text.match(/^\[([^\]]{1,24})\]\s*/);if(bracket)return bracket[1];let colon=text.match(/^([A-Za-z0-9 _-]{1,24}):\s/);if(colon)return colon[1];return null}
async function history(type,id,boxId,scrollToLatest=false){let box=document.getElementById(boxId);if(!id){box.replaceChildren();updateConversationMenu(type);return}if(type==='node'&&isRepeaterNode(id)){let note=document.createElement('p');note.className='map-empty';note.textContent='Direct messages are not supported for repeater nodes.';box.replaceChildren(note);return}try{let response=await fetch('/api/chat-history?target_type='+encodeURIComponent(type)+'&target='+encodeURIComponent(id)),data=await response.json();if(!response.ok)throw new Error(data.error||'Message history could not be loaded');updateConversationMenu(type,data.metadata||{});let wasAtLatest=box.scrollHeight-box.clientHeight-box.scrollTop<=40,previousTop=box.scrollTop,previousBehavior=box.style.scrollBehavior;box.style.scrollBehavior='auto';box.replaceChildren();let peerName=type==='node'?(mapNodes.find(item=>String(item.id)===id)?.name||id):(meshChannels.find(item=>String(item.id)===id)?.name||('Channel '+id));for(let message of data.messages||[]){let outgoing=message.direction==='outgoing';let item=document.createElement('div');item.className='chat-message '+(outgoing?'outgoing':'incoming');let sender=outgoing?'You':(type==='channel'?(parseChannelSender(message.text)||peerName):peerName);let meta=document.createElement('div');meta.className='chat-message-meta';let avatar=document.createElement('span');avatar.className='chat-avatar';avatar.style.background=hashColor(outgoing?'you':id);avatar.textContent=sender.charAt(0)||'?';let senderLabel=document.createElement('span');senderLabel.className='chat-sender';senderLabel.textContent=sender;let time=document.createElement('span');time.className='chat-time';time.textContent=fmtTime(message.timestamp);let status=document.createElement('span');status.className='chat-status';let statusLabels={sent:'Sent',delivered:'Delivered',failed:'Failed',received:'Received'},heardCount=Number(message.heard);status.textContent=message.status==='delivered'&&Number.isInteger(heardCount)&&heardCount>0?'Delivered · '+heardCount+' heard':statusLabels[message.status]||statusLabels[outgoing?'sent':'received'];meta.append(avatar,senderLabel,time,status);let body=document.createElement('div');body.className='chat-message-body';body.textContent=message.text;item.append(meta,body);item.tabIndex=0;item.setAttribute('role','button');item.onclick=()=>openMessageActions(type,id,message,sender,data.blocked||[]);item.onkeydown=event=>{if(event.key==='Enter')item.click()};box.appendChild(item)}box.scrollTop=scrollToLatest||wasAtLatest?box.scrollHeight:previousTop;box.style.scrollBehavior=previousBehavior}catch(error){box.textContent=error.message}}
async function deleteTarget(type){let selected=type==='node'?selectedNodeId:selectedChannelId;if(!selected)return;if(!confirm('Delete this '+type+' from the device? This cannot be undone.'))return;let response=await fetch('/api/delete-target',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_type:type,target:selected})}),data=await response.json();if(!response.ok){alert(data.error||'Delete failed');return}if(type==='node'){selectedNodeId=null;document.getElementById('node-chat-title').textContent='Select a node';document.getElementById('node-chat-detail').textContent='Choose a node to view its conversation.';document.getElementById('node-send').disabled=true;document.getElementById('node-chat-history').replaceChildren()}else{selectedChannelId=null;document.getElementById('channel-chat-title').textContent='Select a channel';document.getElementById('channel-chat-detail').textContent='Choose a channel to view its conversation.';document.getElementById('channel-send').disabled=true;document.getElementById('channel-chat-history').replaceChildren()}updateConversationMenu(type);let menu=document.getElementById(type+'-chat-options');if(menu)menu.open=false;await peers()}
async function manageConversation(type,action){let selected=type==='node'?selectedNodeId:selectedChannelId;if(!selected)return;if(action==='clear'&&!confirm('Clear this conversation history? This cannot be undone.'))return;let response=await fetch('/api/chat-management',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_type:type,target:selected,action})}),data=await response.json();if(!response.ok){alert(data.error||'Conversation could not be updated');return}let menu=document.querySelector('#'+(type==='node'?'node':'channel')+'-chat-options');if(menu)menu.open=false;await history(type,selected,type==='node'?'node-chat-history':'channel-chat-history');await peers()}
function selectNode(id){selectConversation('node',id)}
function selectChannel(id){selectConversation('channel',id)}
function refreshActiveHistory(){if(['nodes','tron-overview'].includes(activeView)&&selectedNodeId)history('node',selectedNodeId,'node-chat-history');if(['channels','tron-overview'].includes(activeView)&&selectedChannelId)history('channel',selectedChannelId,'channel-chat-history')}
function setCustomRadioMode(){let custom=document.getElementById('custom-radio-fields'),enabled=document.getElementById('radio-profile').value==='custom';custom.hidden=!enabled;custom.querySelectorAll('input,select').forEach(input=>input.disabled=!enabled)}
function setCustomPowerMode(){let custom=document.getElementById('custom-power-field'),enabled=document.getElementById('tx-power-mode').value==='custom';custom.hidden=!enabled;custom.querySelector('input').disabled=!enabled}
function populatePowerOptions(maximum,current){let select=document.getElementById('tx-power-mode'),common=[10,14,17,20];select.replaceChildren();for(let value of common){if(value<=maximum)select.add(new Option(value+' dBm',String(value)))}if(!common.includes(Number(current))&&Number(current)<=maximum)select.add(new Option(current+' dBm (current)',String(current)));let currentIsCommon=[...select.options].some(option=>Number(option.value)===Number(current));select.add(new Option('Custom...','custom'));select.value=currentIsCommon?String(current):'custom';document.getElementById('custom-tx-power').value=current??'';document.getElementById('custom-tx-power').max=maximum;setCustomPowerMode()}
async function loadDeviceSettings(){deviceSettingsLoaded=false;loadedDeviceSettings=null;let statusMessage=document.getElementById('device-settings-status');statusMessage.dataset.state='';statusMessage.textContent='Loading settings from device...';try{let response=await fetch('/api/device-settings'),data=await response.json();if(!response.ok)throw new Error(data.error||'Device settings could not be loaded');loadedDeviceSettings=data;document.getElementById('device-name').value=data.name||'';document.getElementById('advert-lat').value=data.adv_lat??'';document.getElementById('advert-lon').value=data.adv_lon??'';document.getElementById('rx-delay').value=data.rx_delay??'';document.getElementById('airtime-factor').value=data.airtime_factor??'';document.getElementById('telemetry-mode-base').value=data.telemetry_mode_base??0;document.getElementById('telemetry-mode-loc').value=data.telemetry_mode_loc??0;document.getElementById('telemetry-mode-env').value=data.telemetry_mode_env??0;document.getElementById('advert-location-policy').value=data.adv_loc_policy??0;document.getElementById('manual-add-contacts').checked=Boolean(data.manual_add_contacts);document.getElementById('multi-acks').checked=Boolean(data.multi_acks);document.getElementById('custom-radio-frequency').value=data.radio_freq??'';document.getElementById('custom-radio-bandwidth').value=data.radio_bw??'';document.getElementById('custom-radio-spreading-factor').value=data.radio_sf??'';document.getElementById('custom-radio-coding-rate').value=data.radio_cr??'';let matchingProfile=Object.entries(commonRadioProfiles).find(([,profile])=>Number(profile.radio_bw)===Number(data.radio_bw)&&Number(profile.radio_sf)===Number(data.radio_sf)&&Number(profile.radio_cr)===Number(data.radio_cr));document.getElementById('radio-profile').value=matchingProfile?.[0]||'custom';setCustomRadioMode();let maximum=Number(data.max_tx_power??30);populatePowerOptions(maximum,data.tx_power);document.getElementById('tx-power-limit').textContent='Device maximum: '+maximum+' dBm';deviceSettingsLoaded=true;statusMessage.textContent='Settings loaded from device.';statusMessage.dataset.state='success'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
async function saveDeviceSettings(event){event.preventDefault();let statusMessage=document.getElementById('device-settings-status'),form=new FormData(event.currentTarget);if(!loadedDeviceSettings){statusMessage.textContent='Load settings from the connected device first.';statusMessage.dataset.state='error';return}let profile=form.get('radio_profile'),values={name:String(form.get('name')||'').trim(),adv_lat:Number(form.get('adv_lat')),adv_lon:Number(form.get('adv_lon')),rx_delay:Number(form.get('rx_delay')),airtime_factor:Number(form.get('airtime_factor')),telemetry_mode_base:Number(form.get('telemetry_mode_base')),telemetry_mode_loc:Number(form.get('telemetry_mode_loc')),telemetry_mode_env:Number(form.get('telemetry_mode_env')),adv_loc_policy:Number(form.get('adv_loc_policy')),manual_add_contacts:form.get('manual_add_contacts')==='on',multi_acks:form.get('multi_acks')==='on'},radio=profile==='custom'?{radio_freq:Number(form.get('radio_freq')),radio_bw:Number(form.get('radio_bw')),radio_sf:Number(form.get('radio_sf')),radio_cr:Number(form.get('radio_cr'))}:{radio_freq:Number(loadedDeviceSettings.radio_freq),...commonRadioProfiles[profile]};Object.assign(values,radio);let powerMode=form.get('tx_power_mode');values.tx_power=Number(powerMode==='custom'?form.get('custom_tx_power'):powerMode);statusMessage.dataset.state='';statusMessage.textContent='Saving settings to device...';try{let response=await fetch('/api/device-settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})}),data=await response.json();if(!response.ok)throw new Error(data.error||'Settings could not be saved');deviceSettingsLoaded=false;await loadDeviceSettings();statusMessage.textContent='Device settings saved.';statusMessage.dataset.state='success'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
let connectInFlight=false;
async function connect(){if(connectInFlight)return;connectInFlight=true;let button=document.getElementById('connect-btn');if(button)button.disabled=true;try{let r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({connection_type:connection_type.value,ble_mac:ble_mac.value,serial_port:serial_port.value,model:model.value})});let d=await r.json();if(!r.ok)alert(d.error);await status();await peers()}finally{connectInFlight=false;if(button)button.disabled=false}}
async function disconnect(){await fetch('/api/disconnect',{method:'POST'});await status();await peers()}
async function sendMessage(event,type,messageId,historyId){event.preventDefault();let selected=type==='node'?selectedNodeId:selectedChannelId,input=document.getElementById(messageId);if(!selected)return;if(type==='node'&&isRepeaterNode(selected)){input.disabled=true;document.getElementById('node-send').disabled=true;return}let message=input.value.trim();if(!message)return;let response=await fetch('/api/transmit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:selected,target_type:type,text:message})}),data=await response.json();if(!response.ok){alert(data.error||'Message could not be sent');return}input.value='';await history(type,selected,historyId,true)}
let consoleDrag=null;
function clampConsolePosition(dock){if(!dock||dock.classList.contains('collapsed'))return;let header=document.querySelector('.dashboard-header');if(!header)return;let minTop=header.getBoundingClientRect().bottom+12;let rect=dock.getBoundingClientRect();if(rect.top<minTop){dock.style.top=minTop+'px';dock.style.bottom='auto'}}
function toggleConsoleDock(){let dock=document.getElementById('console-dock'),toggle=document.getElementById('console-toggle');if(!dock||!toggle)return;let collapsed=dock.classList.toggle('collapsed');toggle.setAttribute('aria-expanded',String(!collapsed));if(!collapsed)clampConsolePosition(dock)}
function saveConsoleLayout(){let dock=document.getElementById('console-dock');if(!dock)return;try{localStorage.setItem('meshcore-console-layout',JSON.stringify({left:dock.style.left,top:dock.style.top,width:dock.offsetWidth+'px',height:dock.offsetHeight+'px'}))}catch(error){}}
function restoreConsoleLayout(){let dock=document.getElementById('console-dock');if(!dock)return;try{let saved=JSON.parse(localStorage.getItem('meshcore-console-layout')||'null');if(saved){if(saved.left){dock.style.left=saved.left;dock.style.right='auto'}if(saved.top){dock.style.top=saved.top;dock.style.bottom='auto'}if(saved.width)dock.style.width=saved.width;if(saved.height)dock.style.height=saved.height}}catch(error){}clampConsolePosition(dock)}
function startConsoleDrag(event){if(event.target.closest('.chat-actions')||event.button!==undefined&&event.button!==0)return;let dock=document.getElementById('console-dock');if(!dock||dock.classList.contains('collapsed'))return;event.preventDefault();let rect=dock.getBoundingClientRect();consoleDrag={offsetX:event.clientX-rect.left,offsetY:event.clientY-rect.top};dock.classList.add('dragging');document.addEventListener('pointermove',onConsoleDrag);document.addEventListener('pointerup',stopConsoleDrag)}
function onConsoleDrag(event){if(!consoleDrag)return;let dock=document.getElementById('console-dock');if(!dock)return;let header=document.querySelector('.dashboard-header');let minTop=(header?header.getBoundingClientRect().bottom:0)+12;let width=dock.offsetWidth,height=dock.offsetHeight;let left=Math.min(Math.max(event.clientX-consoleDrag.offsetX,8),window.innerWidth-width-8);let top=Math.min(Math.max(event.clientY-consoleDrag.offsetY,minTop),window.innerHeight-height-8);dock.style.left=left+'px';dock.style.top=top+'px';dock.style.right='auto';dock.style.bottom='auto'}
function stopConsoleDrag(){consoleDrag=null;let dock=document.getElementById('console-dock');if(dock){dock.classList.remove('dragging');saveConsoleLayout()}document.removeEventListener('pointermove',onConsoleDrag);document.removeEventListener('pointerup',stopConsoleDrag)}
const TRON_PANELS=[{id:'nodes',title:'Nodes',c:6,r:7},{id:'channels',title:'Channels',c:6,r:7},{id:'map',title:'Map',c:8,r:5},{id:'analyzer',title:'Analyzer',c:4,r:5}];
let tronLayout=null;
function defaultTronLayout(){let size={};TRON_PANELS.forEach(p=>size[p.id]={c:p.c,r:p.r});return{order:TRON_PANELS.map(p=>p.id),size,hidden:[]}}
function loadTronLayout(){let layout=defaultTronLayout();try{let saved=JSON.parse(localStorage.getItem('meshcore-tron-layout')||'null');if(saved){let known=TRON_PANELS.map(p=>p.id);if(Array.isArray(saved.order)){let order=saved.order.filter(id=>known.includes(id));known.forEach(id=>{if(!order.includes(id))order.push(id)});layout.order=order}if(saved.size)for(let id of known){let s=saved.size[id];if(s&&Number.isFinite(s.c)&&Number.isFinite(s.r))layout.size[id]={c:Math.min(12,Math.max(3,Math.round(s.c))),r:Math.min(12,Math.max(2,Math.round(s.r)))}}if(Array.isArray(saved.hidden))layout.hidden=saved.hidden.filter(id=>known.includes(id))}}catch(error){}return layout}
function saveTronLayout(){try{localStorage.setItem('meshcore-tron-layout',JSON.stringify(tronLayout))}catch(error){}}
function tronPanelEl(id){return document.getElementById(id+'-view')}
function applyTronLayout(animate){if(!tronLayout)return;let before={};if(animate)TRON_PANELS.forEach(p=>{let el=tronPanelEl(p.id);if(el&&!el.classList.contains('tron-hidden'))before[p.id]=el.getBoundingClientRect()});tronLayout.order.forEach((id,index)=>{let el=tronPanelEl(id);if(!el)return;let size=tronLayout.size[id];el.style.order=index+1;el.style.gridColumn='span '+size.c;el.style.gridRow='span '+size.r;el.classList.toggle('tron-hidden',tronLayout.hidden.includes(id))});if(animate)for(let id in before){let el=tronPanelEl(id),after=el.getBoundingClientRect(),dx=before[id].left-after.left,dy=before[id].top-after.top;if(!dx&&!dy||el.classList.contains('tron-dragging'))continue;el.style.transition='none';el.style.transform=`translate(${dx}px,${dy}px)`;requestAnimationFrame(()=>{el.style.transition='transform .18s ease';el.style.transform='';setTimeout(()=>{el.style.transition=''},200)})}renderTronTray();if(dashboardMap)setTimeout(()=>dashboardMap.invalidateSize(),60)}
function renderTronTray(){let tray=document.getElementById('tron-layout-tray');if(!tray)return;tray.querySelectorAll('[data-panel]').forEach(button=>button.setAttribute('aria-pressed',String(!tronLayout.hidden.includes(button.dataset.panel))))}
function toggleTronPanel(id){let hidden=tronLayout.hidden;if(hidden.includes(id))tronLayout.hidden=hidden.filter(x=>x!==id);else hidden.push(id);saveTronLayout();applyTronLayout(true)}
function resetTronLayout(){tronLayout=defaultTronLayout();saveTronLayout();applyTronLayout(true)}
function startTronMove(event,id){if(event.button>0||event.target.closest('button'))return;event.preventDefault();let el=tronPanelEl(id),lastTarget=null;el.classList.add('tron-dragging');let bar=event.currentTarget;bar.setPointerCapture(event.pointerId);let move=e=>{let hit=document.elementsFromPoint(e.clientX,e.clientY).map(node=>node.closest&&node.closest('main.view-panel')).find(node=>node&&TRON_PANELS.some(p=>p.id+'-view'===node.id)&&!node.classList.contains('tron-hidden'));if(!hit||hit===el){lastTarget=null;return}if(hit===lastTarget)return;lastTarget=hit;let targetId=hit.id.replace(/-view$/,''),order=tronLayout.order,index=order.indexOf(targetId);order.splice(order.indexOf(id),1);order.splice(index,0,id);applyTronLayout(true)},up=()=>{bar.removeEventListener('pointermove',move);bar.removeEventListener('pointerup',up);bar.removeEventListener('pointercancel',up);el.classList.remove('tron-dragging');saveTronLayout()};bar.addEventListener('pointermove',move);bar.addEventListener('pointerup',up);bar.addEventListener('pointercancel',up)}
function startTronResize(event,id){if(event.button>0)return;event.preventDefault();event.stopPropagation();let handle=event.currentTarget,style=getComputedStyle(document.body),cols=style.gridTemplateColumns.split(' ').map(parseFloat),rows=style.gridTemplateRows.split(' ').map(parseFloat),gap=parseFloat(style.columnGap)||0,colUnit=(cols[0]||80)+gap,rowUnit=(rows[1]||60)+(parseFloat(style.rowGap)||0),start=tronLayout.size[id],x0=event.clientX,y0=event.clientY;handle.setPointerCapture(event.pointerId);let move=e=>{let c=Math.min(12,Math.max(3,Math.round(start.c+(e.clientX-x0)/colUnit))),r=Math.min(12,Math.max(2,Math.round(start.r+(e.clientY-y0)/rowUnit)));let size=tronLayout.size[id];if(size.c!==c||size.r!==r){tronLayout.size[id]={c,r};applyTronLayout(false)}},up=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',up);handle.removeEventListener('pointercancel',up);saveTronLayout()};handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',up);handle.addEventListener('pointercancel',up)}
function initTronLayout(){tronLayout=loadTronLayout();let tray=document.createElement('div');tray.id='tron-layout-tray';tray.setAttribute('aria-label','Dashboard panels');tray.innerHTML='<span>Panels</span>';for(let p of TRON_PANELS){let el=tronPanelEl(p.id);if(!el)continue;let bar=document.createElement('div');bar.className='tron-panel-bar';bar.title='Drag to move';bar.innerHTML='<span class="tron-grip">&#8942;&#8942; '+p.title+'</span><button type="button" title="Hide panel" aria-label="Hide '+p.title+' panel">&minus;</button>';bar.addEventListener('pointerdown',e=>startTronMove(e,p.id));bar.querySelector('button').addEventListener('click',()=>toggleTronPanel(p.id));el.prepend(bar);let handle=document.createElement('div');handle.className='tron-resize';handle.title='Drag to resize';handle.addEventListener('pointerdown',e=>startTronResize(e,p.id));el.append(handle);let toggle=document.createElement('button');toggle.type='button';toggle.dataset.panel=p.id;toggle.textContent=p.title;toggle.title='Show or hide '+p.title;toggle.addEventListener('click',()=>toggleTronPanel(p.id));tray.append(toggle)}let reset=document.createElement('button');reset.type='button';reset.textContent='Reset';reset.title='Restore the default layout';reset.addEventListener('click',resetTronLayout);tray.append(reset);document.body.append(tray);applyTronLayout(false);if(window.ResizeObserver){let map=document.getElementById('map-view');if(map)new ResizeObserver(()=>{if(dashboardMap)dashboardMap.invalidateSize()}).observe(map)}}
function initConsoleDock(){let dock=document.getElementById('console-dock'),toggle=document.getElementById('console-toggle');if(!dock||!toggle)return;let collapsed=true;dock.classList.toggle('collapsed',collapsed);toggle.setAttribute('aria-expanded',String(!collapsed));restoreConsoleLayout();if(window.ResizeObserver)new ResizeObserver(()=>{if(!consoleDrag)saveConsoleLayout()}).observe(dock);window.addEventListener('resize',()=>clampConsolePosition(dock))}
function applyBannerVisibility(){let prefs={link:true,battery:true,weather:true,time:true};try{prefs={...prefs,...JSON.parse(localStorage.getItem('meshcore-banner-visibility')||'{}')}}catch(error){}let map={link:'banner-item-link',battery:'banner-item-battery',weather:'banner-item-weather',time:'banner-item-time'};for(let key of Object.keys(map)){let item=document.getElementById(map[key]);if(item)item.hidden=!prefs[key];let checkbox=document.getElementById('banner-toggle-'+key);if(checkbox)checkbox.checked=prefs[key]}}
function saveBannerVisibility(){let prefs={link:document.getElementById('banner-toggle-link').checked,battery:document.getElementById('banner-toggle-battery').checked,weather:document.getElementById('banner-toggle-weather').checked,time:document.getElementById('banner-toggle-time').checked};try{localStorage.setItem('meshcore-banner-visibility',JSON.stringify(prefs))}catch(error){}applyBannerVisibility()}
window.addEventListener('DOMContentLoaded',()=>{loadFavoriteNodes();fields();status();peers();loadAppConfig();loadOllamaModels();updateClock();initConsoleDock();applyBannerVisibility();syncNotificationControl();initTronLayout();pollNoiseFloor();setInterval(pollNoiseFloor,2000);pollCpuTemp();setInterval(pollCpuTemp,2000);setInterval(updateClock,1000);setInterval(refreshActiveHistory,3000);setInterval(loadLocalWeather,30*60*1000)});setInterval(status,2000);setInterval(peers,10000);
</script></head><body>
<header class="dashboard-header">
<div class="brand-lockup"><img class="dashboard-logo" src="/dashboard-logo.png" alt="Dashboard logo"><div class="brand-copy"><span class="header-label">MESHCORE + OLLAMA</span><h1>DASHBOARD</h1></div></div>
<section class="incoming-adverts" aria-label="Incoming radio adverts"><div class="incoming-adverts-heading"><h3>Incoming Adverts</h3><span id="incoming-adverts-count" class="incoming-adverts-count">0</span></div><div id="incoming-adverts-list" class="incoming-adverts-list" aria-live="polite"><p class="incoming-advert-empty">Waiting for incoming adverts...</p></div></section>
<section class="noise-floor" aria-label="Noise floor"><div class="noise-floor-heading"><h3>Noise Floor</h3><span id="noise-floor-value" class="noise-floor-value">-- dBm</span></div><canvas id="noise-floor-canvas" class="noise-floor-canvas" role="img" aria-label="Real-time noise floor in dBm"></canvas><div class="cpu-temp" title="CPU temperature"><h3>CPU</h3><span id="cpu-temp-value" class="noise-floor-value">--°F</span></div></section>
<nav class="top-nav" aria-label="Dashboard pages"><button type="button" class="nav-tab" data-view="nodes" aria-pressed="true" onclick="showView('nodes')">Nodes</button><button type="button" class="nav-tab" data-view="channels" aria-pressed="false" onclick="showView('channels')">Channels</button><button type="button" class="nav-tab" data-view="map" aria-pressed="false" onclick="showView('map')">Map <span class="nav-count" id="map-node-count" hidden>0</span></button><button type="button" class="nav-tab" data-view="analyzer" aria-pressed="false" onclick="showView('analyzer')">Analyzer <span class="nav-count" id="analyzer-count" hidden>0</span></button><button type="button" class="nav-tab" data-view="settings" aria-pressed="false" onclick="showView('settings')">App Settings</button><button type="button" class="nav-tab" data-view="device-settings" aria-pressed="false" onclick="showView('device-settings')">Device Settings</button></nav>
<div class="header-meta">
<div id="banner-item-link" class="header-meta-item"><span id="status" class="header-status disconnected">DISCONNECTED</span></div>
<div id="banner-item-battery" class="header-meta-item"><div class="battery-readings"><span class="battery-reading"><span class="header-label">RADIO</span><span id="gateway_battery" class="header-metric">Unavailable</span></span><span class="battery-reading"><span class="header-label">SYSTEM</span><span id="system_battery" class="header-metric">Unavailable</span></span></div></div>
<div id="banner-item-weather" class="header-meta-item weather-widget" aria-live="polite"><span id="weather-location" class="header-label weather-place">Location not set</span><div class="weather-current"><span id="weather-icon" class="weather-icon" role="img" aria-label="Weather condition unavailable" title="Weather condition unavailable"></span><strong id="weather-temperature">--°F</strong><span id="weather-condition">Set location</span><span id="weather-extra" class="weather-extra"></span></div></div>
<div id="banner-item-time" class="header-meta-item"><span id="current-datetime" class="header-metric">--</span></div>
</div>
<div class="tron-quick-tabs" aria-label="TRON navigation"><button type="button" onclick="showView('nodes')">Dashboard</button><button type="button" onclick="showView('settings')">App Settings</button><button type="button" onclick="showView('device-settings')">Device Settings</button></div>
<button type="button" id="console-toggle" class="console-toggle" onclick="toggleConsoleDock()" aria-expanded="false" aria-controls="console-dock">Console</button>
</header>
<main id="nodes-view" class="view-panel page-view">
<div class="messages-layout">
<aside class="card conversation-rail"><div class="panel-heading"><div><span class="eyebrow">DIRECT MESSAGES</span><h2>Nodes</h2></div><span class="panel-index">02</span></div><div class="conversation-filters"><div class="node-search-label"><label for="node-search">Search</label><div class="search-input-row"><input type="search" id="node-search" placeholder="Filter by name or ID" oninput="renderConversationTargets('node')"><details class="search-filter-menu"><summary aria-label="Sort and type filters" title="Sort and type filters"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16l-6.5 7.5v5l-3 1.5v-6.5L4 5Z"/></svg></summary><div class="search-filter-panel"><label for="node-sort">Sort<select id="node-sort" onchange="renderConversationTargets('node')"><option value="az">A-Z</option><option value="heard">Heard recently</option><option value="messages">Latest messages</option></select></label><label for="node-type-filter">Type<select id="node-type-filter" onchange="renderConversationTargets('node')"><option value="all">All</option><option value="favorites">Favorites</option><option value="users">Users</option><option value="repeaters">Repeaters</option><option value="room-servers">Room servers</option><option value="sensors">Sensors</option></select></label><label for="node-chat-filter">Chats<select id="node-chat-filter" onchange="renderConversationTargets('node')"><option value="active">Active</option><option value="archived">Archived</option><option value="all">All</option></select></label></div></details></div></div></div><div class="conversation-target-list" id="node-target-list"><p class="map-empty">Waiting for nodes...</p></div></aside>
<section class="card chat-panel"><div class="chat-header"><div class="chat-header-main"><div class="chat-header-copy"><strong id="node-chat-title">Select a node</strong><span id="node-chat-detail">Choose a node to view its conversation.</span></div><details class="chat-actions" id="node-chat-options"><summary aria-label="Node chat options">Options</summary><div class="chat-action-menu"><button id="node-clear-action" type="button" disabled onclick="manageConversation('node','clear')">Clear chat</button><button id="node-archive-action" type="button" disabled onclick="manageConversation('node','archive')">Archive chat</button><button id="node-pin-action" type="button" disabled onclick="manageConversation('node','pin')">Pin chat</button><button id="node-delete-action" type="button" disabled onclick="deleteTarget('node')">Delete node</button></div></details></div></div><div id="node-chat-history"></div><form onsubmit="sendMessage(event,'node','node-message','node-chat-history')"><input id="node-message" maxlength="100" placeholder="Message selected node" required><button id="node-send" disabled>Send</button></form></section>
</div>
</main>
<main id="channels-view" class="view-panel page-view" hidden>
<div class="messages-layout">
<aside class="card conversation-rail"><div class="panel-heading"><div><span class="eyebrow">SHARED FREQUENCY</span><h2>Channels</h2></div><span class="panel-index">03</span></div><div class="conversation-filters"><div class="node-search-label"><label for="channel-search">Search</label><div class="search-input-row"><input type="search" id="channel-search" placeholder="Filter by name or ID" oninput="renderConversationTargets('channel')"><details class="search-filter-menu"><summary aria-label="Chat filters" title="Chat filters"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16l-6.5 7.5v5l-3 1.5v-6.5L4 5Z"/></svg></summary><div class="search-filter-panel"><label for="channel-chat-filter">Chats<select id="channel-chat-filter" onchange="renderConversationTargets('channel')"><option value="active">Active</option><option value="archived">Archived</option><option value="all">All</option></select></label></div></details></div></div></div><div class="conversation-target-list" id="channel-target-list"><p class="map-empty">Waiting for channels...</p></div></aside>
<section class="card chat-panel"><div class="chat-header"><div class="chat-header-main"><div class="chat-header-copy"><strong id="channel-chat-title">Select a channel</strong><span id="channel-chat-detail">Choose a channel to view its conversation.</span></div><details class="chat-actions" id="channel-chat-options"><summary aria-label="Channel chat options">Options</summary><div class="chat-action-menu"><button id="channel-clear-action" type="button" disabled onclick="manageConversation('channel','clear')">Clear chat</button><button id="channel-archive-action" type="button" disabled onclick="manageConversation('channel','archive')">Archive chat</button><button id="channel-pin-action" type="button" disabled onclick="manageConversation('channel','pin')">Pin chat</button><button id="channel-delete-action" type="button" disabled onclick="deleteTarget('channel')">Delete channel</button></div></details></div></div><div id="channel-chat-history"></div><form onsubmit="sendMessage(event,'channel','channel-message','channel-chat-history')"><input id="channel-message" maxlength="100" placeholder="Message selected channel" required><button id="channel-send" disabled>Send</button></form></section>
</div>
</main>
<main id="analyzer-view" class="view-panel page-view" hidden>
<div class="analyzer-shell">
<section class="analyzer-main"><header class="analyzer-toolbar"><div><span class="eyebrow">LOCAL RADIO STREAM</span><h2>Packet Analyzer</h2><p>Message activity from this gateway. Network-wide packet capture requires an observer feed.</p></div><span class="live-tag">LIVE</span></header><div class="analyzer-table-wrap"><table class="analyzer-table"><thead><tr><th>Time</th><th>Direction</th><th>Transport</th><th>Target</th><th>Status</th></tr></thead><tbody id="analyzer-packet-list"><tr><td class="packet-empty" colspan="5">Waiting for radio activity...</td></tr></tbody></table></div></section>
<aside class="analyzer-side"><section class="analyzer-side-section"><h3 id="analyzer-session-title">Session</h3><div class="analyzer-stat-grid"><div class="analyzer-stat"><strong id="analyzer-total">0</strong><span>Events</span></div><div class="analyzer-stat"><strong id="analyzer-nodes">0</strong><span>Peers</span></div><div class="analyzer-stat"><strong id="analyzer-channels">0</strong><span>Channels</span></div><div class="analyzer-stat"><strong id="analyzer-located">0</strong><span>Located</span></div></div><div id="analyzer-radio-status" class="analyzer-radio-status" aria-live="polite"><div class="analyzer-detail">Connect to a MeshCore radio to view live statistics.</div></div></section><section class="analyzer-side-section"><h3>Selected event</h3><div id="analyzer-event-detail" class="analyzer-detail">Select a packet row to inspect its local event metadata.</div></section><section class="analyzer-side-section"><h3>Scope</h3><div class="analyzer-detail"><strong>Source</strong><br>Connected MeshCore gateway<br><br><strong>Retention</strong><br>Last 60 local activity events</div></section></aside>
</div>
</main>
<main id="settings-view" class="view-panel page-view" hidden>
<button type="button" class="tron-settings-back" onclick="showView('nodes')">Dashboard</button>
<div class="settings-layout">
<section class="card"><div class="panel-heading"><div><span class="eyebrow">APPLICATION</span><h2>Settings</h2></div><span class="panel-index">04</span></div>
<div class="settings-tabs" role="tablist" aria-label="Settings sections"><button type="button" class="settings-tab" role="tab" data-settings-tab="preferences" aria-pressed="true" onclick="showSettingsTab('preferences')">Themes</button><button type="button" class="settings-tab" role="tab" data-settings-tab="ollama" aria-pressed="false" onclick="showSettingsTab('ollama')">Ollama</button><button type="button" class="settings-tab" role="tab" data-settings-tab="bot" aria-pressed="false" onclick="showSettingsTab('bot')">Bot</button><button type="button" class="settings-tab" role="tab" data-settings-tab="tightvnc" aria-pressed="false" onclick="showSettingsTab('tightvnc')">Remote Desktop</button><button type="button" class="settings-tab" role="tab" data-settings-tab="weather" aria-pressed="false" onclick="showSettingsTab('weather')">Weather</button><button type="button" class="settings-tab" role="tab" data-settings-tab="config" aria-pressed="false" onclick="showSettingsTab('config')">Update</button><button type="button" class="settings-tab" role="tab" data-settings-tab="banner" aria-pressed="false" onclick="showSettingsTab('banner')">Banner</button><button type="button" class="settings-tab" role="tab" data-settings-tab="logs" aria-pressed="false" onclick="showSettingsTab('logs')">Logs</button></div>
<section id="settings-preferences-panel" class="settings-tab-panel">
<div class="settings-grid">
<div class="settings-item"><div class="theme-control-row"><label for="theme-mode-select">Theme</label><select id="theme-mode-select" onchange="selectThemeMode(this.value)"><option value="tron">TRON</option><option value="classic">Classic</option></select></div><div id="classic-theme-control" class="theme-control-row"><label for="theme-select">Colors</label><select id="theme-select" onchange="selectClassicTheme(this.value)"><option value="midnight">Midnight</option><option value="light">Light</option><option value="ocean">Ocean</option><option value="amber">Amber</option><option value="linux">Linux Console</option><option value="macos">macOS</option><option value="cyberpunk">Hacker Cyberpunk</option></select></div><p class="settings-description">Select TRON mode or choose Classic with a color palette.</p></div>
<div class="settings-item"><label><input type="checkbox" id="notification-toggle" checked onchange="saveNotificationSetting()"> Message notification sound and pop-up</label><p class="settings-description">Play a sound and show a brief pop-up when a new message arrives. Browsers may require a click on the page before audio can play.</p></div>
</div>
<p id="preferences-status" class="preferences-status" aria-live="polite"></p>
</section>
<section id="settings-ollama-panel" class="settings-tab-panel" hidden>
<div class="settings-grid">
<div class="settings-item"><label for="model">Active Ollama model</label><select id="model" onchange="savePreference('model',this.value)">{{MODEL_OPTIONS}}</select><p class="settings-description">Saved in config.json and used for bot replies.</p></div>
<div class="settings-item"><label for="ollama-power-toggle">Ollama server</label><button type="button" id="ollama-power-toggle" onclick="toggleOllama()">Turn off</button><p class="settings-description" id="ollama-power-status">Checking status...</p></div>
<div class="settings-item settings-item-full">
<div class="model-manager-header"><label>Installed models</label><button type="button" class="secondary refresh-btn" onclick="loadOllamaModels()">Refresh</button></div>
<p class="settings-description">Manage installed models on disk. Deleting unused models frees up space on Live Kali or small drives.</p>
<div id="installed-models-list" class="installed-models-list"><p class="map-empty">Loading models...</p></div>
<p id="ollama-model-status" class="preferences-status" aria-live="polite"></p>
</div>
<div class="settings-item settings-item-full">
<label for="ollama-download-input">Download new model</label>
<div class="download-control">
<input id="ollama-download-input" placeholder="e.g. qwen2.5:0.5b or llama3.2:1b">
<button type="button" id="ollama-download-button" onclick="downloadOllamaModel()">Download</button>
</div>
<div class="model-preset-row">
<span>Presets:</span>
<button type="button" class="preset-btn" onclick="setDownloadModel('qwen2.5:0.5b')">qwen2.5:0.5b (~400 MB)</button>
<button type="button" class="preset-btn" onclick="setDownloadModel('llama3.2:1b')">llama3.2:1b (~1.3 GB)</button>
<button type="button" class="preset-btn" onclick="setDownloadModel('llama3.2:3b')">llama3.2:3b (~2.0 GB)</button>
<button type="button" class="preset-btn" onclick="setDownloadModel('phi3:mini')">phi3:mini (~2.2 GB)</button>
</div>
<p class="settings-description">Enter any Ollama model name or select a preset to pull it from the Ollama library.</p>
<div id="ollama-download-progress" class="ollama-download-progress" hidden aria-live="polite">
<p id="ollama-download-phase">Preparing download...</p>
<progress id="ollama-download-progress-bar" max="100"></progress>
<p id="ollama-download-detail"></p>
</div>
</div>
</div>
<form class="settings-grid" style="margin-top:16px" onsubmit="saveOllamaSettings(event)">
<div class="settings-item"><label><input type="checkbox" id="ollama-schedule-enabled"> Schedule on/off automatically</label><p class="settings-description">Turns Ollama on at the start time and off at the end time every day, to lower power use.</p></div>
<div class="settings-item"><label for="ollama-schedule-start">Turn on at</label><input type="time" id="ollama-schedule-start" value="07:00"></div>
<div class="settings-item"><label for="ollama-schedule-end">Turn off at</label><input type="time" id="ollama-schedule-end" value="17:00"></div>
<div class="settings-actions"><button type="submit">Save schedule</button><p id="ollama-settings-status" class="preferences-status" aria-live="polite"></p></div>
</form>
</section>
<section id="settings-bot-panel" class="settings-tab-panel" hidden>
<form class="settings-grid" onsubmit="saveBotSettings(event)">
<div class="settings-item"><label for="bot-name">Bot name</label><input id="bot-name" name="name" maxlength="40" required><p class="settings-description">Shown as the reply prefix in channel messages.</p></div>
<div class="settings-item"><label for="bot-personality">Personality</label><input id="bot-personality" name="personality" maxlength="120" required><p class="settings-description">Tone and phrasing style used for replies, e.g. "helpful, friendly, and concise".</p></div>
<div class="settings-item"><label><input type="checkbox" id="bot-greet-new-users" onchange="saveGreetingSetting(this.checked)"> Greet new users</label><p class="settings-description">Send /greet on in the channel where you want greetings sent. Off by default.</p></div>
<div class="settings-item"><label for="bot-response-length">Response length</label><select id="bot-response-length" name="response_length"><option value="short">Short (up to 3 packets)</option><option value="medium">Medium (up to 6 packets)</option><option value="long">Long (up to 12 packets)</option></select><p class="settings-description">Caps how many mesh-radio packets a direct message or channel reply can use, so long answers don't flood the network.</p></div>
<div class="settings-actions"><button type="submit">Save bot settings</button><p id="bot-settings-status" class="preferences-status" aria-live="polite"></p></div>
</form>
<div class="bot-terminal" id="bot-terminal"><div class="bot-terminal-bar"><span>BOT TERMINAL</span><span class="bot-terminal-note">Local test chat &middot; not sent to the MeshCore device</span><button type="button" onclick="clearBotTerminal()">Clear</button></div><div class="bot-terminal-output" id="bot-terminal-output" role="log" aria-live="polite"></div><form class="bot-terminal-input" onsubmit="sendBotTerminal(event)"><span>&gt;</span><input id="bot-terminal-text" autocomplete="off" maxlength="2000" placeholder="Ask the model something..."><button type="submit" id="bot-terminal-send">Send</button></form></div>
</section>
<section id="settings-tightvnc-panel" class="settings-tab-panel" hidden>
<div class="settings-grid">
<div class="settings-item settings-item-full"><label>TightVNC / noVNC remote desktop</label><p class="settings-description">Manage the TightVNC server on localhost:5901 and the secure browser proxy on port 6080. Connect at <span class="live-host-url" data-port="6080" data-path="/vnc.html"></span> and accept the self-signed certificate warning.</p><div class="device-action-grid"><button type="button" data-tightvnc-action="on" onclick="setTightvnc('on')">Turn on</button><button type="button" class="secondary" data-tightvnc-action="off" onclick="setTightvnc('off')">Turn off</button><button type="button" class="secondary" data-tightvnc-action="restart" onclick="setTightvnc('restart')">Restart</button></div><p id="tightvnc-status" class="preferences-status" aria-live="polite">Checking status...</p></div>
<div class="settings-item settings-item-full"><label>SSH terminal</label><p class="settings-description">Opens a browser terminal on port 7681 that logs into this Orange Pi over SSH. You sign in with the Pi's own username and password. Accept the self-signed certificate warning.</p><div class="device-action-grid"><button type="button" data-ssh-action="on" onclick="setSshTerminal('on')">Turn on</button><button type="button" class="secondary" data-ssh-action="off" onclick="setSshTerminal('off')">Turn off</button><button type="button" class="secondary" data-ssh-action="restart" onclick="setSshTerminal('restart')">Restart</button><button type="button" id="ssh-open-button" class="secondary" onclick="openSshTerminal()" disabled>Open terminal</button></div><p id="ssh-terminal-status" class="preferences-status" aria-live="polite">Checking status...</p></div>
</div>
</section>
<section id="settings-config-panel" class="settings-tab-panel" hidden>
<label for="config-json-editor">config.json contents</label><textarea id="config-json-editor" class="config-json-editor" rows="18" spellcheck="false" aria-label="Edit config.json"></textarea>
<label class="settings-item-full"><input type="checkbox" id="auto-update-enabled" checked onchange="saveAutoUpdateSetting()"> Automatically check the repository for updates every 24 hours and prompt to install</label>
<label class="settings-item-full"><input type="checkbox" id="autostart-enabled" onchange="saveAutostart()"> Start the dashboard automatically when the system boots <span id="autostart-status" aria-live="polite"></span></label>
<div class="config-actions"><button type="button" onclick="saveConfigFile()">Save config.json</button><button id="restart-dashboard-button" type="button" onclick="restartDashboard()">Restart Dashboard</button><button id="update-app-button" class="secondary" type="button" onclick="updateApp()">Update from repository</button><p id="config-status" class="config-status" aria-live="polite"></p></div>
</section>
<section id="settings-weather-panel" class="settings-tab-panel" hidden>
<form class="weather-settings-layout" onsubmit="saveWeatherLocation(event)">
<label for="weather-city">City<input id="weather-city" name="city" maxlength="80" value="" required></label>
<label for="weather-state">State (optional)<input id="weather-state" name="state" maxlength="80" value="" placeholder="Optional"></label>
<div class="settings-actions"><button type="submit">Save location</button><p id="weather-settings-status" class="preferences-status" aria-live="polite"></p></div>
</form>
</section>
<section id="settings-logs-panel" class="settings-tab-panel" hidden>
<pre id="app-log-output" class="device-debug-output" style="max-height:60vh;margin:0 0 12px">Loading...</pre>
<div class="config-actions"><button type="button" onclick="loadAppLogs()">Refresh</button><button type="button" class="secondary" onclick="clearAppLogs()">Clear logs</button><p id="app-log-status" class="config-status" aria-live="polite"></p></div>
</section>
<section id="settings-banner-panel" class="settings-tab-panel" hidden>
<div class="settings-grid">
<label><input type="checkbox" id="banner-toggle-link" checked onchange="saveBannerVisibility()"> Link status</label>
<label><input type="checkbox" id="banner-toggle-battery" checked onchange="saveBannerVisibility()"> Battery</label>
<label><input type="checkbox" id="banner-toggle-weather" checked onchange="saveBannerVisibility()"> Weather</label>
<label><input type="checkbox" id="banner-toggle-time" checked onchange="saveBannerVisibility()"> Local time</label>
</div>
<p class="settings-description">Choose which indicators appear in the top right of the banner.</p>
</section>
</section>
</div>
</main>
<main id="device-settings-view" class="view-panel page-view" hidden>
<div class="device-settings-layout reference-device-settings">
<section class="card connection-card">
<div class="panel-heading"><div><span class="eyebrow">RADIO LINK</span><h2>Connection</h2></div><span class="panel-index">01</span></div>
<label for="connection_type">Connection type</label><select id="connection_type" onchange="fields()"><option value="bluetooth">Bluetooth</option><option value="serial">Serial</option></select>
<div id="ble-field"><label for="ble_mac">Bluetooth device</label><div class="scan-control"><select id="ble_mac"><option value="">Scan for Bluetooth devices</option></select><button id="ble-scan" type="button" onclick="scanBluetooth()">Scan</button></div><p id="ble-scan-status" class="scan-status" aria-live="polite"></p></div>
<div id="serial-field" style="display:none"><label for="serial_port">Serial port</label><div class="scan-control"><select id="serial_port"><option value="">Scan for serial ports</option></select><button id="serial-scan" type="button" onclick="scanSerial()">Scan</button></div><p id="serial-scan-status" class="scan-status" aria-live="polite"></p></div>
<div class="connection-actions"><button id="connect-btn" onclick="connect()">Connect</button><button onclick="disconnect()">Disconnect</button></div>
</section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">IDENTITY</span><h2 id="device-identity-name">Connected device</h2></div><span id="device-identity-status" class="header-status disconnected">DISCONNECTED</span></div><button id="identity-toggle" class="device-setting-row" type="button" onclick="toggleDeviceIdentity()" aria-expanded="false"><span><strong>Device information</strong><small>Identifier, battery, firmware, key, contacts and channels</small></span><span id="identity-expand-icon">+</span></button><dl id="device-identity-details" class="device-info-grid" hidden></dl></section>
<form id="device-settings-form" onsubmit="saveDeviceSettings(event)">
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">NODE</span><h2>Node settings</h2></div></div><div class="device-settings-grid"><label><span>Node name</span><input id="device-name" name="name" maxlength="32" required></label><label><span>Radio preset</span><select id="radio-preset" onchange="applyRadioPreset()"><option value="custom">Custom settings</option></select></label><label><span>Frequency (MHz)</span><input id="custom-radio-frequency" name="radio_freq" type="number" min="150" max="2500" step="0.001" required></label><label><span>Bandwidth</span><select id="custom-radio-bandwidth" name="radio_bw"><option value="7.8">7.8 kHz</option><option value="10.4">10.4 kHz</option><option value="15.6">15.6 kHz</option><option value="20.8">20.8 kHz</option><option value="31.25">31.25 kHz</option><option value="41.7">41.7 kHz</option><option value="62.5">62.5 kHz</option><option value="125">125 kHz</option><option value="250">250 kHz</option><option value="500">500 kHz</option></select></label><label><span>Spreading factor</span><select id="custom-radio-spreading-factor" name="radio_sf"><option>5</option><option>6</option><option>7</option><option>8</option><option>9</option><option>10</option><option>11</option><option>12</option></select></label><label><span>Coding rate</span><select id="custom-radio-coding-rate" name="radio_cr"><option value="5">4/5</option><option value="6">4/6</option><option value="7">4/7</option><option value="8">4/8</option></select></label><label><span>TX power (dBm)</span><input id="custom-tx-power" name="tx_power" type="number" min="-9" max="30" step="1" required></label><label class="device-toggle-row" id="client-repeat-row"><span><strong>Client repeat</strong><small>Allow this client to repeat packets</small></span><input id="client-repeat" name="repeat" type="checkbox"></label><label><span>Path hash mode</span><select id="path-hash-mode" name="path_hash_mode"><option value="0">1 byte per hop</option><option value="1">2 bytes per hop</option><option value="2">3 bytes per hop</option></select></label><label><span>RX delay</span><input id="rx-delay" name="rx_delay" type="number" min="0" max="4294967295" step="1"></label><label><span>Airtime factor</span><input id="airtime-factor" name="airtime_factor" type="number" min="0" max="4294967295" step="1"></label></div><p id="tx-power-limit" class="settings-description">Power range depends on connected hardware.</p></section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">REGIONS</span><h2>Region management</h2></div></div><div class="device-settings-grid"><label><span>Default region</span><select id="default-region" onchange="saveDefaultRegion()"><option value="">None</option></select></label><label><span>Add region</span><input id="new-region-name" maxlength="30" pattern="[a-z0-9-]{1,30}" placeholder="region-name"></label></div><div class="device-action-grid"><button type="button" class="secondary" onclick="addLocalRegion()">Add region</button><button type="button" class="secondary" disabled title="This companion library does not expose anonymous repeater region queries">Fetch from repeaters</button></div><div id="local-region-list" class="local-region-list"></div><p id="region-status" class="settings-status" aria-live="polite"></p><p class="settings-description">Regions are stored locally in this browser. Fetching region lists from repeaters requires a companion query API that is not exposed by the current Python client.</p></section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">LOCATION</span><h2>Location settings</h2></div></div><div class="device-settings-grid"><label><span>Latitude</span><input id="advert-lat" name="adv_lat" type="number" min="-90" max="90" step="0.000001"></label><label><span>Longitude</span><input id="advert-lon" name="adv_lon" type="number" min="-180" max="180" step="0.000001"></label><label><span>GPS update interval (seconds)</span><input id="gps-interval" name="gps_interval" type="number" min="60" max="86399" step="1"></label><label class="device-toggle-row"><span><strong>GPS enabled</strong><small>Enable device GPS updates when supported</small></span><input id="gps-enabled" name="gps_enabled" type="checkbox"></label></div></section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">CONTACTS</span><h2>Contact settings</h2></div></div><div class="device-settings-grid"><label class="device-toggle-row"><span><strong>Auto-add users</strong><small>Accept new chat contacts automatically</small></span><input id="auto-add-users" type="checkbox"></label><label class="device-toggle-row"><span><strong>Auto-add repeaters</strong><small>Accept repeater contacts automatically</small></span><input id="auto-add-repeaters" type="checkbox"></label><label class="device-toggle-row"><span><strong>Auto-add room servers</strong><small>Accept room server contacts automatically</small></span><input id="auto-add-rooms" type="checkbox"></label><label class="device-toggle-row"><span><strong>Auto-add sensors</strong><small>Accept sensor contacts automatically</small></span><input id="auto-add-sensors" type="checkbox"></label><label class="device-toggle-row"><span><strong>Overwrite oldest when full</strong><small>Replace oldest non-favorite contact when contact storage is full</small></span><input id="auto-add-overwrite" type="checkbox"></label></div></section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">PRIVACY</span><h2>Telemetry and privacy</h2></div></div><div class="device-settings-grid"><label class="device-toggle-row"><span><strong>Advertise location</strong><small>Include device location in advertisements</small></span><input id="advert-location-policy" type="checkbox"></label><label class="device-toggle-row"><span><strong>Multi-ACK</strong><small>Send multiple acknowledgements for delivery reliability</small></span><input id="multi-acks" type="checkbox"></label><label><span>Base telemetry</span><select id="telemetry-mode-base"><option value="0">Deny all</option><option value="1">Allow by contact flags</option><option value="2">Allow all</option></select></label><label><span>Location telemetry</span><select id="telemetry-mode-loc"><option value="0">Deny all</option><option value="1">Allow by contact flags</option><option value="2">Allow all</option></select></label><label><span>Environment telemetry</span><select id="telemetry-mode-env"><option value="0">Deny all</option><option value="1">Allow by contact flags</option><option value="2">Allow all</option></select></label><label><span>Manual contact approval</span><input id="manual-add-contacts" type="checkbox"></label></div></section>
<div class="settings-actions"><button type="submit">Save device settings</button><button class="secondary" type="button" onclick="loadDeviceSettings()">Refresh from device</button><p id="device-settings-status" class="settings-status" aria-live="polite"></p></div>
</form>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">ACTIONS</span><h2>Device actions</h2></div></div><div class="device-action-grid"><button type="button" class="secondary" onclick="runDeviceAction('sync_time')">Sync time</button><button type="button" class="secondary" onclick="runDeviceAction('refresh_contacts')">Refresh contacts</button><button type="button" class="secondary" onclick="runDeviceAction('advert_zero_hop')">Send advert (zero hop)</button><button type="button" class="secondary" onclick="runDeviceAction('advert_flood')">Send advert (flood routed)</button><button type="button" class="secondary" onclick="runDeviceAction('reboot')">Reboot device</button><button type="button" class="secondary danger-action" onclick="runDeviceAction('delete_paths')">Delete all paths</button></div><p id="device-action-status" class="settings-status" aria-live="polite"></p></section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">EXPORT</span><h2>GPX export</h2></div></div><div class="device-action-grid"><button type="button" class="secondary" onclick="exportDeviceGpx('repeaters')">Export repeaters</button><button type="button" class="secondary" onclick="exportDeviceGpx('contacts')">Export contacts</button><button type="button" class="secondary" onclick="exportDeviceGpx('all')">Export all</button></div><p id="gpx-export-status" class="settings-status" aria-live="polite"></p></section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">DIAGNOSTICS</span><h2>Debug and statistics</h2></div></div><div class="device-action-grid"><button type="button" class="secondary" onclick="showDeviceLogs()">App debug log</button><button type="button" class="secondary" disabled title="BLE transport debug logs are not exposed by the Python client">Companion debug log</button><button type="button" class="secondary" onclick="runDeviceAction('stats',{stats_type:'radio'})">Radio statistics</button><button type="button" class="secondary" onclick="runDeviceAction('stats',{stats_type:'core'})">Core statistics</button><button type="button" class="secondary" onclick="runDeviceAction('stats',{stats_type:'packets'})">Packet statistics</button><button type="button" class="secondary" onclick="runDeviceAction('telemetry')">Self telemetry</button></div><pre id="device-debug-output" class="device-debug-output" hidden></pre></section>
<section class="card device-settings-card"><div class="panel-heading"><div><span class="eyebrow">ABOUT</span><h2>MeshCore Dashboard</h2></div></div><p class="settings-description">MeshCore device control with the Ollama-powered local assistant.</p><button class="secondary" type="button" onclick="showDeviceAbout()">About this dashboard</button></section>
</div>
</main>
<main id="map-view" class="map-workspace view-panel page-view" hidden>
<div class="map-layout">

<section class="card map-surface" aria-label="Mesh node map"><div class="map-search-overlay" id="map-search-overlay"><div class="map-peer-filters"><div class="node-search-label"><label for="map-node-search">Search</label><div class="search-input-row"><input type="search" id="map-node-search" placeholder="Search contacts" oninput="updateMapPeerFilters()"><button type="button" class="add-btn" data-kind="node" onclick="openAddDialog('node')">+ Add</button><details class="search-filter-menu"><summary aria-label="Sort and type filters" title="Sort and type filters"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16l-6.5 7.5v5l-3 1.5v-6.5L4 5Z"/></svg></summary><div class="search-filter-panel"><label for="map-node-sort">Sort<select id="map-node-sort" onchange="updateMapPeerFilters()"><option value="az">A-Z</option><option value="heard">Heard recently</option><option value="messages">Latest messages</option></select></label><label for="map-style-select">Map style<select id="map-style-select" onchange="applyMapStyle()"><option value="standard">Standard</option><option value="dark">Dark mode</option><option value="terrain">Terrain</option></select></label><label for="map-node-type-filter">Type<select id="map-node-type-filter" onchange="updateMapPeerFilters()"><option value="all">All</option><option value="favorites">Favorites</option><option value="users">Users</option><option value="repeaters">Repeaters</option><option value="room-servers">Room servers</option><option value="sensors">Sensors</option></select></label></div></details></div></div></div><div id="map-node-list" class="conversation-target-list"></div></div><div id="map-canvas"></div><div class="map-message" id="map-message">Waiting for map data...</div></section>
</div>
</main>
<footer id="console-dock" class="console-dock collapsed"><section class="card console-card"><div class="panel-heading" onpointerdown="startConsoleDrag(event)"><div><span class="eyebrow">SYSTEM ACTIVITY</span><h2>Console</h2></div><span class="live-tag">LIVE</span></div><pre id="console"></pre></section></footer>
<script>
const referenceRadioPresets=[
['Australia',915.8,250,10,5,20],['Australia (Narrow)',916.575,62.5,7,5,20],['Australia (Mid)',915.075,125,9,5,20],['Australia SA, WA, QLD',923.125,62.5,8,5,20],['Czech Republic',869.432,62.5,7,5,14],['EU 433MHz',433.65,250,11,5,20],['EU/UK (Long Range)',869.525,250,11,5,14],['EU/UK (Medium Range)',869.525,250,10,5,14],['EU/UK (Narrow)',869.618,62.5,8,5,14],['New Zealand',917.375,250,11,5,20],['New Zealand (Narrow)',917.375,62.5,7,5,20],['Portugal 433',433.375,62.5,9,5,20],['Portugal 869',869.618,62.5,7,5,14],['Russia Artyom (VVO)',864.281,62.5,8,6,20],['Russia Biysk (BSK)',869,62.5,8,5,20],['Russia Chelyabinsk (CEK)',868.731,62.5,8,6,20],['Russia Cherepovets (CEE)',868.57,62.5,7,8,20],['Russia Irkutsk (IKT)',868.731,62.5,7,7,20],['Russia Ivanovo (IWA)',868.731,62.5,8,8,20],['Russia Izhevsk (IJK)',868.732,62.5,8,8,20],['Russia Kaluga (KLF)',868.731,62.5,7,7,20],['Russia Kazan (KZN)',868.731,62.5,8,6,20],['Russia Khabarovsk (KHV)',864.281,62.5,8,6,20],['Russia Kirov (KVX)',868.731,62.5,8,8,20],['Russia Lipetsk (LPK)',868.95,62.5,9,7,20],['Russia Moscow (MOW)',868.731,62.5,7,7,20],['Russia Nizhny Novgorod (GOJ)',868.731,62.5,8,6,20],['Russia Novosibirsk (OVB)',869,62.5,9,8,20],['Russia Rostov-on-Don (ROV)',868.731,62.5,9,7,20],['Russia Ryazan (RZN)',868.88,62.5,9,5,20],['Russia Samara (KUF)',864.281,62.5,8,7,20],['Russia Saratov (GSV)',864.281,62.5,8,7,20],['Russia St. Petersburg (LED)',868.856,62.5,7,7,20],['Russia Tambov (TBW)',868.95,62.5,10,5,20],['Russia Tula (TYA)',868.731,62.5,8,7,20],['Russia Tver (KLD)',869.169,62.5,8,8,20],['Russia Ufa (UFA)',868.732,62.5,8,8,20],['Russia Volgograd (VOG)',869.525,62.5,7,7,20],['Russia Voronezh (VOZ)',868.731,62.5,8,6,20],['Russia Yekaterinburg (SVX)',869.046,62.5,7,7,20],['Switzerland',869.618,62.5,8,5,14],['USA Arizona',908.205,62.5,9,8,22],['USA Philly',902.25,500,11,5,22],['USA/Canada',910.525,62.5,7,5,22],['Vietnam',920.25,250,11,5,20],['Off-Grid 433',433,250,11,8,20],['Off-Grid 869',869.495,250,11,8,14],['Off-Grid 918',918,250,11,8,20]
];
function initializeRadioPresets(){let select=document.getElementById('radio-preset');if(!select)return;select.replaceChildren(new Option('Custom settings','custom'));referenceRadioPresets.forEach((preset,index)=>select.add(new Option(preset[0],String(index))))}
function applyRadioPreset(){let select=document.getElementById('radio-preset'),preset=referenceRadioPresets[Number(select.value)];if(!preset)return;document.getElementById('custom-radio-frequency').value=preset[1];document.getElementById('custom-radio-bandwidth').value=String(preset[2]);document.getElementById('custom-radio-spreading-factor').value=String(preset[3]);document.getElementById('custom-radio-coding-rate').value=String(preset[4]);document.getElementById('custom-tx-power').value=preset[5];let repeat=document.getElementById('client-repeat');if(preset[0].startsWith('Off-Grid')&&!repeat.disabled)repeat.checked=true}
function setIdentityRows(data){let list=document.getElementById('device-identity-details'),info=data.device_info||{},battery=data.battery_info||{};list.replaceChildren();let rows=[['Device ID',info.device_id||info.deviceId||data.public_key||'Unavailable'],['Battery',battery.level!=null?String(battery.level)+' mV':'Unavailable'],['Hardware',info.model||info.hw_model||info.board||info.manufacturer||'Unavailable'],['Firmware',info.ver||info.fw_ver||info.firmware||info.fw_build||'Unavailable'],['Public key',data.public_key||'Unavailable'],['Contacts',String(mapNodes.length)],['Channels',String(meshChannels.length)]];for(let [label,value] of rows){let term=document.createElement('dt'),detail=document.createElement('dd');term.textContent=label;detail.textContent=String(value);if(label==='Public key'&&value!=='Unavailable'){detail.title='Click to copy';detail.tabIndex=0;detail.onclick=()=>navigator.clipboard?.writeText(String(value));detail.onkeydown=event=>{if(event.key==='Enter')detail.click()}}list.append(term,detail)}}
function toggleDeviceIdentity(){let details=document.getElementById('device-identity-details'),button=document.getElementById('identity-toggle');details.hidden=!details.hidden;if(button){button.setAttribute('aria-expanded',String(!details.hidden));document.getElementById('identity-expand-icon').textContent=details.hidden?'+':'−'}}
async function loadDeviceSettings(){deviceSettingsLoaded=false;loadedDeviceSettings=null;let message=document.getElementById('device-settings-status');message.dataset.state='';message.textContent='Loading settings from device...';try{let response=await fetch('/api/device-settings'),data=await response.json();if(!response.ok)throw new Error(data.error||'Device settings could not be loaded');loadedDeviceSettings=data;document.getElementById('device-identity-name').textContent=data.name||'Unnamed device';document.getElementById('device-identity-status').textContent='CONNECTED';document.getElementById('device-identity-status').className='header-status connected';setIdentityRows(data);document.getElementById('device-name').value=data.name||'';document.getElementById('custom-radio-frequency').value=data.radio_freq??'';document.getElementById('custom-radio-bandwidth').value=String(data.radio_bw??125);document.getElementById('custom-radio-spreading-factor').value=String(data.radio_sf??7);document.getElementById('custom-radio-coding-rate').value=String(data.radio_cr??5);document.getElementById('custom-tx-power').value=data.tx_power??20;document.getElementById('rx-delay').value=data.rx_delay??0;document.getElementById('airtime-factor').value=data.airtime_factor??0;document.getElementById('advert-lat').value=data.adv_lat??'';document.getElementById('advert-lon').value=data.adv_lon??'';document.getElementById('gps-interval').value=data.custom_vars?.gps_interval??'';document.getElementById('gps-enabled').checked=data.custom_vars?.gps==='1';document.getElementById('path-hash-mode').value=String(Math.max(0,Number(data.path_hash_mode??0)));document.getElementById('manual-add-contacts').checked=Boolean(data.manual_add_contacts);document.getElementById('multi-acks').checked=Number(data.multi_acks)===1;document.getElementById('advert-location-policy').checked=Number(data.adv_loc_policy)!==0;document.getElementById('telemetry-mode-base').value=String(data.telemetry_mode_base??0);document.getElementById('telemetry-mode-loc').value=String(data.telemetry_mode_loc??0);document.getElementById('telemetry-mode-env').value=String(data.telemetry_mode_env??0);let flags=Number(data.auto_add_config?.config??0);document.getElementById('auto-add-overwrite').checked=Boolean(flags&1);document.getElementById('auto-add-users').checked=Boolean(flags&2);document.getElementById('auto-add-repeaters').checked=Boolean(flags&4);document.getElementById('auto-add-rooms').checked=Boolean(flags&8);document.getElementById('auto-add-sensors').checked=Boolean(flags&16);let repeat=document.getElementById('client-repeat'),repeatValue=data.device_info?.repeat;repeat.disabled=repeatValue===undefined||repeatValue===null;repeat.checked=Boolean(repeatValue);document.getElementById('client-repeat-row').hidden=repeat.disabled;document.getElementById('tx-power-limit').textContent='Device maximum: '+String(data.max_tx_power??30)+' dBm. Check your local radio regulations before applying a preset.';initializeRadioPresets();let presetIndex=referenceRadioPresets.findIndex(p=>Number(p[1])===Number(data.radio_freq)&&Number(p[2])===Number(data.radio_bw)&&Number(p[3])===Number(data.radio_sf)&&Number(p[4])===Number(data.radio_cr));document.getElementById('radio-preset').value=presetIndex<0?'custom':String(presetIndex);deviceSettingsLoaded=true;message.textContent='Device settings loaded.';message.dataset.state='success'}catch(error){message.textContent=error.message;message.dataset.state='error'}}
async function saveDeviceSettings(event){event.preventDefault();let message=document.getElementById('device-settings-status');if(!loadedDeviceSettings){message.textContent='Load settings from the connected device first.';message.dataset.state='error';return}let form=new FormData(document.getElementById('device-settings-form')),autoFlags=(document.getElementById('auto-add-overwrite').checked?1:0)|(document.getElementById('auto-add-users').checked?2:0)|(document.getElementById('auto-add-repeaters').checked?4:0)|(document.getElementById('auto-add-rooms').checked?8:0)|(document.getElementById('auto-add-sensors').checked?16:0),values={name:String(form.get('name')||'').trim(),radio_freq:Number(form.get('radio_freq')),radio_bw:Number(form.get('radio_bw')),radio_sf:Number(form.get('radio_sf')),radio_cr:Number(form.get('radio_cr')),tx_power:Number(form.get('tx_power')),rx_delay:Number(form.get('rx_delay')||0),airtime_factor:Number(form.get('airtime_factor')||0),adv_lat:Number(form.get('adv_lat')||loadedDeviceSettings.adv_lat||0),adv_lon:Number(form.get('adv_lon')||loadedDeviceSettings.adv_lon||0),telemetry_mode_base:Number(form.get('telemetry_mode_base')),telemetry_mode_loc:Number(form.get('telemetry_mode_loc')),telemetry_mode_env:Number(form.get('telemetry_mode_env')),adv_loc_policy:document.getElementById('advert-location-policy').checked?1:0,manual_add_contacts:document.getElementById('manual-add-contacts').checked,multi_acks:document.getElementById('multi-acks').checked,repeat:document.getElementById('client-repeat').checked,path_hash_mode:Number(form.get('path_hash_mode')),gps_enabled:document.getElementById('gps-enabled').checked,auto_add_flags:autoFlags};let interval=document.getElementById('gps-interval').value.trim();if(interval)values.gps_interval=Number(interval);message.dataset.state='';message.textContent='Saving settings to device...';try{let response=await fetch('/api/device-settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})}),data=await response.json();if(!response.ok)throw new Error(data.error||'Device settings could not be saved');await loadDeviceSettings();message.textContent='Device settings saved.';message.dataset.state='success'}catch(error){message.textContent=error.message;message.dataset.state='error'}}
async function runDeviceAction(action,options={}){if(action==='reboot'&&!window.confirm('Reboot the connected MeshCore device?'))return;if(action==='delete_paths'&&!window.confirm('Reset known routing paths for all contacts?'))return;let status=document.getElementById('device-action-status'),output=document.getElementById('device-debug-output');status.dataset.state='';status.textContent='Running device action...';try{let response=await fetch('/api/device-settings/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...options})}),data=await response.json();if(!response.ok)throw new Error(data.error||'Device action failed');status.textContent=data.message||'Action completed.';if(data.result!==undefined){output.hidden=false;output.textContent=JSON.stringify(data.result,null,2)}}catch(error){status.textContent=error.message;status.dataset.state='error'}}
async function exportDeviceGpx(type){let status=document.getElementById('gpx-export-status');status.textContent='Preparing GPX export...';try{let response=await fetch('/api/device-settings/export?type='+encodeURIComponent(type));if(!response.ok){let error=await response.json();throw new Error(error.error||'GPX export failed')}let blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='meshcore_'+type+'.gpx';link.click();URL.revokeObjectURL(url);status.textContent='GPX export downloaded.'}catch(error){status.textContent=error.message;status.dataset.state='error'}}
async function showDeviceLogs(){let output=document.getElementById('device-debug-output');try{let response=await fetch('/api/status'),data=await response.json();output.hidden=false;output.textContent=(data.logs||[]).join('\n')||'No app log entries.'}catch(error){output.textContent=error.message;output.hidden=false}}
function showDeviceAbout(){window.alert('MeshCore Dashboard\nMeshCore radio controls and local Ollama assistant.\nDevice controls depend on connected firmware capabilities.')}
const localRegionStorageKey='meshcore-dashboard-regions';
function readLocalRegions(){try{let parsed=JSON.parse(localStorage.getItem(localRegionStorageKey)||'{}');return {regions:Array.isArray(parsed.regions)?parsed.regions:[],defaultRegion:parsed.defaultRegion||''}}catch(error){return {regions:[],defaultRegion:''}}}
function writeLocalRegions(settings){try{localStorage.setItem(localRegionStorageKey,JSON.stringify(settings))}catch(error){let status=document.getElementById('region-status');if(status)status.textContent='Browser storage is unavailable; region changes will not persist.'}}
function renderLocalRegions(){let settings=readLocalRegions(),select=document.getElementById('default-region'),list=document.getElementById('local-region-list');if(!select||!list)return;select.replaceChildren(new Option('None',''));settings.regions.forEach(region=>select.add(new Option(region,region)));select.value=settings.defaultRegion;list.replaceChildren();if(!settings.regions.length){let empty=document.createElement('p');empty.className='settings-description';empty.textContent='No local regions added.';list.appendChild(empty);return}for(let region of settings.regions){let row=document.createElement('div');row.className='local-region-item';let name=document.createElement('span');name.textContent=region;let remove=document.createElement('button');remove.type='button';remove.className='secondary';remove.textContent='Remove';remove.onclick=()=>removeLocalRegion(region);row.append(name,remove);list.appendChild(row)}}
function saveDefaultRegion(){let settings=readLocalRegions();settings.defaultRegion=document.getElementById('default-region').value;writeLocalRegions(settings)}
function addLocalRegion(){let input=document.getElementById('new-region-name'),region=input.value.trim().toLowerCase(),status=document.getElementById('region-status');if(!/^[a-z0-9-]{1,30}$/.test(region)){status.textContent='Use 1-30 lowercase letters, numbers, or hyphens.';status.dataset.state='error';return}let settings=readLocalRegions();if(settings.regions.includes(region)){status.textContent='That region is already in the list.';status.dataset.state='error';return}settings.regions.push(region);settings.regions.sort();writeLocalRegions(settings);input.value='';status.textContent='Region added to this browser.';status.dataset.state='success';renderLocalRegions()}
function removeLocalRegion(region){let settings=readLocalRegions();settings.regions=settings.regions.filter(item=>item!==region);if(settings.defaultRegion===region)settings.defaultRegion='';writeLocalRegions(settings);renderLocalRegions()}
const syncConfigControlsWithGreeting=syncConfigControls;
syncConfigControls=function(){syncConfigControlsWithGreeting();let greetingToggle=document.getElementById('bot-greet-new-users');if(greetingToggle)greetingToggle.checked=Boolean(appConfig.bot.greet_new_users)}
const loadDeviceSettingsWithLimits=loadDeviceSettings;
loadDeviceSettings=async function(){await loadDeviceSettingsWithLimits();if(!loadedDeviceSettings)return;let maxPower=String(loadedDeviceSettings.max_tx_power??30),txPower=document.getElementById('custom-tx-power');txPower.max=maxPower;let pathHash=document.getElementById('path-hash-mode'),pathHashSupported=loadedDeviceSettings.device_info?.path_hash_mode!==undefined&&loadedDeviceSettings.device_info?.path_hash_mode!==null;pathHash.disabled=!pathHashSupported;pathHash.title=pathHashSupported?'':'Requires companion firmware v1.14 or newer'}
const saveDeviceSettingsWithLimits=saveDeviceSettings;
saveDeviceSettings=async function(event){let pathHash=document.getElementById('path-hash-mode'),wasDisabled=pathHash.disabled;pathHash.disabled=false;try{return await saveDeviceSettingsWithLimits(event)}finally{pathHash.disabled=wasDisabled}}
window.addEventListener('DOMContentLoaded',()=>{initializeRadioPresets();renderLocalRegions()});
</script>
<dialog id="add-dialog"><form id="add-form" method="dialog"><h3 id="add-title"></h3><p class="add-note" id="add-capacity"></p><div id="add-fields"></div><div class="add-status" id="add-status" role="status"></div><div class="add-actions"><button type="button" id="add-cancel">Close</button><button type="submit" id="add-submit">Add</button></div></form></dialog>
<script>
(function(){
let mode='node';
const dlg=document.getElementById('add-dialog'),form=document.getElementById('add-form'),fields=document.getElementById('add-fields'),statusBox=document.getElementById('add-status');
const field=(id,label,type,extra)=>'<label for="'+id+'">'+label+'</label><input id="'+id+'" type="'+(type||'text')+'" autocomplete="off" '+(extra||'')+'>';
function capacityText(kind){
  let used=kind==='node'?mapNodes.length:meshChannels.length,max=kind==='node'?peerLimits.max_contacts:peerLimits.max_channels;
  return {text:(kind==='node'?'Contacts ':'Channels ')+used+(max?' / '+max:''),full:Boolean(max)&&used>=max};
}
window.updateAddCapacity=function(){
  for(let el of document.querySelectorAll('.rail-capacity')){let c=capacityText(el.dataset.kind);el.textContent=c.text;el.dataset.full=String(c.full)}
  for(let btn of document.querySelectorAll('.add-btn')){let c=capacityText(btn.dataset.kind);btn.disabled=c.full;btn.title=c.full?'Limit reached on this device':'Add'}
};
function renderFields(){
  if(mode==='channel'){
    fields.innerHTML='<label for="add-kind">Channel type</label><select id="add-kind"><option value="public">Public channel</option><option value="private">Private channel</option><option value="hashtag">Hashtag channel</option></select><div id="add-channel-extra"></div>';
    const kind=document.getElementById('add-kind'),extra=document.getElementById('add-channel-extra');
    const draw=()=>{
      if(kind.value==='public')extra.innerHTML='<p class="add-note">Joins the standard MeshCore Public channel.</p>';
      else if(kind.value==='private')extra.innerHTML=field('add-name','Channel name','text','maxlength="32"')+field('add-secret','Secret key (optional, 32 hex)','text','maxlength="32" spellcheck="false"')+'<p class="add-note">Leave the key blank to generate a new random one. Share the key with people you want in the channel.</p>';
      else extra.innerHTML=field('add-name','Hashtag name','text','maxlength="31" placeholder="#example"')+'<p class="add-note">Anyone who joins the same hashtag name can talk in it. Letters, numbers and dashes only.</p>';
    };
    kind.onchange=draw;draw();
  }else{
    fields.innerHTML='<label for="add-method">Add using</label><select id="add-method"><option value="manual">Public key</option><option value="card">Contact link</option></select><div id="add-node-extra"></div>';
    const method=document.getElementById('add-method'),extra=document.getElementById('add-node-extra');
    const draw=()=>{
      if(method.value==='card')extra.innerHTML='<label for="add-card">meshcore:// contact link</label><textarea id="add-card" spellcheck="false" placeholder="meshcore://..."></textarea>';
      else extra.innerHTML='<label for="add-node-type">Type</label><select id="add-node-type"><option value="repeater">Repeater</option><option value="companion">Companion / chat node</option><option value="room">Room server</option><option value="sensor">Sensor</option></select>'+field('add-name','Name','text','maxlength="31"')+field('add-key','Public key (64 hex)','text','maxlength="64" spellcheck="false"')+field('add-lat','Latitude (optional)','number','step="any" min="-90" max="90"')+field('add-lon','Longitude (optional)','number','step="any" min="-180" max="180"');
    };
    method.onchange=draw;draw();
  }
}
window.openAddDialog=function(kind){
  mode=kind;
  document.getElementById('add-title').textContent=kind==='channel'?'Add channel':'Add node / repeater';
  let c=capacityText(kind);document.getElementById('add-capacity').textContent=c.text+(c.full?' - limit reached':'');
  statusBox.textContent='';statusBox.dataset.state='';
  renderFields();
  dlg.showModal();
};
document.getElementById('add-cancel').onclick=()=>dlg.close();
const value=id=>{let el=document.getElementById(id);return el?el.value.trim():''};
form.onsubmit=async event=>{
  event.preventDefault();
  let url,body;
  if(mode==='channel'){url='/api/channels/add';body={type:value('add-kind'),name:value('add-name'),secret:value('add-secret')}}
  else if(value('add-method')==='card'){url='/api/contacts/add';body={card:value('add-card')}}
  else{url='/api/contacts/add';body={node_type:value('add-node-type'),name:value('add-name'),public_key:value('add-key'),latitude:value('add-lat'),longitude:value('add-lon')}}
  let submit=document.getElementById('add-submit');submit.disabled=true;statusBox.dataset.state='';statusBox.textContent='Sending to device...';
  try{
    let response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(data.error||'Request failed');
    statusBox.dataset.state='success';
    statusBox.textContent=data.secret?'Added '+data.name+'. Secret key: '+data.secret:'Added.';
    if(typeof peers==='function')await peers();
    if(!data.secret)setTimeout(()=>{if(dlg.open)dlg.close()},900);
  }catch(error){statusBox.dataset.state='error';statusBox.textContent=error.message}
  finally{submit.disabled=false}
};
function installButtons(){
  const spots=[['#nodes-view .conversation-rail','node'],['#channels-view .conversation-rail','channel'],['#map-view .map-rail','node']];
  for(let [selector,kind] of spots){
    let heading=document.querySelector(selector+' .panel-heading');
    if(!heading||heading.querySelector('.add-btn'))continue;
    let tools=document.createElement('div'),btn=document.createElement('button'),cap=document.createElement('span');
    tools.className='heading-tools';btn.type='button';btn.className='add-btn';btn.dataset.kind=kind;btn.textContent='+ Add';btn.onclick=()=>openAddDialog(kind);
    cap.className='rail-capacity';cap.dataset.kind=kind;
    heading.firstElementChild.append(cap);
    while(heading.children.length>1)tools.append(heading.children[1]);
    tools.prepend(btn);heading.append(tools);
  }
  updateAddCapacity();
}
installButtons();
})();
</script>
<div id="msg-actions" role="dialog" aria-modal="true" aria-label="Message Actions"><div class="sheet" id="msg-sheet"></div></div>
<script>
(function(){
const overlay=document.getElementById('msg-actions'),sheet=document.getElementById('msg-sheet');
const ICONS={copy:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',reply:'<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v3"/>',path:'<circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="M6 16V8a2 2 0 0 1 2-2h8M18 8v8"/>',user:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',signal:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',block:'<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',map:'<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'};
function close(){overlay.classList.remove('open')}
overlay.addEventListener('click',event=>{if(event.target===overlay)close()});
document.addEventListener('keydown',event=>{if(event.key==='Escape')close()});
function bodyText(type,text){if(type!=='channel')return text;return text.replace(/^\s*(?:\[[^\]]{1,40}\]|[^:\r\n\[]{1,40}:)\s+/,'')}
async function copyText(text){
  try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return true}}catch(e){}
  let area=document.createElement('textarea');area.value=text;area.style.cssText='position:fixed;opacity:0;top:0;left:0';document.body.append(area);area.focus();area.select();
  let ok=false;try{ok=document.execCommand('copy')}catch(e){}area.remove();return ok;
}
function action(label,icon,handler,danger){
  let button=document.createElement('button');button.type='button';button.className='sheet-action'+(danger?' danger':'');
  button.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true">'+ICONS[icon]+'</svg><span></span>';button.querySelector('span').textContent=label;
  button.onclick=handler;return button;
}
function showPaths(message,sender){
  let rows=[['Sender',sender||'Unknown'],['Received',fmtTime(message.timestamp)||'Unknown'],['Hops',Number.isInteger(message.path_len)?(message.path_len===0?'Direct (0 hops)':message.path_len+' hop'+(message.path_len===1?'':'s')):'Not reported'],['Signal (SNR)',typeof message.snr==='number'?message.snr.toFixed(2)+' dB':'Not reported']];
  let box=document.createElement('div');box.className='sheet-paths';
  for(let [label,value] of rows){let row=document.createElement('div'),a=document.createElement('span'),b=document.createElement('strong');a.textContent=label;b.textContent=value;row.append(a,b);box.append(row)}
  let nodes=Array.isArray(message.path_nodes)?message.path_nodes:[];
  if(nodes.length){let route=document.createElement('div');route.className='muted';route.style.display='block';route.textContent='Route: '+[sender||'Sender',...nodes.map(n=>(n.name||'Unknown repeater')+' ('+n.hash+')'),'You'].join(' → ');box.append(route)}
  else{let note=document.createElement('div');note.className='muted';note.textContent=message.path_len===0?'Received directly, no repeaters in the path.':'The radio did not report the repeaters for this message.';box.append(note)}
  sheet.querySelector('.sheet-paths')?.remove();sheet.append(box);
}
window.openNodeActions=function(peer){
  window.openMessageActions('node',String(peer.id),{text:String(peer.name||peer.id)+' \u00b7 '+peerTypeLabel(peer.type),direction:'incoming'},'',[],peer);
};
window.openMessageActions=function(type,id,message,sender,blocked,nodePeer){
  let nodeMode=!!nodePeer;
  sheet.replaceChildren();
  let head=document.createElement('div'),closeButton=document.createElement('button'),title=document.createElement('span');
  head.className='sheet-head';closeButton.type='button';closeButton.textContent='\u00d7';closeButton.setAttribute('aria-label','Close');closeButton.onclick=close;title.textContent=nodeMode?'Node Actions':'Message Actions';head.append(closeButton,title);
  let preview=document.createElement('div');preview.className='sheet-preview';preview.textContent=message.text;
  sheet.append(head,preview);
  let text=bodyText(type,message.text),incoming=message.direction!=='outgoing',channelSender=type==='channel'&&incoming?sender:'';
  if(nodeMode)nodePeer=mapNodes.find(item=>String(item.id)===String(id))||nodePeer;
  let peerId=type==='node'?String(id):'',peerName=type==='channel'&&message.direction!=='outgoing'?String(sender||''):'';
  let knownPeer=type==='node'?mapNodes.find(item=>String(item.id)===peerId):(peerName?mapNodes.find(item=>String(item.name||'').trim().toLowerCase()===peerName.trim().toLowerCase()):null);
  let isRepeater=!nodeMode&&Number(knownPeer?.type)===2;
  if(!nodeMode)sheet.append(action('Copy Text','copy',async()=>{let ok=await copyText(text);close();showToast(ok?'Copied':'Copy failed',ok?'Message text copied.':'Your browser blocked copying.')}));
  if(type==='channel'&&!isRepeater){
    if(channelSender)sheet.append(action('Reply','reply',()=>{close();let input=document.getElementById('channel-message');input.value='@['+channelSender+'] ';input.focus()}));
    if(incoming)sheet.append(action('View Message Paths','path',()=>showPaths(message,channelSender)));
    if(channelSender){let isBlocked=blocked.includes(channelSender.toLowerCase());sheet.append(action(isBlocked?'Unblock Sender':'Block Sender','block',async()=>{
      if(!isBlocked&&!confirm('Block '+channelSender+'? Their messages will be hidden and ignored.'))return;
      let response=await fetch('/api/block-sender',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:channelSender,blocked:!isBlocked})});
      close();if(!response.ok){showToast('Error','Could not update block list.');return}
      showToast(isBlocked?'Unblocked':'Blocked',channelSender);history('channel',id,'channel-chat-history')}))}
  }
  if(!isRepeater){
    sheet.append(action('Node Telemetry','signal',()=>{
      if(!knownPeer){showToast('Telemetry','Add this sender as a contact first.');return}
      sheet.querySelector('.sheet-paths')?.remove();let box=document.createElement('div');box.className='sheet-paths';let list=document.createElement('dl');list.className='peer-inline-detail';box.append(list);sheet.append(box);renderPeerDetails(String(knownPeer.id),list)}));
  if(knownPeer){
    sheet.append(action('View on Map','map',()=>{let lat=Number(knownPeer.latitude),lon=Number(knownPeer.longitude);if(!Number.isFinite(lat)||!Number.isFinite(lon)||(lat===0&&lon===0)){showToast('No location',(knownPeer.name||'This node')+' has not reported a location.');return}close();focusMapPoint(lat,lon);setTimeout(()=>{if(dashboardMap)dashboardMap.invalidateSize();focusMapPoint(lat,lon)},250)}));
    let post=async(url,body)=>{let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let d=await r.json().catch(()=>({}));d._status=r.status;return d};
    sheet.append(action('Set Path','path',()=>{
      sheet.querySelector('.sheet-setpath')?.remove();let box=document.createElement('div');box.className='sheet-paths sheet-setpath';
      let inp=document.createElement('input');inp.placeholder='Repeater hashes in order, e.g. a1,b2,c3';
      let save=document.createElement('button');save.type='button';save.textContent='Save path';
      save.onclick=async()=>{let d=await post('/api/contacts/path',{target:String(knownPeer.id),action:'set',path:inp.value});if(d._status===200){close();showToast('Path set',(knownPeer.name||knownPeer.id)+': route saved');if(typeof peers==='function')peers()}else showToast('Error',d.error||'Could not set path')};
      box.append(inp,save);sheet.append(box);inp.focus()}));
    sheet.append(action('Reset Path','path',async()=>{if(!confirm('Reset the route to '+(knownPeer.name||knownPeer.id)+'? The next message will use flood routing.'))return;let d=await post('/api/contacts/path',{target:String(knownPeer.id),action:'reset'});close();if(d._status===200){showToast('Path reset',(knownPeer.name||knownPeer.id)+' will be re-discovered');if(typeof peers==='function')peers()}else showToast('Error',d.error||'Could not reset path')}));
    if(Number(knownPeer.type)===2)sheet.append(action('Ping','signal',async()=>{showToast('Ping','Pinging '+(knownPeer.name||knownPeer.id)+'...');let d=await post('/api/contacts/ping',{target:String(knownPeer.id)});close();if(d.ok)showToast('Ping reply',(knownPeer.name||knownPeer.id)+': '+d.rtt_ms+' ms'+(Number.isInteger(d.hops)&&d.hops>=0?', '+d.hops+' hop'+(d.hops===1?'':'s'):''));else showToast('Ping failed',d.error||'No reply')}));
    if([2,3].includes(Number(knownPeer.type)))sheet.append(action('Manage','user',()=>{
      sheet.querySelector('.sheet-manage')?.remove();let box=document.createElement('div');box.className='sheet-paths sheet-manage';
      let pw=document.createElement('input');pw.type='password';pw.placeholder='Admin password';pw.autocomplete='off';
      let login=document.createElement('button');login.type='button';login.textContent='Log in';
      let out=document.createElement('pre');out.style.cssText='white-space:pre-wrap;margin:6px 0;max-height:160px;overflow:auto';
      let cmd=document.createElement('input');cmd.placeholder='Command (e.g. ver, get name, advert)';cmd.disabled=true;
      let send=document.createElement('button');send.type='button';send.textContent='Send';send.disabled=true;
      let quick=document.createElement('div');quick.style.cssText='display:flex;flex-wrap:wrap;gap:4px;margin:6px 0';
      let run=async text=>{if(!text)return;out.textContent+='> '+text+'\\n';send.disabled=true;let d=await post('/api/contacts/manage',{target:String(knownPeer.id),action:'command',command:text});send.disabled=false;out.textContent+=(d.reply||d.error||'No reply')+'\\n';out.scrollTop=out.scrollHeight};
      for(let c of ['ver','clock','get name','get radio','neighbors','advert','reboot']){let b=document.createElement('button');b.type='button';b.textContent=c;b.disabled=true;b.onclick=()=>{if(c==='reboot'&&!confirm('Reboot '+(knownPeer.name||knownPeer.id)+'?'))return;run(c)};quick.append(b)}
      let unlock=()=>{cmd.disabled=false;send.disabled=false;quick.querySelectorAll('button').forEach(b=>b.disabled=false)};
      login.onclick=async()=>{login.disabled=true;out.textContent='Logging in...\\n';let d=await post('/api/contacts/manage',{target:String(knownPeer.id),action:'login',password:pw.value});pw.value='';login.disabled=false;if(d.ok){out.textContent='Logged in.\\n';unlock()}else out.textContent=(d.error||'Login failed')+'\\n'};
      send.onclick=()=>{let t=cmd.value.trim();cmd.value='';run(t)};cmd.onkeydown=e=>{if(e.key==='Enter')send.click()};
      box.append(pw,login,quick,cmd,send,out);sheet.append(box);pw.focus()}));
  }
  }
  if((type==='node'||peerName)&&!isRepeater){
    if(knownPeer)sheet.append(action('Remove Contact','trash',async()=>{
      if(!confirm('Remove '+(knownPeer.name||knownPeer.id)+' from your contacts?'))return;
      let response=await fetch('/api/contacts/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:String(knownPeer.id)})}),data=await response.json().catch(()=>({}));
      close();if(!response.ok){showToast('Error',data.error||'Could not remove contact');return}
      showToast('Removed',knownPeer.name||String(knownPeer.id));if(typeof peers==='function')peers()},true));
    else sheet.append(action('Add Contact','user',async()=>{
      let response=await fetch('/api/contacts/add-heard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:peerId,name:peerName})}),data=await response.json().catch(()=>({}));
      close();if(response.status===404&&typeof window.openAddDialog==='function'){showToast('Not heard yet','Add this contact with its link or public key.');window.openAddDialog('node');return}if(!response.ok){showToast('Error',data.error||'Could not add contact');return}
      showToast('Added',data.name||peerName||peerId);if(typeof peers==='function')peers()}));
  }
  if(nodeMode){
    sheet.append(action(nodePeer.archived?'Unarchive Chat':'Archive Chat','path',async()=>{
      let response=await fetch('/api/chat-management',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_type:'node',target:id,action:'archive'})});
      let data=await response.json();
      close();if(!response.ok){showToast('Error',data.error||'Chat could not be updated');return}
      await peers();if(selectedNodeId===id)await history('node',id,'node-chat-history');
    }));
    overlay.classList.add('open');return;
  }
  sheet.append(action('Delete','trash',async()=>{
    if(!confirm('Delete this message from the dashboard?'))return;
    let response=await fetch('/api/delete-message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_type:type,target:id,id:message.id})});
    close();if(!response.ok){let data=await response.json().catch(()=>({}));showToast('Error',data.error||'Delete failed');return}
    history(type,id,type==='channel'?'channel-chat-history':'node-chat-history')},true));
  overlay.classList.add('open');
};
})();
</script>
<script>
(function(){const box=document.getElementById('map-search-overlay'),input=document.getElementById('map-node-search');if(!box||!input)return;
input.addEventListener('focus',()=>box.classList.add('open'));
document.addEventListener('pointerdown',e=>{if(!box.contains(e.target))box.classList.remove('open')});
document.addEventListener('keydown',e=>{if(e.key==='Escape')box.classList.remove('open')});
})();
</script>
</body></html>'''


async def index_handler(request):
    available_models = list(app_state["available_models"])
    if app_state["selected_model"] not in available_models:
        available_models.append(app_state["selected_model"])
    options = "".join(
        f'<option value="{html.escape(model)}">{html.escape(model)}</option>'
        for model in available_models
    )
    return web.Response(
        text=PAGE.replace("{{MODEL_OPTIONS}}", options),
        content_type="text/html",
    )


TILE_SOURCES = {
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19),
    "topo": ("https://tile.opentopomap.org/{z}/{x}/{y}.png", 17),
}
TILE_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "meshcore-madhat" / "tiles"
TILE_USER_AGENT = "Meshcore-Madhat/1.0 (local dashboard tile cache)"
# 1x1 transparent PNG, served when a tile is neither cached nor reachable.
def _blank_png():
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + b"\x00\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(row)) + chunk(b"IEND", b"")


BLANK_TILE = _blank_png()
TILE_OFFLINE_RETRY_SECONDS = 60
tile_offline_until = 0.0


async def tile_handler(request):
    global tile_offline_until
    source = TILE_SOURCES.get(request.match_info["source"])
    try:
        z, x, y = (int(request.match_info[k]) for k in ("z", "x", "y"))
    except ValueError:
        raise web.HTTPBadRequest()
    if source is None or not 0 <= z <= source[1] or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise web.HTTPNotFound()
    name = request.match_info["source"]
    path = TILE_CACHE_DIR / name / str(z) / str(x) / f"{y}.png"
    headers = {"Cache-Control": "public, max-age=86400"}
    try:
        if path.is_file():
            return web.Response(body=await asyncio.to_thread(path.read_bytes), content_type="image/png", headers=headers)
    except OSError:
        pass
    if time.monotonic() >= tile_offline_until:
        try:
            async with ClientSession(timeout=ClientTimeout(total=8), headers={"User-Agent": TILE_USER_AGENT}) as session:
                async with session.get(source[0].format(z=z, x=x, y=y)) as response:
                    if response.status == 200:
                        data = await response.read()
                        if data[:4] == b"\x89PNG":
                            def store():
                                path.parent.mkdir(parents=True, exist_ok=True)
                                tmp = path.with_suffix(".tmp")
                                tmp.write_bytes(data)
                                tmp.replace(path)
                            try:
                                await asyncio.to_thread(store)
                            except OSError:
                                pass
                            return web.Response(body=data, content_type="image/png", headers=headers)
        except Exception as error:
            tile_offline_until = time.monotonic() + TILE_OFFLINE_RETRY_SECONDS
            log_to_dash(f"Map tiles unavailable, using cache only: {type(error).__name__}")
    return web.Response(body=BLANK_TILE, content_type="image/png", headers={"Cache-Control": "no-store"})


async def dashboard_logo_handler(request):
    logo_path = Path(__file__).resolve().with_name("dashboard-logo.png")
    if not logo_path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(logo_path, headers={"Cache-Control": "no-cache"})


async def status_handler(request):
    return web.json_response({
        "is_connected": app_state["is_connected"],
        "logs": app_state["logs"],
        "trace_events": app_state["trace_events"],
        "ollama_running": app_state["ollama_running"],
        "incoming_message_count": app_state["incoming_message_count"],
        "latest_incoming_message": app_state["latest_incoming_message"],
    })


bot_console_history = []
bot_console_lock = asyncio.Lock()


async def bot_console_handler(request):
    # Isolated from the radio: never touches the MeshCore device or chat logs.
    try:
        data = await request.json()
    except Exception:
        data = {}
    if data.get("reset"):
        bot_console_history.clear()
        return web.json_response({"ok": True})
    message = str(data.get("message", "")).strip()[:2000]
    if not message:
        return web.json_response({"error": "Enter a message"}, status=400)
    if not app_state["ollama_running"]:
        return web.json_response({"error": "Ollama is not running"}, status=503)
    system = (
        f"You are {bot_settings['name']}, an AI assistant. "
        "Use this communication style only for tone and phrasing: "
        f"{bot_settings['personality']}. "
        f"The current date and time is {datetime.now():%A, %B %d, %Y at %I:%M %p}. "
        "You have no internet access; never invent facts."
    )
    async with bot_console_lock:
        bot_console_history.append({"role": "user", "content": message})
        del bot_console_history[:-MAX_HISTORY_MESSAGES]
        try:
            reply = await asyncio.to_thread(
                sync_generate,
                [{"role": "system", "content": system}, *bot_console_history],
                app_state["selected_model"],
            )
        except Exception as error:
            bot_console_history.pop()
            log_to_dash(f"Bot console error: {error}")
            return web.json_response({"error": "The model could not respond"}, status=502)
        reply = reply.strip()
        bot_console_history.append({"role": "assistant", "content": reply})
    return web.json_response({"reply": reply, "model": app_state["selected_model"]})


async def ollama_toggle_handler(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    enabled = bool(data.get("enabled", not app_state["ollama_running"]))
    if enabled:
        await update_available_models()
    else:
        await stop_ollama_server()
    return web.json_response({"ollama_running": app_state["ollama_running"]})


async def ollama_models_handler(request):
    if app_state["ollama_running"]:
        try:
            await fetch_available_models()
        except Exception as error:
            log_to_dash(f"Failed to refresh Ollama models: {error}")

    return web.json_response({
        "models": app_state.get("models_info", []),
        "available_models": list(app_state.get("available_models", [])),
        "selected_model": app_config.get("model", DEFAULT_MODEL),
        "ollama_running": app_state["ollama_running"],
        "busy": app_state.get("model_action_busy", False),
    })


async def ollama_model_progress_handler(request):
    return web.json_response({
        "progress": app_state.get("model_progress"),
        "busy": app_state.get("model_action_busy", False),
    })


async def ollama_pull_model_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    model_name = str(data.get("model", "")).strip()
    if not model_name:
        return web.json_response({"error": "Model name is required"}, status=400)

    if not re.match(r"^[a-zA-Z0-9_\-./:]+$", model_name):
        return web.json_response({"error": "Invalid characters in model name"}, status=400)

    if app_state.get("model_action_busy"):
        return web.json_response(
            {"error": "Another model operation is already in progress. Please wait."},
            status=409,
        )

    if not app_state["ollama_running"]:
        await update_available_models()
        if not app_state["ollama_running"]:
            return web.json_response(
                {"error": "Ollama server is not running and could not be started."},
                status=400,
            )

    app_state["model_action_busy"] = True
    app_state["model_progress"] = {
        "model": model_name,
        "phase": "downloading",
        "status": "Starting download",
        "completed": 0,
        "total": 0,
    }
    log_to_dash(f"Starting download of Ollama model '{model_name}'...")

    try:
        def pull_with_progress():
            for response in ollama.pull(model_name, stream=True):
                if isinstance(response, dict):
                    status = str(response.get("status", ""))
                    completed = response.get("completed", 0)
                    total = response.get("total", 0)
                else:
                    status = str(getattr(response, "status", ""))
                    completed = getattr(response, "completed", 0)
                    total = getattr(response, "total", 0)
                status_lower = status.lower()
                phase = (
                    "installing"
                    if status_lower in {
                        "verifying sha256 digest",
                        "writing manifest",
                        "removing any unused layers",
                        "success",
                    }
                    else "downloading"
                )
                app_state["model_progress"] = {
                    "model": model_name,
                    "phase": phase,
                    "status": status or ("Installing model" if phase == "installing" else "Downloading model"),
                    "completed": completed if isinstance(completed, (int, float)) else 0,
                    "total": total if isinstance(total, (int, float)) else 0,
                }

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, pull_with_progress)
        app_state["model_progress"] = {
            "model": model_name,
            "phase": "installing",
            "status": "Installation complete",
            "completed": 1,
            "total": 1,
        }
        log_to_dash(f"Model '{model_name}' downloaded successfully.")
        await fetch_available_models()
        return web.json_response({
            "success": True,
            "message": f"Model '{model_name}' downloaded successfully.",
            "models": app_state.get("models_info", []),
            "available_models": list(app_state.get("available_models", [])),
            "selected_model": app_config.get("model", DEFAULT_MODEL),
        })
    except Exception as error:
        app_state["model_progress"] = {
            "model": model_name,
            "phase": "error",
            "status": str(error),
            "completed": 0,
            "total": 0,
        }
        log_to_dash(f"Failed to download model '{model_name}': {error}")
        return web.json_response(
            {"error": f"Failed to download '{model_name}': {error}"},
            status=500,
        )
    finally:
        app_state["model_action_busy"] = False


async def ollama_delete_model_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    model_name = str(data.get("model", "")).strip()
    if not model_name:
        return web.json_response({"error": "Model name is required"}, status=400)

    if app_state.get("model_action_busy"):
        return web.json_response(
            {"error": "Another model operation is already in progress. Please wait."},
            status=409,
        )

    if not app_state["ollama_running"]:
        return web.json_response(
            {"error": "Ollama server is not running."},
            status=400,
        )

    app_state["model_action_busy"] = True
    log_to_dash(f"Deleting Ollama model '{model_name}'...")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: ollama.delete(model_name))
        log_to_dash(f"Model '{model_name}' deleted successfully.")
        await fetch_available_models()

        if app_config.get("model") == model_name:
            remaining = [m for m in app_state.get("available_models", []) if m != model_name]
            fallback = remaining[0] if remaining else DEFAULT_MODEL
            app_config["model"] = fallback
            app_state["selected_model"] = fallback
            try:
                write_app_config(app_config)
                log_to_dash(f"Active model switched to '{fallback}'.")
            except Exception as write_err:
                log_to_dash(f"Could not update config.json after deleting model: {write_err}")

        return web.json_response({
            "success": True,
            "message": f"Model '{model_name}' deleted successfully.",
            "models": app_state.get("models_info", []),
            "available_models": list(app_state.get("available_models", [])),
            "selected_model": app_config.get("model", DEFAULT_MODEL),
        })
    except Exception as error:
        log_to_dash(f"Failed to delete model '{model_name}': {error}")
        return web.json_response(
            {"error": f"Failed to delete '{model_name}': {error}"},
            status=500,
        )
    finally:
        app_state["model_action_busy"] = False


async def config_handler(request):
    return web.json_response(app_config)


async def update_config_handler(request):
    global app_config, bot_settings

    try:
        payload = await request.json()
        config = validate_app_config(payload)
    except (ValueError, json.JSONDecodeError) as error:
        return web.json_response({"error": str(error)}, status=400)
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    try:
        write_app_config(config)
    except OSError as error:
        log_to_dash(f"Failed to save config.json: {error}")
        return web.json_response({"error": "Could not write config.json"}, status=500)

    app_config = config
    bot_settings = app_config["bot"]
    app_state["selected_model"] = app_config["model"]
    return web.json_response(app_config)


async def local_weather_handler(request):
    city = app_config["weather"]["city"]
    state = app_config["weather"]["state"]
    if not city:
        return web.json_response({"error": "Enter a city in Weather settings"}, status=400)

    try:
        location_query = f"{city}, {state}" if state else city
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            async with session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location_query, "count": 5, "language": "en", "format": "json"},
            ) as response:
                response.raise_for_status()
                places = (await response.json()).get("results", [])
            if not places:
                return web.json_response({"error": f"Could not find {location_query}"}, status=404)

            requested_state = state.casefold()
            place = (
                next(
                    (item for item in places if str(item.get("admin1", "")).casefold() == requested_state),
                    places[0],
                )
                if state
                else places[0]
            )
            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m",
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "timezone": "auto",
                },
            ) as response:
                response.raise_for_status()
                current = (await response.json())["current"]

        return web.json_response({
            "location": ", ".join(
                str(place[key]) for key in ("name", "admin1", "country") if place.get(key)
            ),
            "short_location": f"{place.get('name') or city}, {state}" if state else str(place.get("name") or city),
            "temperature_f": current["temperature_2m"],
            "humidity": current.get("relative_humidity_2m"),
            "wind_mph": current.get("wind_speed_10m"),
            "weather_code": int(current["weather_code"]),
            "condition": WEATHER_CODES.get(int(current["weather_code"]), "conditions unavailable"),
            "updated": current.get("time"),
            "source": "Open-Meteo",
        })
    except asyncio.TimeoutError:
        return web.json_response({"error": "Weather lookup timed out"}, status=504)
    except Exception as error:
        log_to_dash(f"Local weather lookup failed: {error}")
        return web.json_response({"error": "Could not load local weather"}, status=502)


def schedule_restart(delay=0.3):
    async def restart_dashboard():
        await asyncio.sleep(delay)
        await disconnect_hardware()
        os.environ["MESHC_OPS_RESTARTING"] = "1"
        os.execv(sys.executable, [sys.executable, *sys.argv])

    return asyncio.create_task(restart_dashboard())


async def restart_dashboard_handler(request):
    schedule_restart()
    return web.json_response({"restarting": True}, status=202)


async def update_app_handler(request):
    repo_dir = Path(__file__).resolve().parent
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return web.json_response(
            {"error": status.stderr.strip() or "Could not inspect repository"},
            status=500,
        )
    # config.json is rewritten at runtime whenever settings are saved, so a
    # dirty config.json alone shouldn't block updates; stash it around the
    # pull instead of discarding the operator's saved settings.
    changed_paths = [line[3:].strip() for line in status.stdout.splitlines()]
    config_name = CONFIG_FILE_PATH.name
    if any(path != config_name for path in changed_paths):
        return web.json_response(
            {
                "error": (
                    "Update blocked: commit or discard local changes before "
                    "updating."
                )
            },
            status=409,
        )

    config_dirty = config_name in changed_paths
    if config_dirty:
        stash = subprocess.run(
            ["git", "stash", "push", "--quiet", "--", config_name],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if stash.returncode != 0:
            return web.json_response(
                {"error": stash.stderr.strip() or "Could not preserve local config.json changes"},
                status=500,
            )

    branch_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    branch = branch_result.stdout.strip() or "main"
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    pull = subprocess.run(
        ["git", "pull", "--ff-only", "origin", branch],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if pull.returncode != 0:
        if config_dirty:
            subprocess.run(
                ["git", "stash", "pop", "--quiet"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
        return web.json_response(
            {"error": pull.stderr.strip() or pull.stdout.strip() or "Update failed"},
            status=502,
        )

    if config_dirty:
        pop = subprocess.run(
            ["git", "stash", "pop", "--quiet"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if pop.returncode != 0:
            log_to_dash(
                f"Could not restore local config.json after update: {pop.stderr.strip()}"
            )

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if before == after:
        return web.json_response(
            {"updated": False, "message": "The app is already up to date."}
        )

    schedule_restart(delay=getattr(request, "restart_delay", 0.3))
    return web.json_response({"updated": True, "message": "Update installed. Restarting dashboard."}, status=202)


async def check_for_update_handler(request):
    repo_dir = Path(__file__).resolve().parent
    branch_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    branch = branch_result.stdout.strip() or "main"
    fetch = subprocess.run(
        ["git", "fetch", "--quiet", "origin", branch],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch.returncode != 0:
        return web.json_response(
            {"error": fetch.stderr.strip() or "Could not check the repository for updates"},
            status=502,
        )

    local = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    remote = subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    return web.json_response({
        "update_available": bool(local) and bool(remote) and local != remote,
        "branch": branch,
    })


async def peers_handler(request):
    # The dashboard polls this endpoint every 10s; forcing a fresh device
    # fetch each time competes with telemetry_loop's own refresh and floods
    # the radio with get_contacts calls, which was tripping ERR_CODE_BAD_STATE.
    # Serve the cached snapshot instead and let telemetry_loop keep it warm.
    nodes = []
    for node_id, entry in app_state["contacts"].items():
        coordinates = coordinates_from_entry(entry)
        contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
        if not isinstance(contact, dict):
            contact = {}
        messages = chat_history.get(chat_key("node", str(node_id)), [])
        latest_message = messages[-1] if messages else {}
        nodes.append({
            "id": str(node_id),
            "name": display_name(node_id, entry),
            **get_chat_metadata("node", str(node_id)),
            "public_key": contact.get("public_key", str(node_id)),
            "type": contact.get("type", entry.get("type") if isinstance(entry, dict) else None),
            "last_heard": max(int(contact.get("last_advert") or contact.get("last_heard") or 0), advert_seen_at.get(str(contact.get("public_key", node_id)), 0)),
            "hops": contact.get("out_path_len"),
            "last_message_at": latest_message.get("sort_timestamp", 0),
            "latitude": coordinates[0] if coordinates else None,
            "longitude": coordinates[1] if coordinates else None,
        })
    return web.json_response({
        "nodes": nodes,
        "channels": [
            {
                "id": str(i),
                "name": display_name(i, value),
                **get_chat_metadata("channel", str(i)),
            }
            for i, value in app_state["channels"].items()
        ],
        "limits": {
            "max_contacts": app_state["limits"]["max_contacts"],
            "max_channels": app_state["limits"]["max_channels"],
        },
        "gateway_telemetry": app_state["gateway_telemetry"],
        "system_battery": system_battery_percentage(),
    })


async def peer_telemetry_handler(request):
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    node_id = request.query.get("node_id", "").strip()
    if not node_id:
        return web.json_response({"error": "A node ID is required"}, status=400)

    await refresh_contacts()
    entry = app_state["contacts"].get(node_id)
    if entry is None:
        entry = next(
            (
                contact for contact in app_state["contacts"].values()
                if isinstance(contact, dict)
                and contact.get("public_key", "").startswith(node_id)
            ),
            None,
        )
    contact = entry.get("contact", entry) if isinstance(entry, dict) else None
    if not isinstance(contact, dict) or not contact.get("public_key"):
        return web.json_response({"error": "Peer is not in the contact list"}, status=404)

    request_telemetry = getattr(meshcore_instance.commands, "req_telemetry_sync", None)
    if request_telemetry is None:
        return web.json_response({"error": "This MeshCore version cannot request peer telemetry"}, status=501)

    try:
        async with paced_hardware_lock():
            telemetry = await asyncio.wait_for(
                request_telemetry(contact, min_timeout=8),
                timeout=20,
            )
    except asyncio.TimeoutError:
        return web.json_response({"error": "Peer telemetry request timed out"}, status=504)
    except Exception as error:
        log_to_dash(f"Peer telemetry request failed for {node_id}: {error}")
        return web.json_response({"error": "Could not request telemetry from this peer"}, status=502)

    coordinates = coordinates_from_entry(contact)
    return web.json_response({
        "node": {
            "id": node_id,
            "name": display_name(node_id, entry),
            "public_key": contact.get("public_key", node_id),
            "type": contact.get("type"),
            "last_heard": contact.get("last_advert", contact.get("last_heard")),
            "hops": contact.get("out_path_len"),
            "latitude": coordinates[0] if coordinates else None,
            "longitude": coordinates[1] if coordinates else None,
        },
        "telemetry": telemetry or [],
    })


async def chat_history_handler(request):
    target_type = request.query.get("target_type", "node")
    target = request.query.get("target", "")
    if target_type not in {"node", "channel"} or not target:
        return web.json_response({"error": "A valid conversation target is required"}, status=400)
    messages = chat_history.get(chat_key(target_type, target), [])
    if target_type == "channel":
        messages = [
            message for message in messages
            if message.get("direction") == "outgoing"
            or parse_channel_sender(message.get("text", "")).casefold() not in blocked_senders
        ]
    return web.json_response({
        "messages": messages,
        "metadata": get_chat_metadata(target_type, target),
        "blocked": sorted(blocked_senders),
    })


async def delete_message_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    target_type = data.get("target_type")
    target = str(data.get("target", "")).strip()
    message_id = data.get("id")
    if target_type not in {"node", "channel"} or not target or not isinstance(message_id, str):
        return web.json_response({"error": "A valid message is required"}, status=400)
    key = chat_key(target_type, target)
    remaining = [message for message in chat_history.get(key, []) if message.get("id") != message_id]
    if len(remaining) == len(chat_history.get(key, [])):
        return web.json_response({"error": "Message not found"}, status=404)
    chat_history[key] = remaining
    if not save_chat_store():
        return web.json_response({"error": "Chat changes could not be saved"}, status=500)
    return web.json_response({"ok": True})


async def block_sender_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 40:
        return web.json_response({"error": "A sender name is required"}, status=400)
    if data.get("blocked", True) is False:
        blocked_senders.discard(name.casefold())
    else:
        blocked_senders.add(name.casefold())
    if not save_chat_store():
        return web.json_response({"error": "Block list could not be saved"}, status=500)
    return web.json_response({"ok": True, "blocked": sorted(blocked_senders)})


async def manage_chat_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "Invalid JSON object"}, status=400)

    target_type = data.get("target_type")
    target = str(data.get("target", "")).strip()
    action = data.get("action")
    if not isinstance(target_type, str) or target_type not in {"node", "channel"} or not target:
        return web.json_response({"error": "A valid conversation target is required"}, status=400)
    if not isinstance(action, str) or action not in {"clear", "archive", "pin"}:
        return web.json_response({"error": "Choose clear, archive, or pin"}, status=400)

    key = chat_key(target_type, target)
    metadata = chat_metadata.setdefault(key, {"archived": False, "pinned": False})
    if action == "clear":
        chat_history[key] = []
    else:
        flag = "archived" if action == "archive" else "pinned"
        metadata[flag] = not metadata.get(flag, False)

    if not save_chat_store():
        return web.json_response({"error": "Chat changes could not be saved"}, status=500)
    return web.json_response({
        "messages": chat_history.get(key, []),
        "metadata": get_chat_metadata(target_type, target),
    })


PUBLIC_CHANNEL_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
CONTACT_ADD_TYPES = {"companion": 1, "repeater": 2, "room": 3, "sensor": 4}
HEX_KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


async def read_json_object(request):
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def add_channel_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    kind = str(data.get("type", "")).strip().lower()
    name = str(data.get("name", "")).strip()
    max_channels = app_state["limits"]["max_channels"]
    if len(app_state["channels"]) >= max_channels:
        return web.json_response(
            {"error": f"Channel limit reached ({len(app_state['channels'])}/{max_channels})."}, status=409
        )

    if kind == "public":
        name, secret = "Public", PUBLIC_CHANNEL_SECRET
    elif kind == "hashtag":
        name = "#" + name.lstrip("#").strip().lower()
        if not re.fullmatch(r"#[a-z0-9-]{1,31}", name):
            return web.json_response(
                {"error": "Hashtag channels use letters, numbers and dashes only (max 31)."}, status=400
            )
        secret = None
    elif kind == "private":
        if not name or len(name.encode("utf-8")) > 32 or name.startswith("#"):
            return web.json_response(
                {"error": "Enter a channel name up to 32 bytes that does not start with #."}, status=400
            )
        raw_secret = str(data.get("secret", "")).strip().lower()
        if raw_secret:
            if not re.fullmatch(r"[0-9a-f]{32}", raw_secret):
                return web.json_response({"error": "Secret key must be 32 hex characters."}, status=400)
            secret = bytes.fromhex(raw_secret)
        else:
            secret = secrets.token_bytes(16)
    else:
        return web.json_response({"error": "Channel type must be public, private or hashtag."}, status=400)

    if any(
        str(entry.get("name", "")).casefold() == name.casefold()
        for entry in app_state["channels"].values()
    ):
        return web.json_response({"error": f"A channel named {name} already exists."}, status=409)

    try:
        used = {int(entry["channel_idx"]) for entry in app_state["channels"].values()}
        commands = meshcore_instance.commands
        index = None
        for candidate in range(max_channels):
            if candidate in used:
                continue
            # The scan hides duplicate entries, so confirm the slot is really empty.
            async with paced_hardware_lock():
                try:
                    existing = await asyncio.wait_for(commands.get_channel(candidate), timeout=3.0)
                except asyncio.TimeoutError:
                    existing = None
            payload = existing.payload if existing is not None and existing.type != EventType.ERROR else None
            if not payload or not str(payload.get("channel_name", "")).strip("\x00 "):
                index = candidate
                break
        if index is None:
            return web.json_response({"error": "No free channel slot on the device."}, status=409)
        async with paced_hardware_lock():
            result = await commands.set_channel(index, name, secret)
        if result.type == EventType.ERROR:
            return web.json_response({"error": f"Device rejected channel: {result.payload}"}, status=502)
    except Exception as error:
        return web.json_response({"error": f"Could not add channel: {error}"}, status=500)

    log_to_dash(f"Added {kind} channel {name} at slot {index}")
    await refresh_channels()
    response = {"ok": True, "name": name, "index": index}
    if kind == "private":
        response["secret"] = secret.hex()
    return web.json_response(response)


async def add_contact_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    max_contacts = app_state["limits"]["max_contacts"]
    count = len(app_state["contacts"])
    if max_contacts and count >= max_contacts:
        return web.json_response({"error": f"Contact limit reached ({count}/{max_contacts})."}, status=409)

    commands = meshcore_instance.commands
    card = str(data.get("card", "")).strip()
    try:
        if card:
            card_hex = re.sub(r"^meshcore://", "", card, flags=re.IGNORECASE).strip()
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", card_hex):
                return web.json_response({"error": "Paste a meshcore:// contact link or its hex data."}, status=400)
            async with paced_hardware_lock():
                result = await commands.import_contact(bytes.fromhex(card_hex))
        else:
            public_key = str(data.get("public_key", "")).strip().lower()
            name = str(data.get("name", "")).strip()
            node_type = CONTACT_ADD_TYPES.get(str(data.get("node_type", "repeater")).strip().lower())
            if not HEX_KEY_PATTERN.fullmatch(public_key):
                return web.json_response({"error": "Public key must be 64 hex characters."}, status=400)
            if not name or len(name.encode("utf-8")) > 31:
                return web.json_response({"error": "Enter a name up to 31 bytes."}, status=400)
            if node_type is None:
                return web.json_response({"error": "Unknown node type."}, status=400)
            if any(
                str((entry.get("contact", entry) if isinstance(entry, dict) else {}).get("public_key", key)).lower()
                == public_key
                for key, entry in app_state["contacts"].items()
            ):
                return web.json_response({"error": "That node is already in your contacts."}, status=409)
            try:
                latitude = float(data.get("latitude") or 0)
                longitude = float(data.get("longitude") or 0)
            except (TypeError, ValueError):
                return web.json_response({"error": "Latitude and longitude must be numbers."}, status=400)
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                return web.json_response({"error": "Latitude or longitude is out of range."}, status=400)
            contact = {
                "public_key": public_key,
                "type": node_type,
                "flags": 0,
                "out_path_len": -1,
                "out_path": "",
                "out_path_hash_mode": 0,
                "adv_name": name,
                "last_advert": int(time.time()),
                "adv_lat": latitude,
                "adv_lon": longitude,
            }
            async with paced_hardware_lock():
                result = await commands.add_contact(contact)
        if result.type == EventType.ERROR:
            return web.json_response({"error": f"Device rejected contact: {result.payload}"}, status=502)
    except Exception as error:
        return web.json_response({"error": f"Could not add contact: {error}"}, status=500)

    log_to_dash("Contact added from dashboard")
    await refresh_contacts()
    return web.json_response({"ok": True})


def contact_public_key(entry, fallback=""):
    contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
    return str(contact.get("public_key") or fallback)


cli_reply_waiters = {}


def find_contact_entry(target):
    target = str(target).strip()
    entry = app_state["contacts"].get(target)
    if entry is None:
        entry = next(
            (
                value for value in app_state["contacts"].values()
                if contact_public_key(value).lower().startswith(target.lower())
            ),
            None,
        ) if target else None
    if entry is None:
        return None, None
    contact = entry.get("contact", entry) if isinstance(entry, dict) else None
    if not isinstance(contact, dict) or not contact.get("public_key"):
        return None, None
    return entry, contact


async def ping_contact_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)
    entry, contact = find_contact_entry(data.get("target", ""))
    if contact is None:
        return web.json_response({"error": "Peer is not in the contact list"}, status=404)
    if int(contact.get("type") or 0) != 2:
        return web.json_response({"error": "Ping is only available for repeaters"}, status=400)
    request_status = getattr(meshcore_instance.commands, "req_status_sync", None)
    if request_status is None:
        return web.json_response({"error": "This MeshCore version cannot ping repeaters"}, status=501)
    started = time.monotonic()
    try:
        async with paced_hardware_lock():
            status = await asyncio.wait_for(request_status(contact, min_timeout=8), timeout=25)
    except asyncio.TimeoutError:
        status = None
    except Exception as error:
        log_to_dash(f"Ping failed for {display_name(data.get('target'), entry)}: {error}")
        return web.json_response({"error": "Ping failed"}, status=502)
    if not status:
        return web.json_response({"ok": False, "error": "No reply from repeater (timed out)"})
    rtt_ms = int((time.monotonic() - started) * 1000)
    log_to_dash(f"Ping to {display_name(data.get('target'), entry)}: {rtt_ms} ms")
    return web.json_response({
        "ok": True,
        "rtt_ms": rtt_ms,
        "hops": contact.get("out_path_len"),
        "uptime": status.get("uptime") if isinstance(status, dict) else None,
        "battery_mv": status.get("bat") if isinstance(status, dict) else None,
    })


async def manage_contact_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)
    entry, contact = find_contact_entry(data.get("target", ""))
    if contact is None:
        return web.json_response({"error": "Peer is not in the contact list"}, status=404)
    if int(contact.get("type") or 0) not in {2, 3}:
        return web.json_response({"error": "Remote management is only available for repeaters and room servers"}, status=400)
    action = str(data.get("action", "")).strip()
    commands = meshcore_instance.commands
    name = display_name(data.get("target"), entry)
    try:
        if action == "login":
            password = str(data.get("password", ""))
            async with paced_hardware_lock():
                event = await asyncio.wait_for(
                    commands.send_login_sync(contact, password, min_timeout=8), timeout=30
                )
            if event is None or event.type == EventType.ERROR:
                return web.json_response({"error": "Login failed or timed out. Check the password."}, status=401)
            log_to_dash(f"Remote management login to {name}")
            return web.json_response({"ok": True})
        if action == "logout":
            async with paced_hardware_lock():
                await commands.send_logout(contact)
            return web.json_response({"ok": True})
        if action == "command":
            command = str(data.get("command", "")).strip()
            if not command or len(command) > 150:
                return web.json_response({"error": "Enter a command (150 characters max)"}, status=400)
            prefix = str(contact["public_key"]).lower()[:12]
            queue = asyncio.Queue()
            cli_reply_waiters[prefix] = queue
            try:
                async with paced_hardware_lock():
                    result = await commands.send_cmd(contact, command, dst_type=contact.get("type"))
                if result.type == EventType.ERROR:
                    return web.json_response({"error": f"Device rejected command: {result.payload}"}, status=502)
                try:
                    reply = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    return web.json_response({"ok": False, "error": "No reply (timed out). You may need to log in first."})
            finally:
                cli_reply_waiters.pop(prefix, None)
            log_to_dash(f"Remote command sent to {name}: {command.split()[0]}")
            return web.json_response({"ok": True, "reply": reply})
    except Exception as error:
        log_to_dash(f"Remote management failed for {name}: {error}")
        return web.json_response({"error": "Remote management request failed"}, status=502)
    return web.json_response({"error": "action must be login, logout, or command"}, status=400)


async def add_heard_contact_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)
    target = str(data.get("target", "")).strip().lower()
    name = str(data.get("name", "")).strip().lower()
    if not target and not name:
        return web.json_response({"error": "A node ID or name is required"}, status=400)

    max_contacts = app_state["limits"]["max_contacts"]
    if max_contacts and len(app_state["contacts"]) >= max_contacts:
        return web.json_response({"error": "Contact limit reached."}, status=409)

    pending = getattr(meshcore_instance, "pending_contacts", {}) or {}
    match = next(
        (
            contact for key, contact in pending.items()
            if (target and str(key).lower().startswith(target))
            or (name and str(contact.get("adv_name", "")).strip().lower() == name)
        ),
        None,
    )
    if match is None:
        return web.json_response(
            {"error": "No advert has been heard from this node yet. Use + Add with its link or public key."},
            status=404,
        )
    try:
        async with paced_hardware_lock():
            result = await meshcore_instance.commands.add_contact(match)
        if result.type == EventType.ERROR:
            return web.json_response({"error": f"Device rejected contact: {result.payload}"}, status=502)
    except Exception as error:
        return web.json_response({"error": f"Could not add contact: {error}"}, status=500)
    meshcore_instance.pop_pending_contact(match["public_key"])
    log_to_dash(f"Contact {match.get('adv_name', '')} added from message actions")
    await refresh_contacts()
    return web.json_response({"ok": True, "name": match.get("adv_name", "")})


async def remove_contact_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)
    target = resolve_contact_id(str(data.get("target", "")).strip())
    entry = app_state["contacts"].get(target)
    if entry is None:
        return web.json_response({"error": "Contact not found"}, status=404)
    try:
        async with paced_hardware_lock():
            result = await meshcore_instance.commands.remove_contact(contact_public_key(entry, target))
        if result.type == EventType.ERROR:
            return web.json_response({"error": f"Device rejected removal: {result.payload}"}, status=502)
    except Exception as error:
        return web.json_response({"error": f"Could not remove contact: {error}"}, status=500)
    log_to_dash("Contact removed from message actions")
    await refresh_contacts()
    return web.json_response({"ok": True})


async def contact_path_handler(request):
    data = await read_json_object(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)
    entry, contact = find_contact_entry(data.get("target", ""))
    if contact is None:
        return web.json_response({"error": "Peer is not in the contact list"}, status=404)
    action = str(data.get("action", "")).strip()
    commands = meshcore_instance.commands
    name = display_name(data.get("target"), entry)
    try:
        if action == "reset":
            async with paced_hardware_lock():
                result = await commands.reset_path(contact["public_key"])
            if result.type == EventType.ERROR:
                return web.json_response({"error": f"Device rejected reset: {result.payload}"}, status=502)
            log_to_dash(f"Path reset for {name}")
        elif action == "set":
            tokens = [t for t in re.split(r"[\s,>:-]+", str(data.get("path", "")).strip().lower()) if t]
            if not tokens or len(tokens) > 63:
                return web.json_response({"error": "Enter 1-63 repeater hashes, e.g. a1,b2,c3"}, status=400)
            size = len(tokens[0])
            if size not in (2, 4, 6) or any(len(t) != size or not re.fullmatch(r"[0-9a-f]+", t) for t in tokens):
                return web.json_response({"error": "Each hash must be 2, 4 or 6 hex characters, all the same length"}, status=400)
            async with paced_hardware_lock():
                result = await commands.change_contact_path(contact, "".join(tokens), path_hash_mode=size // 2 - 1)
            if result.type == EventType.ERROR:
                return web.json_response({"error": f"Device rejected path: {result.payload}"}, status=502)
            log_to_dash(f"Path set for {name}: {len(tokens)} hop(s)")
        else:
            return web.json_response({"error": "action must be set or reset"}, status=400)
    except Exception as error:
        log_to_dash(f"Path update failed for {name}: {error}")
        return web.json_response({"error": "Path update failed"}, status=502)
    await refresh_contacts()
    return web.json_response({"ok": True})


async def delete_target_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "Invalid JSON object"}, status=400)
    target_type = data.get("target_type")
    target = str(data.get("target", "")).strip()
    if target_type not in {"node", "channel"} or not target:
        return web.json_response({"error": "A valid target is required"}, status=400)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    try:
        commands = meshcore_instance.commands
        if target_type == "node":
            target = resolve_contact_id(target)
            entry = app_state["contacts"].get(target)
            if entry is None:
                return web.json_response({"error": "Node not found"}, status=404)
            contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
            public_key = contact.get("public_key") or target
            async with paced_hardware_lock():
                result = await commands.remove_contact(public_key)
        else:
            entry = app_state["channels"].get(target)
            if entry is None:
                return web.json_response({"error": "Channel not found"}, status=404)
            # An empty name with an all-zero secret clears the channel slot.
            async with paced_hardware_lock():
                result = await commands.set_channel(int(entry["channel_idx"]), "", bytes(16))
        if result.type == EventType.ERROR:
            return web.json_response({"error": f"Device rejected delete: {result.payload}"}, status=502)
    except Exception as error:
        return web.json_response({"error": f"Delete failed: {error}"}, status=500)

    key = chat_key(target_type, target)
    chat_history.pop(key, None)
    chat_metadata.pop(key, None)
    save_chat_store()
    if target_type == "node":
        await refresh_contacts()
    else:
        await refresh_channels()
    return web.json_response({"ok": True})


def validate_device_settings(current, values):
    if not isinstance(values, dict) or not values:
        raise ValueError("Provide at least one device setting to update")

    allowed = {
        "name", "adv_lat", "adv_lon", "radio_freq", "radio_bw", "radio_sf",
        "radio_cr", "tx_power", "rx_delay", "airtime_factor",
        "telemetry_mode_base", "telemetry_mode_loc", "telemetry_mode_env",
        "adv_loc_policy", "manual_add_contacts", "multi_acks", "device_pin",
        "repeat", "path_hash_mode", "gps_enabled", "gps_interval",
        "auto_add_flags",
    }
    unsupported = set(values) - allowed
    if unsupported:
        raise ValueError(f"Unsupported device settings: {', '.join(sorted(unsupported))}")

    updated = {key: current.get(key) for key in allowed}
    updated.update(values)

    name = updated.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip().encode("utf-8")) > 31:
        raise ValueError("Device name must contain 1 to 31 UTF-8 bytes")
    updated["name"] = name.strip()

    for key, minimum, maximum in (("adv_lat", -90, 90), ("adv_lon", -180, 180)):
        try:
            value = float(updated[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        updated[key] = value

    for key, minimum, maximum in (
        ("radio_freq", 100, 3000),
        ("radio_bw", 7.8, 1000),
    ):
        try:
            value = float(updated[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        updated[key] = value

    for key, minimum, maximum in (
        ("radio_sf", 5, 12),
        ("radio_cr", 5, 8),
        ("tx_power", -9, int(current.get("max_tx_power", 30))),
        ("rx_delay", 0, 2**32 - 1),
        ("airtime_factor", 0, 2**32 - 1),
        ("telemetry_mode_base", 0, 2),
        ("telemetry_mode_loc", 0, 2),
        ("telemetry_mode_env", 0, 2),
        ("adv_loc_policy", 0, 1),
        ("path_hash_mode", 0, 2),
        ("auto_add_flags", 0, 31),
    ):
        value = updated[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    for key in ("manual_add_contacts", "multi_acks", "gps_enabled", "repeat"):
        if not isinstance(updated[key], bool):
            raise ValueError(f"{key} must be true or false")
        updated[key] = int(updated[key])
    gps_interval = updated.get("gps_interval")
    if gps_interval not in (None, ""):
        if isinstance(gps_interval, bool) or not isinstance(gps_interval, int):
            raise ValueError("gps_interval must be an integer")
        if not 60 <= gps_interval < 86400:
            raise ValueError("gps_interval must be between 60 and 86399 seconds")
    device_pin = updated.get("device_pin")
    if device_pin not in (None, ""):
        if not isinstance(device_pin, str) or not device_pin.isdigit():
            raise ValueError("device_pin must be an unsigned integer")
        if not 0 <= int(device_pin) <= 2**32 - 1:
            raise ValueError("device_pin must be between 0 and 4294967295")
    return updated


async def device_settings_handler(request):
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    try:
        async with paced_hardware_lock():
            result = await meshcore_instance.commands.send_appstart()
        if result.type == EventType.ERROR:
            return web.json_response({"error": str(result.payload)}, status=502)
        settings = dict(result.payload or {})
        get_tuning = getattr(meshcore_instance.commands, "get_tuning", None)
        if get_tuning:
            try:
                async with paced_hardware_lock():
                    tuning = await get_tuning()
                if tuning.type != EventType.ERROR:
                    settings.update(tuning.payload or {})
            except Exception as error:
                log_to_dash(f"Optional tuning read failed: {error}")
        commands = meshcore_instance.commands
        for method_name, key in (
            ("send_device_query", "device_info"),
            ("get_bat", "battery_info"),
            ("get_custom_vars", "custom_vars"),
            ("get_autoadd_config", "auto_add_config"),
        ):
            method = getattr(commands, method_name, None)
            if method is None:
                continue
            try:
                async with paced_hardware_lock():
                    extra = await method()
                if extra.type != EventType.ERROR:
                    settings[key] = extra.payload or {}
            except Exception as error:
                log_to_dash(f"Optional {key} read failed: {error}")
        get_path_hash = getattr(commands, "get_path_hash_mode", None)
        if get_path_hash:
            try:
                async with paced_hardware_lock():
                    settings["path_hash_mode"] = await get_path_hash()
            except Exception as error:
                log_to_dash(f"Optional path hash read failed: {error}")
        return web.json_response(settings)
    except Exception as error:
        log_to_dash(f"Device settings read error: {error}")
        return web.json_response({"error": str(error)}, status=502)


async def update_device_settings_handler(request):
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)
    try:
        payload = await request.json()
        values = payload.get("values", payload)
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    try:
        async with paced_hardware_lock():
            commands = meshcore_instance.commands
            current_result = await commands.send_appstart()
            if current_result.type == EventType.ERROR:
                return web.json_response({"error": str(current_result.payload)}, status=502)
            current = current_result.payload
            device_query = getattr(commands, "send_device_query", None)
            if device_query:
                device_info = await device_query()
                if device_info.type != EventType.ERROR:
                    current["repeat"] = (device_info.payload or {}).get("repeat")
            tuning = getattr(commands, "get_tuning", None)
            if tuning:
                tuning_result = await tuning()
                if tuning_result.type != EventType.ERROR:
                    current.update(tuning_result.payload or {})
            get_auto_add = getattr(commands, "get_autoadd_config", None)
            if get_auto_add:
                auto_add_result = await get_auto_add()
                if auto_add_result.type != EventType.ERROR:
                    current["auto_add_flags"] = (auto_add_result.payload or {}).get("config", 0)
            get_path_hash = getattr(commands, "get_path_hash_mode", None)
            if get_path_hash:
                current["path_hash_mode"] = await get_path_hash()
            get_custom_vars = getattr(commands, "get_custom_vars", None)
            if get_custom_vars:
                custom_result = await get_custom_vars()
                if custom_result.type != EventType.ERROR:
                    custom_vars = custom_result.payload or {}
                    current["gps_enabled"] = custom_vars.get("gps") == "1"
                    try:
                        current["gps_interval"] = int(custom_vars.get("gps_interval", 0)) or None
                    except (TypeError, ValueError):
                        current["gps_interval"] = None
            updated = validate_device_settings(current, values)

            if updated["name"] != current.get("name"):
                result = await commands.set_name(updated["name"])
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            radio_keys = ("radio_freq", "radio_bw", "radio_sf", "radio_cr", "repeat")
            if any(updated[key] != current.get(key) for key in radio_keys):
                result = await commands.set_radio(
                    updated["radio_freq"],
                    updated["radio_bw"],
                    updated["radio_sf"],
                    updated["radio_cr"],
                    repeat=updated["repeat"] if current.get("repeat") is not None else None,
                )
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            if updated["tx_power"] != current.get("tx_power"):
                result = await commands.set_tx_power(updated["tx_power"])
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            if any(
                updated[key] != current.get(key)
                for key in ("adv_lat", "adv_lon")
            ):
                result = await commands.set_coords(updated["adv_lat"], updated["adv_lon"])
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            if current.get("rx_delay") is not None and current.get("airtime_factor") is not None and any(
                updated[key] != current.get(key)
                for key in ("rx_delay", "airtime_factor")
            ):
                result = await commands.set_tuning(
                    updated["rx_delay"], updated["airtime_factor"]
                )
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            other_keys = (
                "manual_add_contacts", "telemetry_mode_base", "telemetry_mode_loc",
                "telemetry_mode_env", "adv_loc_policy", "multi_acks",
            )
            if any(updated[key] != current.get(key) for key in other_keys):
                set_other = getattr(commands, "set_other_params_from_infos", None)
                if set_other is None:
                    set_other = commands.set_other_params
                    result = await set_other(
                        updated["manual_add_contacts"],
                        updated["telemetry_mode_base"],
                        updated["telemetry_mode_loc"],
                        updated["telemetry_mode_env"],
                        updated["adv_loc_policy"],
                    )
                else:
                    result = await set_other(updated)
                    # Older firmware rejects the 5-byte frame carrying multi_acks.
                    if result.type == EventType.ERROR and updated["multi_acks"] == current.get("multi_acks"):
                        result = await commands.set_other_params(
                            updated["manual_add_contacts"],
                            updated["telemetry_mode_base"],
                            updated["telemetry_mode_loc"],
                            updated["telemetry_mode_env"],
                            updated["adv_loc_policy"],
                        )
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            if updated.get("device_pin") not in (None, ""):
                result = await commands.set_devicepin(int(updated["device_pin"]))
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            if "gps_enabled" in values:
                result = await commands.set_custom_var(
                    "gps", "1" if updated["gps_enabled"] else "0"
                )
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))
            if updated.get("gps_interval") not in (None, ""):
                result = await commands.set_custom_var(
                    "gps_interval", str(updated["gps_interval"])
                )
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))
            if "auto_add_flags" in values:
                set_auto_add = getattr(commands, "set_autoadd_config", None)
                if set_auto_add is None:
                    raise RuntimeError("This MeshCore version cannot edit auto-add settings")
                result = await set_auto_add(updated["auto_add_flags"])
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))
            if "path_hash_mode" in values:
                set_path_hash = getattr(commands, "set_path_hash_mode", None)
                if set_path_hash is None:
                    raise RuntimeError("This MeshCore version cannot edit path hash mode")
                result = await set_path_hash(updated["path_hash_mode"])
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            refreshed = await commands.send_appstart()
            if refreshed.type == EventType.ERROR:
                raise RuntimeError(str(refreshed.payload))
            return web.json_response(refreshed.payload)
    except ValueError as error:
        return web.json_response({"error": str(error)}, status=400)
    except Exception as error:
        log_to_dash(f"Device settings update error: {error}")
        return web.json_response({"error": str(error)}, status=409)


async def device_action_handler(request):
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)
    try:
        payload = await request.json()
        action = payload.get("action")
        commands = meshcore_instance.commands
        async with paced_hardware_lock():
            if action == "sync_time":
                result = await commands.set_time(int(datetime.now().timestamp()))
            elif action == "reboot":
                result = await commands.reboot()
            elif action in ("advert_zero_hop", "advert_flood"):
                result = await commands.send_advert(flood=action == "advert_flood")
            elif action == "stats":
                stats_type = payload.get("stats_type", "radio")
                methods = {
                    "core": "get_stats_core",
                    "radio": "get_stats_radio",
                    "packets": "get_stats_packets",
                }
                method = getattr(commands, methods.get(stats_type, ""), None)
                if method is None:
                    return web.json_response({"error": "This firmware does not support these statistics"}, status=501)
                result = await method()
            elif action == "telemetry":
                method = getattr(commands, "get_self_telemetry", None)
                if method is None:
                    return web.json_response({"error": "This firmware does not support telemetry"}, status=501)
                result = await method()
            elif action == "delete_paths":
                reset_path = getattr(commands, "reset_path", None)
                if reset_path is None:
                    return web.json_response({"error": "This MeshCore version cannot reset paths"}, status=501)
                count = 0
                for entry in app_state["contacts"].values():
                    contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
                    public_key = contact.get("public_key") if isinstance(contact, dict) else None
                    if not public_key:
                        continue
                    result = await reset_path(public_key)
                    if result.type == EventType.ERROR:
                        raise RuntimeError(str(result.payload))
                    count += 1
                return web.json_response({"message": f"Reset paths for {count} contacts."})
            elif action == "refresh_contacts":
                result = await commands.get_contacts(timeout=20)
                if result.type != EventType.ERROR:
                    app_state["contacts"] = normalize_entries(result.payload)
                    return web.json_response({"message": f"Loaded {len(app_state['contacts'])} contacts."})
            else:
                return web.json_response({"error": "Unknown device action"}, status=400)
        if result.type == EventType.ERROR:
            return web.json_response({"error": str(result.payload)}, status=502)
        return web.json_response({"message": "Device action completed.", "result": result.payload})
    except Exception as error:
        log_to_dash(f"Device action failed: {error}")
        return web.json_response({"error": str(error)}, status=502)


async def cpu_temp_handler(request):
    value = await asyncio.to_thread(cpu_temperature_f)
    return web.json_response({"temperature_f": value})


async def noise_floor_handler(request):
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"connected": False}, status=503)
    method = getattr(meshcore_instance.commands, "get_stats_radio", None)
    if method is None:
        return web.json_response({"error": "This firmware does not support radio statistics"}, status=501)
    # Skip the read while the radio is busy sending so the scope never delays replies.
    if not hardware_lock.locked():
        try:
            async with paced_hardware_lock():
                result = await asyncio.wait_for(method(), timeout=3)
            if result is not None and result.type != EventType.ERROR and isinstance(result.payload, dict):
                noise = result.payload.get("noise_floor")
                if isinstance(noise, (int, float)):
                    app_state["noise_floor"] = {
                        "noise_floor": noise,
                        "last_rssi": result.payload.get("last_rssi"),
                        "last_snr": result.payload.get("last_snr"),
                        "time": time.time(),
                    }
        except Exception as error:
            log_to_dash(f"Noise floor read failed: {error}")
    return web.json_response({"connected": True, **(app_state.get("noise_floor") or {})})


async def device_gpx_export_handler(request):
    export_type = request.query.get("type", "all")
    if export_type not in {"repeaters", "contacts", "all"}:
        return web.json_response({"error": "Unknown GPX export type"}, status=400)

    root = ET.Element("gpx", {"version": "1.1", "creator": "MeshCore Dashboard", "xmlns": "http://www.topografix.com/GPX/1/1"})
    count = 0
    for node_id, entry in app_state["contacts"].items():
        contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
        if not isinstance(contact, dict):
            contact = {}
        node_type = contact.get("type", entry.get("type") if isinstance(entry, dict) else None)
        if export_type == "repeaters" and str(node_type) != "2":
            continue
        if export_type == "contacts" and str(node_type) == "2":
            continue
        coordinates = coordinates_from_entry(entry)
        if not coordinates:
            continue
        waypoint = ET.SubElement(root, "wpt", {"lat": f"{coordinates[0]:.7f}", "lon": f"{coordinates[1]:.7f}"})
        ET.SubElement(waypoint, "name").text = display_name(node_id, entry)
        ET.SubElement(waypoint, "type").text = {"1": "User", "2": "Repeater", "3": "Room server", "4": "Sensor"}.get(str(node_type), "MeshCore peer")
        count += 1
    if not count:
        return web.json_response({"error": "No contacts with coordinates are available to export"}, status=404)
    content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    filename = f"meshcore_{export_type}.gpx"
    return web.Response(
        body=content,
        content_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    app_config["connection"] = {
        "type": connection_type,
        "ble_mac": ble_mac,
        "serial_port": serial_port,
    }
    if data.get("model"):
        selected_model = str(data["model"]).strip()
        if selected_model:
            app_state["selected_model"] = selected_model
            app_config["model"] = selected_model
    try:
        write_app_config(app_config)
    except OSError as error:
        log_to_dash(f"Failed to save connection settings to config.json: {error}")

    app_state["auto_reconnect"] = True
    await connect_hardware()
    return web.json_response({"is_connected": app_state["is_connected"]})


async def disconnect_handler(request):
    app_state["auto_reconnect"] = False
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
    if target_type == "node":
        entry = app_state["contacts"].get(target)
        contact = entry.get("contact", entry) if isinstance(entry, dict) else {}
        if isinstance(contact, dict) and str(contact.get("type", "")) == "2":
            return web.json_response(
                {"error": "Direct messages to repeater nodes are not supported"},
                status=400,
            )
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    try:
        async with paced_hardware_lock():
            result, pending_delivery, hops = await send_to_target_with_pending_confirmation(
                target, target_type, message
            )
        if result.type == EventType.ERROR:
            return web.json_response({"error": str(result.payload)}, status=500)
        sent_message = add_chat_message(target_type, target, "outgoing", message)
        update_chat_message_status(sent_message, "sent")
        schedule_delivery_status_update(sent_message, pending_delivery, hops)
        record_trace_event(
            "direct" if target_type == "node" else "channel",
            "outbound",
            target,
        )
        log_to_dash(f"{target_type.title()} message sent to {target}")
        return web.json_response({"success": True})
    except Exception as error:
        log_to_dash(f"Transmit error: {error}")
        return web.json_response({"error": str(error)}, status=500)


async def auto_connect_hardware():
    # Keeps retrying so the bot recovers when the radio is unplugged or reboots.
    while True:
        try:
            # A dropped serial/BLE link leaves is_connected stuck on True otherwise.
            if app_state["is_connected"] and meshcore_instance is not None:
                if getattr(meshcore_instance, "is_connected", True) is False:
                    log_to_dash("Device link lost; reconnecting...")
                    app_state["is_connected"] = False
            if app_state["auto_reconnect"] and not app_state["is_connected"] and not connection_lock.locked():
                if app_state["connection_type"] == "bluetooth":
                    target = app_state["ble_mac"]
                else:
                    target = app_state["serial_port"]
                    if target == DEFAULT_SERIAL_PORT and not os.path.exists(target):
                        target = ""
                if target:
                    log_to_dash(f"Auto-connecting to MeshCore device {target}...")
                    await connect_hardware()
            await asyncio.sleep(AUTO_RECONNECT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_to_dash(f"Auto-connect error: {error}")
            await asyncio.sleep(AUTO_RECONNECT_INTERVAL_SECONDS)


async def on_startup(app):
    app["telemetry_task"] = asyncio.create_task(telemetry_loop())
    app["ollama_task"] = asyncio.create_task(update_available_models())
    app["ollama_schedule_task"] = asyncio.create_task(ollama_schedule_loop())
    app["connection_task"] = asyncio.create_task(auto_connect_hardware())


async def on_cleanup(app):
    for task in (
        app["connection_task"], app["telemetry_task"], app["ollama_task"],
        app["ollama_schedule_task"],
    ):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await disconnect_hardware()
    executor.shutdown(wait=False)


AUTH_PATH = CONFIG_DIR / "meshcore-ollama-bot" / "dashboard_auth.json"
DEFAULT_DASHBOARD_PASSWORD = "orange pi"
AUTH_COOKIE = "madhat_session"
AUTH_SESSION_SECONDS = 30 * 24 * 3600
AUTH_PUBLIC_PATHS = {"/login", "/api/login", "/api/forgot", "/api/reset"}
RESET_CODE_SECONDS = 600
RESET_CODE_ATTEMPTS = 5
auth_failures = defaultdict(list)
reset_state = {"code": None, "expires": 0.0, "attempts": 0, "requested": 0.0}


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def save_auth(password):
    salt = secrets.token_hex(16)
    auth = {"salt": salt, "hash": hash_password(password, salt), "secret": secrets.token_hex(32)}
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = AUTH_PATH.with_suffix(".tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(auth, handle)
    os.replace(temp_path, AUTH_PATH)
    return auth


def load_auth():
    try:
        auth = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        if all(isinstance(auth.get(key), str) for key in ("salt", "hash", "secret")):
            return auth
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return save_auth(DEFAULT_DASHBOARD_PASSWORD)


def check_password(password):
    auth = load_auth()
    return hmac.compare_digest(hash_password(password, auth["salt"]), auth["hash"])


def make_session_token():
    expires = str(int(time.time()) + AUTH_SESSION_SECONDS)
    signature = hmac.new(load_auth()["secret"].encode(), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def valid_session_token(token):
    expires, _, signature = (token or "").partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expected = hmac.new(load_auth()["secret"].encode(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def auth_rate_limited(request, record=False):
    now = time.time()
    attempts = [stamp for stamp in auth_failures[request.remote] if now - stamp < 300]
    auth_failures[request.remote] = attempts
    if record:
        attempts.append(now)
    return len(attempts) >= 5


def login_response(token):
    response = web.json_response({"ok": True})
    response.set_cookie(
        AUTH_COOKIE, token, max_age=AUTH_SESSION_SECONDS, httponly=True, samesite="Strict", path="/"
    )
    return response


@web.middleware
async def auth_middleware(request, handler):
    if request.path in AUTH_PUBLIC_PATHS or valid_session_token(request.cookies.get(AUTH_COOKIE)):
        return await handler(request)
    if request.path.startswith("/api/"):
        return web.json_response({"error": "Login required"}, status=401)
    raise web.HTTPFound("/login")


async def read_auth_json(request):
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def login_page_handler(request):
    return web.Response(text=LOGIN_PAGE, content_type="text/html")


async def login_handler(request):
    if auth_rate_limited(request):
        return web.json_response({"error": "Too many attempts. Try again in a few minutes."}, status=429)
    data = await read_auth_json(request)
    password = data.get("password")
    if not isinstance(password, str) or not check_password(password):
        auth_rate_limited(request, record=True)
        return web.json_response({"error": "Wrong password"}, status=401)
    auth_failures.pop(request.remote, None)
    return login_response(make_session_token())


async def logout_handler(request):
    response = web.HTTPFound("/login")
    response.del_cookie(AUTH_COOKIE, path="/")
    raise response


async def forgot_password_handler(request):
    now = time.time()
    if now - reset_state["requested"] < 60:
        return web.json_response({"error": "Wait a minute before requesting another code."}, status=429)
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected, so the code cannot be sent."}, status=503)
    admins = app_config["bot"].get("admins", [])
    if not admins:
        return web.json_response({"error": "No bot.admins configured to receive the code."}, status=400)
    reset_state["requested"] = now
    code = f"{secrets.randbelow(1_000_000):06d}"
    message = f"Dashboard password reset code: {code} (valid 10 min). Enter it on the login page with a new password."
    sent = 0
    for admin in admins:
        target = resolve_contact_id(admin)
        try:
            async with paced_hardware_lock():
                result = await send_to_target(target, "node", message)
            if result.type != EventType.ERROR:
                sent += 1
        except Exception as error:
            log_to_dash(f"Password reset DM to {admin} failed: {error}")
    if not sent:
        return web.json_response({"error": "Could not send the code to any admin."}, status=502)
    reset_state.update(code=code, expires=now + RESET_CODE_SECONDS, attempts=0)
    log_to_dash("Dashboard password reset code sent to admins")
    return web.json_response({"ok": True})


async def reset_password_handler(request):
    data = await read_auth_json(request)
    code = data.get("code")
    password = data.get("password")
    if not isinstance(code, str) or not isinstance(password, str):
        return web.json_response({"error": "Code and new password are required"}, status=400)
    if len(password) < 4 or len(password) > 128:
        return web.json_response({"error": "Password must be 4-128 characters"}, status=400)
    if not reset_state["code"] or time.time() > reset_state["expires"]:
        return web.json_response({"error": "No valid reset code. Request a new one."}, status=400)
    reset_state["attempts"] += 1
    if not hmac.compare_digest(code.strip(), reset_state["code"]):
        if reset_state["attempts"] >= RESET_CODE_ATTEMPTS:
            reset_state["code"] = None
        return web.json_response({"error": "Wrong code"}, status=401)
    reset_state["code"] = None
    save_auth(password)
    log_to_dash("Dashboard password was reset")
    return login_response(make_session_token())


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MadHat Login</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1020;color:#e6e9f2;font:16px system-ui,sans-serif}
form[hidden]{display:none}
.pwrap{position:relative;display:grid}.pwrap input{padding-right:44px}.eye{position:absolute;right:2px;top:2px;bottom:2px;width:40px;padding:0;background:none;border:0;color:#8fb0ff;font-size:18px;cursor:pointer}.eye.on{opacity:.6;text-decoration:line-through}
form{width:min(340px,90vw);padding:24px;border:1px solid #2b3556;border-radius:12px;background:#121a33;display:grid;gap:12px}
input,button{padding:10px;border-radius:8px;border:1px solid #2b3556;background:#0b1020;color:inherit;font:inherit}
button{background:#3b6cf6;border:0;cursor:pointer}a{color:#8fb0ff;cursor:pointer;font-size:14px}#msg{min-height:1.2em;font-size:14px;color:#ffb4b4}</style></head><body>
<form id="login"><h2 style="margin:0">MadHat Dashboard</h2>
<div class="pwrap"><input id="pw" type="password" placeholder="Password" autocomplete="current-password" autofocus><button type="button" class="eye" aria-label="Show password" aria-pressed="false">&#128065;</button></div>
<button>Log in</button><a id="forgot">Forgot password?</a><div id="msg"></div></form>
<form id="reset" hidden><h2 style="margin:0">Reset password</h2>
<div style="font-size:14px">A code was sent to the admin over the mesh. Enter it with a new password.</div>
<input id="code" placeholder="Reset code" inputmode="numeric" autocomplete="one-time-code">
<div class="pwrap"><input id="npw" type="password" placeholder="New password" autocomplete="new-password"><button type="button" class="eye" aria-label="Show password" aria-pressed="false">&#128065;</button></div>
<button>Set password</button><a id="back">Back to login</a><div id="msg2"></div></form>
<script>
const post=(u,b)=>fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}).then(async r=>({ok:r.ok,d:await r.json().catch(()=>({}))}));
const $=i=>document.getElementById(i);
document.querySelectorAll(".eye").forEach(b=>b.onclick=()=>{const i=b.previousElementSibling,show=i.type==="password";i.type=show?"text":"password";b.classList.toggle("on",show);b.setAttribute("aria-pressed",show);b.setAttribute("aria-label",show?"Hide password":"Show password")});
$("login").onsubmit=async e=>{e.preventDefault();const r=await post("/api/login",{password:$("pw").value});if(r.ok)location="/";else $("msg").textContent=r.d.error||"Login failed"};
$("forgot").onclick=async()=>{$("msg").textContent="Sending code...";const r=await post("/api/forgot",{});if(r.ok){$("login").hidden=true;$("reset").hidden=false}else $("msg").textContent=r.d.error||"Failed"};
$("back").onclick=()=>{$("reset").hidden=true;$("login").hidden=false};
$("reset").onsubmit=async e=>{e.preventDefault();const r=await post("/api/reset",{code:$("code").value,password:$("npw").value});if(r.ok)location="/";else $("msg2").textContent=r.d.error||"Reset failed"};
</script></body></html>"""


def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/login", login_page_handler)
    app.router.add_post("/api/login", login_handler)
    app.router.add_post("/api/forgot", forgot_password_handler)
    app.router.add_post("/api/reset", reset_password_handler)
    app.router.add_get("/logout", logout_handler)
    app.router.add_get("/", index_handler)
    app.router.add_get("/dashboard-logo.png", dashboard_logo_handler)
    app.router.add_get("/tiles/{source}/{z}/{x}/{y}.png", tile_handler)
    app.router.add_static("/assets/leaflet/", Path(__file__).resolve().parent / "assets" / "leaflet")
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/config", config_handler)
    app.router.add_patch("/api/config", update_config_handler)
    app.router.add_get("/api/local-weather", local_weather_handler)
    app.router.add_post("/api/restart", restart_dashboard_handler)
    app.router.add_post("/api/update", update_app_handler)
    app.router.add_get("/api/update/check", check_for_update_handler)
    app.router.add_post("/api/bot-console", bot_console_handler)
    app.router.add_post("/api/ollama/toggle", ollama_toggle_handler)
    app.router.add_get("/api/ollama/models", ollama_models_handler)
    app.router.add_get("/api/ollama/models/progress", ollama_model_progress_handler)
    app.router.add_post("/api/ollama/models/pull", ollama_pull_model_handler)
    app.router.add_post("/api/ollama/models/delete", ollama_delete_model_handler)
    app.router.add_get("/api/peers", peers_handler)
    app.router.add_get("/api/peer-telemetry", peer_telemetry_handler)
    app.router.add_get("/api/autostart", autostart_handler)
    app.router.add_post("/api/autostart", update_autostart_handler)
    app.router.add_get("/api/tightvnc", tightvnc_handler)
    app.router.add_post("/api/tightvnc", update_tightvnc_handler)
    app.router.add_get("/api/ssh-terminal", ssh_terminal_handler)
    app.router.add_post("/api/ssh-terminal", update_ssh_terminal_handler)
    app.router.add_get("/api/logs", logs_handler)
    app.router.add_delete("/api/logs", clear_logs_handler)
    app.router.add_get("/api/scan/bluetooth", bluetooth_scan_handler)
    app.router.add_get("/api/scan/serial", serial_scan_handler)
    app.router.add_get("/api/chat-history", chat_history_handler)
    app.router.add_get("/api/noise-floor", noise_floor_handler)
    app.router.add_get("/api/cpu-temp", cpu_temp_handler)
    app.router.add_post("/api/chat-management", manage_chat_handler)
    app.router.add_post("/api/delete-target", delete_target_handler)
    app.router.add_post("/api/delete-message", delete_message_handler)
    app.router.add_post("/api/block-sender", block_sender_handler)
    app.router.add_post("/api/channels/add", add_channel_handler)
    app.router.add_post("/api/contacts/add", add_contact_handler)
    app.router.add_post("/api/contacts/add-heard", add_heard_contact_handler)
    app.router.add_post("/api/contacts/remove", remove_contact_handler)
    app.router.add_post("/api/contacts/ping", ping_contact_handler)
    app.router.add_post("/api/contacts/path", contact_path_handler)
    app.router.add_post("/api/contacts/manage", manage_contact_handler)
    app.router.add_get("/api/device-settings", device_settings_handler)
    app.router.add_post("/api/device-settings/action", device_action_handler)
    app.router.add_get("/api/device-settings/export", device_gpx_export_handler)
    app.router.add_post("/api/connect", connect_handler)
    app.router.add_post("/api/disconnect", disconnect_handler)
    app.router.add_post("/api/transmit", transmit_handler)
    app.router.add_patch("/api/device-settings", update_device_settings_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--set-autostart" and sys.argv[2] in {"on", "off"}:
        set_autostart(sys.argv[2] == "on")
        sys.exit(0)
    if (
        not os.environ.pop("MESHC_OPS_RESTARTING", None)
        and should_auto_open_browser()
        and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    ):
        threading.Timer(
            1.0,
            lambda: webbrowser.open(
                f"http://127.0.0.1:{WEB_PORT}"
            ),
        ).start()
    web.run_app(create_app(), host=WEB_HOST, port=WEB_PORT)
