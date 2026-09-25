"""统一节点拓扑：帧槽与参考槽并存（v1.17.0 拆分生成/编辑两档的核心机制）。

为什么需要这个测试：
    旧的互斥口径是「fl2va 只收 first_frame/last_frame、edit 只收 reference_images」，
    换统一聚合节点后两条都成立且可同传 —— 若网关还按互斥拦，帧+参考组合（官方 Reference
    模式的常规用法）会被 400 拒掉。反向风险是「静默放宽」：没有槽位的字段（如 FL2VA 档传
    reference_videos）会被 wire 循环悄悄丢掉。

覆盖：
    A. 离线 —— 槽位计数（frames 前缀 / images 余量）、toolinfo 生效性、_slot_values 混合取值；
    B. HTTP —— 无槽字段一律 400，绝不静默丢弃。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

NODE = Path(__file__).resolve().parent.parent
ROOT = NODE.parent.parent
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

import gateway.pipeline as pipeline  # noqa: E402
from gateway import toolinfo  # noqa: E402
from gateway.registry import registry  # noqa: E402
from gateway.routes import register_routes  # noqa: E402
from gateway.config import settings  # noqa: E402

registry.load(settings.models_file, settings.workflows_dir, settings.default_model)

PORT = 8201
BASE = f"http://127.0.0.1:{PORT}"

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + str(detail)) if detail and not ok else ''}")
    if not ok:
        failures += 1


def slot_counts(name: str) -> dict:
    return toolinfo._slot_counts(registry.resolve(name))


def field_applies(name: str, field: str) -> bool:
    entry = toolinfo._model_entry(registry.resolve(name))
    f = [x for x in entry["fields"] if x["name"] == field][0]
    return f["applies"] is True


def offline_section() -> None:
    print("== [A] 离线：槽位计数 / toolinfo 生效性 / 混合取值 ==")
    c_gen = slot_counts("minimax-h3")
    check("生成档 frames=2", c_gen.get("frames") == 2, c_gen)
    check("生成档 images=6", c_gen.get("images") == 6, c_gen)
    check("生成档 videos=3 audios=3（音频实测不迁移音色，槽位已接）",
          (c_gen.get("videos"), c_gen.get("audios")) == (3, 3), c_gen)
    c_edit = slot_counts("minimax-h3-edit")
    check("编辑档 frames 不存在", "frames" not in c_edit or c_edit.get("frames") in (0, None), c_edit)
    check("编辑档 images=6 videos=3 audios=3",
          (c_edit.get("images"), c_edit.get("videos"), c_edit.get("audios")) == (6, 3, 3), c_edit)
    c_lift = slot_counts("minimax-h3-lift")
    check("lift 生成档 frames=2 images=6", c_lift.get("frames") == 2 and c_lift.get("images") == 6, c_lift)

    check("生成档 reference_images 生效", field_applies("minimax-h3", "reference_images"))
    check("生成档 first_frame 生效", field_applies("minimax-h3", "first_frame"))
    check("编辑档 reference_videos 生效", field_applies("minimax-h3-edit", "reference_videos"))
    check("编辑档 first_frame 不生效", not field_applies("minimax-h3-edit", "first_frame"))
    check("fasth3 reference_images 不生效（纯帧槽）", not field_applies("fasth3", "reference_images"))

    spec = registry.resolve("minimax-h3")
    req = types.SimpleNamespace(first_frame="F.png", last_frame="L.png",
                                reference_images=["r1.png", "r2.png"], image=None)
    vals = pipeline._slot_values(spec, req)
    check("帧+参考混合取值", vals == ["F.png", "L.png", "r1.png", "r2.png", None, None, None, None], vals)
    req2 = types.SimpleNamespace(first_frame=None, last_frame=None,
                                 reference_images=["r1.png"], image=None)
    vals2 = pipeline._slot_values(spec, req2)
    check("纯参考取值（帧槽空缺不挡后续槽）",
          vals2 == [None, None, "r1.png", None, None, None, None, None], vals2)
    req3 = types.SimpleNamespace(first_frame=None, last_frame=None, reference_images=[], image=None)
    vals3 = pipeline._slot_values(spec, req3)
    check("全空取值（纯文生）", vals3 == [None] * 8, vals3)


def post(body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + "/v1/videos/generations", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:  # noqa: F821
        return e.code, json.loads(e.read())


async def http_section() -> None:
    print("== [B] HTTP：无槽字段一律 400 ==")
    app = web.Application()
    register_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    loop = asyncio.get_running_loop()

    async def call(body: dict) -> tuple[int, dict]:
        return await loop.run_in_executor(None, post, body)

    st, js = await call({"model": "fasth3", "prompt": "x", "reference_videos": ["v.mp4"]})
    check("fasth3 无视频槽传参考视频 → 400", st == 400 and "videos" in json.dumps(js), (st, js))
    st, js = await call({"model": "minimax-h3", "prompt": "x",
                         "reference_images": [f"r{i}.png" for i in range(7)]})
    check("生成档参考图超 6 张 → 400", st == 400 and "at most 6" in json.dumps(js), (st, js))
    st, js = await call({"model": "minimax-h3-edit", "prompt": "x", "first_frame": "f.png"})
    check("编辑档传首帧 → 400", st == 400 and "first_frame" in json.dumps(js), (st, js))
    st, js = await call({"model": "fasth3", "prompt": "x", "reference_images": ["r.png"]})
    check("fasth3 纯帧槽传参考图 → 400", st == 400, (st, js))

    await site.stop()
    await runner.cleanup()


async def main() -> int:
    offline_section()
    await http_section()
    print(f"\n {'=' * 46}\n  {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
