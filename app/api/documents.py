"""文档管理层 REST 接口：统一查看、在线预览、重新解析、删除、批量操作。"""
import os
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.rag import get_pipeline
from app.core.config import UPLOAD_DIR
from app.core import folder_store

router = APIRouter(prefix="/api", tags=["文档管理"])


class BatchRequest(BaseModel):
    file_names: List[str]


@router.get("/files", summary="获取已入库文档列表（注册表）")
def list_files():
    """返回 documents.json 中登记的文档清单。"""
    pipe = get_pipeline()
    return pipe.list_documents()


@router.get("/files/all", summary="统一查看所有上传文件")
def list_all_files():
    """
    以 uploads 目录为事实来源，列出全部已上传文件，
    并合并解析/入库状态、分块数、页数、大小、上传时间，用于统一查看。
    """
    pipe = get_pipeline()
    return pipe.list_all_files()


# 预览结果缓存：(文件路径, 修改时间) -> 预览数据。翻页时直接命中，避免重复解析
# （尤其是扫描件 PDF 的视觉 OCR 解析开销较大）。
_PREVIEW_CACHE: dict = {}


@router.get("/files/preview", summary="在线预览文档正文")
def preview_file(file_name: str = "", file_path: str = ""):
    """
    按文件名或路径加载原始解析文本，用于前端在线预览。

    - 按页返回 ``pages`` / ``page_count``（PDF/PPT/Excel 按原生页，Word/TXT/MD 按约 1500 字切页），
      前端以「文档阅读器」形式翻页阅读；同时保留 ``content`` 兼容旧调用；
    - 预览文本总量上限 50000 字符；
    - 结果按 (文件路径, 修改时间) 缓存，翻页不重复解析。
    """
    pipe = get_pipeline()
    target = file_path or pipe._find_path(file_name)
    mtime = os.path.getmtime(target) if target and os.path.exists(target) else None
    cache_key = (target or file_name, mtime)
    if target and mtime is not None:
        cached = _PREVIEW_CACHE.get(cache_key)
        if cached is not None:
            return cached
    try:
        data = pipe.preview_document(file_name=file_name, file_path=file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预览失败：{e}")
    if target and mtime is not None:
        if len(_PREVIEW_CACHE) >= 64:  # 简单上限，避免无限增长
            _PREVIEW_CACHE.clear()
        _PREVIEW_CACHE[cache_key] = data
    return data


class IndexRequest(BaseModel):
    file_name: str


class FolderCreate(BaseModel):
    parent: str = ""
    name: str


class FolderDelete(BaseModel):
    path: str


class MoveRequest(BaseModel):
    file_name: str
    folder: str = ""


@router.post("/files/index", summary="解析并入库指定上传文件")
def index_existing(req: IndexRequest):
    """
    对已上传但未解析（或需重新解析）的文件执行 Loader 解析 -> 分块 -> 向量化入库。
    """
    full = os.path.join(UPLOAD_DIR, req.file_name)
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="文件不存在，可能已被删除。")
    pipe = get_pipeline()
    # 上传时间：优先取文件名时间戳前缀，否则用当前时间
    prefix = req.file_name.split("_", 1)[0]
    if len(prefix) == 14 and prefix.isdigit():
        try:
            upload_time = datetime.strptime(prefix, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = pipe.ingest_file(full, req.file_name, upload_time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析入库失败：{e}")
    return {
        "file_name": req.file_name,
        "pages": result["pages"],
        "chunks": result["chunks"],
        "embedding_model": result.get("embedding_model", ""),
    }


@router.delete("/files", summary="删除上传文件")
def delete_file(file_name: str = ""):
    """从磁盘与注册表中删除指定文件（按文件名匹配）。"""
    if not file_name:
        raise HTTPException(status_code=400, detail="缺少 file_name 参数。")
    pipe = get_pipeline()
    try:
        return pipe.delete_file(file_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _infer_upload_time(file_name: str) -> str:
    """从文件名时间戳前缀推断上传时间，无前缀则用当前时间。"""
    prefix = file_name.split("_", 1)[0]
    if len(prefix) == 14 and prefix.isdigit():
        try:
            return datetime.strptime(prefix, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@router.post("/files/batch-index", summary="批量解析并入库")
def batch_index(req: BatchRequest):
    """
    对已上传文件批量执行 Loader 解析 -> 分块 -> 向量化入库。
    常用于“仅上传未解析”的存量文件一次性补解析。
    """
    pipe = get_pipeline()
    results = []
    for fn in req.file_names:
        full = os.path.join(UPLOAD_DIR, fn)
        if not os.path.exists(full):
            results.append({
                "file_name": fn, "status": "error",
                "message": "文件不存在，可能已被删除。",
            })
            continue
        upload_time = _infer_upload_time(fn)
        try:
            res = pipe.ingest_file(full, fn, upload_time)
            results.append({
                "file_name": fn, "status": "success",
                "pages": res["pages"], "chunks": res["chunks"],
                "embedding_model": res.get("embedding_model", ""),
            })
        except ValueError as e:
            results.append({"file_name": fn, "status": "error", "message": str(e)})
        except Exception as e:
            results.append({"file_name": fn, "status": "error", "message": f"解析入库失败：{e}"})
    success = sum(1 for r in results if r["status"] == "success")
    return {
        "total": len(results), "success": success,
        "failed": len(results) - success, "results": results,
    }


@router.post("/files/batch-delete", summary="批量删除上传文件")
def batch_delete(req: BatchRequest):
    """从磁盘与注册表批量删除指定文件（按文件名精确匹配）。"""
    pipe = get_pipeline()
    results = []
    for fn in req.file_names:
        try:
            d = pipe.delete_file(fn)
            results.append({
                "file_name": fn, "status": "success",
                "deleted": d.get("deleted", fn),
            })
        except Exception as e:
            results.append({"file_name": fn, "status": "error", "message": str(e)})
    success = sum(1 for r in results if r["status"] == "success")
    return {
        "total": len(results), "success": success,
        "failed": len(results) - success, "results": results,
    }


@router.get("/files/tree", summary="获取树形结构（文件夹 + 文件）")
def get_tree(folder: str = ""):
    """
    返回知识管理模块的树形结构（嵌套节点），用于前端“树形展示”。

    - ``folder`` 为空表示返回完整树；传入某个文件夹路径则只返回该子树。
    - 节点：folder -> ``{type, name, path, children, file_count}``；
      file  -> ``{type, name, path(=disk_name), meta}``。
    """
    pipe = get_pipeline()
    files = pipe.list_all_files()
    try:
        tree = folder_store.build_tree(files, folder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "tree": tree,
        "folders": folder_store.list_folder_choices(),
    }


@router.post("/files/folders", summary="新建文件夹")
def create_folder(req: FolderCreate):
    """在 ``parent`` 下新建名为 ``name`` 的文件夹（虚拟文件夹，不影响磁盘布局）。"""
    try:
        path = folder_store.create_folder(req.parent or "", req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": path, "folders": folder_store.list_folder_choices()}


@router.delete("/files/folders", summary="删除空文件夹")
def delete_folder(req: FolderDelete):
    """删除一个文件夹；仅当该文件夹（及其子文件夹）不含任何文件时才允许。"""
    try:
        folder_store.delete_folder(req.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": req.path, "folders": folder_store.list_folder_choices()}


@router.post("/files/move", summary="移动文件到指定文件夹")
def move_file(req: MoveRequest):
    """将某个上传文件移动到 ``folder``（'' 表示根目录），更新其虚拟文件夹标签。"""
    if not req.file_name:
        raise HTTPException(status_code=400, detail="缺少 file_name 参数。")
    try:
        folder = folder_store.normalize(req.folder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    folder_store.set_file_folder(req.file_name, folder)
    return {
        "file_name": req.file_name,
        "folder": folder,
        "folders": folder_store.list_folder_choices(),
    }


@router.post("/files/rebuild", summary="按当前模型重建整个知识库")
def rebuild_kb():
    """
    清空并重建向量库：以当前配置的 embedding 模型，重新解析、分块、向量化
    uploads 目录下的全部文件。用于修复「模型/维度不一致导致检索不到内容」、
    索引损坏，或切换向量模型后需要全量重建的场景。
    """
    pipe = get_pipeline()
    try:
        results = pipe.rebuild_index()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重建知识库失败：{e}")
    success = sum(1 for r in results if r.get("ok"))
    return {
        "embedding_model": pipe.embeddings.active_model,
        "total": len(results), "success": success,
        "failed": len(results) - success, "results": results,
    }
