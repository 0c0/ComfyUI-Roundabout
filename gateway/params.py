"""把 OpenAI 请求参数规范化成 ComfyUI 工作流需要的语义参数。"""

from __future__ import annotations

import base64
import binascii
import os
import random
import re
from pathlib import Path
from typing import Any

import aiohttp

from .comfy_client import get_session
from .config import settings
from .errors import APIError
from .registry import ModelSpec

_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$", re.IGNORECASE)
_DATAURL_RE = re.compile(r"^data:(?P<mime>[\w/+.-]+)?;base64,(?P<data>.*)$", re.DOTALL)

_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"GIF87a": ("gif", "image/gif"),
    b"GIF89a": ("gif", "image/gif"),
    b"RIFF": ("webp", "image/webp"),
}

# 产物按扩展名判定媒体类型（视频/图片/音频共用，供 /view 与文件服务路由使用）
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "m4v": "video/mp4",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
}

# MIME → 扩展名（dataURL 场景反查用）
_MIME_EXT = {v: k for k, v in _EXT_MIME.items()}


# ------------------------------------------------------------------ 尺寸
def parse_size(size: str | None, spec: ModelSpec) -> tuple[int | None, int | None]:
    """返回 (width, height)；auto/None 时用模型默认值。"""
    if not size or size.strip().lower() in {"auto", "default", ""}:
        return spec.defaults.get("width"), spec.defaults.get("height")

    m = _SIZE_RE.match(size)
    if not m:
        raise APIError(
            f"Invalid `size`: {size!r}. Expected `<width>x<height>` (e.g. 1024x1024) or `auto`.",
            param="size",
        )
    w, h = int(m.group(1)), int(m.group(2))

    if spec.size_choices and f"{w}x{h}" not in spec.size_choices:
        raise APIError(
            f"Unsupported `size` {w}x{h} for model `{spec.name}`. Supported: {', '.join(spec.size_choices)}.",
            param="size",
        )

    for label, v in (("width", w), ("height", h)):
        if not 64 <= v <= 4096:
            raise APIError(f"`size` {label} must be between 64 and 4096, got {v}.", param="size")

    # 扩散模型的隐空间是 8 倍下采样，非 8 的倍数会直接报错或出现边缘伪影
    return _round8(w), _round8(h)


def _round8(v: int) -> int:
    return max(64, int(round(v / 8.0)) * 8)


# ------------------------------------------------------------------ 视频分辨率预设
# 视频模型（MiniMax H3 等）用「画质档位 × 宽高比」描述分辨率。这里把用户友好的
# 预设键换算成具体 width/height。**所有维度必须是 32 的倍数**：隐空间 16 倍下采样后
# 还要过 DiT 的 2×2 patch ⇒ latent 宽高必须为偶数（16×2=32；latent 出现奇数会在
# patchify 时炸 shape 错误，如 1360→latent 85）。档位基准边不是 32 倍数时就近上取
# （1080→1088、720→736），与扩散对齐约束一致。
#
# 约定：「p」指该档位的基准边——横向比例(16:9/4:3/1:1)取 height=档位，纵向比例(9:16/3:4)
# 取 width=档位。例如 480p-16:9 = 848×480，720p-9:16 = 720×1280，1080p-16:9 = 1920×1088。
# 档位：480 / 576 / 720 / 768 / 1080 / 1440。基准边不是 16 倍数时（1080）就近上取，保证所有维度都对齐。
# 576 与 1440 都是 16 的倍数 ⇒ 这两档的五种比例全部自然对齐、无需上取。576 给「快速调试」用
# （576p-16:9 = 1024×576 ≈ 768p 的 55% 像素）；1440p 是 8GB 显存**直接生跑不动**的大档
# （16:9 = 2560×1440 ≈ 768p 的 3.6 倍像素），要更大画面优先走 lift（输出 = 画布 × 1.5）。
VIDEO_RES_PRESETS: dict[int, dict[str, tuple[int, int]]] = {
    480: {
        "1:1": (480, 480),
        "4:3": (640, 480),
        "3:4": (480, 640),
        "16:9": (864, 480),
        "9:16": (480, 864),
    },
    576: {
        "1:1": (576, 576),
        "4:3": (768, 576),
        "3:4": (576, 768),
        "16:9": (1024, 576),
        "9:16": (576, 1024),
    },
    720: {
        # 720 是 16 的倍数但 720/16=45 为奇数 ⇒ 与 1080 同样需要上取，就近取 736（46×16、23×32）。
        "1:1": (736, 736),
        "4:3": (992, 736),
        "3:4": (736, 992),
        "16:9": (1312, 736),
        "9:16": (736, 1312),
    },
    768: {
        "1:1": (768, 768),
        "4:3": (1024, 768),
        "3:4": (768, 1024),
        # 768×16/9 = 1365.3 → 上取 1376（86×16、43×32）；1360 的 latent 宽是 85（奇数）会炸
        "16:9": (1376, 768),
        "9:16": (768, 1376),
    },
    1080: {
        # 1080 不是 16 的倍数（1080 / 16 = 67.5），就近上取到 1088 = 16×68 = 32×34，
        # 于是 16:9 落成标准的 1920×1088（比标称高 8 px，换来所有维度都满足扩散对齐约束）。
        "1:1": (1088, 1088),
        "4:3": (1440, 1088),
        "3:4": (1088, 1440),
        "16:9": (1920, 1088),
        "9:16": (1088, 1920),
    },
    1440: {
        # 1440 本身是 16 的倍数（1440 / 16 = 90）⇒ 无需像 1080 那样上取，16:9 即标准 2560×1440。
        "1:1": (1440, 1440),
        "4:3": (1920, 1440),
        "3:4": (1440, 1920),
        "16:9": (2560, 1440),
        "9:16": (1440, 2560),
    },
}
_RES_TIERS = {480, 576, 720, 768, 1080, 1440}
_RES_RATIOS = {"1:1", "3:4", "4:3", "16:9", "9:16"}
_RES_PRESET_RE = re.compile(
    r"^(?P<tier>[0-9]+)p[-_](?P<ratio>[0-9]+:[0-9]+)$"
    r"|^(?P<ratio2>[0-9]+:[0-9]+)[-_@](?P<tier2>[0-9]+)p$"
)


