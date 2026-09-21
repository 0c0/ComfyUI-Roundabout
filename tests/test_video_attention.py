"""注意力档位（`attention: sparse|dense`）测试。

背景：算法侧实测 sol-attn 稀疏耗时 0.76~0.79x，画质与致密**高度一致**、差异只在高频细节
（同 seed MAE 20.2 / SSIM 0.615，约为换种子噪声的 1/3）。于是把它做成用户可选的
「更快 ↔ 更高质量」档，让调用方自己拿捏。

适用范围**只有 base 四支**（`minimax-h3` / `-edit` / `-lift` / `-lift-edit`，跑 training-free
的 sol-attn）。FastH3 两支**不参与**：它的稀疏是 `vsa`，与蒸馏权重配对训练，关掉不是
「更高画质」而是脱离训练分布 —— 该档只求快，恒定稀疏就是它的最优形态，传 `attention` 报 400。

机理：`BlockSparseAttention` 没有 dense 选项，但它的 `start_percent` 就是开关 ——
`apply_block_sparse_attention` 把它过 `percent_to_sigma()`，而所有实现里
`percent >= 1.0 → 0.0`；`SparseAttnPatch.dense_reason()` 判 `sigma > sigma_start` 即走 dense
⇒ `start_percent = 1.0` 等效全程关闭稀疏。

本测试覆盖四层：
  1. 档位 → 数值的映射（含大小写、非法值）
  2. base 四支的 binding 在、且路径真落到 `BlockSparseAttention.start_percent`
  3. 端到端：请求 → build_video_values → build_workflow，检查提交图里那个字段的真值
  4. FastH3 两支必须**显式拒绝**（且摘档位不改变它的实际行为）、图片档同样拒绝

纯逻辑 + 真实 models.yaml 加载：不起监听、不碰正在运行的 ComfyUI。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# 从本文件位置反推目录：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/tests/test_video_attention.py
NODE = Path(__file__).resolve().parent.parent   # 节点目录
ROOT = NODE.parent.parent                       # ComfyUI 根目录
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 只给包挂 __path__、不执行 gateway/__init__.py：避免 import 期就去建 registry 单例。
_pkg = types.ModuleType("rb_gateway")
_pkg.__path__ = [str(NODE / "gateway")]
sys.modules["rb_gateway"] = _pkg

from rb_gateway.errors import APIError  # noqa: E402
from rb_gateway.params import ATTENTION_SPARSE_START, resolve_attention  # noqa: E402
from rb_gateway.pipeline import build_video_values  # noqa: E402
from rb_gateway.registry import build_workflow, registry  # noqa: E402
from rb_gateway.schemas import VideoGenerationRequest  # noqa: E402

# 有档位的四支 → 稀疏节点所在路径
SUPPORTED = {
    "minimax-h3": "159",
    "minimax-h3-edit": "159",
    "minimax-h3-lift": "159",
    "minimax-h3-lift-edit": "159",
}
# 恒稀疏的两支（FastH3）：稀疏模式与蒸馏权重配对，不提供档位
FASTH3 = {
    "fasth3": "105:127",
    "fasth3-edit": "105:127",
}
# 工作流模板里 BlockSparseAttention 的 start_percent 字面值（= sparse 档）
TEMPLATE_SPARSE_START = 0.2

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def _err(attention: str, model: str = "minimax-h3") -> str:
    """跑一遍请求构造，返回 APIError 文案；没报错则返回空串。"""
    try:
        _values(attention, model=model)
    except APIError as exc:
        return str(exc)
    return ""


def _values(attention: str | None, model: str = "minimax-h3"):
    spec = registry.resolve(model)
    req = VideoGenerationRequest(prompt="p", model=model, duration=5, attention=attention)
    return spec, build_video_values(req, spec, "p", None)


def main() -> int:
    print("=== [1] 档位 → 数值映射 ===")
    check("档位表恰好 {sparse, dense}", set(ATTENTION_SPARSE_START) == {"sparse", "dense"},
          f"got={sorted(ATTENTION_SPARSE_START)}")
    check("sparse -> 模板默认 0.2", resolve_attention("sparse") == TEMPLATE_SPARSE_START)
    check("dense -> 1.0（等效关闭稀疏）", resolve_attention("dense") == 1.0)
    check("大小写 / 首尾空白不敏感", resolve_attention("  Dense ") == 1.0)
    # 直接调 resolve_attention：`_err` 要经过 registry，而本段还没 load，
    # 那样只会命中「模型不存在」分支 —— 看着有报错、其实没测到非法档位值。
    try:
        resolve_attention("turbo")
        invalid_msg = ""
    except APIError as exc:
        invalid_msg = str(exc)
    check("非法值报错而不是静默回落", "Invalid `attention`" in invalid_msg,
          f"got={invalid_msg!r}")

    print("\n=== [2] base 四支的 binding ===")
    registry.load(NODE / "models.yaml", NODE / "workflows", "minimax-h3")
    for name, node in SUPPORTED.items():
        spec = registry.resolve(name)
        paths = spec.bindings.get("sparse_start_percent") or []
        want = f"{node}.inputs.start_percent"
        check(f"{name} 绑到 {want}", paths == [want], f"got={paths}")
    video_names = {n for n in registry.names() if registry.resolve(n).is_video}
    check("视频档集合 = base 四支 + FastH3 两支",
          video_names == set(SUPPORTED) | set(FASTH3), f"got={sorted(video_names)}")

    print("\n=== [3] 端到端：请求 → 提交图（base）===")
    spec, values = _values("dense")
    check("dense 翻译成内部键 sparse_start_percent=1.0",
          values.get("sparse_start_percent") == 1.0, f"got={values.get('sparse_start_percent')!r}")
    wf = build_workflow(spec, values)
    check("dense 真的落到 159.inputs.start_percent",
          wf["159"]["inputs"]["start_percent"] == 1.0,
          f"got={wf['159']['inputs']['start_percent']!r}")

    spec, values = _values("sparse")
    wf = build_workflow(spec, values)
    check("sparse 显式注入 0.2", wf["159"]["inputs"]["start_percent"] == TEMPLATE_SPARSE_START,
          f"got={wf['159']['inputs']['start_percent']!r}")

    spec, values = _values(None)
    check("不传 attention 时不注入内部键",
          "sparse_start_percent" not in values, f"got={values.get('sparse_start_percent')!r}")
    wf = build_workflow(spec, values)
    check("不传 attention 时保持模板字面值（零回归）",
          wf["159"]["inputs"]["start_percent"] == TEMPLATE_SPARSE_START,
          f"got={wf['159']['inputs']['start_percent']!r}")

    print("\n=== [4] FastH3 两支：恒稀疏，不提供档位 ===")
    for name, node in FASTH3.items():
        spec = registry.resolve(name)
        check(f"{name} 没有 sparse_start_percent binding",
              not spec.bindings.get("sparse_start_percent"),
              f"got={spec.bindings.get('sparse_start_percent')!r}")
        # 稀疏是模板里定死的，摘掉档位后实际行为必须不变
        _, values = _values(None, model=name)
        wf = build_workflow(spec, values)
        got = wf[node]["inputs"]["start_percent"]
        check(f"{name} 模板仍是恒定稀疏 start_percent={TEMPLATE_SPARSE_START}（行为不变）",
              got == TEMPLATE_SPARSE_START, f"got={got!r}")
        for value in ("sparse", "dense"):
            msg = _err(value, model=name)
            check(f"{name} 传 attention={value} 必须报错（不静默忽略）", bool(msg),
                  f"got={msg!r}")
            check(f"{name} 报错文案说明「恒稀疏」",
                  "always runs sparse" in msg, f"got={msg!r}")

    print("\n=== [5] 非视频档：与稀疏无关，同样拒绝 ===")
    others = [n for n in registry.names() if not registry.resolve(n).is_video]
    if others:
        name = others[0]
        msg = _err("dense", model=name)
        check(f"图片档 {name} 传 attention 报错", bool(msg), f"got={msg!r}")
        check(f"图片档 {name} 报错文案是「无块稀疏阶段」",
              "no block-sparse attention" in msg, f"got={msg!r}")
        check(f"图片档 {name} 报错文案不是「恒稀疏」",
              "always runs sparse" not in msg, f"got={msg!r}")
    else:
        check("存在非视频档用于反例", False, "registry 里没有图片模型")

    total, ok = len(results), sum(results)
    print(f"\n===== {ok} passed / {total - ok} failed =====")
    return 1 if ok != total else 0


if __name__ == "__main__":
    sys.exit(main())
