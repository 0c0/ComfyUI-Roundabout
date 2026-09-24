"""调用结构自描述：给前端 / agent 用的「完整调用面」单一事实来源。

为什么需要它：MCP 工具描述与 REST 文档都是**手写的自然语言**，模型一多就必然
失同步（历史上 MCP 描述里就残留过「编辑模型可以不传 image」这类错口径）。
本模块不写散文，只从**运行期真实状态**推导结构：

- 模型清单 / 能力 / 别名 / 生效默认值  ← `registry.all()`（已含 yaml + 显存分档合并结果）
- 每个请求字段的类型 / 区间 / 枚举 / 默认值 / 本模型是否生效 ← `schemas.py` 的 pydantic 字段
- 尺寸档位 / 种子上限 / 注意力档位          ← `params.py` 的运行时表
- 张数上限 / 输入体积上限 / 负向拆分行      ← `config.settings`

因此工具描述可以只留一句话，细节全部推给本接口。

**不依赖 aiohttp / web**：REST 端（admin）与 MCP 端（mcp_server，无 web 栈）共用同一份。
"""

from __future__ import annotations

from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .config import settings
from .params import (
    ATTENTION_SPARSE_START,
    SAFE_SEED,
    VIDEO_RES_PRESETS,
    _RES_RATIOS,
    _RES_TIERS,
)
from .registry import registry
from .schemas import ImageGenerationRequest, VideoGenerationRequest

_API_VERSION = "roundabout-tool-info-1"

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


# ---------------------------------------------------------------- 字段元数据
# pydantic v2 把区间约束放在 FieldInfo.metadata 的约束对象上（属性名与字段名不同）
_NUM_BOUNDS = (("ge", "min"), ("gt", "min_exclusive"), ("le", "max"), ("lt", "max_exclusive"))


def _base_type(annotation: Any) -> Any:
    """剥掉 Optional 包装，回到单一底层类型注解（`str | None` → `str`）。"""
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _base_type(args[0])
    return annotation


def _json_type(annotation: Any) -> Any:
    """注解 → JSON Schema 风格的简写（前端直接照着渲染输入控件）。

    可选性由 `nullable` 单列，故这里只报**底层类型**：`str | None` → `"string"`。
    真正的联合（`str | list[str]`）保留成列表，让前端知道两种形态都收。
    """
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return [_json_type(a) for a in args]
    if origin is Literal:
        # 字面量常量集：类型取首项的底层类型（str 常量 → "string"），候选值走 `choices`
        return _json_type(type(get_args(annotation)[0]))
    if origin in (list, set, tuple):
        inner = get_args(annotation)
        return {"type": "array", "items": _json_type(inner[0]) if inner else "any"}
    if origin is dict:
        return "object"
    return _JSON_TYPES.get(annotation, "any")


def _field_meta(fi: FieldInfo) -> dict[str, Any]:
    """单个 pydantic 字段 → 可渲染的元数据（区间 / 枚举 / 默认值 / 说明）。"""
    annotation = fi.annotation
    base = _base_type(annotation)
    meta: dict[str, Any] = {"type": _json_type(annotation)}
    for constraint in fi.metadata or ():
        for src, key in _NUM_BOUNDS:
            value = getattr(constraint, src, None)
            if value is not None:
                meta[key] = value
    if get_origin(base) is Literal:
        meta["choices"] = list(get_args(base))
    if fi.description:
        meta["description"] = fi.description
    # 必填 = 无默认值且不可为 None（如 video 的 prompt）。前端据此标红必填项。
    meta["required"] = fi.is_required()
    if fi.is_required() or fi.default is None:
        meta["nullable"] = True
    else:
        meta["default"] = fi.default
    return meta


def _walk_request_fields(model: type[BaseModel]) -> list[dict[str, Any]]:
    """请求模型的字段清单（保持声明顺序）。alias（如 `async`）作为单独键回传。"""
    out: list[dict[str, Any]] = []
    for name, fi in model.model_fields.items():
        entry: dict[str, Any] = {"name": name, **_field_meta(fi)}
        if fi.alias and fi.alias != name:
            entry["alias"] = fi.alias
        out.append(entry)
    return out


