"""
向量处理层 - Embedding（文本向量化）
====================================
封装两类向量化服务，实现 LangChain 的 ``Embeddings`` 接口，可直接用于 FAISS 等向量数据库：

1. **智谱 AI**（``ZhipuEmbeddings``）：``embedding-2`` / ``embedding-3``，
   走 OpenAI 兼容接口 ``https://open.bigmodel.cn/api/paas/v4/embeddings``。
   接口文档：https://open.bigmodel.cn/dev/api/vector/embedding
2. **阿里云百炼 DashScope**（``DashScopeEmbeddings``）：
   - 文本向量模型（``qwen3.7-text-embedding`` 等）：走 OpenAI 兼容模式
     ``{base}/compatible-mode/v1/embeddings``（请求体 ``{"model","input":[...],"dimensions":N}``）；
   - 多模态向量模型（``qwen3-vl-embedding``）：**OpenAI 兼容模式不支持**，
     需走原生接口 ``{base}/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding``，
     请求体为 ``{"model","input":{"contents":[{"text":...}]},"parameters":{...}}``。
   接口文档：https://help.aliyun.com/zh/model-studio/embedding

两个类对外接口一致（``embed_documents`` / ``embed_query`` / ``active_model`` / ``dim``），
由 ``create_embeddings(cfg)`` 按当前配置统一构建，上层 RAGPipeline 无需感知差异。

设计要点：
  1. 智谱侧当主模型调用失败（如账号未开通该模型，返回 400/404）时自动回退到备选模型，
     避免“上传即报错”阻断整个 RAG 入库流程。
  2. 任何非 2xx 响应都会把服务端返回的完整错误体带出来，便于定位根因。
  3. 不同模型的向量维度不同，**不能混用同一个向量库**，因此两类实现都通过 ``dim``
     属性显式暴露维度，供向量库在切换模型时判断是否需要重建。
"""
import requests
from langchain_core.embeddings import Embeddings

# ---------------------------------------------------------------------------
# 智谱 AI
# ---------------------------------------------------------------------------
EMBEDDING_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/embeddings"
# 单次请求最大批量条数（智谱限制），超出分批处理
BATCH_SIZE = 50
# 智谱当前可用的向量化模型清单（用于失败回退）
AVAILABLE_MODELS = ["embedding-2", "embedding-3"]
# 智谱已知模型的向量维度（embedding-2 与 embedding-3 维度不同，不能混用同一索引）
ZHIPU_MODEL_DIMS = {"embedding-2": 1024, "embedding-3": 2048}

# ---------------------------------------------------------------------------
# 阿里云百炼 DashScope
# ---------------------------------------------------------------------------
DASHSCOPE_DEFAULT_BASE = "https://dashscope.aliyuncs.com"
# OpenAI 兼容模式下的向量接口路径（文本模型）
DASHSCOPE_COMPAT_EMBED_PATH = "/compatible-mode/v1/embeddings"
# 原生多模态向量接口路径（qwen3-vl-embedding，兼容模式不支持）
DASHSCOPE_NATIVE_EMBED_PATH = (
    "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
)
# 百炼向量模型规格：可选维度 / 默认维度 / 是否为多模态模型
DASHSCOPE_MODEL_SPECS = {
    "qwen3.7-text-embedding": {
        "dims": [2560, 2048, 1536, 1024, 768, 512, 256],
        "default": 1024, "multimodal": False,
    },
    "qwen3.7-text-embedding-flash": {
        "dims": [1024, 768, 512, 256], "default": 1024, "multimodal": False,
    },
    "text-embedding-v4": {
        "dims": [2048, 1536, 1024, 768, 512, 256, 128, 64],
        "default": 1024, "multimodal": False,
    },
    "qwen3-vl-embedding": {
        "dims": [2560, 2048, 1536, 1024, 768, 512, 256],
        "default": 2560, "multimodal": True,
    },
}
# 百炼单次请求最大批量条数（内容元素总数不超过 20）
DASHSCOPE_BATCH_SIZE = 20


