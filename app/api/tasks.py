"""
定时任务 REST 接口
==================
提供任务 CRUD、手动触发、运行记录查询与报告下载。
"""
import os
import re
from datetime import datetime, time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.core import task_store
from app.core.task_scheduler import get_scheduler, _compute_next_run

router = APIRouter(prefix="/api/tasks", tags=["定时任务"])


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------

class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    prompt: str = Field(..., min_length=1)
    workspace: Optional[str] = "默认工作空间"
    access: Optional[str] = "full"
    schedule_type: Optional[str] = "once"
    start_at: str  # 格式：YYYY-MM-DD HH:MM 或 ISO
    valid_until: Optional[str] = None
    enabled: Optional[bool] = True
    model: Optional[str] = None
    tags: Optional[List[str]] = []


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    workspace: Optional[str] = None
    access: Optional[str] = None
    schedule_type: Optional[str] = None
    start_at: Optional[str] = None
    valid_until: Optional[str] = None
    enabled: Optional[bool] = None
    model: Optional[str] = None
    tags: Optional[List[str]] = None


class TaskOut(BaseModel):
    id: str
    name: str
    prompt: str
    workspace: str
    access: str
    schedule_type: str
    start_at: str
    valid_until: Optional[str]
    enabled: bool
    model: Optional[str]
    tags: List[str]
    created_at: str
    updated_at: str
    last_run_at: Optional[str]
    next_run_at: Optional[str]
    status: str


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------

def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_time_only(s: str) -> Optional[time]:
    """解析纯时间（09:00 或 09:00:00），失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _validate_task(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="任务名称不能为空。")
    if not prompt:
        raise HTTPException(status_code=400, detail="提示词不能为空。")
    stype = (data.get("schedule_type") or "once").lower()
    if stype not in ("once", "daily", "weekly", "monthly"):
        raise HTTPException(status_code=400, detail="执行频率仅支持：单次 / 每天 / 每周 / 每月。")

    start_raw = (data.get("start_at") or "").strip()
    start = _parse_dt(start_raw)
    if not start and stype in ("daily", "weekly", "monthly"):
        # 周期性任务允许只输入时间，如每天 09:00，自动用今天的日期补齐
        t = _parse_time_only(start_raw)
        if t:
            start = datetime.combine(datetime.now().date(), t)
    if not start:
        raise HTTPException(status_code=400, detail="执行时间格式错误，单次请使用：2026-09-24 09:12；周期任务也可只写时间：09:12")

    until = _parse_dt(data.get("valid_until")) if data.get("valid_until") else None
    if until and until < start:
        raise HTTPException(status_code=400, detail="有效期不能早于执行时间。")
    data["schedule_type"] = stype
    data["start_at"] = start.strftime("%Y-%m-%d %H:%M:%S")
    data["valid_until"] = until.strftime("%Y-%m-%d %H:%M:%S") if until else None
    return data


def _format_schedule(task: dict) -> str:
    """把任务 schedule 转换成截图中的可读文本，如「每天 08:40」。"""
    stype = task.get("schedule_type") or "once"
    start = task.get("start_at") or ""
    time_part = start[11:16] if len(start) >= 16 else ""
    date_part = start[:10] if len(start) >= 10 else ""
    mapping = {
        "once": f"单次 {date_part} {time_part}".strip(),
        "daily": f"每天 {time_part}".strip(),
        "weekly": f"每周 {time_part}".strip(),
        "monthly": f"每月 {time_part}".strip(),
    }
    return mapping.get(stype, start)


def _to_out(task: dict) -> dict:
    # 实时校正 next_run_at / status，确保 UI 展示准确
    nxt = _compute_next_run(task, datetime.now())
    task["next_run_at"] = nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None
    task["status"] = "active" if task.get("enabled") else "paused"
    return task


# ---------------------------------------------------------------------------
# 任务 CRUD
# ---------------------------------------------------------------------------

@router.get("", summary="任务列表")
def list_tasks():
    """返回所有定时任务，含实时计算的下次执行时间。"""
    return {"tasks": [_to_out(t) for t in task_store.load_tasks()]}


@router.post("", summary="创建任务", response_model=TaskOut)
def create_task(req: TaskCreate):
    data = _validate_task(req.model_dump())
    task = task_store.create_task(data)
    get_scheduler().notify()
    return _to_out(task)


@router.put("/{task_id}", summary="更新任务", response_model=TaskOut)
def update_task(task_id: str, req: TaskUpdate):
    existing = task_store.get_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail="任务不存在。")
    data = req.model_dump(exclude_unset=True)
    # 合并旧值用于校验
    merged = {**existing, **data}
    data = _validate_task(merged)
    task = task_store.update_task(task_id, data)
    get_scheduler().notify()
    return _to_out(task)


@router.delete("/{task_id}", summary="删除任务")
def delete_task(task_id: str):
    if not task_store.delete_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在。")
    get_scheduler().notify()
    return {"deleted": task_id}


@router.post("/{task_id}/run", summary="立即执行一次")
def run_task_now(task_id: str):
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    run = get_scheduler().run_now(task_id)
    if not run:
        raise HTTPException(status_code=500, detail="任务执行失败，请查看后端日志。")
    return {"run": run}


@router.post("/{task_id}/toggle", summary="启用 / 暂停")
def toggle_task(task_id: str):
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    enabled = not task.get("enabled", True)
    updated = task_store.update_task(task_id, {"enabled": enabled})
    get_scheduler().notify()
    return _to_out(updated)


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------

@router.get("/{task_id}/runs", summary="某任务的运行记录")
def list_task_runs(task_id: str, limit: int = Query(50, ge=1, le=200)):
    return {"runs": task_store.get_runs(task_id=task_id, limit=limit)}


@router.get("/runs/all", summary="全部运行记录")
def list_all_runs(limit: int = Query(50, ge=1, le=200)):
    return {"runs": task_store.get_runs(limit=limit)}


@router.get("/runs/{run_id}", summary="单条运行记录详情")
def get_run(run_id: str):
    run = task_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在。")
    return {"run": run}


@router.get("/{task_id}/output/{run_id}", summary="下载任务输出报告")
def download_output(task_id: str, run_id: str):
    run = task_store.get_run(run_id)
    if not run or run.get("task_id") != task_id:
        raise HTTPException(status_code=404, detail="运行记录不存在。")
    path = run.get("output_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="输出文件不存在或已被清理。")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=os.path.basename(path),
    )
