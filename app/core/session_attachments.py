"""
会话级附件存储（Ephemeral Attachment Store）
==========================================
服务于「对话框附件」功能：用户在对话中上传的附件，仅在本会话内解析与检索，
不会写入持久化知识库（data/vectorstore）。每个会话以 session_id 隔离，
附件解析后的文本片段缓存在内存并构建临时向量索引，供多轮对话复用，
会话结束后可手动清空释放资源。

设计要点：
  - 线程安全：对会话字典的读写通过 _lock 保护。
  - 懒构建索引：首次检索时才构建临时向量索引并缓存，避免空问也烧 embedding 配额。
  - 自动回退：优先用 FAISS 构建临时索引；若 FAISS 不可用则回退到纯 Python 内存向量库。
  - 不污染知识库：附件与持久化知识库完全隔离，互不影响。
"""
import os
import shutil
import threading
import uuid
from typing import Dict, List, Optional

from langchain_core.documents import Document

from app.core.config import DATA_DIR
from app.core.vectorstore import MemoryVectorStore

# 会话附件落盘目录：data/attachments/<session_id>/<原始文件名>
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")

_lock = threading.Lock()
# session_id -> {"docs": List[Document], "index": Optional[向量索引], "model": str}
_sessions: Dict[str, dict] = {}


def _new_session() -> str:
    return uuid.uuid4().hex


def ensure_session(session_id: Optional[str]) -> str:
    """保证会话存在并返回 session_id（未提供则创建新会话）。"""
    if not session_id:
        session_id = _new_session()
    with _lock:
        _sessions.setdefault(
            session_id, {"docs": [], "index": None, "model": "", "pages": {}}
        )
    return session_id


def add_files(
    session_id: str,
    docs: List[Document],
    model: str = "",
    pages: Optional[Dict[str, List[Dict]]] = None,
):
    """
    将解析后的附件片段追加到会话，并置空索引（下次检索时重建）。
    同一会话可多次追加，后续问答会综合全部附件内容。

    :param pages: {文件名: [{"page": n, "content": 原文}, ...]}，上传时顺手把
        「未分块的原始分页文本」也存一份，供预览接口直接复用——
        否则预览要重新解析一次文件（docx/pptx 的图片解析要走视觉模型，
        动辄几十秒，前端会读到超时）。
    """
    with _lock:
        sess = _sessions.setdefault(
            session_id, {"docs": [], "index": None, "model": model, "pages": {}}
        )
        sess["docs"].extend(docs)
        sess["index"] = None  # 文档变更，失效旧索引
        if model:
            sess["model"] = model
        if pages:
            sess.setdefault("pages", {}).update(pages)


def list_files(session_id: str) -> List[str]:
    """返回该会话已附加的去重文件名列表（按首次出现顺序）。"""
    with _lock:
        docs = _sessions.get(session_id, {}).get("docs", [])
    names: List[str] = []
    for d in docs:
        fn = d.metadata.get("file_name") or os.path.basename(
            str(d.metadata.get("source", ""))
        ) or "未知文件"
        if fn not in names:
            names.append(fn)
    return names


def delete_file(session_id: str, file_name: str) -> bool:
    """删除指定会话中的单个附件（内存文档 + 落盘文件），并失效索引。"""
    if not session_id or not file_name:
        return False
    file_name = os.path.basename(file_name)
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return False
        before = len(sess["docs"])
        sess["docs"] = [
            d for d in sess["docs"]
            if (d.metadata.get("file_name") or os.path.basename(
                str(d.metadata.get("source", ""))
            )) != file_name
        ]
        sess["index"] = None  # 文档变更，失效旧索引
        sess.get("pages", {}).pop(file_name, None)
        removed_mem = len(sess["docs"]) < before

    sess_dir = os.path.join(ATTACH_DIR, session_id)
    path = os.path.join(sess_dir, file_name)
    removed_disk = False
    if os.path.isfile(path):
        try:
            os.remove(path)
            removed_disk = True
        except Exception:
            pass
    return removed_mem or removed_disk


def clear(session_id: str):
    """释放会话内存并删除落盘附件。"""
    with _lock:
        _sessions.pop(session_id, None)
    if session_id:
        sess_dir = os.path.join(ATTACH_DIR, session_id)
        if os.path.isdir(sess_dir):
            shutil.rmtree(sess_dir, ignore_errors=True)


def get_pages(session_id: str, file_name: str) -> Optional[List[Dict]]:
    """返回上传时缓存的分页原文（预览用）；没有则返回 None（调用方回退到重新解析）。"""
    if not session_id or not file_name:
        return None
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return None
        pages = sess.get("pages", {}).get(os.path.basename(file_name))
        return list(pages) if pages else None


def get_docs(session_id: str) -> List[Document]:
    """
    返回该会话已解析的附件片段原文（不依赖向量索引），用于兜底注入。

    当向量检索不可用（如 embedding 额度耗尽触发 429）或召回为空时，
    直接以附件原文（尤其是图片的视觉描述）注入上下文，保证附件内容
    仍能被回答模型看到，避免「文本模型 + 图片」场景下图片被完全忽略。
    """
    with _lock:
        sess = _sessions.get(session_id)
        return list(sess["docs"]) if sess else []


def get_store(session_id: str, embeddings) -> Optional[object]:
    """
    返回该会话的临时向量索引（懒加载：首次检索时构建并缓存）。
    无附件或构建失败时返回 None。
    """
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return None
        docs = sess["docs"]
        if not docs:
            return None
        index = sess["index"]

    if index is not None:
        return index

    # 构建临时索引：优先 FAISS，失败回退内存向量库
    try:
        from langchain_community.vectorstores import FAISS

        index = FAISS.from_documents(docs, embeddings)
    except Exception:
        store = MemoryVectorStore(embeddings)
        store.add_documents(docs)
        index = store

    with _lock:
        # 仅当会话与文档列表未被清空/替换时才缓存，避免覆盖并发产生的新索引
        cur = _sessions.get(session_id)
        if cur is not None and cur.get("docs") is docs:
            cur["index"] = index
    return index
