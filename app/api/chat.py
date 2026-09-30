"""检索推理层 REST 接口：RAG 对话问答 + 会话附件 + 提示词增强。"""
import os
import json
import base64
import shutil
import threading
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from app.models.schemas import (
    ChatRequest,
    ChatResponse,
    EnhanceRequest,
    TranslateRequest,
    TranslateResponse,
)
from app.core.rag import get_pipeline
from app.core.loaders import load_document
from app.core.splitters import split_documents
from app.core.llm import zhipu_chat, _IMAGE_MIME
from app.core.session_attachments import (
    ensure_session,
    add_files,
    get_store,
    get_pages,
    list_files,
    delete_file,
    clear,
)
from app.core.config import DATA_DIR

router = APIRouter(prefix="/api", tags=["RAG问答"])

ATTACH_DIR = os.path.join(DATA_DIR, "attachments")
# 与解析层支持格式保持一致（见 app/services/file_service.py）
ALLOWED_EXT = {
    ".pdf", ".docx", ".doc",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".md", ".markdown",
    ".txt",
    # 图片类：经视觉模型生成文字描述后入库
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
}

# 提示词增强风格模板：system 提示词决定增强侧重点
ENHANCE_STYLES = {
    "default": (
        "你是一个提示词优化助手。请将用户的简短输入优化为更清晰、更完整的提问："
        "补全必要的背景与约束条件，明确期望的输出形式，但不得改变用户原本的意图。"
        "直接输出优化后的提问，不要解释、不要使用引号包裹。"
    ),
    "rigorous": (
        "你是一个严谨的学术助手。请将用户的输入改写为结构化的严谨提问："
        "明确研究/分析目标，要求引用依据、分点阐述、使用精确术语，并说明论证逻辑。"
        "直接输出优化后的提问，不要解释。"
    ),
    "concise": (
        "你是一个高效助手。请将用户的输入改写为直击要点的简洁提问："
        "去除冗余信息，聚焦核心诉求，要求给出可执行、可落地的结论。"
        "直接输出优化后的提问，不要解释。"
    ),
    "step_by_step": (
        "你是一个分步推理助手。请将用户的输入改写为要求「逐步思考、分步推理、最后给出结论」的提问，"
        "强调逻辑链路完整，每一步都要有依据。直接输出优化后的提问，不要解释。"
    ),
    "extract": (
        "你是一个信息抽取助手。请将用户提供的文本或问题改写为指令："
        "要求从中抽取关键实体、事实、数字与关系，并以结构化列表或表格呈现。"
        "直接输出优化后的指令，不要解释。"
    ),
}


@router.post("/chat", response_model=ChatResponse, summary="RAG 问答")
def chat(req: ChatRequest):
    """
    接收用户问题 -> （可选）会话附件检索 + 知识库向量检索召回 TopN
    -> 拼接上下文送入大模型 -> 返回答案与来源引用。

    支持通过 ``model`` 临时覆盖对话模型，通过 ``session_id`` 关联本会话上传的附件。
    """
    pipe = get_pipeline()
    if not pipe.cfg.zhipu_api_key:
        return ChatResponse(
            answer="未配置 ZHIPU_API_KEY，请在项目根目录 .env 中填写智谱 API Key 后重试。",
            sources=[],
        )
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空。")
    model = req.model or pipe.cfg.chat_model
    try:
        answer, sources = pipe.answer(
            req.question,
            req.history,
            model=model,
            session_id=req.session_id,
            web_search=req.web_search,
        )
    except Exception as e:
        return ChatResponse(answer=f"问答生成失败：{e}", sources=[])
    return ChatResponse(answer=answer, sources=sources)


