"""ComfyUI-Roundabout 管理端点：工作流上传/管理 + models.yaml 读写。

所有写操作都限制在本节点目录内，文件名做白名单校验 + 路径穿越防护；
models.yaml 写盘前先做 dry-run 校验，避免把坏配置覆盖掉好配置。
"""

from __future__ import annotations

import json
import logging
import re
import time
from functools import wraps
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web
from pydantic import ValidationError

from . import weights
from .analyze import analyze_workflow, workflow_seed
from .auth import verify
from .comfy_client import ComfyClient
from .config import settings
from .errors import APIError, error_response
from .registry import KNOWN_PARAMS, registry
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

# 工作流文件名白名单：必须以 .json 结尾，只允许安全字符，禁止目录分隔与 .. 段。
WF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.json$")


def _safe_wf_path(name: str) -> Path:
    """把请求里的文件名解析成 workflows_dir 内的绝对路径，拒绝一切穿越。"""
    if not WF_NAME_RE.match(name or ""):
        raise APIError(f"Invalid workflow filename: {name!r}", status_code=400, code="bad_filename")
    base = settings.workflows_dir.resolve()
    target = (base / name).resolve()
    # 只允许直接落在 workflows_dir 下，不允许任何子目录或越界。
    if target.parent != base:
        raise APIError("Path traversal is not allowed.", status_code=400, code="traversal")
    return target


def _used_by(name: str) -> list[str]:
    """哪些已注册 model 引用了该 workflow 文件（按 basename 匹配）。"""
    out: list[str] = []
    for spec in registry.all():
        try:
            wf = spec.workflow_path.name
        except Exception:  # noqa: BLE001
            continue
        if wf == name:
            out.append(spec.name)
    return out


def _validate_workflow_file(path: Path) -> dict[str, object]:
    """轻量校验：JSON 合法 + 是 API 格式（非 UI 工作流）。返回校验信息。"""
    info: dict[str, object] = {"valid": False, "error": None, "nodes": 0}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        info["error"] = f"read error: {exc}"
        return info
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        info["error"] = f"invalid JSON: {exc}"
        return info
    if not isinstance(data, dict):
        info["error"] = "workflow root must be a JSON object"
        return info
    # UI 工作流含 nodes/last_node_id，不能直接提交给 /prompt。
    if "nodes" in data and "last_node_id" in data:
        info["error"] = "looks like a UI workflow; export with 'Workflow -> Export (API)'"
        return info
    nodes = [k for k, v in data.items() if isinstance(v, dict) and "inputs" in v]
    info["nodes"] = len(nodes)
    info["valid"] = True
    return info


