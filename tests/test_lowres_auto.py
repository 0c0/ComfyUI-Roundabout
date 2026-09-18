"""`lowres_scale` 的 auto 口径回归测试。

`lowres_scale` 是 SelfLift 一采（低分前缀）的**相对**分辨率，质量却只跟低分的**绝对**
分辨率有关（低分长边越过 H3 原生画布 1344 就掉细节、并织出规则假网格）。所以 auto 的口径
写成「按目标尺寸反推」：`L = min(84 / 目标长边latent, 0.70)`，1080p→0.70、2K→0.525、4K→0.35。

本测试盯三件事：
1. 公式在实测点上复现（0.70 / 0.525 这两点都是真跑过的）
2. 越界显式值：能拦的拦（超节点边界）、该放行的放行（超原生只告警）
3. **注入闭环**：走真实组装路径 build_video_values → build_workflow，节点里拿到的是浮点数，
   而不是字符串 "auto"（后者会被 ComfyUI 的 FLOAT 输入框直接打回）

    python tests/test_lowres_auto.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.errors import APIError  # noqa: E402
from gateway.lowres import (  # noqa: E402
    AUTO_MAX_SCALE,
    NATIVE_LATENT,
    auto_lowres_scale,
    lowres_latent,
    resolve_lowres_scale,
)
from gateway.pipeline import build_video_values  # noqa: E402
from gateway.registry import Registry, build_workflow  # noqa: E402
from gateway.schemas import VideoGenerationRequest  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


registry = Registry()
registry.load(ROOT / "models.yaml", ROOT / "workflows", "z-image")

FAST = registry.resolve("fastvideo-fasth3-self-lift")
FAST_EDIT = registry.resolve("fastvideo-fasth3-self-lift-edit")
LIFT = registry.resolve("minimax-h3-self-lift")
LIFT_EDIT = registry.resolve("minimax-h3-self-lift-edit")
IMAGE = registry.resolve("z-image")


def values_for(spec=FAST, prompt="x", **kw):
    """走真实组装路径（含预设展开与显式入参覆盖）。"""
    return build_video_values(VideoGenerationRequest(prompt=prompt, **kw), spec, prompt, None)


# ============================================================================
print("== 1. 注册解析：四支 SelfLift 都能注入，默认档按实测覆盖度给 ==")
for spec, node in ((FAST, "302"), (FAST_EDIT, "302"), (LIFT, "235"), (LIFT_EDIT, "235")):
    check(f"{spec.name}: lowres_scale 绑定到 {node}",
          spec.bindings.get("lowres_scale") == [f"{node}.inputs.lowres_scale"],
          spec.bindings.get("lowres_scale"))
for spec in (FAST, FAST_EDIT, LIFT, LIFT_EDIT):
    check(f"{spec.name}: 默认 auto（实测过的四支）", spec.defaults.get("lowres_scale") == "auto",
          spec.defaults.get("lowres_scale"))
check("z-image 没有 lowres_scale 绑定", "lowres_scale" not in IMAGE.bindings)

print("\n== 2. 公式：在实测点上复现，且低分长边不越过原生画布 ==")
# 0.70 / 0.525 分别是「一采分辨率扫描」里 1080p 与 2K 真跑过的取值
for label, (w, h), expect in (
    ("1080p-16:9", (1920, 1088), 0.70),
    ("768p-16:9", (1344, 768), 0.70),
    ("480p-16:9", (854, 480), 0.70),
    ("2K", (2560, 1440), 0.525),
    ("4K", (3840, 2160), 0.35),
):
    got = auto_lowres_scale(w, h)
    check(f"auto @ {label} = {expect}", abs(got - expect) < 1e-9, got)

check("auto 上限 = 0.70（1080p 那个点）", AUTO_MAX_SCALE == 0.70, AUTO_MAX_SCALE)
check("1080p / 2K 的低分 latent 相同（同为原生画布）",
      lowres_latent(1920, 1088, auto_lowres_scale(1920, 1088))
      == lowres_latent(2560, 1440, auto_lowres_scale(2560, 1440)) == (48, 84),
      lowres_latent(2560, 1440, auto_lowres_scale(2560, 1440)))
check("2K 照搬 0.70 会超原生（这就是 auto 必须知道目标尺寸的原因）",
      max(lowres_latent(2560, 1440, 0.70)) > NATIVE_LATENT,
      lowres_latent(2560, 1440, 0.70))
check("尺寸未知时退回上限值", auto_lowres_scale(None, None) == AUTO_MAX_SCALE)

print("\n== 3. 解析：None 不注入 / auto 不分大小写 / 数字与数字串等价 / 越界报错 ==")
check("None -> None（沿用模板字面值）", resolve_lowres_scale(None, 1920, 1088) is None)
check('"auto" -> 0.70', resolve_lowres_scale("auto", 1920, 1088) == 0.70)
check('"AUTO " -> 0.70（去空格 + 大小写不敏感）', resolve_lowres_scale("AUTO ", 1920, 1088) == 0.70)
check('"" -> 0.70（等同 auto）', resolve_lowres_scale("", 2560, 1440) == 0.525)
check("0.4 -> 0.4（显式值原样透传）", resolve_lowres_scale(0.4, 1920, 1088) == 0.4)
check('"0.55" -> 0.55（字符串数字也认）', resolve_lowres_scale("0.55", 1920, 1088) == 0.55)
for bad, why in ((0.1, "低于节点下界 0.25"), (1.5, "高于节点上界 1.0"), ("x", "既不是数字也不是 auto"),
                 (True, "bool 不当成 1.0 用"), (object(), "完全非法")):
    try:
        resolve_lowres_scale(bad, 1920, 1088)
        check(f"越界/非法值 {bad!r} 被拦下（{why}）", False, "没有抛错")
    except APIError as exc:
        check(f"越界/非法值 {bad!r} 被拦下（{why}）", exc.param == "lowres_scale", exc)

print("\n== 4. 注入闭环：fasth3 self-lift 默认 auto -> 节点收到浮点数 ==")
for label, size, expect in (("默认尺寸 1344x768", None, 0.70), ("1080p-16:9", "1080p-16:9", 0.70),
                            ("2K 显式 WxH", "2560x1440", 0.525)):
    for spec in (FAST, FAST_EDIT):
        v = values_for(spec, size=size) if size else values_for(spec)
        check(f"{spec.name} @ {label}: values = {expect}", v.get("lowres_scale") == expect,
              v.get("lowres_scale"))
        wf = build_workflow(spec, v)
        got = wf["302"]["inputs"]["lowres_scale"]
        check(f"{spec.name} @ {label}: 节点 = {expect} 且是 float",
              isinstance(got, float) and abs(got - expect) < 1e-9, f"{got!r} ({type(got).__name__})")

print("\n== 5. 显式覆盖：请求优先于 auto 默认 ==")
v = values_for(FAST, lowres_scale=0.4)
check("lowres_scale=0.4 -> values 0.4", v.get("lowres_scale") == 0.4, v.get("lowres_scale"))
check("lowres_scale=0.4 -> 节点 0.4", build_workflow(FAST, v)["302"]["inputs"]["lowres_scale"] == 0.4)
v = values_for(FAST, size="2560x1440", lowres_scale="auto")
check("2K + auto -> 0.525（显式 auto 与默认走同一口径）", v.get("lowres_scale") == 0.525, v.get("lowres_scale"))
try:
    values_for(FAST, lowres_scale=2.0)
    check("lowres_scale=2.0 在组装阶段被拦下", False, "没有抛错")
except APIError as exc:
    check("lowres_scale=2.0 在组装阶段被拦下", exc.param == "lowres_scale", exc)

print("\n== 6. minimax-h3-self-lift*：默认 auto，显式值可覆盖 ==")
for spec, node in ((LIFT, "235"), (LIFT_EDIT, "235")):
    v = values_for(spec)
    check(f"{spec.name}: 默认 auto -> 0.70 @1344x768", v.get("lowres_scale") == 0.70, v.get("lowres_scale"))
    got = build_workflow(spec, v)[node]["inputs"]["lowres_scale"]
    check(f"{spec.name}: 节点收到 float 0.70", isinstance(got, float) and abs(got - 0.70) < 1e-9, got)
    v2 = values_for(spec, lowres_scale=0.4)
    check(f"{spec.name}: 显式 0.4 覆盖 auto", v2.get("lowres_scale") == 0.4, v2.get("lowres_scale"))

print("\n== 7. 安全网：直接调 build_workflow 也不会把字符串塞进节点 ==")
wf = build_workflow(FAST, {"prompt": "p"})
got = wf["302"]["inputs"]["lowres_scale"]
check("build_workflow 单独调用 -> float 0.70（不是 'auto'）",
      isinstance(got, float) and abs(got - 0.70) < 1e-9, f"{got!r} ({type(got).__name__})")
wf = build_workflow(FAST_EDIT, {"prompt": "p", "width": 2560, "height": 1440})
got = wf["302"]["inputs"]["lowres_scale"]
check("安全网按 width/height 反推 -> 0.525", isinstance(got, float) and abs(got - 0.525) < 1e-9,
      f"{got!r} ({type(got).__name__})")
check("非 SelfLift 模型不受影响",
      "lowres_scale" not in build_workflow(IMAGE, {"prompt": "p"}), "")

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
