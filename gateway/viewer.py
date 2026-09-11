"""view.html 的后端：input/output 资源浏览 + 任务进度与产物。

用途：MCP 工具 `get_view_url` 会返回一个页面地址给用户，浏览器打开后无需任何
命令行即可查看 ComfyUI 生成/上传的素材，并实时跟踪生成任务（同步与异步都记表）。

三个端点：
  GET /roundabout/view         页面本身（读 web/view.html）
  GET /roundabout/view/files    列目录（root=input|output, path=子目录）
  GET /roundabout/view/tasks    任务表快照（前端轮询做进度与产物展示）
"""

from __future__ import annotations

import hmac
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from .admin import gateway_handler
from .auth import verify
from .comfy_client import ComfyClient
from .config import settings
from .errors import APIError
from .handlers import _OPENAI_STATUS
from .pipeline import _comfy_input_root, _comfy_output_root
from .tasks import task_store

log = logging.getLogger("roundabout.viewer")

# 队列读取复用一个 ComfyClient（aiohttp 会话进程内共享，构造本身很轻）
_comfy = ComfyClient(settings.comfy_base_url)

VIEW_PATH = "/roundabout/view"
VIEW_HTML = Path(__file__).resolve().parent.parent / "web" / "view.html"

# 文件分页：默认一页 120 个，最大 500。output 目录可能积累上万文件，全量返回会拖垮浏览器。
DEFAULT_PAGE_SIZE = 120
MAX_PAGE_SIZE = 500
# 单次扫描硬上限（只影响 total 的准确性，超出置 truncated），防止极端目录下的长时间 stat
MAX_SCAN = 20000

_ROOTS = {"input": _comfy_input_root, "output": _comfy_output_root}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"}
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}


def view_url(base: str | None = None) -> str:
    """页面对外地址：MCP 工具与日志共用，避免各处拼串不一致。"""
    return f"{(base or settings.public_base_url or settings.comfy_base_url).rstrip('/')}{VIEW_PATH}"


def _authorize(request: web.Request) -> None:
    """浏览器直接打开无法带自定义头，故额外支持 ?key=<api_key>。未启用鉴权直接放行。"""
    if not settings.auth_enabled:
        return
    key = request.query.get("key")
    if key and any(hmac.compare_digest(key, k) for k in settings.api_keys):
        return
    verify(request)


