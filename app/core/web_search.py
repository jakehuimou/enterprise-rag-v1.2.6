"""
联网检索层 - 互联网搜索（Web Search Fallback）
==============================================
当知识库与会话附件都检索不到与问题相关的内容时，调用智谱 AI 的联网搜索
（Web Search API）实时检索互联网资料，作为「外部补充上下文」交给大模型作答。

为什么放在这一层：
  - 与对话大模型同源（复用同一个 ZHIPU_API_KEY），无需再申请第三方搜索服务；
  - 返回结构统一归一化为 {title, link, content, publish_date, media, refer}，
    上层 RAGPipeline 直接包装成 Document 参与上下文拼装与来源标注。

对外暴露：
  - WEB_SEARCH_ENGINES / WEB_SEARCH_ENGINE_CHOICES：可用搜索引擎
  - WebSearchError：统一的联网检索失败异常（携带可读中文原因）
  - zhipu_web_search：执行一次联网搜索，返回归一化后的结果列表
"""
import re
from typing import Dict, List

import requests

# 部分搜索结果的标题末尾自带「（发布时间：2026-08-12 14:05:56）」，
# 与单独返回的 publish_date 重复，这里统一剥离，避免来源列表出现两遍时间。
_TITLE_TAIL_RE = re.compile(r"[\（\(]\s*发布时间[：:][^\）\)]*[\）\)]\s*$")

WEB_SEARCH_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/web_search"

# 可用搜索引擎（值与智谱官方 search_engine 取值一致）
WEB_SEARCH_ENGINES: List[str] = [
    "search_std",
    "search_pro",
    "search_pro_sogou",
    "search_pro_quark",
]
WEB_SEARCH_ENGINE_LABELS: Dict[str, str] = {
    "search_std": "标准版（免费额度多）",
    "search_pro": "高阶版（结果更全）",
    "search_pro_sogou": "搜狗",
    "search_pro_quark": "夸克",
}
# 供 Gradio 下拉框使用的 (显示名, 取值) 列表
WEB_SEARCH_ENGINE_CHOICES = [
    (f"{k} · {WEB_SEARCH_ENGINE_LABELS[k]}", k) for k in WEB_SEARCH_ENGINES
]

# 单条联网结果正文的截断长度：搜索接口返回的正文可能很长，
# 截断后再入上下文，避免少量结果就把上下文挤满。
DEFAULT_MAX_CHARS = 1200


class WebSearchError(RuntimeError):
    """联网检索失败（网络异常 / 鉴权失败 / 配额不足 / 接口报错）。"""


def _error_message(resp: requests.Response) -> str:
    """从智谱响应体中提取可读错误信息。"""
    try:
        data = resp.json()
        err = data.get("error") or {}
        msg = err.get("message")
        if msg:
            return msg
        return data.get("message") or resp.text
    except Exception:
        return resp.text


def zhipu_web_search(
    query: str,
    api_key: str,
    engine: str = "search_std",
    count: int = 5,
    content_size: str = "medium",
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: int = 30,
) -> List[Dict]:
    """
    调用智谱联网搜索接口检索互联网资料。

    :param query: 搜索词（一般直接使用用户问题）
    :param api_key: 智谱 API Key
    :param engine: 搜索引擎，见 ``WEB_SEARCH_ENGINES``
    :param count: 期望返回条数（1~50，实际由搜索引擎决定）
    :param content_size: 正文长度档位 medium / high
    :param max_chars: 单条正文的最大保留字符数
    :return: 归一化结果列表，每项含
             ``title`` / ``link`` / ``content`` / ``publish_date`` / ``media`` / ``refer``
    :raises WebSearchError: 未配置 Key、网络异常或接口返回非 200
    """
    if not api_key:
        raise WebSearchError("未配置 ZHIPU_API_KEY，无法联网搜索。")
    if not query or not query.strip():
        raise WebSearchError("搜索词为空，无法联网搜索。")
    if engine not in WEB_SEARCH_ENGINES:
        engine = "search_std"
    try:
        count = max(1, min(int(count or 5), 50))
    except Exception:
        count = 5

    payload = {
        "search_engine": engine,
        "search_query": query.strip(),
        "count": count,
        "content_size": content_size,
    }
    try:
        resp = requests.post(
            WEB_SEARCH_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except Exception as e:
        raise WebSearchError(f"联网搜索请求失败：{str(e)[:200]}")

    if resp.status_code != 200:
        raise WebSearchError(f"联网搜索接口错误 [{resp.status_code}]：{_error_message(resp)[:200]}")

    try:
        data = resp.json()
    except Exception:
        raise WebSearchError("联网搜索返回内容无法解析。")

    results: List[Dict] = []
    for item in (data.get("search_result") or []):
        content = (item.get("content") or "").strip()
        if not content:
            continue
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "…"
        link = (item.get("link") or "").strip()
        title = _TITLE_TAIL_RE.sub("", (item.get("title") or "").strip()).strip()
        results.append({
            "title": title or "网络资料",
            "link": link,
            "content": content,
            "publish_date": (item.get("publish_date") or "").strip(),
            "media": (item.get("media") or "").strip(),
            "refer": (item.get("refer") or "").strip(),
        })
    return results
