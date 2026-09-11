"""工作流分析器：从 ComfyUI 的 API 格式工作流图推导 models.yaml 条目草稿。

目标：上传工作流时无需手编 YAML，由这里自动识别
  - mode        : 含视频保存节点 -> video，否则 image
  - output_node : 最终保存节点 id
  - capabilities: 有 LoadImage -> image-to-image，否则 text-to-image；无提示词 -> promptless
  - bindings    : 语义参数 -> "<节点ID>.inputs.<字段>" 路径
  - defaults    : 从图中读取当前采样步数/尺寸等，作为合理默认值

推导是启发式的（按常见节点类名/输入名匹配），生成的草稿交给结构化编辑器
二次微调；任何绑定路径都会经 registry._validate_bindings 在加载时强制校验。

已知覆盖不到、只能手补的部分：视频的 width/height（在聚合器节点上）、duration、
fps（在 CreateVideo 上），以及视频参考槽位 references 段。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("roundabout.analyze")

# 视频类保存节点（输出视频）
VIDEO_SAVE_NODES = {
    "VHS_VideoCombine",
    "SaveAnimatedWEBM",
    "SaveAnimatedMP4",
    "SaveAnimatedPNG",
    "SaveAnimatedGIF",
    "VHS_SaveVideo",
    "SaveVideo",
}
# 图片类保存节点（输出图片）
IMAGE_SAVE_NODES = {"SaveImage", "PreviewImage"}
# 提示词编码节点：类名含 "TextEncode" 的都认——除 CLIPTextEncode 家族外，
# Boogu / MageFlow 的编辑编码节点（TextEncodeBooguEdit / TextEncodeMageFlowEdit）
# 用的是 prompt / negative_prompt 字段，漏掉它们整条绑定会空。
PROMPT_ENCODE_NODES = {"TextEncode"}

# 采样参数 -> (候选节点类关键词, 候选输入字段)，按参数逐个查找。
# 现代采样链把参数拆到了多个节点上（SamplerCustomAdvanced 自身没有任何参数：
# seed 在 RandomNoise、steps/scheduler/denoise 在 BasicScheduler、
# sampler_name 在 KSamplerSelect、cfg 在 CFGGuider），所以不能只认"某个采样器节点"。
SAMPLER_PARAMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "seed": (("RandomNoise", "SamplerCustom", "KSampler"), ("noise_seed", "seed", "rand_seed")),
    "steps": (("Scheduler", "KSampler"), ("steps",)),
    "cfg": (("CFGGuider", "SamplerCustom", "KSampler"), ("cfg",)),
    "sampler_name": (("KSamplerSelect", "KSampler"), ("sampler_name",)),
    "scheduler": (("Scheduler", "KSampler"), ("scheduler",)),
    "denoise": (("Scheduler", "KSampler"), ("denoise",)),
}
PARAM_ORDER = ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise")


def _classes(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """返回 [(node_id, node)]，node 含 class_type/inputs。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for nid, node in data.items():
        if isinstance(node, dict) and "class_type" in node and "inputs" in node:
            out.append((str(nid), node))
    return out


def _match_class(node: dict[str, Any], names: set[str]) -> bool:
    ct = node.get("class_type") or ""
    return any(name in ct for name in names)


# 常见 seed 字段名（不同节点写法不一：采样器用 noise_seed，随机数节点用 seed/rand_seed）
SEED_FIELDS = ("noise_seed", "seed", "rand_seed")


def _read_path(data: dict[str, Any], path: str) -> Any:
    """按 "12.inputs.noise_seed" 形式的绑定路径取值。"""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _inputs_seed(inputs: Any) -> int | None:
    if not isinstance(inputs, dict):
        return None
    for key in SEED_FIELDS:
        v = inputs.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return None


def workflow_seed(data: dict[str, Any], preferred: list[str] | None = None) -> int | None:
    """读出工作流里实际生效的 seed（队列监控用它分辨批量提交的同 prompt 任务）。

    `preferred` 传 models.yaml 各模型 `bindings.seed` 的路径（如 "129.inputs.noise_seed"），
    命中即返回——那是网关真正写入 seed 的位置，比启发式扫描可靠。都未命中时退回：
    先看采样器节点，再扫描任意节点的常见 seed 字段。取不到返回 None。
    """
    if not isinstance(data, dict):
        return None
    for path in preferred or ():
        v = _read_path(data, path)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    nodes = _classes(data)
    hit = _find_param_node(nodes, *SAMPLER_PARAMS["seed"])
    if hit is not None:
        v = _read_path(data, f"{hit[0]}.inputs.{hit[1]}")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    for _nid, node in nodes:
        v = _inputs_seed(node.get("inputs"))
        if v is not None:
            return v
    return None


def _link_source(link: Any) -> str | None:
    """ComfyUI 连线是 [node_id, slot]；返回源节点 id（字符串）。"""
    if isinstance(link, list) and link and isinstance(link[0], (str, int)):
        return str(link[0])
    return None


