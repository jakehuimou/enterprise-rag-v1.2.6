"""结果导出层 REST 接口：把问答回答导出为 Word / Excel 文件。"""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.utils.export_builder import (
    build_docx,
    build_xlsx,
    export_filename,
    has_table,
)

router = APIRouter(prefix="/api/export", tags=["结果导出"])

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExportRequest(BaseModel):
    """导出请求。"""
    content: str               # 回答正文（Markdown）
    title: str = ""            # 文档标题（Word 用；为空则用默认标题）


def _attachment_header(filename: str) -> dict:
    """生成兼容中文文件名的 Content-Disposition（RFC 5987）。"""
    return {
        "Content-Disposition": (
            f"attachment; filename=\"{filename.encode('ascii', 'ignore').decode() or 'export'}\"; "
            f"filename*=UTF-8''{quote(filename)}"
        )
    }


@router.post("/word", summary="导出为 Word(.docx)")
def export_word(req: ExportRequest):
    """
    把回答正文（Markdown）渲染成 Word 文档并直接返回文件流。

    支持标题、段落、有序/无序列表、引用、代码块与表格；表格会渲染为真正的 Word 表格。
    """
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="没有可导出的回答内容。")
    title = (req.title or "").strip() or "对话回答导出"
    try:
        data = build_docx(title, content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Word 生成失败：{str(e)[:200]}")
    return Response(content=data, media_type=DOCX_MIME,
                    headers=_attachment_header(export_filename("word")))


@router.post("/excel", summary="导出为 Excel(.xlsx)")
def export_excel(req: ExportRequest):
    """
    抽取回答中的 Markdown 表格并导出为 Excel（一个表格一个 Sheet，表头冻结 + 筛选）。

    若回答中不含表格数据，返回 400 与可读原因，由前端提示用户。
    """
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="没有可导出的回答内容。")
    if not has_table(content):
        raise HTTPException(
            status_code=400,
            detail="当前回答中没有表格数据（需要 Markdown 表格），无法导出 Excel。",
        )
    try:
        data = build_xlsx(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Excel 生成失败：{str(e)[:200]}")
    return Response(content=data, media_type=XLSX_MIME,
                    headers=_attachment_header(export_filename("excel")))


@router.post("/check", summary="检查回答是否含可导出的表格")
def export_check(req: ExportRequest):
    """返回回答中是否含 Markdown 表格，供前端决定「导出 Excel」是否可用。"""
    return {"has_table": has_table(req.content or "")}
