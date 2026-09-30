"""
离线入库脚本（命令行）
======================
将本地文件或整个目录下的文档批量解析、分块并写入向量库，
无需通过 Web 界面上传。适合初始化知识库。

用法（在项目根目录执行）：
    # 入库单个文件
    python scripts/ingest.py "docs/制度规范.docx"

    # 批量入库整个目录
    python scripts/ingest.py "docs/"
"""
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core.rag import get_pipeline


def collect_files(path: str):
    if os.path.isdir(path):
        return [
            os.path.join(path, f)
            for f in os.listdir(path)
            if os.path.isfile(os.path.join(path, f))
        ]
    return [path]


def main():
    if len(sys.argv) < 2:
        print("用法：python scripts/ingest.py <文件或目录路径>")
        sys.exit(1)

    target = sys.argv[1]
    if not os.path.exists(target):
        print(f"路径不存在：{target}")
        sys.exit(1)

    pipe = get_pipeline()
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    files = collect_files(target)

    print(f"开始入库，共发现 {len(files)} 个文件。")
    for f in files:
        try:
            res = pipe.ingest_file(f, os.path.basename(f), upload_time)
            print(f"  ✅ {os.path.basename(f)}：解析 {res['pages']} 页，生成 {res['chunks']} 个分块")
        except Exception as e:
            print(f"  ❌ {os.path.basename(f)}：{e}")


if __name__ == "__main__":
    main()