@router.post("/chat/stream", summary="RAG 问答（流式输出）")
def chat_stream(req: ChatRequest):
    """
    流式版问答：以 SSE（``text/event-stream``）逐块推送模型回答。

    每个事件格式为 ``data: {"type":"token","content":"..."}``（增量文本片段），
    结束时推送 ``data: {"type":"done","sources":[...]}``（来源引用元数据）。
    前端收到后累加 token 即可实现打字机式输出。
    """
    pipe = get_pipeline()
    if not pipe.cfg.zhipu_api_key:
        def _empty():
            yield f"data: {json.dumps({'type': 'token', 'content': '未配置 ZHIPU_API_KEY，请在项目根目录 .env 中填写智谱 API Key 后重试。'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'sources': []}, ensure_ascii=False)}\n\n"
        return StreamingResponse(_empty(), media_type="text/event-stream")
    if not req.question or not req.question.strip():
        def _bad():
            yield f"data: {json.dumps({'type': 'token', 'content': '问题不能为空。'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'sources': []}, ensure_ascii=False)}\n\n"
        return StreamingResponse(_bad(), media_type="text/event-stream")

    model = req.model or pipe.cfg.chat_model

    def event_stream():
        try:
            for kind, payload in pipe.answer_stream(
                req.question, req.history, model=model, session_id=req.session_id,
                web_search=req.web_search,
            ):
                if kind == "token":
                    yield f"data: {json.dumps({'type': 'token', 'content': payload}, ensure_ascii=False)}\n\n"
                elif kind == "notice":
                    yield f"data: {json.dumps({'type': 'notice', 'content': payload}, ensure_ascii=False)}\n\n"
                elif kind == "sources":
                    yield f"data: {json.dumps({'type': 'done', 'sources': payload}, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = str(e)
            yield f"data: {json.dumps({'type': 'token', 'content': f'问答生成失败：{err}'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'sources': []}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/chat/attach", summary="上传会话附件并解析")
def attach_files(session_id: str = Form(None), files: List[UploadFile] = File(...)):
    """
    接收本次对话会话的附件文件（multipart 多文件），保存到会话临时目录，
    解析 -> 分块 -> 累积进该 session_id 的临时向量索引（不写入持久化知识库）。
    返回每个文件的分页/分块明细与已附加文件清单。
    """
    pipe = get_pipeline()
    if not pipe.cfg.zhipu_api_key:
        raise HTTPException(status_code=400, detail="未配置 ZHIPU_API_KEY，无法解析附件。")
    if not files:
        raise HTTPException(status_code=400, detail="未接收到任何附件文件。")

    session_id = ensure_session(session_id)
    sess_dir = os.path.join(ATTACH_DIR, session_id)
    os.makedirs(sess_dir, exist_ok=True)

    results = []
    all_docs = []
    pages_by_file = {}
    for f in files:
        original = f.filename or "unknown"
        ext = os.path.splitext(original)[1].lower()
        if ext not in ALLOWED_EXT:
            results.append({
                "file_name": original,
                "status": "error",
                "message": f"不支持的格式：{ext or '无扩展名'}",
            })
            continue
        dest = os.path.join(sess_dir, original)
        try:
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            docs = load_document(
                dest,
                original,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                vision_api_key=pipe.cfg.zhipu_api_key,
                vision_model=pipe.cfg.vision_model,
            )
            # 顺手缓存「未分块的原始分页文本」：预览直接复用，避免二次解析
            # （docx/pptx 里的图片要走视觉模型，重新解析常达数十秒 -> 前端读超时）
            # 同一文件名重新上传时，让旧的预览缓存失效，避免看到上一版内容
            _PREVIEW_CACHE.pop((session_id, original), None)
            pages_by_file[original] = [
                {"page": d.metadata.get("page", i), "content": d.page_content}
                for i, d in enumerate(docs, start=1)
            ]
            chunks = split_documents(docs, pipe.cfg)
            all_docs.extend(chunks)
            results.append({
                "file_name": original,
                "status": "success",
                "pages": len(docs),
                "chunks": len(chunks),
            })
        except Exception as e:
            msg = str(e)[:300]
            # 视觉模型限流（免费版 glm-4v-flash 高频 429）时给出可操作提示
            if "429" in msg or "限流" in msg or "code 1305" in msg:
                msg += "（图片解析依赖视觉模型，免费版易限流；可在 .env 设置 VISION_MODEL=glm-4.6v 等付费模型后重试）"
            results.append({
                "file_name": original,
                "status": "error",
                "message": msg,
            })

    if all_docs:
        add_files(session_id, all_docs, model=pipe.embeddings.active_model,
                  pages=pages_by_file)

    return {
        "session_id": session_id,
        "attached_files": list_files(session_id),
        "total": len(results),
        "success": sum(1 for r in results if r["status"] == "success"),
        "failed": sum(1 for r in results if r["status"] != "success"),
        "results": results,
    }


