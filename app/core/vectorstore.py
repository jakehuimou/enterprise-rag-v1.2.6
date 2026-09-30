"""
向量处理层 - 向量数据库（持久化存储）
=====================================
优先使用 FAISS 作为本地向量数据库；当 FAISS 未安装或不可用时，
自动回退到纯 Python 实现的内存向量库（MemoryVectorStore），
保证项目在任何环境下都能直接运行，不被缺失的 C 扩展阻塞。

持久化路径：
- FAISS：data/vectorstore/enterprise_rag/
- 内存库：data/vectorstore/memory_store.pkl

两种实现对外接口一致（add_documents / similarity_search），
上层 RAGPipeline 无需关心底层使用的是哪种 store。
"""
import os
import pickle
from typing import Optional, Union

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.core.config import AppConfig, VECTORSTORE_DIR
from app.core.embeddings import create_embeddings

# 尝试导入 FAISS；失败则标记为不可用，后续使用内存向量库兜底
# 注意：仅导入 langchain_community.vectorstores.FAISS 不会触发 faiss 包加载，
# 因此必须同时执行 import faiss 才能真正判断 FAISS 是否可用。
try:
    import faiss  # noqa: F401
    from langchain_community.vectorstores import FAISS
    _FAISS_AVAILABLE = True
except Exception:  # pragma: no cover
    FAISS = None
    _FAISS_AVAILABLE = False

INDEX_NAME = "enterprise_rag"
MEMORY_STORE_FILE = "memory_store.pkl"

# 已知模型的向量维度；embedding-2 与 embedding-3 维度不同，不能混用同一索引。
# 百炼模型的维度由 ``embeddings.dim`` 直接给出，这里只保留兜底用的对照表。
_MODEL_DIMS = {"embedding-2": 1024, "embedding-3": 2048}


def _expected_dim(embeddings: Embeddings) -> Optional[int]:
    """获取当前 embedding 模型应产出的向量维度（无需额外 API 调用时直接读取）。

    优先级：模型自报维度（``dim``）-> 内置维度表 -> 探测一次（仅 1 个 token，开销极小）。
    """
    dim = getattr(embeddings, "dim", None)
    if isinstance(dim, int) and dim > 0:
        return dim
    name = getattr(embeddings, "model", None) or getattr(embeddings, "_active_model", None)
    if name in _MODEL_DIMS:
        return _MODEL_DIMS[name]
    try:
        return len(embeddings.embed_query("dimension-probe"))
    except Exception:
        return None


def build_embeddings(cfg: AppConfig) -> Embeddings:
    """根据配置构建 Embedding 模型实例（智谱 / 阿里云百炼，由配置决定）。"""
    return create_embeddings(cfg)


# ---------------------------------------------------------------------------
# FAISS 持久化辅助
# ---------------------------------------------------------------------------
def _index_dir() -> str:
    return os.path.join(VECTORSTORE_DIR, INDEX_NAME)


def _index_exists() -> bool:
    """检查本地是否已有 FAISS 持久化索引文件。"""
    d = _index_dir()
    if not os.path.isdir(d):
        return False
    files = os.listdir(d)
    return any(f.endswith(".faiss") for f in files) and any(f.endswith(".pkl") for f in files)


# ---------------------------------------------------------------------------
# 内存向量库持久化辅助
# ---------------------------------------------------------------------------
def _memory_path() -> str:
    return os.path.join(VECTORSTORE_DIR, MEMORY_STORE_FILE)


def _memory_exists() -> bool:
    return os.path.isfile(_memory_path())


