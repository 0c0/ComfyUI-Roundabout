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
    resolve_attention,
    resolve_seed,
    resolve_video_size,
    resolve_output_scale,
    sniff_image,
    split_negative_prompt,
)
from .registry import (
    CAP_IMG2IMG,
    ModelSpec,
    REF_VIDEO_AUDIO_KEY_FMT,
    build_workflow,
    ref_key,
    registry,
)
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
    # 默认回 url：本机/局域网用的是这个网关的主场，而 b64 会把整张图（512² 就有 ~700KB）
    # 灌进调用方上下文，除真正需要内联字节的客户端外没有收益。
    response_format = (req.response_format or "url").lower()

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
    ref_imgs = list(req.reference_images or [])
    img2img = bool(image_inputs) or bool(req.image) or bool(ref_imgs)
    log.info(
        "req=%s | IMAGE gen | model=%s n=%d size=%s fmt=%s img2img=%s refs=%d mode=%s prompt=%s",
        request_id, spec.name, n, req.size or "-",
        response_format, img2img, len(ref_imgs), req.mode or "-", _trunc(prompt_in),
    )

    # ---- 1. 收集图生图输入 ----
    images = list(image_inputs or [])
    if not images and req.image:
        if isinstance(req.image, list):
            # `image` 的语义是「重绘基图」，只收单张。早期版本对数组只取第一张、其余静默丢弃，
            # 不再容忍 —— 多图参考请走 reference_images（按序接入模型声明的参考槽）。
            raise APIError(
                "`image` accepts a single image. Use `reference_images` for multi-image editing.",
                param="image",
            )
        images = [await load_image_input(str(req.image))]
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
    # 参考图：只有声明了参考槽的模型收，且张数不得超过槽数（超了报 400，不静默截断）
    if ref_imgs:
        n_slots = len((spec.references or {}).get("images") or [])
        if not n_slots:
            alts = [s.name for s in registry.all()
                    if (s.references or {}).get("images") and not s.is_video]
            hint = f" Models with reference slots: {', '.join(alts)}." if alts else ""
            raise APIError(
                f"Model `{spec.name}` does not accept `reference_images`.{hint}",
                param="reference_images",
            )
        if len(ref_imgs) > n_slots:
            raise APIError(
                f"`reference_images` accepts at most {n_slots} image(s) for `{spec.name}` "
                f"(got {len(ref_imgs)}).",
                param="reference_images",
            )
    if (
        CAP_IMG2IMG in spec.capabilities
        and not spec.capabilities - {CAP_IMG2IMG}
        and not images
        and not ref_imgs
    ):
        raise APIError(f"Model `{spec.name}` requires an input `image`.", param="image")

    # ---- 2. 上传输入图，拿到 LoadImage 能引用的文件名 ----
    uploaded: str | None = None
    uploaded_mask: str | None = None
    if images:
        ext, _ = sniff_image(images[0])
        uploaded = await client.upload_image(images[0], f"{request_id}_src.{ext}")
    if mask_input and spec.binds("mask"):
        ext, _ = sniff_image(mask_input)
        uploaded_mask = await client.upload_image(mask_input, f"{request_id}_mask.{ext}")

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

    # 模型专属 mode 校验（候选值由模型自己在 models.yaml 声明）
    if req.mode is not None and spec.mode_choices and req.mode not in spec.mode_choices:
        raise APIError(
            f"Model `{spec.name}`: invalid `mode` {req.mode!r}. "
            f"Allowed: {', '.join(spec.mode_choices)}.",
            param="mode",
        )

    values = apply_presets(values, spec, req.style)
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

    # 尺寸来源开关（Qwen 2.1 合并档）：有参考素材 ⇒ latent 跟随它（聚合节点自己出的 latent）；
    # 一张都没有 ⇒ 走 width/height 直传的空 latent，这才是纯文生那一支。值由参考素材有无推导，
    # 不是请求参数（模型只在 bindings 里声明落点，调用方无需也无法直传）。
    if spec.binds("use_custom_size"):
        values["use_custom_size"] = not (ref_imgs or images)

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
        rendered, _ = await _run_once(spec, values, req.workflow_overrides, client, timeout, request_id, req_refs=req)
    else:
        if spec.supports_batch:
            values["batch_size"] = 1
        for i in range(n):
            values["seed"] = resolve_seed(req.seed, i)
            used_seeds.append(values["seed"])
            rendered_part, _ = await _run_once(spec, values, req.workflow_overrides, client, timeout, f"{request_id}-{i}", req_refs=req)
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


