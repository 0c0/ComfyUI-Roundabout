"""ComfyUI-Roundabout —— MCP 网关（Model Context Protocol server）。

在 OpenAI 风格 REST 网关之外，提供一套 **工具化 + 自描述 schema** 的接入层：
任意支持 MCP 的客户端（WorkBuddy / Claude Desktop / Cursor 等）都能直接
「对话式」调用本机 ComfyUI 的图像 / 视频生成能力，无需手工拼 HTTP JSON。

复用 gateway/ 的全部业务逻辑（registry 模型注册表、pipeline 生成链路、
task_store 异步任务表、ComfyClient 后端客户端），本文件只做协议适配。

两种运行模式：
1) 独立进程（stdio 传输，标准 MCP 通道）：
       <comfyui-python> mcp_server.py
    或按 MCP 客户端约定配置为外部命令：
       {"command": "<comfyui-python>", "args": ["<路径>/mcp_server.py"]}
2) 嵌入 ComfyUI 进程（streamable-http，与 REST 网关共享 task_store/registry）：
       由节点 __init__.py 在启动时自动拉起，路径见 MCP_PATH（默认 /mcp），内部
       后端端口见 MCP_PORT / MCP_PORT_MAP（都留空时由系统分配空闲端口，同机多
       实例并存不抢端口）。
       默认 MCP_SHARE_PORT=true 时客户端连 http://<comfyui-host>:<comfyui-port>/mcp
       即可，提交/轮询与 REST 网关完全互通。

工具清单：
    list_models       列出可用模型（模式 / 能力 / 默认参数）
    generate_image    文生图 / 图生图（同步或异步，异步返回 task 对象）
    generate_video    文生视频 / 参考生视频（同步或异步，异步返回 task 对象）
    get_task          查询异步任务状态与产物（含 prompt_id / 工作流快照）
    cancel_task       取消 ComfyUI 中运行的任务
    queue_status      队列监控合并视图（ComfyUI 队列 + 网关异步任务）
    get_workflow      三层查找任务的工作流 JSON（队列 / history / 任务快照）
    reload            热加载 models.yaml 模型配置
    health            网关与后端健康状态
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import Field

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

# 允许以「脚本直接运行」或「python -m」两种方式定位节点根目录
_ROOT = Path(__file__).resolve().parent
import sys  # noqa: E402

# gateway 必须与宿主用同一份，否则 registry / task_store 会被加载两遍，表现为：
# 热加载 models.yaml 后 MCP 侧看不到新模型、MCP 建的异步任务在 REST 与任务面板里查不到。
#   - 嵌入 ComfyUI 进程：本模块属于节点包（__package__ 非空），走相对导入，与 __init__.py 共用同一份；
#   - 直接运行脚本：没有包上下文，把节点根目录挂到 sys.path 后按顶层包导入。
if __package__:
    from .gateway.comfy_client import ComfyClient  # noqa: E402
    from .gateway.config import settings  # noqa: E402
    from .gateway.errors import APIError  # noqa: E402
    from .gateway.handlers import (  # noqa: E402
        _run_video_task,
        cancel_task as _cancel_task,
        generate_tracked,
        generate_video_tracked,
    )
    from .gateway.log_filters import client_gone, exception_chain_names  # noqa: E402
    from .gateway.registry import registry  # noqa: E402
    from .gateway.schemas import ImageGenerationRequest, VideoGenerationRequest  # noqa: E402
    from .gateway.tasks import task_store  # noqa: E402
    from .gateway.viewer import view_url  # noqa: E402
else:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from gateway.comfy_client import ComfyClient  # noqa: E402
    from gateway.config import settings  # noqa: E402
    from gateway.errors import APIError  # noqa: E402
    from gateway.handlers import (  # noqa: E402
        _run_video_task,
        cancel_task as _cancel_task,
        generate_tracked,
        generate_video_tracked,
    )
    from gateway.log_filters import client_gone, exception_chain_names  # noqa: E402
    from gateway.registry import registry  # noqa: E402
    from gateway.schemas import ImageGenerationRequest, VideoGenerationRequest  # noqa: E402
    from gateway.tasks import task_store  # noqa: E402
    from gateway.viewer import view_url  # noqa: E402

# ------------------------------------------------------------------ 日志
# MCP stdio 通道上不能打 stdout/stderr（会污染协议帧），统一走 logging 到 stderr。
# 嵌入 ComfyUI 进程时（_EMBEDDED=1）不重复 basicConfig，复用宿主日志配置。
log = logging.getLogger("roundabout.mcp")
if not os.environ.get("ROUNDABOUT_MCP_EMBEDDED"):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _package_version(default: str = "0.0.0") -> str:
    """版本号以同目录 pyproject.toml 为唯一事实来源。

    MCP 客户端在 initialize 的 serverInfo 里看到的就是这个号（health 工具也报它），
    而注册表发布用的是 pyproject 的 version——写死两处必然漂移，所以运行时读一次。
    tomllib 需 Python 3.11+，ComfyUI 环境未必满足，故回落轻量正则。
    """
    toml = _ROOT / "pyproject.toml"
    try:
        import tomllib  # Python 3.11+

        with open(toml, "rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:  # noqa: BLE001 - 缺文件 / 无 tomllib / 结构不符都走下面的正则
        pass
    try:
        import re

        m = re.search(r'^\s*version\s*=\s*"([^"]+)"', toml.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return default


VERSION = _package_version()

# ------------------------------------------------------------------ 后端客户端
comfy = ComfyClient(settings.comfy_base_url)

# 加载模型注册表：独立进程必须自行加载；嵌入模式由节点 __init__.py 加载（此处幂等重载无害）
try:
    registry.load(
        settings.models_file,
        settings.workflows_dir,
        settings.default_model,
    )
except Exception as exc:  # noqa: BLE001
    log.error("MCP gateway: failed to load models: %s", exc)


def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _absolutize_urls(payload: dict[str, Any]) -> dict[str, Any]:
    """补全产物相对 url（MCP 无 request 上下文，回落 PUBLIC_BASE_URL / comfy_base_url）。

    与 REST 侧 handlers._absolutize 同语义：对 data[].url 及 output.data[].url 中
    以 "/" 开头的相对路径补成绝对 http 地址，否则客户端拿到 /view?... 无法直接用。
    """
    base = settings.public_base_url or settings.comfy_base_url

    def _patch(items):
        for item in items or []:
            url = item.get("url")
            if url and isinstance(url, str) and url.startswith("/"):
                item["url"] = base + url

    _patch(payload.get("data"))
    out = payload.get("output")
    if isinstance(out, dict):
        _patch(out.get("data"))
    return payload


# ------------------------------------------------------------------ 工具
mcp = MCPServer(name="comfyui-roundabout", version=VERSION)

# ---- 工具 1：list_models --------------------------------------------------
@mcp.tool(name="list_models", description="列出网关可用模型及其能力、模式、默认参数。")
async def list_models() -> list[dict[str, Any]]:
    models = []
    for s in registry.all():
        models.append({
            "name": s.name,
            "mode": s.mode,
            "capabilities": sorted(s.capabilities),
            "description": s.description,
            "defaults": dict(s.defaults or {}),
            "aliases": list(s.aliases or []),
        })
    return models


# ---- 工具 2：generate_image ----------------------------------------------
@mcp.tool(
    name="generate_image",
    description=(
        "生成图像（文生图为主）。model 不传则用默认；支持 negative_prompt / seed / "
        "size / steps / cfg 等精调参数；response_format=path 返回磁盘绝对路径。"
        "返回 OpenAI 风格响应（created/data/seed 回显）。"
        "【编辑已有图片不要用本工具】改图/去背景请用专门的 edit_image / remove_background 工具。"
        "filename_prefix 指定落盘前缀（可含 \"/\" 建子目录），不传则用工作流模板自带前缀。"
    ),
)
async def generate_image(
    prompt: str,
    model: str = Field(
        default="",
        description=(
            "模型选择（不传=默认 z-image-turbo）。用途：z-image-turbo=8 步快速文生图（日常首选）；"
            "z-image=30 步高质量；boogu-image-turbo / boogu-image-base-4step=4 步极速预览；"
            "boogu-image-base=30 步高质量备选。"
            "编辑已有图片请用 edit_image 工具，去背景用 remove_background 工具。"
        ),
    ),
    n: int = 1,
    size: str = "",
    response_format: str = "",
    negative_prompt: str = "",
    seed: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    image: str = "",
    mask: str = "",
    workflow_overrides: str = "",  # JSON 字符串，形如 {"3.inputs.cfg": 4.5}
    mode: str = "",
    filename_prefix: str = "",
    ctx: Context = None,  # type: ignore[assignment]  # MCP SDK 按注解自动注入
) -> dict[str, Any]:
    req = ImageGenerationRequest(
        prompt=prompt,
        model=model or None,
        n=n,
        size=size or None,
        response_format=response_format or None,  # type: ignore[arg-type]
        negative_prompt=negative_prompt or None,
        seed=seed,
        steps=steps,
        cfg=cfg,
        image=image or None,
        mask=mask or None,
        workflow_overrides=_parse_json_or_none(workflow_overrides),
        mode=mode or None,
        filename_prefix=filename_prefix or None,
    )
    # 视频模型打到图片工具 → 自动转视频链路（与 REST 端点行为一致）
    spec = registry.resolve(req.model)
    if spec.is_video:
        vreq = VideoGenerationRequest.model_validate(req.model_dump(exclude_none=True))
        return await _handle_video(vreq, ctx)
    result = await generate_tracked(req)
    return _absolutize_urls(result.model_dump(exclude_none=True))


# ---- 工具 3：edit_image ---------------------------------------------------
@mcp.tool(
    name="edit_image",
    description=(
        "编辑已有图片（提示词驱动的图像编辑，独立工具）。prompt 描述要改什么，"
        "image 传待编辑的图（本地绝对路径 / http(s) URL / dataURL / base64 / 相对 ComfyUI input 的路径）。"
        "model 可选：默认 flux2-klein-image-edit-turbo（语义改写首选：换背景/换材质/增删物体，指令跟随好）；"
        "改写/添加图内文字传 model='boogu-image-edit-turbo'（快）或 'boogu-image-edit'（高质量）。"
        "尺寸跟随输入图（输出约 1MP，size 不生效）。response_format=url/path/b64_json；"
        "返回 OpenAI 风格响应。只做文生图请用 generate_image。"
    ),
)
async def edit_image(
    prompt: str,
    image: str,
    model: str = "",
    n: int = 1,
    negative_prompt: str = "",
    seed: int | None = None,
    response_format: str = "",
    filename_prefix: str = "",
    mask: str = "",
    workflow_overrides: str = "",  # JSON 字符串，形如 {"3.inputs.cfg": 4.5}
) -> dict[str, Any]:
    req = ImageGenerationRequest(
        prompt=prompt,
        # 不传 model 时不要落回全局默认（那是文生图模型，会被下面的校验拒掉）：
        # 编辑工具自己的默认 = 语义改写首选的 flux2 klein
        model=model or "flux2-klein-image-edit-turbo",
        n=n,
        negative_prompt=negative_prompt or None,
        seed=seed,
        response_format=response_format or None,  # type: ignore[arg-type]
        filename_prefix=filename_prefix or None,
        image=image or None,
        mask=mask or None,
        workflow_overrides=_parse_json_or_none(workflow_overrides),
    )
    spec = registry.resolve(req.model)
    if spec.is_video or not spec.supports_img2img:
        alts = [s.name for s in registry.all() if s.supports_img2img and not s.is_video]
        raise APIError(
            f"Model `{spec.name}` is not an image-edit model. "
            f"Edit-capable models: {', '.join(alts)}.",
            param="model",
        )
    result = await generate_tracked(req)
    return _absolutize_urls(result.model_dump(exclude_none=True))


# ---- 工具 4：remove_background --------------------------------------------
@mcp.tool(
    name="remove_background",
    description=(
        "图片去背景（BiRefNet 高精度抠图，独立工具，无需提示词）。"
        "image 支持：本地绝对路径 / http(s) URL / dataURL / base64 / 相对 ComfyUI input 目录的路径。"
        "输出透明背景 PNG；response_format=url 返回可访问链接，path 返回磁盘绝对路径，b64_json 返回 base64。"
        "返回 OpenAI 风格响应（created/data/usage）。"
    ),
)
async def remove_background(
    image: str,
    response_format: str = "url",
    filename_prefix: str = "",
) -> dict[str, Any]:
    req = ImageGenerationRequest(
        model="utility-birefnet-remove-background",
        response_format=response_format or None,  # type: ignore[arg-type]
        filename_prefix=filename_prefix or None,
        image=image or None,
    )
    result = await generate_tracked(req)
    return _absolutize_urls(result.model_dump(exclude_none=True))


# ---- 工具 5：generate_video ----------------------------------------------
@mcp.tool(
    name="generate_video",
    description=(
        "生成视频（文生视频 / 参考生视频）。model 默认 minimax-h3；支持 duration(1-15s) / "
        "fps / size(如 576p-16:9 / 720p-16:9 / 768p-16:9 / 1080p-16:9 / 1440p-16:9=2560x1440，1440p 需大显存) / seed / reference_images(参考图，最多 6 张，"
        "支持 base64/URL/本地路径) / reference_videos / reference_audios。"
        "minimax-h3-lift / -lift-edit = base / edit 骨架 + 尾部确定性潜空间放大（输出画布 x1.5）；"
        "fasth3=FastVideo 8 步蒸馏档（reference_images 传 0/1/2 张 = 文生 / 首帧 / 首尾帧）；"
        "fasth3-edit=FastH3 参考生视频（6 图 + 3 视频 + 3 音频）；"
        "filename_prefix 指定落盘前缀（可含 \"/\" 建子目录，不传则用模板默认）。"
        "默认同步等待（长任务建议 background=pending 异步，再轮询 get_task）。"
    ),
)
async def generate_video_tool(
    prompt: str,
    model: str = Field(
        default="minimax-h3",
        description=(
            "模型选择（MiniMax H3 / FastH3 系列）：minimax-h3=base 档（默认 1344x768@30；"
            "草稿传 size=\"576p-16:9\" + steps=8）；"
            "minimax-h3-edit=参考/编辑变体"
            "（配合 reference_images/videos/audios 使用，最多 6 图 + 3 视频 + 3 音频）；"
            "fasth3=FastVideo FastH3 8 步蒸馏档（文生 / 首尾帧）；"
            "fasth3-edit=FastH3 参考生视频（配合 reference_images/videos/audios，最多 6 图 + 3 视频 + 3 音频）；"
            "minimax-h3-lift=H3 确定性放大档（原生 1344x768 采样 → 学习式 lift，默认输出 2016x1152，"
            "构图零重掷、纹理 +152% vs 白放大；scale/rho 精调用 workflow_overrides 点名 "
            "910.inputs.scale / 910.inputs.rho）。"
            "attention 可选：sparse（默认，块稀疏注意力，8 步 768p 实测约 0.76x 耗时，"
            "画质与致密高度一致、差异只在高频细节）/ dense（关闭稀疏换致密画质，耗时回满）。"
            "仅 base 四支（minimax-h3 / -edit / -lift / -lift-edit）支持；FastH3 两支恒定稀疏、"
            "传了报错。"
        ),
    ),
    duration: float | None = None,
    fps: int | None = None,
    size: str = "",
    seed: int | None = None,
    negative_prompt: str = "",
    reference_images: list[str] | None = None,
    reference_videos: list[str] | None = None,
    reference_audios: list[str] | None = None,
    steps: int | None = None,
    attention: str = "",  # "sparse"（默认，稀疏加速）| "dense"（关闭稀疏，画质优先）；仅 base 四支
    background: str = "",  # "pending" 触发异步
    response_format: str = "",
    filename_prefix: str = "",
    ctx: Context = None,  # type: ignore[assignment]  # MCP SDK 按注解自动注入
) -> dict[str, Any]:
    req = VideoGenerationRequest(
        prompt=prompt,
        model=model or None,
        duration=duration,
        fps=fps,
        size=size or None,
        seed=seed,
        negative_prompt=negative_prompt or None,
        reference_images=reference_images,
        reference_videos=reference_videos,
        reference_audios=reference_audios,
        steps=steps,
        background=background or None,
        response_format=response_format or None,  # type: ignore[arg-type]
        filename_prefix=filename_prefix or None,
        attention=attention or None,  # type: ignore[arg-type]
    )
    return await _handle_video(req, ctx)


# ---- 工具 6：get_task -----------------------------------------------------
@mcp.tool(name="get_task", description="查询异步生成任务的状态与产物（含 prompt_id 与工作流快照标记）。")
async def get_task(task_id: str) -> dict[str, Any]:
    task = task_store.get(task_id)
    if task is None:
        raise APIError("Task not found.", status_code=404, code="task_not_found")
    return {
        "id": task.id,
        "status": _openai_status(task.status),
        "model": task.model,
        "prompt_id": task.prompt_id,
        "has_workflow": task.workflow is not None,
        "created_at": int(task.created_at),
        "output": _absolutize_urls(task.result) if task.result else None,
        "error": {"message": task.error, "code": task.code} if task.error else None,
    }


# ---- 工具 7：cancel_task --------------------------------------------------
@mcp.tool(
    name="cancel_task",
    description=(
        "取消任务：传 task_id 取消指定异步任务（pending 的从 ComfyUI 队列移除，running 的中断执行，"
        "并标记本地任务为 cancelled）；不传 task_id 则中断 ComfyUI 当前正在执行的任务。"
    ),
)
async def cancel_task(task_id: str = "") -> dict[str, Any]:
    if task_id:
        return await _cancel_task(task_id)
    await comfy.interrupt()
    return {"ok": True, "message": "Interrupt sent to ComfyUI."}


# ---- 工具 8：queue_status -------------------------------------------------
@mcp.tool(name="queue_status", description="队列监控：ComfyUI 执行队列（running/pending）+ 网关异步任务表。")
async def queue_status() -> dict[str, Any]:
    # admin.queue_status 是 aiohttp handler，复用其内部逻辑不便，直接自组
    try:
        info = await comfy.queue()
    except APIError as exc:
        return {"comfy_reachable": False, "comfy_error": exc.message, "tasks": [_task_summary(t) for t in task_store.snapshot()]}
    now = time.time()
    return {
        "comfy_reachable": True,
        "running": [_comfy_item(x, now) for x in info.get("queue_running") or []],
        "pending": [_comfy_item(x, now) for x in info.get("queue_pending") or []],
        "running_count": len(info.get("queue_running") or []),
        "pending_count": len(info.get("queue_pending") or []),
        "tasks": [_task_summary(t) for t in task_store.snapshot()],
    }


# ---- 工具 9：get_workflow ------------------------------------------------
@mcp.tool(name="get_workflow", description="三层查找任务的工作流 JSON：ComfyUI 队列 → history → 网关任务快照。")
async def get_workflow(prompt_id: str) -> dict[str, Any]:
    info = await comfy.queue()
    for key in ("queue_running", "queue_pending"):
        for item in info.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) > 2 and item[1] == prompt_id:
                return {"prompt_id": prompt_id, "status": "running" if key == "queue_running" else "pending",
                        "workflow": item[2] if isinstance(item[2], dict) else None, "source": "queue"}
    entry = await comfy.history(prompt_id)
    if entry:
        p = entry.get("prompt")
        wf = p[2] if isinstance(p, (list, tuple)) and len(p) > 2 else None
        if isinstance(wf, dict):
            return {"prompt_id": prompt_id, "status": (entry.get("status") or {}).get("status_str", "unknown"),
                    "workflow": wf, "source": "history"}
    task = task_store.get(prompt_id)
    if task is not None and task.workflow is not None:
        return {"prompt_id": prompt_id, "status": task.status, "workflow": task.workflow, "source": "task"}
    raise APIError(f"Prompt/task `{prompt_id}` not found in queue, history or task store.",
                   status_code=404, code="prompt_not_in_queue")


# ---- 工具 10：reload --------------------------------------------------------
@mcp.tool(name="reload", description="热加载 models.yaml 模型配置（无需重启 ComfyUI）。")
async def reload() -> dict[str, Any]:
    registry.load(settings.models_file, settings.workflows_dir, settings.default_model)
    return {"ok": True, "models": [s.name for s in registry.all()]}


# ---- 工具 11：health --------------------------------------------------------
@mcp.tool(name="health", description="网关与 ComfyUI 后端健康状态。")
async def health() -> dict[str, Any]:
    try:
        r = await comfy._request("GET", "/system_stats")
        comfy_ok = r.status == 200
    except Exception:  # noqa: BLE001
        comfy_ok = False
    return {
        "gateway": "ok",
        "comfy_reachable": comfy_ok,
        "models": len(registry.all()),
        "version": VERSION,
    }


# ---- 工具 12：get_view_url -------------------------------------------------
@mcp.tool(
    name="get_view_url",
    description=(
        "返回 Roundabout 可视化页面的地址，浏览器直接打开即可使用，无需命令行。"
        "页面内容：浏览 ComfyUI input/output 目录里的图片/视频/音频（缩略图、大图预览、"
        "视频播放），以及异步生成任务的实时进度（排队中/执行中/已完成/失败，含耗时与产物预览）。"
        "当用户问「生成的东西在哪看」「给我一个查看页面」「有没有界面」「任务进度怎么看」"
        "或想浏览素材目录时，调用本工具并把 url 原样给用户。"
    ),
)
async def get_view_url() -> dict[str, Any]:
    url = view_url()
    payload: dict[str, Any] = {"url": url}
    if settings.auth_enabled and settings.api_keys:
        # 浏览器无法带自定义头，鉴权开启时把 key 拼进查询串，否则页面会 401。
        payload["url"] = f"{url}?key={settings.api_keys[0]}"
        payload["auth_required"] = True
    return payload


# ------------------------------------------------------------------ 内部辅助
async def _run_video_task_and_notify(
    ctx: Context, task_id: str, req: VideoGenerationRequest, request_id: str
) -> None:
    """异步视频任务执行 + 完成后向提交客户端推送标准 notification（替代轮询）。

    只在**有状态模式**（`MCP_STATELESS=false`）下被调用——推送挂在会话上，无状态模式没有
    常驻会话 / GET SSE 通道可推，调用方 `_handle_video` 会直接起 `_run_video_task`，
    由客户端轮询 `GET /v1/videos/tasks/{id}`。

    走 MCP 标准 notifications/message（LoggingMessageNotification，客户端必处理），
    data 里带 task_id / status / url 等结构化信息。客户端保持 GET SSE 连接即可收到。
    """
    try:
        await _run_video_task(task_id, req, request_id)
    finally:
        task = task_store.get(task_id)
        if task is None:
            return
        payload: dict[str, Any] = {
            "task_id": task_id,
            "status": task.status,
            "model": task.model,
            "prompt_id": task.prompt_id,
        }
        if task.result:
            items = task.result.get("data") or []
            if items and items[0].get("url"):
                payload["url"] = _absolutize_urls({"data": items})["data"][0]["url"]
        if task.error:
            payload["error"] = task.error
        try:
            from mcp_types import LoggingMessageNotification, LoggingMessageNotificationParams

            notif = LoggingMessageNotification(
                params=LoggingMessageNotificationParams(
                    level="info", logger="roundabout.mcp", data=payload
                )
            )
            await ctx.session.send_notification(notif)
            log.info("MCP gateway: task %s completion notification sent (status=%s)", task_id, task.status)
        except Exception as exc:  # noqa: BLE001 - session 可能已断开，静默降级
            log.debug("MCP gateway: task %s notification failed (session closed?): %s", task_id, exc)


async def _handle_video(req: VideoGenerationRequest, ctx: Context | None = None) -> dict[str, Any]:
    """视频生成统一入口：异步（background=pending）或同步。"""
    if req.is_async:
        task_id = uuid.uuid4().hex
        request_id = _new_request_id()
        task_store.create(task_id, model=req.model, request_id=request_id)
        # 有状态模式才有推送通道（会话在，notification 才发得出去）；无状态模式连通知 task
        # 都不用起——客户端按返回的 id 轮询 get_task。
        if ctx is not None and not settings.mcp_stateless:
            asyncio.create_task(_run_video_task_and_notify(ctx, task_id, req, request_id))
        else:
            asyncio.create_task(_run_video_task(task_id, req, request_id))
        return _task_payload(task_id, req.model, "pending")
    # 同步：直接等完（MCP 客户端通常可接受较长超时；长视频建议用 background=pending 异步）
    result = await generate_video_tracked(req)
    return _absolutize_urls(result.model_dump(exclude_none=True))


def _task_payload(task_id: str, model: str | None, status: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "object": "image_generation.task",
        "status": status,
        "created_at": int(time.time()),
        "model": model,
    }


def _openai_status(s: str) -> str:
    return {"queued": "pending", "processing": "in_progress", "succeeded": "completed", "failed": "failed"}.get(s, s)


def _task_summary(t) -> dict[str, Any]:
    return {
        "id": t.id,
        "status": t.status,
        "code": t.code,
        "model": t.model,
        "prompt_id": t.prompt_id,
        "has_workflow": t.workflow is not None,
        "created": t.created_at,
        "elapsed": round(time.time() - t.created_at, 1),
    }


def _comfy_item(item, now: float) -> dict[str, Any]:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return {}
    prompt_id = item[1]
    extra = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
    created = (extra.get("create_time") or 0) / 1000.0
    return {
        "prompt_id": prompt_id,
        "created": created or None,
        "elapsed": round(now - created, 1) if created else None,
        "node_count": len(item[2]) if len(item) > 2 and isinstance(item[2], dict) else None,
    }


def _parse_json_or_none(s: str) -> dict[str, Any] | None:
    if not s:
        return None
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ 入口
def main() -> None:
    # MCPServer.run 内部用 anyio 管理事件循环（stdio 传输），不要在外面再包 asyncio.run
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ 嵌入 ComfyUI 进程
def _bound_port(server) -> int | None:
    """从已启动的 uvicorn Server 上读回实际监听端口。

    port=0 时端口由操作系统分配，只有在 bind 成功之后才知道具体是哪一个。
    """
    for srv in getattr(server, "servers", None) or []:
        for sock in getattr(srv, "sockets", None) or []:
            try:
                return int(sock.getsockname()[1])
            except Exception:  # noqa: BLE001 - socket 已关闭等情况直接跳过
                continue
    return None


async def _report_bound_port(server, report) -> None:
    """等 uvicorn 完成绑定，再把实际端口交给 report()。"""
    try:
        while not server.started and not server.should_exit:
            await asyncio.sleep(0.05)
        report(_bound_port(server) if server.started else None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("MCP gateway: failed to determine backend port: %s", exc)
        report(None)


async def serve_embedded(
    host: str = "127.0.0.1",
    port: int = 0,
    path: str = "/mcp",
    on_ready=None,
    fallback_auto: bool = True,
    stateless: bool = False,
) -> None:
    """在 ComfyUI 进程内以 streamable-http 提供 MCP 端点。

    与 REST 网关同进程：registry / task_store / ComfyClient 全部共享，
    通过 MCP 提交的异步任务可在 REST 队列监控中看到，反之亦然。
    由节点 __init__.py 在启动事件循环中 create_task 调用。

    port 来自 gateway.config.resolve_mcp_port()：显式 MCP_PORT 优先，其次是
    MCP_PORT_MAP 按本实例的 ComfyUI 端口映射出来的值，都没命中时为 0（由操作系统
    分配空闲端口）——同一台机器同时跑多个 ComfyUI 实例时各占各的，不会互抢。

    fallback_auto=True（默认）：配置的端口若已被占用，退回让系统分配一个空闲端口，
    保证 MCP 端点始终可用（共享端口模式下该端口只是进程内回环后端，换端口对外无感）；
    False 则绑定失败即回传 None。端口只有 bind 成功后才确定，因此监听就绪后经
    on_ready(actual_port) 回传；最终失败回传 None。
    """
    import uvicorn

    reported = False
    suppress_none = bool(port) and fallback_auto  # 还有退路时先别宣告失败

    def _report(actual: int | None) -> None:
        nonlocal reported
        if reported:
            return
        if actual is None and suppress_none:
            # 这一轮没绑上，但马上要用随机端口重试：等重试结果出来再回传，否则
            # 共享端口模式的 /mcp 代理会拿到 None 而永久失效。
            return
        reported = True
        if on_ready is None:
            return
        try:
            on_ready(actual)
        except Exception as exc:  # noqa: BLE001 - 回调出错不该拖垮后端
            log.warning("MCP gateway: on_ready callback failed: %s", exc)

    async def _serve_once(bind_port: int) -> None:
        starlette_app = mcp.streamable_http_app(
            streamable_http_path=path, host=host, stateless_http=stateless
        )
        config = uvicorn.Config(starlette_app, host=host, port=bind_port, log_level="warning")
        server = uvicorn.Server(config)
        watcher = (
            asyncio.ensure_future(_report_bound_port(server, _report))
            if on_ready is not None
            else None
        )
        try:
            await server.serve()
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()

    try:
        try:
            await _serve_once(port)
        except (SystemExit, OSError) as exc:
            # 端口被占用时 uvicorn 走 sys.exit(1)：吞掉，不要连带影响 ComfyUI 主进程。
            if not suppress_none:
                log.error(
                    "MCP gateway: backend server stopped (%s); MCP endpoint is unavailable", exc
                )
                _report(None)
                return
            suppress_none = False
            log.warning(
                "MCP gateway: configured port %s is unavailable (%s); falling back to an "
                "OS-assigned port so the MCP endpoint stays up",
                port,
                exc,
            )
            await _serve_once(0)
    except (SystemExit, OSError) as exc:  # 连系统分配的端口都绑不上
        log.error("MCP gateway: backend server stopped (%s); MCP endpoint is unavailable", exc)
        _report(None)


def start_embedded_task(
    host: str = "127.0.0.1", port: int = 0, path: str = "/mcp", on_ready=None
) -> None:
    """在运行中的事件循环里调度 MCP streamable-http server（嵌入模式入口）。"""
    loop = asyncio.get_running_loop()
    loop.create_task(serve_embedded(host=host, port=port, path=path, on_ready=on_ready))


# ------------------------------------------------------------------ 与 ComfyUI 同端口（aiohttp 原生代理）
# 后端就绪前的 /mcp 请求最多等这么久，超过才回 503。MCP 客户端通常只在会话开始时
# 连一次，ComfyUI 启动窗口内等它一下，比直接拒绝（客户端往往不重试）体验好得多。
READY_WAIT_TIMEOUT = 10.0


class BackendTarget:
    """共享端口代理的转发目标，允许先建后填。

    为什么需要它：aiohttp 的路由表在 `AppRunner.setup()` 里就被冻结
    （`app.freeze()`），此后任何 `add_route` 都会抛
    `RuntimeError: Cannot register a resource into frozen router`。
    而后端端口要等 uvicorn 真正 bind 成功才知道（MCP_PORT=0 由系统分配、
    配好的端口被占还会回落）。所以拆成两步——路由赶在冻结前注册好，转发目标
    留空，等 on_ready 回填；`_proxy` 每次请求现读 `url`，自然看到最新值，
    并且在拿到之前会先 `wait_ready()` 等一小会儿。
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url
        self._ready = asyncio.Event()
        if url:
            self._ready.set()

    @property
    def url(self) -> str | None:
        return self._url

    @url.setter
    def url(self, value: str | None) -> None:
        self._url = value
        if value:
            self._ready.set()

    async def wait_ready(self, timeout: float = READY_WAIT_TIMEOUT) -> bool:
        """等后端回填端口。超时返回 False（后端没起来，调用方负责回 503）。"""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False


