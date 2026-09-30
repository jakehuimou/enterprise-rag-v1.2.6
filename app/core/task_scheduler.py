"""
定时任务调度器
==============
轻量级后台调度线程，负责：
  - 按「单次 / 每天 / 每周 / 每月」计算任务的下次执行时间；
  - 到点后调用 RAG 问答流水线生成回答；
  - 自动把结果导出为 Word 并写入运行记录；
  - 支持通过 ``notify()`` 让调度器立即感知任务增删改。

不依赖 APScheduler / celery 等外部组件，纯标准库 + python-dateutil，
保持项目最小依赖。
"""
import os
import threading
import heapq
import calendar
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional

from dateutil.relativedelta import relativedelta

from app.core import task_store
from app.core.config import load_config
from app.utils.export_builder import build_docx, export_filename


def _parse_dt(s: str) -> Optional[datetime]:
    """把前端输入的 ``2026-09-24 09:12`` 解析为 datetime（无时区）。"""
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


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _compute_next_run(task: Dict, after: datetime) -> Optional[datetime]:
    """计算某任务在 ``after`` 之后的下一次应执行时间。"""
    start_at = _parse_dt(task.get("start_at") or "")
    if not start_at:
        return None
    valid_until = _parse_dt(task.get("valid_until") or "")
    if valid_until and start_at > valid_until:
        return None

    stype = task.get("schedule_type") or "once"

    def _clamp_valid(candidate: datetime) -> Optional[datetime]:
        if valid_until and candidate > valid_until:
            return None
        return candidate

    if stype == "once":
        # 允许 60 秒以内的「刚过点」任务立即补跑一次
        if start_at >= after - timedelta(seconds=60):
            return _clamp_valid(start_at)
        return None

    t = start_at.time()

    if stype == "daily":
        cand = datetime.combine(after.date(), t)
        if cand < after:
            cand += timedelta(days=1)
        return _clamp_valid(cand)

    if stype == "weekly":
        target_wd = start_at.weekday()
        days_ahead = (target_wd - after.weekday()) % 7
        if days_ahead == 0 and after.time() > t:
            days_ahead = 7
        cand = datetime.combine(after.date() + timedelta(days=days_ahead), t)
        # 若被有效期截断，继续找下一个周期可能也超期，这里直接截断
        if valid_until and cand > valid_until:
            return None
        return cand

    if stype == "monthly":
        target_day = start_at.day

        def _month_candidate(year: int, month: int) -> datetime:
            last = calendar.monthrange(year, month)[1]
            day = min(target_day, last)
            return datetime.combine(datetime(year, month, day).date(), t)

        cand = _month_candidate(after.year, after.month)
        if cand < after:
            nxt = after + relativedelta(months=1)
            cand = _month_candidate(nxt.year, nxt.month)
        return _clamp_valid(cand)

    return None


def _task_next_run(task: Dict, now: datetime) -> Optional[datetime]:
    """包装：未启用 / 一次性已过期 返回 None。"""
    if not task.get("enabled"):
        return None
    return _compute_next_run(task, now)


