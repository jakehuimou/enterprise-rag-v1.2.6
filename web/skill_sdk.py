"""外部技能开发 SDK
================

本模块供 ``skills/<skill_id>/skill.py`` 中的外部技能调用，封装了「调用大模型」
与「读取配置」两类最常用的能力，使外部技能无需 import 庞大的 ``gradio_app``，
从而避免循环依赖。

外部技能文件示例（``skills/demo/skill.py``）::

    from web.skill_sdk import call_llm, cfg

    def run(raw: str, model: str = "glm-4-flash") -> str:
        \"\"\"返回一段 Markdown 文本作为对话回答。\"\"\"
        if not raw.strip():
            return "⚠️ 请提供输入内容。"
        return call_llm("请把以下内容摘要成 3 条要点：\\n" + raw, model)

注意：外部技能模块通过 ``importlib`` 以独立模块名加载，``web`` 包已在运行时
路径中，因此使用 ``from web.skill_sdk import ...`` 即可。
"""

import os

from app.core.config import load_config

# 全局配置对象（与 gradio_app 共用同一份 .env 配置）
cfg = load_config()

try:
    import requests as _requests
    _REQ_OK = True
except Exception:  # pragma: no cover
    _requests = None
    _REQ_OK = False


def call_llm(prompt, model=None, temperature=0.3, system=None):
    """单次对话补全，返回内容字符串。

    未配置密钥或网络异常时抛出 ``RuntimeError``，由注册中心统一捕获并回显给用户。

    :param prompt: 用户侧提示词（作为 user 消息）
    :param model: 模型名，默认 glm-4-flash
    :param temperature: 采样温度
    :param system: 可选 system 提示词
    """
    if not _REQ_OK:
        raise RuntimeError("运行环境缺少 requests 依赖，无法调用大模型。")
    api_key = getattr(cfg, "zhipu_api_key", None)
    if not api_key:
        raise RuntimeError("未配置 ZHIPU_API_KEY，无法调用大模型。请在「模型设置」中配置密钥。")
    model = model or "glm-4-flash"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    r = _requests.post(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=120,
    )
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        raise RuntimeError("HTTP %s：%s" % (r.status_code, detail))
    return r.json()["choices"][0]["message"]["content"]


def list_files(dir_path):
    """列出目录下的文件（外部技能如需读取自带资源时可用）。"""
    if not os.path.isdir(dir_path):
        return []
    return [os.path.join(dir_path, f) for f in sorted(os.listdir(dir_path))]
