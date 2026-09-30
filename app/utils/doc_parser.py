"""文档解析与重建工具。

支持 Word(.docx)、PDF(.pdf)、PPT(.pptx)、文本(.txt/.md) 的读取与结果写出。
- `translate_docx`：原地翻译 Word，逐段落/表格单元格翻译，可选「替换原文」或「对照翻译」模式；
- `write_paragraphs_to_pdf`：将译文段落直接生成可打开的 PDF（支持中文）。
移植自 TranlateProject，保留全部版式/样式处理逻辑。
"""
import copy
import os

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from docx.table import Table


def _iter_paragraphs_in_order(document):
    """按文档阅读顺序产出所有段落（含表格单元格、嵌套表格）。

    普通 `document.paragraphs` 只返回正文直接子段落，会漏掉表格内文字，
    因此这里递归遍历 body 与表格单元格。
    """
    def walk(parent_el, proxy):
        for child in parent_el.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, proxy)
            elif isinstance(child, CT_Tbl):
                tbl = Table(child, proxy)
                for row in tbl.rows:
                    for cell in row.cells:
                        yield from walk(cell._tc, cell)

    yield from walk(document.element.body, document)


def _insert_paragraph_after(src_para, text):
    """在 `src_para` 之后插入一个携带 `text` 的新段落，保留段落样式与首段字符格式。"""
    new_p = OxmlElement("w:p")
    pPr = src_para._p.find(qn("w:pPr"))
    if pPr is not None:
        new_p.append(copy.deepcopy(pPr))
    run = OxmlElement("w:r")
    first_run = src_para._p.find(qn("w:r"))
    if first_run is not None:
        rPr = first_run.find(qn("w:rPr"))
        if rPr is not None:
            run.append(copy.deepcopy(rPr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run.append(t)
    new_p.append(run)
    src_para._p.addnext(new_p)
    return Paragraph(new_p, src_para._parent)


def translate_docx(source_path, translate_fn, output_path, mode="replace"):
    """翻译 Word 文档。

    mode="replace"  ：将原文段落/单元格文本替换为译文（保留样式与结构）。
    mode="parallel" ：保留原文，并在其后插入对应译文（对照翻译 / 上下对照）。
    表格（含嵌套表格）内的文字均会被翻译。
    """
    document = Document(source_path)
    paras = list(_iter_paragraphs_in_order(document))

    if mode == "parallel":
        for para in paras:
            if para.text.strip():
                translated = translate_fn(para.text)
                _insert_paragraph_after(para, translated)
    else:
        for para in paras:
            if para.text.strip():
                para.text = translate_fn(para.text)

    document.save(output_path)
    return output_path


def translate_docx_to_pdf(source_path, translate_fn, output_path, mode="replace"):
    """将 Word 文档翻译后直接生成 PDF。

    会按文档顺序提取正文与表格文字；对照模式下先写原文行再写译文行。
    """
    document = Document(source_path)
    paras = list(_iter_paragraphs_in_order(document))
    lines = []
    for para in paras:
        if mode == "parallel":
            lines.append(para.text)
            if para.text.strip():
                lines.append(translate_fn(para.text))
        else:
            lines.append(translate_fn(para.text) if para.text.strip() else "")
    write_paragraphs_to_pdf(lines, output_path)
    return output_path


def write_paragraphs_to_pdf(paragraphs, output_path: str, title: str = ""):
    """将译文段落列表直接生成 PDF（支持中文，含基础排版）。"""
    try:
        from reportlab.lib.enums import TA_JUSTIFY
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
        )
        from xml.sax.saxutils import escape
    except ImportError:
        raise RuntimeError(
            "生成 PDF 需要 reportlab 库，请先执行 `pip install reportlab` 后重试。"
        )

    # 注册中文 CID 字体（reportlab 内置，无需额外字体文件，保证中文不乱码）
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        cjk_font = "STSong-Light"
    except Exception:
        cjk_font = "Helvetica"

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "BodyCN",
        parent=styles["Normal"],
        fontName=cjk_font,
        fontSize=11,
        leading=19,
        alignment=TA_JUSTIFY,
        firstLineIndent=22,
        spaceAfter=6,
    )
    title_style = ParagraphStyle(
        "TitleCN",
        parent=styles["Title"],
        fontName=cjk_font,
        fontSize=16,
        leading=24,
    )

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )
    flow = []
    if title:
        flow.append(Paragraph(escape(title), title_style))
        flow.append(Spacer(1, 12))
    for para in paragraphs:
        if para and para.strip():
            # 转义 XML 特殊字符，避免 reportlab 解析失败
            flow.append(Paragraph(escape(para), body))
        else:
            flow.append(Spacer(1, 12))
    doc.build(flow)


