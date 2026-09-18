"""SelfLift 两支（minimax-h3-self-lift / -edit，无 LoRA 标定）回归测试。

背景：旧 SelfLift 两支在基础权重上挂 0.65 强度的 4 步蒸馏 LoRA（lightx2v / ref2v turbo），
两阶段渐进采样下细节劣化。新两支去掉 LoRA，基础 fl2va / ref2va 权重直跑，步数与 σ 标定
不再迁就蒸馏档：steps=8 / ts=8 / extra=2 / σ0.9（模板字面值与 defaults 同步，其中
extra_steps / start_at_sigma 只在 defaults——fastvideo-fasth3-self-lift 同款做法），
lowres_scale 默认 auto，保存产物落 video/MiniMax_H3_Lift。

验证点：
  [1] 模板结构：无 LoRA 节点、220 直连 148、模板字面值 8/8/2/0.9、前缀换族
  [2] defaults 流：ts=8 合法性（extra_steps=2 必须被校验感知）、auto 解析、注入闭环
  [3] 参考槽插拔：0/1/2 张图无悬空连线（复用 _prune_unused_references）

自测：python tests/test_minimax_self_lift.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent  # <ComfyUI>
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.disable(logging.CRITICAL)

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def main() -> int:
    os.environ["ROUNDABOUT_VRAM_GB"] = "8"
    from gateway import vram
    from gateway.pipeline import _prune_unused_references, build_video_values
    from gateway.registry import Registry, build_workflow
    from gateway.schemas import VideoGenerationRequest

    vram.reset_cache()
    reg = Registry()
    reg.load(HERE / "models.yaml", HERE / "workflows", "z-image")

    cases = (
        ("minimax-h3-self-lift", ("150", "164")),
        ("minimax-h3-self-lift-edit", ("300", "301")),
    )

    print("\n[1] 模板结构")
    for name, _ in cases:
        spec = reg.resolve(name)
        tpl = spec.template
        check(f"{name}: 已注册", spec.name == name)
        check(f"{name}: 模板无 LoraLoaderModelOnly",
              not [k for k, n in tpl.items()
                   if isinstance(n, dict) and n.get("class_type") == "LoraLoaderModelOnly"])
        check(f"{name}: 220 直连 MAB(148)", tpl["220"]["inputs"]["model"] == ["148", 0],
              tpl["220"]["inputs"]["model"])
        lit = (tpl["124"]["inputs"]["steps"], tpl["235"]["inputs"]["transition_step"],
               tpl["147"]["inputs"]["extra_steps"], tpl["147"]["inputs"]["start_at_sigma"])
        check(f"{name}: 模板字面值 8/8/2/0.9", lit == (8, 8, 2, 0.9), lit)
        check(f"{name}: 保存前缀 video/MiniMax_H3_Lift",
              tpl["238"]["inputs"]["filename_prefix"] == "video/MiniMax_H3_Lift",
              tpl["238"]["inputs"]["filename_prefix"])

    print("\n[2] defaults 流与注入闭环")
    for name, _ in cases:
        spec = reg.resolve(name)
        d = spec.defaults
        check(f"{name}: defaults extra_steps=2 / sigma=0.9 / auto",
              d.get("extra_steps") == 2 and d.get("start_at_sigma") == 0.9
              and d.get("lowres_scale") == "auto",
              {k: d.get(k) for k in ("extra_steps", "start_at_sigma", "lowres_scale")})
        vals = build_video_values(VideoGenerationRequest(prompt="x"), spec, "x", None)
        check(f"{name}: ts=8 通过 ts<=steps+extra-1 校验",
              vals.get("transition_step") == 8 and vals.get("steps") == 8, dict(vals))
        check(f"{name}: lowres auto -> 0.70 @1344x768",
              abs((vals.get("lowres_scale") or 0) - 0.70) < 0.02, vals.get("lowres_scale"))
        wf = build_workflow(spec, vals, None)
        check(f"{name}: 注入闭环 235.lowres_scale=0.7 / ts=8",
              wf["235"]["inputs"]["lowres_scale"] == 0.7
              and wf["235"]["inputs"]["transition_step"] == 8,
              wf["235"]["inputs"])
        check(f"{name}: 8GB 档 highres_tiling=True / 分块 6/24/4096",
              wf["235"]["inputs"]["highres_tiling"] is True
              and wf["219"]["inputs"]["chunks"] == 6
              and wf["220"]["inputs"]["head_chunks"] == 24
              and wf["219"]["inputs"]["seq_threshold"] == 4096,
              (wf["219"]["inputs"]["chunks"], wf["220"]["inputs"]["head_chunks"]))

    print("\n[3] 参考槽插拔")
    for name, img_nodes in cases:
        spec = reg.resolve(name)
        for n, keep in ((0, ()), (1, img_nodes[:1]), (2, img_nodes[:2])):
            wf2 = build_workflow(spec, {"prompt": "p"}, None)
            req = VideoGenerationRequest(prompt="p",
                                         reference_images=(["a.png"] * n) or None)
            _prune_unused_references(wf2, spec, req)
            present = tuple(nid for nid in img_nodes if nid in wf2)
            keys = sorted(k for k in wf2["136"]["inputs"] if k.startswith("ref_images"))
            check(f"{name} 传 {n} 张图 -> 保留 {len(keep)} 节点", present == keep, present)
            check(f"{name} 传 {n} 张图 -> aggregator 键同步",
                  keys == [f"ref_images.ref_image_{i}" for i in range(n)], keys)
            dangling = [f"{nid}.{k}->{v[0]}" for nid, node in wf2.items()
                        for k, v in (node.get("inputs") or {}).items()
                        if isinstance(v, list) and len(v) == 2
                        and isinstance(v[0], str) and v[0] not in wf2]
            check(f"{name} 传 {n} 张图 -> 无悬空连线", not dangling, "; ".join(dangling))

    print()
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
