"""模型注册表 + 工作流注入引擎。

核心思路：workflow 模板保持「从 ComfyUI 导出的原始 API JSON」不做任何改造，
所有可变点通过 models.yaml 里的 bindings 用 JSON 路径（如 `3.inputs.seed`）声明。
这样换工作流只需要改 YAML，不用动代码。
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import APIError, ModelNotFound
from . import vram

log = logging.getLogger("roundabout.registry")

# 所有可注入的语义参数
KNOWN_PARAMS = {
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "seed",
    "steps",
    "cfg",
    "sampler_name",
    "scheduler",
    "denoise",
    "batch_size",
    "image",
    "mask",
    "filename_prefix",
    # ---- 模型专属动态参数 ----
    "mode",
    # ---- 视频专属 ----
    "duration",
    "fps",
    "num_frames",
    # SelfLift 渐进采样：总步数复用 steps，这里是「低分辨率 → 高分辨率」的过渡步
    "transition_step",
    # ---- 低显存分块（按显卡档位自动填默认值，见 gateway/vram.py）----
    "chunks",
    "head_chunks",
    "seq_threshold",
    "highres_tiling",
}

CAP_TXT2IMG = "text-to-image"
CAP_IMG2IMG = "image-to-image"


@dataclass
class ModelSpec:
    name: str
    workflow_path: Path
    template: dict[str, Any]
    bindings: dict[str, list[str]] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    capabilities: set[str] = field(default_factory=lambda: {CAP_TXT2IMG})
    output_node: str | None = None
    description: str = ""
    mode: str = "image"  # "image" | "video"
    quality_presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    style_presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 命名运动档（如 story=文戏 / fight=打戏）：请求传一个词，展开成多组参数注入节点。
    # 适合「总步数 + 过渡步」这类必须成对调整、单独调容易配错的参数。未声明的模型传了报错。
    motion_presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    size_choices: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    timeout: float | None = None
    mode_choices: list[str] = field(default_factory=list)  # 如 Ideogram4 的 [Quality, Default, Turbo]
    # 视频参考资源拓扑（动态删除未上传节点用）：aggregator + 各资源类别的 load 节点 id 列表
    references: dict[str, Any] | None = None
    # 工具类工作流（去背景等）没有提示词概念：声明后可不绑 prompt，
    # 且 pipeline 不会再要求请求带 prompt。
    promptless: bool = False
    # 按本机显存档位自动填默认值（大模型分块：chunks / head_chunks / seq_threshold /
    # highres_tiling）。档位表来自 `defaults.vram_tiers`，本机命中哪一档记在 vram_tier。
    vram_adaptive: bool = False
    vram_tier: dict[str, Any] = field(default_factory=dict)

    @property
    def supports_batch(self) -> bool:
        return "batch_size" in self.bindings

    @property
    def supports_img2img(self) -> bool:
        return CAP_IMG2IMG in self.capabilities and "image" in self.bindings

    @property
    def is_video(self) -> bool:
        return self.mode == "video"

    def binds(self, key: str) -> bool:
        return key in self.bindings


# ---------------------------------------------------------------- JSON 路径读写
def _split(path: str) -> list[str]:
    return [seg for seg in path.split(".") if seg != ""]


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for seg in _split(path):
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        else:
            return None
    return cur


def set_path(obj: Any, path: str, value: Any) -> bool:
    segs = _split(path)
    if not segs:
        return False
    cur = obj
    for seg in segs[:-1]:
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return False
        elif isinstance(cur, dict):
            if seg not in cur or not isinstance(cur[seg], (dict, list)):
                return False
            cur = cur[seg]
        else:
            return False
    last = segs[-1]
    if isinstance(cur, list):
        try:
            cur[int(last)] = value
            return True
        except (ValueError, IndexError):
            return False
    if isinstance(cur, dict):
        cur[last] = value
        return True
    return False


# ---------------------------------------------------------------- 注册表加载
class Registry:
    def __init__(self) -> None:
        self._models: dict[str, ModelSpec] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self.default_model: str | None = None
        self._models_file: Path | None = None

    # -- 查询 --
    def names(self) -> list[str]:
        return list(self._order)

    def all(self) -> list[ModelSpec]:
        return [self._models[n] for n in self._order]

    def resolve(self, name: str | None) -> ModelSpec:
        with self._lock:
            key = (name or self.default_model or "").strip()
            if not key:
                raise APIError("`model` is required and no DEFAULT_MODEL is configured.", param="model")
            spec = self._models.get(key) or self._models.get(key.lower())
            if spec is None:
                raise ModelNotFound(key, self._order)
            return spec

    # -- 加载 --
    def _build(
        self,
        raw: Any,
        models_file: Path,
        workflows_dir: Path,
        default_model: str,
    ) -> dict[str, Any]:
        """解析并校验一份 models.yaml 的内容，返回待生效的状态 dict。

        不修改任何实例状态；校验失败直接抛异常（供 load 与 validate_content 共用）。
        """
        if not isinstance(raw, dict):
            raise RuntimeError("models.yaml top-level must be a mapping")
        shared_defaults: dict[str, Any] = raw.get("defaults") or {}
        # 低显存分块档位表（按本机显存挑一档，见 gateway/vram.py）
        vram_tiers = vram.normalize_tiers(shared_defaults.get("vram_tiers"))
        entries: dict[str, Any] = raw.get("models") or {}
        if not entries:
            raise RuntimeError("no models defined in config")

        models: dict[str, ModelSpec] = {}
        order: list[str] = []

        for name, cfg in entries.items():
            cfg = cfg or {}
            wf_name = cfg.get("workflow")
            if not wf_name:
                raise RuntimeError(f"model `{name}`: missing `workflow`")
            wf_path = Path(wf_name)
            if not wf_path.is_absolute():
                wf_path = workflows_dir / wf_name
            if not wf_path.exists():
                raise RuntimeError(f"model `{name}`: workflow file not found: {wf_path}")

            try:
                template = json.loads(wf_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"model `{name}`: workflow is not valid JSON ({wf_path}): {exc}") from exc

            # ComfyUI 完整工作流（带 nodes/links）不能直接提交，必须是 API 格式
            if "nodes" in template and "last_node_id" in template:
                raise RuntimeError(
                    f"model `{name}`: {wf_path.name} looks like a UI workflow. "
                    "Export it with 'Workflow -> Export (API)' instead."
                )

            bindings = _normalize_bindings(name, cfg.get("bindings") or {})
            # 显存分档：档位值作为默认值的地基，模型自己写的 defaults 覆盖它。
            # 探测不到显存时 tier 为空，行为等同不做自适应。
            tier: dict[str, Any] = {}
            if cfg.get("vram_adaptive"):
                tier = vram.select_tier(vram.total_vram_gb(), vram_tiers)
                if not tier:
                    log.warning(
                        "model `%s`: vram_adaptive 已开启，但本机显存探测不到 / 档位表为空，"
                        "沿用工作流自带的参数",
                        name,
                    )
                else:
                    log.info(
                        "model `%s`: 显存 %.1f GiB -> 分块档位 %s",
                        name, vram.total_vram_gb() or 0.0,
                        {k: v for k, v in tier.items() if k in KNOWN_PARAMS},
                    )
            merged_defaults = {
                **(shared_defaults.get("params") or {}),
                **tier,
                **(cfg.get("defaults") or {}),
            }

            caps = set(cfg.get("capabilities") or [CAP_TXT2IMG])
            if "image" in bindings:
                caps.add(CAP_IMG2IMG)

            mode = str(cfg.get("mode") or "image").lower()
            if mode not in {"image", "video"}:
                raise RuntimeError(f"model `{name}`: invalid `mode` {mode!r} (expected image|video)")

            ref = cfg.get("references")
            if ref:
                ref = _normalize_references(ref)

            spec = ModelSpec(
                name=name,
                workflow_path=wf_path,
                template=template,
                bindings=bindings,
                defaults=merged_defaults,
                capabilities=caps,
                output_node=str(cfg["output_node"]) if cfg.get("output_node") is not None else None,
                description=cfg.get("description", ""),
                mode=mode,
                quality_presets=cfg.get("quality_presets") or shared_defaults.get("quality_presets") or {},
                style_presets=cfg.get("style_presets") or shared_defaults.get("style_presets") or {},
                motion_presets=cfg.get("motion_presets") or shared_defaults.get("motion_presets") or {},
                size_choices=[str(s) for s in (cfg.get("sizes") or [])],
                aliases=[str(a) for a in (cfg.get("aliases") or [])],
                timeout=float(cfg["timeout"]) if cfg.get("timeout") else None,
                mode_choices=[str(a) for a in (cfg.get("mode_choices") or [])],
                references=ref,
                promptless=bool(cfg.get("promptless")),
                vram_adaptive=bool(cfg.get("vram_adaptive")),
                vram_tier=tier,
            )
            _validate_bindings(spec)
            _validate_references(spec)

            models[name] = spec
            order.append(name)
            for alias in spec.aliases:
                models[alias] = spec

        return {
            "models": models,
            "order": order,
            "default_model": default_model or raw.get("default_model") or order[0],
        }

    def validate_content(
        self,
        text: str,
        workflows_dir: Path,
        default_model: str = "",
    ) -> bool:
        """仅校验一份 YAML 文本能否被正常加载（不改内存状态、不写磁盘）。"""
        raw = yaml.safe_load(text)
        self._build(raw, self._models_file or Path("models.yaml"), workflows_dir, default_model)
        return True

    def load(
        self,
        models_file: Path,
        workflows_dir: Path,
        default_model: str = "",
    ) -> None:
        if not models_file.exists():
            raise RuntimeError(f"models file not found: {models_file}")
        self._models_file = models_file
        raw = yaml.safe_load(models_file.read_text(encoding="utf-8")) or {}
        state = self._build(raw, models_file, workflows_dir, default_model)

        with self._lock:
            self._models = state["models"]
            self._order = state["order"]
            self.default_model = state["default_model"]

        log.info(
            "loaded %d models: %s (default=%s)",
            len(state["order"]),
            ", ".join(state["order"]),
            self.default_model,
        )


def _normalize_bindings(model: str, raw: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if key not in KNOWN_PARAMS:
            log.warning("model `%s`: unknown binding key `%s` (ignored)", model, key)
            continue
        paths = [value] if isinstance(value, str) else list(value or [])
        out[key] = [str(p) for p in paths]
    return out


def _validate_bindings(spec: ModelSpec) -> None:
    """启动即校验路径是否存在，避免线上才发现 node id 写错。"""
    missing: list[str] = []
    for key, paths in spec.bindings.items():
        for path in paths:
            if get_path(spec.template, path) is None:
                missing.append(f"{key} -> {path}")
    if missing:
        raise RuntimeError(
            f"model `{spec.name}`: binding path(s) not found in {spec.workflow_path.name}: " + "; ".join(missing)
        )
    if "prompt" not in spec.bindings and not spec.promptless:
        raise RuntimeError(f"model `{spec.name}`: a `prompt` binding is required (or set `promptless: true`)")
    if spec.output_node and spec.output_node not in spec.template:
        raise RuntimeError(f"model `{spec.name}`: output_node `{spec.output_node}` not in workflow")


def _normalize_references(raw: dict[str, Any]) -> dict[str, Any]:
    """把 models.yaml 的 references 段规范化为字符串节点 id 列表。"""
    out: dict[str, Any] = {}
    agg = raw.get("aggregator")
    if agg is not None:
        out["aggregator"] = str(agg)
    for cat in ("images", "videos", "audios"):
        out[cat] = [str(x) for x in (raw.get(cat) or [])]
    return out


def _validate_references(spec: ModelSpec) -> None:
    """启动时校验参考资源拓扑：aggregator 与各 load 节点必须存在于工作流中。"""
    ref = spec.references
    if not ref:
        return
    tpl = spec.template
    agg = ref.get("aggregator")
    if agg is None or agg not in tpl:
        raise RuntimeError(
            f"model `{spec.name}`: references.aggregator `{agg}` not found in {spec.workflow_path.name}"
        )
    agg_ins = tpl[agg].get("inputs", {})

    def _need(cat: str, key_fmt: str, nid: str, idx: int) -> None:
        if nid not in tpl:
            raise RuntimeError(
                f"model `{spec.name}`: references.{cat}[{idx}]={nid} not found in {spec.workflow_path.name}"
            )
        key = key_fmt.format(idx)
        if key not in agg_ins:
            raise RuntimeError(f"model `{spec.name}`: aggregator `{agg}` missing input `{key}`")

    for i, nid in enumerate(ref.get("images", [])):
        _need("images", "ref_images.ref_image_{}", nid, i)
    for i, nid in enumerate(ref.get("videos", [])):
        _need("videos", "ref_videos.ref_video_{}", nid, i)
        _need("videos", "ref_video_audios.ref_video_audio_{}", nid, i)
    for i, nid in enumerate(ref.get("audios", [])):
        _need("audios", "ref_audios.ref_audio_{}", nid, i)


# ---------------------------------------------------------------- 注入
def build_workflow(spec: ModelSpec, values: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """把语义参数注入 workflow 模板副本，返回可直接提交给 /prompt 的 JSON。

    `spec.defaults` 是地基，`values` 覆盖它：调用方只传「本次请求真正相关」的字段
    （pipeline 里那份白名单），模型自带的默认值 —— 包括按显存挑出来的分块档位 ——
    必须在这里兜住，否则模板里写死的字面值会悄悄胜出。
    """
    wf = copy.deepcopy(spec.template)

    effective: dict[str, Any] = {k: v for k, v in spec.defaults.items() if k in spec.bindings}
    effective.update(values)

    for key, value in effective.items():
        if value is None:
            continue
        for path in spec.bindings.get(key, []):
            if not set_path(wf, path, value):
                log.warning("model `%s`: failed to inject %s at `%s`", spec.name, key, path)

    for path, value in (overrides or {}).items():
        if not set_path(wf, str(path), value):
            raise APIError(
                f"workflow_overrides: path `{path}` does not exist in workflow `{spec.name}`.",
                param="workflow_overrides",
            )
    return wf


registry = Registry()