def _int_arg(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    """带上下界钳制的整数查询参数。"""
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise APIError(f"'{name}' must be an integer.", param=name)
    if value < lo:
        raise APIError(f"'{name}' must be >= {lo}.", param=name)
    return min(value, hi)


def _kind(ext: str) -> str:
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "file"


def _view_url(root: str, rel_path: str) -> str:
    """ComfyUI 原生 /view 地址（复用其自身的目录白名单与类型解析，不另开静态服务）。"""
    subfolder, _, filename = rel_path.rpartition("/")
    params = {"filename": filename, "type": root}
    if subfolder:
        params["subfolder"] = subfolder
    return f"/view?{urlencode(params)}"


def _resolve_dir(root: str, rel: str) -> tuple[Path, Path]:
    """把 root + 相对路径解析为 (根目录, 目标目录)，并拦截路径穿越。"""
    base = _ROOTS[root]()
    if not base:
        raise APIError(
            f"ComfyUI '{root}' directory is unavailable (folder_paths not resolvable).",
            status_code=503,
            code="root_unavailable",
        )
    base_path = Path(base).resolve()
    target = (base_path / rel).resolve() if rel else base_path
    if target != base_path and base_path not in target.parents:
        raise APIError("Path escapes the root directory.", code="bad_path")
    if not target.is_dir():
        raise APIError(f"Not a directory: {rel or '/'}", status_code=404, code="not_a_dir")
    return base_path, target


@gateway_handler
async def view_page(request: web.Request) -> web.Response:
    _authorize(request)
    if not VIEW_HTML.is_file():
        raise APIError(f"view.html not found at {VIEW_HTML}", status_code=404, code="view_missing")
    return web.Response(
        text=VIEW_HTML.read_text(encoding="utf-8"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@gateway_handler
async def list_dir(request: web.Request) -> web.Response:
    """列 input/output 下的一个目录：子目录（全量）+ 文件（分页）。

    分页参数：`offset`（默认 0）、`limit`（默认 120，上限 500）。
    子目录通常很少，不做分页；只有 files 切片，同时回 `total` 供前端算页数。

    排序：`sort=mtime`（默认，按修改时间，新的在前）/ `sort=name`（按文件名）；
    `order=desc`（默认）/ `order=asc`。子目录始终按名称升序排在最前。
    """
    _authorize(request)
    root = request.query.get("root", "output")
    if root not in _ROOTS:
        raise APIError(f"Unknown root '{root}'. Use 'input' or 'output'.", param="root")
    rel = (request.query.get("path") or "").strip().strip("/")
    base_path, target = _resolve_dir(root, rel)
    offset = _int_arg(request, "offset", 0, 0, 10**9)
    limit = _int_arg(request, "limit", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    sort = request.query.get("sort", "mtime")
    if sort not in ("mtime", "name"):
        raise APIError("'sort' must be 'mtime' or 'name'.", param="sort")
    order = request.query.get("order", "desc")
    if order not in ("asc", "desc"):
        raise APIError("'order' must be 'asc' or 'desc'.", param="order")

    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    truncated = False
    for i, entry in enumerate(sorted(target.iterdir(), key=lambda p: p.name.lower())):
        if i >= MAX_SCAN:
            truncated = True
            break
        if entry.name.startswith("."):
            continue
        rel_path = entry.relative_to(base_path).as_posix()
        if entry.is_dir():
            dirs.append({"name": entry.name, "path": rel_path})
            continue
        stat = entry.stat()
        ext = entry.suffix.lower()
        url = _view_url(root, rel_path)
        files.append({
            "name": entry.name,
            "path": rel_path,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "ext": ext,
            "kind": _kind(ext),
            "url": url,
            # 缩略图走 ComfyUI 的 preview 转码（webp q70），仅图片有意义
            "thumb": f"{url}&preview=webp;70" if _kind(ext) == "image" else None,
        })

    # 目录按名升序在前，文件按选定规则排（默认 mtime 倒序：刚生成的排最前）。
    # mtime 排序额外用文件名兜底，避免同一秒生成的多个文件在刷新后顺序跳动。
    reverse = order == "desc"
    if sort == "mtime":
        files.sort(key=lambda f: (-f["mtime"] if reverse else f["mtime"], f["name"].lower()))
    else:
        files.sort(key=lambda f: f["name"].lower(), reverse=reverse)

    crumbs: list[dict[str, str]] = [{"name": root, "path": ""}]
    acc: list[str] = []
    for part in rel.split("/") if rel else []:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})

    total = len(files)
    page = files[offset:offset + limit]
    return web.json_response({
        "root": root,
        "root_path": str(base_path),
        "path": rel,
        "crumbs": crumbs,
        "dirs": dirs,
        "files": page,
        # 分页元信息：total 是「文件总数」（不含目录），has_more 供前端决定下一页是否可用
        "total": total,
        "dir_count": len(dirs),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
        "sort": sort,
        "order": order,
        "truncated": truncated,
    })


async def _comfy_queue() -> dict[str, Any]:
    """ComfyUI 执行队列快照。

    任务表里只有「已提交的生成请求」这一层信息，队列才能看到当下的执行/排队实况
    （含图片工作流的中间节点）。队列不可达时降级为 reachable=false，不影响任务表。
    """
    try:
        info = await _comfy.queue()
    except Exception as exc:  # noqa: BLE001 - 后端不可达不应拖垮整个面板
        return {"reachable": False, "error": str(exc), "running": [], "pending": []}

    def _ids(key: str) -> list[str]:
        return [
            item[1]
            for item in (info.get(key) or [])
            if isinstance(item, (list, tuple)) and len(item) > 1
        ]

    running = _ids("queue_running")
    pending = _ids("queue_pending")
    return {"reachable": True, "running": running, "pending": pending}


def _absolutize_product(base: str, target: str) -> str | None:
    """任务产物 → 浏览器可直接打开的绝对地址。

    产物有三种形态：`http(s)://`（已绝对）、`/view?...`（ComfyUI 原生相对地址）、
    磁盘绝对路径（response_format=file/path）。最后一种要翻译回 /view 查询串，否则页面上
    会拼出 `http://hostE:\\...` 这种废链接。
    """
    if target.startswith(("http://", "https://")):
        return target
    if target.startswith("/"):
        return base + target
    rel = _local_view_url(target)
    return base + rel if rel else None


def _local_view_url(path: str) -> str | None:
    """磁盘路径 → ComfyUI /view?filename=..&subfolder=..&type=input|output，仅限两个根目录内。"""
    try:
        target = Path(path).resolve()
    except OSError:
        return None
    for kind, resolve_root in _ROOTS.items():
        root = resolve_root()
        if not root:
            continue
        root_path = Path(root).resolve()
        if root_path not in target.parents:
            continue
        query = {"filename": target.name, "type": kind}
        sub = target.parent.relative_to(root_path).as_posix()
        if sub and sub != ".":
            query["subfolder"] = sub
        return "/view?" + urlencode(query)
    return None


@gateway_handler
async def tasks(request: web.Request) -> web.Response:
    """任务表快照 + ComfyUI 队列：状态按 OpenAI 枚举映射，成功任务附带产物地址。"""
    _authorize(request)
    now = time.time()
    # 与 REST 的 _absolutize 一致：产物地址要对「打开页面的那个浏览器」可达。
    # 用 settings.comfy_base_url 会把远程访问者的链接钉到 127.0.0.1 上，必须回落到请求 origin。
    base = (settings.public_base_url or str(request.url.origin())).rstrip("/")
    items: list[dict[str, Any]] = []
    for t in task_store.snapshot():
        item: dict[str, Any] = {
            "id": t.id,
            "status": _OPENAI_STATUS.get(t.status, t.status),
            "model": t.model,
            "created": t.created_at,
            "elapsed": round(now - t.created_at, 1),
            "prompt_id": t.prompt_id,
        }
        if t.status == "succeeded" and t.result:
            data = t.result.get("data") or []
            datum = data[0] if data else {}
            product = datum.get("url") or datum.get("path")
            if isinstance(product, str):
                resolved = _absolutize_product(base, product)
                if resolved:
                    item["url"] = resolved
                # 产物落在 input/output 之外（自定义输出目录）时只给原路径，前端展示为不可点
                else:
                    item["path"] = product
        elif t.status in ("failed", "cancelled"):
            item["error"] = t.error
        items.append(item)
    return web.json_response({
        "server_time": now,
        "tasks": items,
        "queue": await _comfy_queue(),
    })