def is_dashscope_model(model: str) -> bool:
    """按模型名判断是否属于阿里云百炼（DashScope）向量模型。"""
    m = (model or "").strip().lower()
    if not m:
        return False
    if m in DASHSCOPE_MODEL_SPECS:
        return True
    return m.startswith(("qwen", "text-embedding-v", "tongyi-embedding"))


def dashscope_model_dims(model: str) -> list:
    """返回指定百炼模型支持的维度列表（未知模型返回常用维度集合）。"""
    spec = DASHSCOPE_MODEL_SPECS.get((model or "").strip().lower())
    if spec:
        return list(spec["dims"])
    return [2560, 2048, 1536, 1024, 768, 512, 256]


def dashscope_default_dim(model: str):
    """返回指定百炼模型的默认维度（未知模型返回 None，表示由服务端决定）。"""
    spec = DASHSCOPE_MODEL_SPECS.get((model or "").strip().lower())
    return spec["default"] if spec else None


class EmbeddingModelError(Exception):
    """智谱 Embedding 接口返回的错误（携带 HTTP 状态码与响应体）。"""

    def __init__(self, model: str, status_code: int, body: str):
        self.model = model
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"智谱 Embedding 模型「{model}」调用失败 (HTTP {status_code})：{body[:400]}"
        )


class DashScopeEmbeddingError(Exception):
    """阿里云百炼向量接口返回的错误（携带 HTTP 状态码与响应体）。"""

    def __init__(self, model: str, status_code: int, body: str):
        self.model = model
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"阿里云百炼向量模型「{model}」调用失败 (HTTP {status_code})：{str(body)[:400]}"
        )


class ZhipuEmbeddings(Embeddings):
    """智谱 AI 文本向量化封装，兼容 LangChain Embeddings 接口。

    支持模型自动回退：构造时传入 ``model`` 为主模型，调用失败（400/404）
    时会依次尝试 ``fallback_models`` 中的备选用模型。
    """

    def __init__(self, api_key: str, model: str = "embedding-2", fallback_models: list = None):
        self.api_key = api_key
        self.model = model
        # 回退列表：去掉主模型后保留的其余可用模型
        if fallback_models is not None:
            self.fallback_models = list(fallback_models)
        else:
            self.fallback_models = [m for m in AVAILABLE_MODELS if m != model]
        # 实际生效模型（首次成功调用后记录，供检索时保持一致）
        self._active_model: str = model

    @property
    def active_model(self) -> str:
        """当前实际生效的向量化模型名称。"""
        return self._active_model

    @property
    def dim(self):
        """当前生效模型的向量维度（未知模型返回 None，由调用方探测）。"""
        return ZHIPU_MODEL_DIMS.get(self._active_model or self.model)

    def _embed_once(self, texts: list, model: str) -> list:
        """用指定模型完成一次批量向量化。非 2xx 抛 ``EmbeddingModelError``。"""
        if not self.api_key:
            raise ValueError(
                "未配置智谱 AI API Key。请在项目根目录的 .env 文件中设置 ZHIPU_API_KEY，"
                "或设置对应的环境变量，并重启服务。"
            )
        vectors = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            resp = requests.post(
                EMBEDDING_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "input": batch},
                timeout=60,
            )
            if resp.status_code >= 400:
                raise EmbeddingModelError(model, resp.status_code, resp.text)
            data = resp.json().get("data", [])
            data.sort(key=lambda x: x.get("index", 0))
            vectors.extend([item["embedding"] for item in data])
        return vectors

    def _embed(self, texts: list) -> list:
        """带自动回退的向量化：主模型失败则尝试备用模型。

        关键约束：embedding-2（1024 维）与 embedding-3（2048 维）维度不同，
        不能混用在同一个向量库里，否则 FAISS 检索会因维度不匹配而失败。
        因此一旦某个模型在本会话中成功过，就将其“锁定”为 _active_model，
        后续调用优先复用它，确保「建库」与「查询」始终使用同一维度模型。
        """
        # 优先使用本会话已验证可用的模型，避免维度在多次调用间来回切换
        models_to_try = []
        if getattr(self, "_active_model", None):
            models_to_try.append(self._active_model)
        if self.model not in models_to_try:
            models_to_try.append(self.model)
        for m in self.fallback_models:
            if m not in models_to_try:
                models_to_try.append(m)

        last_err: Exception = None
        for model in models_to_try:
            try:
                vectors = self._embed_once(texts, model)
                self._active_model = model
                return vectors
            except EmbeddingModelError as e:
                last_err = e
                # 仅当模型本身不可用（400/404）才回退；认证/限流等不回退
                if e.status_code in (400, 404):
                    continue
                raise
            except requests.RequestException as e:
                last_err = e
                continue
        # 全部模型均失败
        raise RuntimeError(
            f"所有向量化模型均调用失败（依次尝试：{models_to_try}）。"
            f"最后一次错误：{last_err}"
        )

    def embed_documents(self, texts: list) -> list:
        """批量向量化文档片段（供向量库写入）。"""
        return self._embed(texts)

    def embed_query(self, text: str) -> list:
        """向量化单条查询（供检索使用）。"""
        return self._embed([text])[0]


