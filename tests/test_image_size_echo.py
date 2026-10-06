"""同步回执的 `size` 必须是**实测**值 —— 而且是「到最后一米还在」的。

为什么需要这个测试：
    尺寸出过一次**静默**事故：请求漏传 `size`，网关落模型默认 1024x1024，回执里却看不到
    任何尺寸字段 —— 画幅变成 1:1 这件事只能靠肉眼从产物里发现。根因是 ImageResponse 压根
    没有 `size`（VideoResponse 有），于是同一个「实际输出尺寸」在两条链路上一个有一个没有。

    这个测试锁四件事：
      1. 尺寸解析（PNG/JPEG/GIF/WebP 头）本身对；
      2. 三条取证路径的**优先级**：工作流自报 > 产物字节 > 画布参数，且都没有时不编造；
      3. 结构护栏：ImageResponse/VideoResponse 的**每一处构造**都带 `size=`，且 _run_once
         把工作流自报值带回调用方 —— 「字段加了但没接上」正是这次事故的形态；
      4. 视频四支模板都真的插了 RoundaboutSizeProbe 并接在 VAEDecode 上（自报值的来源）。

不碰正在运行的 ComfyUI、不联网、不落产物：纯逻辑 + AST + JSON 断言。

    python tests/test_image_size_echo.py
"""
from __future__ import annotations

import ast
import json
import struct
import sys
from pathlib import Path

NODE = Path(__file__).resolve().parent.parent   # 节点目录
ROOT = NODE.parent.parent                        # ComfyUI 根目录
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gateway.comfy_client import probe_node_id, reported_size  # noqa: E402
from gateway.params import image_dimensions  # noqa: E402
from gateway.pipeline import estimated_video_size, response_size  # noqa: E402
from gateway.schemas import ImageResponse  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
    if not ok:
        failures += 1


# ------------------------------------------------------------------ 样本字节
def png(w: int, h: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h)
            + b"\x08\x06\x00\x00\x00" + b"\x00\x00\x00\x00")


def jpeg(w: int, h: int) -> bytes:
    # SOI + SOF0（精度 8，高、宽，1 个分量）+ 尾巴填充
    return (b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", 11) + b"\x08"
            + struct.pack(">HH", h, w) + b"\x01\x11\x00" + b"\xff\xd9")