def resolve_video_size(size: str | None, spec: ModelSpec) -> tuple[int | None, int | None]:
    """视频尺寸解析：优先匹配预设键 `<tier>p-<ratio>` 或 `<ratio>@<tier>p`，

    否则回退到 OpenAI 风格 `<width>x<height>`（parse_size）。auto/None 用模型默认。
    返回 (width, height)。
    """
    if not size or size.strip().lower() in {"auto", "default", ""}:
        return spec.defaults.get("width"), spec.defaults.get("height")

    m = _RES_PRESET_RE.match(size.strip())
    if m:
        tier = int(m.group("tier") or m.group("tier2"))  # type: ignore[arg-type]
        ratio = (m.group("ratio") or m.group("ratio2")).strip()  # type: ignore[union-attr]
        table = VIDEO_RES_PRESETS.get(tier)
        if table is None or ratio not in table:
            raise APIError(
                f"Unsupported resolution preset `{size}`. "
                f"Tiers: {', '.join(f'{t}p' for t in sorted(_RES_TIERS))}. "
                f"Ratios: {', '.join(sorted(_RES_RATIOS))}.",
                param="size",
            )
        return table[ratio]

    # 回退：显式 WxH —— 视频档要求 32 对齐（比图像档的 8 对齐更严，见上方注释）
    w, h = parse_size(size, spec)
    return _round32(w), _round32(h)


def _round32(v: int) -> int:
    return max(64, int(round(v / 32.0)) * 32)


def resolve_output_scale(output_size: str, in_w: int, in_h: int, spec: ModelSpec) -> float:
    """由期望输出尺寸反推 lift 放大倍率。

    lift 的 latent 上采样是**等比**的（单一 scale 因子），输出保持画布宽高比：
    scale = 输出短边 / 画布短边，长边随画布比例走（预设键两边比例若与画布不一致，
    长边会有少量偏差，实际输出尺寸由响应 `size` 回显）。

    宽高比偏差超过 5%（如 16:9 画布配 9:16 输出）直接 400 —— 那是换构图，不是放大。
    """
    out_w, out_h = resolve_video_size(output_size, spec)
    # 宽高比带方向直接比（先比比例再算 scale）：只用长短边比会漏掉「横竖旋转」——
    # 如 7:4 画布配 9:16 输出，长短边的比值恰好接近（2560/1344≈1.905 vs 1440/768=1.875）。
    in_ar, out_ar = in_w / in_h, out_w / out_h
    if abs(out_ar - in_ar) / in_ar > 0.05:
        scale0 = min(out_w, out_h) / min(in_w, in_h)
        raise APIError(
            f"`output_size` {out_w}x{out_h} does not match the input canvas aspect "
            f"({in_w}x{in_h}): the lift stage scales uniformly, so the output keeps the "
            f"canvas aspect. Closest achievable from this canvas: "
            f"{round(in_w * scale0)}x{round(in_h * scale0)} "
            "(actual output size is echoed in the response `size`).",
            param="output_size",
        )
    scale = min(out_w, out_h) / min(in_w, in_h)
    if not 1.0 <= scale <= 4.0:
        raise APIError(
            f"`output_size` {out_w}x{out_h} implies scale {scale:.3f}, outside the "
            f"supported 1.0–4.0 range (canvas {in_w}x{in_h}).",
            param="output_size",
        )
    return round(scale, 6)


