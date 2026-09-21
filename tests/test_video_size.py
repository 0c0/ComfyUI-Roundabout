"""视频分辨率预设（`size`）的档位覆盖与解析测试。

档位：480p / 576p / 720p / 768p / 1080p / 1440p；比例：1:1 / 3:4 / 4:3 / 16:9 / 9:16。
铁律：预设里**每个维度都必须是 16 的倍数**（扩散视频模型隐空间约束）；
基准边本身不是 16 倍数时（1080）就近上取到 1088。

纯逻辑测试：不起监听、不依赖网络、不碰正在运行的 ComfyUI。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/tests/test_video_size.py
NODE = Path(__file__).resolve().parent.parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 只给包挂 __path__、不执行 gateway/__init__.py：避免 import 期就去建 registry 单例。
_pkg = types.ModuleType("rb_gateway")
_pkg.__path__ = [str(NODE / "gateway")]
sys.modules["rb_gateway"] = _pkg

from rb_gateway.errors import APIError  # noqa: E402
from rb_gateway.params import (  # noqa: E402
    VIDEO_RES_PRESETS,
    _RES_RATIOS,
    _RES_TIERS,
    resolve_video_size,
)

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def _base(tier: int) -> int:
    """该档位的基准边：本身是 16 倍数则原样，否则上取到最近的 16 倍数。"""
    return tier if tier % 16 == 0 else ((tier + 15) // 16) * 16


# 鸭子类型足够（resolve_video_size 只读 name / defaults / size_choices），
# 因此不必拉起 registry 单例。
SPEC = types.SimpleNamespace(name="test-video", defaults={"width": 1280, "height": 720},
                             size_choices=[])


def _expect_error(size: str, spec=SPEC) -> str:
    try:
        resolve_video_size(size, spec)
    except APIError as exc:
        return str(exc)
    return ""


def main() -> int:
    # ---- 1. 档位与比例覆盖 ----
    check("档位集合 = {480, 576, 720, 768, 1080, 1440}",
          _RES_TIERS == {480, 576, 720, 768, 1080, 1440}, f"got={sorted(_RES_TIERS)}")
    check("预设表键与档位一致", set(VIDEO_RES_PRESETS) == _RES_TIERS,
          f"got={sorted(VIDEO_RES_PRESETS)}")
    check("比例集合 = 1:1/3:4/4:3/16:9/9:16",
          _RES_RATIOS == {"1:1", "3:4", "4:3", "16:9", "9:16"},
          f"got={sorted(_RES_RATIOS)}")
    for tier in sorted(_RES_TIERS):
        check(f"{tier}p 档覆盖全部 5 种比例", set(VIDEO_RES_PRESETS[tier]) == _RES_RATIOS,
              f"got={sorted(VIDEO_RES_PRESETS[tier])}")

    # ---- 2. 所有维度对齐 16 的倍数 ----
    bad = [
        f"{tier}p-{ratio}={wh}"
        for tier, table in VIDEO_RES_PRESETS.items()
        for ratio, wh in table.items()
        for v in wh
        if v % 16
    ]
    check("预设里每个维度都是 16 的倍数", not bad, f"bad={bad}")

    # ---- 3. 基准边规则：横向比例看 height，纵向比例看 width，1:1 两者都是基准边 ----
    wrong = []
    for tier, table in VIDEO_RES_PRESETS.items():
        expect = _base(tier)
        for ratio, (w, h) in table.items():
            if ratio == "1:1":
                if (w, h) != (expect, expect):
                    wrong.append(f"{tier}p-1:1={w}x{h}")
            elif ratio in {"4:3", "16:9"}:
                if h != expect:
                    wrong.append(f"{tier}p-{ratio}={w}x{h}")
            else:  # 3:4 / 9:16
                if w != expect:
                    wrong.append(f"{tier}p-{ratio}={w}x{h}")
    check("基准边落位正确（横比例看 height，纵比例看 width）", not wrong, f"wrong={wrong}")

    # ---- 4. 解析：新增档位的关键取值 ----
    cases = {
        "480p-16:9": (848, 480),
        "576p-16:9": (1024, 576),
        "576p-1:1": (576, 576),
        "576p-9:16": (576, 1024),
        "720p-9:16": (720, 1280),
        "768p-16:9": (1360, 768),
        "768p-1:1": (768, 768),
        "768p-4:3": (1024, 768),
        "768p-3:4": (768, 1024),
        "1080p-16:9": (1920, 1088),
        "1080p-9:16": (1088, 1920),
        "1080p-1:1": (1088, 1088),
        "1080p-4:3": (1440, 1088),
        "1440p-16:9": (2560, 1440),
        "1440p-9:16": (1440, 2560),
        "1440p-1:1": (1440, 1440),
        "1440p-4:3": (1920, 1440),
        "1440p-3:4": (1440, 1920),
    }
    for size, want in cases.items():
        got = resolve_video_size(size, SPEC)
        check(f'resolve("{size}") == {want[0]}x{want[1]}', got == want, f"got={got}")

    # ---- 5. 反向写法与分隔符宽容 ----
    for size, want in {
        "9:16@1080p": (1088, 1920),
        "16:9@768p": (1360, 768),
        "720p_16:9": (1280, 720),
        "1:1-480p": (480, 480),
    }.items():
        got = resolve_video_size(size, SPEC)
        check(f'resolve("{size}") == {want[0]}x{want[1]}', got == want, f"got={got}")

    # ---- 6. 非法档位 / 比例 ----
    msg = _expect_error("2160p-16:9")
    check("未支持档位 2160p 报错", bool(msg), f"msg={msg[:60]!r}")
    check("错误提示列出全部六个档位",
          all(f"{t}p" in msg for t in (480, 576, 720, 768, 1080, 1440)), f"msg={msg!r}")
    check("未支持比例 720p-21:9 报错", bool(_expect_error("720p-21:9")))

    # ---- 7. 回退路径 ----
    check("auto 用模型默认", resolve_video_size("auto", SPEC) == (1280, 720))
    check("None 用模型默认", resolve_video_size(None, SPEC) == (1280, 720))
    check("显式 WxH 回退到 parse_size", resolve_video_size("1360x768", SPEC) == (1360, 768),
          f"got={resolve_video_size('1360x768', SPEC)}")
    check("WxH 按 8 倍数取整（1080 保留）",
          resolve_video_size("1920x1080", SPEC) == (1920, 1080),
          f"got={resolve_video_size('1920x1080', SPEC)}")

    # ---- 8. 模型自带的 sizes 白名单对 WxH 仍生效 ----
    strict = types.SimpleNamespace(name="strict", defaults={}, size_choices=["1280x720"])
    check("不在 size_choices 的 WxH 被拒", bool(_expect_error("1024x1024", strict)))
    check("在 size_choices 的 WxH 放行", resolve_video_size("1280x720", strict) == (1280, 720))

    print()
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
