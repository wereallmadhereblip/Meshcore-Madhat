import os
import re
import json
import wave
import asyncio
import subprocess
import urllib.request
from datetime import datetime

WHISPER_BIN = "/root/whisper.cpp/build/bin/whisper-cli"
WHISPER_MODEL = "/root/whisper.cpp/models/ggml-tiny.en.bin"
DSAME_PATH = "/root/dsame3/dsame.py"
TEMP_WAV_22K = "/tmp/noaa_capture_22k.wav"
TEMP_WAV_16K = "/tmp/noaa_capture_16k.wav"

def clean_ollama_output(text):
    text = text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1 and lines.endswith(":"):
        lines = lines[1:]
    text = " ".join(lines)
    text = re.sub(r"^(here is|here's|summary|weather summary)[^:]{0,40}:\s*", "", text, flags=re.IGNORECASE)
    return text.strip('"').strip("'").strip()

def split_into_packets(text, max_len=145, max_packets=4):
    words = text.split()
    packets = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                packets.append(current)
            if len(packets) >= max_packets:
                break
            current = word[:max_len]
    if current and len(packets) < max_packets:
        packets.append(current)
    return packets

def summarize_with_ollama(raw_transcript, model_name="llama3.2:1b-instruct-q4_K_M"):
    prompt = (
        "Summarize this NOAA weather radio transcript into a detailed weather report between "
        "450 and 540 characters so it fills 4 short radio packets. Include station/location, "
        "current temp, humidity, wind, pressure, today/tonight, full multi-day forecast, and "
        "any warnings. Output ONLY the report with NO intro text:\n\n" + raw_transcript
    )
    payload = json.dumps({
        "model": model_name,
        "prompt": prompt,
        "stream": False
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "http://" + "127.0.0.1" + ":11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            res = clean_ollama_output(data.get("response", ""))
            if res:
                return res[:560]
    except Exception as e:
        print(f"[NOAA] Ollama unavailable ({e}), using raw transcript fallback")

    return raw_transcript.strip()[:560]

def transcribe_wav(wav_path):
    """Resamples to 16kHz mono if needed and transcribes via native whisper-cli."""
    if not os.path.exists(WHISPER_BIN) or not os.path.exists(WHISPER_MODEL):
        return ""
    # Convert any sample rate (e.g. 22050Hz from rtl_fm/espeak) to 16kHz 16-bit mono WAV
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", TEMP_WAV_16K],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    target = TEMP_WAV_16K if os.path.exists(TEMP_WAV_16K) else wav_path
    proc = subprocess.run(
        [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", target, "-nt", "-np"],
        capture_output=True,
        text=True,
        timeout=90
    )
    if os.path.exists(TEMP_WAV_16K):
        os.remove(TEMP_WAV_16K)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip() and not line.strip().startswith("[")]
    return " ".join(lines).strip()

def _blocking_noaa_capture(noaa_cfg, ollama_model):
    freq = noaa_cfg.get("frequency", "162.475M")
    gain = str(noaa_cfg.get("gain", 40))
    same_codes = ",".join(noaa_cfg.get("same_codes", ["009003"]))
    same_flag = f"--same {same_codes}" if same_codes else ""

    raw_file = "/tmp/noaa_raw.pcm"
    cmd = (
        f"timeout 110s rtl_fm -f {freq} -M fm -s 22050 -g {gain} - | "
        f"tee {raw_file} | "
        f"multimon-ng -t raw -a EAS - | "
        f"/root/Meshcore-Ollama-bot/.venv/bin/python {DSAME_PATH} --skip_dependency {same_flag} "
        f"--call /root/Meshcore-Ollama-bot/push_noaa_alert.sh "
        f'--command "{{MESSAGE}}"'
    )
    subprocess.run(cmd, shell=True)

    summary = ""
    if os.path.exists(raw_file) and os.path.getsize(raw_file) > 0:
        with open(raw_file, "rb") as pcm:
            pcm_data = pcm.read()
        with wave.open(TEMP_WAV_22K, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(pcm_data)

        transcript = transcribe_wav(TEMP_WAV_22K)
        if len(transcript) > 25:
            summary = summarize_with_ollama(transcript, model_name=ollama_model)

        os.remove(raw_file)
        if os.path.exists(TEMP_WAV_22K):
            os.remove(TEMP_WAV_22K)
    return summary

async def send_multipacket_wx(send_channel_fn, ch, summary):
    packets = split_into_packets(f"WX: {summary}", max_len=145, max_packets=4)
    total = len(packets)
    for idx, pkt in enumerate(packets, start=1):
        prefix = f"[{idx}/{total}] " if total > 1 else ""
        await send_channel_fn(ch, f"{prefix}{pkt}"[:155])
        if idx < total:
            await asyncio.sleep(2.5)

async def noaa_6hr_loop(get_config_fn, send_channel_fn):
    """Runs at 05:00, 11:00, 17:00, and 23:00 daily."""
    target_hours = {5, 11, 17, 23}
    last_run_hour = None

    while True:
        now = datetime.now()
        if now.hour in target_hours and now.hour != last_run_hour:
            cfg = get_config_fn()
            noaa_cfg = cfg.get("noaa", {"enabled": True, "frequency": "162.475M", "gain": 40, "same_codes": ["009003"], "target_channel": 0})
            if noaa_cfg.get("enabled", True):
                last_run_hour = now.hour
                try:
                    loop = asyncio.get_running_loop()
                    model = cfg.get("ollama", {}).get("model") or cfg.get("ollama_model") or "llama3.2:1b-instruct-q4_K_M"
                    summary = await loop.run_in_executor(
                        None,
                        _blocking_noaa_capture,
                        noaa_cfg,
                        model
                    )
                    if summary:
                        ch = int(noaa_cfg.get("target_channel", 0))
                        await send_multipacket_wx(send_channel_fn, ch, summary)
                except Exception as e:
                    print(f"[NOAA Error] {e}")
        await asyncio.sleep(30)

async def trigger_noaa_now(cfg, send_channel_fn):
    noaa_cfg = cfg.get("noaa", {"enabled": True, "frequency": "162.475M", "gain": 40, "same_codes": ["009003"], "target_channel": 0})
    loop = asyncio.get_running_loop()
    model = cfg.get("ollama", {}).get("model") or cfg.get("ollama_model") or "llama3.2:1b-instruct-q4_K_M"
    summary = await loop.run_in_executor(
        None,
        _blocking_noaa_capture,
        noaa_cfg,
        model
    )
    if summary:
        ch = int(noaa_cfg.get("target_channel", 0))
        await send_multipacket_wx(send_channel_fn, ch, summary)
        return summary
    return ""