def _find_link_source(nodes: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """找同时带 positive / negative 连线的节点的 inputs（KSampler / SamplerCustom / CFGGuider）。"""
    fallback: dict[str, Any] = {}
    for _nid, node in nodes:
        ins = node.get("inputs")
        if not isinstance(ins, dict):
            continue
        if not isinstance(ins.get("positive"), list) or not isinstance(ins.get("negative"), list):
            continue
        ct = node.get("class_type") or ""
        if "Sampler" in ct or "Guider" in ct:
            return ins
        fallback = fallback or ins
    return fallback


def _find_param_node(
    nodes: list[tuple[str, dict[str, Any]]],
    classes: tuple[str, ...],
    fields: tuple[str, ...],
) -> tuple[str, str] | None:
    """按候选类关键词顺序找第一个具备候选字段的节点，返回 (节点id, 字段名)。"""
    for want in classes:
        for nid, node in nodes:
            if want not in (node.get("class_type") or ""):
                continue
            ins = node.get("inputs")
            if not isinstance(ins, dict):
                continue
            for field in fields:
                if field in ins:
                    return nid, field
    return None


def _find_save_node(nodes: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any]] | None:
    """优先视频保存节点，其次图片保存节点。"""
    for nid, node in nodes:
        if _match_class(node, VIDEO_SAVE_NODES):
            return nid, node
    for nid, node in nodes:
        if _match_class(node, IMAGE_SAVE_NODES):
            return nid, node
    return None


def _find_width_height(nodes: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any]] | None:
    """寻找宽高仍是字面量的节点：先认潜空间节点（EmptyLatentImage），
    再退到自带尺寸的文本编码节点（MageFlow 这类把宽高挂在编码节点上）。

    宽高必须是数字：编辑类工作流的宽高通常由 GetImageSize 连过来（值是
    `["62", 0]` 这种连线），把字面量注进去会替换掉连线、把图改坏。
    """
    for want in ("Latent", "TextEncode"):
        for nid, node in nodes:
            if want not in (node.get("class_type") or ""):
                continue
            ins = node.get("inputs", {})
            if not isinstance(ins, dict):
                continue
            if isinstance(ins.get("width"), (int, float)) and isinstance(ins.get("height"), (int, float)):
                return nid, node
    return None


# 承载文本的输入字段（编码节点用 text/prompt，PrimitiveString 用 value）
TEXT_FIELDS = ("text", "prompt", "negative_prompt", "value")


def _text_node(node_id: str | None, node_map: dict[str, Any]) -> str | None:
    """顺着 conditioning 连线往上游找真正承载文本的节点。

    采样链的 positive/negative 常先接到 ReferenceLatent / ConditioningCombine
    这类中转节点，提示词还在更上游，所以逐层回溯到带文本字段的节点为止。
    """
    seen: set[str] = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = node_map.get(node_id)
        if not isinstance(node, dict):
            return None
        ins = node.get("inputs") or {}
        if any(field in ins for field in TEXT_FIELDS):
            return node_id
        node_id = _link_source(ins.get("conditioning"))
    return None


def _find_string_node(nodes: list[tuple[str, dict[str, Any]]]) -> str | None:
    """找承载提示词的 PrimitiveString 节点：视频链路把它接进聚合器，没有编码节点。"""
    for nid, node in nodes:
        if "PrimitiveString" not in (node.get("class_type") or ""):
            continue
        if isinstance((node.get("inputs") or {}).get("value"), str):
            return nid
    return None


def _find_load_image(nodes: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any]] | None:
    for nid, node in nodes:
        if "LoadImage" in (node.get("class_type") or ""):
            return nid, node
    return None


