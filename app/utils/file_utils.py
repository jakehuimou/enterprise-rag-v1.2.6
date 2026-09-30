"""文件工具：上传/输出目录管理、安全文件名、格式白名单。

仅承载与「文档翻译」功能密切相关的纯文件操作，避免与项目其它模块耦合。
"""
import os
import re
import uuid

# 项目根目录（app 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 上传临时目录与翻译结果输出目录
UPLOAD_DIR = os.path.join(ROOT, "data", "translate_uploads")
OUTPUT_DIR = os.path.join(ROOT, "data", "translate_output")

# 文档翻译支持的输入格式
ALLOWED_EXT = {
    ".docx", ".doc", ".pdf", ".pptx", ".ppt", ".txt", ".md",
}


def ensure_dirs():
    """确保上传目录与输出目录存在。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def safe_filename(name: str) -> str:
    """把任意文件名规范化为安全的本地文件名（保留中文，去除路径与危险字符）。"""
    base = os.path.basename(name)
    # 去掉路径分隔与不可见字符，保留字母数字、中文、点、下划线、短横
    base = re.sub(r"[^\w一-龥.\- ]", "_", base, flags=re.UNICODE)
    base = base.strip().strip(".")
    if not base:
        base = f"file_{uuid.uuid4().hex[:8]}"
    return base


def is_allowed(filename: str) -> bool:
    """判断文件名扩展名是否在允许清单内。"""
    if not filename:
        return False
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXT


def output_path(source_path: str, ext: str, reserved: set = None) -> str:
    """根据源文件路径生成翻译结果输出路径（同目录结构映射到 OUTPUT_DIR）。

    - **文件名与原文档名称保持一致**（主体沿用源文件名，不追加额外后缀），
      仅扩展名可能随输出格式变化（如 .docx -> .pdf）；
    - 同名文件默认覆盖，保证反复翻译时结果名始终与原文一致；
    - ``reserved`` 为「本批次已占用文件名集合」（小写），用于避免同一批中
      多个同主体名文件互相覆盖（如 a.docx 与 a.pdf 都输出 docx）；
    - ``ext`` 需带点，如 ``.docx`` / ``.pdf`` / ``.pptx``。
    """
    ensure_dirs()
    stem = os.path.splitext(os.path.basename(source_path))[0]
    name = f"{stem}{ext}"
    if reserved is not None:
        i = 2
        while name.lower() in reserved:
            name = f"{stem}({i}){ext}"
            i += 1
        reserved.add(name.lower())
    return os.path.join(OUTPUT_DIR, name)


def output_path_for_url(url: str, title: str, ext: str) -> str:
    """根据网页 URL 与标题生成翻译结果输出路径（文件名与标题保持一致）。"""
    ensure_dirs()
    safe = re.sub(r"[^\w一-龥.\- ]", "_", title or "web", flags=re.UNICODE).strip(" .") or "web"
    safe = safe[:80]
    return os.path.join(OUTPUT_DIR, f"{safe}{ext}")
