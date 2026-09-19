"""FastVideo FastH3（`fasth3` / `fasth3-edit`）的接线、键名覆盖与参考槽裁剪测试。

背景：这两支工作流从 ComfyUI 画布导出时状态不完整 —— 音频解码槽误接了 video VAE、
video VAE 还是 fp16 旧档、CLIP 用的是需 Blackwell 的 nvfp4 变体，而 `fasth3-edit` 干脆
没有任何参考槽。更关键的是 `fasth3` 的聚合节点是 `MiniMaxH3ImageToVideo`：它的首尾帧
走**关键帧**槽 `first_frame` / `last_frame`，不是 Ref2VA 那套 `ref_images.ref_image_N`，
所以 models.yaml 用 `image_keys` 覆盖了聚合节点的输入键名（gateway/registry.py 的 ref_key）。

验证点：
  [1] 工作流文件：API 格式、骨架节点齐全、无悬空连线 / 不可达孤儿
  [2] 权重档位：video VAE=int8_convrot、audio VAE=fp32、CLIP 统一 int8、steps=8
  [3] references 声明 ↔ 实际接线（含 image_keys 逐槽覆盖、参考视频与音轨同源）
  [4] 参数注入落点（prompt / seed / steps / size / duration / fps / 保存前缀）
  [5] 参考槽裁剪：0 / 部分 / 满配，且首尾帧顺序 = 第 1 张首帧、第 2 张尾帧
  [6] 键名覆盖机制：默认值、逐槽覆盖、越界回落、长度不匹配要报错
  [7] 不影响同族其它模型

自测：python tests/test_fasth3.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent  # <ComfyUI>
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.disable(logging.CRITICAL)  # 静音被测模块的 normal 日志，只留断言输出

WF_T2V = "video_fastvideo_fasth3.json"
WF_EDIT = "video_fastvideo_fasth3_edit.json"
MODEL = "fasth3"
MODEL_EDIT = "fasth3-edit"

VIDEO_VAE = "minimax_h3_video_vae_int8_convrot.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
PREFIX = "video/FastH3"

# 骨架节点：两支共用（聚合节点 105:104 / 105:145 单独看）
SKELETON = ("92", "105:6", "105:9", "105:10", "105:11", "105:13", "105:14",
            "105:15", "105:16", "105:17", "105:23", "105:24", "105:91",
            "105:107", "105:111", "105:127", "105:128")
FRAME_IDS = ["105:200", "105:201"]
IMG_IDS = ["105:200", "105:201", "105:202", "105:203", "105:204", "105:205"]
VID_IDS = ["105:206", "105:207", "105:208"]
VCOMP_IDS = ["105:209", "105:210", "105:211"]
AUD_IDS = ["105:212", "105:213", "105:214"]

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


def main() -> int:  # noqa: C901
    from gateway.registry import (
        ModelSpec,
        Registry,
        _validate_references,
        build_workflow,
        ref_key,
    )
    from gateway import pipeline, vram
    from gateway.schemas import VideoGenerationRequest

    saved_env = os.environ.get(vram.ENV_KEY)
    t2v = json.loads((HERE / "workflows" / WF_T2V).read_text(encoding="utf-8"))
    edit = json.loads((HERE / "workflows" / WF_EDIT).read_text(encoding="utf-8"))

    print("[1] 工作流文件与骨架")
    for label, wf in (("fasth3", t2v), ("fasth3-edit", edit)):
        check(f"{label}: API 格式（无 nodes 数组）",
              "nodes" not in wf and all(isinstance(v, dict) and "class_type" in v
                                        for v in wf.values()))
        missing = [nid for nid in SKELETON if nid not in wf]
        check(f"{label}: 骨架节点齐全", not missing, f"missing={missing}")
        orphan = sorted(set(wf) - reachable(wf, "92"))
        check(f"{label}: 无不可达孤儿", not orphan, f"orphans={orphan}")
        check(f"{label}: 无悬空连线", not dangling(wf), "; ".join(dangling(wf)))
    check("fasth3 聚合节点是 MiniMaxH3ImageToVideo",
          t2v["105:104"]["class_type"] == "MiniMaxH3ImageToVideo")
    check("fasth3-edit 聚合节点是 MiniMaxH3ReferenceToVideo",
          edit["105:145"]["class_type"] == "MiniMaxH3ReferenceToVideo")
    check("output_node 92 在两支里都是 SaveVideo",
          t2v["92"]["class_type"] == "SaveVideo" and edit["92"]["class_type"] == "SaveVideo")

    print("\n[2] 权重档位（导出时的三处失配已修）")
    for label, wf in (("fasth3", t2v), ("fasth3-edit", edit)):
        check(f"{label}: video VAE = int8_convrot（不再是 fp16）",
              wf["105:11"]["inputs"]["vae_name"] == VIDEO_VAE,
              wf["105:11"]["inputs"]["vae_name"])
        # 音频解码槽曾被误接成 video VAE，改回 audio_vae_fp32
        check(f"{label}: audio VAE = {AUDIO_VAE}",
              wf["105:24"]["inputs"]["vae_name"] == AUDIO_VAE,
              wf["105:24"]["inputs"]["vae_name"])
        check(f"{label}: CLIP 统一 int8_convrot（不用需 Blackwell 的 nvfp4）",
              wf["105:13"]["inputs"]["clip_name"] == CLIP,
              wf["105:13"]["inputs"]["clip_name"])
        check(f"{label}: 模板 steps = 8", wf["105:9"]["inputs"]["steps"] == 8,
              wf["105:9"]["inputs"]["steps"])
    check("VAEDecodeAudio 用的是 audio VAE 那一支",
          t2v["105:23"]["inputs"]["vae"] == ["105:24", 0]
          and edit["105:23"]["inputs"]["vae"] == ["105:24", 0])
    check("聚合节点的 video/audio VAE 分别来自 105:11 / 105:24",
          t2v["105:104"]["inputs"]["vae"] == ["105:11", 0]
          and edit["105:145"]["inputs"]["vae"] == ["105:11", 0]
          and edit["105:145"]["inputs"]["audio_vae"] == ["105:24", 0])

    print("\n[3] references 声明 ↔ 工作流接线")
    ins = t2v["105:104"]["inputs"]
    check("fasth3: 首尾帧两槽存在且顺序 = first_frame / last_frame",
          ins.get("first_frame") == ["105:200", 0] and ins.get("last_frame") == ["105:201", 0],
          f"{ins.get('first_frame')} / {ins.get('last_frame')}")
    check("fasth3: 两个 LoadImage 挂的是 105:200 / 105:201",
          all(t2v[n]["class_type"] == "LoadImage" for n in FRAME_IDS))
    check("fasth3: aggregator 上不存在 ref_images.* 键（键名已覆盖）",
          not any(k.startswith("ref_images.") for k in ins))

    eins = edit["105:145"]["inputs"]
    check("fasth3-edit: 图槽 6 个", all(f"ref_images.ref_image_{i}" in eins for i in range(6)))
    check("fasth3-edit: 视频槽 3 个", all(f"ref_videos.ref_video_{i}" in eins for i in range(3)))
    check("fasth3-edit: 视频音轨槽 3 个",
          all(f"ref_video_audios.ref_video_audio_{i}" in eins for i in range(3)))
    check("fasth3-edit: 音频槽 3 个", all(f"ref_audios.ref_audio_{i}" in eins for i in range(3)))
    check("fasth3-edit: 参考键总数 = 15",
          len([k for k in eins if k.startswith("ref_") and k != "ref_image_size"]) == 15)
    check("fasth3-edit: 图槽按顺序指向 105:200..205",
          [eins[f"ref_images.ref_image_{i}"][0] for i in range(6)] == IMG_IDS)
    check("fasth3-edit: 音频槽按顺序指向 105:212..214",
          [eins[f"ref_audios.ref_audio_{i}"][0] for i in range(3)] == AUD_IDS)
    check("fasth3-edit: 视频与音轨同源（同一 GetVideoComponents 的 0 / 1 输出）",
          all(eins[f"ref_videos.ref_video_{i}"] == [VCOMP_IDS[i], 0]
              and eins[f"ref_video_audios.ref_video_audio_{i}"] == [VCOMP_IDS[i], 1]
              for i in range(3)))
    check("fasth3-edit: LoadVideo 各自接自己的 GetVideoComponents",
          all(edit[VCOMP_IDS[i]]["inputs"]["video"] == [VID_IDS[i], 0] for i in range(3)))
    check("fasth3-edit: loader 节点类型正确",
          all(edit[n]["class_type"] == "LoadImage" for n in IMG_IDS)
          and all(edit[n]["class_type"] == "LoadVideo" for n in VID_IDS)
          and all(edit[n]["class_type"] == "LoadAudio" for n in AUD_IDS))

    reg = Registry()
    reg.load(HERE / "models.yaml", HERE / "workflows", "z-image")
    spec, es = reg.resolve(MODEL), reg.resolve(MODEL_EDIT)
    check("两个模型都已注册", spec.name == MODEL and es.name == MODEL_EDIT)
    check("别名可解析",
          reg.resolve("fastvideo-fasth3").name == MODEL
          and reg.resolve("fast-h3").name == MODEL
          and reg.resolve("fastvideo-fasth3-edit").name == MODEL_EDIT)
    check("fasth3 references: 聚合 105:104 + image_keys 覆盖",
          str(spec.references["aggregator"]) == "105:104"
          and [str(x) for x in spec.references["images"]] == FRAME_IDS
          and [str(x) for x in spec.references["image_keys"]] == ["first_frame", "last_frame"],
          spec.references)
    check("fasth3-edit references 与工作流接线一致",
          str(es.references["aggregator"]) == "105:145"
          and [str(x) for x in es.references["images"]] == IMG_IDS
          and [str(x) for x in es.references["videos"]] == VID_IDS
          and [str(x) for x in es.references["audios"]] == AUD_IDS)
    check("两支 output_node 都是 92",
          str(spec.output_node) == "92" and str(es.output_node) == "92")

    print("\n[4] 参数注入落点")
    values = {"prompt": "p", "seed": 12345, "steps": 10, "width": 960, "height": 544,
              "duration": 6, "fps": 30}
    built = build_workflow(spec, values, None)
    check("prompt -> 105:104", built["105:104"]["inputs"]["prompt"] == "p")
    check("seed -> 105:15.noise_seed", built["105:15"]["inputs"]["noise_seed"] == 12345)
    check("steps -> 105:9", built["105:9"]["inputs"]["steps"] == 10)
    check("size -> 105:104 width/height",
          (built["105:104"]["inputs"]["width"], built["105:104"]["inputs"]["height"]) == (960, 544))
    check("duration -> 105:111（秒，供 length 表达式换算帧数）",
          built["105:111"]["inputs"]["value"] == 6)
    check("fps -> 105:91", built["105:91"]["inputs"]["fps"] == 30)
    check("不传前缀时保留模板默认 video/FastH3",
          built["92"]["inputs"]["filename_prefix"] == PREFIX, built["92"]["inputs"]["filename_prefix"])
    ebuilt = build_workflow(es, {"prompt": "p2"}, None)
    check("fasth3-edit: prompt -> 105:145", ebuilt["105:145"]["inputs"]["prompt"] == "p2")

    print("\n[5] 参考槽裁剪")
    for label, imgs, expect_dropped, expect_first, expect_last in (
        ("0 图（纯文生视频）", [], 2, None, None),
        ("1 图（首帧）", ["a.png"], 1, ["105:200", 0], None),
        ("2 图（首尾帧）", ["a.png", "b.png"], 0, ["105:200", 0], ["105:201", 0]),
    ):
        w2 = build_workflow(spec, {"prompt": "p"}, None)
        dropped = pipeline._prune_unused_references(
            w2, spec, VideoGenerationRequest(prompt="p", reference_images=imgs or None))
        a = w2["105:104"]["inputs"]
        check(f"fasth3 {label}: 删除 {expect_dropped} 节点", dropped == expect_dropped, f"got={dropped}")
        check(f"fasth3 {label}: first_frame={expect_first} last_frame={expect_last}",
              a.get("first_frame") == expect_first and a.get("last_frame") == expect_last,
              f"{a.get('first_frame')} / {a.get('last_frame')}")
        check(f"fasth3 {label}: 无悬空连线 / 孤儿",
              not dangling(w2) and not (set(w2) - reachable(w2, "92")))

    for label, imgs, vids, auds, expect_dropped in (
        ("0/0/0", [], [], [], 15),
        ("1/0/0", ["a.png"], [], [], 14),
        ("2/1/1", ["a.png", "b.png"], ["v.mp4"], ["a.wav"], 10),
        ("满配 6/3/3", ["a.png"] * 6, ["v.mp4"] * 3, ["a.wav"] * 3, 0),
    ):
        w2 = build_workflow(es, {"prompt": "p"}, None)
        dropped = pipeline._prune_unused_references(
            w2, es,
            VideoGenerationRequest(prompt="p", reference_images=imgs or None,
                                   reference_videos=vids or None, reference_audios=auds or None))
        left = [k for k in w2["105:145"]["inputs"]
                if k.startswith(("ref_images.", "ref_videos.", "ref_audios."))]
        orphan = sorted(set(w2) - reachable(w2, "92"))
        check(f"fasth3-edit {label}: 删除 {expect_dropped} 节点", dropped == expect_dropped, f"got={dropped}")
        check(f"fasth3-edit {label}: 参考键 {len(imgs) + len(vids) + len(auds)} 个",
              len(left) == len(imgs) + len(vids) + len(auds), f"got={len(left)}")
        check(f"fasth3-edit {label}: 聚合节点保留 + 无孤儿/悬空",
              "105:145" in w2 and not orphan and not dangling(w2), f"orphans={orphan}")

    print("\n[6] 键名覆盖机制")
    check("默认键名未受影响", ref_key(None, "images", 0) == "ref_images.ref_image_0"
          and ref_key(None, "videos", 2) == "ref_videos.ref_video_2"
          and ref_key(None, "audios", 1) == "ref_audios.ref_audio_1")
    keys = {"image_keys": ["first_frame", "last_frame"]}
    check("逐槽覆盖命中", ref_key(keys, "images", 0) == "first_frame"
          and ref_key(keys, "images", 1) == "last_frame")
    check("越过覆盖列表后回落默认（不会静默错位到别人槽位）",
          ref_key(keys, "images", 2) == "ref_images.ref_image_2")
    bad = ModelSpec(
        name="bad", workflow_path=HERE / "models.yaml",
        template={"1": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"first_frame": ["2", 0]}},
                  "2": {"class_type": "LoadImage", "inputs": {"image": "x.png"}}},
        references={"aggregator": "1", "images": ["2"], "image_keys": ["first_frame", "last_frame"]},
    )
    try:
        _validate_references(bad)
        check("image_keys 与 images 数量不匹配时报错", False, "没有抛错")
    except RuntimeError as exc:
        check("image_keys 与 images 数量不匹配时报错", "对不上" in str(exc), str(exc))

    print("\n[7] 同族其它模型未受影响")
    for name, expect_vae in (("minimax-h3", "video/save"), ("minimax-h3-turbo-edit", "video/save")):
        s = reg.resolve(name)
        w = build_workflow(s, {"prompt": "p"}, None)
        vaes = [n["inputs"]["vae_name"] for n in w.values()
                if isinstance(n, dict) and n.get("class_type") == "VAELoader"]
        check(f"{name}: video VAE 仍是 int8_convrot",
              VIDEO_VAE in vaes, f"{vaes}")
        check(f"{name}: audio VAE 仍是 fp32",
              AUDIO_VAE in vaes, f"{vaes}")
    check("H3 系列视频模型总数为 11（5 支 MiniMax 常规——含 HyperFlow 加速档 + 2 支 MiniMax SelfLift + 2 支 FastH3 + 2 支 FastH3 SelfLift）",
          len([s for s in reg.all() if s.mode == "video"]) == 11,
          [s.name for s in reg.all() if s.mode == "video"])

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