# ------------------------------------------------------------------ 注意力档位
# 稀疏注意力（sol-attn）与致密画质高度一致、差异只在高频细节（同 seed MAE 20.2 / SSIM 0.615，
# 约为「换种子内容噪声」的 1/3），代价端是耗时（8 步 768p 实测 0.76~0.79x）。所以把它做成
# 用户可选的「更快 ↔ 更高质量」档：
#   sparse（默认）= 保留 BlockSparseAttention 模板默认起始点 0.2（8 步档吃头 2 步走致密）
#   dense         = 把 start_percent 顶到 1.0
# 只给 base 四支（minimax-h3 / -edit / -lift / -lift-edit）：它们跑的是 training-free 的
# sol-attn，「关掉」就是换回本来会算的致密注意力。FastH3 两支不参与 —— 它的 `vsa` 与
# 蒸馏权重配对训练，关掉不是「更高画质」而是脱离训练分布；该档只求快，恒定稀疏即最优。
#
# 1.0 为什么等效「关闭稀疏」：apply_block_sparse_attention 把 start_percent 过
# percent_to_sigma()，而所有 percent_to_sigma 实现里 `percent >= 1.0` 一律返回 0.0；
# SparseAttnPatch.dense_reason() 判 `sigma > sigma_start` 即走 dense ⇒ 每个采样步
# （sigma 恒 > 0）都落在窗外 ⇒ 全程致密。
ATTENTION_SPARSE_START: dict[str, float] = {"sparse": 0.2, "dense": 1.0}


def resolve_attention(attention: str) -> float:
    """把注意力档位翻译成 `BlockSparseAttention.start_percent` 的值。

    非法值直接报 400，不回落 —— 静默回落会让「参数看起来支持、实际无效」难以定位。
    """
    key = attention.strip().lower()
    try:
        return ATTENTION_SPARSE_START[key]
    except KeyError:
        raise APIError(
            f"Invalid `attention`: {attention!r}. Expected one of: "
            f"{', '.join(sorted(ATTENTION_SPARSE_START))}.",
            param="attention",
        ) from None


# ------------------------------------------------------------------ 种子
# JS 安全整数上限（2^53-1 ≈ 9.007e15，16 位）。超过它的种子经过任何把 JSON number
# 转 float64 的中间层（JS 宿主、浏览器、部分 SDK）都会静默丢精度——实测会出现
# 「末几位被改写」（如 1788460445875452919 -> 1788460445875453000）。网关自己透传
# 没问题，但拦在上游：超限直接报错，把静默损坏变成显式失败，建议换短种子。
SAFE_SEED = 2**53 - 1


def resolve_seed(seed: int | None, index: int = 0) -> int:
    # 不传 / -1 → 随机（OpenAI 兼容语义：-1 视为"未指定"）；0 与正整数 → 固定种子
    if seed is None or seed < 0:
        # 随机种子也压在安全区，保证回显值能被任何客户端原样复用
        return random.randint(0, SAFE_SEED)
    seed = int(seed)
    if seed > SAFE_SEED:
        raise APIError(
            f"`seed` {seed} exceeds the safe integer limit {SAFE_SEED} (2^53-1). "
            "Longer seeds lose precision in JSON/JS number layers before reaching the "
            "gateway (trailing digits get rewritten). Use a shorter seed, e.g. <= 15 digits.",
            param="seed",
        )
    # 批量生成时递增，保证 n>1 不返回 n 张一样的图
    return seed + index


