"""文件接入层 REST 接口：上传并解析入库。"""
import os
import traceback
from datetime import datetime
from typing import List

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from app.services.file_service import save_upload
from app.core.rag import get_pipeline
from app.core import folder_store

router = APIRouter(prefix="/api", tags=["文件接入"])


@router.post("/files/upload", summary="上传文件并解析入库")
def upload_file(file: UploadFile = File(...), folder: str = Form("")):
    """
    接收多格式文件 -> 保存到本地 -> Loader 解析 -> 分块 -> 向量化入库。
    返回生成的 Chunk 数量与解析页数。``folder`` 为可选的虚拟文件夹路径。
    """
    try:
        path, original = save_upload(file)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 以磁盘实际文件名（含时间戳前缀）作为注册标识，保证与统一查看列表一致
    disk_name = os.path.basename(path)
    try:
        folder_store.set_file_folder(disk_name, folder)
    except ValueError:
        # 非法文件夹路径忽略（归到根目录），不阻断上传
        pass
    try:
        pipe = get_pipeline()
        result = pipe.ingest_file(path, disk_name, upload_time)
    except ValueError as e:
        # 配置类错误（如缺少 API Key）返回 400，提示更明确
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"文档解析入库失败：{e}")
    return {
        "file_name": disk_name,
        "path": path,
        "folder": folder_store.get_file_folder(disk_name),
        "pages": result["pages"],
        "chunks": result["chunks"],
        "embedding_model": result.get("embedding_model", ""),
    }


@router.post("/files/batch-upload", summary="批量上传文件并解析入库")
def batch_upload(files: List[UploadFile] = File(...), folder: str = Form("")):
    """
    一次接收多个文件 -> 逐个保存 -> 解析 -> 分块 -> 向量化入库。
    返回每个文件的成功/失败明细，以及汇总计数，保证“部分失败不中断整体”。
    ``folder`` 为可选的虚拟文件夹路径（对批次内所有文件生效）。
    """
    norm_folder = ""
    try:
        norm_folder = folder_store.normalize(folder)
    except ValueError:
        norm_folder = ""
    results = []
    for file in files:
        item = {
            "original": file.filename or "unknown",
            "file_name": "",
            "status": "error",
            "pages": 0,
            "chunks": 0,
            "message": "",
        }
        try:
            path, original = save_upload(file)
        except ValueError as e:
            item["message"] = str(e)
            results.append(item)
            continue

        disk_name = os.path.basename(path)
        item["file_name"] = disk_name
        item["original"] = original
        folder_store.set_file_folder(disk_name, norm_folder)
        item["folder"] = norm_folder
        upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            pipe = get_pipeline()
            res = pipe.ingest_file(path, disk_name, upload_time)
            item["pages"] = res["pages"]
            item["chunks"] = res["chunks"]
            item["embedding_model"] = res.get("embedding_model", "")
            item["status"] = "success"
        except ValueError as e:
            item["message"] = str(e)
        except Exception as e:
            traceback.print_exc()
            item["message"] = f"文档解析入库失败：{e}"
        results.append(item)

    success = sum(1 for r in results if r["status"] == "success")
    return {
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "results": results,
    }
