import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import gc
import re
import json
import wave
import asyncio
import subprocess
import urllib.request
from datetime import datetime
from vosk import Model, KaldiRecognizer

VOSK_MODEL_PATH = "/root/vosk-model"
DSAME_PATH = "/root/dsame3/dsame.py"
TEMP_WAV = "/tmp/noaa_capture.wav"

def clean_ollama_output(text):
    text = text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1 and lines.endswith(":"):
        lines = lines[1:]
    text = " ".join(lines)
    text = re.sub(r"^(here is|here's|summary|weather summary)[^:]{0,40}:\s*", "", text, flags=re.IGNORECASE)
    return text.strip('"').strip("'").strip()

def split_into_packets(text, max_len=145, max_packets=6):
    words = text.split()
    packets, current = [], ""
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
        "600 and 780 characters so it fits cleanly into 5 to 6 short radio packets without cutting off. "
        "Include station/location, current temp, humidity, wind, pressure, today/tonight, full "
        "multi-day forecast, and any warnings. Output ONLY the report with NO intro text:\n\n" + raw_transcript
    )
    payload = json.dumps({"model": model_name, "prompt": prompt, "stream": False}).encode("utf-8")
    try:
        req = urllib.request.Request("http://" + "127.0.0.1" + ":11434/api/generate", data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            res = clean_ollama_output(json.loads(resp.read().decode("utf-8")).get("response", ""))
            if res:
                return res[:840]
    except Exception as e:
        print(f"[NOAA] Ollama unavailable ({e}), using raw transcript fallback")
    return raw_transcript.strip()[:840]

def transcribe_wav(wav_path):
    if not os.path.exists(VOSK_MODEL_PATH):
        return ""
    model = Model(VOSK_MODEL_PATH)
    wf = wave.open(wav_path, "rb")
    rec = KaldiRecognizer(model, wf.getframerate())
    results = []
    while True:
        data = wf.readframes(4000)
        if not data:
            break
        if rec.AcceptWaveform(data):
            results.append(json.loads(rec.Result()).get("text", ""))
    results.append(json.loads(rec.FinalResult()).get("text", ""))
    wf.close()
    del rec, model
    gc.collect()
    return " ".join(results).strip()

def _get_target_channels(noaa_cfg):
    chs = noaa_cfg.get("target_channels")
    if isinstance(chs, list) and len(chs) > 0:
        return [int(c) for c in chs]
    if "target_channel" in noaa_cfg:
        return [int(noaa_cfg["target_channel"])]
    return list((5,))

def _blocking_noaa_capture(noaa_cfg, ollama_model):
    freq = noaa_cfg.get("frequency", "162.475M")
    gain = str(noaa_cfg.get("gain", 40))
    same_codes = ",".join(noaa_cfg.get("same_codes", ["009003"]))
    same_flag = f"--same {same_codes}" if same_codes else ""
    raw_file = "/tmp/noaa_raw.pcm"
    cmd = (
        f"timeout 110s rtl_fm -f {freq} -M fm -s 22050 -g {gain} - | tee {raw_file} | "
        f"multimon-ng -t raw -a EAS - | /root/Meshcore-Ollama-bot/.venv/bin/python {DSAME_PATH} "
        f"--skip_dependency {same_flag} --call /root/Meshcore-Ollama-bot/push_noaa_alert.sh --command \"{{MESSAGE}}\""
    )
    subprocess.run(cmd, shell=True)
    summary = ""
    if os.path.exists(raw_file) and os.path.getsize(raw_file) > 0:
        with open(raw_file, "rb") as pcm:
            pcm_data = pcm.read()
        with wave.open(TEMP_WAV, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050); wf.writeframes(pcm_data)
        transcript = transcribe_wav(TEMP_WAV)
        if len(transcript) > 25:
            summary = summarize_with_ollama(transcript, model_name=ollama_model)
        os.remove(raw_file)
        if os.path.exists(TEMP_WAV):
            os.remove(TEMP_WAV)
    return summary

async def send_multipacket_wx(send_channel_fn, channels, summary):
    if isinstance(channels, int):
        channels = [channels]
    packets = split_into_packets(f"WX: {summary}", max_len=145, max_packets=6)
    for ch in channels:
        for idx, pkt in enumerate(packets, start=1):
            prefix = f"[{idx}/{len(packets)}] " if len(packets) > 1 else ""
            await send_channel_fn(int(ch), f"{prefix}{pkt}"[:155])
            if idx < len(packets):
                await asyncio.sleep(3.0)

async def noaa_6hr_loop(get_config_fn, send_channel_fn):
    target_hours, last_run_hour = {5, 11, 17, 23}, None
    while True:
        now = datetime.now()
        if now.hour in target_hours and now.hour != last_run_hour:
            cfg = get_config_fn()
            noaa_cfg = cfg.get("noaa", {"enabled": True, "frequency": "162.475M", "gain": 40, "same_codes": ["009003"], "target_channels": list((5,))})
            if noaa_cfg.get("enabled", True):
                last_run_hour = now.hour
                try:
                    loop = asyncio.get_running_loop()
                    model = cfg.get("ollama", {}).get("model") or cfg.get("ollama_model") or "llama3.2:1b-instruct-q4_K_M"
                    summary = await loop.run_in_executor(None, _blocking_noaa_capture, noaa_cfg, model)
                    if summary:
                        await send_multipacket_wx(send_channel_fn, _get_target_channels(noaa_cfg), summary)
                except Exception as e:
                    print(f"[NOAA Error] {e}")
        await asyncio.sleep(30)

async def trigger_noaa_now(cfg, send_channel_fn):
    noaa_cfg = cfg.get("noaa", {"enabled": True, "frequency": "162.475M", "gain": 40, "same_codes": ["009003"], "target_channels": list((5,))})
    loop = asyncio.get_running_loop()
    model = cfg.get("ollama", {}).get("model") or cfg.get("ollama_model") or "llama3.2:1b-instruct-q4_K_M"
    summary = await loop.run_in_executor(None, _blocking_noaa_capture, noaa_cfg, model)
    if summary:
        await send_multipacket_wx(send_channel_fn, _get_target_channels(noaa_cfg), summary)
    return summary
