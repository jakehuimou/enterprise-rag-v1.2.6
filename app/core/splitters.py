"""
文档分块层（Document Chunking Layer）
====================================
将解析后的 Document 切分为语义完整、粒度稳定、边界清晰的 Chunk。

提供三种分块策略（可配置）：
  1. recursive（默认，通用文档优先）：递归字符拆分 RecursiveCharacterTextSplitter
  2. markdown：按 Markdown 标题层级拆分 + 递归字符二次拆分
  3. token：按 Token 拆分 TokenTextSplitter

每个 Chunk 在元数据中追加 ``chunk_index``，便于溯源与调试。
"""
from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    MarkdownHeaderTextSplitter,
    TokenTextSplitter,
)

from app.core.config import AppConfig


def split_documents(docs: List[Document], cfg: AppConfig) -> List[Document]:
    """根据配置的分块策略对文档列表进行分块。"""
    if cfg.splitter_type == "markdown":
        return _split_markdown(docs, cfg)
    if cfg.splitter_type == "token":
        return _split_token(docs, cfg)
    return _split_recursive(docs, cfg)


def _base_recursive(cfg: AppConfig) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
        separators=cfg.separators,
        keep_separator=True,
    )


def _split_recursive(docs: List[Document], cfg: AppConfig) -> List[Document]:
    """递归字符拆分（默认）：按分隔符优先级逐级切分，保证语义连贯。"""
    splitter = _base_recursive(cfg)
    chunks = splitter.split_documents(docs)
    return _add_chunk_index(chunks)


def _split_markdown(docs: List[Document], cfg: AppConfig) -> List[Document]:
    """按 Markdown 标题拆分，再对每个标题块做递归字符二次拆分。"""
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "h1"),
            ("##", "h2"),
            ("###", "h3"),
            ("####", "h4"),
        ]
    )
    sub = _base_recursive(cfg)
    chunks: List[Document] = []
    for d in docs:
        try:
            md_chunks = header_splitter.split_text(d.page_content)
        except Exception:
            md_chunks = [d]
        for mc in md_chunks:
            merged_meta = {**d.metadata, **mc.metadata}
            sub_chunks = sub.split_documents(
                [Document(page_content=mc.page_content, metadata=merged_meta)]
            )
            chunks.extend(sub_chunks)
    return _add_chunk_index(chunks)


def _split_token(docs: List[Document], cfg: AppConfig) -> List[Document]:
    """按 Token 拆分，适合对 token 长度有严格要求的场景。"""
    splitter = TokenTextSplitter(
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
    )
    chunks = splitter.split_documents(docs)
    return _add_chunk_index(chunks)


def _add_chunk_index(chunks: List[Document]) -> List[Document]:
    """为每个 Chunk 追加 chunk_index 序号。"""
    for i, c in enumerate(chunks):
        c.metadata = dict(c.metadata)
        c.metadata["chunk_index"] = i
    return chunks