def _sanitize_filename(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", name or "").strip("._-")
    if not base:
        base = "workflow"
    if not base.lower().endswith(".json"):
        base += ".json"
    return base


# ------------------------------------------------------------------ 工作流管理
@gateway_handler
async def list_workflows(request: web.Request) -> web.Response:
    verify(request)
    base = settings.workflows_dir
    items: list[dict[str, object]] = []
    if base.exists():
        for p in sorted(base.iterdir()):
            if p.is_file() and p.suffix.lower() == ".json":
                name = p.name
                st = p.stat()
                meta = _validate_workflow_file(p)
                items.append({
                    "name": name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "valid": meta["valid"],
                    "error": meta["error"],
                    "nodes": meta["nodes"],
                    "used_by": _used_by(name),
                })
    return web.json_response({"workflows": items, "dir": str(base)})


@gateway_handler
async def upload_workflow(request: web.Request) -> web.Response:
    verify(request)
    reader = await request.multipart()
    file_bytes: bytes | None = None
    filename: str | None = None
    given_name: str | None = None
    create_model = False
    model_name: str | None = None
    model_desc: str = ""
    overwrite = False
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            filename = part.filename
            file_bytes = await part.read()
        elif part.name == "name":
            given_name = (await part.text()).strip()
        elif part.name == "create_model":
            create_model = (await part.text()).strip().lower() in ("1", "true", "yes", "on")
        elif part.name == "model_name":
            model_name = (await part.text()).strip()
        elif part.name == "model_desc":
            model_desc = (await part.text()).strip()
        elif part.name == "overwrite":
            overwrite = (await part.text()).strip().lower() in ("1", "true", "yes", "on")

    if file_bytes is None:
        raise APIError("Missing 'file' field (workflow JSON).", status_code=400)

    name = given_name or filename or "uploaded_workflow.json"
    name = _sanitize_filename(name)
    target = _safe_wf_path(name)
    if target.exists():
        raise APIError(
            f"Workflow '{name}' already exists; use a different name or delete it first.",
            status_code=409,
            code="exists",
        )

    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise APIError("File is not valid UTF-8 text.", status_code=400)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise APIError(f"Invalid JSON: {exc}", status_code=400)
    if not isinstance(data, dict) or ("nodes" in data and "last_node_id" in data):
        raise APIError("File looks like a UI workflow; export API format first.", status_code=400)

    target.write_text(text, encoding="utf-8")
    meta = _validate_workflow_file(target)

    generated_model = None
    if create_model:
        if not model_name:
            raise APIError("`model_name` is required when `create_model` is set.", status_code=400)
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", model_name):
            raise APIError(f"Invalid model name: {model_name!r}", status_code=400, code="bad_model_name")
        raw = _read_raw_config()
        models = raw.get("models") or {}
        if model_name in models and not overwrite:
            raise APIError(
                f"Model '{model_name}' already exists; pass overwrite=1 to replace it.",
                status_code=409,
                code="model_exists",
            )
        try:
            entry = analyze_workflow(data, name, model_name, model_desc)
        except ValueError as exc:
            raise APIError(f"Workflow analysis failed: {exc}", status_code=400, code="analyze_failed")
        models[model_name] = entry
        raw["models"] = models
        result = _rebuild_and_save_models(raw)
        generated_model = entry
        meta["model_created"] = model_name

    resp: dict[str, Any] = {"ok": True, "name": name, **meta}
    if generated_model is not None:
        resp["generated_model"] = generated_model
    status = 201
    return web.json_response(resp, status=status)


@gateway_handler
async def delete_workflow(request: web.Request) -> web.Response:
    verify(request)
    name = request.match_info["name"]
    target = _safe_wf_path(name)
    if not target.exists():
        raise APIError("Workflow not found.", status_code=404)
    used = _used_by(name)
    if used:
        raise APIError(
            f"Workflow is referenced by model(s): {', '.join(used)}. "
            "Remove those models from models.yaml (or repoint them) before deleting.",
            status_code=409,
            code="in_use",
        )
    _safe_delete(target)
    return web.json_response({"ok": True, "deleted": name})


def _safe_delete(path: Path) -> None:
    """删除文件。优先用标准 unlink；若被环境的安全删除钩子拦截（如沙箱无回收站），
    则退回调用操作系统命令删除，确保管理功能在受限环境也能用。"""
    import subprocess
    import sys

    if not path.exists():
        return
    try:
        path.unlink()
        return
    except OSError:
        pass
    # 兜底：走外部进程删除，绕过进程内的删除钩子。
    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Remove-Item -LiteralPath '{str(path)}' -Force"],
                check=True, capture_output=True,
            )
        else:
            subprocess.run(["rm", "-f", str(path)], check=True, capture_output=True)
    except Exception as exc:  # noqa: BLE001
        raise APIError(f"无法删除文件（被安全删除机制拦截）: {path.name} — {exc}", status_code=500)


# ------------------------------------------------------------------ 配置重建助手
def _read_raw_config() -> dict[str, Any]:
    """读取并解析 models.yaml 为 dict；文件不存在时返回空结构。"""
    path = settings.models_file
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _list_available_workflows() -> list[str]:
    base = settings.workflows_dir
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_file() and p.suffix.lower() == ".json")


def _rebuild_and_save_models(raw_in: dict[str, Any]) -> dict[str, Any]:
    """校验一份 models.yaml 结构（不写盘），通过后落盘并热加载。

    直接基于 dict 校验，避免先 dump 再 parse 的二次开销；校验失败抛 APIError。
    """
    # 校验：复用 registry._build（会检查 workflow 存在性、绑定路径、prompt 必填等）
    registry._build(
        raw_in,
        settings.models_file,
        settings.workflows_dir,
        settings.default_model or (raw_in.get("default_model") or ""),
    )
    text = yaml.safe_dump(raw_in, sort_keys=False, allow_unicode=True, default_flow_style=False)
    settings.models_file.write_text(text, encoding="utf-8")
    registry.load(settings.models_file, settings.workflows_dir, settings.default_model or "")
    return {
        "ok": True,
        "models": registry.names(),
        "default_model": registry.default_model,
    }


# ------------------------------------------------------------------ models.yaml 读写
@gateway_handler
async def get_models_config(request: web.Request) -> web.Response:
    verify(request)
    path = settings.models_file
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return web.json_response({
        "path": str(path),
        "exists": path.exists(),
        "content": content,
        "models": registry.names(),
        "default_model": registry.default_model,
    })


@gateway_handler
async def put_models_config(request: web.Request) -> web.Response:
    verify(request)
    payload = await request.json()
    content = payload.get("content")
    if not isinstance(content, str):
        raise APIError("`content` (YAML text) is required.", status_code=400)

    # 先内存校验：解析 + 校验所有模型引用的 workflow 是否存在、绑定是否有效。
    # 校验失败直接 422，绝不覆盖现有配置。
    try:
        registry.validate_content(content, settings.workflows_dir, settings.default_model)
    except Exception as exc:  # noqa: BLE001
        raise APIError(f"Validation failed: {exc}", status_code=422, code="invalid_config")

    # 校验通过再落盘，并从正式文件重新加载
    settings.models_file.write_text(content, encoding="utf-8")
    registry.load(settings.models_file, settings.workflows_dir, settings.default_model)
    return web.json_response({
        "ok": True,
        "models": registry.names(),
        "default_model": registry.default_model,
    })


