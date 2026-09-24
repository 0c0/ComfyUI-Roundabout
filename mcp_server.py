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
    get_tool_info     调用结构自描述：逐模型「哪些字段生效 / 区间 / 枚举 / 默认值」（选型前先看它）
    generate_image    文生图 / 图生图（同步或异步，异步返回 task 对象）
    edit_image        提示词驱动的图像编辑（含多图参考）
    remove_background 图片去背景（BiRefNet，无需提示词）
    generate_video    文生视频 / 参考生视频（同步或异步，异步返回 task 对象）
    get_task          查询异步任务状态与产物（含 prompt_id / 工作流快照）
    cancel_task       取消 ComfyUI 中运行的任务
    queue_status      队列监控合并视图（ComfyUI 队列 + 网关异步任务）
    get_workflow      三层查找任务的工作流 JSON（队列 / history / 任务快照）
    reload            热加载 models.yaml 模型配置
    health            网关与后端健康状态
    get_view_url      可视化页面地址（浏览 input/output 素材 + 任务进度）
    get_skills        返回配套 agent-skill 清单（安装 URL + 何时用），生成/注册前应先调用
    check_weights     权重体检：列出缺失权重文件与下载命令

工具描述只保留一句话定位；参数细节（逐模型生效性、区间、枚举、默认值）一律查 get_tool_info。
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
    from .gateway import board, toolinfo, weights  # noqa: E402
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
    from gateway import board, toolinfo, weights  # noqa: E402
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
# `instructions` 是 MCP initialize 握手返回给客户端的字段，所有 MCP 客户端（WorkBuddy /
# Claude Desktop / Cursor 等）在连接时都会收到——这是把「配套 skill 推荐」推给 agent 的唯一
# 可靠入口（仓库 README 不会被已连接的 agent 自动读到）。agent 拿到后应在本地装好这些 skill，
# 写 H3 提示词前先查 h3-playbook、拿不准调哪个工具/参数时查 roundabout。
SERVER_INSTRUCTIONS = (
    "This MCP server (comfyui-roundabout) ships companion agent-skills that you MUST install to "
    "use it correctly. BEFORE handling ANY image/video generation, H3 prompt writing, model "
    "registration, or 'which tool/parameter should I use' question, CALL the `get_skills` tool — "
    "it returns the skill list with install URLs. Install each skill, then consult it:\n"
    "- roundabout skill (https://github.com/0c0/roundabout-skill): the usage guide for THIS "
    "server (model selection, request parameters, troubleshooting, registering/unregistering "
    "workflows).\n"
    "- h3-playbook skill (https://github.com/0c0/h3-playbook-skill): MiniMax "
    "H3 / FastH3 official playbook (prompt formula, generation modes, duration/resolution limits, "
    "reference-material rules).\n"
    "- qwen-image-prompt-writing skill (https://github.com/0c0/qwen-image2.1-prompt-writing-skill): the Qwen-Image-2.1 prompt contract "
    "(t2i observer report / edit instruction + wh_ratio / ratio_follow) behind this server's "
    "qwen-image-2.1 model.\n"
    "- h3-prompt-writing skill (https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing): MiniMax's OWN writing guide for H3 prompt "
    "fields, section order and timing notation (T2VA / I2VA / FL2VA / L2VA / Ref2VA) — maintained "
    "by MiniMax, not by this server.\n"
    "For MiniMax H3 / FastH3 prompts load h3-playbook FIRST for limits, then h3-prompt-writing for "
    "the field format; for Qwen-Image-2.1 prompts load qwen-image-prompt-writing; consult "
    "roundabout when unsure which tool or parameter to use."
)

mcp = MCPServer(name="comfyui-roundabout", version=VERSION, instructions=SERVER_INSTRUCTIONS)

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