@router.delete("/chat/attach", summary="清空会话附件")
def clear_attach(session_id: str = Form(...)):
    """释放指定会话的附件内存与落盘文件，并清理预览缓存。"""
    clear(session_id)
    # 同步清理预览缓存，避免残留旧文件的分页结果
    for _k in list(_PREVIEW_CACHE.keys()):
        if _k[0] == session_id:
            _PREVIEW_CACHE.pop(_k, None)
    return {"session_id": session_id, "cleared": True}


@router.post("/chat/attach/delete", summary="删除单个会话附件")
def delete_attach_file(session_id: str = Form(...), file_name: str = Form(...)):
    """删除指定会话中的单个附件，并清理该文件的预览缓存。"""
    delete_file(session_id, file_name)
    _PREVIEW_CACHE.pop((session_id, os.path.basename(file_name)), None)
    return {
        "session_id": session_id,
        "file_name": file_name,
        "deleted": True,
        "attached_files": list_files(session_id),
    }


# 附件预览解析结果缓存：按 (session_id, file_name) 缓存，避免翻页时重复解析大文档
_PREVIEW_CACHE: dict = {}

# 解析去重锁：同一附件的并发预览（如上传后自动预览 + 用户手点）只解析一次
_PARSE_LOCKS: Dict[tuple, threading.Lock] = {}
_PARSE_LOCKS_GUARD = threading.Lock()

# 预览正文总字数上限，避免超大文档拖垮前端
_PREVIEW_MAX_CHARS = 30000


def _parse_lock(key: tuple) -> threading.Lock:
    with _PARSE_LOCKS_GUARD:
        lk = _PARSE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PARSE_LOCKS[key] = lk
        return lk


def _truncate_pages(pages: List[Dict]) -> List[Dict]:
    """把分页正文截断到 ``_PREVIEW_MAX_CHARS`` 以内（保留页码，末页注明截断）。"""
    total = sum(len(p.get("content") or "") for p in pages)
    if total <= _PREVIEW_MAX_CHARS:
        return pages
    kept, acc = [], 0
    for p in pages:
        if acc >= _PREVIEW_MAX_CHARS:
            break
        content = p.get("content") or ""
        remain = _PREVIEW_MAX_CHARS - acc
        if len(content) <= remain:
            kept.append(p)
            acc += len(content)
        else:
            kept.append({
                "page": p.get("page"),
                "content": content[:remain] + "\n…（内容过长，已截断显示前 30000 字）",
            })
            break
    return kept


