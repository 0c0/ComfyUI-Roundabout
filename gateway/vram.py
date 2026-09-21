"""按本机显存总量选「低显存分块」档位。

MiniMax H3 这类大模型（多支视频工作流权重合计 ~65 GiB）在显存/内存不足时靠
节点级分块把激活张量切小：

- ``MiniMaxChunkFeedForward.chunks``  切 FFN 的 token 维
- ``MiniMaxChunkFeedForward.seq_threshold`` 只有 token 数超过它才分块
- ``MiniMaxLowVRAMAttention.head_chunks`` 切 56 个注意力头 —— **当前无绑定**：该节点与
  ``BlockSparseAttention`` 硬互斥（前者替换 block.forward，不收后者补传的 ``attention``），
  6 支视频档已统一走稀疏注意力 + FFN 分块，档位表里的数值暂无人消费
- ``SelfLiftH3Sampler.highres_tiling`` 高分辨率阶段按剩余显存**自动**决定切几块 ——
  SelfLiftH3Sampler 随 self-lift 下线，同样无绑定

前面几项是纯粹的「用时间换显存」，不改变输出；``highres_tiling`` 的块数不可手调。它们的
合适取值只取决于显存大小，所以做成按档位自动填默认值 —— 同一份工作流换机器不用改 JSON。

档位表放在 ``models.yaml`` 的 ``defaults.vram_tiers``（改参数不用动代码），本模块只
负责两件纯函数式的事：**探测显存** 与 **选档**，便于离线测试。
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("roundabout.vram")

# 手动钉住档位（测试 / 共享机器上固定配置用）；值为 GiB，如 "8"、"24"。
ENV_KEY = "ROUNDABOUT_VRAM_GB"

# 档位匹配容差（GiB）。显卡报的可用显存普遍略低于标称值（如 12G 卡报 12282 MiB
# = 11.994 GiB），不留容差会掉到下一档。
_TIER_TOLERANCE_GB = 0.6

_UNPROBED = object()
_detected: Any = _UNPROBED


def reset_cache() -> None:
    """清掉探测缓存（测试用）。"""
    global _detected
    _detected = _UNPROBED


def total_vram_gb() -> float | None:
    """本机单卡显存总量（GiB）。探测不到（纯 CPU / 无 torch）返回 None。

    结果缓存：显存总量在进程生命周期内不变，而 ``torch.cuda.get_device_properties``
    会初始化 CUDA 上下文，不适合反复调用。
    """
    global _detected
    if _detected is _UNPROBED:
        _detected = _probe()
        if _detected is None:
            log.info("vram: 未探测到 GPU 显存，按档位自动调参将跳过")
        else:
            log.info("vram: 本机显存总量 %.1f GiB", _detected)
    return _detected


def _probe() -> float | None:
    """依次尝试：环境变量覆盖 → CUDA → XPU。都拿不到就返回 None。"""
    override = os.environ.get(ENV_KEY)
    if override not in (None, ""):
        try:
            value = float(override)
            if value > 0:
                return value
        except ValueError:
            log.warning("vram: %s=%r 不是合法数字，忽略", ENV_KEY, override)
        return None

    try:
        import torch
    except Exception:  # noqa: BLE001 - 没有 torch 就当探测失败
        return None

    # N 卡：优先跟随 ComfyUI 自己选的设备（多卡时可能不是 0 号）
    if torch.cuda.is_available():
        device: Any = 0
        try:
            from comfy import model_management as mm  # noqa: PLC0415 - 延迟导入，避免拖慢网关冷启动

            device = mm.get_torch_device()
        except Exception:  # noqa: BLE001 - 脱离 ComfyUI 进程运行时没有 comfy 包
            pass
        size = _cuda_props_gb(torch, device)
        if size is not None:
            return size

    # Intel 核显/独显
    try:
        if torch.xpu.is_available():
            return round(torch.xpu.get_device_properties(0).total_memory / (1024**3), 2)
    except Exception:  # noqa: BLE001
        pass
    return None


def _cuda_props_gb(torch: Any, device: Any) -> float | None:
    for target in (device, 0):
        try:
            props = torch.cuda.get_device_properties(target)
            total = getattr(props, "total_memory", None)
            if total:
                return round(total / (1024**3), 2)
        except Exception:  # noqa: BLE001 - 设备号不存在等
            continue
    return None


def select_tier(vram_gb: float | None, tiers: list[dict[str, Any]] | None) -> dict[str, Any]:
    """按显存总量从档位表里挑一档，返回该档的参数（不含 ``min_gb``）。

    档位表条目形如 ``{"min_gb": 24, "chunks": 2, "head_chunks": 8, ...}``：
    取 ``min_gb`` 不超过本机显存的最大那一档（留 :data:`_TIER_TOLERANCE_GB` 容差，
    抵消显卡标称容量与实际可用量的差）。探测不到显存返回空字典 —— 此时不覆盖
    工作流自带的值，行为等同旧版本。
    """
    if not tiers or vram_gb is None:
        return {}

    picked: dict[str, Any] | None = None
    picked_min = float("-inf")
    for entry in tiers:
        raw = entry.get("min_gb")
        try:
            floor = float(raw)
        except (TypeError, ValueError):
            continue
        if floor - _TIER_TOLERANCE_GB <= vram_gb and floor > picked_min:
            picked, picked_min = entry, floor

    if picked is None:
        return {}
    return {k: v for k, v in picked.items() if k != "min_gb"}


def normalize_tiers(raw: Any) -> list[dict[str, Any]]:
    """校验并规范化 ``defaults.vram_tiers``，按 ``min_gb`` 升序返回。"""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise RuntimeError("defaults.vram_tiers must be a list of `{min_gb: <GiB>, <param>: <value>}`")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise RuntimeError(f"defaults.vram_tiers[{i}] must be a mapping")
        if "min_gb" not in entry:
            raise RuntimeError(f"defaults.vram_tiers[{i}] is missing `min_gb`")
        try:
            min_gb = float(entry["min_gb"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"defaults.vram_tiers[{i}].min_gb is not a number: {entry['min_gb']!r}") from exc
        out.append({**entry, "min_gb": min_gb})
    out.sort(key=lambda e: e["min_gb"])
    return out