def _extract_vectors(payload: dict) -> list:
    """从百炼返回体中提取向量列表（兼容 OpenAI 兼容模式与原生两种结构）。

    - OpenAI 兼容模式：``{"data": [{"index":0, "embedding":[...]}, ...]}``
    - 原生文本接口：``{"output": {"embeddings": [{"text_index":0, "embedding":[...]}]}}``
    - 原生多模态接口：``{"output": {"embeddings": [{"index":0, "embedding":[...]}]}}``
    """
    # 1) OpenAI 兼容模式
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict) and "embedding" in data[0]:
        items = sorted(data, key=lambda x: x.get("index", x.get("text_index", 0)))
        return [it["embedding"] for it in items]

    out = payload.get("output")
    if not isinstance(out, dict):
        return []
    embs = out.get("embeddings")
    # 2) output.embeddings = [{"embedding": [...]}, ...]
    if isinstance(embs, list) and embs:
        if isinstance(embs[0], dict) and "embedding" in embs[0]:
            items = sorted(embs, key=lambda x: x.get("index", x.get("text_index", 0)))
            return [it["embedding"] for it in items]
        # 3) output.embeddings 直接就是向量列表
        if isinstance(embs[0], list):
            return embs
    # 4) output.embeddings = {"text_embedding": [...]} 之类的嵌套结构
    if isinstance(embs, dict):
        for v in embs.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "embedding" in v[0]:
                items = sorted(v, key=lambda x: x.get("index", x.get("text_index", 0)))
                return [it["embedding"] for it in items]
    return []


