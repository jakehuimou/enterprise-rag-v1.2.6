"""配置层 REST 接口：在线查看 / 调整运行参数。"""
from fastapi import APIRouter

from app.models.schemas import ConfigUpdate
from app.core.config import load_config, save_runtime_config, config_to_dict
from app.core.embeddings import is_dashscope_model
from app.core.rag import rebuild_pipeline

router = APIRouter(prefix="/api", tags=["参数配置"])


@router.get("/config", summary="获取当前配置")
def get_config():
    """返回当前生效的全部配置（含运行时覆盖）。"""
    return config_to_dict(load_config())


@router.post("/config", summary="更新运行参数")
def update_config(body: ConfigUpdate):
    """
    更新可调参数并持久化到 runtime.json，随后重建 RAG 管道使新参数生效。

    说明：
      - 只提交 ``embedding_model`` 时，会按模型名自动推断向量服务商
        （``qwen*`` / ``text-embedding-v*`` / ``tongyi-embedding*`` 判为阿里云百炼），
        避免「选了百炼模型却仍走智谱接口」。
      - 修改 embedding_model / embedding_dim 后，已入库的旧向量维度随之变化，
        需调用 ``POST /api/files/rebuild`` 重建知识库以保证检索一致性。
    """
    cfg = load_config()
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(cfg, k, v)
    if "embedding_model" in data and "embedding_provider" not in data:
        cfg.embedding_provider = (
            "dashscope" if is_dashscope_model(cfg.embedding_model) else "zhipu"
        )
    save_runtime_config(cfg)
    rebuild_pipeline(cfg)
    return config_to_dict(cfg)
