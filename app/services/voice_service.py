"""语音服务：音频/视频保存、语音识别、会议纪要生成、记录管理、文字转语音（TTS）。

说明：
- 音视频文件保存在 ``data/voice_uploads/``；
- 识别记录保存在 ``data/voice_records.json``；
- 语音识别走阿里云百炼 DashScope 的 OpenAI 兼容接口（`/compatible-mode/v1/audio/transcriptions`）；
- 文字转语音走 DashScope 多模态生成接口（Qwen-TTS 系列：`/api/v1/services/aigc/multimodal-generation/generation`），
  合成结果保存到 ``data/tts_outputs/``。
"""
import base64
import json
import os
import re
import time
import traceback
import uuid
import wave
from datetime import datetime
from typing import Optional

import requests

from app.core.config import BASE_DIR, DATA_DIR, load_config

VOICE_UPLOAD_DIR = os.path.join(DATA_DIR, "voice_uploads")
VOICE_RECORDS_FILE = os.path.join(DATA_DIR, "voice_records.json")
TTS_OUTPUT_DIR = os.path.join(DATA_DIR, "tts_outputs")

# 语音模型分类（ASR 用于语音识别，TTS 用于语音合成）
ASR_MODELS = (
    "qwen-audio-3.0-asr-flash",
    "qwen-audio-3.0-asr-filetrans",
    "qwen-audio-3.0-asr-streaming",
    "qwen3-asr-flash",
    "qwen3-asr-flash-filetrans",
    "qwen3-asr-flash-realtime",
    "paraformer-v2",
    "paraformer-v1",
    "fun-asr",
    "fun-asr-flash",
)
TTS_MODELS = (
    "qwen-tts",
    "qwen3-tts-flash",
    "qwen3-tts-instruct-flash",
    "qwen-audio-3.0-tts",
    "qwen-audio-3.1-tts",
    "cosyvoice-v1",
    "cosyvoice-v2",
    "cosyvoice-v3",
)

# 语音合成音色清单：(显示名, voice 参数值, 适用模型前缀)
# - qwen-tts / qwen-tts-latest 仅支持 Cherry / Serena / Ethan / Chelsie 四个系统音色；
# - qwen3-tts-flash 系列的可用音色更多，这里收录常用项。
TTS_VOICES = [
    ("芊悦 Cherry（阳光亲切·女）", "Cherry", ("qwen-tts", "qwen3-tts")),
    ("苏瑶 Serena（温柔·女）", "Serena", ("qwen-tts", "qwen3-tts")),
    ("晨煦 Ethan（阳光温暖·男）", "Ethan", ("qwen-tts", "qwen3-tts")),
    ("千雪 Chelsie（二次元·女）", "Chelsie", ("qwen-tts", "qwen3-tts")),
    ("茉兔 Momo（撒娇搞怪·女）", "Momo", ("qwen3-tts",)),
    ("十三 Vivian（小暴躁·女）", "Vivian", ("qwen3-tts",)),
    ("月白 Moon（率性帅气·男）", "Moon", ("qwen3-tts",)),
    ("四月 Maia（知性温柔·女）", "Maia", ("qwen3-tts",)),
    ("凯 Kai（耳朵的SPA·男）", "Kai", ("qwen3-tts",)),
    ("阿闻 Neil（新闻主持·男）", "Neil", ("qwen3-tts",)),
    ("小婉 Seren（助眠·女）", "Seren", ("qwen3-tts",)),
    ("邻家妹妹 Nini（甜软·女）", "Nini", ("qwen3-tts",)),
]


def _ensure_dirs():
    os.makedirs(VOICE_UPLOAD_DIR, exist_ok=True)
    os.makedirs(TTS_OUTPUT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(VOICE_RECORDS_FILE), exist_ok=True)