def extract_pdf_text(path: str) -> str:
    """提取 PDF 全文，按页以空行分隔。"""
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages)


def extract_pptx_text(path: str) -> str:
    """提取 PPT 全文（按幻灯片 / 形状 / 表格阅读顺序）。"""
    from pptx import Presentation

    prs = Presentation(path)
    paras = _collect_pptx_paragraphs(prs)
    lines = [p.text for p in paras if p.text and p.text.strip()]
    return "\n".join(lines)


def extract_document_text(path: str, max_chars: int = 20000) -> str:
    """按扩展名提取文档纯文本（支持 txt / md / docx / pdf / pptx）。

    - 长文本会被截断到 `max_chars`，避免对话上下文过大；
    - 不支持的类型抛出 ValueError，由调用方决定是否忽略。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    elif ext == ".docx":
        document = Document(path)
        paras = list(_iter_paragraphs_in_order(document))
        text = "\n".join(p.text for p in paras)
    elif ext == ".pdf":
        text = extract_pdf_text(path)
    elif ext == ".pptx":
        text = extract_pptx_text(path)
    else:
        raise ValueError(f"不支持的聊天文件类型: {ext}")

    text = text or ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...（内容已截断，原文约 {len(text)} 字符）"
    return text


def split_into_paragraphs(text: str) -> list:
    """按行切分为段落，过滤纯空白行。"""
    blocks = []
    for raw in text.split("\n"):
        if raw.strip():
            blocks.append(raw.strip())
    return blocks


def write_text_to_docx(text: str, output_path: str):
    """将翻译后的文本写成一个 Word 文档（按行作为段落）。"""
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(output_path)


# ----------------------------- PPTX 翻译 -----------------------------


def _iter_pptx_paragraphs_in_shapes(shapes):
    """递归产出形状集合中的所有段落（含文本框、表格单元格、组合形状）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    for para in cell.text_frame.paragraphs:
                        yield para
        elif shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                yield para
        elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_pptx_paragraphs_in_shapes(shape.shapes)


def _collect_pptx_paragraphs(prs):
    """收集演示文稿中所有可翻译段落（按幻灯片顺序）。"""
    paras = []
    for slide in prs.slides:
        paras.extend(_iter_pptx_paragraphs_in_shapes(slide.shapes))
    return paras


def _insert_pptx_paragraph_after(src_para, text):
    """在 PPT 段落 `src_para` 之后插入携带 `text` 的新段落，保留段落与首段字符格式。"""
    from pptx.oxml.ns import qn
    from pptx.text.text import _Paragraph as PptxParagraph

    new_p = OxmlElement("a:p")
    pPr = src_para._p.find(qn("a:pPr"))
    if pPr is not None:
        new_p.append(copy.deepcopy(pPr))
    run = OxmlElement("a:r")
    first_run = src_para._p.find(qn("a:r"))
    if first_run is not None:
        rPr = first_run.find(qn("a:rPr"))
        if rPr is not None:
            run.append(copy.deepcopy(rPr))
    t = OxmlElement("a:t")
    t.text = text
    run.append(t)
    new_p.append(run)
    src_para._p.addnext(new_p)
    return PptxParagraph(new_p, src_para._parent)


def _append_pptx_text_with_breaks(run, text):
    """向 run 写入文本，遇到换行符 \\n 转为 <a:br/> 软换行（避免新增段落破坏版式）。"""
    # xml:space 属于保留的 XML 命名空间，需用完整 URI（pptx 的 qn 不认识 xml: 前缀）
    XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
    for i, part in enumerate(text.split("\n")):
        if i > 0:
            run.append(OxmlElement("a:br"))
        if part:
            t = OxmlElement("a:t")
            t.text = part
            t.set(XML_SPACE, "preserve")
            run.append(t)


