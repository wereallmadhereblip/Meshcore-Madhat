import asyncio
import html
import inspect
import threading
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import ollama
from aiohttp import web
from meshcore import EventType, MeshCore

DEFAULT_BLE_MAC = "A4:CB:8F:A6:67:39"
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_MODEL = "llama3.2:1b"
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
MAX_HISTORY_LENGTH = 2
MAX_CHANNELS = 40
BATTERY_MIN_MV = 3200
BATTERY_MAX_MV = 4200
NEWS_FEED_URLS = [
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "https://feeds.bbci.co.uk/news/rss.xml",
]
HACKER_NEWS_URL = "https://hnrss.org/frontpage"
FALLBACK_NEWS = [
    "Google News: Global markets steady as investors assess policy signals.",
    "BBC: Energy and transport sectors remain under close watch amid supply shifts.",
    "Tech: AI infrastructure spending keeps technology stocks in focus.",
    "Markets: Shipping and logistics firms track weather and congestion risks.",
]

app_state = {
    "connection_type": "bluetooth",
    "ble_mac": DEFAULT_BLE_MAC,
    "serial_port": DEFAULT_SERIAL_PORT,
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


def log_to_dash(message):
    formatted = f"[{datetime.now():%H:%M:%S}] {message}"
    print(formatted)
    app_state["logs"].append(formatted)
    app_state["logs"] = app_state["logs"][-50:]


def fetch_rss_headlines(url):
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=15) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        headlines = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if title:
                headlines.append(title)
        if headlines:
            return headlines[:20]

        for entry in root.findall(".//entry"):
            title = (entry.findtext("title") or "").strip()
            if title:
                headlines.append(title)
        return headlines[:20]
    except Exception:
        return []


def get_news_headlines():
    for url in NEWS_FEED_URLS:
        headlines = fetch_rss_headlines(url)
        if headlines:
            return headlines
    return FALLBACK_NEWS


def get_hacker_news_headlines():
    headlines = fetch_rss_headlines(HACKER_NEWS_URL)
    if headlines:
        return headlines
    return [
        "Hacker News: Community discussions keep pushing new developer tools and ideas.",
        "Hacker News: AI experiments and open-source releases continue to lead the signal.",
        "Hacker News: Security, systems, and product engineering remain the hot topics.",
    ]


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


def chat_key(target_type, target):
    return f"{target_type}:{target}"


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


async def update_available_models():
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(executor, ollama.list)
        models = [m.get("name") for m in result.get("models", []) if m.get("name")]
        if models:
            app_state["available_models"] = models
    except Exception as error:
        log_to_dash(f"Failed to fetch Ollama models: {error}")


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