class DashScopeEmbeddings(Embeddings):
    """阿里云百炼（DashScope）向量化封装，兼容 LangChain Embeddings 接口。

    - 文本模型（``qwen3.7-text-embedding`` 等）：OpenAI 兼容模式 ``/compatible-mode/v1/embeddings``；
    - 多模态模型（``qwen3-vl-embedding``）：原生 ``multimodal-embedding`` 接口，
      批量文本按「独立向量」返回（``enable_fusion=False``，每个内容元素一个向量），
      保证文档分块与查询用同一口径、同一维度。

    ``dimension`` 为 0 表示不传维度参数，由服务端使用模型默认维度。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "qwen3.7-text-embedding",
        base_url: str = DASHSCOPE_DEFAULT_BASE,
        dimension: int = 0,
        enable_fusion: bool = False,
    ):
        self.api_key = api_key or ""
        self.model = model
        self.base_url = (base_url or DASHSCOPE_DEFAULT_BASE).strip().rstrip("/")
        self.dimension = int(dimension or 0)
        self.enable_fusion = bool(enable_fusion)
        self._active_model: str = model

    @property
    def active_model(self) -> str:
        """当前实际生效的向量化模型名称。"""
        return self._active_model

    @property
    def dim(self):
        """当前生效模型的向量维度（未显式指定时取该模型默认维度）。"""
        if self.dimension > 0:
            return self.dimension
        return dashscope_default_dim(self.model)

    @property
    def is_multimodal(self) -> bool:
        """当前模型是否为多模态向量模型（需走原生接口）。"""
        return bool((DASHSCOPE_MODEL_SPECS.get(self.model) or {}).get("multimodal"))

    @property
    def endpoint(self) -> str:
        """当前模型对应的完整请求地址。"""
        path = DASHSCOPE_NATIVE_EMBED_PATH if self.is_multimodal else DASHSCOPE_COMPAT_EMBED_PATH
        return f"{self.base_url}{path}"

    def _build_payload(self, texts: list) -> dict:
        """按模型类型构造请求体。"""
        if self.is_multimodal:
            params = {"enable_fusion": self.enable_fusion}
            if self.dimension > 0:
                params["dimension"] = self.dimension
            return {
                "model": self.model,
                "input": {"contents": [{"text": t} for t in texts]},
                "parameters": params,
            }
        body = {"model": self.model, "input": texts, "encoding_format": "float"}
        if self.dimension > 0:
            body["dimensions"] = self.dimension
        return body

    def _embed_once(self, texts: list) -> list:
        """完成一次批量向量化（受单次 20 条限制，由调用方分批）。"""
        if not self.api_key:
            raise ValueError(
                "未配置阿里云百炼 API Key。请在「知识管理 → 向量模型与 API 设置」中填写，"
                "或在项目根目录 .env 中设置 DASHSCOPE_API_KEY 后重启服务。"
            )
        resp = requests.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=self._build_payload(texts),
            timeout=120,
        )
        if resp.status_code >= 400:
            raise DashScopeEmbeddingError(self.model, resp.status_code, resp.text)
        try:
            payload = resp.json()
        except Exception:
            raise DashScopeEmbeddingError(self.model, resp.status_code, resp.text)
        # 百炼在 HTTP 200 时也可能返回业务错误码
        code = payload.get("code")
        if code and str(code) not in ("", "None", "Success"):
            raise DashScopeEmbeddingError(
                self.model, resp.status_code, f"{code}: {payload.get('message', '')}"
            )
        vectors = _extract_vectors(payload)
        if not vectors:
            raise DashScopeEmbeddingError(
                self.model, resp.status_code, f"响应中未解析到向量：{str(payload)[:400]}"
            )
        return vectors

    def _embed(self, texts: list) -> list:
        """分批向量化，并校验返回条数。"""
        if not texts:
            return []
        vectors: list = []
        for i in range(0, len(texts), DASHSCOPE_BATCH_SIZE):
            batch = texts[i : i + DASHSCOPE_BATCH_SIZE]
            part = self._embed_once(batch)
            if len(part) != len(batch):
                raise DashScopeEmbeddingError(
                    self.model, 200,
                    f"返回向量条数({len(part)})与输入条数({len(batch)})不一致，"
                    f"请检查该模型是否支持批量输入。",
                )
            vectors.extend(part)
        self._active_model = self.model
        return vectors

    def embed_documents(self, texts: list) -> list:
        """批量向量化文档片段（供向量库写入）。"""
        return self._embed(texts)

    def embed_query(self, text: str) -> list:
        """向量化单条查询（供检索使用）。"""
        return self._embed([text])[0]


def create_embeddings(cfg) -> Embeddings:
    """按配置构建向量化实例（智谱 / 阿里云百炼二选一）。

    判定规则：``embedding_provider`` 显式为 ``dashscope``，
    或模型名属于百炼系列（``qwen*`` / ``text-embedding-v*`` / ``tongyi-embedding*``）
    时使用 ``DashScopeEmbeddings``，否则使用 ``ZhipuEmbeddings``。
    """
    provider = (getattr(cfg, "embedding_provider", "zhipu") or "zhipu").strip().lower()
    model = (getattr(cfg, "embedding_model", "embedding-2") or "embedding-2").strip()
    if provider == "dashscope" or is_dashscope_model(model):
        return DashScopeEmbeddings(
            api_key=getattr(cfg, "dashscope_api_key", "") or "",
            model=model,
            base_url=getattr(cfg, "dashscope_base_url", DASHSCOPE_DEFAULT_BASE) or DASHSCOPE_DEFAULT_BASE,
            dimension=int(getattr(cfg, "embedding_dim", 0) or 0),
        )
    return ZhipuEmbeddings(api_key=cfg.zhipu_api_key, model=model)
