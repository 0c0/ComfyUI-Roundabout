"""H3 SelfLift 质量档（`minimax-h3-self-lift-max` / `-self-lift-edit-max`）回归测试。

背景：这两支由加速版（`-self-lift` / `-self-lift-edit`）派生，派生手段就是「逐字复制骨架 +
定点替换」，所以真正需要盯住的是**改动范围没有被扩大**：只允许删掉 4step 加速 LoRA
（连带把 220.model 从 145 改接 148）、总步数 6 -> 30、过渡步 5 -> 25 三件事。骨架节点、
低显存分块、latent upscaler、SigmaRefiner、参考槽一律照抄 —— 一旦有人手改这两份 JSON，
本测试会立刻指出差异集合变了。

写法和 test_ref2va_self_lift.py 一致：断言分四组，直接走 gateway 的真实组装路径
（`build_video_values` + `build_workflow`），不在测试里自己拼 values。

    python tests/test_self_lift_max.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# (源文件, 派生文件, 模型名, 骨架里允许的差异)
PAIRS = [
    ("video_minimax_h3_self_lift.json", "video_minimax_h3_self_lift_max.json",
     "minimax-h3-self-lift-max"),
    ("video_minimax_h3_self_lift_edit.json", "video_minimax_h3_self_lift_edit_max.json",
     "minimax-h3-self-lift-edit-max"),
]

# 派生时允许出现的全部差异（其余一律视为越界改动）
ALLOWED_DIFF = {"/124/inputs/steps", "/220/inputs/model[0]", "/235/inputs/transition_step"}

MAX_STEPS = 30
MAX_TRANSITION = 25

# SelfLift 采样器自带的骨架参数：派生时应当一字不动
LIFT_KNOBS = ("lowres_scale", "rho", "w_min", "w_max", "upscaler_model", "highres_tiling",
              "seed", "cfg")

results: list[bool] = []


def check(name: str, ok: bool, extra: object = "") -> None:
    results.append(bool(ok))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def flat(o, p=""):
    """把嵌套 dict/list 摊平成 {路径: 标量}，用来做「差异恰好是这三处」的断言。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from flat(v, f"{p}/{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from flat(v, f"{p}[{i}]")
    else:
        yield p, o


def load(name: str) -> dict:
    return json.loads((HERE / "workflows" / name).read_text(encoding="utf-8"))


def main() -> int:  # noqa: C901
    from gateway.errors import APIError
    from gateway.pipeline import build_video_values
    from gateway.registry import Registry, build_workflow
    from gateway.schemas import VideoGenerationRequest
    from gateway import vram

    saved_env = os.environ.get(vram.ENV_KEY)

    print("[1] 工作流文件：骨架保留 + 差异范围受控")
    for src_name, dst_name, model in PAIRS:
        src, dst = load(src_name), load(dst_name)
        check(f"{dst_name}: API 格式",
              "nodes" not in dst and all(isinstance(v, dict) and "class_type" in v
                                        for v in dst.values()))
        check(f"{dst_name}: 加速 LoRA 节点 145 已移除", "145" not in dst)
        check(f"{dst_name}: 节点数 = 源 {len(src)} - 1", len(dst) == len(src) - 1, len(dst))
        check(f"{dst_name}: 无残留 LoraLoaderModelOnly",
              not [n for n, d in dst.items() if d.get("class_type") == "LoraLoaderModelOnly"])
        check(f"{dst_name}: 220.model 已改接 148（不再是 145）",
              dst["220"]["inputs"]["model"] == ["148", 0], dst["220"]["inputs"]["model"])
        check(f"{dst_name}: 148 仍接 UNET 127",
              dst["148"]["class_type"] == "ModelAttentionBackend"
              and dst["148"]["inputs"]["model"] == ["127", 0])
        check(f"{dst_name}: UNET 权重与源一致（未顺手换模型）",
              dst["127"]["inputs"] == src["127"]["inputs"], dst["127"]["inputs"])

        s_flat, d_flat = dict(flat(src)), dict(flat(dst))
        s_flat = {k: v for k, v in s_flat.items() if not k.startswith("/145")}
        diff = {k for k in set(s_flat) | set(d_flat) if s_flat.get(k) != d_flat.get(k)}
        check(f"{dst_name}: 与源文件的差异恰好 3 处（步数 / 过渡步 / 220 接线）",
              diff == ALLOWED_DIFF, sorted(diff))

        ctypes = {n: d["class_type"] for n, d in dst.items()}
        s_ctypes = {n: d["class_type"] for n, d in src.items() if n != "145"}
        check(f"{dst_name}: 节点类型全表与源一致", ctypes == s_ctypes)

    print("\n[2] 采样参数：仍是 SelfLift 那条链，只换了步数")
    for _, dst_name, _ in PAIRS:
        dst = load(dst_name)
        check(f"{dst_name}: KSamplerSelect 仍是 euler（SelfLift 只接受标准 euler）",
              dst["123"]["inputs"]["sampler_name"] == "euler", dst["123"]["inputs"])
        check(f"{dst_name}: 调度器 simple / denoise 1（与原 h3 工作流一致）",
              dst["124"]["inputs"]["scheduler"] == "simple" and dst["124"]["inputs"]["denoise"] == 1,
              dst["124"]["inputs"])
        check(f"{dst_name}: 124.steps == {MAX_STEPS}", dst["124"]["inputs"]["steps"] == MAX_STEPS,
              dst["124"]["inputs"]["steps"])
        check(f"{dst_name}: 235.transition_step == {MAX_TRANSITION}",
              dst["235"]["inputs"]["transition_step"] == MAX_TRANSITION,
              dst["235"]["inputs"]["transition_step"])
        check(f"{dst_name}: 满足 SelfLift 硬约束 1 <= transition_step <= steps-1",
              1 <= MAX_TRANSITION <= MAX_STEPS - 1)
        src = load(PAIRS[[p[1] for p in PAIRS].index(dst_name)][0])
        left = {k: v for k, v in dst["235"]["inputs"].items() if k not in ("transition_step",)}
        right = {k: v for k, v in src["235"]["inputs"].items() if k not in ("transition_step",)}
        check(f"{dst_name}: SelfLiftH3Sampler 其余旋钮逐字沿用源文件", left == right)
        check(f"{dst_name}: H3SigmaRefiner 参数逐字沿用",
              dst["147"]["inputs"] == src["147"]["inputs"], dst["147"]["inputs"])
        check(f"{dst_name}: 低显存分块接线仍是 219/220 链",
              dst["219"]["inputs"]["model"] == ["220", 0]
              and dst["220"]["inputs"]["model"] == ["148", 0])

    print("\n[3] 注册与组装（走真实路径，8 GiB 档）")
    os.environ[vram.ENV_KEY] = "8"
    vram.reset_cache()
    reg = Registry()
    reg.load(HERE / "models.yaml", HERE / "workflows", "z-image")

    for _, _, model in PAIRS:
        spec = reg.resolve(model)
        accel = reg.resolve(model.replace("-max", ""))
        check(f"{model}: 已注册", spec.name == model)
        check(f"{model}: 未声明 motion 档", not spec.motion_presets and not spec.defaults.get("motion"),
              spec.motion_presets)
        check(f"{model}: bindings 与加速版逐字一致", spec.bindings == accel.bindings)
        check(f"{model}: references 与加速版一致", spec.references == accel.references)
        check(f"{model}: vram_adaptive 与加速版一致",
              spec.vram_adaptive == accel.vram_adaptive and spec.vram_adaptive)
        check(f"{model}: output_node 仍是 SaveVideo 238", str(spec.output_node) == "238")

        values = build_video_values(VideoGenerationRequest(prompt="p"), spec, "p", None)
        wf = build_workflow(spec, values)
        check(f"{model}: 默认注入 124.steps={MAX_STEPS} / 235.transition_step={MAX_TRANSITION}",
              (wf["124"]["inputs"]["steps"], wf["235"]["inputs"]["transition_step"])
              == (MAX_STEPS, MAX_TRANSITION),
              (wf["124"]["inputs"]["steps"], wf["235"]["inputs"]["transition_step"]))
        check(f"{model}: 组装后仍无 LoRA 节点",
              not [n for n, d in wf.items() if d.get("class_type") == "LoraLoaderModelOnly"])
        v2 = build_video_values(VideoGenerationRequest(prompt="p", steps=20, transition_step=15),
                                spec, "p", None)
        w2 = build_workflow(spec, v2)
        check(f"{model}: 请求参数可覆盖（20 / 15）",
              (w2["124"]["inputs"]["steps"], w2["235"]["inputs"]["transition_step"]) == (20, 15),
              (w2["124"]["inputs"]["steps"], w2["235"]["inputs"]["transition_step"]))
        got = (wf["219"]["inputs"]["chunks"], wf["220"]["inputs"]["head_chunks"],
               wf["219"]["inputs"]["seq_threshold"], wf["235"]["inputs"]["highres_tiling"])
        check(f"{model}: 8 GiB 档位照旧注入（6 / 24 / 4096 / true）",
              got == (6, 24, 4096, True), got)
        try:
            build_video_values(VideoGenerationRequest(prompt="p", motion="story"), spec, "p", None)
            check(f"{model}: 传 motion 应当报错", False, "没有报错")
        except APIError as exc:
            check(f"{model}: 传 motion 报错并说明无档位", "no `motion` presets" in str(exc),
                  str(exc)[:90])
        try:
            build_video_values(VideoGenerationRequest(prompt="p", steps=20, transition_step=20),
                               spec, "p", None)
            check(f"{model}: transition_step == steps 应当报错", False, "没有报错")
        except APIError as exc:
            check(f"{model}: transition_step == steps 被拦下", "必须满足" in str(exc),
                  str(exc)[:90])

    print("\n[4] 模板字面值 == defaults（防「改了 yaml 忘了改模板」）")
    for _, _, model in PAIRS:
        spec = reg.resolve(model)
        check(f"{model}: defaults.steps {spec.defaults['steps']} == 模板 {spec.template['124']['inputs']['steps']}",
              spec.defaults["steps"] == spec.template["124"]["inputs"]["steps"])
        check(f"{model}: defaults.transition_step "
              f"{spec.defaults['transition_step']} == 模板 "
              f"{spec.template['235']['inputs']['transition_step']}",
              spec.defaults["transition_step"] == spec.template["235"]["inputs"]["transition_step"])
        check(f"{model}: defaults.steps 进 bindings（不是只写在 defaults 里）",
              "steps" in spec.bindings and "transition_step" in spec.bindings)

    if saved_env is not None:
        os.environ[vram.ENV_KEY] = saved_env
    else:
        os.environ.pop(vram.ENV_KEY, None)
    vram.reset_cache()

    print()
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
