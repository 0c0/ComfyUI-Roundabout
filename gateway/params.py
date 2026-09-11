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

MAX_SEED = 2**63 - 1
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
# 预设键换算成具体 width/height，所有维度对齐到 16 的倍数（扩散视频模型隐空间约束）。
#
# 约定：「p」指该档位的基准边——横向比例(16:9/4:3/1:1)取 height=档位，纵向比例(9:16/3:4)
# 取 width=档位。例如 480p-16:9 = 848×480，720p-9:16 = 720×1280。
VIDEO_RES_PRESETS: dict[int, dict[str, tuple[int, int]]] = {
    480: {
        "1:1": (480, 480),
        "4:3": (640, 480),
        "3:4": (480, 640),
        "16:9": (848, 480),
        "9:16": (480, 848),
    },
    720: {
        "1:1": (720, 720),
        "4:3": (960, 720),
        "3:4": (720, 960),
        "16:9": (1280, 720),
        "9:16": (720, 1280),
    },
}
_RES_TIERS = {480, 720}
_RES_RATIOS = {"1:1", "3:4", "4:3", "16:9", "9:16"}
_RES_PRESET_RE = re.compile(
    r"^(?P<tier>480|720)p[-_](?P<ratio>[0-9]+:[0-9]+)$"
    r"|^(?P<ratio2>[0-9]+:[0-9]+)[-_@](?P<tier2>480|720)p$"
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
                f"Tiers: 480p, 720p. Ratios: {', '.join(sorted(_RES_RATIOS))}.",
                param="size",
            )
        return table[ratio]

    # 回退：显式 WxH
    return parse_size(size, spec)


# ------------------------------------------------------------------ 种子
def resolve_seed(seed: int | None, index: int = 0) -> int:
    # 不传 / -1 → 随机（OpenAI 兼容语义：-1 视为"未指定"）；0 与正整数 → 固定种子
    if seed is None or seed < 0:
        return random.randint(0, MAX_SEED)
    # 批量生成时递增，保证 n>1 不返回 n 张一样的图
    return (int(seed) + index) % (MAX_SEED + 1)


# ------------------------------------------------------------------ 预设
def apply_presets(values: dict[str, Any], spec: ModelSpec, quality: str | None, style: str | None) -> dict[str, Any]:
    out = dict(values)

    if quality:
        preset = spec.quality_presets.get(quality) or spec.quality_presets.get(quality.lower())
        if preset is None and spec.quality_presets and quality.lower() not in {"auto", "standard"}:
            raise APIError(
                f"Unsupported `quality` {quality!r} for model `{spec.name}`. "
                f"Supported: {', '.join(spec.quality_presets)}.",
                param="quality",
            )
        for k, v in (preset or {}).items():
            out[k] = v  # 预设优先级低于显式入参，调用方在后面再覆盖

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
