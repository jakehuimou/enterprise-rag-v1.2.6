"""外部技能：智能 PPT 生成助手（pptgen）

由「技能注册中心」自动加载。在对话中输入 ``/pptgen <主题>`` 即可触发本函数，
它调用大模型生成大纲与正文，再用 python-pptx（已随项目依赖安装）渲染成
``.pptx`` 文件，最后把文件落盘路径作为 Markdown 返回。

设计要点
--------
- 默认走 Python 渲染链路（python-pptx），无需联网安装 Node 依赖即可工作。
- 大模型调用失败（无密钥 / 网络异常）时，自动回退到「确定性模板大纲」，
  保证**任何情况下都能产出一份可用的 PPT**，不会卡死。
- 支持参数（写在主题后面，空格分隔）：
    --pages N       页数（内容页数量，默认 8，范围 3-20）
    --palette NAME  配色方案名（见 PALETTES，默认 midnight-executive）
    --lang zh|en    语言（默认 zh）
示例：``/pptgen 企业知识库建设方案 --pages 10 --palette tech-dark``
"""

import os
import re
import json

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

from web.skill_sdk import call_llm

# 项目根目录（enterprise-rag/），用于放置生成产物
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "data", "generated_ppt")

# 配色方案（与 references/ 中的规范对应；这里取核心三色即可）
PALETTES = {
    "midnight-executive": ("1E2761", "CADCFC", "FFFFFF", "FFFFFF", "1E2761"),
    "tech-dark":          ("0D1117", "161B22", "58A6FF", "FFFFFF", "F0F6FC"),
    "coral-energy":       ("F96167", "F9E795", "2F3C7E", "FFFFFF", "1F2937"),
    "warm-terracotta":    ("B85042", "E7E8D1", "A7BEAE", "FDFBF7", "3D3D3D"),
    "ocean-gradient":     ("065A82", "1C7293", "21295C", "FFFFFF", "065A82"),
    "charcoal-minimal":   ("36454F", "F2F2F2", "212121", "FFFFFF", "36454F"),
    "teal-trust":         ("028090", "00A896", "02C39A", "FFFFFF", "065A60"),
    "berry-cream":        ("6D2E46", "A26769", "ECE2D0", "FDFBF7", "3D3D3D"),
    "sage-calm":          ("84B59F", "69A297", "50808E", "F5F9F8", "3D4F4E"),
    "cherry-bold":        ("990011", "FCF6F5", "2F3C7E", "FFFFFF", "1F2937"),
}


def _parse_args(raw):
    """从用户输入中解析主题与可选参数。"""
    pages = 8
    palette = "midnight-executive"
    lang = "zh"

    m = re.search(r"--pages\s+(\d+)", raw)
    if m:
        pages = max(3, min(20, int(m.group(1))))
    m = re.search(r"--palette\s+(\S+)", raw)
    if m and m.group(1) in PALETTES:
        palette = m.group(1)
    m = re.search(r"--lang\s+(zh|en)", raw)
    if m:
        lang = m.group(1)

    topic = re.sub(r"--pages\s+\d+", "", raw)
    topic = re.sub(r"--palette\s+\S+", "", topic)
    topic = re.sub(r"--lang\s+(zh|en)", "", topic)
    topic = topic.strip(" \t-")
    if not topic:
        topic = "未命名演示文稿"
    return topic, pages, palette, lang


def _build_outline_via_llm(topic, pages, lang, model):
    """用大模型生成结构化大纲；失败返回 None。"""
    lang_hint = "中文" if lang == "zh" else "English"
    prompt = (
        "你是一名专业的 PPT 大纲设计师。请为主题《%s》设计一份演示文稿大纲，"
        "使用%s。请只返回如下 JSON（不要解释、不要代码围栏）：\n"
        "{\n"
        '  "title": "主标题",\n'
        '  "subtitle": "副标题",\n'
        '  "sections": [\n'
        '    {"title": "章节标题", "points": ["要点1", "要点2", "要点3"]},\n'
        "    ... 共 %d 个内容章节\n"
        "  ]\n"
        "}" % (topic, lang_hint, pages)
    )
    try:
        text = call_llm(prompt, model, temperature=0.4)
    except Exception:
        return None
    # 容错解析：剥离可能的 ```json 围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("sections"):
        return None
    return data


