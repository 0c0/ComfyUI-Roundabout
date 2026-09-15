"""Ref2VA 版 SelfLift（`minimax-h3-self-lift-edit`）的接线与参考槽裁剪测试。

背景：`minimax-h3-self-lift` 只挂两个 LoadImage（文生 / 首尾帧），而 Ref2VA 那支要
保留 6 图 + 3 视频 + 3 音频的全套参考槽。新工作流由 self-lift 派生：采样链路、低显存
分块、显存分档逐字保留，只换 UNET（ref2va）与 LoRA（ref2v turbo 4step），参考槽扩容。

验证点：
  [1] 工作流文件：API 格式、骨架节点齐全、骨架参数与 self-lift 逐字一致
  [2] 权重替换：UNET 换 ref2va、LoRA 换 ref2v turbo（strength 沿用）
  [3] references 声明与工作流实际接线一一对应（含参考视频与音轨同源）
  [4] 显存分档注入仍然生效
  [5] 参考槽裁剪：0 / 部分 / 满配三类组合，无悬空连线、无不可达孤儿
  [6] 不影响同族其它模型

自测：python test_ref2va_self_lift.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # <ComfyUI>
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.disable(logging.CRITICAL)  # 静音被测模块的 normal 日志，只留断言输出

WF_SL = "video_minimax_h3_self_lift.json"
WF_REF = "video_minimax_h3_self_lift_edit.json"
MODEL = "minimax-h3-self-lift-edit"

# 骨架节点：派生时应当逐字保留
SKELETON = ("121", "122", "123", "124", "128", "131", "132", "136", "147", "148",
            "173", "184", "219", "220", "227", "232", "234", "235", "238")
IMG_IDS = ["300", "301", "302", "303", "304", "305"]
VID_IDS = ["306", "307", "308"]
VCOMP_IDS = ["309", "310", "311"]
AUD_IDS = ["312", "313", "314"]

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
    from gateway.registry import Registry, build_workflow
    from gateway import pipeline, vram
    from gateway.schemas import VideoGenerationRequest

    saved_env = os.environ.get(vram.ENV_KEY)

    wf = json.loads((HERE / "workflows" / WF_REF).read_text(encoding="utf-8"))
    sl = json.loads((HERE / "workflows" / WF_SL).read_text(encoding="utf-8"))

    print("[1] 工作流文件与骨架")
    check("文件是 API 格式（节点 id 直接映射，无 nodes 数组）",
          "nodes" not in wf and all(isinstance(v, dict) and "class_type" in v for v in wf.values()))
    check("节点数 = 骨架 23 + 参考槽 13（去掉原 2 个 LoadImage 再加 15）", len(wf) == 36, f"got={len(wf)}")
    missing = [nid for nid in SKELETON if nid not in wf]
    check("骨架节点齐全", not missing, f"missing={missing}")
    diff = []
    for nid in SKELETON:
        if nid in ("136", "232"):  # 参考键与示例提示词必然不同，单独校验
            continue
        a = {k: v for k, v in wf[nid]["inputs"].items()}
        b = {k: v for k, v in sl[nid]["inputs"].items()}
        if a != b:
            diff.append(nid)
    check("骨架参数与 self-lift 逐字一致（136 / 232 除外）", not diff, f"diff={diff}")
    check("示例提示词节点仍是多行字符串且非空",
          wf["232"]["class_type"] == "PrimitiveStringMultiline"
          and bool(wf["232"]["inputs"]["value"].strip()))
    check("原 self-lift 的两个 LoadImage 已移除",
          "150" not in wf and "164" not in wf)

    print("\n[2] 权重替换")
    check("UNET 换 ref2va", wf["127"]["inputs"]["unet_name"] == "minimax_h3_ref2va_int8_convrot.safetensors",
          wf["127"]["inputs"]["unet_name"])
    check("self-lift 仍是 fl2va（未被波及）",
          sl["127"]["inputs"]["unet_name"] == "minimax_h3_fl2va_int8_convrot.safetensors",
          sl["127"]["inputs"]["unet_name"])
    check("LoRA 换 ref2v turbo 4step",
          wf["145"]["inputs"]["lora_name"] == "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
          wf["145"]["inputs"]["lora_name"])
    check("LoRA strength 沿用 self-lift 的 0.65",
          abs(wf["145"]["inputs"]["strength_model"] - sl["145"]["inputs"]["strength_model"]) < 1e-9,
          f"{wf['145']['inputs']['strength_model']} vs {sl['145']['inputs']['strength_model']}")

    print("\n[3] references 声明 ↔ 工作流接线")
    ins = wf["136"]["inputs"]
    check("图槽 6 个", all(f"ref_images.ref_image_{i}" in ins for i in range(6)))
    check("视频槽 3 个", all(f"ref_videos.ref_video_{i}" in ins for i in range(3)))
    check("视频音轨槽 3 个", all(f"ref_video_audios.ref_video_audio_{i}" in ins for i in range(3)))
    check("音频槽 3 个", all(f"ref_audios.ref_audio_{i}" in ins for i in range(3)))
    check("参考键总数 = 15（ref_image_size 是尺寸参数，不算槽位）",
          len([k for k in ins if k.startswith("ref_") and k != "ref_image_size"]) == 15)
    check("图槽按顺序指向 300..305",
          [ins[f"ref_images.ref_image_{i}"][0] for i in range(6)] == IMG_IDS)
    check("音频槽按顺序指向 312..314",
          [ins[f"ref_audios.ref_audio_{i}"][0] for i in range(3)] == AUD_IDS)
    check("视频与音轨同源（同一个 GetVideoComponents 的 0 / 1 两个输出）",
          all(ins[f"ref_videos.ref_video_{i}"] == [VCOMP_IDS[i], 0]
              and ins[f"ref_video_audios.ref_video_audio_{i}"] == [VCOMP_IDS[i], 1]
              for i in range(3)))
    check("LoadVideo 各自接自己的 GetVideoComponents",
          all(wf[VCOMP_IDS[i]]["inputs"]["video"] == [VID_IDS[i], 0] for i in range(3)))
    check("loader 节点类型正确",
          all(wf[n]["class_type"] == "LoadImage" for n in IMG_IDS)
          and all(wf[n]["class_type"] == "LoadVideo" for n in VID_IDS)
          and all(wf[n]["class_type"] == "LoadAudio" for n in AUD_IDS))

    os.environ[vram.ENV_KEY] = "8"
    vram.reset_cache()
    reg = Registry()
    reg.load(HERE / "models.yaml", HERE / "workflows", "z-image")
    spec = reg.resolve(MODEL)
    check("模型已注册", spec.name == MODEL)
    check("别名 ref2va-selflift 可解析", reg.resolve("ref2va-selflift").name == MODEL)
    check("references 段与工作流接线一致",
          [str(x) for x in spec.references["images"]] == IMG_IDS
          and [str(x) for x in spec.references["videos"]] == VID_IDS
          and [str(x) for x in spec.references["audios"]] == AUD_IDS
          and str(spec.references["aggregator"]) == "136")
    check("output_node 指向 SaveVideo", str(spec.output_node) == "238")

    print("\n[4] 显存分档注入（8 GiB 档）")
    built = build_workflow(spec, {"prompt": "p"}, None)
    got = (built["219"]["inputs"]["chunks"], built["220"]["inputs"]["head_chunks"],
           built["219"]["inputs"]["seq_threshold"], built["235"]["inputs"]["highres_tiling"])
    check("8 GiB -> 6 / 24 / 4096 / true", got == (6, 24, 4096, True), f"got={got}")
    check("分档值确实写到节点上（非模板里的 2 / 8）", built["219"]["inputs"]["chunks"] == 6)
    check("请求参数仍可覆盖档位值",
          build_workflow(spec, {"prompt": "p", "chunks": 3}, None)["219"]["inputs"]["chunks"] == 3)

    print("\n[5] 参考槽裁剪")
    cases = [
        ("0 图 0 视频 0 音频", [], [], [], 15, 0),
        ("2 图", ["a.png", "b.png"], [], [], 13, 2),
        ("1 图 1 视频", ["a.png"], ["v.mp4"], [], 12, 3),
        ("1 图 1 音频", ["a.png"], [], ["a.wav"], 13, 2),
        ("满配 6 图 3 视频 3 音频", ["a.png"] * 6, ["v.mp4"] * 3, ["a.wav"] * 3, 0, 15),
    ]
    for label, imgs, vids, auds, expect_dropped, expect_keys in cases:
        w2 = build_workflow(spec, {"prompt": "p"}, None)
        dropped = pipeline._prune_unused_references(
            w2, spec,
            VideoGenerationRequest(prompt="p", reference_images=imgs or None,
                                   reference_videos=vids or None, reference_audios=auds or None),
        )
        left = sorted(k for k in w2["136"]["inputs"]
                      if k.startswith("ref_") and k != "ref_image_size")
        orphan = sorted(set(w2) - reachable(w2, "238"), key=int)
        check(f"{label} -> 删除 {expect_dropped} 节点", dropped == expect_dropped, f"got={dropped}")
        check(f"{label} -> 保留 {expect_keys} 个参考键", len(left) == expect_keys, f"got={len(left)}")
        check(f"{label} -> aggregator 与聚合节点保留", "136" in w2 and "238" in w2)
        check(f"{label} -> 无悬空连线", not dangling(w2), "; ".join(dangling(w2)))
        check(f"{label} -> 无不可达孤儿节点", not orphan, f"orphans={orphan}")

    print("\n[6] 同族其它模型未受影响")
    for name, expect_unet in (
        ("minimax-h3-self-lift", "fl2va"),
        ("minimax-h3-turbo-edit", "ref2va"),
        ("minimax-h3", "fl2va"),
    ):
        s = reg.resolve(name)
        w = build_workflow(s, {"prompt": "p"}, None)
        unets = [n["inputs"]["unet_name"] for n in w.values()
                 if isinstance(n, dict) and n.get("class_type") == "UNETLoader"]
        check(f"{name} 权重仍含 {expect_unet}",
              any(expect_unet in u for u in unets), f"{unets}")

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
