"""SelfLift 低分前缀的分辨率策略（``lowres_scale``）。

SelfLift 的两阶段采样里，一采（低分前缀）跑在 ``lowres_scale`` 指定的画布上：

    low_h = round(H_latent * lowres_scale / 2) * 2      # comfyui-SelfLift/nodes.py

关键在于**它是相对比例** —— 同一个数值在不同目标尺寸下对应的绝对分辨率完全不同：

| 目标 | `lowres_scale=0.70` 的低分 |
|---|---|
| 1920x1088（1080p） | 1344x768 |
| 2560x1440（2K） | 1792x992（已超原生 33%）|

而画质只跟低分的**绝对**分辨率有关。H3 的原生画布长边是 1344 px（84 个 latent），
实测（见 skill: selflift-progressive-upscale 的《一采分辨率扫描》）：

- 低分长边 **= 1344**（正好原生）→ 细节份额峰值、二采净增益最高
- 低分长边 **> 1344**（超原生）→ 宽谱细节塌陷 **且** 毛发被织出规则的斜向假网格，
  两种失效各自独立（864p 档起掉细节，992p 档起织网）
- 低分长边 **< 1344** → 只是把负担推给高分阶段，是安全的省时档

所以 ``auto`` 的口径是：**把低分长边压在原生画布上，并保留一次有意义的放大**。

    L = min(84 / max(W, H)_latent, 0.70)

1080p → 0.70（实测最优点）、2K → 0.525、4K → 0.35。2K 与 4K 照搬 0.70 会越过原生
画布，正是上面那两种失效的触发条件，所以这个反推必须知道**目标尺寸**。

放在独立模块（而不是 params.py）是为了让 registry 也能引用：params 依赖 registry，
反向 import 会成环。
"""

from __future__ import annotations

import logging

from .errors import APIError

log = logging.getLogger("roundabout.lowres")

# H3 原生画布长边（latent 单位）= 1344 px / 16
NATIVE_LATENT = 84
# auto 的比例上限：84/120，即 1920x1088 的取值（实测细节份额峰值点）。
# 它的另一个作用是把「目标本身小于原生画布」的情形也钉在同一个相对放大率上，
# 避免退化成恒等放大（L=1.0）。
AUTO_MAX_SCALE = 0.70
# comfyui-SelfLift 节点自己的硬边界（nodes.py: `0.25 <= lowres_scale <= 1.0`）
MIN_SCALE = 0.25
MAX_SCALE = 1.0
# 与 auto 等价的字符串写法（大小写不敏感）
_AUTO_TOKENS = {"auto", "default", ""}


def lowres_latent(width: int | None, height: int | None, scale: float) -> tuple[int, int]:
    """低分 latent 的 ``(h, w)``，复刻 comfyui-SelfLift 的算法（四舍五入到偶数）。"""
    def one(px: int | None) -> int:
        return max(2, round((int(px or 0) // 16) * scale / 2) * 2)

    return one(height), one(width)


def auto_lowres_scale(width: int | None, height: int | None) -> float:
    """按目标尺寸反推 auto 比例；尺寸拿不到时退回 :data:`AUTO_MAX_SCALE`。"""
    try:
        long_px = max(int(width or 0), int(height or 0))
    except (TypeError, ValueError):
        long_px = 0
    if long_px <= 0:
        log.debug("lowres_scale=auto: 目标尺寸未知，退回 %.2f", AUTO_MAX_SCALE)
        return AUTO_MAX_SCALE
    value = _clamp(min(NATIVE_LATENT / (long_px // 16), AUTO_MAX_SCALE))
    _warn_if_over_native(value, width, height)
    return value


def resolve_lowres_scale(
    raw: object,
    width: int | None,
    height: int | None,
) -> float | None:
    """把请求 / 默认值里的 ``lowres_scale`` 解析成可注入节点的浮点数。

    返回 ``None`` 表示「不注入」—— 沿用工作流模板里的字面值，这是没声明
    ``lowres_scale`` 默认档的老行为。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool 是 int 的子类，单独拦掉避免 True -> 1.0
        raise _bad(raw)
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _AUTO_TOKENS:
            return auto_lowres_scale(width, height)
        try:
            value = float(token)
        except ValueError:
            raise _bad(raw) from None
    else:
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise _bad(raw) from None

    if not MIN_SCALE <= value <= MAX_SCALE:
        raise APIError(
            f"`lowres_scale` must be between {MIN_SCALE} and {MAX_SCALE} "
            f"(SelfLift node limit), got {value}.",
            param="lowres_scale",
        )
    _warn_if_over_native(value, width, height)
    return value


def _bad(raw: object) -> APIError:
    return APIError(
        f'`lowres_scale` must be a number in [{MIN_SCALE}, {MAX_SCALE}] or "auto", got {raw!r}.',
        param="lowres_scale",
    )


def _clamp(value: float) -> float:
    return max(MIN_SCALE, min(MAX_SCALE, value))


def _warn_if_over_native(scale: float, width: int | None, height: int | None) -> None:
    """低分长边越过原生画布时告警 —— 显式给大值不拦（属于调用方的选择），但要说清代价。"""
    if not width or not height:
        return
    h, w = lowres_latent(width, height, scale)
    if max(h, w) <= NATIVE_LATENT:
        return
    log.warning(
        "lowres_scale=%.3f @ %sx%s -> 低分 latent %dx%d（长边 %d px）越过 H3 原生画布 1344："
        "实测会掉宽谱细节并织出规则假网格，建议 <= %.3f（或用 auto）。",
        scale, width, height, w, h, max(h, w) * 16,
        min(NATIVE_LATENT / max(int(width) // 16, int(height) // 16), AUTO_MAX_SCALE),
    )
