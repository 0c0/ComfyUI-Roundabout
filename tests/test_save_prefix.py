"""视频工作流保存前缀约定回归测试。

背景：H3 系列五份工作流一直落 `video/MiniMax_H3`，后加的两份 SelfLift 却沿用了
ComfyUI 默认的 `video/ComfyUI`，产出散在两个目录里——没有任何测试盯着这条约定，
所以漂了很久才被发现。这里把约定固定下来。

    python tests/test_save_prefix.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.pipeline import build_video_values  # noqa: E402
from gateway.registry import Registry, build_workflow  # noqa: E402
from gateway.schemas import VideoGenerationRequest  # noqa: E402

VIDEO_PREFIX = "video/"
H3_PREFIX = "video/MiniMax_H3"

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

video_specs = [s for s in registry.all() if s.mode == "video"]


def save_prefix(spec):
    """模板里那个保存节点的 filename_prefix（先按 binding 找，找不到就全图扫）。"""
    paths = spec.bindings.get("filename_prefix") or []
    for path in paths:
        node_id = path.split(".")[0]
        node = spec.template.get(node_id)
        if node and "filename_prefix" in (node.get("inputs") or {}):
            return node_id, node["inputs"]["filename_prefix"]
    for node_id, node in spec.template.items():
        if isinstance(node, dict) and "filename_prefix" in (node.get("inputs") or {}):
            return node_id, node["inputs"]["filename_prefix"]
    return None, None


print("== 1. 每个视频工作流都有可覆盖的保存前缀 ==")
check("确实扫到了视频模型", len(video_specs) >= 6, [s.name for s in video_specs])
for spec in video_specs:
    node_id, prefix = save_prefix(spec)
    check(f"{spec.name}: 保存前缀绑到节点 {node_id}", isinstance(prefix, str) and prefix,
          (node_id, prefix))
    check(f"{spec.name}: 请求可覆盖（binding 指向保存节点）",
          spec.bindings.get("filename_prefix") == [f"{node_id}.inputs.filename_prefix"],
          spec.bindings.get("filename_prefix"))

print("\n== 2. 目录约定：视频一律落 video/ 下 ==")
for spec in video_specs:
    _, prefix = save_prefix(spec)
    check(f"{spec.name}: {prefix!r} 在 video/ 下", str(prefix).startswith(VIDEO_PREFIX), prefix)

print("\n== 3. H3 系列统一为 video/MiniMax_H3 ==")
h3_specs = [s for s in video_specs if "h3" in s.name]
check("H3 系列共 6 支（4 支常规 + 2 支 SelfLift）", len(h3_specs) == 6, [s.name for s in h3_specs])
for spec in h3_specs:
    _, prefix = save_prefix(spec)
    check(f"{spec.name}: {prefix!r} == {H3_PREFIX!r}", prefix == H3_PREFIX, prefix)

print("\n== 4. 不传就用模板默认（不是被网关改写成别的） ==")
LIFT = registry.resolve("minimax-h3-self-lift")
req = VideoGenerationRequest(prompt="x")
values = build_video_values(req, LIFT, "x", None)
check("请求未传时 values 里是 None（回落模板，而非网关造值）",
      values.get("filename_prefix") is None, values.get("filename_prefix"))
wf = build_workflow(LIFT, values)
check("注入后节点仍是模板默认 video/MiniMax_H3",
      wf["238"]["inputs"]["filename_prefix"] == H3_PREFIX,
      wf["238"]["inputs"]["filename_prefix"])
values = build_video_values(VideoGenerationRequest(prompt="x", filename_prefix="my/dir"), LIFT, "x", None)
wf = build_workflow(LIFT, values)
check("显式传入时原样覆盖（可含 / 建子目录）",
      wf["238"]["inputs"]["filename_prefix"] == "my/dir",
      wf["238"]["inputs"]["filename_prefix"])

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