def _find_prompt_encode(nodes: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    return [(nid, node) for nid, node in nodes if _match_class(node, PROMPT_ENCODE_NODES)]


def _prompt_field(node: dict[str, Any] | None, positive: bool, same_node: bool = False) -> str | None:
    """返回提示词节点上承接正/负向文本的输入字段名。

    CLIPTextEncode 家族用 `text`；Boogu / MageFlow 的编辑编码节点用
    `prompt` / `negative_prompt`。找不到可承接字段时返回 None，表示不应暴露该绑定：
    负向接到 ConditioningZeroOut 这类零化节点，或正负复用同一个只有 `text` 的节点
    （此时 same_node 为真，避免负向把正向的文本覆盖掉）。
    """
    if not isinstance(node, dict):
        return None
    ins = node.get("inputs")
    if not isinstance(ins, dict):
        return None
    if positive:
        for field in ("text", "prompt"):
            if field in ins:
                return field
        return None
    if "negative_prompt" in ins:
        return "negative_prompt"
    if "text" in ins and not same_node:
        return "text"
    return None


def analyze_workflow(data: dict[str, Any], workflow_filename: str, model_name: str,
                     description: str = "") -> dict[str, Any]:
    """从工作流图推导一份 models.yaml 条目草稿。

    返回形如:
        {
          "workflow": "<filename>",
          "description": "...",
          "mode": "image" | "video",
          "capabilities": ["text-to-image"] (+ "image-to-image"),
          "output_node": "<id>",
          "timeout": 300 | 600,
          "defaults": {...},
          "bindings": {"prompt": "6.inputs.text", ...},
        }
    """
    nodes = _classes(data)
    if not nodes:
        raise ValueError("workflow contains no recognizable nodes")

    save_id, save_node = _find_save_node(nodes)
    latent = _find_width_height(nodes)
    load_img = _find_load_image(nodes)
    prompt_nodes = _find_prompt_encode(nodes)
    node_map = dict(nodes)

    is_video = save_node is not None and _match_class(save_node, VIDEO_SAVE_NODES)

    # ---- 正/负提示词节点：先顺着采样器 / guider 的 positive-negative 连线追溯，
    # 追溯不到再按编码节点出现顺序取（第二个多半是 negative）----
    link_src = _find_link_source(nodes)
    positive_id = _text_node(_link_source(link_src.get("positive")), node_map)
    negative_id = _text_node(_link_source(link_src.get("negative")), node_map)
    if positive_id is None and prompt_nodes:
        positive_id = prompt_nodes[0][0]
    if negative_id is None and len(prompt_nodes) > 1:
        negative_id = prompt_nodes[1][0]

    bindings: dict[str, str] = {}

    def bind(param: str, node_id: str | None, field: str) -> None:
        if node_id is None:
            return
        path = f"{node_id}.inputs.{field}"
        # 仅当路径在图中真实存在才绑定（加载时会再校验）
        if _read_path(data, path) is None:
            return
        bindings[param] = path

    pos_field = _prompt_field(node_map.get(positive_id), True)
    if positive_id and pos_field:
        bind("prompt", positive_id, pos_field)
    # 负向接到 ConditioningZeroOut 之类的零化节点、或与正向复用同一个只有 `text`
    # 的编码节点时，_prompt_field 返回 None，表示不应暴露 negative_prompt 绑定。
    neg_field = _prompt_field(node_map.get(negative_id), False, same_node=negative_id == positive_id)
    if negative_id and neg_field:
        bind("negative_prompt", negative_id, neg_field)
    if "prompt" not in bindings:
        # 视频链路把提示词塞在 PrimitiveStringMultiline 里，没有文本编码节点
        bind("prompt", _find_string_node(nodes), "value")

    # ---- 采样参数：逐个参数在整张图里找归属节点（采样链可能拆成多个节点）----
    for param in PARAM_ORDER:
        hit = _find_param_node(nodes, *SAMPLER_PARAMS[param])
        if hit is not None:
            bind(param, hit[0], hit[1])

    # width / height：优先 EmptyLatentImage 这类节点
    if latent is not None:
        bind("width", latent[0], "width")
        bind("height", latent[0], "height")

    # 图生图：LoadImage 的 image 输入（视频的参考图槽位走 references 段，不绑 image）
    if load_img and not is_video:
        bind("image", load_img[0], "image")
        # 某些工作流用单独的 mask 输入（LoadImageMask / MaskToImage 等），不强求

    # 输出文件名前缀：绑定后调用方能用 filename_prefix 控制落盘目录
    if save_node is not None and "filename_prefix" in (save_node.get("inputs") or {}):
        bind("filename_prefix", save_id, "filename_prefix")

    # 视频：保存节点自身的 frame_rate / fps（CreateVideo 上的 fps 需手工补）
    if is_video and save_node is not None:
        ins = save_node.get("inputs", {})
        if "frame_rate" in ins:
            bind("fps", save_id, "frame_rate")
        elif "fps" in ins:
            bind("fps", save_id, "fps")

    # ---- defaults：从图中读取当前值，给出合理默认 ----
    defaults: dict[str, Any] = {}
    if latent is not None:
        ins = latent[1].get("inputs", {})
        for key in ("width", "height"):
            val = ins.get(key)
            if isinstance(val, (int, float)):
                defaults[key] = int(val)
    for param in PARAM_ORDER:
        if param not in bindings:  # 未暴露成绑定的没机会被调用方覆盖，不写默认值
            continue
        path = bindings[param]
        val = _read_path(data, path)
        if val is not None and not isinstance(val, (list, dict)):
            defaults[param] = val
    if is_video and save_node is not None and "fps" in bindings:
        fr = _read_path(data, bindings["fps"])
        if isinstance(fr, (int, float)):
            defaults["fps"] = int(fr)

    # ---- 能力声明 ----
    # 对齐 models.yaml 既有约定：纯编辑模型只写 image-to-image（pipeline 会据此
    # 强制要求传 image）；无提示词的工具类工作流标 promptless，避免加载校验报错。
    if is_video:
        capabilities = ["text-to-video"]
    elif "prompt" not in bindings:
        capabilities = ["image-to-image"]
    elif load_img:
        capabilities = ["image-to-image"]
    else:
        capabilities = ["text-to-image"]

    entry: dict[str, Any] = {
        "workflow": workflow_filename,
        "description": description or f"由工作流 {workflow_filename} 自动生成",
        "mode": "video" if is_video else "image",
        "capabilities": capabilities,
        "output_node": save_id,
        "timeout": 600 if is_video else 300,
        "bindings": bindings,
    }
    if "prompt" not in bindings:
        entry["promptless"] = True
    if defaults:
        entry["defaults"] = defaults
    return entry