def _read_records() -> list:
    if not os.path.exists(VOICE_RECORDS_FILE):
        return []
    try:
        with open(VOICE_RECORDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_records(records: list):
    _ensure_dirs()
    with open(VOICE_RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def human_size(size: int) -> str:
    """将字节数转换为可读文本。"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def audio_duration(path: str) -> Optional[float]:
    """获取音频时长（秒）。优先读 wav 头；其他格式无法解析时返回 None。"""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".wav":
            with wave.open(path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return round(frames / rate, 2) if rate else None
        # 其余格式未安装 ffmpeg 时不做精确探测
        return None
    except Exception:
        return None


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def classify_voice_model(model: str) -> str:
    """判断模型用途：asr / tts / unknown。"""
    m = (model or "").strip().lower()
    if any(m == x or m.startswith(x) for x in ASR_MODELS):
        return "asr"
    if any(m == x or m.startswith(x) for x in TTS_MODELS):
        return "tts"
    return "unknown"


def save_voice_file(upload) -> tuple:
    """保存上传的音视频文件，返回 (disk_path, disk_name, original_name, size_bytes)。"""
    _ensure_dirs()
    original = getattr(upload, "filename", None) or "audio.wav"
    safe = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_.\-]", "_", os.path.basename(original))
    if not safe or safe.startswith("."):
        safe = "audio.wav"
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    disk_name = f"{ts}_{safe}"
    disk_path = os.path.join(VOICE_UPLOAD_DIR, disk_name)
    with open(disk_path, "wb") as f:
        if hasattr(upload, "file"):
            content = upload.file.read()
        elif hasattr(upload, "read"):
            content = upload.read()
        else:
            content = upload
        f.write(content)
    size = os.path.getsize(disk_path)
    return disk_path, disk_name, original, size


def _dashscope_asr(path: str, model: str, api_key: str, base_url: str) -> dict:
    """调用 DashScope OpenAI 兼容语音识别接口（同步）。"""
    url = (base_url or "https://dashscope.aliyuncs.com").rstrip("/") + "/compatible-mode/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    mime = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/m4a",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
    }.get(ext, "audio/wav")
    with open(path, "rb") as f:
        files = {"file": (filename, f, mime)}
        data = {"model": model, "response_format": "json"}
        try:
            r = requests.post(url, headers=headers, files=files, data=data, timeout=300)
        except Exception as e:
            return {"ok": False, "error": f"ASR 请求失败：{e}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"ASR 接口返回 {r.status_code}：{r.text[:200]}"}
    try:
        obj = r.json()
    except Exception:
        return {"ok": True, "text": r.text.strip()}
    text = obj.get("text") or obj.get("content") or obj.get("transcription")
    if text is None:
        return {"ok": False, "error": f"ASR 返回异常：{json.dumps(obj, ensure_ascii=False)[:200]}"}
    return {"ok": True, "text": text}


def _zhipu_chat(prompt: str, model: str, api_key: str) -> str:
    """调用智谱 AI Chat API 生成会议纪要。"""
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model or "glm-4-flash",
        "messages": [
            {"role": "system", "content": "你是一名专业的会议纪要助手，请把会议录音转写内容整理成结构清晰、重点突出的会议纪要。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
        if r.status_code != 200:
            return f"（纪要生成失败：{r.status_code} {r.text[:150]}）"
        obj = r.json()
        return obj["choices"][0]["message"]["content"]
    except Exception as e:
        return f"（纪要生成失败：{e}）"


def process_voice(path: str, disk_name: str, model: str, cfg) -> dict:
    """对单个音频文件执行识别（ASR）并生成纪要。"""
    kind = classify_voice_model(model)
    if kind == "tts":
        return {
            "ok": False,
            "error": f"当前模型「{model}」为语音合成模型（TTS），不能用于语音识别。"
                    f"请切换到 ASR 模型，如 qwen-audio-3.0-asr-flash。",
        }
    if kind == "unknown":
        return {"ok": False, "error": f"暂不支持的语音模型：{model}。请确认模型名称正确。"}

    api_key = cfg.dashscope_api_key or ""
    if not api_key:
        return {"ok": False, "error": "未配置百炼 API Key（DASHSCOPE_API_KEY），无法进行语音识别。"}

    asr = _dashscope_asr(path, model, api_key, cfg.dashscope_base_url)
    if not asr["ok"]:
        return asr
    transcript = asr["text"]

    # 生成会议纪要（使用配置中的对话模型与智谱 Key；任一缺失则跳过）
    summary = ""
    if cfg.zhipu_api_key and cfg.chat_model:
        summary = _zhipu_chat(
            f"请根据以下会议录音转写内容生成会议纪要：\n\n{transcript}",
            cfg.chat_model,
            cfg.zhipu_api_key,
        )
    return {"ok": True, "transcript": transcript, "summary": summary}


def create_record(file, model: Optional[str] = None) -> dict:
    """保存文件、识别、生成记录并落盘。"""
    cfg = load_config()
    model = (model or cfg.voice_model or "qwen-audio-3.0-asr-flash").strip()
    disk_path, disk_name, original, size = save_voice_file(file)
    duration = audio_duration(disk_path)
    record = {
        "id": str(uuid.uuid4()),
        "name": original,
        "disk_name": disk_name,
        "disk_path": disk_path,
        "model": model,
        "size": size,
        "size_text": human_size(size),
        "duration": duration,
        "duration_text": fmt_duration(duration),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "识别中",
        "transcript": "",
        "summary": "",
    }
    records = _read_records()
    records.insert(0, record)
    _write_records(records)

    # 异步识别
    result = process_voice(disk_path, disk_name, model, cfg)
    if result["ok"]:
        record["status"] = "已完成"
        record["transcript"] = result.get("transcript", "")
        record["summary"] = result.get("summary", "")
    else:
        record["status"] = f"识别失败：{result['error']}"
    _write_records(_replace_record(record))
    return record


def _replace_record(record: dict) -> list:
    records = _read_records()
    return [r if r.get("id") != record["id"] else record for r in records]


def list_records() -> list:
    """返回所有记录，按创建时间倒序。"""
    records = _read_records()
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records


def delete_record(record_id: str) -> bool:
    records = _read_records()
    target = next((r for r in records if r.get("id") == record_id), None)
    if not target:
        return False
    try:
        path = target.get("disk_path", "")
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        traceback.print_exc()
    records = [r for r in records if r.get("id") != record_id]
    _write_records(records)
    return True


# ==================== 文字转语音（TTS） ====================

def tts_voice_options(model: Optional[str] = None) -> list:
    """返回指定模型可用的音色选项：[{"label":..., "value":...}, ...]。

    ``model`` 为空时返回全部音色；按模型前缀过滤（qwen-tts / qwen3-tts）。
    """
    m = (model or "").strip().lower()
    out = []
    for label, value, prefixes in TTS_VOICES:
        if not m or any(m.startswith(p) for p in prefixes):
            out.append({"label": label, "value": value})
    return out or [{"label": l, "value": v} for l, v, _ in TTS_VOICES]


def resolve_tts_audio(file_name: str) -> Optional[str]:
    """把 TTS 输出文件名解析为安全的本地路径（防目录穿越），不存在则返回 None。"""
    name = os.path.basename((file_name or "").strip())
    if not name:
        return None
    path = os.path.join(TTS_OUTPUT_DIR, name)
    return path if os.path.exists(path) else None


def _dashscope_tts(text: str, voice: str, model: str, api_key: str,
                   base_url: str, language_type: str = "Auto") -> dict:
    """调用 DashScope 语音合成（非流式），返回音频 URL 或 base64 数据。

    - Qwen-TTS 系列：POST /api/v1/services/aigc/multimodal-generation/generation
    - CosyVoice 系列：POST /api/v1/services/aigc/text2speech/speech-synthesis
    成功返回 ``{"ok": True, "url":..., "data":...}``。
    """
    base = (base_url or "https://dashscope.aliyuncs.com").rstrip("/")
    m = (model or "qwen-tts-latest").strip().lower()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if m.startswith("cosyvoice"):
        url = base + "/api/v1/services/aigc/text2speech/speech-synthesis"
        payload = {
            "model": model,
            "input": {"text": text, "voice": voice},
            "parameters": {"format": "wav", "sample_rate": 24000},
        }
    else:
        url = base + "/api/v1/services/aigc/multimodal-generation/generation"
        payload = {
            "model": model,
            "input": {
                "text": text,
                "voice": voice,
                "language_type": language_type or "Auto",
            },
        }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
    except Exception as e:
        return {"ok": False, "error": f"语音合成请求失败：{e}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"语音合成接口返回 {r.status_code}：{r.text[:200]}"}
    try:
        obj = r.json()
    except Exception:
        return {"ok": False, "error": f"语音合成返回非 JSON：{r.text[:200]}"}
    audio = ((obj.get("output") or {}).get("audio") or {})
    audio_url = audio.get("url") or ""
    audio_data = audio.get("data") or ""
    if not audio_url and not audio_data:
        return {
            "ok": False,
            "error": f"语音合成返回异常：{json.dumps(obj, ensure_ascii=False)[:200]}",
        }
    return {"ok": True, "url": audio_url, "data": audio_data,
            "request_id": obj.get("request_id", "")}


def text_to_speech(text: str, voice: Optional[str] = None,
                   model: Optional[str] = None,
                   language_type: str = "Auto") -> dict:
    """文字转语音：合成音频并落盘，返回可下载的文件信息（dict）。

    成功返回含 ``file_name`` / ``url``（下载相对路径）/ ``voice`` / ``model`` / ``duration_text`` 等；
    失败返回 ``{"ok": False, "error": "..."}``。
    """
    cfg = load_config()
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "待合成文本为空，请输入需要转换的文字。"}
    if len(text) > 2000:
        return {"ok": False, "error": f"文本过长（当前 {len(text)} 字，建议不超过 2000 字），请分段合成。"}

    model = (model or cfg.tts_model or "qwen-tts-latest").strip()
    kind = classify_voice_model(model)
    if kind == "asr":
        return {
            "ok": False,
            "error": f"当前模型「{model}」为语音识别模型（ASR），不能用于语音合成。"
                    f"请切换到 TTS 模型，如 qwen-tts-latest。",
        }
    if kind == "unknown":
        return {"ok": False, "error": f"暂不支持的语音合成模型：{model}。请确认模型名称正确。"}

    voice = (voice or cfg.tts_voice or "Cherry").strip()
    api_key = cfg.dashscope_api_key or ""
    if not api_key:
        return {"ok": False, "error": "未配置百炼 API Key（DASHSCOPE_API_KEY），无法进行语音合成。"}

    res = _dashscope_tts(text, voice, model, api_key, cfg.dashscope_base_url, language_type)
    if not res["ok"]:
        return res

    _ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    file_name = f"tts_{ts}_{uuid.uuid4().hex[:8]}.wav"
    disk_path = os.path.join(TTS_OUTPUT_DIR, file_name)
    try:
        if res.get("url"):
            with requests.get(res["url"], timeout=180) as resp:
                resp.raise_for_status()
                content = resp.content
        else:
            content = base64.b64decode(res["data"])
        if not content:
            return {"ok": False, "error": "语音合成结果为空音频。"}
        with open(disk_path, "wb") as f:
            f.write(content)
    except Exception as e:
        return {"ok": False, "error": f"音频下载/保存失败：{e}"}

    size = os.path.getsize(disk_path)
    duration = audio_duration(disk_path)
    return {
        "ok": True,
        "file_name": file_name,
        "disk_path": disk_path,
        "url": f"/api/voice/tts/audio/{file_name}",
        "voice": voice,
        "model": model,
        "language_type": language_type or "Auto",
        "size": size,
        "size_text": human_size(size),
        "duration": duration,
        "duration_text": fmt_duration(duration),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
