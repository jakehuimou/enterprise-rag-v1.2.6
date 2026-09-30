"""文件接入层 - 上传落盘与格式校验服务。"""
import os
import shutil
from datetime import datetime

from app.core.config import UPLOAD_DIR

# 允许上传的文件格式（与解析层支持格式保持一致）
ALLOWED_EXT = {
    ".pdf", ".docx", ".doc",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".md", ".markdown",
    ".txt",
    # 图片类：经视觉模型生成文字描述后入库
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
}


def save_upload(file) -> (str, str):
    """
    将上传的文件保存到 data/uploads 目录。

    :param file: FastAPI 的 UploadFile 对象
    :return: (保存后的服务器路径, 文件原始名称)
    :raises ValueError: 格式不支持时
    """
    original = file.filename or "unknown"
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(
            f"不支持的文件格式：{ext or '无扩展名'}。仅支持："
            f"{', '.join(sorted(ALLOWED_EXT))}"
        )
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # 用时间戳前缀避免同名覆盖
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_name = f"{ts}_{original}"
    path = os.path.join(UPLOAD_DIR, safe_name)
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return path, original