class TaskScheduler:
    """后台调度线程。"""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        self._wakeup = threading.Event()
        self._exec_lock = threading.Lock()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._wakeup.clear()
        self._thread = threading.Thread(target=self._loop, name="task-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._shutdown.set()
        self._wakeup.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def notify(self):
        """任务配置发生变化时唤醒调度线程重新计算。"""
        self._wakeup.set()

    # -----------------------------------------------------------------------
    # 主循环
    # -----------------------------------------------------------------------

    def _loop(self):
        while not self._shutdown.is_set():
            now = datetime.now()
            tasks = task_store.load_tasks()
            # 构建 (next_run, task_id, task) 小根堆，只保留有下次执行时间的任务
            heap: List[tuple] = []
            for t in tasks:
                nxt = _task_next_run(t, now)
                if nxt:
                    heapq.heappush(heap, (nxt, t.get("id"), t))
                    # 同步写回 next_run_at，方便 UI 展示
                    if t.get("next_run_at") != _fmt_dt(nxt):
                        task_store.patch_task(t["id"], {"next_run_at": _fmt_dt(nxt)})
                else:
                    if t.get("next_run_at"):
                        task_store.patch_task(t["id"], {"next_run_at": None})

            if not heap:
                # 没有任何可执行任务，睡 60 秒或直到被唤醒
                self._wakeup.wait(60)
                self._wakeup.clear()
                continue

            nxt_run, task_id, _ = heap[0]
            wait = (nxt_run - datetime.now()).total_seconds()
            if wait > 0:
                # 最多一次睡 60 秒，避免长时间无法响应 notify
                slept = self._wakeup.wait(min(wait, 60))
                self._wakeup.clear()
                if slept:
                    continue

            # 到点：找出所有已到期（含 60 秒内刚过点）的任务依次执行
            now2 = datetime.now()
            due = []
            while heap:
                when, tid, tsk = heapq.heappop(heap)
                if when <= now2 + timedelta(seconds=60):
                    due.append((when, tsk))
                else:
                    break
            for _, tsk in due:
                if self._shutdown.is_set():
                    break
                with self._exec_lock:
                    self._execute(tsk)

    # -----------------------------------------------------------------------
    # 执行单条任务
    # -----------------------------------------------------------------------

    def _execute(self, task: Dict):
        task_id = task["id"]
        task_name = task.get("name") or "未命名任务"
        started = datetime.now()
        run = {
            "task_id": task_id,
            "task_name": task_name,
            "started_at": _fmt_dt(started),
            "finished_at": None,
            "status": "running",
            "answer": "",
            "sources": [],
            "output_path": None,
            "error_message": None,
        }
        # 先占个位，让 UI 能看到「执行中」
        run = task_store.add_run(run)

        try:
            from app.core.rag import get_pipeline
            cfg = load_config()
            pipe = get_pipeline(cfg)
            model = task.get("model") or cfg.chat_model
            if not task.get("prompt"):
                raise ValueError("任务提示词为空")
            answer, sources = pipe.answer(
                task["prompt"],
                history=[],
                model=model,
                session_id=None,
            )
            run["answer"] = answer
            run["sources"] = sources or []
            run["status"] = "success"

            # 生成 Word 报告（放在 data/tasks/outputs/<task_id>/<run_id>.docx）
            try:
                out_dir = task_store.output_dir_for(task_id)
                safe_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in task_name)[:30]
                fname = f"{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
                out_path = os.path.join(out_dir, fname)
                data = build_docx(title=task_name, content=answer)
                with open(out_path, "wb") as f:
                    f.write(data)
                run["output_path"] = out_path
            except Exception as e:
                # 导出失败不影响主记录
                run["error_message"] = f"回答已生成，但 Word 导出失败：{e}"

        except Exception as e:
            run["status"] = "error"
            run["error_message"] = str(e)[:1000]

        run["finished_at"] = _fmt_dt(datetime.now())
        # 更新运行记录
        _update_run(run)

        # 更新任务的最后执行时间、下次执行时间
        patch = {"last_run_at": run["started_at"]}
        if task.get("schedule_type") == "once":
            patch["enabled"] = False
            patch["status"] = "paused"
            patch["next_run_at"] = None
        else:
            nxt = _compute_next_run(task, datetime.now())
            patch["next_run_at"] = _fmt_dt(nxt) if nxt else None
            if not nxt:
                patch["enabled"] = False
                patch["status"] = "paused"
        task_store.patch_task(task_id, patch)

    def run_now(self, task_id: str) -> Optional[Dict]:
        """手动立即执行一次任务，返回运行记录。"""
        task = task_store.get_task(task_id)
        if not task:
            return None
        with self._exec_lock:
            self._execute(task)
        # 返回该任务的最新一条记录
        return next((r for r in task_store.get_runs(task_id=task_id) if r.get("task_name") == task.get("name")), None)


def _update_run(run: Dict):
    runs = task_store.load_runs()
    for i, r in enumerate(runs):
        if r.get("id") == run.get("id"):
            runs[i] = run
            task_store.save_runs(runs)
            return
    # 兜底：没找到则插入
    runs.insert(0, run)
    task_store.save_runs(runs)


# 全局单例
_scheduler: Optional[TaskScheduler] = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> TaskScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = TaskScheduler()
        return _scheduler
