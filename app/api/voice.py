"""语音模块 REST 接口：实时录音 / 音视频上传、识别、文字转语音、记录管理。"""
import os
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import load_config
from app.models.schemas import TTSRequest
from app.services.voice_service import (
    create_record,
    delete_record,
    list_records,
    resolve_tts_audio,
    text_to_speech,
    tts_voice_options,
)

router = APIRouter(prefix="/api", tags=["语音识别"])


@router.post("/voice/upload", summary="上传音视频并识别")
def voice_upload(
    file: UploadFile = File(...),
    model: str = Form(default=""),
):
    """
    上传单个 mp3/wav/mp4 等音视频文件，保存后调用配置的语音模型进行识别，并生成会议纪要。
    ``model`` 为空时取当前配置的 ``voice_model``。
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="未收到文件")
    cfg = load_config()
    model = (model or cfg.voice_model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="未指定语音模型")
    try:
        record = create_record(file, model)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"识别失败：{e}")
    if "识别失败" in record.get("status", ""):
        raise HTTPException(status_code=500, detail=record["status"])
    return record


@router.post("/voice/batch-upload", summary="批量上传音视频并识别")
def voice_batch_upload(
    files: List[UploadFile] = File(...),
    model: str = Form(default=""),
):
    """一次上传多个音视频文件，逐个识别并返回明细。"""
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")
    results = []
    for f in files:
        item = {"original": f.filename, "status": "error", "message": ""}
        try:
            record = create_record(f, model)
            item["status"] = record.get("status", "已完成")
            item["id"] = record.get("id")
        except Exception as e:
            item["message"] = str(e)
        results.append(item)
    return {"total": len(results), "results": results}


@router.get("/voice/records", summary="获取语音识别记录列表")
def voice_records():
    """返回识别记录列表（含转写文本、会议纪要、状态等）。"""
    return list_records()


@router.delete("/voice/records/{record_id}", summary="删除语音识别记录")
def voice_delete(record_id: str):
    """删除指定识别记录及其本地音频文件。"""
    if delete_record(record_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="记录不存在")


# ==================== 文字转语音（TTS） ====================

@router.post("/voice/tts", summary="文字转语音（语音合成）")
def voice_tts(req: TTSRequest):
    """将文本合成为语音，返回可下载的音频文件信息（含音色、时长、大小）。

    - ``model`` 为空时取当前配置的 ``tts_model``；``voice`` 为空时取 ``tts_voice``；
    - 合成成功返回 ``{"ok": True, "file_name":..., "url": "/api/voice/tts/audio/xxx.wav", ...}``；
    - 失败返回 400/500，``detail`` 为具体原因。
    """
    result = text_to_speech(
        text=req.text,
        voice=req.voice,
        model=req.model,
        language_type=req.language_type or "Auto",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "语音合成失败"))
    return result


@router.get("/voice/tts/voices", summary="获取可用音色列表")
def voice_tts_voices(model: Optional[str] = ""):
    """返回指定语音合成模型可用的音色选项（``model`` 为空则返回全部）。"""
    return {"model": model or "", "voices": tts_voice_options(model)}


@router.get("/voice/tts/audio/{file_name}", summary="下载/试听合成的音频")
def voice_tts_audio(file_name: str):
    """按文件名下载已合成的音频（用于前端试听与下载）。"""
    path = resolve_tts_audio(file_name)
    if not path:
        raise HTTPException(status_code=404, detail="音频不存在或已过期")
    return FileResponse(path, media_type="audio/wav", filename=os.path.basename(path))
