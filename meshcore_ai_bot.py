import asyncio
import html
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from bleak import BleakScanner
import ollama
from aiohttp import ClientSession, ClientTimeout, web
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
CONFIG_FILE_PATH = Path(__file__).resolve().with_name("config.json")
AVAILABLE_THEMES = {"midnight", "light", "ocean", "amber", "linux", "macos", "cyberpunk"}


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


def load_app_config():
    config = {
        "model": DEFAULT_MODEL,
        "theme": "midnight",
        "connection": {"type": "bluetooth", "ble_mac": "", "serial_port": ""},
        "weather": {"city": "", "state": ""},
        "bot": load_bot_settings(),
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
    if not {"model", "theme", "connection", "weather", "bot"}.issubset(saved_config):
        try:
            write_app_config(config)
        except OSError:
            pass
    return config


def validate_app_config(value):
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object")
    unsupported = set(value) - {"model", "theme", "connection", "weather", "bot"}
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
    if not isinstance(bot, dict) or set(bot) - {"name", "personality"}:
        raise ValueError("Bot settings must contain only name and personality")
    validated_bot = {}
    for key, limit in (("name", 40), ("personality", 120)):
        setting = bot.get(key, app_config["bot"][key])
        if not isinstance(setting, str):
            raise ValueError(f"Bot {key} must be text")
        setting = clean_bot_setting(setting, limit)
        if not setting:
            raise ValueError(f"Bot {key} cannot be empty")
        validated_bot[key] = setting
    return {
        "model": model.strip(),
        "theme": theme,
        "connection": connection_values,
        "weather": weather_values,
        "bot": validated_bot,
    }


def write_app_config(config):
    temporary_path = CONFIG_FILE_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(CONFIG_FILE_PATH)


app_config = load_app_config()
bot_settings = app_config["bot"]


def update_bot_settings_from_prompt(prompt):
    global app_config, bot_settings

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
    return f"Understood. I'll be {value} from now on."

app_state = {
    "connection_type": app_config["connection"]["type"],
    "ble_mac": app_config["connection"]["ble_mac"],
    "serial_port": app_config["connection"]["serial_port"],
    "selected_model": app_config["model"],
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
pending_weather_requests = set()
announced_contact_adverts = {}
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
        "sort_timestamp": datetime.now().timestamp(),
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
    seen_names = set()
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

            # Some devices report the same channel (e.g. "Public") at more
            # than one index; keep only the first one so it isn't listed twice.
            if channel_name and channel_name.casefold() not in seen_names:
                seen_names.add(channel_name.casefold())
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
            result = await request_self_telemetry()

            if result.type != EventType.ERROR:
                gateway_values = parse_lpp_telemetry(result.payload)

        except Exception as error:
            log_to_dash(f"Gateway telemetry request failed: {error}")

    if request_self_info is not None:
        try:
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


def weather_location_from_prompt(prompt):
    location_marker = r"\b(?:in|for|near|around|at)\s+"
    temporal_location = (
        r"(?:today|tonight|tomorrow|right now|this (?:morning|afternoon|evening|week|weekend)|"
        r"(?:the )?next (?:\d+|few|couple of|several) days?|next week|\d+ days?|"
        r"\d{1,2}(?::\d{2})?\s*(?:am|pm))"
    )
    for match in re.finditer(location_marker, prompt, re.IGNORECASE):
        location = re.split(
            location_marker,
            prompt[match.end():],
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        location = re.sub(
            r"\s+\b(?:today|tonight|tomorrow|right now|this morning|this afternoon|"
            r"this evening|this week|this weekend|next week)\b.*$",
            "",
            location,
            flags=re.IGNORECASE,
        ).strip(" \t\r\n.,?!")
        if not location or re.fullmatch(temporal_location, location, re.IGNORECASE):
            continue
        if location.casefold() in {"here", "my location", "my area", "the gateway"}:
            return None
        return location

    location = re.sub(
        r"^\s*(?:what(?:'s| is)\s+)?(?:the\s+)?(?:weather|forecast|temperature)\b"
        r"(?:\s+(?:like|in|for|near|around|at))?\s*",
        "",
        prompt,
        flags=re.IGNORECASE,
    )
    location = re.sub(
        r"\s+\b(?:today|tonight|tomorrow|right now|this morning|this afternoon|"
        r"this evening|this week|this weekend|next week)\b.*$",
        "",
        location,
        flags=re.IGNORECASE,
    ).strip(" \t\r\n.,?!")
    if location and not re.fullmatch(temporal_location, location, re.IGNORECASE):
        if location.casefold() not in {"here", "my location", "my area", "the gateway"}:
            return location
    return None


async def geocode_location(session, location):
    # Open-Meteo's geocoding search wants just a place name, so a combined
    # "city state"/"city country" string (e.g. "hartford connecticut") often
    # returns no matches. Retry with trailing words dropped until it hits.
    words = location.split()
    for word_count in range(len(words), 0, -1):
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


async def fetch_weather_response(prompt, sender_id=None):
    weather_requested = bool(re.search(
        r"\b(?:weather|forecast|temperature|raining|rain|snow|humidity|windy|"
        r"sunny|cloudy|clear|showers|thunderstorm)\b",
        prompt,
        re.IGNORECASE,
    ))
    is_location_followup = sender_id in pending_weather_requests and not weather_requested
    if not weather_requested and not is_location_followup:
        return None

    location = prompt.strip(" \t\r\n.,?!") if is_location_followup else weather_location_from_prompt(prompt)
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
            if location:
                place = await geocode_location(session, location)
                if not place:
                    return f"I couldn't find {location}. Please try a nearby city or town."
                latitude = place["latitude"]
                longitude = place["longitude"]
                location_label = ", ".join(
                    str(place[key]) for key in ("name", "admin1", "country") if place.get(key)
                )
            else:
                telemetry = app_state["gateway_telemetry"]
                latitude = telemetry.get("latitude")
                longitude = telemetry.get("longitude")
                if latitude is None or longitude is None:
                    if sender_id is not None:
                        pending_weather_requests.add(sender_id)
                    return "Which city or town should I check? I don't have a gateway GPS location."
                location_label = "your gateway location"

            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
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
            f"Current weather for {location_label}: {current['temperature_2m']:.1f} {temperature_label}, "
            f"feels like {current['apparent_temperature']:.1f} {temperature_label}, "
            f"{WEATHER_CODES.get(code, 'conditions unavailable')}; "
            f"humidity {current['relative_humidity_2m']}%, "
            f"wind {current['wind_speed_10m']} {wind_label}."
        )
        if forecast_requested:
            daily = weather["daily"]
            for index, day_name in enumerate(("Today", "Tomorrow")):
                answer += (
                    f" {day_name}: {WEATHER_CODES.get(int(daily['weather_code'][index]), 'conditions unavailable')}, "
                    f"high {daily['temperature_2m_max'][index]:.1f} {temperature_label}, "
                    f"low {daily['temperature_2m_min'][index]:.1f} {temperature_label}."
                )
        if sender_id is not None:
            pending_weather_requests.discard(sender_id)
        return answer + " Source: Open-Meteo."
    except Exception as error:
        log_to_dash(f"Live weather lookup failed: {error}")
        return "I couldn't retrieve live weather right now, so I don't want to guess. Please try again shortly."


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

    weather_reply = await fetch_weather_response(prompt, sender_id)
    if weather_reply is not None:
        return weather_reply

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

    # Some MeshCore clients prefix channel text with the sender's name
    # (e.g. "[Alice] /bot ..." or "Alice: /bot ...") before it reaches us,
    # so look for /bot anywhere after a word boundary rather than only at
    # the very start of the message.
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


async def handle_new_contact(event):
    if not meshcore_instance or not app_state["is_connected"]:
        return

    contact = event.payload or {}
    public_key = contact.get("public_key")
    if not public_key:
        return
    advert_timestamp = contact.get("last_advert")
    if announced_contact_adverts.get(str(public_key)) == advert_timestamp:
        return

    channels = list(app_state["channels"])
    if not channels:
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
    async with hardware_lock:
        for channel in channels:
            for part in greeting_parts:
                try:
                    result = await send_to_target(channel, "channel", part)
                    if result.type == EventType.ERROR:
                        log_to_dash(f"Channel {channel} peer greeting rejected: {result.payload}")
                        break
                    add_chat_message("channel", channel, "outgoing", part)
                except Exception as error:
                    log_to_dash(f"Channel {channel} peer greeting failed: {error}")
                    break
    log_to_dash(f"Announced new peer {display_name(public_key, contact)} in {len(channels)} channel(s).")


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

        if meshcore_instance is None:
            raise RuntimeError("MeshCore did not return a connection instance; check device address/port.")

        await meshcore_instance.start_auto_message_fetching()
        meshcore_instance.subscribe(EventType.CONTACT_MSG_RECV, handle_incoming_message)
        meshcore_instance.subscribe(
            EventType.CHANNEL_MSG_RECV,
            handle_incoming_channel_message,
        )
        app_state["is_connected"] = True
        await refresh_contacts()
        await refresh_channels()
        meshcore_instance.subscribe(EventType.NEW_CONTACT, handle_new_contact)
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
.weather-widget{min-width:170px}
.weather-current{display:flex;align-items:center;gap:8px;white-space:nowrap}
.weather-icon{flex:none;width:40px;height:40px;display:grid;place-items:center;color:var(--accent)}
.weather-icon svg{width:36px;height:36px;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;stroke-width:1.7}
.weather-current strong{color:var(--text);font-size:14px;font-weight:700;font-variant-numeric:tabular-nums}
.weather-current span,.weather-location{color:var(--muted);font-size:10px}
.weather-current .weather-icon{color:var(--accent)}
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
.nav-count{min-width:18px;padding:1px 5px;border-radius:10px;background:var(--panel-raised);font:10px ui-monospace,monospace;text-align:center}
.view-panel[hidden]{display:none!important}
.page-view{width:min(100%,1800px);flex:1;margin:0 auto}
.connection-layout{width:min(100%,520px);display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:start}
.settings-layout{width:min(100%,760px);display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:start}
.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.settings-tabs{display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid var(--border)}
.settings-tab{min-height:32px;padding:6px 10px;border-color:transparent;border-bottom:2px solid transparent;border-radius:0;background:transparent;color:var(--muted)}
.settings-tab[aria-pressed="true"]{border-bottom-color:var(--accent);color:var(--accent)}
.settings-tab-panel[hidden]{display:none}
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
.settings-description{margin:8px 0 0;color:var(--muted);font-size:11px}
.card{min-width:0;margin-bottom:12px;padding:12px;background:var(--panel-bg);border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12)}
.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.panel-heading h2{margin:0;color:var(--text);font-size:14px;font-weight:600;line-height:1.25}
.panel-index{color:var(--muted);font:11px ui-monospace,monospace}
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
.chat-panel{height:min(680px,calc(100vh - 270px));min-height:360px;display:flex;flex-direction:column}
.messages-layout{display:grid;grid-template-columns:minmax(210px,280px) minmax(0,1fr);gap:12px}
.conversation-rail{min-height:360px;margin:0;display:flex;flex-direction:column}
.conversation-target-list{display:grid;align-content:start;gap:6px;min-height:0;overflow:auto}
.conversation-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:8px}
.conversation-filters label{min-width:0;margin:0;font-size:10px}
.conversation-filters select{margin-top:4px;padding:7px 20px 7px 7px;font-size:10px}
.conversation-entry{display:grid;grid-template-columns:minmax(0,1fr) 34px;gap:4px}
.conversation-target{display:grid;width:100%;gap:3px;padding:9px;text-align:left}
.conversation-target strong{overflow-wrap:anywhere;color:var(--text);font-size:11px}
.conversation-target small{overflow-wrap:anywhere;color:var(--muted);font-size:10px}
.conversation-target.active{border-color:var(--accent);background:var(--accent-dim)}
.favorite-toggle{width:34px;min-height:36px;padding:4px;color:var(--muted);font-size:17px}
.favorite-toggle[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-dim);color:var(--accent)}
.chat-header{display:grid;gap:2px;min-height:38px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.chat-header strong{color:var(--text);font-size:13px}
.chat-header span{color:var(--muted);font-size:10px;overflow-wrap:anywhere}
.peer-telemetry{max-height:180px;margin:8px 0;padding:9px;overflow:auto;border:1px solid var(--border);border-radius:6px;background:var(--log-bg)}
.peer-telemetry[hidden]{display:none}
.peer-telemetry h3{margin:0 0 6px;color:var(--text);font-size:11px}
.peer-telemetry p{margin:4px 0;color:var(--muted);font-size:10px;overflow-wrap:anywhere}
.peer-telemetry dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 10px;margin:0}
.peer-telemetry dl div{min-width:0}
.peer-telemetry dt{color:var(--muted);font-size:9px}
.peer-telemetry dd{margin:1px 0 0;color:var(--text);font-size:10px;overflow-wrap:anywhere}
#node-chat-history,#channel-chat-history{flex:1;min-height:220px;overflow-y:auto;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--log-bg);white-space:pre-wrap;overflow-wrap:anywhere}
#node-chat-history:empty::before,#channel-chat-history:empty::before{display:block;padding:8px;color:var(--muted);font-size:11px;content:"No messages in this view yet"}
.chat-message{max-width:92%;width:fit-content;margin:6px 0;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--panel-raised);color:var(--text);text-align:left;white-space:pre-wrap;overflow-wrap:anywhere}
.chat-message.incoming{margin-right:auto}
.chat-message.outgoing{margin-left:auto;border-color:color-mix(in srgb,var(--accent) 36%,var(--border));background:var(--accent-dim);color:var(--text)}
.chat-panel form{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:10px}
.chat-panel form input{min-width:0}
.chat-panel form button{border-color:var(--accent);background:var(--accent);color:var(--button-text);white-space:nowrap}
.chat-panel form button:disabled{opacity:.55;cursor:not-allowed}
.device-settings-layout{width:min(100%,820px);display:grid;gap:12px}
.device-settings-layout form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.device-settings-layout label{margin:0}
.device-settings-layout label span{display:block;margin-bottom:5px}
.device-settings-layout .settings-description{grid-column:1/-1;margin:0}
.device-settings-layout .settings-actions{grid-column:1/-1;display:flex;align-items:center;gap:10px}
.custom-radio-fields{grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.custom-radio-fields[hidden],#custom-power-field[hidden]{display:none}
.settings-status{min-height:18px;margin:0;color:var(--muted);font-size:11px}
.settings-status[data-state="error"]{color:var(--danger)}
.settings-status[data-state="success"]{color:var(--accent)}
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
.map-peer-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:8px}
.map-peer-filters label{min-width:0;margin:0;font-size:10px}
.map-peer-filters select{margin-top:4px;padding:7px 20px 7px 7px;font-size:10px}
#map-node-list{flex:1;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--log-bg)}
.map-empty{padding:12px;color:var(--muted);font-size:11px}
.map-node-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px;border-bottom:1px solid var(--border)}
.map-node-row:last-child{border-bottom:0}
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
@media(max-width:720px){body{padding:8px}.dashboard-header{align-items:flex-start;flex-direction:column;gap:12px}.top-nav{order:0;max-width:100%;overflow-x:auto}.nav-tab{flex:none}.header-meta{width:100%;flex-wrap:wrap;justify-content:space-between}.settings-grid,.device-settings-layout form{grid-template-columns:minmax(0,1fr)}.messages-layout{grid-template-columns:minmax(0,1fr)}.conversation-rail{min-height:0;max-height:190px}.chat-panel{height:calc(100vh - 250px);min-height:340px}.device-settings-layout .settings-description,.device-settings-layout .settings-actions{grid-column:1}.map-layout{grid-template-columns:minmax(0,1fr)}.map-rail{min-height:180px;max-height:230px}.map-surface{min-height:48vh}#map-canvas{height:50vh;min-height:320px}.map-toolbar{align-items:flex-start;flex-direction:column}.console-card{height:120px;min-height:120px;max-height:120px}}
</style>
<script>
let gatewayTelemetry={};
let meshChannels=[];
let selectedNodeId='';
let selectedChannelId='';
let activeView='connection';
let deviceSettingsLoaded=false;
let loadedDeviceSettings=null;
let favoriteNodeIds=new Set();
let appConfig={model:'llama3.2:1b',theme:'midnight',connection:{type:'bluetooth',ble_mac:'',serial_port:''},bot:{name:'MeshCore Assistant',personality:'helpful, friendly, and concise'}};
let configEditorLoaded=false;
const commonRadioProfiles={balanced:{radio_bw:125,radio_sf:7,radio_cr:5},long_range:{radio_bw:125,radio_sf:10,radio_cr:5},high_throughput:{radio_bw:250,radio_sf:7,radio_cr:5}};
function applyTheme(theme,persist=true){document.body.dataset.theme=theme;document.getElementById('theme-select').value=theme;if(persist)saveAppConfig({...appConfig,theme})}
function showSettingsTab(tab){document.querySelectorAll('.settings-tab').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.settingsTab===tab)));for(let panel of document.querySelectorAll('.settings-tab-panel'))panel.hidden=panel.id!=='settings-'+tab+'-panel';if(tab==='config'&&!configEditorLoaded)loadConfigEditor()}
function syncConfigControls(){document.getElementById('theme-select').value=appConfig.theme;let modelSelect=document.getElementById('model');if(![...modelSelect.options].some(option=>option.value===appConfig.model))modelSelect.add(new Option(appConfig.model,appConfig.model));modelSelect.value=appConfig.model;document.getElementById('weather-city').value=appConfig.weather.city;document.getElementById('weather-state').value=appConfig.weather.state;applyTheme(appConfig.theme,false)}
const weatherIcons={sun:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></svg>',partly:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="16" cy="7" r="3"/><path d="M16 2v1m0 8v1m5-5h-1m-8 0h-1M5 19h12a3 3 0 0 0 .3-6A5 5 0 0 0 8 11.5 3.8 3.8 0 0 0 5 19Z"/></svg>',cloud:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 19h13a4 4 0 0 0 .4-8A6 6 0 0 0 7 9.5 4.8 4.8 0 0 0 5 19Z"/></svg>',fog:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 14h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 6 4 4 0 0 0 5 14Zm-2 4h14m-10 3h14"/></svg>',rain:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 15h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 7 4 4 0 0 0 5 15Zm2 3-1 2m7-2-1 2m7-2-1 2"/></svg>',snow:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 14h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 6 4 4 0 0 0 5 14Zm2 4h.01M12 19h.01M18 18h.01"/></svg>',storm:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 14h13a3.5 3.5 0 0 0 .3-7A5.5 5.5 0 0 0 7 6 4 4 0 0 0 5 14Zm8 1-3 4h3l-1 3 4-5h-3l1-2"/></svg>'};
function weatherIconName(code){if(code===null||code===undefined||!Number.isFinite(Number(code)))return 'cloud';code=Number(code);if(code===0)return 'sun';if(code===1||code===2)return 'partly';if(code===45||code===48)return 'fog';if(code===51||code===53||code===55||code===56||code===57||code===61||code===63||code===65||code===66||code===67||code===80||code===81||code===82)return 'rain';if(code===71||code===73||code===75||code===77||code===85||code===86)return 'snow';if(code===95||code===96||code===99)return 'storm';return 'cloud'}
function renderWeatherIcon(code,condition='Weather condition unavailable'){let icon=document.getElementById('weather-icon');icon.innerHTML=weatherIcons[weatherIconName(code)]||weatherIcons.cloud;icon.setAttribute('aria-label',condition);icon.title=condition}
async function loadLocalWeather(){let temperature=document.getElementById('weather-temperature'),condition=document.getElementById('weather-condition'),location=document.getElementById('weather-location');condition.textContent='Loading';try{let response=await fetch('/api/local-weather'),data=await response.json();if(!response.ok)throw new Error(data.error||'Weather unavailable');temperature.textContent=Math.round(data.temperature_f)+'°F';condition.textContent=data.condition;location.textContent=data.location;renderWeatherIcon(data.weather_code,data.condition)}catch(error){temperature.textContent='--°F';condition.textContent=error.message.includes('Enter a city')?'Set location':'Unavailable';location.textContent=appConfig.weather.city?(appConfig.weather.state?appConfig.weather.city+', '+appConfig.weather.state:appConfig.weather.city):'Location not set';renderWeatherIcon(null,condition.textContent)}}
async function loadAppConfig(){try{let response=await fetch('/api/config'),data=await response.json();if(!response.ok)throw new Error(data.error||'Settings could not be loaded');appConfig=data;syncConfigControls();loadLocalWeather()}catch(error){let statusMessage=document.getElementById('preferences-status');statusMessage.dataset.state='error';statusMessage.textContent=error.message}}
async function saveAppConfig(config,statusId='preferences-status'){let statusMessage=document.getElementById(statusId);statusMessage.dataset.state='';statusMessage.textContent='Saving config.json...';try{let response=await fetch('/api/config',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)}),data=await response.json();if(!response.ok)throw new Error(data.error||'Settings could not be saved');appConfig=data;syncConfigControls();if(statusId==='config-status'){document.getElementById('config-json-editor').value=JSON.stringify(appConfig,null,2);configEditorLoaded=true}statusMessage.textContent='Saved to config.json.';statusMessage.dataset.state='success'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
async function savePreference(key,value){await saveAppConfig({...appConfig,[key]:value})}
async function saveWeatherLocation(event){event.preventDefault();let city=document.getElementById('weather-city').value.trim(),state=document.getElementById('weather-state').value.trim();if(!city){let statusMessage=document.getElementById('weather-settings-status');statusMessage.textContent='Enter a city.';statusMessage.dataset.state='error';return}await saveAppConfig({...appConfig,weather:{city,state}},'weather-settings-status');if(document.getElementById('weather-settings-status').dataset.state==='success')loadLocalWeather()}
async function loadConfigEditor(){let statusMessage=document.getElementById('config-status');statusMessage.dataset.state='';statusMessage.textContent='Loading config.json...';try{let response=await fetch('/api/config'),data=await response.json();if(!response.ok)throw new Error(data.error||'config.json could not be loaded');appConfig=data;syncConfigControls();document.getElementById('config-json-editor').value=JSON.stringify(appConfig,null,2);configEditorLoaded=true;statusMessage.textContent='Loaded config.json.'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
async function saveConfigFile(){let statusMessage=document.getElementById('config-status'),config;try{config=JSON.parse(document.getElementById('config-json-editor').value)}catch(error){statusMessage.textContent='Invalid JSON: '+error.message;statusMessage.dataset.state='error';return}await saveAppConfig(config,'config-status')}
async function restartDashboard(){let button=document.getElementById('restart-dashboard-button'),statusMessage=document.getElementById('config-status');button.disabled=true;statusMessage.dataset.state='';statusMessage.textContent='Restarting dashboard...';try{await fetch('/api/restart',{method:'POST'})}catch(error){}let attempts=0;async function waitForDashboard(){try{let response=await fetch('/api/status',{cache:'no-store'});if(response.ok){window.location.reload();return}}catch(error){}attempts++;if(attempts>=40){statusMessage.textContent='Dashboard did not restart. Start it again from the terminal.';statusMessage.dataset.state='error';button.disabled=false;return}setTimeout(waitForDashboard,500)}setTimeout(waitForDashboard,500)}
function loadFavoriteNodes(){try{let saved=JSON.parse(localStorage.getItem('meshcore-favorite-nodes')||'[]');if(Array.isArray(saved))favoriteNodeIds=new Set(saved.map(String))}catch(error){favoriteNodeIds=new Set()}}
function toggleNodeFavorite(id){let normalized=String(id);if(favoriteNodeIds.has(normalized))favoriteNodeIds.delete(normalized);else favoriteNodeIds.add(normalized);localStorage.setItem('meshcore-favorite-nodes',JSON.stringify([...favoriteNodeIds]));renderConversationTargets('node');renderKnownPeers();renderMapMarkers()}
function createNodeFavoriteButton(id){let favorite=document.createElement('button'),isFavorite=favoriteNodeIds.has(String(id));favorite.type='button';favorite.className='favorite-toggle';favorite.textContent=isFavorite?'★':'☆';favorite.title=isFavorite?'Remove from favorites':'Add to favorites';favorite.setAttribute('aria-label',favorite.title);favorite.setAttribute('aria-pressed',String(isFavorite));favorite.onclick=()=>toggleNodeFavorite(id);return favorite}
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
function showView(view){let target=document.getElementById(view+'-view');if(!target)return;activeView=view;document.querySelectorAll('.view-panel').forEach(panel=>panel.hidden=panel!==target);document.querySelectorAll('.nav-tab').forEach(tab=>tab.setAttribute('aria-pressed',String(tab.dataset.view===view)));if(view==='map')openMap();if(view==='device-settings'&&!deviceSettingsLoaded)loadDeviceSettings()}
function openMap(){if(!window.L){document.getElementById('map-message').textContent='Map library unavailable. Check your internet connection and reload.';return}if(!dashboardMap){dashboardMap=L.map('map-canvas',{zoomControl:true}).setView([20,0],2);L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(dashboardMap);mapMarkers=L.layerGroup().addTo(dashboardMap)}setTimeout(()=>dashboardMap.invalidateSize(),80);renderMapMarkers()}
function popupContent(title,detail){let content=document.createElement('div');let heading=document.createElement('strong');heading.textContent=title;content.appendChild(heading);if(detail){let line=document.createElement('div');line.textContent=detail;content.appendChild(line)}return content}
function focusMapPoint(latitude,longitude){showView('map');if(dashboardMap&&Number.isFinite(latitude)&&Number.isFinite(longitude))dashboardMap.setView([latitude,longitude],12)}
function appendPeerDetail(container,label,value){if(value===null||value===undefined||value==='')return;let field=document.createElement('div'),term=document.createElement('dt'),description=document.createElement('dd');term.textContent=label;description.textContent=value;field.append(term,description);container.appendChild(field)}
async function loadPeerTelemetry(nodeId,panelId){let panel=document.getElementById(panelId);panel.hidden=false;panel.replaceChildren();let heading=document.createElement('h3');heading.textContent='Peer details';let statusMessage=document.createElement('p');statusMessage.textContent='Requesting telemetry...';let details=document.createElement('dl');panel.append(heading,statusMessage,details);try{let response=await fetch('/api/peer-telemetry?node_id='+encodeURIComponent(nodeId)),data=await response.json();if(!response.ok)throw new Error(data.error||'Peer details could not be loaded');let node=data.node||{},types={0:'Unknown',1:'User',2:'Repeater',3:'Room server',4:'Sensor'};details.replaceChildren();appendPeerDetail(details,'Type',types[Number(node.type)]||'Unknown');let heard=Number(node.last_heard);appendPeerDetail(details,'Last heard',heard>0?new Date(heard*1000).toLocaleString():'Not available');if(Number.isFinite(node.latitude)&&Number.isFinite(node.longitude))appendPeerDetail(details,'Location',node.latitude.toFixed(5)+', '+node.longitude.toFixed(5));let telemetry=data.telemetry||[];for(let item of telemetry){let label=String(item.type||'Telemetry');let value=item.value;if(value&&typeof value==='object')value=Object.entries(value).map(([key,entry])=>key+': '+entry).join(', ');if(value!==null&&value!==undefined)appendPeerDetail(details,label,value)}statusMessage.textContent=telemetry.length?'Latest telemetry reported by this peer.':'No telemetry has been reported by this peer.'}catch(error){statusMessage.textContent=error.message}}
const peerMarkerSvgs={users:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 21a7 7 0 0 1 14 0"/></svg>',repeaters:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20V8M8 20h8M6 11a8 8 0 0 1 12 0M3 8a12 12 0 0 1 18 0"/><circle cx="12" cy="5" r="1"/></svg>','room-servers':'<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="7" rx="1.5"/><rect x="4" y="13" width="16" height="7" rx="1.5"/><path d="M8 7.5h.01M8 16.5h.01M12 7.5h5M12 16.5h5"/></svg>',sensors:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 14.76V5a3 3 0 0 0-6 0v9.76a5 5 0 1 0 6 0Z"/><path d="M11 11v6"/></svg>',unknown:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/></svg>'};
function peerMarkerIcon(peer){let category=nodeCategory(peer);return L.divIcon({className:'',html:`<span class="map-peer-icon map-peer-icon-${category}" aria-label="${category}">${peerMarkerSvgs[category]||peerMarkerSvgs.unknown}</span>`,iconSize:[32,32],iconAnchor:[16,16]})}
function renderMapMarkers(){if(!dashboardMap||!mapMarkers)return;mapMarkers.clearLayers();let bounds=[];for(let peer of sortedFilteredNodes('map-node-sort','map-node-type-filter')){if(!Number.isFinite(peer.latitude)||!Number.isFinite(peer.longitude))continue;let point=[peer.latitude,peer.longitude],marker=L.marker(point,{icon:peerMarkerIcon(peer),title:peer.name+' ('+nodeCategory(peer)+')'}).bindPopup(popupContent(peer.name,peer.id)).addTo(mapMarkers);marker.on('click',()=>loadPeerTelemetry(peer.id,'map-peer-details'));bounds.push(point)}if(Number.isFinite(gatewayTelemetry.latitude)&&Number.isFinite(gatewayTelemetry.longitude)){let point=[gatewayTelemetry.latitude,gatewayTelemetry.longitude];L.circleMarker(point,{radius:9,color:'#0d1117',weight:2,fillColor:'#36d1dc',fillOpacity:1}).bindPopup(popupContent('This gateway','Current radio location')).addTo(mapMarkers);bounds.push(point)}let signature=JSON.stringify(bounds);if(bounds.length&&signature!==mapBoundsSignature){dashboardMap.fitBounds(bounds,{padding:[36,36],maxZoom:12});mapBoundsSignature=signature}else if(!bounds.length){mapBoundsSignature=''}document.getElementById('map-message').hidden=bounds.length>0;document.getElementById('map-message').textContent='No peer or gateway location data is available yet.'}
function renderMapNodes(){let list=document.getElementById('map-node-list');list.replaceChildren();let located=0;for(let peer of mapNodes){let row=document.createElement('div');row.className='map-node-row';let details=document.createElement('div');let name=document.createElement('strong');name.textContent=peer.name;let id=document.createElement('span');id.textContent=peer.id;details.append(name,id);let location=document.createElement('span');let hasLocation=Number.isFinite(peer.latitude)&&Number.isFinite(peer.longitude);location.className='map-location '+(hasLocation?'located':'unlocated');location.textContent=hasLocation?'LOCATED':'NO FIX';if(hasLocation){located++;row.tabIndex=0;row.setAttribute('role','button');row.addEventListener('click',()=>focusMapPoint(peer.latitude,peer.longitude));row.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();row.click()}})}row.append(details,location);list.appendChild(row)}let gatewayLocated=Number.isFinite(gatewayTelemetry.latitude)&&Number.isFinite(gatewayTelemetry.longitude);if(gatewayLocated){located++;let row=document.createElement('div');row.className='map-node-row';let details=document.createElement('div');let name=document.createElement('strong');name.textContent='This gateway';let id=document.createElement('span');id.textContent='Local radio';details.append(name,id);let location=document.createElement('span');location.className='map-location located';location.textContent='LOCATED';row.tabIndex=0;row.setAttribute('role','button');row.addEventListener('click',()=>focusMapPoint(gatewayTelemetry.latitude,gatewayTelemetry.longitude));row.append(details,location);list.appendChild(row)}if(!mapNodes.length&&!gatewayLocated){let empty=document.createElement('div');empty.className='map-empty';empty.textContent='No peers are available yet. Connect to a MeshCore radio to load contacts.';list.appendChild(empty)}document.getElementById('map-node-count').textContent=String(located);document.getElementById('map-peer-total').textContent=String(mapNodes.length);document.getElementById('map-node-summary').textContent=`${located} located / ${mapNodes.length} peers`;renderMapMarkers()}
function nodeCategory(node){let type=String(node.type??'').toLowerCase();if(type==='1'||type==='client')return 'users';if(type==='2'||type==='repeater')return 'repeaters';if(type==='3'||type==='room server'||type==='room_server')return 'room-servers';if(type==='4'||type==='sensor')return 'sensors';return 'unknown'}
function sortedFilteredNodes(sortId='node-sort',typeId='node-type-filter'){let category=document.getElementById(typeId).value,nodes=mapNodes.filter(node=>category==='all'||(category==='favorites'?favoriteNodeIds.has(String(node.id)):nodeCategory(node)===category)),sort=document.getElementById(sortId).value;nodes.sort((left,right)=>{if(sort==='heard')return Number(right.last_heard||0)-Number(left.last_heard||0)||left.name.localeCompare(right.name);if(sort==='messages')return Number(right.last_message_at||0)-Number(left.last_message_at||0)||left.name.localeCompare(right.name);return left.name.localeCompare(right.name)})
return nodes}
function updateMapPeerFilters(){renderKnownPeers();renderMapMarkers()}
function renderKnownPeers(){let list=document.getElementById('map-node-list');if(!list)return;let peers=sortedFilteredNodes('map-node-sort','map-node-type-filter');list.replaceChildren();if(!peers.length){let empty=document.createElement('div');empty.className='map-empty';empty.textContent=mapNodes.length?'No peers match this filter.':'No peers are available yet. Connect to a MeshCore radio to load contacts.';list.appendChild(empty);return}for(let peer of peers){let row=document.createElement('div');row.className='map-node-row';let target=document.createElement('button');target.type='button';target.className='map-peer-target';target.title=Number.isFinite(peer.latitude)&&Number.isFinite(peer.longitude)?'Center map and view peer details':'View peer details';target.onclick=()=>{focusMapPoint(peer.latitude,peer.longitude);loadPeerTelemetry(peer.id,'map-peer-details')};let details=document.createElement('div');let name=document.createElement('strong');name.textContent=peer.name;let id=document.createElement('span');id.textContent=String(peer.id);details.append(name,id);target.appendChild(details);row.append(target,createNodeFavoriteButton(peer.id));list.appendChild(row)}}
function renderConversationTargets(type){let isNode=type==='node',items=isNode?sortedFilteredNodes():meshChannels,list=document.getElementById(isNode?'node-target-list':'channel-target-list'),selected=isNode?selectedNodeId:selectedChannelId;list.replaceChildren();if(!items.length){let empty=document.createElement('p');empty.className='map-empty';empty.textContent=isNode?(mapNodes.length?'No nodes match this filter.':'No nodes found. Connect to a MeshCore radio to load contacts.'):'No channels found on this device.';list.appendChild(empty);return}for(let item of items){let button=document.createElement('button');button.type='button';button.className='conversation-target'+(selected===String(item.id)?' active':'');button.onclick=()=>selectConversation(type,item.id);let title=document.createElement('strong');title.textContent=item.name;let detail=document.createElement('small');detail.textContent=String(item.id);button.append(title,detail);if(isNode){let entry=document.createElement('div');entry.className='conversation-entry';entry.append(button,createNodeFavoriteButton(item.id));list.appendChild(entry)}else list.appendChild(button)}}
function selectConversation(type,id){let normalized=String(id);if(type==='node'){selectedNodeId=normalized;document.getElementById('node-chat-title').textContent=mapNodes.find(item=>String(item.id)===normalized)?.name||normalized;document.getElementById('node-chat-detail').textContent=normalized;document.getElementById('node-send').disabled=false;renderConversationTargets('node');history('node',normalized,'node-chat-history');loadPeerTelemetry(normalized,'node-peer-details')}else{selectedChannelId=normalized;document.getElementById('channel-chat-title').textContent=meshChannels.find(item=>String(item.id)===normalized)?.name||'Channel '+normalized;document.getElementById('channel-chat-detail').textContent='Channel '+normalized;document.getElementById('channel-send').disabled=false;renderConversationTargets('channel');history('channel',normalized,'channel-chat-history')}}
async function peers(){let r=await fetch('/api/peers'),d=await r.json();gatewayTelemetry=d.gateway_telemetry||{};mapNodes=d.nodes||[];meshChannels=d.channels||[];gateway_battery.textContent=gatewayTelemetry.battery!=null?gatewayTelemetry.battery+'%':'Unavailable';if(!mapNodes.some(item=>String(item.id)===selectedNodeId))selectedNodeId='';if(!meshChannels.some(item=>String(item.id)===selectedChannelId))selectedChannelId='';renderConversationTargets('node');renderConversationTargets('channel');renderMapNodes();renderKnownPeers()}
async function history(type,id,boxId){let box=document.getElementById(boxId);if(!id){box.replaceChildren();return}try{let response=await fetch('/api/chat-history?target_type='+encodeURIComponent(type)+'&target='+encodeURIComponent(id)),data=await response.json();if(!response.ok)throw new Error(data.error||'Message history could not be loaded');box.replaceChildren();for(let message of data.messages||[]){let item=document.createElement('div');item.className='chat-message '+(message.direction==='outgoing'?'outgoing':'incoming');item.textContent='['+message.timestamp+'] '+message.text;box.appendChild(item)}box.scrollTop=box.scrollHeight}catch(error){box.textContent=error.message}}
function selectNode(id){selectConversation('node',id)}
function selectChannel(id){selectConversation('channel',id)}
function refreshActiveHistory(){if(activeView==='nodes'&&selectedNodeId)history('node',selectedNodeId,'node-chat-history');if(activeView==='channels'&&selectedChannelId)history('channel',selectedChannelId,'channel-chat-history')}
function setCustomRadioMode(){let custom=document.getElementById('custom-radio-fields'),enabled=document.getElementById('radio-profile').value==='custom';custom.hidden=!enabled;custom.querySelectorAll('input,select').forEach(input=>input.disabled=!enabled)}
function setCustomPowerMode(){let custom=document.getElementById('custom-power-field'),enabled=document.getElementById('tx-power-mode').value==='custom';custom.hidden=!enabled;custom.querySelector('input').disabled=!enabled}
function populatePowerOptions(maximum,current){let select=document.getElementById('tx-power-mode'),common=[10,14,17,20];select.replaceChildren();for(let value of common){if(value<=maximum)select.add(new Option(value+' dBm',String(value)))}if(!common.includes(Number(current))&&Number(current)<=maximum)select.add(new Option(current+' dBm (current)',String(current)));let currentIsCommon=[...select.options].some(option=>Number(option.value)===Number(current));select.add(new Option('Custom...','custom'));select.value=currentIsCommon?String(current):'custom';document.getElementById('custom-tx-power').value=current??'';document.getElementById('custom-tx-power').max=maximum;setCustomPowerMode()}
async function loadDeviceSettings(){deviceSettingsLoaded=false;loadedDeviceSettings=null;let statusMessage=document.getElementById('device-settings-status');statusMessage.dataset.state='';statusMessage.textContent='Loading settings from device...';try{let response=await fetch('/api/device-settings'),data=await response.json();if(!response.ok)throw new Error(data.error||'Device settings could not be loaded');loadedDeviceSettings=data;document.getElementById('device-name').value=data.name||'';document.getElementById('custom-radio-frequency').value=data.radio_freq??'';document.getElementById('custom-radio-bandwidth').value=data.radio_bw??'';document.getElementById('custom-radio-spreading-factor').value=data.radio_sf??'';document.getElementById('custom-radio-coding-rate').value=data.radio_cr??'';let matchingProfile=Object.entries(commonRadioProfiles).find(([,profile])=>Number(profile.radio_bw)===Number(data.radio_bw)&&Number(profile.radio_sf)===Number(data.radio_sf)&&Number(profile.radio_cr)===Number(data.radio_cr));document.getElementById('radio-profile').value=matchingProfile?.[0]||'custom';setCustomRadioMode();let maximum=Number(data.max_tx_power??30);populatePowerOptions(maximum,data.tx_power);document.getElementById('tx-power-limit').textContent='Device maximum: '+maximum+' dBm';deviceSettingsLoaded=true;statusMessage.textContent='Settings loaded from device.';statusMessage.dataset.state='success'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
async function saveDeviceSettings(event){event.preventDefault();let statusMessage=document.getElementById('device-settings-status'),form=new FormData(event.currentTarget);if(!loadedDeviceSettings){statusMessage.textContent='Load settings from the connected device first.';statusMessage.dataset.state='error';return}let profile=form.get('radio_profile'),values={name:String(form.get('name')||'').trim()},radio=profile==='custom'?{radio_freq:Number(form.get('radio_freq')),radio_bw:Number(form.get('radio_bw')),radio_sf:Number(form.get('radio_sf')),radio_cr:Number(form.get('radio_cr'))}:{radio_freq:Number(loadedDeviceSettings.radio_freq),...commonRadioProfiles[profile]};Object.assign(values,radio);let powerMode=form.get('tx_power_mode');values.tx_power=Number(powerMode==='custom'?form.get('custom_tx_power'):powerMode);statusMessage.dataset.state='';statusMessage.textContent='Saving settings to device...';try{let response=await fetch('/api/device-settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})}),data=await response.json();if(!response.ok)throw new Error(data.error||'Settings could not be saved');deviceSettingsLoaded=false;await loadDeviceSettings();statusMessage.textContent='Device settings saved.';statusMessage.dataset.state='success'}catch(error){statusMessage.textContent=error.message;statusMessage.dataset.state='error'}}
async function connect(){let r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({connection_type:connection_type.value,ble_mac:ble_mac.value,serial_port:serial_port.value,model:model.value})});let d=await r.json();if(!r.ok)alert(d.error);await status();await peers()}
async function disconnect(){await fetch('/api/disconnect',{method:'POST'});await status();await peers()}
async function sendMessage(event,type,messageId,historyId){event.preventDefault();let selected=type==='node'?selectedNodeId:selectedChannelId,input=document.getElementById(messageId);if(!selected)return;let message=input.value.trim();if(!message)return;let response=await fetch('/api/transmit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:selected,target_type:type,text:message})}),data=await response.json();if(!response.ok){alert(data.error||'Message could not be sent');return}input.value='';await history(type,selected,historyId)}
window.addEventListener('DOMContentLoaded',()=>{loadFavoriteNodes();fields();status();peers();loadAppConfig();updateClock();setInterval(updateClock,1000);setInterval(refreshActiveHistory,3000);setInterval(loadLocalWeather,30*60*1000)});setInterval(status,2000);setInterval(peers,10000);
</script></head><body>
<header class="dashboard-header">
<div class="brand-lockup"><div class="brand-mark">MC</div><div class="brand-copy"><span class="header-label">LOCAL MESH / RADIO CONTROL</span><h1>MESHCORE <span>AI GATEWAY</span></h1></div></div>
<nav class="top-nav" aria-label="Dashboard pages"><button type="button" class="nav-tab" data-view="connection" aria-pressed="true" onclick="showView('connection')">Connection</button><button type="button" class="nav-tab" data-view="nodes" aria-pressed="false" onclick="showView('nodes')">Nodes</button><button type="button" class="nav-tab" data-view="channels" aria-pressed="false" onclick="showView('channels')">Channels</button><button type="button" class="nav-tab" data-view="map" aria-pressed="false" onclick="showView('map')">Map <span class="nav-count" id="map-node-count">0</span></button><button type="button" class="nav-tab" data-view="settings" aria-pressed="false" onclick="showView('settings')">App Settings</button><button type="button" class="nav-tab" data-view="device-settings" aria-pressed="false" onclick="showView('device-settings')">Device Settings</button></nav>
<div class="header-meta">
<div><span class="header-label">LINK</span><span id="status" class="header-status disconnected">DISCONNECTED</span></div>
<div><span class="header-label">GATEWAY BATTERY</span><span id="gateway_battery" class="header-metric">Unavailable</span></div>
<div class="weather-widget" aria-live="polite"><span class="header-label">LOCAL WEATHER</span><div class="weather-current"><span id="weather-icon" class="weather-icon" role="img" aria-label="Weather condition unavailable" title="Weather condition unavailable"></span><strong id="weather-temperature">--°F</strong><span id="weather-condition">Set location</span></div><span id="weather-location" class="weather-location">Location not set</span></div>
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
<div class="messages-layout">
<aside class="card conversation-rail"><div class="panel-heading"><div><span class="eyebrow">DIRECT MESSAGES</span><h2>Nodes</h2></div><span class="panel-index">02</span></div><div class="conversation-filters"><label for="node-sort">Sort<select id="node-sort" onchange="renderConversationTargets('node')"><option value="az">A-Z</option><option value="heard">Heard recently</option><option value="messages">Latest messages</option></select></label><label for="node-type-filter">Type<select id="node-type-filter" onchange="renderConversationTargets('node')"><option value="all">All</option><option value="favorites">Favorites</option><option value="users">Users</option><option value="repeaters">Repeaters</option><option value="room-servers">Room servers</option><option value="sensors">Sensors</option></select></label></div><div class="conversation-target-list" id="node-target-list"><p class="map-empty">Waiting for nodes...</p></div></aside>
<section class="card chat-panel"><div class="chat-header"><strong id="node-chat-title">Select a node</strong><span id="node-chat-detail">Choose a node to view its conversation.</span></div><section id="node-peer-details" class="peer-telemetry" hidden aria-live="polite"></section><div id="node-chat-history"></div><form onsubmit="sendMessage(event,'node','node-message','node-chat-history')"><input id="node-message" maxlength="100" placeholder="Message selected node" required><button id="node-send" disabled>Send to Node</button></form></section>
</div>
</main>
<main id="channels-view" class="view-panel page-view" hidden>
<div class="messages-layout">
<aside class="card conversation-rail"><div class="panel-heading"><div><span class="eyebrow">SHARED FREQUENCY</span><h2>Channels</h2></div><span class="panel-index">03</span></div><div class="conversation-target-list" id="channel-target-list"><p class="map-empty">Waiting for channels...</p></div></aside>
<section class="card chat-panel"><div class="chat-header"><strong id="channel-chat-title">Select a channel</strong><span id="channel-chat-detail">Choose a channel to view its conversation.</span></div><div id="channel-chat-history"></div><form onsubmit="sendMessage(event,'channel','channel-message','channel-chat-history')"><input id="channel-message" maxlength="100" placeholder="Message selected channel" required><button id="channel-send" disabled>Send to Channel</button></form></section>
</div>
</main>
<main id="settings-view" class="view-panel page-view" hidden>
<div class="settings-layout">
<section class="card"><div class="panel-heading"><div><span class="eyebrow">APPLICATION</span><h2>Settings</h2></div><span class="panel-index">04</span></div>
<div class="settings-tabs" role="tablist" aria-label="Settings sections"><button type="button" class="settings-tab" role="tab" data-settings-tab="preferences" aria-pressed="true" onclick="showSettingsTab('preferences')">Preferences</button><button type="button" class="settings-tab" role="tab" data-settings-tab="weather" aria-pressed="false" onclick="showSettingsTab('weather')">Weather</button><button type="button" class="settings-tab" role="tab" data-settings-tab="config" aria-pressed="false" onclick="showSettingsTab('config')">config.json</button></div>
<section id="settings-preferences-panel" class="settings-tab-panel">
<div class="settings-grid">
<div class="settings-item"><label for="theme-select">Color theme</label><select id="theme-select" onchange="applyTheme(this.value)"><option value="midnight">Midnight</option><option value="light">Light</option><option value="ocean">Ocean</option><option value="amber">Amber</option><option value="linux">Linux Console</option><option value="macos">macOS</option><option value="cyberpunk">Hacker Cyberpunk</option></select><p class="settings-description">Saved in config.json and applied to this dashboard.</p></div>
<div class="settings-item"><label for="model">Ollama model</label><select id="model" onchange="savePreference('model',this.value)">{{MODEL_OPTIONS}}</select><p class="settings-description">Saved in config.json and used for bot replies.</p></div>
</div>
<p id="preferences-status" class="preferences-status" aria-live="polite"></p>
</section>
<section id="settings-config-panel" class="settings-tab-panel" hidden>
<label for="config-json-editor">config.json contents</label><textarea id="config-json-editor" class="config-json-editor" rows="18" spellcheck="false" aria-label="Edit config.json"></textarea>
<div class="config-actions"><button type="button" onclick="saveConfigFile()">Save config.json</button><button id="restart-dashboard-button" type="button" onclick="restartDashboard()">Restart Dashboard</button><p id="config-status" class="config-status" aria-live="polite"></p></div>
</section>
<section id="settings-weather-panel" class="settings-tab-panel" hidden>
<form class="weather-settings-layout" onsubmit="saveWeatherLocation(event)">
<label for="weather-city">City<input id="weather-city" name="city" maxlength="80" value="" required></label>
<label for="weather-state">State (optional)<input id="weather-state" name="state" maxlength="80" value="" placeholder="Optional"></label>
<div class="settings-actions"><button type="submit">Save location</button><p id="weather-settings-status" class="preferences-status" aria-live="polite"></p></div>
</form>
</section>
</section>
</div>
</main>
<main id="device-settings-view" class="view-panel page-view" hidden>
<div class="device-settings-layout">
<section class="card"><div class="panel-heading"><div><span class="eyebrow">CONNECTED RADIO</span><h2>MeshCore Device Settings</h2></div><span class="panel-index">05</span></div>
<form onsubmit="saveDeviceSettings(event)">
<label><span>Device name</span><input id="device-name" name="name" maxlength="32" required></label>
<label><span>Common radio profile</span><select id="radio-profile" name="radio_profile" onchange="setCustomRadioMode()"><option value="balanced">Balanced · 125 kHz / SF7 / CR 4/5</option><option value="long_range">Long range · 125 kHz / SF10 / CR 4/5</option><option value="high_throughput">Higher throughput · 250 kHz / SF7 / CR 4/5</option><option value="custom">Custom settings...</option></select></label>
<div id="custom-radio-fields" class="custom-radio-fields" hidden>
<label><span>Radio frequency (MHz)</span><input id="custom-radio-frequency" name="radio_freq" type="number" min="100" max="3000" step="0.001" required disabled></label>
<label><span>Bandwidth (kHz)</span><input id="custom-radio-bandwidth" name="radio_bw" type="number" min="7.8" max="1000" step="0.1" required disabled></label>
<label><span>Spreading factor</span><input id="custom-radio-spreading-factor" name="radio_sf" type="number" min="5" max="12" step="1" required disabled></label>
<label><span>Coding rate</span><select id="custom-radio-coding-rate" name="radio_cr" disabled><option value="5">4/5</option><option value="6">4/6</option><option value="7">4/7</option><option value="8">4/8</option></select></label>
</div>
<label><span>Transmit power (dBm)</span><select id="tx-power-mode" name="tx_power_mode" onchange="setCustomPowerMode()"></select><small id="tx-power-limit" class="settings-description"></small></label>
<label id="custom-power-field" hidden><span>Custom transmit power (dBm)</span><input id="custom-tx-power" name="custom_tx_power" type="number" min="0" step="1" required disabled></label>
<p class="settings-description">Common profiles keep the current frequency. Custom settings let you edit each radio value. Confirm your local frequency plan and device limits before saving.</p>
<div class="settings-actions"><button type="submit">Save device settings</button><button class="secondary" type="button" onclick="loadDeviceSettings()">Refresh from device</button><p id="device-settings-status" class="settings-status" aria-live="polite"></p></div>
</form>
</section>
</div>
</main>
<main id="map-view" class="map-workspace view-panel page-view" hidden>
<section class="card map-toolbar"><div><span class="eyebrow">LIVE MESH POSITIONS</span><h2>Network Map</h2></div><span class="live-tag" id="map-node-summary">0 of 0 locations</span></section>
<div class="map-layout">
<aside class="card map-rail"><div class="panel-heading"><div><span class="eyebrow">KNOWN PEERS</span><h2>Nodes</h2></div><span class="panel-index" id="map-peer-total">0</span></div><p class="map-rail-summary">Select a peer to view its details and telemetry.</p><div class="map-peer-filters"><label for="map-node-sort">Sort<select id="map-node-sort" onchange="updateMapPeerFilters()"><option value="az">A-Z</option><option value="heard">Heard recently</option><option value="messages">Latest messages</option></select></label><label for="map-node-type-filter">Type<select id="map-node-type-filter" onchange="updateMapPeerFilters()"><option value="all">All</option><option value="favorites">Favorites</option><option value="users">Users</option><option value="repeaters">Repeaters</option><option value="room-servers">Room servers</option><option value="sensors">Sensors</option></select></label></div><section id="map-peer-details" class="peer-telemetry" hidden aria-live="polite"></section><div id="map-node-list"><div class="map-empty">Waiting for nodes...</div></div></aside>
<section class="card map-surface" aria-label="Mesh node map"><div id="map-canvas"></div><div class="map-message" id="map-message">Waiting for map data...</div></section>
</div>
</main>
<footer id="console-dock" class="console-dock"><section class="card console-card"><div class="panel-heading"><div><span class="eyebrow">SYSTEM ACTIVITY</span><h2>Console</h2></div><span class="live-tag">LIVE</span></div><pre id="console"></pre></section></footer>
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


async def status_handler(request):
    return web.json_response({
        "is_connected": app_state["is_connected"],
        "logs": app_state["logs"],
    })


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
                    "current": "temperature_2m,weather_code",
                    "temperature_unit": "fahrenheit",
                    "timezone": "auto",
                },
            ) as response:
                response.raise_for_status()
                current = (await response.json())["current"]

        return web.json_response({
            "location": ", ".join(
                str(place[key]) for key in ("name", "admin1", "country") if place.get(key)
            ),
            "temperature_f": current["temperature_2m"],
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


async def restart_dashboard_handler(request):
    async def restart_dashboard():
        await asyncio.sleep(0.3)
        await disconnect_hardware()
        os.environ["MESHC_OPS_RESTARTING"] = "1"
        os.execv(sys.executable, [sys.executable, *sys.argv])

    asyncio.create_task(restart_dashboard())
    return web.json_response({"restarting": True}, status=202)


async def peers_handler(request):
    await refresh_contacts()
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
            "type": contact.get("type", entry.get("type") if isinstance(entry, dict) else None),
            "last_heard": contact.get("last_advert", contact.get("last_heard", 0)),
            "last_message_at": latest_message.get("sort_timestamp", 0),
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
        async with hardware_lock:
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
            "type": contact.get("type"),
            "last_heard": contact.get("last_advert", contact.get("last_heard")),
            "latitude": coordinates[0] if coordinates else None,
            "longitude": coordinates[1] if coordinates else None,
        },
        "telemetry": telemetry or [],
    })


async def chat_history_handler(request):
    target_type = request.query.get("target_type", "node")
    target = request.query.get("target", "")
    return web.json_response({"messages": chat_history.get(chat_key(target_type, target), [])})


def validate_device_settings(current, values):
    if not isinstance(values, dict) or not values:
        raise ValueError("Provide at least one device setting to update")

    allowed = {"name", "radio_freq", "radio_bw", "radio_sf", "radio_cr", "tx_power"}
    unsupported = set(values) - allowed
    if unsupported:
        raise ValueError(f"Unsupported device settings: {', '.join(sorted(unsupported))}")

    updated = {key: current.get(key) for key in allowed}
    updated.update(values)

    name = updated.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.encode("utf-8")) > 32:
        raise ValueError("Device name must contain 1 to 32 UTF-8 bytes")
    updated["name"] = name.strip()

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
        ("tx_power", 0, int(current.get("max_tx_power", 30))),
    ):
        value = updated[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return updated


async def device_settings_handler(request):
    if not meshcore_instance or not app_state["is_connected"]:
        return web.json_response({"error": "MeshCore is not connected"}, status=503)

    try:
        async with hardware_lock:
            result = await meshcore_instance.commands.send_appstart()
        if result.type == EventType.ERROR:
            return web.json_response({"error": str(result.payload)}, status=502)
        return web.json_response(result.payload)
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
        async with hardware_lock:
            commands = meshcore_instance.commands
            current_result = await commands.send_appstart()
            if current_result.type == EventType.ERROR:
                return web.json_response({"error": str(current_result.payload)}, status=502)
            current = current_result.payload
            updated = validate_device_settings(current, values)

            if updated["name"] != current.get("name"):
                result = await commands.set_name(updated["name"])
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            radio_keys = ("radio_freq", "radio_bw", "radio_sf", "radio_cr")
            if any(updated[key] != current.get(key) for key in radio_keys):
                result = await commands.set_radio(
                    updated["radio_freq"],
                    updated["radio_bw"],
                    updated["radio_sf"],
                    updated["radio_cr"],
                )
                if result.type == EventType.ERROR:
                    raise RuntimeError(str(result.payload))

            if updated["tx_power"] != current.get("tx_power"):
                result = await commands.set_tx_power(updated["tx_power"])
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
    app.router.add_get("/api/config", config_handler)
    app.router.add_patch("/api/config", update_config_handler)
    app.router.add_get("/api/local-weather", local_weather_handler)
    app.router.add_post("/api/restart", restart_dashboard_handler)
    app.router.add_get("/api/peers", peers_handler)
    app.router.add_get("/api/peer-telemetry", peer_telemetry_handler)
    app.router.add_get("/api/scan/bluetooth", bluetooth_scan_handler)
    app.router.add_get("/api/scan/serial", serial_scan_handler)
    app.router.add_get("/api/chat-history", chat_history_handler)
    app.router.add_get("/api/device-settings", device_settings_handler)
    app.router.add_post("/api/connect", connect_handler)
    app.router.add_post("/api/disconnect", disconnect_handler)
    app.router.add_post("/api/transmit", transmit_handler)
    app.router.add_patch("/api/device-settings", update_device_settings_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    if not os.environ.pop("MESHC_OPS_RESTARTING", None):
        threading.Timer(
            1.0,
            lambda: webbrowser.open(
                f"http://127.0.0.1:{WEB_PORT}"
            ),
        ).start()
    web.run_app(create_app(), host=WEB_HOST, port=WEB_PORT)
