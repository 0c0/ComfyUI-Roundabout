"""FastVideo FastH3 的 SelfLift 渐进放大版（`fastvideo-fasth3-self-lift` / `-edit`）专项测试。

背景：这两支是在 `fasth3` / `fasth3-edit` 的基础上把单阶段采样换成 SelfLift
（低分前缀 → 学习式 3D conv upscaler 抬分辨率 → 高分收尾）。改造本身只有四步，
但有三处**必须锁死**的东西：

  1. 采样器只能是 `euler` —— `SelfLiftH3Sampler` 在 `_validate_sampling` 里硬校验，
     传 `res_multistep` 会在采样阶段直接 ValueError。
  2. `negative` 必须有输入 —— fasth3 原来是单条件（BasicGuider），SelfLift 要
     positive + negative 两个，所以补了 `ConditioningZeroOut`。
  3. σ 标定三元组（extra_steps / start_at_sigma / transition_step）与 steps 耦合。
     fasth3 链上有 `MiniMaxH3SigmaShift(shift_video=10)`，σ 网格被挤向高噪端，
     照抄 self-lift 的 1/0.7/5 会让 σ_resume 落在 0.77 —— 等于让高分阶段去干低分的粗活。

验证点：
  [1] 工作流文件：API 格式、骨架齐全、无悬空连线 / 不可达孤儿
  [2] SelfLift 链路：euler 硬门槛、采样三件套已被取代、数据流改接
  [3] σ 标定：复刻 shift + refiner 算出 σ_resume，锁「高分区 ≥2 步」与目标区间
  [4] references：t2v 的 image_keys 覆盖 / edit 的 6 图 + 3 视频 + 3 音频
  [5] 参数注入落点（含 seed 迁移、transition_step、highres_tiling、保存前缀）
  [6] transition_step 范围校验：上界必须是 steps + extra_steps - 1（曾经的 bug）
  [7] 参考槽裁剪：0/1/2 图、部分与满配
  [8] 原版 fasth3 / fasth3-edit 未受影响

自测：python tests/test_fasth3_self_lift.py
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent  # <ComfyUI>
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.disable(logging.CRITICAL)

WF_T2V = "video_fastvideo_fasth3_self_lift.json"
WF_EDIT = "video_fastvideo_fasth3_self_lift_edit.json"
MODEL = "fastvideo-fasth3-self-lift"
MODEL_EDIT = "fastvideo-fasth3-self-lift-edit"

PREFIX = "video/FastH3_Lift"
BASE_T2V = "video_fastvideo_fasth3.json"
BASE_EDIT = "video_fastvideo_fasth3_edit.json"

FRAME_IDS = ["105:200", "105:201"]
IMG_IDS = ["105:200", "105:201", "105:202", "105:203", "105:204", "105:205"]
VID_IDS = ["105:206", "105:207", "105:208"]
VCOMP_IDS = ["105:209", "105:210", "105:211"]
AUD_IDS = ["105:212", "105:213", "105:214"]

# σ 标定目标：σ_resume 落在 [0.25, 0.35]（self-lift 参考档为 0.2303），高分区至少 2 步
SIGMA_RESUME_LO, SIGMA_RESUME_HI = 0.25, 0.35
MIN_HIGHRES_NFE = 2

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def dangling(wf: dict) -> list[str]:
    return [
        f"{nid}.{k} -> {v[0]}"
        for nid, node in wf.items()
        for k, v in (node.get("inputs") or {}).items()
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in wf
    ]


def reachable(wf: dict, start: str) -> set[str]:
    seen: set[str] = set()
    stack = [str(start)]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in wf:
            continue
        seen.add(nid)
        for val in (wf[nid].get("inputs") or {}).values():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                stack.append(val[0])
    return seen


# ---- σ 网格复刻（与 h3_sigma_refiner.py + MiniMaxH3SigmaShift 逐步对齐）--------
def _shift_sigma(s: float, shift: float = 10.0) -> float:
    return shift * s / (1 + (shift - 1) * s)


def _refine(sig: list[float], extra: int, start_at: float,
            end_at: float = 0.0, spacing: str = "cosine") -> list[float]:
    """复刻 H3SigmaRefiner.refine_sigmas（点数 = steps + 1 + extra）。"""
    if extra <= 0:
        return list(sig)
    idx = -1
    for i, s in enumerate(sig):
        if s <= start_at:
            idx = i
            break
    if idx == -1 or idx >= len(sig) - 1:
        return list(sig)
    head = sig[:idx]
    A, B = sig[idx], max(end_at, sig[-1])
    n = len(sig) - idx + extra
    t = [i / (n - 1) for i in range(n)]
    if spacing == "cosine":
        factor = [(1 - math.cos(x * math.pi)) / 2 for x in t]
    elif spacing == "exponential":
        alpha = 3.0
        factor = [(math.exp(x * alpha) - 1) / (math.exp(alpha) - 1) for x in t]
    else:
        factor = t
    tail = [A + (B - A) * f for f in factor]
    if sig[-1] == 0.0 and B > 0.0:
        tail.append(0.0)
    return head + tail


def calibrate(steps: int, extra: int, start_at: float, shift: float = 10.0,
              scheduler: str = "simple") -> list[float]:
    """BasicScheduler(simple) → SigmaShift → H3SigmaRefiner 的最终 σ 网格。"""
    raw = [1.0 - i / steps for i in range(steps + 1)]
    shifted = [_shift_sigma(s, shift) for s in raw]
    return _refine(shifted, extra, start_at)


def main() -> int:  # noqa: C901
    from gateway.errors import APIError
    from gateway import pipeline, vram
    from gateway.registry import Registry, build_workflow
    from gateway.schemas import VideoGenerationRequest

    saved_env = os.environ.get(vram.ENV_KEY)
    t2v = json.loads((HERE / "workflows" / WF_T2V).read_text(encoding="utf-8"))
    edit = json.loads((HERE / "workflows" / WF_EDIT).read_text(encoding="utf-8"))
    base_t2v = json.loads((HERE / "workflows" / BASE_T2V).read_text(encoding="utf-8"))
    base_edit = json.loads((HERE / "workflows" / BASE_EDIT).read_text(encoding="utf-8"))

    reg = Registry()
    reg.load(HERE / "models.yaml", HERE / "workflows", "z-image")
    spec, es = reg.resolve(MODEL), reg.resolve(MODEL_EDIT)

    print("[1] 工作流文件与骨架")
    for label, wf in ((MODEL, t2v), (MODEL_EDIT, edit)):
        check(f"{label}: API 格式（无 nodes 数组）",
              "nodes" not in wf and all(isinstance(v, dict) and "class_type" in v for v in wf.values()))
        for nid in ("92", "105:6", "105:9", "105:10", "105:11", "105:13", "105:17",
                    "105:23", "105:24", "105:91", "105:107", "105:111", "105:127",
                    "105:128", "300", "301", "302"):
            if nid not in wf:
                check(f"{label}: 骨架节点 {nid} 存在", False)
                break
        else:
            check(f"{label}: 骨架节点齐全（含 SelfLift 三节点）", True)
        check(f"{label}: 无不可达孤儿", not (set(wf) - reachable(wf, "92")),
              sorted(set(wf) - reachable(wf, "92")))
        check(f"{label}: 无悬空连线", not dangling(wf), "; ".join(dangling(wf)))

    print("\n[2] SelfLift 链路")
    for label, wf, agg in ((MODEL, t2v, "105:104"), (MODEL_EDIT, edit, "105:145")):
        check(f"{label}: 采样器是 euler（SelfLift 硬校验）",
              wf["105:17"]["inputs"]["sampler_name"] == "euler",
              wf["105:17"]["inputs"]["sampler_name"])
        check(f"{label}: RandomNoise/BasicGuider/SamplerCustomAdvanced 已被取代",
              not any(k in wf for k in ("105:14", "105:15", "105:16")),
              [k for k in ("105:14", "105:15", "105:16") if k in wf])
        s300 = wf["300"]["inputs"]
        check(f"{label}: 300 是 H3SigmaRefiner 且 sigmas <- 105:9",
              wf["300"]["class_type"] == "H3SigmaRefiner" and s300["sigmas"] == ["105:9", 0], s300)
        check(f"{label}: 301 零化条件取自聚合节点[0]",
              wf["301"]["class_type"] == "ConditioningZeroOut"
              and wf["301"]["inputs"]["conditioning"] == [agg, 0])
        s302 = wf["302"]["inputs"]
        check(f"{label}: 302 positive <- 聚合[0] / negative <- 301 / latent <- 聚合[1]",
              s302["positive"] == [agg, 0] and s302["negative"] == ["301", 0]
              and s302["latent_image"] == [agg, 1],
              f"{s302['positive']} / {s302['negative']} / {s302['latent_image']}")
        check(f"{label}: 302 model/vae/sampler/sigmas 接线",
              s302["model"] == ["105:127", 0] and s302["vae"] == ["105:11", 0]
              and s302["sampler"] == ["105:17", 0] and s302["sigmas"] == ["300", 0])
        moved = [n for n, nd in wf.items()
                 if (nd.get("inputs") or {}).get("samples") == ["302", 0]]
        check(f"{label}: 视频 + 音频解码都改接 SelfLift 输出",
              sorted(moved) == ["105:10", "105:23"], moved)
        check(f"{label}: upscaler 权重非空", bool(s302.get("upscaler_model")), s302.get("upscaler_model"))

    print("\n[3] σ 标定（shift=10 下的过渡点必须重算）")
    cfg = spec.defaults
    steps, extra, start = cfg["steps"], cfg["extra_steps"], cfg["start_at_sigma"]
    ts = cfg["transition_step"]
    grid = calibrate(steps, extra, start)
    nfe = len(grid) - 1
    check(f"网格点数 = steps + extra + 1 = {steps + extra + 1}", len(grid) == steps + extra + 1,
          len(grid))
    check(f"总 NFE = steps + extra = {steps + extra}", nfe == steps + extra, nfe)
    check(f"transition_step 在合法区间（1 <= {ts} <= NFE-1 = {nfe - 1}）", 1 <= ts <= nfe - 1)
    sigma_resume = grid[ts]
    check(f"σ_resume={sigma_resume:.4f} 落在 [{SIGMA_RESUME_LO}, {SIGMA_RESUME_HI}]",
          SIGMA_RESUME_LO <= sigma_resume <= SIGMA_RESUME_HI, f"{sigma_resume:.4f}")
    check(f"高分阶段 ≥ {MIN_HIGHRES_NFE} 步（只给 1 步会明显发灰）",
          nfe - ts >= MIN_HIGHRES_NFE, f"high_nfe={nfe - ts}")
    # 反例：照抄 self-lift 的 5 会把 σ_resume 顶到 ~0.77，高分阶段去干低分的粗活
    naive = calibrate(steps, extra, start)
    check("反例：照抄 self-lift 的 transition_step=5 会让 σ_resume 明显偏高（>0.6）",
          naive[5] > 0.6, f"σ_resume(ts=5)={naive[5]:.4f}")

    print("\n[4] references 声明 ↔ 工作流接线")
    ins = t2v["105:104"]["inputs"]
    check("t2v: image_keys 覆盖为 first_frame / last_frame",
          ins.get("first_frame") == ["105:200", 0] and ins.get("last_frame") == ["105:201", 0],
          f"{ins.get('first_frame')} / {ins.get('last_frame')}")
    check("t2v: aggregator 上不存在 ref_images.* 键",
          not any(k.startswith("ref_images.") for k in ins))
    eins = edit["105:145"]["inputs"]
    check("edit: 图/视频/音轨/音频四类槽齐备（6+3+3+3）",
          all(f"ref_images.ref_image_{i}" in eins for i in range(6))
          and all(f"ref_videos.ref_video_{i}" in eins for i in range(3))
          and all(f"ref_video_audios.ref_video_audio_{i}" in eins for i in range(3))
          and all(f"ref_audios.ref_audio_{i}" in eins for i in range(3)))
    check("edit: 参考视频与音轨同源（GetVideoComponents 的 0/1 输出）",
          all(eins[f"ref_videos.ref_video_{i}"] == [VCOMP_IDS[i], 0]
              and eins[f"ref_video_audios.ref_video_audio_{i}"] == [VCOMP_IDS[i], 1]
              for i in range(3)))
    check("edit: references 声明与接线一致",
          [str(x) for x in spec.references["images"]] == FRAME_IDS
          and [str(x) for x in es.references["images"]] == IMG_IDS
          and [str(x) for x in es.references["videos"]] == VID_IDS
          and [str(x) for x in es.references["audios"]] == AUD_IDS)
    check("两支 output_node 都是 92 且 references.aggregator 正确",
          str(spec.output_node) == "92" and str(es.output_node) == "92"
          and str(spec.references["aggregator"]) == "105:104"
          and str(es.references["aggregator"]) == "105:145")

    print("\n[5] 参数注入落点")
    values = pipeline.build_video_values(
        VideoGenerationRequest(prompt="p", seed=4242, steps=8, size="480p-16:9",
                               duration=4, fps=30, transition_step=8), spec, "p", None)
    values["seed"] = 4242  # seed 不走 build_video_values（由 pipeline 在尺寸解析之后单独落盘）
    built = build_workflow(spec, values)
    check("prompt -> 105:104", built["105:104"]["inputs"]["prompt"] == "p")
    check("seed -> 302.inputs.seed（原 RandomNoise 已删，种子跟着搬）",
          built["302"]["inputs"]["seed"] == 4242, built["302"]["inputs"]["seed"])
    check("steps -> 105:9", built["105:9"]["inputs"]["steps"] == 8)
    check("transition_step -> 302.inputs.transition_step",
          built["302"]["inputs"]["transition_step"] == 8, built["302"]["inputs"]["transition_step"])
    check("size -> 105:104 width/height（480p-16:9 = 848x480）",
          (built["105:104"]["inputs"]["width"], built["105:104"]["inputs"]["height"]) == (848, 480),
          (built["105:104"]["inputs"]["width"], built["105:104"]["inputs"]["height"]))
    check("duration -> 105:111", built["105:111"]["inputs"]["value"] == 4)
    check("fps -> 105:91", built["105:91"]["inputs"]["fps"] == 30)
    check("不传前缀时保留模板默认 video/FastH3_Lift",
          built["92"]["inputs"]["filename_prefix"] == PREFIX, built["92"]["inputs"]["filename_prefix"])
    check("extra_steps / start_at_sigma 是模板固定值（请求改不动）",
          built["300"]["inputs"]["extra_steps"] == extra
          and built["300"]["inputs"]["start_at_sigma"] == start,
          (built["300"]["inputs"]["extra_steps"], built["300"]["inputs"]["start_at_sigma"]))
    check("highres_tiling 默认开启（8GB 档靠它压显存）",
          built["302"]["inputs"]["highres_tiling"] is True)
    ebuilt = build_workflow(es, pipeline.build_video_values(
        VideoGenerationRequest(prompt="p2"), es, "p2", None))
    check(f"{MODEL_EDIT}: prompt -> 105:145", ebuilt["105:145"]["inputs"]["prompt"] == "p2")
    check(f"{MODEL_EDIT}: 302 同样改接", ebuilt["302"]["inputs"]["positive"] == ["105:145", 0])

    print("\n[6] transition_step 范围校验（上界 = steps + extra_steps - 1）")
    # 这是本轮修的 bug：旧校验按 steps-1 算，extra_steps=2 时会误拦 ts=8
    def ts_ok(model_spec, st, t):
        try:
            pipeline.build_video_values(
                VideoGenerationRequest(prompt="p", steps=st, transition_step=t), model_spec, "p", None)
            return None
        except APIError as exc:
            return str(exc)

    err = ts_ok(spec, 8, 8)
    check("extra_steps=2 / steps=8 时 ts=8 被接受（旧实现会误拦）", err is None, err or "")
    err = ts_ok(spec, 8, 9)
    check("ts=9 也在合法区间内（上界 9）", err is None, err or "")
    err = ts_ok(spec, 8, 10)
    check("ts=10 超上界被拦下", err is not None and "extra_steps" in err, err or "(未报错)")
    try:
        VideoGenerationRequest(prompt="p", steps=8, transition_step=0)
        zero_rejected = False
    except Exception:
        zero_rejected = True
    check("ts=0 在请求模型层就被拒（schema ge=1，不落到 pipeline）", zero_rejected)
    # self-lift 的 defaults 未声明 extra_steps，上界回退为 steps-1，默认档仍在范围内
    lift = reg.resolve("minimax-h3-self-lift")
    err = ts_ok(lift, lift.defaults["steps"], lift.defaults["transition_step"])
    check("self-lift 默认档（6/5）仍通过（上界回退 steps-1 也不误伤）", err is None, err or "")
    base = reg.resolve("fasth3")
    err = ts_ok(base, 8, 8)
    check("原版 fasth3 未绑定 transition_step，传了也不校验（仍是 res_multistep 链路）",
          err is None, err or "")

    print("\n[7] 参考槽裁剪")
    for label, imgs, expect_dropped, first, last in (
        ("0 图（纯文生）", [], 2, None, None),
        ("1 图（首帧）", ["a.png"], 1, ["105:200", 0], None),
        ("2 图（首尾帧）", ["a.png", "b.png"], 0, ["105:200", 0], ["105:201", 0]),
    ):
        w = build_workflow(spec, pipeline.build_video_values(
            VideoGenerationRequest(prompt="p"), spec, "p", None))
        dropped = pipeline._prune_unused_references(
            w, spec, VideoGenerationRequest(prompt="p", reference_images=imgs or None))
        a = w["105:104"]["inputs"]
        check(f"t2v {label}: 删除 {expect_dropped} 节点", dropped == expect_dropped, f"got={dropped}")
        check(f"t2v {label}: 关键帧槽 {first} / {last}",
              a.get("first_frame") == first and a.get("last_frame") == last,
              f"{a.get('first_frame')} / {a.get('last_frame')}")
        check(f"t2v {label}: 无悬空 / 无孤儿（SelfLift 链未被剪坏）",
              not dangling(w) and not (set(w) - reachable(w, "92")) and "302" in w)

    for label, imgs, vids, auds, expect_dropped in (
        ("0/0/0", [], [], [], 15),
        ("1图/1视频/1音频", ["a.png"], ["v.mp4"], ["a.wav"], 11),
        ("满配 6/3/3", ["a.png"] * 6, ["v.mp4"] * 3, ["a.wav"] * 3, 0),
    ):
        w = build_workflow(es, pipeline.build_video_values(
            VideoGenerationRequest(prompt="p"), es, "p", None))
        dropped = pipeline._prune_unused_references(
            w, es, VideoGenerationRequest(prompt="p", reference_images=imgs or None,
                                          reference_videos=vids or None, reference_audios=auds or None))
        left = [k for k in w["105:145"]["inputs"]
                if k.startswith("ref_") and k != "ref_image_size"]
        check(f"edit {label}: 删除 {expect_dropped} 节点", dropped == expect_dropped, f"got={dropped}")
        # 每条参考视频额外带一条同源音轨（ref_video_audios.*），所以视频按两份计
        expect_keys = len(imgs) + len(vids) * 2 + len(auds)
        check(f"edit {label}: 参考键 {expect_keys} 个", len(left) == expect_keys, f"got={len(left)}")
        check(f"edit {label}: aggregator 保留 + SelfLift 链完整",
              "105:145" in w and "302" in w and not dangling(w)
              and not (set(w) - reachable(w, "92")))

    print("\n[8] 原版 fasth3 未受影响")
    check("fasth3 仍是 res_multistep 单阶段",
          base_t2v["105:17"]["inputs"]["sampler_name"] == "res_multistep"
          and "105:14" in base_t2v and "302" not in base_t2v)
    check("fasth3-edit 仍未接 SelfLift 链",
          base_edit["105:17"]["inputs"]["sampler_name"] == "res_multistep"
          and "302" not in base_edit)
    check("原版前缀仍是 video/FastH3",
          base_t2v["92"]["inputs"]["filename_prefix"] == "video/FastH3"
          and base_edit["92"]["inputs"]["filename_prefix"] == "video/FastH3")
    check("两支新模型都已注册且别名可解析",
          reg.resolve("fastvideo-fasth3-self-lift").name == MODEL
          and reg.resolve("fastvideo-fasth3-self-lift-edit").name == MODEL_EDIT
          and reg.resolve("fasth3-self-lift").name == MODEL
          and reg.resolve("fasth3-lift-edit").name == MODEL_EDIT)

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
