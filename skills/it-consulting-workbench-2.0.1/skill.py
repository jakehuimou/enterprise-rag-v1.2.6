"""外部技能 it-consulting-workbench（导入时自动生成的默认执行器）

本技能包未自带可运行逻辑（无 skill.py / handler / prompt_template），
导入流程已自动生成此默认执行器：把技能目录下的 SKILL.md（去除 frontmatter）
作为 system 提示词，将对话输入交给大模型按技能说明作答。

如需接入真实工具 / API，请用更完整的 skill.py 覆盖本文件，实现 run(raw, model)。
"""

import os

from web.skill_sdk import call_llm


def _strip_frontmatter(text):
    """去除 Markdown 文件开头的 YAML frontmatter（--- ... ---）。"""
    if not text.startswith("---"):
        return text
    nl = chr(10)
    idx = text.find(nl + "---", 3)
    if idx == -1:
        return text
    end = text.find(nl, idx + 1)
    if end == -1:
        return text
    return text[end + 1:]


def _load_instructions():
    """优先读取 SKILL.md 正文；退而求其次读取 skill.yaml 的 description。"""
    here = os.path.dirname(os.path.abspath(__file__))
    md = os.path.join(here, "SKILL.md")
    if os.path.exists(md):
        try:
            with open(md, "r", encoding="utf-8") as f:
                return _strip_frontmatter(f.read()).strip()
        except Exception:
            pass
    yaml_path = os.path.join(here, "skill.yaml")
    if os.path.exists(yaml_path):
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return str(data.get("description", "")).strip()
        except Exception:
            pass
    return ""


def run(raw: str, model: str = "glm-4-flash") -> str:
    """在对话中输入 /it_consulting_workbench_2_0_1 <内容> 即可触发本函数。"""
    if not raw or not raw.strip():
        return "⚠️ 请输入内容，例如：/it_consulting_workbench_2_0_1 你的需求"
    instructions = _load_instructions()
    system = (
        "你是一个技能执行助手。请严格依据下面的「技能说明」来回答用户问题；"
        "若说明要求调用特定工具、API 或命令，请在回答中给出明确、可执行的步骤与示例，"
        "并提示用户可能需要的依赖安装步骤。"
        "\n\n=== 技能说明 ===\n"
        + (instructions or "（无额外说明）")
    )
    try:
        return call_llm(raw, model, system=system)
    except Exception as e:
        return (
            "⚠️ 技能「it-consulting-workbench」执行失败：%s\n\n"
            "（该技能为导入时自动生成的默认执行器；若需真实工具 / API 调用，"
            "请补充 skills/it_consulting_workbench_2_0_1/skill.py 实现 run(raw, model)。）"
        ) % str(e)
