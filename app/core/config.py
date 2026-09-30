"""
配置中心
========
集中管理 RAG 应用的所有可调参数：
  - 智谱 AI 密钥与模型
  - 向量模型服务商（智谱 AI / 阿里云百炼）与百炼 API 配置
  - 文档分块参数
  - 检索 / 推理参数
  - 服务端口

加载优先级：
  1. 代码内置默认值
  2. .env 环境变量
  3. data/config/runtime.json（运行时通过 Web 界面调整的参数，优先级最高）
"""
import os
import json
from dataclasses import dataclass, field, asdict
from typing import List

# 项目根目录（本文件位于 app/core/ 下，根目录为向上三级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
VECTORSTORE_DIR = os.path.join(DATA_DIR, "vectorstore")
REGISTRY_DIR = os.path.join(DATA_DIR, "registry")
RUNTIME_CONFIG_PATH = os.path.join(DATA_DIR, "config", "runtime.json")

# 默认分隔符优先级（递归字符拆分使用）
DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "，", "、", " ", ""]


@dataclass
class AppConfig:
    """应用配置数据类，所有字段均可被运行时覆盖并持久化。"""
    zhipu_api_key: str = ""
    embedding_model: str = "embedding-2"
    chat_model: str = "glm-4-flash"
    vision_model: str = "glm-4v-flash"  # 用于把图片解析为文字描述的视觉大模型

    # 向量模型服务商：zhipu（智谱 AI）| dashscope（阿里云百炼）
    embedding_provider: str = "zhipu"
    embedding_dim: int = 0  # 向量维度，0 表示使用模型默认维度
    # 阿里云百炼（DashScope）向量/语音接口配置
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"
    # 语音模型（语音识别 / 语音合成）
    voice_model: str = "qwen-tts-latest"
    # 语音合成（TTS）：模型与默认音色
    tts_model: str = "qwen-tts-latest"
    tts_voice: str = "Cherry"

    # 分块
    chunk_size: int = 500
    chunk_overlap: int = 80
    separators: List[str] = field(default_factory=lambda: list(DEFAULT_SEPARATORS))
    splitter_type: str = "recursive"  # recursive | markdown | token

    # 检索 / 推理
    top_k: int = 4
    temperature: float = 0.3
    top_p: float = 0.9

    # 联网搜索兜底：知识库/会话附件检索不到相关内容时，自动联网检索补充
    web_search_enabled: bool = True
    web_search_engine: str = "search_std"   # search_std / search_pro / search_pro_sogou / search_pro_quark
    web_search_count: int = 5               # 每次联网检索的结果条数

    # 多轮上下文机制
    # 追问改写：把「它有什么风险」「那成本呢」这类依赖上文的追问，结合历史改写成
    # 可独立检索的完整问题后再去检索（只影响检索，不影响回答措辞）。
    # 关闭后检索将直接使用用户原句。
    context_rewrite_enabled: bool = True
    # 送入模型的历史轮数上限（从最近往前保留）
    context_max_turns: int = 8
    # 送入模型的历史总字符预算：超长回答会先做头尾截断，再按预算累加
    context_char_budget: int = 6000

    # 服务端口
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    gradio_host: str = "127.0.0.1"
    gradio_port: int = 7860

    @property
    def api_base_url(self) -> str:
        """Gradio 前端调用 FastAPI 后端的基础地址。"""
        return f"http://{self.api_host}:{self.api_port}"