def gif(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00" + b"\x3b"


def webp_vp8x(w: int, h: int) -> bytes:
    # RIFF/WEBP + VP8X 块：4B fourcc + 4B 块长 + 1B flags + 3B reserved + 3B 宽-1 + 3B 高-1
    return (b"RIFF" + struct.pack("<I", 22) + b"WEBP" + b"VP8X" + b"\x00\x00\x00\x00"
            + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


class _Img:
    """RenderedImage 的最小替身：response_size 只用到 .data。"""

    def __init__(self, data: bytes) -> None:
        self.data = data


# ================================================================== 1. 头解析
print("1. 产物字节头解析")
cases = [
    ("png", png(1376, 768), (1376, 768)),
    ("jpeg", jpeg(1024, 1024), (1024, 1024)),
    ("gif", gif(480, 640), (480, 640)),
    ("webp-vp8x", webp_vp8x(2528, 1440), (2528, 1440)),
]
for label, blob, want in cases:
    check(f"{label} 尺寸解析", image_dimensions(blob) == want, f"got {image_dimensions(blob)} want {want}")
for label, blob in [("空字节", b""), ("随机字节", b"not an image at all"), ("截断 PNG", png(64, 64)[:12])]:
    check(f"{label} 不瞎猜（返回 None）", image_dimensions(blob) is None, f"got {image_dimensions(blob)}")

# ================================================================== 2. 取证优先级
print("2. size 取证优先级（自报 > 产物字节 > 画布）")
canvas = {"width": 1024, "height": 1024}
check("自报值优先于产物字节",
      response_size([_Img(png(1376, 768))], canvas, "1376x768") == "1376x768",
      str(response_size([_Img(png(1376, 768))], canvas, "1376x768")))
check("无自报时读产物字节（覆盖漏传 size 的场景：画布 1024x1024 而产物 1376x768）",
      response_size([_Img(png(1376, 768))], canvas) == "1376x768",
      str(response_size([_Img(png(1376, 768))], canvas)))
check("产物读不出才回落画布",
      response_size([_Img(b"garbage")], canvas) == "1024x1024",
      str(response_size([_Img(b"garbage")], canvas)))
check("都没有时不给尺寸（不编造）",
      response_size([], {}) is None, str(response_size([], {})))
check("多张产物取第一张的实测值",
      response_size([_Img(png(832, 480)), _Img(png(64, 64))], canvas) == "832x480",
      str(response_size([_Img(png(832, 480)), _Img(png(64, 64))], canvas)))

print("3. 视频换算（只作回落）")
check("lift：画布 1344x768 × 1.875",
      estimated_video_size({"width": 1344, "height": 768, "scale": 1.875}) == "2528x1440",
      str(estimated_video_size({"width": 1344, "height": 768, "scale": 1.875})))
check("非 lift：即画布",
      estimated_video_size({"width": 1376, "height": 768}) == "1376x768",
      str(estimated_video_size({"width": 1376, "height": 768})))
check("无画布则 None", estimated_video_size({}) is None, str(estimated_video_size({})))

print("4. probe 节点发现与自报值读取")
wf = {"122": {"class_type": "VAEDecode"}, "950": {"class_type": "RoundaboutSizeProbe"}}
check("按类名发现节点 id（不写死 950）",
      probe_node_id({"7": {"class_type": "RoundaboutSizeProbe"}}) == "7",
      str(probe_node_id({"7": {"class_type": "RoundaboutSizeProbe"}})))
check("模板没装则 None", probe_node_id({"122": {"class_type": "VAEDecode"}}) is None)
check("列表形态自报值", reported_size({"outputs": {"950": {"size": ["1376x768"]}}}, wf) == "1376x768")
check("字符串形态自报值", reported_size({"outputs": {"950": {"size": "1376x768"}}}, wf) == "1376x768")
check("没跑到 probe 则 None", reported_size({"outputs": {}}, wf) is None)
check("形态不符当没报（不虚构）",
      reported_size({"outputs": {"950": {"size": ["oops"]}}}, wf) is None)

# ================================================================== 3. 结构护栏
print("5. 结构护栏：字段加了必须接上")
schema_src = (NODE / "gateway" / "schemas.py").read_text(encoding="utf-8")
schema_tree = ast.parse(schema_src)
img_fields: set[str] = set()
vid_fields: set[str] = set()
for node in schema_tree.body:
    if isinstance(node, ast.ClassDef) and node.name in ("ImageResponse", "VideoResponse"):
        target = img_fields if node.name == "ImageResponse" else vid_fields
        for sub in node.body:
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                target.add(sub.target.id)
check("ImageResponse 声明了 size", "size" in img_fields, str(sorted(img_fields)))
check("VideoResponse 声明了 size", "size" in vid_fields, str(sorted(vid_fields)))
check("ImageResponse 也带 task_id（同为上一批补过的字段）", "task_id" in img_fields)

pipe_tree = ast.parse((NODE / "gateway" / "pipeline.py").read_text(encoding="utf-8"))
constructed: dict[str, list[int]] = {"ImageResponse": [], "VideoResponse": []}
for node in ast.walk(pipe_tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in constructed:
        constructed[node.func.id].append(node.lineno)
        if not any(kw.arg == "size" for kw in node.keywords):
            check(f"L{node.lineno} {node.func.id}(...) 带了 size=", False, "缺 size 关键字")
check("ImageResponse 至少有一处构造", len(constructed["ImageResponse"]) >= 1, str(constructed))
check("VideoResponse 至少有一处构造", len(constructed["VideoResponse"]) >= 1, str(constructed))
check("pipeline 的 _run_once 返回三元组（把自报值带回调用方）",
      "-> tuple[list[RenderedImage], list[dict[str, Any]], str | None]"
      in (NODE / "gateway" / "pipeline.py").read_text(encoding="utf-8"))
check("pipeline 落了实测自报值（return ... reported_size(entry, workflow)）",
      "reported_size(entry, workflow)" in (NODE / "gateway" / "pipeline.py").read_text(encoding="utf-8"))

# ================================================================== 4. 视频模板
print("6. 视频四支模板都带 RoundaboutSizeProbe，且接在 VAEDecode 上")
nodes_src = (NODE / "nodes.py").read_text(encoding="utf-8")
check("nodes.py 注册了 probe 类", "RoundaboutSizeProbe" in nodes_src)
check("probe 是 OUTPUT_NODE（否则不进 /history）",
      "OUTPUT_NODE = True" in nodes_src.split("class RoundaboutSizeProbe")[1].split("class ")[0],
      "probe 类里没有 OUTPUT_NODE = True")

videos = sorted((NODE / "workflows").glob("video_*.json"))
check("视频模板数量符合预期（4 支）", len(videos) == 4, str([p.name for p in videos]))
for path in videos:
    data = json.loads(path.read_text(encoding="utf-8"))
    probes = {nid: n for nid, n in data.items() if (n or {}).get("class_type") == "RoundaboutSizeProbe"}
    check(f"{path.name}: 带 1 个 probe", len(probes) == 1, str(list(probes)))
    if len(probes) != 1:
        continue
    nid, node = next(iter(probes.items()))
    src = (node.get("inputs") or {}).get("images") or []
    up = data.get(str(src[0]) if src else "", {})
    check(f"{path.name}: probe.{nid} 接在 VAEDecode 输出上",
          bool(src) and (up or {}).get("class_type") == "VAEDecode",
          f"images={src} upstream={(up or {}).get('class_type')}")

# ================================================================== 5. 回执契约
print("7. 回执契约")
dump = ImageResponse(created=1, data=[], seed=2, size="1376x768").model_dump(exclude_none=True)
check("有尺寸时回执带 size", dump.get("size") == "1376x768", str(dump))
no_size = ImageResponse(created=1, data=[], seed=2).model_dump(exclude_none=True)
check("没尺寸时不带 size 键", "size" not in no_size, str(no_size))

print(f"\n {'=' * 46}\n  {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
raise SystemExit(1 if failures else 0)