def build_video_values(
    req: VideoGenerationRequest,
    spec: ModelSpec,
    prompt_in: str | None,
    neg_in: str | None,
    uploaded: str | None = None,
) -> dict[str, Any]:
    """组装视频请求的语义参数：白名单 → 预设（style 命名档）→ 显式入参 → 模型默认。

    优先级：显式入参 > 预设档 > 模型 defaults（defaults 再叠加显存档位，见 registry._build）。

    单独抽成纯函数（无 IO），是为了让回归测试能**直接调用真实组装路径**——在测试里复刻一份
    顺序曾经导致过假绿（分档参数那次）。
    """
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
    values = apply_presets(values, spec, req.style)
    for key in ("steps", "cfg", "sampler_name", "scheduler", "denoise", "duration", "fps", "num_frames", "scale"):
        explicit = getattr(req, key, None)
        if explicit is not None:
            values[key] = explicit
        elif values.get(key) is None:
            values[key] = spec.defaults.get(key)
    if values.get("width") is None:
        values["width"] = spec.defaults.get("width")
    if values.get("height") is None:
        values["height"] = spec.defaults.get("height")
    # 注意力档位：对外 `attention`（sparse|dense）→ 内部 `sparse_start_percent`（数值）。
    # 不传 = 完全不注入，保持工作流模板默认（模板里就是 0.2 = 稀疏档）。
    # 传了但该模型没有这一档 ⇒ 显式拒绝，别让参数静默无效。
    # ⛔ FastH3 两支故意不给档位：它的 `vsa` 稀疏与蒸馏权重配对训练，关掉不是「更高画质」
    #    而是脱离训练分布。该档只有一个诉求（快），恒定稀疏就是它的最优形态。
    if getattr(req, "attention", None):
        if "sparse_start_percent" not in spec.bindings:
            if spec.is_video:
                raise APIError(
                    f"Model `{spec.name}` always runs sparse attention: its weights are "
                    "trained for that sparse pattern, so there is no dense counterpart to "
                    "switch to. `attention` applies to the non-distilled H3 video models "
                    "(minimax-h3, minimax-h3-edit, minimax-h3-lift, minimax-h3-lift-edit).",
                    param="attention",
                )
            raise APIError(
                f"Model `{spec.name}` has no block-sparse attention stage, so `attention` "
                "has nothing to switch. It is only supported by the H3 video models.",
                param="attention",
            )
        values["sparse_start_percent"] = resolve_attention(req.attention)
    # Lift 放大倍率：仅 lift 两支有 `scale` 绑定；其它模型显式拒绝，别静默忽略。
    out_size = getattr(req, "output_size", None)
    req_scale = getattr(req, "scale", None)
    if (out_size is not None or req_scale is not None) and "scale" not in spec.bindings:
        param = "output_size" if out_size is not None else "scale"
        raise APIError(
            f"Model `{spec.name}` has no latent-lift stage: its output size IS `size` "
            f"({values.get('width')}x{values.get('height')}). `output_size`/`scale` only "
            "apply to minimax-h3-lift / minimax-h3-lift-edit.",
            param=param,
        )
    if out_size is not None and req_scale is not None:
        raise APIError(
            "Pass either `output_size` or `scale`, not both (`output_size` is translated "
            "into a scale factor; `scale` is the direct entry).",
            param="output_size",
        )
    if out_size is not None:
        values["scale"] = resolve_output_scale(out_size, values["width"], values["height"], spec)
    return values


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
    n_frames = sum(1 for p in ("first_frame", "last_frame") if getattr(req, p, None))
    n_refs = len(req.reference_images or [])
    log.info(
        "req=%s | VIDEO gen | model=%s n=%d size=%s fmt=%s img2img=%s frames=%d refs=%d duration=%s fps=%s num_frames=%s prompt=%s",
        request_id, spec.name, n, req.size or "-", response_format, img2img,
        n_frames, n_refs,
        req.duration or "-", req.fps or "-", req.num_frames or "-", _trunc(req.prompt),
    )

    # ---- 1. 收集图生视频输入 ----
    images = list(image_inputs or [])
    if not images and req.image:
        raw_list = req.image if isinstance(req.image, list) else [req.image]
        images = [await load_image_input(str(x)) for x in raw_list if x]
    if images and not spec.supports_img2img:
        # 有些视频模型不收 `image`，而是通过专属字段接图：fl2va 走 first_frame /
        # last_frame（首尾帧），ref2va（edit）走 reference_images（参考图）。此时
        # capabilities 里可能写着 image-to-video，直接说「不支持」会自相矛盾，点明该走哪个字段。
        ref_cfg = spec.references or {}
        if ref_cfg.get("frame_params"):
            hint = " Pass it via `first_frame` (and optionally `last_frame`)."
        elif ref_cfg.get("images"):
            hint = f" Pass image(s) via `reference_images` instead (up to {len(ref_cfg['images'])})."
        else:
            hint = ""
        raise APIError(
            f"Video model `{spec.name}` does not take the `image` field "
            f"(capabilities: {', '.join(sorted(spec.capabilities))}).{hint}",
            param="image",
        )

    # ---- 1.5 首尾帧 / 参考图 / 参考视频音频的槽位校验 ----
    # 统一节点拓扑下帧与参考图并存：frame_params 声明的槽走首尾帧（cover 裁剪、
    # 成为输出的第一/最后一帧），images 剩余槽走 reference_images（conditioning）。
    # 校验只按「槽位存在性」判：请求字段没有对应槽就 400 + 指路，不静默丢弃。
    ref_cfg = spec.references or {}
    frame_params = ref_cfg.get("frame_params") or []
    img_slots = ref_cfg.get("images") or []
    ref_slots = max(len(img_slots) - len(frame_params), 0)
    for p in ("first_frame", "last_frame"):
        if getattr(req, p, None) and p not in frame_params:
            if img_slots:
                hint = (f" `{spec.name}` has no frame slots wired: images go via "
                        f"`reference_images` (up to {ref_slots or len(img_slots)}).")
            else:
                hint = " Models with first/last frame slots: minimax-h3, minimax-h3-lift, fasth3."
            raise APIError(f"Model `{spec.name}` does not take `{p}`.{hint}", param=p)
    if req.reference_images and not ref_slots:
        if frame_params:
            raise APIError(
                f"Model `{spec.name}` takes images only via `first_frame`/`last_frame` "
                f"(frames become actual output frames; reference slots are not wired).",
                param="reference_images",
            )
        raise APIError(
            f"Model `{spec.name}` declares no image reference slots.",
            param="reference_images",
        )
    if req.reference_images and len(req.reference_images) > ref_slots:
        # 超槽数的参考图不再被 wire 循环静默截断。
        raise APIError(
            f"`reference_images` accepts at most {ref_slots} image(s) for `{spec.name}` "
            f"(got {len(req.reference_images)}).",
            param="reference_images",
        )
    for field, cat in (("reference_videos", "videos"), ("reference_audios", "audios")):
        if getattr(req, field, None) and not (ref_cfg.get(cat) or []):
            raise APIError(
                f"Model `{spec.name}` declares no {cat} reference slots.",
                param=field,
            )

    # ---- 2. 上传输入图 ----
    uploaded: str | None = None
    if images:
        ext, _ = sniff_image(images[0])
        uploaded = await client.upload_image(images[0], f"{request_id}_src.{ext}")

    # ---- 3. 组装语义参数 ----
    values = build_video_values(req, spec, prompt_in, neg_in, uploaded)

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
        rendered_part, ref_descriptors = await _run_once(spec, values, req.workflow_overrides, client, timeout, request_id, req_refs=req, on_submit=on_submit)
        rendered += rendered_part
        all_refs += ref_descriptors
    else:
        if spec.supports_batch:
            values["batch_size"] = 1
        for i in range(n):
            values["seed"] = resolve_seed(req.seed, i)
            used_seeds.append(values["seed"])
            rendered_part, ref_descriptors = await _run_once(spec, values, req.workflow_overrides, client, timeout, f"{request_id}-{i}", req_refs=req, on_submit=on_submit)
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
    # 实际输出尺寸回显：lift 档 = latent(画布/16) × scale 取整后再 ×16；其余 = 画布本身。
    eff_scale = values.get("scale") or 1.0
    out_w = int(round(values["width"] // 16 * eff_scale)) * 16
    out_h = int(round(values["height"] // 16 * eff_scale)) * 16
    return VideoResponse(created=int(time.time()), data=data, seed=seed_field, references=references,
                         size=f"{out_w}x{out_h}")  # type: ignore[arg-type]


async def _run_once(
    spec: ModelSpec,
    values: dict[str, Any],
    overrides: dict[str, Any] | None,
    client: ComfyClient,
    timeout: float,
    trace: str,
    req_refs: ImageGenerationRequest | VideoGenerationRequest | None = None,
    on_submit: Any | None = None,
) -> tuple[list[RenderedImage], list[dict[str, Any]]]:
    workflow = build_workflow(spec, values, overrides)
    ref_descriptors: list[dict[str, Any]] = []
    # 参考资源（图像档的多图编辑与视频档一样走这套）：先接入实际提供的参考素材，
    # 再删除未上传的槽，最后提交给 ComfyUI
    if spec.references and req_refs is not None:
        ref_descriptors = await _wire_references(workflow, spec, req_refs, client, trace)
        dropped = _prune_unused_references(workflow, spec, req_refs)
        if dropped:
            log.info("req=%s pruned %d unused reference node(s) before submit", trace, dropped)
    async with semaphore():
        prompt_id = await client.submit(workflow, client_id=f"roundabout-{trace}")
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


def _ref_images(spec: ModelSpec, req: ImageGenerationRequest | VideoGenerationRequest) -> list[str]:
    """本次请求实际提供的参考图，含 `image` 的单图回落。

    模型没有 `image` 绑定时（参考槽是唯一入口，如 Qwen 2.1 合并档 —— 它的第 1 槽不能由
    binding 供图，否则槽被「受保护」而删不掉，纯文生那一支会把模板占位图当参考喂进去），
    `image` 在这里充当第 1 张参考图。否则它会被上传却无处注入 —— 静默丢弃。
    有 `image` 绑定的模型（klein / boogu）不受影响：它们的 `image` 由 bindings 落在自己
    声明的落点上。

    接线与剪枝必须用同一份判断，故共用本函数。
    """
    imgs = list(getattr(req, "reference_images", None) or [])
    if not imgs and not spec.is_video and getattr(req, "image", None) and not spec.binds("image"):
        imgs = [req.image]
    return imgs


def _slot_values(spec: ModelSpec, req: ImageGenerationRequest | VideoGenerationRequest) -> list[str | None]:
    """按槽序返回每个 image 槽的图源（None = 该槽未提供）。

    两类拓扑：
      - **frame_params 前缀**（统一节点 / fl2va）：前 len(fp) 个槽取请求字段
        `frame_params[i]`（first_frame / last_frame），语义是「帧」—— 实际成为输出的
        第一/最后一帧；其余槽回落 reference_images，语义是「参考」。
      - **纯参考槽**（无 frame_params）：全部槽取 reference_images[i]，
        `image` 的单图回落仍只在图像档生效（见 `_ref_images`）。

    接线与剪枝必须用同一份判断，故共用本函数。
    """
    ref = spec.references or {}
    fp = ref.get("frame_params") or []
    if fp:
        n = len(ref.get("images") or [])
        imgs = _ref_images(spec, req)
        return [
            (getattr(req, fp[i], None) if i < len(fp)
             else (imgs[i - len(fp)] if i - len(fp) < len(imgs) else None))
            for i in range(n)
        ]
    imgs = _ref_images(spec, req)
    n = len(ref.get("images") or [])
    return [imgs[i] if i < len(imgs) else None for i in range(n)]


def _prune_unused_references(
    wf: dict[str, Any],
    spec: ModelSpec,
    req: ImageGenerationRequest | VideoGenerationRequest,
) -> int:
    """按请求实际提供的参考资源数量，删除工作流 JSON 副本中多余的参考槽。

    仅做删除（用户明确要求）：接口未上传的资源，其对应的 LoadImage / LoadVideo / LoadAudio
    节点及 aggregator 上的输入键一并移除；已提供资源的节点保留（接入由调用方负责）。
    返回被删除的节点数，便于日志。

    级联：删除某个 loader 节点（如 LoadVideo）时，会连带删除依赖它的下游节点
    （如 GetVideoComponents），但**绝不删除聚合节点**（aggregator）。

    两种参考拓扑都支持：
      - **聚合式**（H3 系、Qwen autogrow）：参考都挂在同一个聚合节点上，删槽 = 删 loader
        节点 + 删 aggregator 上对应的输入键；
      - **链式**（Flux2/Klein 的 ReferenceLatent 链）：参考经各自独立链路接线，没有聚合
        节点，删槽 = 删 loader 节点 + `slots[i].nodes` 声明的独占下游 + 清空
        `slots[i].clear` 声明的 optional 输入键（latent 留空即直通，链不用重接）。
    """
    ref = spec.references
    if not ref:
        return 0
    agg = str(ref.get("aggregator") or "")
    agg_node = wf.get(agg) if agg else None
    # 无聚合节点时留空 dict：下面 _drop 对它的 pop 是无操作
    agg_ins: dict[str, Any] = agg_node.setdefault("inputs", {}) if isinstance(agg_node, dict) else {}

    # binding 的落点节点受保护，不参与剪枝。
    # 典型情形：Klein 的向后兼容单图入口 `image: 76.inputs.image`，而 76 同时是 images 的
    # 第 0 槽 loader —— 调用方只传 `image`、不传 `reference_images` 时，该槽看似"未提供"，
    # 实际正是 image 的落点，删掉工作流就悬空了。由 binding 供图的 loader 是调用方可控的
    # 输入面（未传时回落模板默认值，即引入多槽之前的行为），不该当垃圾槽清掉。
    protected: set[str] = {
        str(path).partition(".")[0]
        for paths in (spec.bindings or {}).values()
        for path in paths
    }

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

    def _clear(path: str) -> None:
        """清空 `节点id.输入键`（按第一个点切分 —— 节点 id 不含点，输入键可能含点，
        如 `474.images.image_1`）。仅从 inputs 里摘掉键，不碰节点自身。"""
        nid, _, key = str(path).partition(".")
        node = wf.get(nid)
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
            node["inputs"].pop(key, None)

    vals = _slot_values(spec, req)
    slots = ref.get("slots") or []
    for i, nid in enumerate(ref.get("images", [])):
        if str(nid) in protected:
            continue  # 该槽由 binding 直接供图（单图入口），保留整条链路
        val = vals[i] if i < len(vals) else None
        if not val:
            _drop(ref_key(ref, "images", i), str(nid))
            if i < len(slots):
                slot = slots[i] or {}
                for extra in slot.get("nodes") or []:
                    if wf.pop(str(extra), None) is not None:
                        removed += 1
                for path in slot.get("clear") or []:
                    _clear(path)

    vids = getattr(req, "reference_videos", None) or []
    for i, nid in enumerate(ref.get("videos", [])):
        if str(nid) in protected:
            continue
        if i >= len(vids):
            # 参考视频与其音轨（ref_video_audio_N）同源、同节点，一并移除
            _drop(ref_key(ref, "videos", i), str(nid))
            _drop(REF_VIDEO_AUDIO_KEY_FMT.format(i), str(nid))

    auds = getattr(req, "reference_audios", None) or []
    for i, nid in enumerate(ref.get("audios", [])):
        if str(nid) in protected:
            continue
        if i >= len(auds):
            _drop(ref_key(ref, "audios", i), str(nid))

    return removed


async def _wire_references(
    wf: dict[str, Any],
    spec: ModelSpec,
    req: ImageGenerationRequest | VideoGenerationRequest,
    client: ComfyClient,
    trace: str,
) -> list[dict[str, Any]]:
    """把请求中实际提供的参考素材接到对应 loader 节点，并返回 image 类描述符。

    图像档（多图编辑）与视频档共用：图像请求只有 `reference_images`，
    video/audio 两类用 getattr 兜底。

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

    # ---- 参考图 / 首尾帧：LoadImage.inputs.image ----
    # frame_params 模型（fl2va）逐槽取请求字段，报错参数名也归属到对应字段；
    # 其余模型取 reference_images 列表。
    vals = _slot_values(spec, req)
    fp = (spec.references or {}).get("frame_params") or []
    for i, nid in enumerate(ref.get("images", [])):
        val = vals[i] if i < len(vals) else None
        if not val:
            continue  # 未提供的槽由剪枝删除；混合拓扑下帧槽与参考槽各自前缀填充
        param_name = fp[i] if i < len(fp) else "reference_images"
        node_value, desc = await _stage_ref(str(val), "image", param_name, client, trace, i)
        node = wf.get(str(nid))
        if isinstance(node, dict):
            node.setdefault("inputs", {})[widget["image"]] = node_value
        if desc:
            descriptors.append(desc)

    # ---- 参考视频：LoadVideo.inputs.file ----
    vids = getattr(req, "reference_videos", None) or []
    for i, nid in enumerate(ref.get("videos", [])):
        if i >= len(vids):
            break
        node_value, _ = await _stage_ref(str(vids[i]), "video", "reference_videos", client, trace, i)
        node = wf.get(str(nid))
        if isinstance(node, dict):
            node.setdefault("inputs", {})[widget["video"]] = node_value

    # ---- 参考音频：LoadAudio.inputs.audio ----
    auds = getattr(req, "reference_audios", None) or []
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
        data, f"{trace}_ref{tag}_{idx}.{ext}", content_type=mime_for_ext(ext)
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
