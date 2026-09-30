"""请求 / 响应数据模型（Pydantic Schema）。"""
from typing import List, Optional
from pydantic import BaseModel


class ChatRequest(BaseModel):
    """RAG 问答请求。"""
    question: str
    history: Optional[List[dict]] = None  # 形如 [{"user":..., "assistant":...}]
    model: Optional[str] = None           # 对话模型覆盖（不传则用配置默认 chat_model）
    session_id: Optional[str] = None      # 会话附件标识（上传附件后回传，用于检索本会话附件）
    web_search: Optional[bool] = None     # 联网搜索开关：None=跟随全局配置；True=强制联网；False=关闭


class ChatResponse(BaseModel):
    """RAG 问答响应。"""
    answer: str
    sources: List[dict]  # 召回片段的元数据列表（含 scope 字段：附件 / 知识库）


class EnhanceRequest(BaseModel):
    """提示词增强请求。"""
    text: str                 # 待增强的原始输入
    style: Optional[str] = "default"  # 增强风格：default/rigorous/concise/step_by_step/extract
    model: Optional[str] = None       # 增强所用对话模型（不传则用配置默认）


class TranslateRequest(BaseModel):
    """翻译请求。"""
    text: str                          # 待翻译原文
    source_lang: Optional[str] = "自动检测"  # 源语言（默认自动检测）
    target_lang: Optional[str] = "英文"      # 目标语言
    model: Optional[str] = None        # 翻译所用对话模型（不传则用配置默认）


class TTSRequest(BaseModel):
    """文字转语音（语音合成）请求。"""
    text: str                              # 待合成文本
    voice: Optional[str] = None            # 音色（如 Cherry/Serena/Ethan/Chelsie）
    model: Optional[str] = None            # 语音合成模型（不传则用配置 tts_model）
    language_type: Optional[str] = "Auto"  # 语种：Auto/Chinese/English/... 建议与文本一致


class TranslateResponse(BaseModel):
    """翻译响应。"""
    translated: str                    # 译文
    target_lang: str                   # 实际目标语言


class UploadResponse(BaseModel):
    """文件上传入库响应。"""
    file_name: str
    path: str
    chunks: int
    pages: int


class ConfigUpdate(BaseModel):
    """运行时配置更新（仅传需要修改的字段）。"""
    embedding_model: Optional[str] = None
    chat_model: Optional[str] = None
    vision_model: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    separators: Optional[List[str]] = None
    splitter_type: Optional[str] = None
    top_k: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    # 向量模型服务商与百炼 API 配置
    embedding_provider: Optional[str] = None   # zhipu | dashscope
    embedding_dim: Optional[int] = None        # 0 = 模型默认维度
    dashscope_api_key: Optional[str] = None
    dashscope_base_url: Optional[str] = None
    # 语音模型
    voice_model: Optional[str] = None
    # 语音合成（TTS）模型与默认音色
    tts_model: Optional[str] = None
    tts_voice: Optional[str] = None
    # 联网搜索兜底（知识库检索不到时自动联网检索）
    web_search_enabled: Optional[bool] = None
    web_search_engine: Optional[str] = None
    web_search_count: Optional[int] = None