@router.get("/chat/attach/preview", summary="预览会话附件内容")
def attach_preview(session_id: str, file_name: str, page: int = 1):
    """
    按 session_id + 文件名预览已上传的会话附件内容：

    - 图片类：返回 base64 data URL，供前端 <img> 直接渲染。
    - 文本类（Word/PDF/PPT/Excel/MD/TXT）：解析为分页纯文本返回，
      前端按页展示，模拟 PDF/Word 预览插件的翻页阅读体验（总量截断前 30000 字）。
    """
    pipe = get_pipeline()
    sess_dir = os.path.join(ATTACH_DIR, session_id)
    path = os.path.join(sess_dir, os.path.basename(file_name))
    if not os.path.isfile(path):
        return {"type": "error", "message": f"未找到附件：{file_name}"}
    ext = os.path.splitext(file_name)[1].lower()

    # 图片：直接转 data URL 渲染
    if ext in {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}:
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            mime = _IMAGE_MIME.get(ext.lstrip("."), "image/jpeg")
            return {"type": "image", "data_url": f"data:{mime};base64,{b64}"}
        except Exception as e:
            return {"type": "error", "message": f"图片读取失败：{str(e)[:200]}"}

    # 文本类：优先复用上传时的解析结果，其次才重新解析（带缓存，翻页不再重复解析）
    cache_key = (session_id, os.path.basename(file_name))
    data = _PREVIEW_CACHE.get(cache_key)
    if data is None:
        # ① 快路径：上传附件时已解析过一次，这里直接取分页原文，秒回。
        #    之前每次预览都重新 load_document，docx/pptx 里的图片要走视觉模型，
        #    常常 30s+，前端会报 read timeout。
        pages = get_pages(session_id, file_name)
        if pages is None:
            # ② 兜底：内存里没有（如后端重启过）才真正重新解析，并做并发去重
            with _parse_lock(cache_key):
                data = _PREVIEW_CACHE.get(cache_key)
                if data is None:
                    try:
                        docs = load_document(
                            path,
                            file_name,
                            vision_api_key=pipe.cfg.zhipu_api_key,
                            vision_model=pipe.cfg.vision_model,
                        )
                    except Exception as e:
                        return {"type": "error", "message": f"预览解析失败：{str(e)[:200]}"}
                    pages = [
                        {"page": d.metadata.get("page", i), "content": d.page_content}
                        for i, d in enumerate(docs, start=1)
                    ]
        if data is None:
            pages = _truncate_pages(pages or [])
            data = {"file_name": file_name, "ext": ext, "page_count": len(pages), "pages": pages}
            _PREVIEW_CACHE[cache_key] = data

    return {"type": "text", **data}


@router.post("/translate", response_model=TranslateResponse, summary="文本翻译")
def translate(req: TranslateRequest):
    """
    通用文本翻译：将用户输入翻译为目标语言，保留原文的格式与专业术语。
    直接调用对话大模型完成，不检索知识库。
    """
    pipe = get_pipeline()
    if not pipe.cfg.zhipu_api_key:
        raise HTTPException(status_code=400, detail="未配置 ZHIPU_API_KEY，无法翻译。")
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="待翻译内容为空。")

    src = (req.source_lang or "自动检测").strip()
    tgt = (req.target_lang or "英文").strip()
    src_tip = "（请自动识别源语言）" if src in ("", "自动检测", "auto") else f"（源语言：{src}）"
    system = (
        f"你是一个专业、严谨的翻译引擎。请把用户提供的文本翻译成{tgt}{src_tip}。"
        "要求：忠实原意、术语准确、语句通顺自然；保留原文的段落、列表、编号等格式；"
        "只输出译文本身，不要添加解释、注释或多余说明。"
    )
    model = req.model or pipe.cfg.chat_model
    try:
        translated = zhipu_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": req.text},
            ],
            pipe.cfg.zhipu_api_key,
            model,
            temperature=0.3,
            top_p=0.9,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"翻译失败：{str(e)[:300]}")
    return TranslateResponse(translated=translated, target_lang=tgt)


@router.post("/chat/enhance", summary="提示词增强")
def enhance(req: EnhanceRequest):
    """
    将用户的简短输入按指定风格增强为更有效的提示词，返回增强后的文本，
    供前端回填到输入框，由用户确认后再发送。
    """
    pipe = get_pipeline()
    if not pipe.cfg.zhipu_api_key:
        raise HTTPException(status_code=400, detail="未配置 ZHIPU_API_KEY，无法增强提示词。")
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="提示词内容为空。")

    system = ENHANCE_STYLES.get(req.style, ENHANCE_STYLES["default"])
    model = req.model or pipe.cfg.chat_model
    try:
        enhanced = zhipu_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": req.text},
            ],
            pipe.cfg.zhipu_api_key,
            model,
            temperature=0.4,
            top_p=0.9,
        )
    except Exception as e:
        return {"enhanced": "", "style": req.style, "error": str(e)[:300]}
    return {"enhanced": enhanced, "style": req.style}