# ---- 工具 2：get_tool_info ------------------------------------------------
@mcp.tool(
    name="get_tool_info",
    description=(
        "调用结构自描述（本地直读运行期状态，不占 GPU、不触发生成）。"
        "返回逐模型的**字段生效性**：每个请求字段的类型 / 区间 / 枚举 / 默认值 / "
        "对本模型是否生效与不生效原因，外加参考槽数量、张数上限、尺寸档位与种子上限。"
        "拿不准「这个模型能不能传某参数」「该用哪个模型」时先调它，不要猜。"
        "完整版（含字段说明与尺寸预设表）见 REST GET /roundabout/admin/tool-info。"
    ),
)
async def get_tool_info(model: str = "", include_fields: bool = True) -> dict[str, Any]:
    return toolinfo.compact(model=model or None, include_fields=include_fields)


# ---- 工具 3：generate_image ----------------------------------------------
@mcp.tool(
    name="generate_image",
    description=(
        "文生图 / 图生图。参数细节与逐模型生效性查 get_tool_info。"
        "改图请用 edit_image，去背景请用 remove_background。"
    ),
)
async def generate_image(
    prompt: str,
    model: str = Field(
        default="",
        description="模型选择（不传=默认 z-image-turbo）；逐模型能力与默认参数查 get_tool_info。",
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
    reference_images: list[str] | None = None,
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
        reference_images=reference_images,
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


# ---- 工具 4：edit_image ---------------------------------------------------
@mcp.tool(
    name="edit_image",
    description=(
        "提示词驱动的图像编辑（换背景 / 换材质 / 增删物体 / 改图内文字），含多图参考。"
        "不传 model 默认 flux2-klein-image-edit-turbo；改图内文字用 boogu-image-edit-turbo（快）"
        "或 boogu-image-edit（高质量）。纯文生图请用 generate_image。"
        "各模型的可编辑性、参考槽数量与生效字段查 get_tool_info。"
    ),
)
async def edit_image(
    prompt: str,
    image: str = "",
    model: str = "",
    n: int = 1,
    negative_prompt: str = "",
    seed: int | None = None,
    response_format: str = "",
    filename_prefix: str = "",
    mask: str = "",
    reference_images: list[str] | None = None,
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
        reference_images=reference_images,
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


# ---- 工具 5：remove_background --------------------------------------------
@mcp.tool(
    name="remove_background",
    description=(
        "图片去背景（BiRefNet 高精度抠图，无需提示词）。输出透明背景 PNG；"
        "response_format 默认 url，可改 path（磁盘绝对路径）/ b64_json。"
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


# ---- 工具 6：generate_video ----------------------------------------------
@mcp.tool(
    name="generate_video",
    description=(
        "文生视频 / 参考生视频（MiniMax H3 / FastH3 系列）。model 默认 minimax-h3；"
        "lift 两支 = base/edit 骨架 + 尾部确定性放大（scale 指定倍率）；"
        "fasth3 两支 = 8 步蒸馏档（更快，牺牲动作与音频保真，适合草稿）。"
        "默认同步等待；长任务传 background=\"pending\" 异步，再用 get_task 轮询。"
        "尺寸档位、参考槽数量与各字段生效性查 get_tool_info。"
        "写 H3 / FastH3 提示词前先调 get_skills 装配套 skill。"
    ),
)
async def generate_video_tool(
    prompt: str,
    model: str = Field(
        default="minimax-h3",
        description="模型选择（MiniMax H3 / FastH3 系列）；逐模型能力、尺寸档位与生效字段查 get_tool_info。",
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
    scale: float | None = Field(
        None, ge=1.0, le=4.0,
        description="输出 = 768p 画布 × scale（默认 1.875 → 2520x1440）；仅 minimax-h3-lift / -lift-edit，其它模型传了报 400。",
    ),
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
        scale=scale,
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
        "**要不要给用户这个地址由你自己判断**：用户在等结果、一次出了很多张、或他明显没在看页面时，"
        "主动给（或直接替他打开）；他已经在页面上盯着、或只是顺手改一张图，就不必打扰——"
        "不要变成每生成一次就复读一遍地址的噪音。"
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


@mcp.tool(
    name="pin_view_item",
    description=(
        "把一张产出卡片钉到可视化页面的**任务看板**（顶部那块无限画布），用户在页面上一眼看全，"
        "不必你逐个把文件拉给他看。产物来源三选一：`url` / `path` / `task_id`（优先级依次降低）。"
        "`x`/`y` 给了就摆在那个坐标（画布可平移缩放、坐标允许负数），不给就自动排到空位 —— "
        "要表达顺序、对照或分组（例如分镜 1-5 横排、A/B 两列、按角色分区）就自己给坐标，"
        "卡片尺寸用 `w`/`h`。没有产物也能钉纯文本卡（`note` + kind=text），用来写进度说明或小结。"
        "`path` 指向 input/output 内的目录时会自动落成**目录卡**（没有可预览的产物），"
        "用户在页面上点它就跳到该目录的文件列表。"
        "一轮钉完、或准备切换任务时，用 clear_view_board 清空（内容会存进历史，随时可回看）。"
        "**钉完自己判断要不要帮用户打开页面**：用户在等结果 / 一次钉了很多 / 他没在看页面时，"
        "调 get_view_url 把地址给他（或替他打开）；他正盯着页面看、或只是补一张图，就不必打扰。"
    ),
)
async def pin_view_item(
    title: str = "",
    url: str = "",
    path: str = "",
    task_id: str = "",
    note: str = "",
    kind: str = "",
    model: str = "",
    x: float | None = None,
    y: float | None = None,
    w: float | None = None,
    h: float | None = None,
) -> dict[str, Any]:
    """钉一张卡片到任务看板。详见工具描述；坐标缺省时后端自动找空位。"""
    item = board.pin(
        title=title, url=url, path=path, task_id=task_id, note=note,
        kind=kind, model=model, x=x, y=y, w=w, h=h,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "id": item["id"],
        "item": item,
        "count": len(board.snapshot()),
        "view_url": view_url(),
    }
    if not item.get("url") and item.get("path"):
        # 产物在 input/output 之外，页面没法预览，明确告诉 agent 别以为钉成功了
        payload["warning"] = (
            f"产物不在 ComfyUI 的 input/output 目录内，页面上只能看到路径、点不开：{item['path']}"
        )
    return payload


@mcp.tool(
    name="clear_view_board",
    description=(
        "清空任务看板。内容会**存进历史归档**，随时可在页面「历史」里回看，或用 "
        "get_view_board_history 列出 —— 所以换任务时尽管清，不会丢。"
        "什么时候清：这一轮成果已经交付完、或要切到另一个任务时，别把上一轮的卡片留在旁边造成混淆。"
        "`label` 给这份归档起个名字（如「第 1 轮 · 分镜草图」），回看时好认。"
    ),
)
async def clear_view_board(label: str = "") -> dict[str, Any]:
    """清空看板并归档。返回归档摘要（id / label / 条数）。"""
    entry = board.archive(label)
    return {
        "ok": True,
        "cleared": entry["count"] if entry else 0,
        "archived": (
            {"id": entry["id"], "label": entry["label"], "count": entry["count"]} if entry else None
        ),
        "count": len(board.snapshot()),
    }


@mcp.tool(
    name="get_view_board_history",
    description=(
        "列出任务看板的历史归档（每次 clear_view_board 都会存一份），用来回顾上一轮钉过什么、"
        "或给用户做总结时还原当时都出了哪些东西。传 `archive_id` 看某一份的完整卡片"
        "（含标题、地址、note 与画布坐标）；不传就只列摘要（id / label / 时间 / 张数）。"
    ),
)
async def get_view_board_history(archive_id: str = "", limit: int = 20) -> dict[str, Any]:
    """历史归档：不传 id 列摘要，传了就回该份的完整卡片。"""
    if archive_id:
        entry = next((h for h in board._history if h["id"] == archive_id), None)
        if not entry:
            return {"ok": False, "error": f"No archived board with id '{archive_id}'."}
        return {"ok": True, "archive": entry}
    rows = [
        {"id": h["id"], "label": h["label"], "created": h["created"], "count": h["count"]}
        for h in reversed(board._history)
    ][:max(1, limit)]
    return {"ok": True, "history": rows, "count": len(board._history)}


# ---- 工具 13：get_skills -------------------------------------------------
@mcp.tool(
    name="get_skills",
    description=(
        "Returns the companion agent-skills that make this MCP server correct and safe to use. "
        "CALL THIS FIRST whenever the user asks for image/video generation, H3 prompt writing, "
        "model registration, or 'which tool/parameter should I use'. Each entry carries an "
        "install_url and a when_to_use field — install the skill (bare URL is enough, no target "
        "dir needed) before proceeding. The skills hold the official prompt formulas, capability "
        "boundaries, and per-model guidance that this server's tool descriptions cannot embed "
        "without bloating every one of them."
    ),
)
async def get_skills() -> dict[str, Any]:
    """配套 agent-skill 清单。agent 接到图像/视频生成、H3 提示词、模型注册等请求时，
    应先调用本工具拿到 skill 列表并安装，再继续。返回结构化 dict（server / version / skills[]）。
    """
    return {
        "server": "comfyui-roundabout",
        "version": VERSION,
        "skills": [
            {
                "name": "roundabout",
                "install_url": "https://github.com/0c0/roundabout-skill",
                "purpose": "本 MCP 服务器的总入口用法：模型选择、请求参数、排障、注册/下线工作流。",
                "when_to_use": "拿不准用哪个工具/参数、要注册或排查工作流、或想看某模型能力边界时查它。",
                "source": "roundabout",
                "published": True,
            },
            {
                "name": "h3-playbook",
                "install_url": "https://github.com/0c0/h3-playbook-skill",
                "purpose": "MiniMax H3 / FastH3 官方使用手册口径：提示词三段式公式、三类生成模式写法差异、"
                           "时长/分辨率/宽高比/输入上限。",
                "when_to_use": "写 MiniMax H3 / FastH3 视频提示词前必查，确保提示词结构正确、不踩能力边界。",
                "source": "roundabout",
                "published": True,
            },
            {
                "name": "qwen-image-prompt-writing",
                "install_url": "https://github.com/0c0/qwen-image2.1-prompt-writing-skill",
                "purpose": "Qwen-Image-2.1 官方 Prompt Enhancer 契约的手写替身：t2i 观察者报告与 edit 改写指令，产出 rewritten_prompt + wh_ratio / ratio_follow 结构。",
                "when_to_use": "用网关 qwen-image-2.1 档出图/改图，或要把一句粗糙需求扩写成该模型能吃的描述前必查。",
                "source": "roundabout",
                "published": True,
            },
            {
                "name": "h3-prompt-writing",
                "install_url": "https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing",
                "purpose": "MiniMax 官方的 H3 提示词写作指南：T2VA / I2VA / FL2VA / L2VA 的最终提示词结构，以及 Ref2VA 六段改写格式。",
                "when_to_use": "写 H3 提示词时与 h3-playbook 配合：playbook 管能不能做，writing 管字段与段落怎么写。",
                "source": "official",
                "published": True,
            },
        ],
        "note": "source 区分两类：roundabout = 本网关维护，official = 模型厂商自己维护（内容以其仓库为准）。"
                "其余专精 skill 不随本仓库发布，不在此列出。安装方式：把 install_url 交给 agent 的 skill "
                "安装流程（裸 URL 即可，无需指定目录）。",
    }


# ---- 工具 14：check_weights ------------------------------------------------
@mcp.tool(
    name="check_weights",
    description=(
        "权重体检（只读、不占 GPU、不触发生成）：列出内置工作流当前缺失的权重文件，"
        "并给出每条的下载命令与目标目录，避免等到 generate 报 400 才发现权重没下。"
        "首次部署、换模型、或某个档跑不起来时先调它。"
        "返回 missing[]，每条含 file / dir / repo / size_gb / used_by / command。"
    ),
)
async def check_weights(include_unreferenced: bool = False) -> dict[str, Any]:
    return weights.check(include_unreferenced=include_unreferenced)


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