async def generate_ai_response(sender_id, prompt):
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
        "You are a helpful AI assistant for a mesh messaging bot. "
        f"The current date and time is {datetime.now():%A, %B %d, %Y at %I:%M %p}. "
        "Answer the user's actual question directly. Do not mention network "
        "delays unless asked. Reply in one sentence of no more than 15 words."
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
        if len(reply) > 100:
            reply = reply[:97] + "..."
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

    async with hardware_lock:
        try:
            contacts = await meshcore_instance.commands.get_contacts()
            recipient = sender
            if contacts.type != EventType.ERROR and contacts.payload:
                recipient = contacts.payload.get(sender, sender)
            result = await meshcore_instance.commands.send_msg(recipient, reply)
            if result.type == EventType.ERROR:
                log_to_dash(f"Hardware rejected message: {result.payload}")
            else:
                add_chat_message("node", sender, "outgoing", reply)
                log_to_dash("Direct message reply sent successfully.")
        except Exception as error:
            log_to_dash(f"Message send error: {error}")


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
<style>
body{--page-bg:#121212;--panel-bg:#1e1e1e;--text:#eee;--muted:#999;--accent:#00ff66;--input-bg:#2d2d2d;--input-border:#444;--button-text:#121212;--log-bg:#000;--font:sans-serif;--radius:8px;--shadow:none;--panel-border:0 solid transparent;--page-pattern:none;--button-transform:none;font-family:var(--font);background:var(--page-bg);background-image:var(--page-pattern);color:var(--text);margin:20px}
body[data-theme="light"]{--page-bg:#eef2f5;--panel-bg:#fff;--text:#17202a;--muted:#607080;--accent:#087f5b;--input-bg:#f7f9fb;--input-border:#b8c4ce;--button-text:#fff;--log-bg:#17202a;--font:"Trebuchet MS",sans-serif;--radius:14px;--shadow:0 8px 24px rgba(26,44,62,.12);--panel-border:1px solid #d8e0e7;--page-pattern:radial-gradient(#d6e0e8 1px,transparent 1px);--button-transform:none}
body[data-theme="ocean"]{--page-bg:#071a2b;--panel-bg:#0d2b43;--text:#e5f6ff;--muted:#91b8ca;--accent:#36d1dc;--input-bg:#123b56;--input-border:#28617c;--button-text:#071a2b;--log-bg:#04111d;--font:"Segoe UI",sans-serif;--radius:4px;--shadow:0 12px 30px rgba(0,0,0,.28);--panel-border:1px solid #1d5571;--page-pattern:linear-gradient(135deg,rgba(54,209,220,.05) 25%,transparent 25%,transparent 50%,rgba(54,209,220,.05) 50%,rgba(54,209,220,.05) 75%,transparent 75%);--button-transform:none}
body[data-theme="amber"]{--page-bg:#21180d;--panel-bg:#342311;--text:#fff4dd;--muted:#c5a879;--accent:#ffb703;--input-bg:#4a3215;--input-border:#80602c;--button-text:#21180d;--log-bg:#160f08;--font:"Courier New",monospace;--radius:0;--shadow:4px 4px 0 rgba(255,183,3,.16);--panel-border:1px solid #795722;--page-pattern:repeating-linear-gradient(0deg,rgba(255,183,3,.035) 0,rgba(255,183,3,.035) 1px,transparent 1px,transparent 4px);--button-transform:uppercase}
body[data-theme="linux"]{--page-bg:#020502;--panel-bg:#081008;--text:#d7ffd0;--muted:#6e9b6a;--accent:#39ff14;--input-bg:#050b05;--input-border:#245b24;--button-text:#020502;--log-bg:#000;--font:"DejaVu Sans Mono",monospace;--radius:0;--shadow:0 0 0 1px rgba(57,255,20,.2);--panel-border:1px solid #245b24;--page-pattern:repeating-linear-gradient(0deg,rgba(57,255,20,.025) 0,rgba(57,255,20,.025) 1px,transparent 1px,transparent 3px);--button-transform:uppercase}
body[data-theme="macos"]{--page-bg:#e7ebf0;--panel-bg:rgba(255,255,255,.92);--text:#1d1d1f;--muted:#6e6e73;--accent:#007aff;--input-bg:#f5f5f7;--input-border:#c7c7cc;--button-text:#fff;--log-bg:#1d1d1f;--font:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--radius:14px;--shadow:0 10px 30px rgba(0,0,0,.12);--panel-border:1px solid rgba(0,0,0,.08);--page-pattern:linear-gradient(135deg,#eef1f5,#dfe5ec);--button-transform:none}
body[data-theme="cyberpunk"]{--page-bg:#090511;--panel-bg:#160b24;--text:#f8eaff;--muted:#a56fba;--accent:#ff2bd6;--input-bg:#211033;--input-border:#74358c;--button-text:#090511;--log-bg:#050208;--font:"Courier New",monospace;--radius:2px;--shadow:0 0 18px rgba(255,43,214,.22),inset 0 0 12px rgba(0,234,255,.06);--panel-border:1px solid #8d2da2;--page-pattern:repeating-linear-gradient(135deg,rgba(0,234,255,.045) 0,rgba(0,234,255,.045) 1px,transparent 1px,transparent 12px);--button-transform:uppercase}
body[data-theme="macos"] #node-chat-history,body[data-theme="macos"] #channel-chat-history{background:#f5f5f7;border:1px solid #d1d1d6;border-radius:12px;color:#1d1d1f}
body[data-theme="macos"] .chat-message{max-width:78%;width:fit-content;margin:6px 0;padding:9px 13px;border-radius:18px;background:#e5e5ea;color:#1d1d1f;text-align:left;white-space:pre-wrap}
body[data-theme="macos"] .chat-message.incoming{margin-right:auto}
body[data-theme="macos"] .chat-message.outgoing{margin-left:auto;background:#007aff;color:#fff;text-align:left}
body[data-theme="macos"] .card:has(#node-chat-history),body[data-theme="macos"] .card:has(#channel-chat-history){background:rgba(255,255,255,.96)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.card{background:var(--panel-bg);padding:20px;border:var(--panel-border);border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:20px;cursor:grab}
.card.dragging{opacity:.45;cursor:grabbing}
.console-card{box-sizing:border-box;width:100%}
.dashboard-header{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:20px}
.dashboard-header h1{margin:0}
.header-meta{display:flex;gap:18px;align-items:flex-start;text-align:right}
.header-metric{color:var(--accent);font-weight:bold;white-space:nowrap}
.header-label{display:block;color:var(--muted);font-size:12px;margin-bottom:3px}
.header-status{display:inline-block;padding:4px 8px;border-radius:4px;font-size:12px;font-weight:bold;white-space:nowrap}
.header-status.connected{background:#1b5e20;color:#b7e1cd}
.header-status.disconnected{background:#b71c1c;color:#f4c7c7}
h1,h2{color:var(--accent)}label{display:block;margin:9px 0 4px;font-weight:bold}
input,select,button{box-sizing:border-box;width:100%;padding:10px;margin-bottom:10px;border-radius:var(--radius)}
input,select{background:var(--input-bg);color:var(--text);border:1px solid var(--input-border)}
button{background:var(--accent);color:var(--button-text);border:0;font-weight:bold;text-transform:var(--button-transform);cursor:pointer}
.status{display:inline-block;padding:8px;border-radius:4px;font-weight:bold}
.connected{background:#1b5e20}.disconnected{background:#b71c1c}
.metric{color:var(--accent);font-size:22px;font-weight:bold}
pre,#node-chat-history,#channel-chat-history{height:220px;overflow-y:auto;padding:12px;background:var(--log-bg);white-space:pre-wrap}
.chat-message{padding:6px}.incoming{color:#fff}.outgoing{color:#00ff66;text-align:right}
@media(max-width:768px){.grid{grid-template-columns:1fr}.console-card{width:100%}.dashboard-header{display:block}.header-meta{margin-top:12px;text-align:left;justify-content:space-between}}
</style>
<script>
let gatewayTelemetry={};
function applyTheme(theme){document.body.dataset.theme=theme;localStorage.setItem('meshcore-theme',theme);document.getElementById('theme-select').value=theme}
function loadTheme(){applyTheme(localStorage.getItem('meshcore-theme')||'midnight')}
function fields(){let t=connection_type.value;document.getElementById('ble-field').style.display=t==='bluetooth'?'block':'none';document.getElementById('serial-field').style.display=t==='serial'?'block':'none'}
function updateClock(){document.getElementById('current-datetime').textContent=new Date().toLocaleString()}
async function status(){let r=await fetch('/api/status'),d=await r.json();let b=document.getElementById('status');b.textContent=d.is_connected?'CONNECTED':'DISCONNECTED';b.className='header-status '+(d.is_connected?'connected':'disconnected');document.getElementById('console').innerText=d.logs.join('\n')}
async function peers(){let r=await fetch('/api/peers'),d=await r.json();gatewayTelemetry=d.gateway_telemetry||{};gateway_battery.textContent=gatewayTelemetry.battery!=null?gatewayTelemetry.battery+'%':'Unavailable';node.innerHTML='<option value="">Select node</option>';channel.innerHTML='<option value="">Select channel</option>';for(let n of d.nodes||[]){node.add(new Option(n.name,n.id))}for(let c of d.channels||[]){channel.add(new Option(c.name,c.id))}}
async function history(t,id,boxId){let box=document.getElementById(boxId);if(!id){box.innerHTML='';return}let r=await fetch('/api/chat-history?target_type='+encodeURIComponent(t)+'&target='+encodeURIComponent(id)),d=await r.json();box.innerHTML='';for(let m of d.messages||[]){let e=document.createElement('div');e.className='chat-message '+m.direction;e.textContent='['+m.timestamp+'] '+m.text;box.appendChild(e)}box.scrollTop=box.scrollHeight}
function selectNode(){if(!node.value)return;history('node',node.value,'node-chat-history')}
function selectChannel(){if(!channel.value)return;history('channel',channel.value,'channel-chat-history')}
async function connect(){let r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({connection_type:connection_type.value,ble_mac:ble_mac.value,serial_port:serial_port.value,model:model.value})});let d=await r.json();if(!r.ok)alert(d.error);await status();await peers()}
async function disconnect(){await fetch('/api/disconnect',{method:'POST'});await status();await peers()}
async function sendMessage(e,targetId,type,messageId,historyId){e.preventDefault();let selected=document.getElementById(targetId).value;let input=document.getElementById(messageId);if(!selected)return alert('Select a '+type+' first');let r=await fetch('/api/transmit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:selected,target_type:type,text:input.value})});let d=await r.json();if(!r.ok)return alert(d.error);input.value='';await history(type,selected,historyId)}
function enableCardDragging(){let dragged=null;let cards=[...document.querySelectorAll('.grid .card')];let columns=[...document.querySelectorAll('.grid>div')];function place(column,y){if(!dragged)return;let targets=[...column.querySelectorAll(':scope>.card:not(.dragging)')];let before=targets.find(card=>y<card.getBoundingClientRect().top+card.offsetHeight/2);if(before)column.insertBefore(dragged,before);else column.appendChild(dragged)}cards.forEach(card=>{card.draggable=true;card.addEventListener('dragstart',e=>{dragged=card;card.classList.add('dragging');e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain','dashboard-card')});card.addEventListener('dragend',()=>{card.classList.remove('dragging');dragged=null})});columns.forEach(column=>{column.addEventListener('dragover',e=>{if(!dragged)return;e.preventDefault();e.dataTransfer.dropEffect='move';place(column,e.clientY)});column.addEventListener('drop',e=>{e.preventDefault();place(column,e.clientY)})})}
window.addEventListener('DOMContentLoaded',()=>{loadTheme();fields();status();peers();enableCardDragging();updateClock();setInterval(updateClock,1000)});setInterval(status,2000);setInterval(peers,10000);
</script></head><body>
<div class="dashboard-header"><h1>MeshCore AI Bot Dashboard</h1><div class="header-meta"><div><span class="header-label">Node Battery</span><span id="gateway_battery" class="header-metric">Unavailable</span></div><div><span class="header-label">Status</span><span id="status" class="header-status disconnected">DISCONNECTED</span></div><div><span class="header-label">Local Time</span><span id="current-datetime" class="header-metric">--</span></div><div><span class="header-label">Theme</span><select id="theme-select" onchange="applyTheme(this.value)"><option value="midnight">Midnight</option><option value="light">Light</option><option value="ocean">Ocean</option><option value="amber">Amber</option><option value="linux">Linux Console</option><option value="macos">macOS</option><option value="cyberpunk">Hacker Cyberpunk</option></select></div><div><span class="header-label">Ollama Model</span><select id="model">{{MODEL_OPTIONS}}</select></div></div></div><div class="grid"><div>
<div class="card"><h2>Connection</h2><label>Connection Type</label><select id="connection_type" onchange="fields()"><option value="bluetooth">Bluetooth</option><option value="serial">Serial</option></select><div id="ble-field"><label>Bluetooth MAC</label><input id="ble_mac" value="A4:CB:8F:A6:67:39"></div><div id="serial-field" style="display:none"><label>Serial Port</label><input id="serial_port" value="/dev/ttyACM0"></div><button onclick="connect()">Connect</button><button onclick="disconnect()">Disconnect</button></div>
<div class="card console-card"><h2>Console</h2><pre id="console"></pre></div>
</div><div>
<div class="card"><h2>Node Messages</h2><select id="node" onchange="selectNode()"><option value="">Select node</option></select><div id="node-chat-history"></div><form onsubmit="sendMessage(event,'node','node','node-message','node-chat-history')"><input id="node-message" maxlength="200" placeholder="Message selected node" required><button>Send to Node</button></form></div>
<div class="card"><h2>Channel Messages</h2><select id="channel" onchange="selectChannel()"><option value="">Select channel</option></select><div id="channel-chat-history"></div><form onsubmit="sendMessage(event,'channel','channel','channel-message','channel-chat-history')"><input id="channel-message" maxlength="200" placeholder="Message selected channel" required><button>Send to Channel</button></form></div>
</div></div></body></html>'''


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
    return web.json_response({
        "nodes": [
            {"id": str(i), "name": display_name(i, value)}
            for i, value in app_state["contacts"].items()
        ],
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


async def news_handler(request):
    return web.json_response({"headlines": get_news_headlines()})


async def hacker_news_handler(request):
    return web.json_response({"headlines": get_hacker_news_headlines()})


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
        return web.json_response({"error": "Bluetooth MAC is required"}, status=400)
    if connection_type == "serial" and not serial_port:
        return web.json_response({"error": "Serial port is required"}, status=400)

    app_state["connection_type"] = connection_type
    app_state["ble_mac"] = ble_mac or DEFAULT_BLE_MAC
    app_state["serial_port"] = serial_port or DEFAULT_SERIAL_PORT
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
    await update_available_models()


async def on_cleanup(app):
    app["telemetry_task"].cancel()
    try:
        await app["telemetry_task"]
    except asyncio.CancelledError:
        pass
    await disconnect_hardware()
    executor.shutdown(wait=False)


def create_app():
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/peers", peers_handler)
    app.router.add_get("/api/chat-history", chat_history_handler)
    app.router.add_get("/api/news", news_handler)
    app.router.add_get("/api/hacker-news", hacker_news_handler)
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
