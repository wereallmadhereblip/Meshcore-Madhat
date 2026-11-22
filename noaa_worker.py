import os
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

def summarize_with_ollama(raw_transcript, model_name="qwen2.5:0.5b"):
    prompt = (
        "Summarize this NOAA weather radio transcript into ONE short text message "
        "under 140 characters. Include current temp/sky, forecast, and any active warnings. "
        "No intro text, just the weather summary:\n\n" + raw_transcript
    )
    payload = json.dumps({
        "model": model_name,
        "prompt": prompt,
        "stream": False
    }).encode("utf-8")

    req = urllib.request.Request(
        "http://127.0.0",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("response", "").strip()[:150]

def transcribe_wav(wav_path):
    if not os.path.exists(VOSK_MODEL_PATH):
        return ""
    model = Model(VOSK_MODEL_PATH)
    wf = wave.open(wav_path, "rb")
    rec = KaldiRecognizer(model, wf.getframerate())
    results = []

    while True:
        data = wf.readframes(4000)
        if len(data) == 0:
            break
        if rec.AcceptWaveform(data):
            res = json.loads(rec.Result())
            results.append(res.get("text", ""))
    final_res = json.loads(rec.FinalResult())
    results.append(final_res.get("text", ""))
    wf.close()
    return " ".join(results).strip()

def _blocking_noaa_capture(noaa_cfg, ollama_model):
    freq = noaa_cfg.get("frequency", "162.475M")
    gain = str(noaa_cfg.get("gain", 40))
    same_codes = ",".join(noaa_cfg.get("same_codes", ["009003"]))
    same_flag = f"--same {same_codes}" if same_codes else ""

    raw_file = "/tmp/noaa_raw.pcm"
    cmd = (
        f"timeout 90s rtl_fm -f {freq} -M fm -s 22050 -g {gain} - | "
        f"tee {raw_file} | "
        f"multimon-ng -t raw -a EAS - | "
        f"python3 {DSAME_PATH} {same_flag} "
        f"--call 'curl -s -X POST http://127.0.0 -d' "
        f"--command '\"{{event}} for {{area}} until {{end}}\"'"
    )
    subprocess.run(cmd, shell=True)

    summary = ""
    if os.path.exists(raw_file) and os.path.getsize(raw_file) > 0:
        with open(raw_file, "rb") as pcm:
            pcm_data = pcm.read()
        with wave.open(TEMP_WAV, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(pcm_data)

        transcript = transcribe_wav(TEMP_WAV)
        if len(transcript) > 25:
            summary = summarize_with_ollama(transcript, model_name=ollama_model)

        os.remove(raw_file)
        if os.path.exists(TEMP_WAV):
            os.remove(TEMP_WAV)
    return summary

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
                    summary = await loop.run_in_executor(
                        None,
                        _blocking_noaa_capture,
                        noaa_cfg,
                        cfg.get("ollama_model", "qwen2.5:0.5b")
                    )
                    if summary:
                        ch = int(noaa_cfg.get("target_channel", 0))
                        await send_channel_fn(ch, f"WX: {summary}")
                except Exception as e:
                    print(f"[NOAA Error] {e}")
        await asyncio.sleep(30)
