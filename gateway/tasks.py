"""生成任务存储（进程内内存表）。

存两类任务：异步视频任务（POST 立即返回 id，后台生成，客户端轮询）与同步任务
（图片、同步视频——结果已随 HTTP 响应返回，这里只留一条记录，供视图页显示痕迹与产物）。

为什么异步视频需要它：视频生成耗时很长（数十秒到数分钟），若让 HTTP 请求同步 await
到出片，agent 端 / 中间代理极易超时断连。改为异步模式后，POST 立即返回任务 id，后台
asyncio 任务继续生成，客户端轮询 GET /v1/videos/tasks/{id} 取结果。

注意：任务表是进程内存态，**重启 ComfyUI 后未完成任务会丢失**（视频本身也丢了，客户端
可凭 task_id 缺失判定失败并重试）。这是有意为之——避免引入持久化依赖，且视频生成本就是
一次性、可重试的工作负载。如未来需要跨重启存活，可在此替换为 Redis / 数据库后端。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 任务状态机：queued -> processing -> succeeded | failed
_ACTIVE = ("queued", "processing")

# 记录保留时长。同步生图也入库后条目数随使用量线性增长，必须回收；取 6 小时——远大于
# 任何客户端等待单个任务的时间，又能让长时间运行后内存回落。
RETENTION = 6 * 3600.0


@dataclass
class Task:
    id: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    model: str | None = None
    request_id: str | None = None
    prompt_id: str | None = None  # 提交后回填的 ComfyUI prompt_id（失败/成功都记录，供查询 workflow）
    workflow: dict[str, Any] | None = None  # 提交时的工作流快照（失败后仍可弹窗查看）
    result: dict[str, Any] | None = None  # 成功时的 VideoResponse.model_dump
    error: str | None = None
    code: int | None = None


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def create(self, task_id: str, *, model: str | None = None, request_id: str | None = None) -> Task:
        task = Task(id=task_id, status="queued", model=model, request_id=request_id)
        self._tasks[task_id] = task
        # 顺手回收旧记录：建任务是最自然的清理时机，无需额外的定时器
        self.sweep(RETENTION)
        return task

    def mark_processing(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            task.status = "processing"

    def attach_prompt(self, task_id: str, prompt_id: str, workflow: dict[str, Any] | None) -> None:
        """提交成功后回填 ComfyUI prompt_id 与工作流快照（失败任务也能弹窗查看 workflow）。"""
        task = self._tasks.get(task_id)
        if task is not None:
            task.prompt_id = prompt_id
            if workflow is not None:
                task.workflow = workflow

    def cancel(self, task_id: str) -> bool:
        """标记任务为已取消；仅活跃任务可取消，已终态/不存在返回 False。

        取消后 `complete` / `fail` 不再覆盖状态——后台生成协程在被中断后仍会
        走到失败分支，若不守卫会把 cancelled 冲掉。
        """
        task = self._tasks.get(task_id)
        if task is None or task.status not in _ACTIVE:
            return False
        task.status = "cancelled"
        task.error = "Task cancelled by client."
        task.code = 499
        return True

    def complete(self, task_id: str, result: dict[str, Any]) -> None:
        task = self._tasks.get(task_id)
        if task is not None and task.status != "cancelled":
            task.status = "succeeded"
            task.result = result

    def fail(self, task_id: str, error: str, code: int | None = None) -> None:
        task = self._tasks.get(task_id)
        if task is not None and task.status != "cancelled":
            task.status = "failed"
            task.error = error
            task.code = code

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def snapshot(self) -> list[Task]:
        """返回全部任务（按创建时间倒序），供队列监控/管理面板使用。"""
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    def sweep(self, ttl: float = 3600.0) -> int:
        """清理已终态且超过 ttl 的任务，防止内存无限增长。"""
        if ttl <= 0:
            return 0
        cutoff = time.time() - ttl
        dead = [
            tid
            for tid, t in self._tasks.items()
            if t.status not in _ACTIVE and t.created_at < cutoff
        ]
        for tid in dead:
            del self._tasks[tid]
        return len(dead)


task_store = TaskStore()