def register_share_port_proxy(
    aiohttp_app,
    backend_url: str | None = None,
    path: str = "/mcp",
    ready_timeout: float = READY_WAIT_TIMEOUT,
) -> BackendTarget:
    """把内部 uvicorn MCP 后端通过 aiohttp 原生代理暴露到 ComfyUI 同一端口。

    背景：MCP SDK 的 streamable-http 传输是 Starlette（ASGI）实现的，而 ComfyUI
    的网关是 aiohttp——两套服务器框架无法直接共端口；`aiohttp-asgi` 桥接在
    aiohttp 3.13 下流式响应（SSE）会静默失败（prepare 成功但字节不落 socket）。
    因此采用「回环 uvicorn 后端 + aiohttp 原生代理」：客户端只连 ComfyUI 端口
    （如 http://<host>:8188/mcp），本代理把请求转发到 127.0.0.1:<后端实际端口>；
    aiohttp 原生 StreamResponse 的 SSE 流式是可靠的（见节点内验证）。

    POST / GET(SSE) / DELETE 全部按原样转发，响应头/流式体原样回传。

    backend_url 建议留空：调用方先在 ComfyUI 冻结路由表之前把路由挂上（把返回的
    BackendTarget 存起来），等后端报告端口后再写 `target.url`。显式传入则立即生效
    （便于测试与其它嵌入场景）。
    """
    from aiohttp import ClientError, ClientSession, web

    holder = BackendTarget(backend_url)

    async def _proxy(request: web.Request) -> web.StreamResponse:
        backend = holder.url
        resp: web.StreamResponse | None = None
        try:
            if not backend:
                # 路由注册早于后端就绪（这是刻意的，见上），启动窗口内的请求会落在这里。
                # 先等后端一小会儿：MCP 客户端一般只连一次，直接 503 等于让它失败。
                if not await holder.wait_ready(ready_timeout):
                    log.debug(
                        "MCP gateway: %s requested before the internal backend reported its port",
                        request.path,
                    )
                    return web.Response(
                        status=503,
                        text="MCP backend not ready yet (Roundabout is still starting).",
                        headers={"Retry-After": "1"},
                    )
                backend = holder.url
            target = backend.rstrip("/") + request.path
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "content-length", "connection")
            }
            # 请求体也在 try 内读：客户端若在读到一半时断开，同样不能让异常逃出去。
            data = await request.read()
            # auto_decompress=False：本代理只做**字节**转发，上游响应的
            # Content-Length / Content-Encoding 都原样透传给客户端；若让客户端库
            # 自动解压，头里写的是压缩后长度、发出去的却是解压后的字节，响应会被截断。
            async with ClientSession(auto_decompress=False) as session:
                async with session.request(
                    request.method, target, params=request.query, headers=headers, data=data or None
                ) as upstream:
                    resp = web.StreamResponse(status=upstream.status)
                    clen = upstream.headers.get("Content-Length")
                    for k, v in upstream.headers.items():
                        if k.lower() in ("transfer-encoding", "connection"):
                            continue
                        resp.headers[k] = v
                    if not clen:
                        resp.enable_chunked_encoding()
                    await resp.prepare(request)
                    async for chunk in upstream.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp
        except ConnectionError as exc:
            # 客户端在流式响应（SSE 长连接 / notification 推送）写到一半就断开。
            # MCP streamable-http 下这是常态而非故障：客户端超时或重连后，服务端
            # 仍在往那条旧连接写。这里必须整族兜住 ConnectionError——Windows 上对端
            # 硬断开抛的不一定是 ConnectionResetError，还可能是
            # ConnectionAbortedError(WinError 10053) / BrokenPipeError；
            # aiohttp 自己的 ClientConnectionResetError 也在这一族里。
            # 本分支必须排在下面的 ClientError 之前：前者同时是后者的子类。
            if resp is None:
                # 一个字节都还没回。区分是「客户端没等到结果就走了」还是「后端在回首个
                # 响应前就断了」——前者是常态（降 DEBUG），后者值得 WARNING。
                client_left = request.transport is None or request.transport.is_closing()
                log.log(
                    logging.DEBUG if client_left else logging.WARNING,
                    "MCP gateway: %s %s from %s ended before the first byte: %s",
                    request.method, request.path, request.remote, exc,
                )
                return web.Response(status=502, text="MCP backend unavailable")
            log.debug(
                "MCP gateway: client %s closed the %s stream early: %s",
                request.remote, request.path, exc,
            )
            return resp
        except ClientError as exc:
            if resp is not None:
                # 响应头 / 部分字节已经发给客户端了，上游此刻断流：当作流正常结束即可。
                # 这里**不能**返回一个新的响应对象——那会在同一条连接上再写一遍状态行。
                log.warning(
                    "MCP gateway: upstream %s cut the %s stream mid-flight: %s",
                    backend, request.path, exc,
                )
                return resp
            # 内部 uvicorn 后端不可达：端口被占且未回落、进程未起、或仍在启动中。
            log.warning(
                "MCP gateway: upstream backend %s unreachable (%s); "
                "check MCP_PORT / MCP_PORT_MAP for this instance",
                backend, exc,
            )
            return web.Response(status=502, text="MCP backend unavailable")
        except Exception as exc:  # noqa: BLE001
            # 兜底：连「非 ConnectionError 族、但语义上就是写向已关闭连接」的包装异常也认。
            if client_gone(exc):
                log.debug(
                    "MCP gateway: client %s left during %s %s: %s",
                    request.remote, request.method, request.path, exc,
                )
                if resp is not None:
                    return resp
                return web.Response(status=502, text="MCP backend unavailable")
            # 最后一道网：任何真正的异常都不许逃回 aiohttp——逃回去只会得到一条
            # 「[ERROR] Error handling request from <ip>」+ 整段 traceback，
            # 既看不出是哪个 handler 也看不出请求路径。这里带上上下文自己打，
            # 并把异常链的类型名一并写出：外层常常只是个包装器，真凶挂在 __cause__ 上
            # （典型形态 `OSError: [WinError 995]` <- `CancelledError`）。
            log.exception(
                "MCP gateway: unexpected error proxying %s %s: %s (chain: %s)",
                request.method, request.path, exc, exception_chain_names(exc),
            )
            if resp is not None and resp.prepared:
                return resp
            return web.Response(status=500, text="MCP gateway internal error")

    aiohttp_app.router.add_route("*", path, _proxy)
    if not path.endswith("/"):
        aiohttp_app.router.add_route("*", path + "/{tail:.*}", _proxy)
    log.info(
        "MCP gateway: shared-port proxy registered at %s (backend %s)",
        path,
        backend_url or "pending — filled in once the embedded backend binds",
    )
    return holder


if __name__ == "__main__":
    main()