def _fallback_outline(topic, pages, lang):
    """无大模型时的确定性大纲，保证一定能产出 PPT。"""
    if lang == "zh":
        subs = ["由 AI 智能生成", "结构清晰 · 配色专业", "一键生成 · 即拿即用"]
        sec_titles = ["项目背景", "核心目标", "关键能力", "实施方案",
                      "数据指标", "应用案例", "风险与对策", "总结展望",
                      "价值主张", "行动建议", "阶段规划", "资源保障"]
        summary = ["核心结论一：方案具备清晰路径",
                   "核心结论二：投入产出比可控",
                   "核心结论三：可快速落地见效"]
    else:
        subs = ["AI Generated", "Clear Structure", "Ready to Use"]
        sec_titles = ["Background", "Objectives", "Capabilities", "Approach",
                      "Metrics", "Cases", "Risks", "Outlook",
                      "Value", "Actions", "Roadmap", "Resources"]
        summary = ["Key takeaway 1", "Key takeaway 2", "Key takeaway 3"]
    sections = [{"title": sec_titles[i % len(sec_titles)],
                 "points": ["%s 要点一：说明相关内容" % sec_titles[i % len(sec_titles)],
                            "%s 要点二：补充关键信息" % sec_titles[i % len(sec_titles)],
                            "%s 要点三：强调落地价值" % sec_titles[i % len(sec_titles)]]}
                for i in range(pages)]
    return {"title": topic, "subtitle": subs[0], "sections": sections, "summary": summary}


def _rgb(hexstr):
    return RGBColor.from_string(hexstr)


def _add_bg(slide, prs, color):
    rect = slide.shapes.add_shape(1, 0, 0, prs.slide_width, prs.slide_height)
    rect.fill.solid()
    rect.fill.fore_color.rgb = _rgb(color)
    rect.line.fill.background()
    rect.shadow.inherit = False
    return rect


