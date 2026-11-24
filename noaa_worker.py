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


def words_to_digits(text):
    """Converts spoken English numbers from Vosk transcripts into compact digits."""
    ones = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
        "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
        "eighteen": 18, "nineteen": 19
    }
    tens = {
        "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90
    }
    decades = {
        "twenties": "20s", "thirties": "30s", "forties": "40s",
        "fifties": "50s", "sixties": "60s", "seventies": "70s",
        "eighties": "80s", "nineties": "90s"
    }
    # Replace decades like "forties" -> "40s"
    for w, d in decades.items():
        text = re.sub(rf"\b{w}\b", d, text, flags=re.IGNORECASE)
    # Replace compound tens + ones like "forty seven" -> "47"
    for tw, tv in tens.items():
        for ow, ov in list(ones.items())[1:10]:
            text = re.sub(rf"\b{tw}[-\s]+{ow}\b", str(tv + ov), text, flags=re.IGNORECASE)
        text = re.sub(rf"\b{tw}\b", str(tv), text, flags=re.IGNORECASE)
    # Replace standalone 0-19
    for ow, ov in ones.items():
        text = re.sub(rf"\b{ow}\b", str(ov), text, flags=re.IGNORECASE)
    # Collapse barometric pressure like "30 point 1 8" -> "30.18"
    text = re.sub(r"\b(\d+)\s+point\s+(\d)\s+(\d)\b", r"\1.\2\3", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(\d+)\s+point\s+(\d+)\b", r"\1.\2", text, flags=re.IGNORECASE)
    # Collapse spoken clock times like "2 45 pm" -> "2:45 PM"
    text = re.sub(r"\b(1[0-2]|[1-9])\s+([0-5]\d)\s*(am|pm)\b", lambda m: f"{m.group(1)}:{m.group(2)} {m.group(3).upper()}", text, flags=re.IGNORECASE)
    return text

def summarize_with_ollama(raw_transcript, model_name="llama3.2:1b-instruct-q4_K_M"):
    raw_transcript = words_to_digits(raw_transcript)
    prompt = (
        "Summarize this NOAA weather radio transcript into a detailed weather report between "
        "600 and 780 characters so it fits cleanly into 5 to 6 short radio packets without cutting off. "
        "Include station/location, current temp, humidity, wind, pressure, today/tonight, full "
        "multi-day forecast, and any warnings. Always use numeric digits (e.g., 2:45 PM, 47F, 12 mph) instead of number words. Output ONLY the report with NO intro text:\n\n" + raw_transcript
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
    try:
        with open("/var/log/noaa_worker.log", "a", encoding="utf-8") as _lf:
            _lf.write(f"[{datetime.now().isoformat()}] freq={freq} transcript_len={len(transcript) if 'transcript' in locals() else 0} summary={summary!r}\n")
    except Exception:
        pass
    return summary

async def send_multipacket_wx(send_channel_fn, channels, summary):
    if isinstance(channels, int):
        channels = [channels]
    packets = split_into_packets(f"WX: {summary}", max_len=145, max_packets=6)
    for ch in channels:
        for idx, pkt in enumerate(packets, start=1):
            prefix = f"[{idx}/{len(packets)}] " if len(packets) > 1 else ""
            await send_channel_fn(int(ch), f"{prefix}{pkt}"[:155])
            try:
                with open("/var/log/noaa_worker.log", "a", encoding="utf-8") as _lf:
                    _lf.write(f"[{datetime.now().isoformat()}] Sent ch={ch} {prefix}{pkt[:80]}\n")
            except Exception:
                pass
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

if __name__ == "__main__":
    import time as _time
    from pathlib import Path
    cfg_path = Path("/root/Meshcore-Ollama-bot/config.json")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    noaa_cfg = cfg.get("noaa") or {"enabled": True, "frequency": "162.400M", "gain": 40, "same_codes": ["009003"], "target_channels": list((5,))}
    if not noaa_cfg.get("enabled", True):
        print(f"[{datetime.now().isoformat()}] NOAA scheduled broadcast disabled; skipping.")
        raise SystemExit(0)
    model = cfg.get("model") or (cfg.get("ollama") or {}).get("model") or "llama3.2:1b-instruct-q4_K_M"
    print(f"[{datetime.now().isoformat()}] Starting 90s NOAA capture on {noaa_cfg.get('frequency', '162.400M')}...")
    summary = _blocking_noaa_capture(noaa_cfg, model)
    print(f"[{datetime.now().isoformat()}] Summary: {summary}")
    if summary:
        url = "http://" + "localhost" + ":8080/api/send"
        packets = split_into_packets(summary)
        for ch in _get_target_channels(noaa_cfg):
            for idx, pkt in enumerate(packets, 1):
                payload = json.dumps({"target": str(ch), "target_type": "channel", "message": pkt}).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        print(f"[{datetime.now().isoformat()}] Sent packet {idx}/{len(packets)} to ch {ch}: HTTP {r.status}")
                except Exception as send_err:
                    print(f"[{datetime.now().isoformat()}] Send error ch {ch} pkt {idx}: {send_err}")
                if idx < len(packets):
                    _time.sleep(3.0)