def _replace_pptx_para_runs(para, text):
    """替换段落文本，完整保留样式与版式：每个 run 的 rPr（字体/颜色/加粗/超链接）、
    run 内部的软换行 <a:br/> 与制表符 <a:tab/>、run 之间的换行、以及字段 <a:fld/> 等标记
    全部原样保留；仅把译文按原文本长度比例分配回各文本片段。

    注意：python-pptx 的 `Paragraph.text` 只读会丢弃 <a:br/> 等标记，因此直接按 XML 顺序
    展开段落（连 run 内部也展开），逐片段重建，既不改变样式也不破坏版式。
    """
    from pptx.oxml.ns import qn as aqn

    p = para._p
    children = list(p)

    # 展开为顶层 token；run 内部再展开为 (t/br/tab) 片段
    top_tokens = []   # ("run", rPr_el, segs) | ("mark", tag) | ("keep", el)
    run_segs = []     # 与 run token 一一对应的片段列表
    for child in children:
        tag = child.tag
        if tag == aqn("a:r"):
            rPr = child.find(aqn("a:rPr"))
            segs = []
            for sub in child:
                stag = sub.tag
                if stag == aqn("a:t"):
                    segs.append(("t", sub.text if sub.text is not None else ""))
                elif stag == aqn("a:br"):
                    segs.append(("br", None))
                elif stag == aqn("a:tab"):
                    segs.append(("tab", None))
            run_segs.append(segs)
            top_tokens.append(("run", rPr, segs))
        elif tag in (aqn("a:br"), aqn("a:tab")):
            top_tokens.append(("mark", tag))
        else:
            # a:pPr / a:fld / 其它：原样保留，不翻译
            top_tokens.append(("keep", child))

    # 收集所有 t 片段（文档顺序）用于分配译文
    flat = []     # (run_i, seg_j)
    lengths = []
    for ri, segs in enumerate(run_segs):
        for sj, (k, v) in enumerate(segs):
            if k == "t":
                flat.append((ri, sj))
                lengths.append(len(v))
    if not flat:
        return  # 无可翻译文本（如仅含字段），保持原样
    total = sum(lengths) or 1
    n = len(flat)

    # 按原文本长度比例把整段译文分配回各 t 片段
    pieces = []
    rem = len(text)
    pos = 0
    for i, L in enumerate(lengths):
        if i == n - 1:
            a = rem
        else:
            a = int(round(len(text) * (L / total)))
            rem -= a
        pieces.append(text[pos:pos + a])
        pos += a
    piece_for = {flat[i]: pieces[i] for i in range(n)}

    # 重建段落
    for child in children:
        p.remove(child)

    run_counter = 0
    for kind, *rest in top_tokens:
        if kind == "keep":
            p.append(copy.deepcopy(rest[0]))
        elif kind == "mark":
            local = rest[0].split("}")[-1]
            p.append(OxmlElement("a:" + local))
        else:  # run
            rPr, segs = rest
            ri = run_counter
            run_counter += 1
            new_r = OxmlElement("a:r")
            if rPr is not None:
                new_r.append(copy.deepcopy(rPr))
            for sj, (k, v) in enumerate(segs):
                if k == "t":
                    _append_pptx_text_with_breaks(new_r, piece_for[(ri, sj)])
                elif k == "br":
                    new_r.append(OxmlElement("a:br"))
                elif k == "tab":
                    new_r.append(OxmlElement("a:tab"))
            p.append(new_r)


def translate_pptx(source_path, translate_fn, output_path, mode="replace"):
    """逐行翻译 PPT 文档，保留幻灯片版式与文本框结构。

    mode="replace"  ：译文覆盖原文（保留每个 run 的字体/颜色/加粗等样式）。
    mode="parallel" ：保留原文，并在其后插入对应译文（上下对照，沿用原段样式）。
    支持文本框、表格单元格与组合形状内的文字。
    """
    from pptx import Presentation

    prs = Presentation(source_path)
    paras = _collect_pptx_paragraphs(prs)
    if mode == "parallel":
        for para in paras:
            if para.text.strip():
                _insert_pptx_paragraph_after(para, translate_fn(para.text))
    else:
        for para in paras:
            if para.text.strip():
                _replace_pptx_para_runs(para, translate_fn(para.text))
    prs.save(output_path)
    return output_path


def translate_pptx_to_pdf(source_path, translate_fn, output_path, mode="replace"):
    """将 PPT 翻译后直接生成 PDF（按幻灯片阅读顺序提取文字，含表格）。"""
    from pptx import Presentation

    prs = Presentation(source_path)
    paras = _collect_pptx_paragraphs(prs)
    lines = []
    for para in paras:
        if mode == "parallel":
            lines.append(para.text)
            if para.text.strip():
                lines.append(translate_fn(para.text))
        else:
            lines.append(translate_fn(para.text) if para.text.strip() else "")
    write_paragraphs_to_pdf(lines, output_path)
    return output_path