def _render(topic, outline, palette, lang, model_used):
    primary, secondary, accent, bg_light, text = PALETTES[palette]
    title_txt = outline.get("title") or topic
    subtitle = outline.get("subtitle", "")
    sections = outline.get("sections", [])
    toc = [s.get("title", "章节") for s in sections]
    summary = outline.get("summary") or [p for s in sections for p in s.get("points", [])][:3]

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    # 移除模板自带的默认空白页，避免生成结果首部多一张空 slide
    _sldIdLst = prs.slides._sldIdLst
    if len(_sldIdLst):
        _first = _sldIdLst[0]
        _rid = _first.get("{%s}id" % "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
        if _rid:
            prs.part.drop_rel(_rid)
        _sldIdLst.remove(_first)
    blank = prs.slide_layouts[6]

    # 封面
    s = prs.slides.add_slide(blank)
    _add_bg(s, prs, primary)
    tb = s.shapes.add_textbox(Inches(0.8), Inches(2.6), Inches(11.7), Inches(1.6))
    tf = tb.text_frame; tf.word_wrap = True
    p = tf.paragraphs[0]; p.text = title_txt
    p.font.size = Pt(46); p.font.bold = True; p.font.color.rgb = _rgb(accent); p.alignment = PP_ALIGN.CENTER
    if subtitle:
        sb = s.shapes.add_textbox(Inches(0.8), Inches(4.4), Inches(11.7), Inches(0.8))
        pf = sb.text_frame; pp = pf.paragraphs[0]; pp.text = subtitle
        pp.font.size = Pt(20); pp.font.color.rgb = _rgb(secondary); pp.alignment = PP_ALIGN.CENTER

    # 目录
    s = prs.slides.add_slide(blank)
    _add_bg(s, prs, bg_light)
    h = s.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.9))
    hp = h.text_frame.paragraphs[0]; hp.text = "目录" if lang == "zh" else "Contents"
    hp.font.size = Pt(36); hp.font.bold = True; hp.font.color.rgb = _rgb(primary)
    for i, t in enumerate(toc):
        nb = s.shapes.add_textbox(Inches(1.2), Inches(1.8 + i * 0.8), Inches(0.6), Inches(0.6))
        nb.text_frame.paragraphs[0].text = "%02d" % (i + 1)
        nb.text_frame.paragraphs[0].font.size = Pt(22); nb.text_frame.paragraphs[0].font.bold = True
        nb.text_frame.paragraphs[0].font.color.rgb = _rgb(primary)
        tb2 = s.shapes.add_textbox(Inches(2.0), Inches(1.8 + i * 0.8), Inches(9.5), Inches(0.6))
        tb2.text_frame.paragraphs[0].text = t
        tb2.text_frame.paragraphs[0].font.size = Pt(18); tb2.text_frame.paragraphs[0].font.color.rgb = _rgb(text)

    # 内容页
    for sec in sections:
        s = prs.slides.add_slide(blank)
        _add_bg(s, prs, bg_light)
        bar = s.shapes.add_shape(1, 0, 0, prs.slide_width, Inches(0.15))
        bar.fill.solid(); bar.fill.fore_color.rgb = _rgb(primary); bar.line.fill.background()
        hb = s.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.9))
        hb.text_frame.paragraphs[0].text = sec.get("title", "")
        hb.text_frame.paragraphs[0].font.size = Pt(32); hb.text_frame.paragraphs[0].font.bold = True
        hb.text_frame.paragraphs[0].font.color.rgb = _rgb(primary)
        cb = s.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(11.7), Inches(5))
        ctf = cb.text_frame; ctf.word_wrap = True
        for j, pt in enumerate(sec.get("points", [])):
            para = ctf.paragraphs[0] if j == 0 else ctf.add_paragraph()
            para.text = "• " + str(pt)
            para.font.size = Pt(18); para.font.color.rgb = _rgb(text); para.space_after = Pt(14)

    # 总结
    s = prs.slides.add_slide(blank)
    _add_bg(s, prs, primary)
    hb = s.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.9))
    hb.text_frame.paragraphs[0].text = "总结" if lang == "zh" else "Summary"
    hb.text_frame.paragraphs[0].font.size = Pt(36); hb.text_frame.paragraphs[0].font.bold = True
    hb.text_frame.paragraphs[0].font.color.rgb = _rgb(accent)
    cb = s.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(11.7), Inches(5))
    ctf = cb.text_frame; ctf.word_wrap = True
    for j, pt in enumerate(summary):
        para = ctf.paragraphs[0] if j == 0 else ctf.add_paragraph()
        para.text = "%d. %s" % (j + 1, pt)
        para.font.size = Pt(20); para.font.color.rgb = _rgb(accent); para.space_after = Pt(16)

    # 结束页
    s = prs.slides.add_slide(blank)
    _add_bg(s, prs, primary)
    eb = s.shapes.add_textbox(Inches(0), Inches(3.0), prs.slide_width, Inches(1.5))
    ep = eb.text_frame.paragraphs[0]; ep.text = "谢谢观看" if lang == "zh" else "Thank You"
    ep.font.size = Pt(48); ep.font.bold = True; ep.font.color.rgb = _rgb(accent); ep.alignment = PP_ALIGN.CENTER

    os.makedirs(OUT_DIR, exist_ok=True)
    safe = re.sub(r"[\\/:*?\"<>|]", "_", title_txt)[:60] or "presentation"
    out_path = os.path.join(OUT_DIR, "%s.pptx" % safe)
    # 避免覆盖：同名追加序号
    base, ext = os.path.splitext(out_path)
    n = 1
    while os.path.exists(out_path):
        out_path = "%s_%d%s" % (base, n, ext); n += 1
    prs.save(out_path)
    return out_path


def run(raw: str, model: str = "glm-4-flash") -> str:
    """对话技能入口：``/pptgen <主题> [--pages N] [--palette NAME] [--lang zh|en]``。"""
    if not raw or not raw.strip():
        return ("⚠️ 请提供 PPT 主题，例如：`/pptgen 企业知识库建设方案 --pages 10 --palette tech-dark`\n"
                "可用配色：" + "、".join(PALETTES.keys()))
    topic, pages, palette, lang = _parse_args(raw)
    outline = _build_outline_via_llm(topic, pages, lang, model)
    source = "🤖 大模型生成"
    if outline is None:
        outline = _fallback_outline(topic, pages, lang)
        source = "📋 默认模板（未配置大模型密钥或调用失败，已自动回退）"
    try:
        out_path = _render(topic, outline, palette, lang, model)
    except Exception as e:
        return "❌ 生成 PPT 失败：%s" % e

    rel = os.path.relpath(out_path, ROOT)
    pal_name = palette
    return (
        "✅ **PPT 已生成**（%s）\n\n"
        "📄 **文件：** `%s`\n"
        "🎨 **配色：** `%s`　📑 **页数：** %d　🌐 **语言：** %s\n\n"
        "可直接在文件管理器中打开，或在左侧「📁 知识管理」上传后在线预览。\n"
        "> 提示：本技能默认使用 Python(python-pptx) 渲染；如需更丰富的插画/风格模板，"
        "可改用本目录 `scripts/` 下的 Node(pptxgenjs) 版本，详见 `操作说明.md`。"
    ) % (source, rel, pal_name, len(outline.get("sections", [])) + 3, lang)
