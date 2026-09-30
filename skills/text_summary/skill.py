"""外部技能：智能文档摘要（示例）

演示「外部技能接入」：本文件位于独立目录 ``skills/text_summary/``，由技能注册中心
在启动时自动发现；在对话中输入 ``/summary <文本>`` 即可触发 ``run()``。

如需新增自己的技能，最快捷的方式是在「🧩 技能专家」面板点「➕ 新增技能」，
或复制本目录并修改 skill.yaml / skill.py。
"""

from web.skill_sdk import call_llm


def run(raw: str, model: str = "glm-4-flash") -> str:
    """对输入文本做要点摘要，返回 Markdown。"""
    if not raw or not raw.strip():
        return "⚠️ 请提供需要摘要的内容，例如：/summary 这里粘贴一段长文本"
    prompt = (
        "请将下面的文本摘要为 3-5 条核心要点，使用 Markdown 无序列表，"
        "每条要点简明扼要并保留关键数据：\n\n" + raw
    )
    try:
        result = call_llm(prompt, model)
    except Exception as e:
        return "⚠️ 摘要生成失败：%s" % e
    return "📝 **智能文档摘要**\n\n" + result
