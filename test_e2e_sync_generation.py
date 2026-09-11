"""端到端：独立实例跑真代码，验证「同步生成也会记进任务表、产物链接真能打开」。

会真的提交一次生图（z-image-turbo 512x512，约 10~30s GPU），因此不进常规测试集；
改动了任务记录（handlers.generate_tracked / viewer.tasks）时手动跑一遍。
"""

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/test_e2e_sync_generation.py
NODE = Path(__file__).resolve().parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from gateway.config import settings  # noqa: E402
from gateway.registry import registry  # noqa: E402
from gateway.routes import register_routes  # noqa: E402

PORT = 8201
BASE = f"http://127.0.0.1:{PORT}"


async def main() -> int:
    # 独立实例不会自动加载模型（那是节点 __init__.py 干的），这里手动补上
    registry.load(settings.models_file, settings.workflows_dir, settings.default_model)

    app = web.Application()
    register_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    body = json.dumps({
        "model": "z-image-turbo",
        "prompt": "a red apple on a wooden table, studio light",
        "size": "512x512",
        "seed": 4242,
        "response_format": "url",
    }).encode()

    loop = asyncio.get_running_loop()

    def post(data):
        req = urllib.request.Request(
            BASE + "/v1/images/generations", data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read())

    def get(url):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    print("== 同步生图（走 REST 网关）==")
    st, payload = await loop.run_in_executor(None, post, body)
    print("   HTTP", st, "| seed:", payload.get("seed"))
    print("   data[0]:", payload.get("data"))

    print("== 任务表是否记下这条同步请求 ==")
    _, raw = await loop.run_in_executor(None, get, BASE + "/roundabout/view/tasks")
    tasks = json.loads(raw)["tasks"]
    print("   任务数:", len(tasks))
    for t in tasks:
        print("   ", t["status"], t["model"], t["id"][:8], t.get("url"), t.get("error"))
    assert len(tasks) == 1 and tasks[0]["status"] == "completed", tasks

    print("== 产物链接真的能打开吗 ==")
    # 独立实例不提供 ComfyUI 原生的 /view，所以按 query 去真 ComfyUI 上取文件，
    # 验证「链接里的 filename/subfolder 是真实产物」。
    out = tasks[0].get("url") or ""
    assert "/view?" in out, out
    real = f"http://127.0.0.1:8188/view?{out.split('/view?', 1)[1]}"
    st, _ = await loop.run_in_executor(None, get, real)
    print("   HTTP", st, real)
    assert st == 200, st

    print("== 生成中途失败也要留痕 ==")
    # 给文生图模型塞输入图 → 在 pipeline 里被拒，属于「已开工」的失败，应记录
    bad = json.dumps({
        "model": "z-image-turbo", "prompt": "x",
        "image": "data:image/png;base64,bm90LWEtcG5n",
    }).encode()
    try:
        await loop.run_in_executor(None, post, bad)
    except urllib.error.HTTPError as exc:
        print("   HTTP", exc.code, json.loads(exc.read()).get("error", {}).get("message", "")[:70])
    _, raw = await loop.run_in_executor(None, get, BASE + "/roundabout/view/tasks")
    failed = [x for x in json.loads(raw)["tasks"] if x["status"] == "failed"]
    print("   失败记录:", [(x["model"], (x.get("error") or "")[:40]) for x in failed])
    assert failed, "失败请求没有留下记录"

    await runner.cleanup()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
