"""
文档解析层（Document Parsing Layer）
===================================
采用 LangChain 的 Loader 组件思想，将不同格式的文件统一解析为标准的
``langchain_core.documents.Document`` 对象，每个对象包含：
  - ``page_content``：正文内容
  - ``metadata``：元数据（文件名 / 页码 / 上传时间 / 文件路径等，保障来源可追溯）

支持格式：Word(.docx/.doc)、PDF(.pdf)、PPT(.pptx/.ppt)、Excel(.xlsx/.xls)、
Markdown(.md)、纯文本(.txt)，以及图片(.jpg/.jpeg/.png/.bmp/.gif/.webp)。

图片类文件无法用传统文本 Loader 解析，统一经视觉大模型（glm-4v 系列）生成
文字描述后，再进入「分块 -> 向量化 -> 检索 -> 问答」链路。

设计原则：
  - 优先使用 LangChain 社区 Loader（如 PyMuPDFLoader、Docx2txtLoader 等）；
  - 当对应解析库未安装时，自动回退到基于 pypdf / python-docx / python-pptx /
    openpyxl 的轻量内置实现，确保工程始终可运行。
"""
import os
import tempfile
import uuid
from datetime import datetime
from typing import List

from langchain_core.documents import Document


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_metadata(metadata, file_name: str, upload_time: str, file_path: str) -> dict:
    """统一补全元数据字段，保证来源可追溯。"""
    meta = dict(metadata or {})
    meta.setdefault("source", file_path)
    meta.setdefault("file_name", file_name)
    meta.setdefault("file_path", file_path)
    meta.setdefault("upload_time", upload_time)
    meta.setdefault("page", meta.get("page", 1))
    return meta


def _wrap(raw_docs: List[Document], file_name: str, upload_time: str, file_path: str) -> List[Document]:
    """将解析得到的原始 Document 列表统一补全元数据。"""
    return [
        Document(
            page_content=d.page_content,
            metadata=_ensure_metadata(d.metadata, file_name, upload_time, file_path),
        )
        for d in raw_docs
    ]


def load_document(
    file_path: str,
    file_name: str = None,
    upload_time: str = None,
    vision_api_key: str = None,
    vision_model: str = "glm-4v-flash",
) -> List[Document]:
    """
    根据文件扩展名分发到对应的 Loader，返回标准 Document 列表。

    :param file_path: 文件在服务器上的实际路径
    :param file_name: 文件原始名称（用于元数据追溯）
    :param upload_time: 上传时间（用于元数据追溯）
    :param vision_api_key: 视觉模型 API Key（解析图片必需）
    :param vision_model: 视觉模型名（默认 glm-4v-flash）
    """
    file_name = file_name or os.path.basename(file_path)
    upload_time = upload_time or _now()
    ext = os.path.splitext(file_path)[1].lower()

    dispatch = {
        ".pdf": _load_pdf,
        ".docx": _load_docx,
        ".doc": _load_docx,
        ".pptx": _load_ppt,
        ".ppt": _load_ppt,
        ".xlsx": _load_excel,
        ".xls": _load_excel,
        ".md": _load_text,
        ".markdown": _load_text,
        ".txt": _load_text,
        # 图片类：均经视觉模型生成文字描述
        ".jpg": _load_image,
        ".jpeg": _load_image,
        ".png": _load_image,
        ".bmp": _load_image,
        ".gif": _load_image,
        ".webp": _load_image,
    }
    loader = dispatch.get(ext)
    if loader is None:
        # 未知格式统一按纯文本处理，避免解析失败
        loader = _load_text
    if loader is _load_image:
        return loader(file_path, file_name, upload_time, vision_api_key, vision_model)
    if loader is _load_pdf:
        return loader(file_path, file_name, upload_time, vision_api_key, vision_model)
    return loader(file_path, file_name, upload_time)


# ----------------------- 各格式 Loader 实现 -----------------------