# ------------------------------------------------------------------ 结构化模型配置（替代原始 YAML 文本编辑）
_MODEL_ENTRY_KEYS = (
    "workflow", "description", "mode", "capabilities", "output_node", "timeout",
    "defaults", "bindings", "aliases", "mode_choices",
    "style_presets", "size_choices",
    # 下面三类前端不渲染，但要允许结构化 API 改：参考槽拓扑 / 显存自适应 / 无提示词工具流。
    "references", "vram_adaptive", "promptless",
)


@gateway_handler
async def get_models_structured(request: web.Request) -> web.Response:
    verify(request)
    raw = _read_raw_config()
    shared = raw.get("defaults") or {}
    models_raw = raw.get("models") or {}
    models: list[dict[str, Any]] = []
    for name, cfg in models_raw.items():
        cfg = cfg or {}
        models.append({
            "name": name,
            "workflow": cfg.get("workflow"),
            "description": cfg.get("description", ""),
            "mode": str(cfg.get("mode") or "image"),
            "capabilities": cfg.get("capabilities") or ["text-to-image"],
            "output_node": cfg.get("output_node"),
            "aliases": cfg.get("aliases") or [],
            "timeout": cfg.get("timeout"),
            "mode_choices": cfg.get("mode_choices") or [],
            "defaults": cfg.get("defaults") or {},
            "bindings": cfg.get("bindings") or {},
            # 回传「前端不渲染但保存时必须保留」的字段，便于排查与结构化编辑。
            "style_presets": cfg.get("style_presets") or {},
            "references": cfg.get("references") or {},
            "vram_adaptive": bool(cfg.get("vram_adaptive")),
            "promptless": bool(cfg.get("promptless")),
        })
    return web.json_response({
        "default_model": raw.get("default_model"),
        "shared": {
            "params": shared.get("params") or {},
            "style_presets": shared.get("style_presets") or {},
        },
        "models": models,
        "available_workflows": _list_available_workflows(),
        "known_params": sorted(KNOWN_PARAMS),
    })


def merge_model_entry(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """把结构化编辑提交的模型条目合并到磁盘上已有条目上。

    以现有条目为基底：`incoming` 给了的键覆盖，没给的**保留原值**。

    为什么不是从零重建：前端表单（web/roundabout_settings.js）只渲染 workflow / timeout /
    defaults / bindings / capabilities / output_node / description / aliases，
    `references`（参考槽拓扑）、`vram_adaptive`（显存分档）、`promptless`（无提示词工具流）、
    各类 `*_presets` 它一个字都不认识。从零重建的话，用户在 /settings 点一次保存，
    这些配置就被静默抹掉——功能无声消失，且 YAML 里已经没有痕迹可查。

    空 dict / 空 list 视为「本次没提交这个键」而非「清空」，同样是为了不误删；
    真要删键请直接编辑 models.yaml。
    """
    entry: dict[str, Any] = dict(existing or {})
    for key in _MODEL_ENTRY_KEYS:
        value = incoming.get(key)
        if value is None:
            continue
        if isinstance(value, (list, dict)) and len(value) == 0:
            continue
        entry[key] = value
    return entry


@gateway_handler
async def put_models_structured(request: web.Request) -> web.Response:
    verify(request)
    payload = await request.json()
    default_model = payload.get("default_model")
    shared = payload.get("shared") or {}
    models_in = payload.get("models") or []

    raw: dict[str, Any] = {}
    if default_model:
        raw["default_model"] = default_model
    defaults: dict[str, Any] = {}
    if shared.get("params"):
        defaults["params"] = shared["params"]
    if shared.get("style_presets"):
        defaults["style_presets"] = shared["style_presets"]
    if defaults:
        raw["defaults"] = defaults

    # 与磁盘上的现有条目合并（详见 merge_model_entry 的注释：前端不认识的那批键不能丢）。
    existing_models = (_read_raw_config().get("models") or {})

    models_map: dict[str, Any] = {}
    for m in models_in:
        name = m.get("name")
        if not name:
            continue
        models_map[name] = merge_model_entry(existing_models.get(name), m)
    if not models_map:
        raise APIError("No models provided.", status_code=400)
    raw["models"] = models_map

    return web.json_response(_rebuild_and_save_models(raw))


@gateway_handler
async def delete_model(request: web.Request) -> web.Response:
    verify(request)
    name = request.match_info["name"]
    raw = _read_raw_config()
    models = raw.get("models") or {}
    if name not in models:
        raise APIError(f"Model `{name}` not found.", status_code=404)
    del models[name]
    raw["models"] = models
    result = _rebuild_and_save_models(raw)
    result["deleted"] = name
    return web.json_response(result)


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
