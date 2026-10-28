# -*- coding: utf-8 -*-
"""H3 turbo LoRA 的 shift 落点护栏（162 MiniMaxH3SigmaShift）。

为什么需要这个测试：
    蒸馏 LoRA 与基模的噪声调度不同 —— lightx2v 的 768p 版是 video shift 6 / audio 3
    训练出来的，基模 20 步是 12 / 3。模板没挂 `MiniMaxH3SigmaShift` 时，跑的是模型内建
    默认 12/3，于是「换了 768p LoRA 但 shift 没跟」会静默跑错调度：8 步还能看，4 步几乎必坏，
    而且不报任何错。这类错误靠肉眼很难归因到 shift。

    锁四件事：
      1. 四支视频模板都有 162，类是 MiniMaxH3SigmaShift，字面值 12 / 3（= 旧行为，零回归）；
      2. 162 插在 160(LoRA) 之后：所有原先吃 160 的下游都改指 162，且没有残留直连 160；
      3. 四支的 turbo LoRA 都是 768p 训练版（v1.1），与「shift 6」这条配方对应；
      4. registry 声明 video_shift 并绑到 162，显式传值能落到提交图。

    纯 JSON + registry 断言，不碰运行中的 ComfyUI、不联网、不落产物。

    python tests/test_h3_turbo_shift.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import vram  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.registry import Registry, build_workflow  # noqa: E402

NODE = Path(__file__).resolve().parents[1]
WF = NODE / "workflows"

VIDEO = {
    "minimax-h3": "video_minimax_h3.json",
    "minimax-h3-edit": "video_minimax_h3_edit.json",
    "minimax-h3-lift": "video_minimax_h3_lift.json",
    "minimax-h3-lift-edit": "video_minimax_h3_lift_edit.json",
}

passed = failed = 0


def check(name: str, cond, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


def load_registry(pinned_gb: str | None = None) -> Registry:
    if pinned_gb is None:
        os.environ.pop(vram.ENV_KEY, None)
    else:
        os.environ[vram.ENV_KEY] = pinned_gb
    vram.reset_cache()
    r = Registry()
    r.load(settings.models_file, settings.workflows_dir, settings.default_model, settings.workflows_local_dir)
    return r


def main() -> int:
    # ---------------------------------------------------------------- 1. 节点与字面值
    print("1. 四支都有 162 且字面值是基模值（12 / 3）")
    for model, fname in VIDEO.items():
        wf = json.loads((WF / fname).read_text(encoding="utf-8"))
        node = wf.get("162") or {}
        ins = node.get("inputs") or {}
        check(
            f"{fname}: 162 是 MiniMaxH3SigmaShift",
            node.get("class_type") == "MiniMaxH3SigmaShift",
            str(node.get("class_type")),
        )
        check(
            f"{fname}: 字面值 12 / 3（等于基模默认，不改变未开 turbo 的行为）",
            ins.get("shift_video") == 12.0 and ins.get("shift_audio") == 3.0,
            str({k: ins.get(k) for k in ("shift_video", "shift_audio")}),
        )

    # ---------------------------------------------------------------- 2. 插入位置
    print("2. 162 插在 LoRA(160) 之后，下游全部改指")
    for model, fname in VIDEO.items():
        wf = json.loads((WF / fname).read_text(encoding="utf-8"))
        check(f"{fname}: 162.model <- 160", wf["162"]["inputs"].get("model") == ["160", 0],
              str(wf["162"]["inputs"].get("model")))
        direct = [
            nid for nid, n in wf.items()
            if nid not in ("162",) and n.get("inputs", {}).get("model") == ["160", 0]
        ]
        check(f"{fname}: 没有节点绕过 162 直连 160", not direct, str(direct))
        echo = [
            nid for nid, n in wf.items()
            if nid != "162" and n.get("inputs", {}).get("model") == ["162", 0]
        ]
        check(f"{fname}: 有下游消费 162", bool(echo), str(echo))

    # ---------------------------------------------------------------- 3. LoRA 版本对齐
    print("3. turbo LoRA 是 768p 训练版（与 shift 6 配方对应）")
    for model, fname in VIDEO.items():
        wf = json.loads((WF / fname).read_text(encoding="utf-8"))
        name = wf["160"]["inputs"].get("lora_name", "")
        check(f"{fname}: LoRA 为 8step v1.1 768p", "8step_v1.1_768p" in name, name)
        check(f"{fname}: LoRA 是 comfyui 转换版", "comfyui" in name, name)

    # ---------------------------------------------------------------- 4. 声明与注入
    print("4. registry 声明 video_shift 且能落到 162")
    r = load_registry("8")
    for model in VIDEO:
        spec = r.resolve(model)
        check(f"{model}: 声明 video_shift",
              "video_shift" in spec.param_specs, str(list(spec.param_specs)))
        check(f"{model}: 绑到 162.inputs.shift_video",
              spec.bindings.get("video_shift") == ["162.inputs.shift_video"],
              str(spec.bindings.get("video_shift")))
        check(f"{model}: 默认 12（未开 turbo 时的基模值）",
              spec.defaults.get("video_shift") == 12.0,
              str(spec.defaults.get("video_shift")))
        out = build_workflow(spec, {"video_shift": 6.0})
        check(f"{model}: 显式 6 能落到提交图",
              out["162"]["inputs"]["shift_video"] == 6.0,
              str(out["162"]["inputs"]))

    # turbo 默认开的两支（SelfLift 配方）必须真的会走 6
    for model in ("minimax-h3-lift", "minimax-h3-lift-edit"):
        spec = r.resolve(model)
        check(f"{model}: turbo 默认 1.0（配方前提）", spec.defaults.get("turbo_lora") == 1.0,
              str(spec.defaults.get("turbo_lora")))

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
