"""文档翻译服务（基于 LangChain + 智谱 AI，复用企业项目的 zhipu_chat）。

支持 Word(.docx)、PDF(.pdf)、PPT(.pptx)、文本(.txt/.md) 的批量翻译：
- 按段落/表格单元格翻译，保留文档结构与（Word/PPT 的）段落样式；
- 支持「替换原文」与「对照翻译（原文+译文）」两种模式；
- 超长段落自动按字符切分后再合并，避免超出模型上下文；
- 业务逻辑完全由本服务承载，与 UI 解耦。

移植自 TranlateProject，将 TranlateProject 的 build_llm(config) 替换为
企业项目已有的 ``app.core.llm.zhipu_chat``（直接调用智谱 OpenAI 兼容接口）。
"""
import os

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.llm import zhipu_chat
from app.core.config import load_config
from app.utils import doc_parser, file_utils

# 翻译默认使用纯文本模型（视觉模型对翻译无必要，且输出上限更严）
DEFAULT_TRANSLATE_MODEL = "glm-4-flash"


def _make_translator(target_lang: str, model: str = None, source_lang: str = None):
    """构造一个段落翻译函数。

    source_lang 为可选源语言（如「中文」）；为 None / 空 / 「自动检测」/「auto」
    时，提示词要求模型自动识别源语言，与文本翻译接口行为保持一致。
    """
    cfg = load_config()
    api_key = cfg.zhipu_api_key
    model = model or DEFAULT_TRANSLATE_MODEL
    src = (source_lang or "").strip()
    if src in ("", "自动检测", "auto", "detect"):
        src_tip = "（请自动识别源语言）"
    else:
        src_tip = f"（源语言：{src}）"
    instruction = (
        "你是一名专业的多语言翻译专家，擅长将各类文档准确、流畅地翻译为目标语言。"
        f"请将下面提供的文本{src_tip}翻译为【{target_lang}】。要求：\n"
        "1）仅输出译文，不要添加任何解释、前缀或引号；\n"
        "2）保持原文的专业术语、人名、地名、数字、代码与格式标记不变；\n"
        "3）保持原有段落结构，不要合并或拆分段落。"
    )

    def translate(text: str) -> str:
        if not text.strip():
            return text
        # zhipu_chat 接受 OpenAI 格式消息列表
        messages = [{"role": "user", "content": instruction + "\n\n" + text}]
        return zhipu_chat(
            messages,
            api_key,
            model=model,
            temperature=0.3,
            top_p=0.9,
            max_tokens=2048,
        ).strip()

    return translate


def _translate_long(text: str, translate_fn, chunk_size: int = 3000, chunk_overlap: int = 200) -> str:
    """超长文本先切分再逐段翻译并拼接。"""
    if len(text) <= chunk_size:
        return translate_fn(text)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )
    parts = splitter.split_text(text)
    return "\n".join(translate_fn(p) for p in parts)


def translate_file(path: str, target_lang: str, model: str = None,
                   output_format: str = "docx", mode: str = "replace",
                   reserved: set = None, source_lang: str = None) -> str:
    """翻译单个文件，返回结果文件路径。

    output_format:
      - "docx" 输出 Word 文档；
      - "pdf"  输出 PDF 文档；
      - "pptx" 输出 PowerPoint 文档（仅对 PPT 输入有效，保留幻灯片版式）。
    mode: "replace" 替换原文；"parallel" 对照翻译（保留原文并附译文）。
    reserved: 本批次已占用的输出文件名集合（小写），用于同批同主体名去重；
              译文文件名默认与原文档保持一致。
    source_lang: 可选源语言（如「中文」）；为空/「自动检测」时由模型自动识别。
    """
    ext = os.path.splitext(path)[1].lower()
    # 非 PPT 输入不支持 pptx 输出，统一回退为 docx
    if output_format == "pptx" and ext != ".pptx":
        output_format = "docx"
    if ext == ".docx":
        return _translate_docx(path, target_lang, model, output_format, mode, reserved, source_lang)
    if ext == ".pdf":
        return _translate_pdf(path, target_lang, model, output_format, mode, reserved, source_lang)
    if ext == ".pptx":
        return _translate_pptx(path, target_lang, model, output_format, mode, reserved, source_lang)
    if ext in (".txt", ".md"):
        return _translate_text(path, target_lang, model, output_format, mode, reserved, source_lang)
    raise ValueError(f"不支持的文件格式: {ext}")


def _translate_docx(path, target_lang, model, output_format, mode, reserved=None, source_lang=None):
    translate = _make_translator(target_lang, model, source_lang)
    out = file_utils.output_path(path, "." + output_format, reserved)
    if output_format == "pdf":
        # 提取正文+表格文字翻译后生成 PDF（含对照模式）
        doc_parser.translate_docx_to_pdf(
            path, lambda t: _translate_long(t, translate), out, mode
        )
        return out

    # 默认 / docx：原地翻译段落与表格单元格，保留样式结构
    doc_parser.translate_docx(
        path, lambda t: _translate_long(t, translate), out, mode
    )
    return out


def _translate_pdf(path, target_lang, model, output_format, mode, reserved=None, source_lang=None):
    translate = _make_translator(target_lang, model, source_lang)
    text = doc_parser.extract_pdf_text(path)
    blocks = doc_parser.split_into_paragraphs(text)
    if mode == "parallel":
        rendered = []
        for b in blocks:
            rendered.append(b)
            if b.strip():
                rendered.append(_translate_long(b, translate))
    else:
        rendered = [
            _translate_long(b, translate) if b.strip() else "" for b in blocks
        ]
    if output_format == "pdf":
        out = file_utils.output_path(path, ".pdf", reserved)
        doc_parser.write_paragraphs_to_pdf(rendered, out)
        return out
    out = file_utils.output_path(path, ".docx", reserved)
    doc_parser.write_text_to_docx("\n".join(rendered), out)
    return out


def _translate_pptx(path, target_lang, model, output_format, mode, reserved=None, source_lang=None):
    translate = _make_translator(target_lang, model, source_lang)
    if output_format == "pdf":
        out = file_utils.output_path(path, ".pdf", reserved)
        doc_parser.translate_pptx_to_pdf(
            path, lambda t: _translate_long(t, translate), out, mode
        )
        return out
    # 默认 / pptx / docx：输出翻译后的 PPTX（保留幻灯片版式与文本框结构）
    out = file_utils.output_path(path, ".pptx", reserved)
    doc_parser.translate_pptx(
        path, lambda t: _translate_long(t, translate), out, mode
    )
    return out


def _translate_text(path, target_lang, model, output_format, mode, reserved=None, source_lang=None):
    translate = _make_translator(target_lang, model, source_lang)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    blocks = doc_parser.split_into_paragraphs(text)
    if mode == "parallel":
        rendered = []
        for b in blocks:
            rendered.append(b)
            if b.strip():
                rendered.append(_translate_long(b, translate))
    else:
        rendered = [
            _translate_long(b, translate) if b.strip() else "" for b in blocks
        ]
    ext = os.path.splitext(path)[1].lower()

    if output_format == "pdf":
        out = file_utils.output_path(path, ".pdf", reserved)
        doc_parser.write_paragraphs_to_pdf(rendered, out)
        return out
    if output_format == "docx" or ext not in (".txt", ".md"):
        out = file_utils.output_path(path, ".docx", reserved)
        doc_parser.write_text_to_docx("\n".join(rendered), out)
        return out
    # 保持原格式
    out = file_utils.output_path(path, ext, reserved)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(rendered))
    return out
