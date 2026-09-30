"""
诊断脚本：检查本账号对智谱 AI 各模型的可用情况。
================================================
用于排查“上传报 400/401/429”等问题：
  - 是否配置了 API Key
  - embedding-2 / embedding-3 向量模型是否可用
  - 对话模型（默认 glm-4-flash）是否可用

用法（需先填好项目根目录 .env 的 ZHIPU_API_KEY）：
    python scripts/diagnose.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except Exception:
    pass

import requests


def _probe_embedding(key: str, model: str) -> str:
    url = "https://open.bigmodel.cn/api/paas/v4/embeddings"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "input": ["诊断测试"]},
            timeout=30,
        )
        if r.status_code == 200:
            return "✅ 可用"
        return f"❌ HTTP {r.status_code}：{r.text[:200]}"
    except Exception as e:
        return f"❌ 请求异常：{e}"


def _probe_chat(key: str, model: str) -> str:
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}]},
            timeout=30,
        )
        if r.status_code == 200:
            return "✅ 可用"
        return f"❌ HTTP {r.status_code}：{r.text[:200]}"
    except Exception as e:
        return f"❌ 请求异常：{e}"


def main():
    key = os.getenv("ZHIPU_API_KEY")
    print("=" * 56)
    print("智谱 AI 账号诊断")
    print("=" * 56)
    if not key:
        print("❌ 未检测到 ZHIPU_API_KEY：请确认项目根目录 .env 已配置。")
        return
    print(f"✅ 已检测到 API Key（长度 {len(key)}）\n")

    print("【向量化模型】")
    for m in ["embedding-2", "embedding-3"]:
        print(f"  {m:<12}: {_probe_embedding(key, m)}")

    print("\n【对话模型】")
    chat_model = os.getenv("CHAT_MODEL", "glm-4-flash")
    print(f"  {chat_model:<12}: {_probe_chat(key, chat_model)}")

    print("\n说明：")
    print("  - 若向量模型返回 400/404，多为该模型未在本账号开通；")
    print("    应用会自动回退到另一可用模型（embedding-2 ↔ embedding-3）。")
    print("  - 若返回 401，请检查 API Key 是否正确或已失效。")
    print("  - 若返回 429，为免费额度频率限制，请等待重置或减少调用频率。")


if __name__ == "__main__":
    main()
