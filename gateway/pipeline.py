"""生成流水线：OpenAI 请求 → 工作流注入 → ComfyUI 执行 → OpenAI 响应。"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from .comfy_client import ComfyClient, collect_images
from .config import settings
from .errors import APIError
from .params import (
    _asset_ext,
    apply_presets,
    ext_and_mime,
    load_image_input,
    mime_for_ext,
    parse_size,
    resolve_seed,
    resolve_video_size,
    sniff_image,
    split_negative_prompt,
)
from .registry import CAP_IMG2IMG, ModelSpec, build_workflow, registry
from .schemas import ImageGenerationRequest, ImageResponse, VideoGenerationRequest, VideoResponse
from .store import store

log = logging.getLogger("roundabout.pipeline")


def _trunc(text: Any, limit: int = 200) -> str:
    """截断可能很长的 prompt，避免日志被刷屏（不记录 base64 等体积极大的字段）。"""
    if text is None:
        return "-"
    s = str(text)
    return s if len(s) <= limit else s[:limit] + f"...(truncated {len(s) - limit} chars)"


_semaphore: asyncio.Semaphore | None = None


def semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))
    return _semaphore


@dataclass
class RenderedImage:
    data: bytes
    ext: str
    mime: str
    # ComfyUI 原生文件引用（SaveImage/VHS 写盘后返回的 filename/subfolder/type）。
    # 非空时，url/path 模式可直接返回 ComfyUI 自己的「固化」地址，无需二次落盘。
    ref: dict[str, Any] | None = None


def _native_view_url(ref: dict[str, Any]) -> str:
    """拼出 ComfyUI 自带的 /view 访问地址（相对路径，由 handler 补全 host）。"""
    from urllib.parse import urlencode

    params = {
        "filename": ref.get("filename", ""),
        "subfolder": ref.get("subfolder", "") or "",
        "type": ref.get("type", "output") or "output",
    }
    return "/view?" + urlencode(params)


def _native_abs_path(ref: dict[str, Any]) -> str | None:
    """拼出 ComfyUI 输出目录下的真实磁盘绝对路径；拿不到根目录时返回 None。"""
    root = _comfy_output_root()
    if not root:
        return None
    sub = (ref.get("subfolder") or "").strip("/")
    fname = ref.get("filename", "")
    if not fname:
        return None
    path = Path(root)
    if sub:
        path = path / sub
    return str((path / fname).resolve())


_OUTPUT_ROOT_CACHE: str | None = "<<unset>>"


def _comfy_output_root() -> str | None:
    """解析 ComfyUI 输出根目录：直接从进程内 folder_paths 获取。

    节点运行在 ComfyUI 进程内，应始终经由 ComfyUI 的 folder_paths 接口动态取得，
    不依赖任何本地配置——这是节点可分发的前提（每个用户安装路径不同）。
    拿不到 folder_paths（如独立/远端模式）时返回 None，调用方自动降级到网关缓存路径。
    """
    global _OUTPUT_ROOT_CACHE
    if _OUTPUT_ROOT_CACHE != "<<unset>>":
        return _OUTPUT_ROOT_CACHE or None
    root = ""
    try:  # 节点即 ComfyUI 进程内，folder_paths 已随 ComfyUI 加载
        import folder_paths  # type: ignore

        root = folder_paths.get_output_directory() or ""
    except Exception:  # noqa: BLE001 - 不可用时降级为 None
        root = ""
    _OUTPUT_ROOT_CACHE = root or None
    return _OUTPUT_ROOT_CACHE


_INPUT_ROOT_CACHE: str | None = "<<unset>>"


def _comfy_input_root() -> str | None:
    """解析 ComfyUI input 根目录：直接从进程内 folder_paths 获取。

    参考素材上传后落在 input 文件夹，回显绝对路径时需要它。同样不依赖本地配置，
    以保证节点在不同用户环境间可分发。
    """
    global _INPUT_ROOT_CACHE
    if _INPUT_ROOT_CACHE != "<<unset>>":
        return _INPUT_ROOT_CACHE or None
    root = ""
    try:
        import folder_paths  # type: ignore

        root = folder_paths.get_input_directory() or ""
    except Exception:  # noqa: BLE001
        root = ""
    _INPUT_ROOT_CACHE = root or None
    return _INPUT_ROOT_CACHE


def _is_in_input_dir(path: str) -> bool:
    """判断本地绝对路径是否已在 ComfyUI 配置的 input 根目录内（含子目录）。"""
    root = _comfy_input_root()
    if not root:
        return False
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _resolve_local_ref(v: str) -> str | None:
    """把参考素材地址规范化为「可用于本地判定的绝对路径」；非文件路径返回 None。

    解析优先级（匹配 ComfyUI 原生 loader「相对 input 目录」的约定）：
    - 绝对路径 → 原样使用；
    - 相对路径 → 先按 ComfyUI input 目录解析，解析不到再按进程 CWD 解析；
    - http(s) / dataURL / base64 等非文件路径 → 返回 None，交由 ``load_image_input`` 处理。
    """
    v = v.strip()
    if not v or v.startswith(("http://", "https://", "data:")):
        return None
    if os.path.isabs(v):
        return v
    root = _comfy_input_root()
    if root:
        cand = os.path.join(root, v)
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return os.path.abspath(v)


def _input_rel(path: str) -> tuple[str, str]:
    """把 input 目录内的绝对路径拆成 (subfolder, filename)，根目录下的文件 subfolder 为空。"""
    root = Path(_comfy_input_root() or ".").resolve()
    rel = Path(path).resolve().relative_to(root)
    parts = rel.parts
    if len(parts) <= 1:
        return "", parts[0] if parts else ""
    return "/".join(parts[:-1]), parts[-1]


def _input_abs_path(filename: str, subfolder: str) -> str | None:
    root = _comfy_input_root()
    if not root or not filename:
        return None
    path = Path(root)
    if subfolder:
        path = path / subfolder
    return str((path / filename).resolve())


async def generate(
    req: ImageGenerationRequest,
    client: ComfyClient,
    *,
    request_id: str,
    image_inputs: list[bytes] | None = None,
    mask_input: bytes | None = None,
) -> ImageResponse:
    spec = registry.resolve(req.model)
    # 视频模型不应走图片链路（图片请求没有 reference_images 等字段，会触发 AttributeError）。
    # 正常由 handlers.images_generations 在入口处自动路由到视频链路；此处仅作兜底防御。
    if spec.is_video:
        raise APIError(
            f"Model `{spec.name}` is a video model. Use POST /v1/videos/generations.",
            param="model",
        )
    n = _validate_n(req.n)
    response_format = (req.response_format or "b64_json").lower()

    # prompt 必填校验（从 schema 下沉到这）：绑定 prompt 的模型就必须给提示词；
    # promptless 工具类模型（去背景等）跳过。
    if spec.binds("prompt") and not (req.prompt or "").strip():
        raise APIError(f"Model `{spec.name}` requires a `prompt`.", param="prompt")

    # ---- 0.5 负向分流：支持负向的模型把 prompt 内 "no/without/not X" 抽进 negative_prompt ----
    prompt_in = req.prompt or ""
    neg_in = req.negative_prompt if req.negative_prompt is not None else spec.defaults.get("negative_prompt")
    if settings.auto_split_negative and spec.binds("negative_prompt") and req.prompt:
        prompt_in, neg_in = split_negative_prompt(req.prompt, neg_in, True)
        if prompt_in != req.prompt:
            log.debug("negative-split applied: prompt=%r -> negative=%r", _trunc(req.prompt, 80), _trunc(neg_in, 80))

    # ---- 0. 请求到达日志（关键信息，不含 base64 等大体积字段） ----
    img2img = bool(image_inputs) or bool(req.image)
    log.info(
        "req=%s | IMAGE gen | model=%s n=%d size=%s quality=%s fmt=%s img2img=%s mode=%s prompt=%s",
        request_id, spec.name, n, req.size or "-", req.quality or "-",
        response_format, img2img, req.mode or "-", _trunc(prompt_in),
    )

    # ---- 1. 收集图生图输入 ----
    images = list(image_inputs or [])
    if not images and req.image:
        raw_list = req.image if isinstance(req.image, list) else [req.image]
        images = [await load_image_input(str(x)) for x in raw_list if x]
    if not mask_input and req.mask:
        mask_input = await load_image_input(str(req.mask), param="mask")

    if images and not spec.supports_img2img:
        # 调用方（尤其是 agent）常忘了换模型，直接列出可用的编辑模型省一轮试错
        alts = [s.name for s in registry.all() if s.supports_img2img and not s.is_video]
        hint = f" Image-capable models: {', '.join(alts)}." if alts else ""
        raise APIError(
            f"Model `{spec.name}` does not support image-to-image "
            f"(capabilities: {', '.join(sorted(spec.capabilities))}).{hint}",
            param="image",
        )
    if CAP_IMG2IMG in spec.capabilities and not spec.capabilities - {CAP_IMG2IMG} and not images:
        raise APIError(f"Model `{spec.name}` requires an input `image`.", param="image")

    # ---- 2. 上传输入图，拿到 LoadImage 能引用的文件名 ----
    uploaded: str | None = None
    uploaded_mask: str | None = None
    if images:
        ext, _ = sniff_image(images[0])
        uploaded = await client.upload_image(images[0], f"hermes_{request_id}_src.{ext}")
    if mask_input and spec.binds("mask"):
        ext, _ = sniff_image(mask_input)
        uploaded_mask = await client.upload_image(mask_input, f"hermes_{request_id}_mask.{ext}")

    # ---- 3. 组装语义参数 ----
    width, height = parse_size(req.size, spec)
    values: dict[str, Any] = {
        "prompt": prompt_in,
        "negative_prompt": neg_in,
        "width": width,
        "height": height,
        "steps": req.steps,
        "cfg": req.cfg,
        "sampler_name": req.sampler_name,
        "scheduler": req.scheduler,
        "denoise": req.denoise,
        "image": uploaded,
        "mask": uploaded_mask,
        "filename_prefix": req.filename_prefix,  # 透传：None 时回落模板默认前缀
        "mode": req.mode,
    }

    # 模型专属 mode 校验（如 Ideogram4 的 Quality / Default / Turbo）
    if req.mode is not None and spec.mode_choices and req.mode not in spec.mode_choices:
        raise APIError(
            f"Model `{spec.name}`: invalid `mode` {req.mode!r}. "
            f"Allowed: {', '.join(spec.mode_choices)}.",
            param="mode",
        )

    values = apply_presets(values, spec, req.quality, req.style)
    # 显式入参优先级最高：覆盖 defaults / presets
    for key in ("steps", "cfg", "sampler_name", "scheduler", "denoise"):
        explicit = getattr(req, key, None)
        if explicit is not None:
            values[key] = explicit
        elif values.get(key) is None:
            values[key] = spec.defaults.get(key)
    if values.get("width") is None:
        values["width"] = spec.defaults.get("width")
    if values.get("height") is None:
        values["height"] = spec.defaults.get("height")

    # JOB_TIMEOUT=0（或不传正 timeout）→ 无限等待，仅由 ComfyUI 任务状态判定健康；
    # 否则沿用「模型自身 timeout 优先、回落全局 JOB_TIMEOUT」的旧逻辑（向后兼容）。
    timeout = 0.0 if (settings.job_timeout and settings.job_timeout <= 0) else (spec.timeout or settings.job_timeout)

    # ---- 4. 执行（能批就批，不能批就串行多次） ----
    t0 = time.monotonic()
    rendered: list[RenderedImage] = []
    used_seeds: list[int] = []  # 捕获每次实际解析后的种子，用于响应回显

    if spec.supports_batch and n > 1:
        values["batch_size"] = n
        values["seed"] = resolve_seed(req.seed)
        used_seeds.append(values["seed"])
        rendered, _ = await _run_once(spec, values, req.workflow_overrides, client, timeout, request_id)
    else:
        if spec.supports_batch:
            values["batch_size"] = 1
        for i in range(n):
            values["seed"] = resolve_seed(req.seed, i)
            used_seeds.append(values["seed"])
            rendered_part, _ = await _run_once(spec, values, req.workflow_overrides, client, timeout, f"{request_id}-{i}")
            rendered += rendered_part

    if not rendered:
        raise APIError(
            f"Workflow `{spec.name}` finished but produced no image. "
            "Check that the workflow contains a SaveImage node (or set `output_node` in models.yaml).",
            status_code=502,
            err_type="api_error",
            code="no_output",
        )

    rendered = rendered[:n]
    log.info(
        "req=%s model=%s n=%d images=%d elapsed=%.1fs",
        request_id, spec.name, n, len(rendered), time.monotonic() - t0,
    )

    # ---- 5. 组装 OpenAI 响应 ----
    # 优先返回 ComfyUI 自身的「固化」地址（SaveImage/VHS 已写盘，无需二次落盘）：
    #   url      -> /view?filename=...&subfolder=...&type=...（handler 补全 host，不受网关 TTL 影响）
    #   file/path-> ComfyUI 输出目录下的磁盘绝对路径
    # 拿不到原生引用时（如自定义输出节点），回落到网关缓存 store.save（受 OUTPUT_TTL 约束）。
    data = []
    for img in rendered:
        if response_format in ("file", "path"):
            native = _native_abs_path(img.ref) if img.ref else None
            if native:
                data.append({"path": native})
            else:
                name = store.save(img.data, img.ext)
                abs_path = store.path(name)
                data.append({"path": str(abs_path) if abs_path else f"/v1/images/files/{name}"})
        elif response_format == "url":
            if img.ref:
                data.append({"url": _native_view_url(img.ref)})
            else:
                name = store.save(img.data, img.ext)
                data.append({"url": f"/v1/images/files/{name}"})
        else:
            data.append({"b64_json": base64.b64encode(img.data).decode("ascii")})

    seed_field: int | list[int] | None = used_seeds[0] if len(used_seeds) == 1 else used_seeds
    return ImageResponse(created=int(time.time()), data=data, seed=seed_field)  # type: ignore[arg-type]


async def generate_video(
    req: VideoGenerationRequest,
    client: ComfyClient,
    *,
    request_id: str,
    image_inputs: list[bytes] | None = None,
    on_submit: Any | None = None,
) -> VideoResponse:
    """视频生成：与 generate 同构，但产物默认走 url（视频 base64 体积过大）。

    ``on_submit(prompt_id, workflow)`` 在提交给 ComfyUI 成功后调用（供异步任务回填
    prompt_id 与工作流快照；失败的任务也能弹窗查看 workflow）。
    """
    spec = registry.resolve(req.model)
    if not spec.is_video:
        raise APIError(
            f"Model `{spec.name}` is not a video model (mode={spec.mode}). Use /v1/images/generations instead.",
            param="model",
        )
    n = _validate_n(req.n)
    # 视频默认 url：base64 一个几 MB 的视频不现实
    response_format = (req.response_format or "url").lower()

    # 时长 1–15s（MiniMax H3 等视频模型支持的区间）
    if req.duration is not None and not (1 <= req.duration <= 15):
        raise APIError("`duration` must be between 1 and 15 seconds.", param="duration")

    # ---- 0.5 负向分流：支持负向的模型把 prompt 内 "no/without/not X" 抽进 negative_prompt ----
    prompt_in = req.prompt
    neg_in = req.negative_prompt if req.negative_prompt is not None else spec.defaults.get("negative_prompt")
    if settings.auto_split_negative and spec.binds("negative_prompt"):
        prompt_in, neg_in = split_negative_prompt(req.prompt, neg_in, True)
        if prompt_in != req.prompt:
            log.debug("negative-split applied: prompt=%r -> negative=%r", _trunc(req.prompt, 80), _trunc(neg_in, 80))

    # ---- 0. 请求到达日志（关键信息，不含 base64 等大体积字段） ----
    img2img = bool(image_inputs) or bool(req.image)
    log.info(
        "req=%s | VIDEO gen | model=%s n=%d size=%s fmt=%s img2img=%s duration=%s fps=%s num_frames=%s prompt=%s",
        request_id, spec.name, n, req.size or "-", response_format, img2img,
        req.duration or "-", req.fps or "-", req.num_frames or "-", _trunc(req.prompt),
    )

    # ---- 1. 收集图生视频输入 ----
    images = list(image_inputs or [])
    if not images and req.image:
        raw_list = req.image if isinstance(req.image, list) else [req.image]
        images = [await load_image_input(str(x)) for x in raw_list if x]
    if images and not spec.supports_img2img:
        raise APIError(
            f"Video model `{spec.name}` does not support image-to-video "
            f"(capabilities: {', '.join(sorted(spec.capabilities))}).",
            param="image",
        )

    # ---- 2. 上传输入图 ----
    uploaded: str | None = None
    if images:
        ext, _ = sniff_image(images[0])
        uploaded = await client.upload_image(images[0], f"hermes_{request_id}_src.{ext}")

    # ---- 3. 组装语义参数 ----
    width, height = resolve_video_size(req.size, spec)
    values: dict[str, Any] = {
        "prompt": prompt_in,
        "negative_prompt": neg_in,
        "width": width,
        "height": height,
        "steps": req.steps,
        "cfg": req.cfg,
        "sampler_name": req.sampler_name,
        "scheduler": req.scheduler,
        "denoise": req.denoise,
        "image": uploaded,
        "duration": req.duration,
        "fps": req.fps,
        "num_frames": req.num_frames,
        "filename_prefix": req.filename_prefix,  # 透传：None 时回落模板默认前缀
    }
    values = apply_presets(values, spec, req.quality, req.style)
    for key in ("steps", "cfg", "sampler_name", "scheduler", "denoise", "duration", "fps", "num_frames"):
        explicit = getattr(req, key, None)
        if explicit is not None:
            values[key] = explicit
        elif values.get(key) is None:
            values[key] = spec.defaults.get(key)
    if values.get("width") is None:
        values["width"] = spec.defaults.get("width")
    if values.get("height") is None:
        values["height"] = spec.defaults.get("height")

    # JOB_TIMEOUT=0（或不传正 timeout）→ 无限等待，仅由 ComfyUI 任务状态判定健康；
    # 否则沿用「模型自身 timeout 优先、回落全局 JOB_TIMEOUT」的旧逻辑（向后兼容）。
    timeout = 0.0 if (settings.job_timeout and settings.job_timeout <= 0) else (spec.timeout or settings.job_timeout)

    # ---- 4. 执行（能批就批，不能批就串行多次） ----
    t0 = time.monotonic()
    rendered: list[RenderedImage] = []
    all_refs: list[dict[str, Any]] = []  # 跨 batch 累积的 image 类参考素材描述符（用于响应回显）
    used_seeds: list[int] = []  # 捕获每次实际解析后的种子，用于响应回显

    if spec.supports_batch and n > 1:
        values["batch_size"] = n
        values["seed"] = resolve_seed(req.seed)
        used_seeds.append(values["seed"])
        rendered_part, ref_descriptors = await _run_once(spec, values, req.workflow_overrides, client, timeout, request_id, req_video=req, on_submit=on_submit)
        rendered += rendered_part
        all_refs += ref_descriptors
    else:
        if spec.supports_batch:
            values["batch_size"] = 1
        for i in range(n):
            values["seed"] = resolve_seed(req.seed, i)
            used_seeds.append(values["seed"])
            rendered_part, ref_descriptors = await _run_once(spec, values, req.workflow_overrides, client, timeout, f"{request_id}-{i}", req_video=req, on_submit=on_submit)
            rendered += rendered_part
            all_refs += ref_descriptors

    if not rendered:
        raise APIError(
            f"Video workflow `{spec.name}` finished but produced no output. "
            "Check that the workflow contains a VHS_VideoCombine / SaveAnimatedWEBM node "
            "(or set `output_node` in models.yaml).",
            status_code=502,
            err_type="api_error",
            code="no_output",
        )

    rendered = rendered[:n]
    log.info(
        "req=%s model=%s n=%d videos=%d elapsed=%.1fs",
        request_id, spec.name, n, len(rendered), time.monotonic() - t0,
    )

    # ---- 5. 组装响应（优先 ComfyUI 原生固化地址；拿不到时回落网关缓存）----
    data = []
    for vid in rendered:
        if response_format in ("file", "path"):
            native = _native_abs_path(vid.ref) if vid.ref else None
            if native:
                data.append({"path": native})
            else:
                name = store.save(vid.data, vid.ext)
                abs_path = store.path(name)
                data.append({"path": str(abs_path) if abs_path else f"/v1/videos/files/{name}"})
        elif response_format == "b64_json":
            data.append({"b64_json": base64.b64encode(vid.data).decode("ascii")})
        else:
            if vid.ref:
                data.append({"url": _native_view_url(vid.ref)})
            else:
                name = store.save(vid.data, vid.ext)
                data.append({"url": f"/v1/videos/files/{name}"})

    references = _build_reference_echo(all_refs, response_format)
    seed_field: int | list[int] | None = used_seeds[0] if len(used_seeds) == 1 else used_seeds
    return VideoResponse(created=int(time.time()), data=data, seed=seed_field, references=references)  # type: ignore[arg-type]


async def _run_once(
    spec: ModelSpec,
    values: dict[str, Any],
    overrides: dict[str, Any] | None,
    client: ComfyClient,
    timeout: float,
    trace: str,
    req_video: VideoGenerationRequest | None = None,
    on_submit: Any | None = None,
) -> tuple[list[RenderedImage], list[dict[str, Any]]]:
    workflow = build_workflow(spec, values, overrides)
    ref_descriptors: list[dict[str, Any]] = []
    # 视频参考资源：先接入实际提供的参考素材，再删除未上传的节点，最后提交给 ComfyUI
    if spec.is_video and req_video is not None:
        ref_descriptors = await _wire_references(workflow, spec, req_video, client, trace)
        dropped = _prune_unused_references(workflow, spec, req_video)
        if dropped:
            log.info("req=%s pruned %d unused reference node(s) before submit", trace, dropped)
    async with semaphore():
        prompt_id = await client.submit(workflow, client_id=f"hermes-gateway-{trace}")
        log.debug("req=%s submitted prompt_id=%s", trace, prompt_id)
        # 提交成功后回调（异步任务用它把 prompt_id + workflow 快照写回任务表，失败也能查）
        if on_submit is not None:
            try:
                on_submit(prompt_id, workflow)
            except Exception:  # noqa: BLE001 - 回调失败不影响生成
                log.exception("req=%s on_submit callback failed", trace)
        entry = await client.wait(
            prompt_id,
            timeout=timeout,
            grace=settings.job_grace,
            poll_interval=settings.poll_interval,
            poll_interval_max=settings.poll_interval_max,
        )
        refs = collect_images(entry, spec.output_node)
        out: list[RenderedImage] = []
        for ref in refs:
            raw = await client.fetch_image(ref)
            ext, mime = ext_and_mime(ref.get("filename"), raw)
            out.append(RenderedImage(data=raw, ext=ext, mime=mime, ref=ref))
        return out, ref_descriptors


def _validate_n(n: int) -> int:
    if n < 1:
        raise APIError("`n` must be at least 1.", param="n")
    if n > settings.max_n:
        raise APIError(f"`n` must be between 1 and {settings.max_n}.", param="n")
    return n


def _prune_unused_references(
    wf: dict[str, Any], spec: ModelSpec, req: VideoGenerationRequest
) -> int:
    """按请求实际提供的参考资源数量，删除工作流 JSON 副本中多余的参考节点。

    仅做删除（用户明确要求）：接口未上传的资源，其对应的 LoadImage / LoadVideo / LoadAudio
    节点及 aggregator 上的输入键一并移除；已提供资源的节点保留（接入由调用方负责）。
    返回被删除的节点数，便于日志。

    级联：删除某个 loader 节点（如 LoadVideo）时，会连带删除依赖它的下游节点
    （如 GetVideoComponents），但**绝不删除聚合节点**（aggregator）。
    """
    ref = spec.references
    if not ref:
        return 0
    agg = str(ref.get("aggregator") or "")
    agg_node = wf.get(agg)
    if not isinstance(agg_node, dict):
        return 0
    agg_ins = agg_node.setdefault("inputs", {})

    removed = 0

    def _links_to(node: dict[str, Any], target_id: str) -> bool:
        for val in (node.get("inputs") or {}).values():
            if isinstance(val, list) and len(val) >= 1 and str(val[0]) == str(target_id):
                return True
        return False

    def _drop(key: str, nid: str) -> None:
        nonlocal removed
        agg_ins.pop(key, None)
        if wf.pop(nid, None) is not None:
            removed += 1
            # 级联删除依赖该 loader 的下游节点（如 LoadVideo -> GetVideoComponents），
            # 但绝不删除聚合节点（aggregator）。下游节点的下游由循环自然覆盖。
            for k, v in list(wf.items()):
                if k == agg or not isinstance(v, dict):
                    continue
                if _links_to(v, nid):
                    if wf.pop(k, None) is not None:
                        removed += 1

    imgs = req.reference_images or []
    for i, nid in enumerate(ref.get("images", [])):
        if i >= len(imgs):
            _drop(f"ref_images.ref_image_{i}", str(nid))

    vids = req.reference_videos or []
    for i, nid in enumerate(ref.get("videos", [])):
        if i >= len(vids):
            # 参考视频与其音轨（ref_video_audio_N）同源、同节点，一并移除
            _drop(f"ref_videos.ref_video_{i}", str(nid))
            _drop(f"ref_video_audios.ref_video_audio_{i}", str(nid))

    auds = req.reference_audios or []
    for i, nid in enumerate(ref.get("audios", [])):
        if i >= len(auds):
            _drop(f"ref_audios.ref_audio_{i}", str(nid))

    return removed


async def _wire_references(
    wf: dict[str, Any],
    spec: ModelSpec,
    req: VideoGenerationRequest,
    client: ComfyClient,
    trace: str,
) -> list[dict[str, Any]]:
    """把请求中实际提供的参考素材接到对应 loader 节点，并返回 image 类描述符。

    三类 loader（LoadImage / LoadVideo / LoadAudio）都是 ComfyUI input 文件夹内的
    **文件名** COMBO，逻辑统一：把 ``_stage_ref`` 解析出的文件名写入节点对应的 widget
    （image / file / audio）。未提供的索引由 ``_prune_unused_references`` 删除对应节点。
    仅 image 类记录描述符供响应回显；video/audio 仅接入、不回显。
    """
    ref = spec.references
    if not ref:
        return []
    descriptors: list[dict[str, Any]] = []

    widget = {"image": "image", "video": "file", "audio": "audio"}

    # ---- 参考图：LoadImage.inputs.image ----
    imgs = req.reference_images or []
    for i, nid in enumerate(ref.get("images", [])):
        if i >= len(imgs):
            break
        node_value, desc = await _stage_ref(str(imgs[i]), "image", "reference_images", client, trace, i)
        node = wf.get(str(nid))
        if isinstance(node, dict):
            node.setdefault("inputs", {})[widget["image"]] = node_value
        if desc:
            descriptors.append(desc)

    # ---- 参考视频：LoadVideo.inputs.file ----
    vids = req.reference_videos or []
    for i, nid in enumerate(ref.get("videos", [])):
        if i >= len(vids):
            break
        node_value, _ = await _stage_ref(str(vids[i]), "video", "reference_videos", client, trace, i)
        node = wf.get(str(nid))
        if isinstance(node, dict):
            node.setdefault("inputs", {})[widget["video"]] = node_value

    # ---- 参考音频：LoadAudio.inputs.audio ----
    auds = req.reference_audios or []
    for i, nid in enumerate(ref.get("audios", [])):
        if i >= len(auds):
            break
        node_value, _ = await _stage_ref(str(auds[i]), "audio", "reference_audios", client, trace, i)
        node = wf.get(str(nid))
        if isinstance(node, dict):
            node.setdefault("inputs", {})[widget["audio"]] = node_value

    return descriptors


async def _stage_ref(
    value: str, kind: str, param: str, client: ComfyClient, trace: str, idx: int
) -> tuple[str, dict[str, Any] | None]:
    """把一个参考素材（base64 / dataURL / http(s) URL / 本地路径）落地为 input 文件夹内的文件名。

    三类 loader（LoadImage / LoadVideo / LoadAudio）都只按 ComfyUI ``input`` 文件夹内的
    **文件名**解析（ComfyUI 的 ``[output]/[input]/[temp]`` 标注后缀在当前构建里存在 off-by-one
    解析 bug，且 loaders 校验只认 input 目录，故只有 input 内的本地文件可免转存）：
    - 本地路径（绝对路径，或等于 input 目录内的相对路径）且文件已在 ComfyUI 配置的 input
      目录下 → **不转存**，直接返回其相对文件名（含子目录时返回 subfolder/filename），
      原文件原地引用；
    - 其余来源（远程 / dataURL / input 目录外的本地文件，含 output/temp 目录）→ 统一读字节后
      上传到 input 文件夹（原文件不动）。
    返回 ``(节点填入值, image 描述符或 None)``；仅 image 类记录描述符用于响应回显。
    """
    v = value.strip()
    # —— 已在 input 目录内的本地文件：免转存，直接按文件名引用（保留子目录） ——
    # 相对路径先按 input 目录解析（匹配原生 loader 约定），再回退 CWD；http/dataURL/base64 返回 None。
    abs_v = _resolve_local_ref(v)
    if abs_v and os.path.isfile(abs_v) and _is_in_input_dir(abs_v):
        sub, fname = _input_rel(abs_v)
        node_value = f"{sub}/{fname}" if sub else fname
        desc = None
        if kind == "image":
            desc = {
                "type": "image",
                "filename": fname,
                "subfolder": sub,
                "b64": None,
                "source": abs_v,
                "local": True,
                "root": "input",
            }
        return node_value, desc
    # —— 其余来源：读字节并上传到 input 文件夹 ——
    data = await load_image_input(v, param=param, max_mb=settings.max_input_asset_mb)
    ext = _asset_ext(v, data, kind)
    tag = {"image": "img", "video": "vid", "audio": "aud"}[kind]
    name = await client.upload_image(
        data, f"hermes_{trace}_ref{tag}_{idx}.{ext}", content_type=mime_for_ext(ext)
    )
    desc = None
    if kind == "image":
        sub, _, fname = name.rpartition("/")
        desc = {"type": "image", "filename": fname, "subfolder": sub, "b64": data, "source": v}
    return name, desc


def _build_reference_echo(
    descriptors: list[dict[str, Any]], response_format: str
) -> list[dict[str, Any]] | None:
    """按 response_format 把已接入的 image 类参考素材回显为 url / path / b64_json。"""
    if not descriptors:
        return None
    out: list[dict[str, Any]] = []
    for d in descriptors:
        entry: dict[str, Any] = {"type": "image"}
        src = d.get("source") or ""
        # 回显原始来源（base64/dataURL 过长则省略，避免响应膨胀）
        if src and len(src) <= 512 and not src.startswith("data:"):
            entry["source"] = src
        if d.get("local"):
            # 已在 ComfyUI input 目录内：无需上传副本，原地引用
            if response_format == "b64_json":
                try:
                    raw = Path(src).read_bytes()
                except OSError as exc:
                    raise APIError(
                        f"Cannot read local reference `{src}`: {exc}", param="reference_images"
                    ) from exc
                entry["b64_json"] = base64.b64encode(raw).decode("ascii")
            elif response_format in ("file", "path"):
                entry["path"] = src
            else:  # url
                entry["url"] = _native_view_url(
                    {"filename": d["filename"], "subfolder": d["subfolder"], "type": "input"}
                )
        else:
            if response_format == "b64_json":
                entry["b64_json"] = base64.b64encode(d["b64"]).decode("ascii")
            elif response_format in ("file", "path"):
                p = _input_abs_path(d["filename"], d["subfolder"])
                entry["path"] = p or _native_view_url(
                    {"filename": d["filename"], "subfolder": d["subfolder"], "type": "input"}
                )
            else:  # url
                entry["url"] = _native_view_url(
                    {"filename": d["filename"], "subfolder": d["subfolder"], "type": "input"}
                )
        out.append(entry)
    return out


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