def load_config() -> AppConfig:
    """按 默认值 -> .env -> runtime.json 的顺序加载配置。"""
    # 加载 .env（若存在）
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE_DIR, ".env"))
    except Exception:
        pass

    cfg = AppConfig()
    cfg.zhipu_api_key = os.getenv("ZHIPU_API_KEY", cfg.zhipu_api_key)
    cfg.embedding_model = os.getenv("EMBEDDING_MODEL", cfg.embedding_model)
    cfg.chat_model = os.getenv("CHAT_MODEL", cfg.chat_model)
    cfg.vision_model = os.getenv("VISION_MODEL", cfg.vision_model)
    cfg.embedding_provider = os.getenv("EMBEDDING_PROVIDER", cfg.embedding_provider)
    cfg.embedding_dim = int(os.getenv("EMBEDDING_DIM", cfg.embedding_dim) or 0)
    cfg.dashscope_api_key = os.getenv("DASHSCOPE_API_KEY", cfg.dashscope_api_key)
    cfg.dashscope_base_url = os.getenv("DASHSCOPE_BASE_URL", cfg.dashscope_base_url)
    cfg.tts_model = os.getenv("TTS_MODEL", cfg.tts_model)
    cfg.tts_voice = os.getenv("TTS_VOICE", cfg.tts_voice)
    # 语音识别/合成模型（dataclass 里本就有该字段，此前漏了 .env 读取，补齐）
    cfg.voice_model = os.getenv("VOICE_MODEL", cfg.voice_model)
    cfg.chunk_size = int(os.getenv("CHUNK_SIZE", cfg.chunk_size))
    cfg.chunk_overlap = int(os.getenv("CHUNK_OVERLAP", cfg.chunk_overlap))

    sep = os.getenv("SEPARATORS")
    if sep:
        cfg.separators = [s for s in sep.split(",") if s != ""] or list(DEFAULT_SEPARATORS)

    cfg.splitter_type = os.getenv("SPLITTER_TYPE", cfg.splitter_type)
    cfg.top_k = int(os.getenv("TOP_K", cfg.top_k))
    cfg.temperature = float(os.getenv("TEMPERATURE", cfg.temperature))
    cfg.top_p = float(os.getenv("TOP_P", cfg.top_p))
    # 联网搜索兜底：WEB_SEARCH_ENABLED=0/false 可关闭
    _wse = os.getenv("WEB_SEARCH_ENABLED")
    if _wse is not None:
        cfg.web_search_enabled = str(_wse).strip().lower() not in ("0", "false", "no", "off", "")
    cfg.web_search_engine = os.getenv("WEB_SEARCH_ENGINE", cfg.web_search_engine)
    cfg.web_search_count = int(os.getenv("WEB_SEARCH_COUNT", cfg.web_search_count))
    # 多轮上下文：CONTEXT_REWRITE_ENABLED=0/false 可关闭追问改写
    _cre = os.getenv("CONTEXT_REWRITE_ENABLED")
    if _cre is not None:
        cfg.context_rewrite_enabled = str(_cre).strip().lower() not in ("0", "false", "no", "off", "")
    cfg.context_max_turns = int(os.getenv("CONTEXT_MAX_TURNS", cfg.context_max_turns))
    cfg.context_char_budget = int(os.getenv("CONTEXT_CHAR_BUDGET", cfg.context_char_budget))
    cfg.api_host = os.getenv("API_HOST", cfg.api_host)
    cfg.api_port = int(os.getenv("API_PORT", cfg.api_port))
    cfg.gradio_host = os.getenv("GRADIO_HOST", cfg.gradio_host)
    cfg.gradio_port = int(os.getenv("GRADIO_PORT", cfg.gradio_port))

    # 运行时覆盖（Web 界面调整后写入的 runtime.json 优先级最高）
    if os.path.exists(RUNTIME_CONFIG_PATH):
        try:
            with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                overrides = json.load(f)
            for k, v in overrides.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception:
            pass

    return cfg


# 允许在 Web 界面通过 API 动态调整的参数白名单
TUNABLE_KEYS = [
    "embedding_model", "chat_model", "vision_model", "voice_model",
    "tts_model", "tts_voice", "chunk_size", "chunk_overlap",
    "separators", "splitter_type", "top_k", "temperature", "top_p",
    "embedding_provider", "embedding_dim", "dashscope_api_key", "dashscope_base_url",
    "web_search_enabled", "web_search_engine", "web_search_count",
]


def save_runtime_config(cfg: AppConfig):
    """将可调参数持久化到 runtime.json，供下次启动加载。"""
    os.makedirs(os.path.dirname(RUNTIME_CONFIG_PATH), exist_ok=True)
    data = {k: getattr(cfg, k) for k in TUNABLE_KEYS}
    with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def config_to_dict(cfg: AppConfig) -> dict:
    """将配置转为可序列化的字典（供 API 返回）。"""
    return json.loads(json.dumps(asdict(cfg), ensure_ascii=False, default=str))