# ---------------------------------------------------------------- 生效性判定
def _applies(spec, name: str) -> tuple[bool, str | None]:
    """该字段对本模型是否真的生效，附**不生效时的**原因（前端据此置灰输入框）。

    判定一律走 `ModelSpec` 的派生属性（`binds` / `supports_batch` / `is_video` /
    `references`），不重新解析 yaml —— 这些属性本身就是 pipeline 的判据，两处各写一遍
    必然漂移。特别地，`attention` 的落点是 `sparse_start_percent` 绑定（公开枚举 →
    内部数值的翻译在 `params.resolve_attention`），不存在名为 `attention` 的 binding。

    契约：返回 `(True, None)` 或 `(False, 原因)` —— 生效的字段绝不带原因，否则前端
    会显示一个与「可用」自相矛盾的解释。
    """
    if name == "prompt":
        if spec.promptless:
            return False, "promptless tool model (no prompt)"
        return True, None
    if name == "negative_prompt":
        # 没有反向输入落点时网关不报错、只是静默无效，所以必须在这里标出来。
        if spec.binds(name):
            return True, None
        return False, "model declares no negative_prompt binding"
    if name == "mode":
        if spec.mode_choices:
            return True, None
        return False, "model declares no mode_choices"
    if name == "size":
        if spec.is_video:
            return True, None
        # 图像档的 size 只是「模板默认值的兜底」；无 w/h 绑定则完全落不下去。
        if spec.binds("width") or spec.binds("height"):
            return True, None
        return False, "model takes no width/height binding (output size is fixed by the template)"
    if name == "n":
        if spec.supports_batch:
            return True, None
        return False, "model declares no batch_size binding"
    if name == "image":
        if spec.is_video:  # 视频档：参考帧走 image_keys 路径，恒可用
            return True, None
        if spec.binds(name):
            return True, None
        return False, "model declares no image binding"
    if name == "mask":
        if spec.binds(name):
            return True, None
        return False, "model declares no mask binding"
    if name == "attention":
        if not spec.is_video:
            return False, "video-only"
        if spec.binds("sparse_start_percent"):
            return True, None
        return False, "model is always sparse (no attention tier; FastH3 pairs vsa with its distilled weights)"
    if name == "scale":
        # 参数解析器按「模型是否声明 scale 绑定」判定，lift 两支正是唯一声明者。
        if spec.binds(name):
            return True, None
        return False, "lift only (minimax-h3-lift / -lift-edit)"
    if name in ("reference_images", "reference_videos", "reference_audios"):
        cat = name.split("_")[1]
        n = len((spec.references or {}).get(cat) or [])
        if n:
            return True, None
        return False, f"model declares no {cat} reference slots"
    if name in ("fps", "num_frames", "duration", "background", "async_mode"):
        if spec.is_video:
            return True, None
        return False, "video-only"
    if spec.binds(name):
        return True, None
    return False, "model declares no binding for this parameter"


# ---------------------------------------------------------------- 模型块
def _slot_counts(spec) -> dict[str, int]:
    ref = spec.references or {}
    return {cat: len(ref.get(cat) or []) for cat in ("images", "videos", "audios")}


def _model_entry(spec) -> dict[str, Any]:
    is_video = spec.is_video
    fields = _walk_request_fields(VideoGenerationRequest if is_video else ImageGenerationRequest)
    for field in fields:
        applies, reason = _applies(spec, field["name"])
        field["applies"] = applies
        if reason:
            field["inactive_reason"] = reason
    entry: dict[str, Any] = {
        "name": spec.name,
        "mode": spec.mode,
        "description": spec.description,
        "capabilities": sorted(spec.capabilities),
        "img2img": spec.supports_img2img,
        "batch": spec.supports_batch,
        "promptless": spec.promptless,
        "aliases": list(spec.aliases or []),
        "defaults": dict(spec.defaults or {}),
        "bindings": sorted(spec.bindings),
        "workflow": spec.workflow_path.name,
        "reference_slots": _slot_counts(spec),
        "timeout": spec.timeout,
        "fields": fields,
    }
    if spec.size_choices:
        entry["size_choices"] = list(spec.size_choices)
    if spec.mode_choices:
        entry["mode_choices"] = list(spec.mode_choices)
    if spec.vram_adaptive:
        entry["vram_tier"] = dict(spec.vram_tier or {})
    return entry


# ---------------------------------------------------------------- 全局块
def _endpoints() -> dict[str, list[str]]:
    """两套入口的固定路径。与路由表 / 工具清单一一对应，改路由时同步这里。"""
    return {
        "rest": [
            "POST /v1/images/generations",
            "POST /v1/images/edits",
            "POST /v1/images/remove-background",
            "POST /v1/videos/generations",
            "GET /v1/models",
            "GET /v1/images/tasks/{id}",
            "GET /v1/videos/tasks/{id}",
            "DELETE /v1/images/tasks/{id}",
            "GET /roundabout/admin/tool-info",
            "GET /roundabout/view",
            "GET /roundabout/view/board",
            "POST /roundabout/view/board/items",
            "DELETE /roundabout/view/board/items/{id}",
            "DELETE /roundabout/view/board",
            "GET /roundabout/view/board/history",
            "GET /roundabout/view/board/history/{id}",
            "DELETE /roundabout/view/board/history/{id}",
            "POST /roundabout/view/board/history/{id}/load",
            "POST /roundabout/view/reveal",
        ],
        "mcp": [
            "list_models", "generate_image", "edit_image", "remove_background",
            "generate_video", "get_task", "cancel_task", "queue_status",
            "get_workflow", "reload", "health", "get_view_url", "get_skills",
            "check_weights", "get_tool_info",
            "pin_view_item", "clear_view_board", "get_view_board_history",
            "load_view_board",
        ],
    }


