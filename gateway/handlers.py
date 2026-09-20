"""aiohttp 路由处理器：把 OpenAI 风格的请求转成 ComfyUI 工作流生成。

所有路由在节点 __init__.py 中通过 web.RouteTableDef 登记到 PromptServer。
响应体对齐 OpenAI Images / Videos 规范（video 为扩展端点，结构沿用 images）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from functools import wraps
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from .auth import verify
from .comfy_client import ComfyClient
from .config import settings
from .errors import APIError, error_response
from .pipeline import generate, generate_video, new_request_id
from .registry import registry
from .tasks import task_store
from .schemas import (
    ImageGenerationRequest,
    ModelCard,
    ModelList,
    VideoGenerationRequest,
)
from .store import store

log = logging.getLogger("roundabout.handlers")

BOOT_TS = int(time.time())
SERVICE_NAME = "comfyui-roundabout"

# 网关即 ComfyUI 自身，后端默认指向本机 loopback（见 config._default_comfy_base_url）
comfy = ComfyClient(settings.comfy_base_url)


# ------------------------------------------------------------------ 异常兜底
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
            log.exception("unhandled error: %s", exc)
            return error_response(APIError("Internal server error.", status_code=500, code="internal_error"))

    return wrapper


# ------------------------------------------------------------------ helpers
def _absolutize(payload: dict[str, Any], request: web.Request) -> dict[str, Any]:
    """补全相对 url：优先用 PUBLIC_BASE_URL，否则回落到请求自身的 host。

    产物可能来自 ComfyUI 原生 /view（网关即 ComfyUI 进程内，同端口）或网关缓存
    /v1/.../files，二者都是相对路径，统一在此补全为绝对 http 地址。
    """
    base = settings.public_base_url or str(request.url.origin())
    for item in payload.get("data", []):
        url = item.get("url")
        if url and url.startswith("/"):
            item["url"] = base + url
    return payload


async def _parse_json(request: web.Request) -> dict[str, Any]:
    try:
        return await request.json()
    except Exception as exc:  # noqa: BLE001
        raise APIError(f"Invalid JSON body: {exc}", status_code=400, param="body")


def _int_or_none(v: str | None):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _float_or_none(v: str | None):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _new_task(model: str | None) -> tuple[str, str]:
    task_id = uuid.uuid4().hex
    request_id = new_request_id()
    task_store.create(task_id, model=model, request_id=request_id)
    task_store.mark_processing(task_id)
    return task_id, request_id


def _record_failure(task_id: str, exc: Exception) -> None:
    message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
    task_store.fail(task_id, message, getattr(exc, "status_code", None))


async def generate_tracked(
    req: ImageGenerationRequest,
    *,
    image_inputs: list[bytes] | None = None,
    mask_input: bytes | None = None,
):
    """同步生图，同时在任务表留一条记录。

    同步请求本身不需要任务表（结果直接从 HTTP 响应返回），但视图页只看得见任务表——
    不记的话 agent 用 MCP / REST 生成完，页面上既没有痕迹也没有产物入口。
    """
    task_id, request_id = _new_task(req.model)
    try:
        result = await generate(
            req, comfy, request_id=request_id, image_inputs=image_inputs, mask_input=mask_input
        )
    except Exception as exc:  # noqa: BLE001
        _record_failure(task_id, exc)
        raise
    task_store.complete(task_id, result.model_dump(exclude_none=True))
    return result


async def generate_video_tracked(req: VideoGenerationRequest):
    """同步生视频，同时在任务表留一条记录（异步链路自带记录，见 _run_video_task）。"""
    task_id, request_id = _new_task(req.model)
    try:
        result = await generate_video(req, comfy, request_id=request_id)
    except Exception as exc:  # noqa: BLE001
        _record_failure(task_id, exc)
        raise
    task_store.complete(task_id, result.model_dump(exclude_none=True))
    return result


# ------------------------------------------------------------------ 业务路由
@gateway_handler
async def images_generations(request: web.Request) -> web.Response:
    """文生图（也支持通过扩展字段 `image` 走图生图）。"""
    verify(request)
    body = await _parse_json(request)
    req = ImageGenerationRequest.model_validate(body)
    # Hermes 等客户端可能把视频模型也统一打到 images 端点。识别出视频模型后转视频链路，
    # 否则会被当成图片模型处理——图片请求没有 reference_images 等字段，会在接线阶段触发
    # `ImageGenerationRequest has no attribute 'reference_images'` 的 AttributeError。
    spec = registry.resolve(req.model)
    if spec.is_video:
        vreq = VideoGenerationRequest.model_validate(body)
        result = await generate_video_tracked(vreq)
        return web.json_response(_absolutize(result.model_dump(exclude_none=True), request))
    result = await generate_tracked(req)
    return web.json_response(_absolutize(result.model_dump(exclude_none=True), request))


@gateway_handler
async def images_edits(request: web.Request) -> web.Response:
    """OpenAI 标准的图生图端点（multipart/form-data）。"""
    verify(request)
    reader = await request.multipart()
    fields: dict[str, str] = {}
    images: list[bytes] = []
    mask: bytes | None = None
    while True:
        part = await reader.next()
        if part is None:
            break
        name = part.name
        if name == "image":
            images.append(await part.read(decode=False))
        elif name == "mask":
            mask = await part.read(decode=False)
        else:
            fields[name] = await part.text()

    req = ImageGenerationRequest(
        prompt=fields.get("prompt"),
        model=fields.get("model"),
        n=_int_or_none(fields.get("n")) or 1,
        size=fields.get("size"),
        response_format=fields.get("response_format"),  # type: ignore[arg-type]
        user=fields.get("user"),
        negative_prompt=fields.get("negative_prompt"),
        seed=_int_or_none(fields.get("seed")),
        steps=_int_or_none(fields.get("steps")),
        cfg=_float_or_none(fields.get("cfg")),
        denoise=_float_or_none(fields.get("denoise")),
    )
    result = await generate_tracked(
        req,
        image_inputs=images or None,
        mask_input=mask,
    )
    return web.json_response(_absolutize(result.model_dump(exclude_none=True), request))


# 默认去背景模型（promptless 工具类工作流，BiRefNet matting）。
# 也接受 multipart `model` 字段覆盖，方便以后挂别的 utility 工作流。
REMOVE_BG_MODEL = "utility-birefnet-remove-background"


@gateway_handler
async def images_remove_background(request: web.Request) -> web.Response:
    """独立去背景端点（multipart/form-data）：无 prompt，只需 image。

    与 /v1/images/edits 的区别：不收 prompt / 采样参数，模型固定为
    utility-birefnet-remove-background（可用 `model` 字段覆盖为其他 promptless 模型）。
    返回结构与 images 端点一致（response_format=url/b64_json/path）。
    """
    verify(request)
    reader = await request.multipart()
    fields: dict[str, str] = {}
    images: list[bytes] = []
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "image":
            images.append(await part.read(decode=False))
        else:
            fields[part.name] = await part.text()

    model = fields.get("model") or REMOVE_BG_MODEL
    spec = registry.resolve(model)
    if spec.is_video or not spec.promptless:
        raise APIError(
            f"Model `{spec.name}` is not a promptless utility model; "
            "this endpoint only serves background-removal style workflows.",
            param="model",
        )
    req = ImageGenerationRequest(
        model=model,
        n=_int_or_none(fields.get("n")) or 1,
        response_format=fields.get("response_format"),  # type: ignore[arg-type]
        user=fields.get("user"),
        filename_prefix=fields.get("filename_prefix"),
    )
    result = await generate_tracked(req, image_inputs=images or None)
    return web.json_response(_absolutize(result.model_dump(exclude_none=True), request))


@gateway_handler
async def videos_generations(request: web.Request) -> web.Response:
    """文生视频 / 图生视频（扩展端点，结构对齐 OpenAI images）。

    支持异步模式：body 带 "async": true 时立即返回 {"id","status":"queued"}，
    后台 asyncio 任务继续生成；客户端轮询 GET /v1/videos/tasks/{id} 取结果。
    解决长时间生成导致的 agent / 代理连接超时。默认仍为同步返回（向后兼容）。
    """
    verify(request)
    body = await _parse_json(request)
    req = VideoGenerationRequest.model_validate(body)

    if req.is_async:
        task_id = uuid.uuid4().hex
        request_id = new_request_id()
        task_store.create(task_id, model=req.model, request_id=request_id)
        log.info("req=%s async task=%s model=%s queued", request_id, task_id, req.model)
        asyncio.create_task(_run_video_task(task_id, req, request_id))
        # 对齐 OpenAI 异步范式：返回 image_generation.task 对象（status: pending）
        return web.json_response({
            "id": task_id,
            "object": "image_generation.task",
            "status": "pending",
            "created_at": int(time.time()),
        })

    result = await generate_video_tracked(req)
    return web.json_response(_absolutize(result.model_dump(exclude_none=True), request))


# 内部状态机 → OpenAI 标准枚举（未知状态原样透出，如 cancelled）
_OPENAI_STATUS = {
    "queued": "pending",
    "processing": "in_progress",
    "succeeded": "completed",
    "failed": "failed",
}


def _openai_status(status: str) -> str:
    return _OPENAI_STATUS.get(status, status)


@gateway_handler
async def video_task(request: web.Request) -> web.Response:
    """查询异步任务的状态与结果（对齐 OpenAI：GET /v1/images/tasks/{id} 或 /v1/videos/tasks/{id}）。

    返回 OpenAI image_generation.task 对象：
      {"id","object":"image_generation.task","status","created_at","model",
       "output":{"data":[...]} (completed), "error":{"message","code"} (failed)}
    status 枚举对齐 OpenAI：queued→pending / processing→in_progress / succeeded→completed / failed→failed。
    output 中的相对 url 会补全为绝对地址。
    """
    task_id = request.match_info["id"]
    task = task_store.get(task_id)
    if task is None:
        return error_response(APIError("Task not found.", status_code=404, code="task_not_found"))

    payload: dict[str, Any] = {
        "id": task.id,
        "object": "image_generation.task",
        "status": _openai_status(task.status),
        "created_at": int(task.created_at),
        "model": task.model,
    }
    if task.status == "succeeded" and task.result is not None:
        # OpenAI 标准：output.data[]，每个元素含 url / b64_json
        output = {"data": task.result.get("data", [])}
        payload["output"] = _absolutize(output, request)
    elif task.status == "failed":
        payload["error"] = {"message": task.error, "code": task.code}
    return web.json_response(payload)


async def cancel_task(task_id: str) -> dict[str, Any]:
    """取消一个异步任务（与 request 无关，REST 与 MCP 共用）。

    ComfyUI 侧：pending 的按 prompt_id 从队列移除；running 的只能 interrupt
    （ComfyUI 没有按 id 中断运行中任务的接口，这会中断当前正在执行的那个）。
    本地侧：标记 cancelled，且 complete/fail 不再覆盖它。
    """
    task = task_store.get(task_id)
    if task is None:
        raise APIError("Task not found.", status_code=404, code="task_not_found")

    cancelled = task_store.cancel(task_id)
    prompt_id = task.prompt_id
    detail = "cancelled_local_only" if cancelled else "task_already_finished"

    if cancelled and prompt_id:
        try:
            info = await comfy.queue()

            def _ids(key: str) -> list[str]:
                return [
                    item[1]
                    for item in info.get(key) or []
                    if isinstance(item, (list, tuple)) and len(item) > 1
                ]

            if prompt_id in _ids("queue_pending"):
                await comfy.delete_queue([prompt_id])
                detail = "removed_from_queue"
            elif prompt_id in _ids("queue_running"):
                await comfy.interrupt()
                detail = "interrupted_running_task"
            else:
                detail = "not_in_comfy_queue"
        except Exception as exc:  # noqa: BLE001 - 尽力而为，本地取消已生效
            log.warning("cancel task=%s: ComfyUI side cancel failed: %s", task_id, exc)
            detail = f"comfy_cancel_failed: {exc}"

    return {
        "id": task.id,
        "object": "image_generation.task",
        "status": "cancelled" if cancelled else _openai_status(task.status),
        "created_at": int(task.created_at),
        "model": task.model,
        "prompt_id": prompt_id,
        "cancelled": cancelled,
        "detail": detail,
    }


@gateway_handler
async def cancel_video_task(request: web.Request) -> web.Response:
    """取消异步任务：DELETE /v1/videos/tasks/{id} 或 /v1/images/tasks/{id}。"""
    return web.json_response(await cancel_task(request.match_info["id"]))


async def _run_video_task(task_id: str, req: VideoGenerationRequest, request_id: str) -> None:
    """异步任务执行体：提交后脱离 HTTP 连接继续生成，结果写入 task_store。

    复用 generate_video 的全部逻辑（参数组装 / 参考接线 / 并发信号量 / 产物落盘），
    仅把「同步等待」移到后台协程完成。失败时捕获并标记任务失败，避免 create_task 的
    协程抛出未处理异常导致 asyncio 告警、任务卡在 processing。
    """
    task_store.mark_processing(task_id)
    try:
        result = await generate_video(
            req,
            comfy,
            request_id=request_id,
            on_submit=lambda pid, wf: task_store.attach_prompt(task_id, pid, wf),
        )
        task_store.complete(task_id, result.model_dump(exclude_none=True))
    except APIError as exc:
        task_store.fail(task_id, exc.message, exc.status_code)
    except Exception as exc:  # noqa: BLE001
        log.exception("async video task %s failed: %s", task_id, exc)
        task_store.fail(task_id, "Internal server error during generation.", 500)


@gateway_handler
async def list_models(request: web.Request) -> web.Response:
    return web.json_response(
        ModelList(data=[ModelCard(id=s.name, created=BOOT_TS) for s in registry.all()]).model_dump()
    )


@gateway_handler
async def get_model(request: web.Request) -> web.Response:
    model_id = request.match_info["model_id"]
    spec = registry.resolve(model_id)
    return web.json_response(ModelCard(id=spec.name, created=BOOT_TS).model_dump())


@gateway_handler
async def get_file(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    path = store.path(name)
    if path is None:
        return error_response(APIError("File not found or expired.", status_code=404, code="file_not_found"))
    return web.FileResponse(path)


@gateway_handler
async def get_video_file(request: web.Request) -> web.Response:
    """视频产物的 url 落盘代理（response_format=url 时返回的就是这个地址）。"""
    name = request.match_info["name"]
    path = store.path(name)
    if path is None:
        return error_response(APIError("File not found or expired.", status_code=404, code="file_not_found"))
    return web.FileResponse(path)


# ------------------------------------------------------------------ 运维路由
@gateway_handler
async def health(request: web.Request) -> web.Response:
    payload: dict[str, Any] = {
        "status": "ok",
        "service": SERVICE_NAME,
        "comfy_base_url": settings.comfy_base_url,
        "models": registry.names(),
        "default_model": registry.default_model,
        "auth": settings.auth_enabled,
    }
    try:
        stats = await comfy.ping()
        payload["comfyui"] = {
            "reachable": True,
            "version": (stats.get("system") or {}).get("comfyui_version"),
        }
    except APIError as exc:
        payload["status"] = "degraded"
        payload["comfyui"] = {"reachable": False, "error": exc.message}
    return web.json_response(payload, status=200 if payload["status"] == "ok" else 503)


@gateway_handler
async def reload_registry(request: web.Request) -> web.Response:
    """改完 models.yaml / workflow JSON 后热加载，不用重启进程。"""
    registry.load(settings.models_file, settings.workflows_dir, settings.default_model)
    return web.json_response({"reloaded": True, "models": registry.names()})