# ---------------------------------------------------------------------------
# 内存向量库（纯 Python 实现，完全不依赖 faiss）
# ---------------------------------------------------------------------------
class MemoryVectorStore:
    """
    基于 numpy 的轻量级内存向量库。

    接口与 LangChain VectorStore 保持一致：
      - add_documents(docs)
      - similarity_search(query, k=4)
    """

    def __init__(self, embeddings):
        self.embeddings = embeddings
        self.texts: list = []
        self.vectors: list = []
        self.metadatas: list = []

    def add_documents(self, docs: list) -> None:
        if not docs:
            return
        texts = [d.page_content for d in docs]
        metas = [d.metadata for d in docs]
        vectors = self.embeddings.embed_documents(texts)
        self.texts.extend(texts)
        self.vectors.extend(vectors)
        self.metadatas.extend(metas)

    def similarity_search(self, query: str, k: int = 4) -> list:
        if not self.texts:
            return []

        import numpy as np

        q_vec = np.asarray(self.embeddings.embed_query(query), dtype=np.float32)
        vectors = np.asarray(self.vectors, dtype=np.float32)

        q_norm = np.linalg.norm(q_vec)
        if q_norm == 0:
            return []

        # 余弦相似度 = 点积 / (||q|| * ||v||)
        dots = vectors @ q_vec
        norms = np.linalg.norm(vectors, axis=1)
        sims = dots / (norms * q_norm)

        # 取 TopK，并过滤掉数值异常（如 NaN）的结果
        top_idx = np.argsort(sims)[::-1][:k]
        results = []
        for idx in top_idx:
            if not np.isfinite(sims[idx]):
                continue
            results.append(
                Document(page_content=self.texts[idx], metadata=self.metadatas[idx])
            )
        return results


def _load_memory_store(embeddings: Embeddings) -> Optional[MemoryVectorStore]:
    """加载之前持久化的内存向量库。"""
    if not _memory_exists():
        return None
    try:
        with open(_memory_path(), "rb") as f:
            store = pickle.load(f)
        if isinstance(store, MemoryVectorStore):
            store.embeddings = embeddings
            return store
    except Exception:  # pragma: no cover
        return None
    return None


def _save_memory_store(store: MemoryVectorStore) -> None:
    """将内存向量库 pickle 落盘。"""
    os.makedirs(VECTORSTORE_DIR, exist_ok=True)
    with open(_memory_path(), "wb") as f:
        pickle.dump(store, f)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def get_vectorstore(
    cfg: AppConfig, embeddings: Embeddings
) -> Union[FAISS, MemoryVectorStore, None]:
    """
    获取本地向量库实例。

    加载顺序：
      1. 若 FAISS 可用且本地有 FAISS 索引，则加载 FAISS。
      2. 若本地有内存库 pickle 文件，则加载内存库。
      3. 返回 None（首次上传时由 add_documents 创建新库）。
    """
    os.makedirs(VECTORSTORE_DIR, exist_ok=True)

    if _FAISS_AVAILABLE and _index_exists():
        try:
            import faiss as _faiss

            idx = _faiss.read_index(os.path.join(_index_dir(), "index.faiss"))
            exp = _expected_dim(embeddings)
            if exp and idx.d != exp:
                # 维度不匹配：通常是更换了向量模型导致旧索引不可用。
                # 直接丢弃旧索引，返回 None，由上层在入库/自愈时重建。
                import shutil

                shutil.rmtree(_index_dir(), ignore_errors=True)
                return None
            return FAISS.load_local(
                _index_dir(),
                embeddings,
                allow_dangerous_deserialization=True,
            )
        except Exception:
            # 索引损坏或维度不兼容：丢弃旧库，下次入库时重建
            return None

    if _memory_exists():
        store = _load_memory_store(embeddings)
        if store is not None:
            return store

    return None


def add_documents(
    store: Optional[Union[FAISS, MemoryVectorStore]],
    docs: list,
    embeddings: Embeddings,
) -> Union[FAISS, MemoryVectorStore]:
    """向向量库写入文档片段并持久化。"""
    if not docs:
        return store

    if _FAISS_AVAILABLE:
        os.makedirs(_index_dir(), exist_ok=True)
        if store is None or isinstance(store, MemoryVectorStore):
            # 从内存库切换到 FAISS 时，丢弃旧内存库数据，以 FAISS 为准
            store = FAISS.from_documents(docs, embeddings)
        else:
            try:
                store.add_documents(docs)
            except Exception:
                # 维度不匹配（如切换到不同向量模型）时，以当前数据重建库
                store = FAISS.from_documents(docs, embeddings)
        store.save_local(_index_dir())
    else:
        if store is None:
            store = MemoryVectorStore(embeddings)
        store.add_documents(docs)
        _save_memory_store(store)

    return store


def backend_type_label() -> str:
    """返回当前实际使用的向量库名称，用于启动日志展示。"""
    return "FAISS" if _FAISS_AVAILABLE else "MemoryVectorStore(pure-python)"
