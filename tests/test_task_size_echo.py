"""异步任务回执要带上 size —— 轮询完不必再自己 ffprobe 落盘文件。

为什么需要这个测试：
    `size`（实际输出尺寸 "WxH"）早就存在于同步响应里，而异步链路把 `VideoResponse.model_dump()`
    整份存进任务表，REST 回执却只透出 `output.data[]` —— size 在最后一米被丢掉，于是「异步生成
    完想知道分辨率」只能对着产物跑一次 ffprobe。丢字段是**静默**的：响应结构合法、status 正常、
    什么都不报错，只有缺少键这一件事看得出来，所以必须机械断言兜住。

覆盖两条产品线（同一个 handler 挂两个命名空间）：
    GET /v1/videos/tasks/{id}   GET /v1/images/tasks/{id}

不碰正在运行的 ComfyUI：独立 aiohttp 实例绑 8198 回环，任务直接写内存任务表。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/tests/test_task_size_echo.py
NODE = Path(__file__).resolve().parent.parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from gateway.routes import register_routes  # noqa: E402
from gateway.tasks import task_store  # noqa: E402

PORT = 8199
BASE = f"http://127.0.0.1:{PORT}"

# 送进任务表的形状与 VideoResponse.model_dump(exclude_none=True) 一致
RESULT_WITH_SIZE = {
    "created": 1700000000,
    "data": [{"url": "/view?filename=out.mp4&type=output"}],
    "seed": 12345,
    "size": "1376x768",
}
RESULT_NO_SIZE = {"created": 1700000000, "data": [{"url": "/view?filename=out.mp4&type=output"}]}


async def main() -> int:
    app = web.Application()
    register_routes(app)

    t_ok = task_store.create("size-ok-0000000000", model="minimax-h3")
    task_store.complete(t_ok.id, RESULT_WITH_SIZE)
    t_no = task_store.create("size-none-00000000", model="minimax-h3")
    task_store.complete(t_no.id, RESULT_NO_SIZE)
    t_run = task_store.create("size-run-00000000", model="minimax-h3")
    t_run.status = "processing"
    t_bad = task_store.create("size-fail-0000000", model="minimax-h3")
    task_store.fail(t_bad.id, "boom", 500)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
        if not ok:
            failures += 1

    def get(path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:  # noqa: F821
            return e.code, json.loads(e.read())

    # urlopen 是阻塞的，必须丢进线程池：直接在事件循环里调会把服务器自己堵死
    async def fetch(path: str) -> tuple[int, dict]:
        return await asyncio.get_running_loop().run_in_executor(None, get, path)

    # 1. completed：size 与同步响应同位（顶层），且 data[].url 照旧被绝对化
    st, js = await fetch(f"/v1/videos/tasks/{t_ok.id}")
    check("completed 回执 status=200", st == 200, f"status={st}")
    check("completed 回执带顶层 size", js.get("size") == "1376x768", f"size={js.get('size')!r}")
    check("output.data[0].url 已绝对化",
          str((((js.get("output") or {}).get("data") or [{}])[0]).get("url", "")).startswith(BASE + "/"),
          str(js.get("output")))

    # 2. images 命名空间复用同一 handler，行为必须一致
    _, js_img = await fetch(f"/v1/images/tasks/{t_ok.id}")
    check("images 命名空间同位带 size", js_img.get("size") == "1376x768", f"size={js_img.get('size')!r}")

    # 3. 结果里本来就没有 size 时（如旧记录）不要编一个 null 出来
    _, js_no = await fetch(f"/v1/videos/tasks/{t_no.id}")
    check("无 size 的结果不带 size 键", "size" not in js_no, f"payload={js_no}")

    # 4. 未终态 / 失败态不伪造尺寸：产物还没落盘，谈不上分辨率
    _, js_run = await fetch(f"/v1/videos/tasks/{t_run.id}")
    check("in_progress 不带 size", js_run.get("status") == "in_progress" and "size" not in js_run, str(js_run))
    _, js_bad = await fetch(f"/v1/videos/tasks/{t_bad.id}")
    check("failed 不带 size", js_bad.get("status") == "failed" and "size" not in js_bad, str(js_bad))
    check("failed 仍带 error", (js_bad.get("error") or {}).get("message") == "boom", str(js_bad.get("error")))

    await site.stop()
    await runner.cleanup()
    print(f"\n {'=' * 46}\n  {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
