"""
检索推理层 - 大模型问答（LLM Inference）
=======================================
封装智谱 AI 的对话大模型（默认 glm-4-flash），通过 requests 调用
OpenAI 兼容的 chat/completions 接口，生成最终答案。

本模块只负责「把拼装好的 messages 送给大模型并返回文本」，
检索上下文的拼装与来源引用由 rag.py 统一编排。

额外提供：
  - ``image_data_uri``：将本地图片读为 base64 data URL，供视觉模型识别。
  - ``zhipu_vision_caption``：调用视觉模型（glm-4v 系列）把图片转成文字描述，
    使图片也能进入「解析 -> 分块 -> 向量化 -> 检索 -> 问答」链路。
"""
import os
import time
import json
import base64
import requests

CHAT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 支持“看图作答”的视觉大模型集合（仅保留 glm-4v-flash 与新一代 glm-4.6v）
VISION_MODEL_SET = {
    "glm-4v-flash", "glm-4.6v",
}
# 各视觉模型的 max_tokens 输出上限（超出会触发 HTTP 400）。
# glm-4v-flash 限制为 1024；glm-4.6v 旗舰版支持到 32768。
VISION_MAX_OUTPUT = {
    "glm-4v-flash": 1024,
    "glm-4.6v": 32768,
}

# 图片扩展名 -> MIME 类型
_IMAGE_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "bmp": "image/bmp", "gif": "image/gif", "webp": "image/webp",
}


def _zhipu_error_message(resp: requests.Response) -> str:
    """从智谱 API 响应体中提取中文错误信息，失败则返回原始文本。"""
    try:
        data = resp.json()
        err = data.get("error", {})
        return err.get("message") or resp.text
    except Exception:
        return resp.text


def _post_with_retry(
    endpoint: str,
    headers: dict,
    payload: dict,
    max_retries: int = 3,
    timeout: int = 240,
) -> requests.Response:
    """
    向智谱 API 发 POST，对以下两类瞬时故障做指数退避重试：

    - ``429``：免费模型共享配额限流（如 glm-4v-flash）；
    - 网络层超时 / 连接中断（``ReadTimeout`` / ``ConnectTimeout`` / ``ConnectionError``）：
      典型表现为 ``Read timed out. (read timeout=120)``，多为链路抖动或上游瞬时拥塞，
      重试通常可恢复，避免单段翻译卡死导致整篇文档失败。

    重试结束后仍失败时，抛出包含智谱中文错误信息的 HTTPError（或原网络异常）。
    """
    # 连接建立超时设小一些，避免 DNS / 握手阶段长时间挂死；读取仍给足 240s 余量。
    req_timeout = timeout if isinstance(timeout, (tuple, list)) else (30, timeout)
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=req_timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # 瞬断 / 超时：退避后重试（退避上限比 429 略大，因网络恢复更慢）
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            retry_hint = f"网络请求已重试{max_retries}次仍失败"
            raise requests.exceptions.ConnectionError(f"{retry_hint}：{exc}") from exc
        if resp.ok:
            return resp
        # 429：免费模型共享配额限流（如 glm-4v-flash），做有限次退避重试
        if resp.status_code == 429 and attempt < max_retries:
            sleep_time = min(2 ** attempt, 4)
            time.sleep(sleep_time)
            continue
        retry_hint = f"，已重试{attempt}次" if attempt > 0 else ""
        raise requests.exceptions.HTTPError(
            f"智谱 API 错误 [{resp.status_code}{retry_hint}]: {_zhipu_error_message(resp)}",
            response=resp,
        )


def image_data_uri(image_path: str) -> str:
    """将本地图片文件读取为 base64 data URL（含 MIME 前缀），供视觉模型接口使用。"""
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"图片不存在：{image_path}")
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime = _IMAGE_MIME.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def zhipu_vision_caption(
    image_path: str,
    api_key: str,
    model: str = "glm-4v-flash",
    prompt: str = None,
) -> str:
    """
    调用智谱视觉大模型（默认 glm-4v-flash）对单张图片生成文字描述。

    用于将图片（截图、扫描件、图表、带文字的照片等）转化为可检索、可问答的
    文本内容。返回模型给出的描述文字。
    """
    data_url = image_data_uri(image_path)
    prompt = prompt or (
        "请详细、完整地描述这张图片的全部可见内容，包括其中的文字、表格、图表、"
        "数据、版面结构等，尽量保留原始表述，便于后续检索与问答。"
        "只输出描述内容本身，不要评论或解释。"
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "top_p": 0.8,
        # 视觉模型对 max_tokens 上限更严格（glm-4v 系列 1024、flash 系列 4096），按模型取上限避免 400
        "max_tokens": min(2048, VISION_MAX_OUTPUT.get(model, 1024)),
    }
    resp = _post_with_retry(
        CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
    )
    return resp.json()["choices"][0]["message"]["content"]


def zhipu_chat(
    messages: list,
    api_key: str,
    model: str = "glm-4-flash",
    temperature: float = 0.3,
    top_p: float = 0.9,
    max_tokens: int = 2048,
) -> str:
    """
    调用智谱对话接口生成回答。

    :param messages: OpenAI 格式的消息列表（system/user/assistant）
    :return: 模型生成的文本内容
    """
    # 视觉模型对 max_tokens 有各自的上限（glm-4v 系列 1024，glm-4.6v 系列 32768），超出会返回 400
    if model in VISION_MAX_OUTPUT:
        max_tokens = min(max_tokens, VISION_MAX_OUTPUT[model])
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    resp = _post_with_retry(
        CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
    )
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def zhipu_chat_stream(
    messages: list,
    api_key: str,
    model: str = "glm-4-flash",
    temperature: float = 0.3,
    top_p: float = 0.9,
    max_tokens: int = 2048,
):
    """
    流式版 ``zhipu_chat``：以生成器方式逐块产出模型回答文本（token 流）。

    通过 ``stream=True`` 调用智谱 OpenAI 兼容接口，按 SSE 逐行解析 ``data:``
    增量片段并 ``yield``。对免费模型的 429 限流在建立连接阶段做有限次退避重试。
    调用方（如 rag.answer_stream）可借此实现打字机式流式输出。
    """
    if model in VISION_MAX_OUTPUT:
        max_tokens = min(max_tokens, VISION_MAX_OUTPUT[model])
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 建立连接阶段的 429 限流退避重试（流式开始后不再重试，避免中断输出）
    for attempt in range(3):
        resp = requests.post(
            CHAT_ENDPOINT, headers=headers, json=payload, stream=True, timeout=(10, 180)
        )
        if resp.ok:
            break
        if resp.status_code == 429 and attempt < 2:
            time.sleep(min(2 ** attempt, 4))
            continue
        raise requests.exceptions.HTTPError(
            f"智谱 API 错误 [{resp.status_code}]: {_zhipu_error_message(resp)}",
            response=resp,
        )

    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except Exception:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        piece = delta.get("content")
        if piece:
            yield piece
    resp.close()
