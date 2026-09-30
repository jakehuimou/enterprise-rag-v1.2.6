"""文档翻译接口：接收上传文档（可多选），返回翻译结果（单文件直接下载，多文件打包 zip）。

与单文本翻译接口 ``/api/translate`` 区分，本接口面向「文档级批量翻译」。
"""
import os
import tempfile
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.core.config import load_config
from app.core.loaders import load_document
from app.utils import file_utils
from app.services import translation_service

router = APIRouter(prefix="/api", tags=["translate-doc"])

# 译文预览解析缓存：按 (文件名, 修改时间) 缓存，避免翻页重复解析；
# 用 mtime 参与键值，保证同名文件被重新翻译后不会命中旧缓存。
_PREVIEW_CACHE: dict = {}
# 单页大致字数（用于 docx/txt 这类无天然分页的文档切页展示）
_PAGE_CHARS = 1500


@router.post("/translate/doc", summary="上传文档批量翻译")
async def api_translate_doc(
    files: list[UploadFile] = File(...),
    target_language: str = Form(...),
    output_format: str = Form("docx"),
    mode: str = Form("replace"),
    model: str = Form(None),
    source_language: str = Form(None),
):
    """批量翻译上传的文档，返回翻译结果文件。

    - files：一个或多个待翻译文档（Word/PDF/PPT/TXT/MD）。
    - target_language：目标语言，如「英文」「中文」。
    - output_format：docx / pdf（pptx 仅对 pptx 输入生效，其余回退 docx）。
    - mode：replace（替换原文）/ parallel（对照翻译，原文+译文）。
    """
    if not files:
        raise HTTPException(status_code=400, detail="未接收到文件")
    if output_format not in ("docx", "pdf", "pptx"):
        output_format = "docx"
    if mode not in ("replace", "parallel"):
        mode = "replace"

    file_utils.ensure_dirs()

    saved_paths = []
    try:
        for uf in files:
            if not file_utils.is_allowed(uf.filename):
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的文件格式: {os.path.splitext(uf.filename)[1]}",
                )
            content = await uf.read()
            dest = os.path.join(
                file_utils.UPLOAD_DIR, file_utils.safe_filename(uf.filename)
            )
            with open(dest, "wb") as f:
                f.write(content)
            saved_paths.append(dest)

        # 阻塞式同步翻译（含模型网络请求）交由线程池执行，避免占用事件循环；
        # 这里串行翻译文件，保证单个大文档翻译期间其它请求不被卡死（Gradio 单用户场景足够）。
        outputs = []
        reserved: set = set()  # 同一批次内已占用的输出文件名，避免同主体名互相覆盖
        for p in saved_paths:
            out = await run_in_threadpool(
                translation_service.translate_file,
                p, target_language, model, output_format, mode, reserved,
                source_lang=source_language,
            )
            outputs.append(out)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"翻译失败: {e}")

    # 清理上传的临时文件
    for p in saved_paths:
        try:
            os.remove(p)
        except Exception:
            pass

    if len(outputs) == 1:
        path = outputs[0]
        name = os.path.basename(path)
        # 额外回传原始文件名（URL 编码，保证中文名可被前端可靠读取并用于预览）
        return FileResponse(
            path, filename=name, headers={"X-File-Name": quote(name)}
        )

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".zip", dir=file_utils.OUTPUT_DIR
    )
    with zipfile.ZipFile(tmp.name, "w") as zf:
        for p in outputs:
            zf.write(p, arcname=os.path.basename(p))
    tmp.close()
    return FileResponse(tmp.name, filename="translated_documents.zip")


def _paginate_text(text: str, page_chars: int = _PAGE_CHARS):
    """把无天然分页的长文本按段落切成约 page_chars 字一页，便于前端翻页阅读。"""
    lines = text.split("\n")
    pages, buf, cur = [], [], 0
    for ln in lines:
        if buf and cur + len(ln) + 1 > page_chars:
            pages.append("\n".join(buf).strip("\n"))
            buf, cur = [], 0
        buf.append(ln)
        cur += len(ln) + 1
    if buf:
        pages.append("\n".join(buf).strip("\n"))
    pages = [p for p in pages if p.strip()]
    return pages or [text.strip()]


@router.get("/translate/doc/preview", summary="预览翻译结果文档")
def api_translate_doc_preview(file: str, page: int = 1):
    """按文件名预览 OUTPUT_DIR 中的译文文档（翻译后在线预览）。

    - PDF/PPT 等有天然分页的文档：按页返回；Word/TXT 等：按约 1500 字切页；
    - 多文件打包的 zip 不支持在线预览，返回提示；
    - 返回 ``type=text`` + ``pages`` / ``page_count``，前端按页渲染「文档阅读器」。
    """
    name = file_utils.safe_filename(os.path.basename(file or ""))
    if not name:
        return {"type": "error", "message": "缺少文件名。"}
    path = os.path.join(file_utils.OUTPUT_DIR, name)
    if not os.path.isfile(path):
        return {"type": "error", "message": f"未找到译文文件：{name}"}
    ext = os.path.splitext(name)[1].lower()
    if ext == ".zip":
        return {
            "type": "error",
            "message": "本批为多文件打包（zip），暂不支持在线预览，请下载后查看。",
        }

    mtime = os.path.getmtime(path)
    cache_key = (name, mtime)
    data = _PREVIEW_CACHE.get(cache_key)
    if data is None:
        cfg = load_config()
        try:
            docs = load_document(
                path, name,
                vision_api_key=cfg.zhipu_api_key,
                vision_model=cfg.vision_model,
            )
        except Exception as e:
            return {"type": "error", "message": f"预览解析失败：{str(e)[:200]}"}

        if len(docs) > 1:
            pages = [
                {"page": d.metadata.get("page", i), "content": d.page_content}
                for i, d in enumerate(docs, start=1)
            ]
        else:
            text = docs[0].page_content if docs else ""
            pages = [
                {"page": i, "content": c}
                for i, c in enumerate(_paginate_text(text), start=1)
            ]

        # 总量截断，避免超大文档拖垮前端
        total_chars = sum(len(p["content"]) for p in pages)
        if total_chars > 30000:
            kept, acc = [], 0
            for p in pages:
                if acc >= 30000:
                    break
                remain = 30000 - acc
                if len(p["content"]) <= remain:
                    kept.append(p)
                    acc += len(p["content"])
                else:
                    kept.append({
                        "page": p["page"],
                        "content": p["content"][:remain] + "\n…（内容过长，已截断显示前 30000 字）",
                    })
                    break
            pages = kept

        data = {"file_name": name, "ext": ext, "page_count": len(pages), "pages": pages}
        _PREVIEW_CACHE[cache_key] = data

    return {"type": "text", **data}