# ------------------------------------------------------------------ 预设
def apply_presets(
    values: dict[str, Any],
    spec: ModelSpec,
    style: str | None,
) -> dict[str, Any]:
    out = dict(values)

    if style:
        preset = spec.style_presets.get(style) or spec.style_presets.get(style.lower()) or {}
        suffix = preset.get("prompt_suffix")
        if suffix and out.get("prompt"):
            out["prompt"] = f"{out['prompt']}, {suffix}"
        if preset.get("negative_suffix"):
            base = out.get("negative_prompt") or ""
            out["negative_prompt"] = f"{base}, {preset['negative_suffix']}".strip(", ")
    return out


# ------------------------------------------------------------------ 负向分流
# 把 prompt 里的 "no / without / not X" 抽出来移入 negative_prompt。
# 仅对支持负向提示词的模型生效；不支持的模型保持原样（negative_prompt 会被静默丢弃，
# 只能靠 prompt 内联否定兜底）。引号内一律视为字面量，绝不抽取。
_NEG_RE = re.compile(r"\b(no|without|not)\b", re.IGNORECASE)
_DELIM_RE = re.compile(r"[,;.]")
_QUOTE_RE = re.compile(r'"[^"]*"|\'[^\']*\'')


def split_negative_prompt(
    prompt: str | None,
    negative_prompt: str | None,
    supports_negative: bool,
) -> tuple[str | None, str | None]:
    """把 prompt 中的 "no/without/not X" 抽取并移入 negative_prompt。

    返回 (清理后的 prompt, 合并后的 negative_prompt)。

    规则：
    - supports_negative=False：原样返回（不支持负向的模型无法消费 negative_prompt）。
    - 引号内内容视作字面量，绝不抽取（如 `"no entry"` 是真实主体文字）。
    - 抽取对象到逗号/分号/句号或下一个否定词即截断（支持多词对象 `no red hat`）。
    - 若清理后 prompt 变空（整句都是否定），则正向留空、被抽内容全部进入 negative_prompt。
    """
    if not supports_negative or not prompt:
        return prompt, negative_prompt

    # 1) 保护引号内容（用占位符替换，避免引号内的 "no ..." 被误抽）
    quotes: list[str] = []

    def _protect(m: re.Match) -> str:
        quotes.append(m.group(0))
        return f"\x00Q{len(quotes) - 1}\x00"

    protected = _QUOTE_RE.sub(_protect, prompt)

    # 2) 逐段扫描否定词，抽取对象
    extracted: list[str] = []
    removed: list[tuple[int, int]] = []
    pos = 0
    while pos <= len(protected):
        m = _NEG_RE.search(protected, pos)
        if not m:
            break
        ostart = m.end()
        end = len(protected)
        dm = _DELIM_RE.search(protected, ostart)
        if dm:
            end = min(end, dm.start())
        nm = _NEG_RE.search(protected, ostart)
        if nm:
            end = min(end, nm.start())
        obj = protected[ostart:end].strip()
        obj = _restore_quotes(obj, quotes)  # 还原引号占位为原文
        obj = obj.strip().strip("\"'").strip()
        obj = _strip_leading_article(obj)
        if obj:
            extracted.append(obj)
            removed.append((m.start(), end))
        pos = end

    # 3) 拼接剩余文本并还原引号
    if removed:
        pieces: list[str] = []
        last = 0
        for s, e in removed:
            pieces.append(protected[last:s])
            last = e
        pieces.append(protected[last:])
        cleaned = "".join(pieces)
        # 合并被抽走片段两侧残留的多个分隔符（如 "a, , b" → "a, b"）
        cleaned = re.sub(r"(?:\s*[,;]\s*)+", ", ", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        cleaned = cleaned.strip(" ,;.-")
    else:
        cleaned = protected

    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        return quotes[idx] if idx < len(quotes) else m.group(0)

    cleaned = re.sub(r"\x00Q(\d+)\x00", _restore, cleaned)

    return cleaned, _merge_negative(negative_prompt, extracted)


def _restore_quotes(text: str, quotes: list[str]) -> str:
    return re.sub(
        r"\x00Q(\d+)\x00",
        lambda m: quotes[int(m.group(1))] if int(m.group(1)) < len(quotes) else m.group(0),
        text,
    )


def _strip_leading_article(obj: str) -> str:
    m = re.match(r"^(a|an|the)\s+", obj, re.IGNORECASE)
    return obj[m.end():] if m else obj


def _merge_negative(base: str | None, extracted: list[str]) -> str | None:
    if not extracted:
        return base
    if base:
        existing = {p.strip().lower() for p in base.split(",") if p.strip()}
        extra = [e for e in extracted if e.lower() not in existing]
        if not extra:
            return base
        return base.rstrip(", ") + ", " + ", ".join(extra)
    return ", ".join(extracted)


# ------------------------------------------------------------------ 输入图
def sniff_image(data: bytes) -> tuple[str, str]:
    for magic, (ext, mime) in _MAGIC.items():
        if data.startswith(magic):
            return ext, mime
    return "png", "image/png"


def mime_for_ext(ext: str) -> str:
    """按扩展名返回媒体类型；未知扩展名回退 application/octet-stream。"""
    return _EXT_MIME.get(ext.lstrip(".").lower(), "application/octet-stream")


def ext_and_mime(filename: str | None, data: bytes) -> tuple[str, str]:
    """优先按产物文件名扩展名判定（视频多为 mp4/webm，魔数嗅探识别不了）；
    扩展名未知时回退到图片魔数嗅探。"""
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _EXT_MIME:
            return ext, _EXT_MIME[ext]
    return sniff_image(data)


def decode_b64_image(raw: str, *, param: str = "image", max_mb: float | None = None) -> bytes:
    payload = raw.strip()
    m = _DATAURL_RE.match(payload)
    if m:
        payload = m.group("data")
    payload = re.sub(r"\s+", "", payload)
    padding = (-len(payload)) % 4
    try:
        data = base64.b64decode(payload + "=" * padding, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise APIError(f"`{param}` is not valid base64 image data: {exc}", param=param) from exc
    _check_size(data, param, max_mb)
    if not data:
        raise APIError(f"`{param}` decoded to an empty payload.", param=param)
    return data


async def fetch_image_url(url: str, *, param: str = "image", max_mb: float | None = None) -> bytes:
    session = get_session()
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise APIError(f"Failed to download `{param}` from {url}: HTTP {resp.status}", param=param)
            data = await resp.read()
    except aiohttp.ClientError as exc:
        raise APIError(f"Failed to download `{param}` from {url}: {exc}", param=param) from exc
    _check_size(data, param, max_mb)
    return data


async def load_image_input(value: str, *, param: str = "image", max_mb: float | None = None) -> bytes:
    """把参考素材（图/视频/音频）解析为字节：base64 / dataURL / http(s) URL / 本地路径。

    `max_mb` 为体积上限（MB）；参考视频/音频用更大的 max_input_asset_mb，默认仍用图片上限。
    与网关同机时常见本地路径（如 ComfyUI 的 input 目录）：仅在文件确实存在时才按路径读取，
    避免把 base64 字符串误判成路径。
    """
    v = value.strip()
    if v.startswith(("http://", "https://")):
        return await fetch_image_url(v, param=param, max_mb=max_mb)
    if os.path.exists(v):
        try:
            data = Path(v).read_bytes()
        except OSError as exc:
            raise APIError(f"Cannot read `{param}` from file `{v}`: {exc}", param=param)
        _check_size(data, param, max_mb)
        return data
    return decode_b64_image(v, param=param, max_mb=max_mb)


def _asset_ext(value: str | None, data: bytes, kind: str) -> str:
    """推断参考素材扩展名（用于上传时落盘文件名），按 路径/URL 扩展名 → dataURL MIME → 魔数 → 默认 顺序。"""
    if isinstance(value, str):
        v = value.strip()
        if "://" not in v:
            tail = v.rsplit("/", 1)[-1]
            if "." in tail:
                ext = tail.rsplit(".", 1)[-1].lower()
                if ext.isalnum() and 1 <= len(ext) <= 5:
                    return ext
        m = _DATAURL_RE.match(v)
        if m and m.group("mime"):
            e = _MIME_EXT.get(m.group("mime").lower())
            if e:
                return e
    if kind == "image":
        ext, _ = sniff_image(data)
        return ext
    return "mp4" if kind == "video" else "wav"


def _check_size(data: bytes, param: str, max_mb: float | None = None) -> None:
    cap = max_mb if max_mb is not None else settings.max_input_image_mb
    limit = cap * 1024 * 1024
    if len(data) > limit:
        raise APIError(
            f"`{param}` is too large ({len(data) / 1048576:.1f} MB); limit is {cap} MB.",
            param=param,
        )
