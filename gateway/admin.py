"""ComfyUI-Roundabout 管理端点（只读 + reload）。

v1.40.0 起设置面板退役：工作流/models.yaml 的写端点已删（配置一律走
git 管理 + 测试护栏），这里只剩状态、队列监控、权重体检与调用面查询。
"""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any

import yaml
from aiohttp import web
from pydantic import ValidationError

from . import toolinfo, weights
from .analyze import workflow_seed
from .auth import verify
from .comfy_client import ComfyClient
from .config import settings
from .errors import APIError, error_response
from .registry import registry
from .tasks import task_store

log = logging.getLogger("roundabout.admin")

# 队列监控复用一个 ComfyClient（与 handlers 相同指向，aiohttp 会话进程内共享）
_comfy = ComfyClient(settings.comfy_base_url)


# ------------------------------------------------------------------ 异常兜底（对齐 handlers.gateway_handler）
def gateway_handler(fn):
    @wraps(fn)
    async def wrapper(request: web.Request):
        try:
            return await fn(request)
        except APIError as exc:
            return error_response(exc)
        except ValidationError as exc:
            first = (exc.errors() or [{}])[0]
            loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body",))
            return error_response(
                APIError(f"{loc or 'request'}: {first.get('msg', 'invalid request')}", status_code=400, param=loc or None)
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled admin error: %s", exc)
            return error_response(APIError("Internal server error.", status_code=500, code="internal_error"))

    return wrapper

@gateway_handler
async def get_tool_info(request: web.Request) -> web.Response:
    """完整调用结构（模型 × 字段生效性 + 全局限制）。

    与 MCP 的 `get_tool_info` 同源（同在 `gateway/toolinfo.py`，输出分别取 `build` /
    `compact`）：这里默认给完整版供前端渲染表单，那边给裁剪版供 agent 选型。
    `?view=compact` 可切到裁剪版；`&model=<名>` 限定单模型，`&fields=0` 省掉字段清单。
    """
    verify(request)
    if (request.query.get("view") or "").lower() == "compact":
        return web.json_response(toolinfo.compact(
            model=request.query.get("model") or None,
            include_fields=(request.query.get("fields") or "1") != "0",
        ))
    return web.json_response(toolinfo.build())


@gateway_handler
async def admin_state(request: web.Request) -> web.Response:
    verify(request)
    return web.json_response({
        "models_file": str(settings.models_file),
        "workflows_dir": str(settings.workflows_dir),
        "auth_enabled": settings.auth_enabled,
        "models": registry.names(),
        "default_model": registry.default_model,
    })


# ------------------------------------------------------------------ 队列监控
def _seed_preferred_paths() -> list[str]:
    """各模型 `bindings.seed` 的路径（网关实际写 seed 的位置），供 workflow_seed 优先匹配。

    bindings 的值是路径列表（一个语义参数可绑定多处），这里展平去重。
    """
    paths: list[str] = []
    for spec in registry.all():
        for path in (spec.bindings or {}).get("seed") or ():
            if path and path not in paths:
                paths.append(path)
    return paths


def _queue_item_summary(item: list | tuple, now: float) -> dict[str, Any] | None:
    """把 ComfyUI /queue 的一个条目 [number, prompt_id, workflow, extra_data, outputs_to_execute] 压成紧凑摘要。

    时间取自 extra_data.create_time（ComfyUI 入队时写入的毫秒时间戳），据此算出
    已排队/已执行时长；不返回整份 workflow（体积大），弹窗查看时走 queue_workflow 端点。
    """
    if not isinstance(item, (list, tuple)) or len(item) < 4:
        return None
    number, prompt_id, workflow, extra_data = item[0], item[1], item[2], item[3]
    outputs = item[4] if len(item) > 4 else []
    create_ms = None
    if isinstance(extra_data, dict):
        create_ms = extra_data.get("create_time")
    created = (create_ms / 1000.0) if isinstance(create_ms, (int, float)) and create_ms else None
    return {
        "number": number,
        "prompt_id": prompt_id,
        "created": created,
        "elapsed": round(now - created, 1) if created else None,
        "node_count": len(workflow) if isinstance(workflow, dict) else None,
        "seed": workflow_seed(workflow, _seed_preferred_paths()),
        "outputs_to_execute": outputs if isinstance(outputs, list) else [],
    }


def _task_summary(t, now: float) -> dict[str, Any]:
    """网关异步任务 → 面板摘要（含 prompt_id、seed 与是否可查 workflow）。"""
    return {
        "id": t.id,
        "status": t.status,
        "code": t.code,
        "model": t.model,
        "request_id": t.request_id,
        "prompt_id": t.prompt_id,
        # 从提交时的工作流快照读取——未提交（无快照）时为 None
        "seed": workflow_seed(t.workflow, _seed_preferred_paths()),
        "has_workflow": t.workflow is not None,
        "created": t.created_at,
        "elapsed": round(now - t.created_at, 1),
    }


@gateway_handler
async def queue_status(request: web.Request) -> web.Response:
    """网关层队列监控：ComfyUI 执行队列（running/pending）+ 网关异步任务表合并视图。"""
    verify(request)
    now = time.time()
    try:
        info = await _comfy.queue()
    except APIError as exc:
        # ComfyUI 不可达时仍返回任务表，队列部分标记不可用
        return web.json_response({
            "server_time": now,
            "comfy_reachable": False,
            "comfy_error": exc.message,
            "running": [],
            "pending": [],
            "tasks": [_task_summary(t, now) for t in task_store.snapshot()],
        })
    running = [s for s in (_queue_item_summary(x, now) for x in (info.get("queue_running") or [])) if s]
    pending = [s for s in (_queue_item_summary(x, now) for x in (info.get("queue_pending") or [])) if s]
    return web.json_response({
        "server_time": now,
        "comfy_reachable": True,
        "running": running,
        "pending": pending,
        "running_count": len(running),
        "pending_count": len(pending),
        "tasks": [_task_summary(t, now) for t in task_store.snapshot()],
    })


@gateway_handler
async def queue_workflow(request: web.Request) -> web.Response:
    """按 prompt_id 取出该任务的工作流 JSON（弹窗查看用），三层查找：

    1. ComfyUI 当前队列（running/pending）——正在排/正在跑的任务；
    2. ComfyUI history——已出队（成功/失败/中断）的任务，条目 ``prompt[2]`` 即完整 workflow；
    3. 网关异步任务表快照——按 task id 查（提交成功后 attach_prompt 记录的工作流）。
    覆盖成功与失败任务，均能弹窗查看。
    """
    verify(request)
    prompt_id = request.match_info["prompt_id"]
    info = await _comfy.queue()
    for key in ("queue_running", "queue_pending"):
        for item in info.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) > 2 and item[1] == prompt_id:
                workflow = item[2]
                return web.json_response({
                    "prompt_id": prompt_id,
                    "status": "running" if key == "queue_running" else "pending",
                    "workflow": workflow if isinstance(workflow, dict) else None,
                    "source": "queue",
                })
    # ---- 2. history（已出队任务：成功/失败/中断都保留原始 prompt）----
    entry = await _comfy.history(prompt_id)
    if entry:
        prompt = entry.get("prompt")
        wf = prompt[2] if isinstance(prompt, (list, tuple)) and len(prompt) > 2 else None
        status = (entry.get("status") or {}).get("status_str", "unknown")
        if isinstance(wf, dict):
            return web.json_response({
                "prompt_id": prompt_id,
                "status": status,
                "workflow": wf,
                "source": "history",
            })
    # ---- 3. 网关异步任务快照（按 task id 查；提交前/提交后失败都能看）----
    task = task_store.get(prompt_id)
    if task is not None and task.workflow is not None:
        return web.json_response({
            "prompt_id": prompt_id,
            "status": task.status,
            "workflow": task.workflow,
            "source": "task",
        })
    raise APIError(
        f"Prompt/task `{prompt_id}` not found in queue, history or task store.",
        status_code=404,
        code="prompt_not_in_queue",
    )


# ------------------------------------------------------------------ 权重体检
@gateway_handler
async def weights_status(request: web.Request) -> web.Response:
    """权重体检：内置工作流引用的权重里，当前缺哪些 + 每条的下载命令。

    只读：不触发生成、不占 GPU，只对每个文件做一次 isfile。查询参数
    `?unreferenced=1` 附带当前无工作流引用的条目，`?mirror=modelscope` 换下载源。
    """
    unref = (request.query.get("unreferenced") or "").strip().lower() in ("1", "true", "yes")
    mirror = (request.query.get("mirror") or "hf").strip() or "hf"
    report = weights.check(include_unreferenced=unref, mirror=mirror)
    log.info("weights check: %s", weights.summary_line(report))
    return web.json_response(report)
