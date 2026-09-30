"""
一键启动脚本
============
同时拉起 FastAPI 后端（API 服务）与 Gradio 前端（Web 界面）：
  - FastAPI  : http://127.0.0.1:8000  （可在 .env 配置 API_PORT）
  - Gradio   : http://127.0.0.1:7860  （可在 .env 配置 GRADIO_PORT）

用法（在项目根目录执行）：
    python scripts/run.py
"""
import os
import subprocess
import sys
import threading
import time

# 将项目根目录加入 Python 路径，保证 app / web 包可导入
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 若当前未在项目 .venv 中运行，自动切换到 .venv 解释器重新执行本脚本，
# 避免用系统 Python 启动时任 gradio_app 导入 pptx / fastapi / langchain 等
# 缺失依赖而崩溃（NameError: RGBColor / ModuleNotFoundError: pptx 等）。
# 判定方式：解释器位于 .venv\Scripts 下即视为 venv 内（兼容 ragserver.exe 等
# 改名副本——某些环境会按 python.exe 进程名回收后台服务）。
_VENV_SCRIPTS = os.path.join(ROOT, ".venv", "Scripts")
_VENV_EXE = os.path.join(_VENV_SCRIPTS, "python.exe")
if os.path.exists(_VENV_EXE) and not os.path.abspath(sys.executable).lower().startswith(
    os.path.abspath(_VENV_SCRIPTS).lower() + os.sep
):
    _args = [os.path.abspath(sys.argv[0])] + list(sys.argv[1:])
    os.execv(_VENV_EXE, [_VENV_EXE] + _args)

from app.core.config import load_config

LOG_DIR = os.path.join(ROOT, "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
BACKEND_LOG = os.path.join(LOG_DIR, "backend.log")


def _warn_if_no_api_key(cfg):
    """若未配置智谱 API Key，给出醒目提示但不阻断启动。"""
    if not cfg.zhipu_api_key:
        print("\n" + "=" * 60, flush=True)
        print("⚠️  警告：未检测到 ZHIPU_API_KEY 环境变量或 .env 配置。", flush=True)
        print("    文档上传与 RAG 问答功能需要调用智谱 AI 接口，", flush=True)
        print("    请在项目根目录创建 .env 文件并写入：", flush=True)
        print("        ZHIPU_API_KEY=你的智谱APIKey", flush=True)
        print("=" * 60 + "\n", flush=True)


def _stream_backend(proc: subprocess.Popen):
    """将后端子进程的 stdout/stderr 实时输出到控制台并写入日志文件。"""
    with open(BACKEND_LOG, "a", encoding="utf-8", errors="replace") as log:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\n")
            if line:
                print(line, flush=True)
                log.write(line + "\n")
                log.flush()


def _resolve_backend_python() -> str:
    """解析用于启动 Uvicorn 后端的 Python 解释器。

    优先使用项目 .venv，确保 pypdf / fastapi / uvicorn 等依赖齐全；
    若存在 ragserver.exe（python.exe 的改名副本，用于规避按进程名回收），
    优先使用它；否则退回 sys.executable。
    """
    scripts_dir = os.path.join(ROOT, ".venv", "Scripts")
    rag = os.path.join(scripts_dir, "ragserver.exe")
    if os.path.exists(rag):
        return rag
    venv_exe = os.path.join(scripts_dir, "python.exe")
    if os.path.exists(venv_exe):
        return venv_exe
    return sys.executable


def start_api_subprocess(host: str, port: int):
    """在独立子进程中启动 Uvicorn 后端，避免线程/EventLoop 问题。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    python_exe = _resolve_backend_python()
    cmd = [
        python_exe,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    return subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _wait_for_backend(base_url: str, timeout: int = 30):
    """轮询后端 /api/health，确认服务就绪后再启动前端。"""
    import requests

    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{base_url}/api/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    cfg = load_config()
    _warn_if_no_api_key(cfg)

    base_url = cfg.api_base_url
    api_proc = start_api_subprocess(cfg.api_host, cfg.api_port)
    streamer = threading.Thread(target=_stream_backend, args=(api_proc,), daemon=True)
    streamer.start()

    try:
        print(f"INFO:     Starting FastAPI backend on {base_url}", flush=True)
        if not _wait_for_backend(base_url, timeout=30):
            print("ERROR:    FastAPI backend failed to start within 30 seconds.", flush=True)
            print(f"ERROR:    Backend logs written to {BACKEND_LOG}", flush=True)
            api_proc.terminate()
            try:
                api_proc.wait(timeout=5)
            except Exception:
                api_proc.kill()
            return

        print("INFO:     FastAPI backend is ready.", flush=True)

        # 启动 Gradio 前端（阻塞主线程）
        from web.gradio_app import launch

        launch()
    finally:
        print("INFO:     Shutting down FastAPI backend...", flush=True)
        api_proc.terminate()
        try:
            api_proc.wait(timeout=5)
        except Exception:
            api_proc.kill()


if __name__ == "__main__":
    main()