def _surface() -> dict[str, Any]:
    return {
        "default_model": registry.default_model,
        "models": registry.names(),
        "max_n": settings.max_n,
        "auto_split_negative": settings.auto_split_negative,
        "max_input_image_mb": settings.max_input_image_mb,
        "max_input_asset_mb": settings.max_input_asset_mb,
        "auth_required": settings.auth_enabled,
        "seed": {
            "min": 0,
            "max": SAFE_SEED,
            "note": "不传或 -1 随机；超过上限报 400",
        },
        "image_inputs": "dataURL / 裸 base64 / http(s) URL / 本地绝对路径 / 相对 ComfyUI input 目录的路径",
        "video": {
            # duration 的 1–15 是网关侧硬闸（pipeline 里 400），不体现在 pydantic 约束上，
            # 所以必须在这里声明，否则只看字段清单的调用者无从得知。
            "duration_seconds": {"min": 1, "max": 15},
            "note": "H3 系权重训练区间是 124–362 帧 ≈ 5–15s，d≤4 落在分布外（能跑，但别据此下画质结论）",
        },
        "video_sizes": {
            "tiers": sorted(_RES_TIERS),
            "ratios": sorted(_RES_RATIOS),
            "keys": ["<tier>p-<ratio>", "<ratio>@<tier>p"],
            "width_x_height": "直接写 WxH（如 1280x720）",
            "presets": {
                str(tier): {ratio: list(dims) for ratio, dims in table.items()}
                for tier, table in sorted(VIDEO_RES_PRESETS.items())
            },
        },
        "attention": {
            "choices": {k: v for k, v in ATTENTION_SPARSE_START.items()},
            "note": "sparse 更快更省显存，dense 换回致密画质；未声明 binding 的模型传了报 400",
        },
        "response_format": ["b64_json", "url", "file", "path"],
    }


def build() -> dict[str, Any]:
    """完整调用结构。纯读运行期状态，无副作用、无网络、可高频调用。"""
    return {
        "object": "roundabout.tool_info",
        "api_version": _API_VERSION,
        "counts": {"models": len(registry.all())},
        "endpoints": _endpoints(),
        "surface": _surface(),
        "field_order": {
            "image": list(ImageGenerationRequest.model_fields),
            "video": list(VideoGenerationRequest.model_fields),
        },
        "models": [_model_entry(s) for s in registry.all()],
    }


# ---------------------------------------------------------------- 返回给 agent 的裁剪视图
def _compact_field(field: dict[str, Any]) -> dict[str, Any]:
    """字段元数据 → 裁剪版：去掉长 description 与不可用字段的 default/choices。

    `applies` 与 `inactive_reason` **必须保留** —— 「这个字段对该模型生不生效」正是
    本视图存在的理由；早期的写法在这里把 `applies` 丢了，等于返回了一份没有答案的清单。
    """
    keep = ("name", "type", "required", "choices", "min", "max", "default",
            "applies", "inactive_reason")
    out = {k: field[k] for k in keep if k in field}
    if not out.get("applies"):
        out.pop("default", None)
        out.pop("choices", None)
    return out


def compact(model: str | None = None, include_fields: bool = True) -> dict[str, Any]:
    """给对话式调用者的裁剪版：去掉长 description 与全局大表，只留选型必需信息。

    与 `build()` 同源：模型块直接复用 `_model_entry`（因此带 `applies`），只在字段层
    做裁剪。MCP 返回给 agent 的是这一份；前端要渲染完整表单时调 REST 的
    `/roundabout/admin/tool-info`（完整版）。
    """
    specs = [registry.resolve(model)] if model else registry.all()
    models: list[dict[str, Any]] = []
    for spec in specs:
        entry = _model_entry(spec)
        entry.pop("workflow", None)
        entry.pop("bindings", None)
        if not include_fields:
            entry.pop("fields", None)
        else:
            entry["fields"] = [_compact_field(f) for f in entry["fields"]]
        models.append(entry)
    return {
        "object": "roundabout.tool_info.compact",
        "api_version": _API_VERSION,
        "default_model": registry.default_model,
        "surface": {
            "max_n": settings.max_n,
            "auto_split_negative": settings.auto_split_negative,
            "seed_max": SAFE_SEED,
            "duration_seconds": {"min": 1, "max": 15},
            "video_sizes": f"<tier>p-<ratio> / <ratio>@<tier>p，tier∈{sorted(_RES_TIERS)}，"
                           f"ratio∈{sorted(_RES_RATIOS)}，或直接写 WxH",
        },
        "models": models,
        "full": "完整结构（含字段说明 / 尺寸预设表 / 绑定清单）见 REST GET /roundabout/admin/tool-info",
    }
