"""
定时任务持久化存储
==================
任务元数据与运行记录均保存在 ``data/tasks/`` 下的 JSON 文件中：
  - tasks.json    任务列表
  - runs.json     运行记录

不引入数据库，保持与现有项目（文件夹 / 注册表均为 JSON）一致的轻量风格。
"""
import os
import json
import uuid
import re
from datetime import datetime
from typing import List, Dict, Optional

from app.core.config import DATA_DIR

TASKS_DIR = os.path.join(DATA_DIR, "tasks")
TASKS_FILE = os.path.join(TASKS_DIR, "tasks.json")
RUNS_FILE = os.path.join(TASKS_DIR, "runs.json")
OUTPUT_DIR = os.path.join(TASKS_DIR, "outputs")

_TASK_LOCK = __import__("threading").Lock()
_RUN_LOCK = __import__("threading").Lock()


def _ensure_dirs():
    os.makedirs(TASKS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def _load_json(path: str, default) -> any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data):
    _ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def load_tasks() -> List[Dict]:
    with _TASK_LOCK:
        return _load_json(TASKS_FILE, [])


def save_tasks(tasks: List[Dict]):
    with _TASK_LOCK:
        _save_json(TASKS_FILE, tasks)


def get_task(task_id: str) -> Optional[Dict]:
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def create_task(data: Dict) -> Dict:
    now = datetime.now().isoformat(timespec="seconds")
    task = {
        "id": str(uuid.uuid4()),
        "name": (data.get("name") or "").strip(),
        "prompt": (data.get("prompt") or "").strip(),
        "workspace": (data.get("workspace") or "默认工作空间").strip(),
        "access": data.get("access") or "full",
        "schedule_type": data.get("schedule_type") or "once",
        "start_at": (data.get("start_at") or "").strip(),
        "valid_until": (data.get("valid_until") or "").strip() or None,
        "enabled": bool(data.get("enabled", True)),
        "model": (data.get("model") or "").strip() or None,
        "tags": _normalize_tags(data.get("tags")),
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "next_run_at": None,
        "status": "active" if data.get("enabled", True) else "paused",
    }
    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)
    return task


def update_task(task_id: str, data: Dict) -> Optional[Dict]:
    tasks = load_tasks()
    for i, t in enumerate(tasks):
        if t.get("id") == task_id:
            now = datetime.now().isoformat(timespec="seconds")
            t["name"] = (data.get("name") or t["name"]).strip()
            t["prompt"] = (data.get("prompt") or t["prompt"]).strip()
            if "workspace" in data:
                t["workspace"] = (data.get("workspace") or "默认工作空间").strip()
            if "access" in data:
                t["access"] = data.get("access") or t["access"]
            if "schedule_type" in data:
                t["schedule_type"] = data.get("schedule_type") or t["schedule_type"]
            if "start_at" in data:
                t["start_at"] = (data.get("start_at") or "").strip()
            if "valid_until" in data:
                t["valid_until"] = (data.get("valid_until") or "").strip() or None
            if "enabled" in data:
                t["enabled"] = bool(data["enabled"])
            if "model" in data:
                t["model"] = (data.get("model") or "").strip() or None
            if "tags" in data:
                t["tags"] = _normalize_tags(data.get("tags"))
            t["updated_at"] = now
            t["status"] = "active" if t["enabled"] else "paused"
            save_tasks(tasks)
            return t
    return None


def delete_task(task_id: str) -> bool:
    tasks = load_tasks()
    filtered = [t for t in tasks if t.get("id") != task_id]
    if len(filtered) == len(tasks):
        return False
    save_tasks(filtered)
    # 运行记录保留，便于审计；输出文件也保留
    return True


def patch_task(task_id: str, patch: Dict) -> Optional[Dict]:
    """用于调度器在执行后更新 last_run_at / next_run_at 等字段（不刷新 updated_at）。"""
    tasks = load_tasks()
    for t in tasks:
        if t.get("id") == task_id:
            t.update(patch)
            save_tasks(tasks)
            return t
    return None


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------

def load_runs() -> List[Dict]:
    with _RUN_LOCK:
        return _load_json(RUNS_FILE, [])


def save_runs(runs: List[Dict]):
    with _RUN_LOCK:
        _save_json(RUNS_FILE, runs)


def add_run(record: Dict) -> Dict:
    record.setdefault("id", str(uuid.uuid4()))
    with _RUN_LOCK:
        runs = _load_json(RUNS_FILE, [])
        runs.insert(0, record)
        _save_json(RUNS_FILE, runs)
    return record


def get_runs(task_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
    runs = load_runs()
    if task_id:
        runs = [r for r in runs if r.get("task_id") == task_id]
    if limit:
        runs = runs[:limit]
    return runs


def get_run(run_id: str) -> Optional[Dict]:
    for r in load_runs():
        if r.get("id") == run_id:
            return r
    return None


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _normalize_tags(tags) -> List[str]:
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,，\s]+", tags) if t.strip()]
    elif isinstance(tags, (list, tuple)):
        tags = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tags = []
    return tags[:8]


def output_dir_for(task_id: str) -> str:
    d = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(d, exist_ok=True)
    return d