def _load_pdf(file_path, file_name, upload_time, vision_api_key=None, vision_model="glm-4v-flash") -> List[Document]:
    """
    解析 PDF：优先提取文本；若检测到扫描件/图片型页面（单页文字过少），
    则自动将该页渲染为图片并通过视觉模型做 OCR 描述，保证课程表、手册等
    图片密集型 PDF 也能被检索与问答。
    """
    raw: List[Document] = []
    try:
        from langchain_community.document_loaders import PyMuPDFLoader
        raw = PyMuPDFLoader(file_path).load()
    except Exception:
        # 回退：pypdf 逐页解析
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        raw = [
            Document(page_content=(page.extract_text() or ""), metadata={"page": i + 1})
            for i, page in enumerate(reader.pages)
        ]

    if not raw:
        return _wrap(raw, file_name, upload_time, file_path)

    # 判断是否需要 OCR：平均每页有效字符 < 80，或任意一页 < 30 且总页数 <= 50
    avg_len = sum(len(d.page_content.strip()) for d in raw) / len(raw)
    need_ocr = avg_len < 80 or any(len(d.page_content.strip()) < 30 for d in raw)
    if not need_ocr:
        return _wrap(raw, file_name, upload_time, file_path)

        # 使用 PyMuPDF 将低密度页面渲染为图片后做视觉 OCR
    try:
        import pymupdf as fitz  # PyMuPDF（新版推荐导入名）
        from app.core.llm import zhipu_vision_caption

        doc = fitz.open(file_path)
        ocr_docs: List[Document] = []
        for d in raw:
            page_no = int(d.metadata.get("page", 1)) - 1
            if page_no < 0 or page_no >= doc.page_count:
                continue
            text = d.page_content.strip()
            # 文字充足的页面保留原文字；文字稀少的页面用 OCR 描述补充/替换
            if len(text) >= 120:
                ocr_docs.append(d)
                continue
            page = doc.load_page(page_no)
            pix = page.get_pixmap(dpi=200)
            tmp_png = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}.png")
            pix.save(tmp_png)
            try:
                caption = zhipu_vision_caption(
                    tmp_png, vision_api_key, vision_model,
                    prompt="请完整识别这张 PDF 页面中的所有文字、表格、课程安排、时间安排、章节标题等内容，"
                           "保留原始结构和表述，只输出识别到的内容，不要评论。"
                )
            except Exception:
                caption = text
            finally:
                try:
                    os.remove(tmp_png)
                except Exception:
                    pass
            merged_text = (text + "\n\n" + caption).strip() if text else caption
            ocr_docs.append(Document(
                page_content=merged_text,
                metadata={**dict(d.metadata), "ocr": True, "file_name": file_name},
            ))
        doc.close()
        raw = ocr_docs
    except Exception:
        # OCR 失败仍返回原始文本，避免阻塞
        pass
    return _wrap(raw, file_name, upload_time, file_path)


def _load_docx(file_path, file_name, upload_time) -> List[Document]:
    try:
        from langchain_community.document_loaders import Docx2txtLoader
        raw = Docx2txtLoader(file_path).load()
    except Exception:
        # 回退：python-docx 提取段落
        from docx import Document as DocxDocument
        doc = DocxDocument(file_path)
        text = "\n".join(p.text for p in doc.paragraphs if p.text)
        raw = [Document(page_content=text, metadata={"page": 1})]
    return _wrap(raw, file_name, upload_time, file_path)


def _load_ppt(file_path, file_name, upload_time) -> List[Document]:
    try:
        from langchain_community.document_loaders import UnstructuredPowerPointLoader
        raw = UnstructuredPowerPointLoader(file_path).load()
    except Exception:
        # 回退：python-pptx 逐幻灯片解析
        from pptx import Presentation
        prs = Presentation(file_path)
        raw = []
        for i, slide in enumerate(prs.slides):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(shape.text_frame.text)
            raw.append(Document(page_content="\n".join(texts), metadata={"page": i + 1}))
    return _wrap(raw, file_name, upload_time, file_path)


def _load_excel(file_path, file_name, upload_time) -> List[Document]:
    try:
        from langchain_community.document_loaders import UnstructuredExcelLoader
        raw = UnstructuredExcelLoader(file_path, mode="elements").load()
    except Exception:
        # 回退：openpyxl 逐工作表解析
        import openpyxl
        wb = openpyxl.load_workbook(file_path, data_only=True)
        raw = []
        for i, ws in enumerate(wb.worksheets):
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    rows.append("\t".join(cells))
            raw.append(Document(page_content="\n".join(rows), metadata={"page": i + 1, "sheet": ws.title}))
    return _wrap(raw, file_name, upload_time, file_path)


def _load_text(file_path, file_name, upload_time) -> List[Document]:
    try:
        from langchain_community.document_loaders import TextLoader
        raw = TextLoader(file_path, encoding="utf-8", autodetect_encoding=True).load()
    except Exception:
        # 回退：直接读取
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        raw = [Document(page_content=text, metadata={"page": 1})]
    return _wrap(raw, file_name, upload_time, file_path)


def _load_image(file_path, file_name, upload_time, vision_api_key=None, vision_model="glm-4v-flash") -> List[Document]:
    """
    图片解析：调用视觉大模型把图片内容转写为文字描述，作为可检索/可问答的文本片段。
    元数据标记 is_image=True 与 image_path，供问答阶段可选地附上原图做图文作答。
    """
    if not vision_api_key:
        raise ValueError("未配置 ZHIPU_API_KEY，无法解析图片（图片需经视觉模型生成文字描述）。")
    from app.core.llm import zhipu_vision_caption

    caption = zhipu_vision_caption(file_path, vision_api_key, vision_model)
    doc = Document(
        page_content=caption,
        metadata={"page": 1, "is_image": True, "image_path": file_path},
    )
    return _wrap([doc], file_name, upload_time, file_path)
