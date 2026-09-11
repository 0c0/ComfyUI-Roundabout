"""view.html 后端冒烟：独立 aiohttp 实例注册网关路由，验证三个新端点。

不影响正在运行的 ComfyUI（绑 8199 回环）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

ROOT = r"E:\ai\ComfyUI-aki-v3\ComfyUI"
NODE = rf"{ROOT}\custom_nodes\ComfyUI-Roundabout"
for p in (NODE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from gateway.routes import register_routes  # noqa: E402
from gateway.tasks import task_store  # noqa: E402

PORT = 8199
BASE = f"http://127.0.0.1:{PORT}"


async def main() -> int:
    app = web.Application()
    register_routes(app)

    # 造几条假任务，验证任务面板（含成功态的 url 绝对化）
    t1 = task_store.create("task-aaaaaaaaaaaa", model="z-image-turbo")
    t1.status = "processing"
    t2 = task_store.create("task-bbbbbbbbbbbb", model="minimax-h3-turbo")
    task_store.complete(t2.id, {"data": [{"url": "/view?filename=x.mp4&type=output"}]})
    t3 = task_store.create("task-cccccccccccc", model="sdxl")
    task_store.fail(t3.id, "boom", 500)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    def get(path: str):
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:  # noqa: F821
            return e.code, e.read()

    loop = asyncio.get_running_loop()
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
        if not ok:
            failures += 1

    print("== GET /roundabout/view ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view")
    html = body.decode("utf-8", "replace")
    check("返回 200", st == 200, f"status={st}")
    check("是 HTML", html.lstrip().startswith("<!DOCTYPE html>"), html[:80])
    check("含任务面板", "任务进度" in html)
    check("含 Output/Input 切换", "tabInput" in html)

    print("== GET /roundabout/view/files (output) ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output")
    d = json.loads(body)
    check("返回 200", st == 200, f"status={st} {body[:200]}")
    check("root=output", d.get("root") == "output")
    check("有 root_path", bool(d.get("root_path")), str(d)[:200])
    check("有 dirs 或 files", bool(d.get("dirs") or d.get("files")), str(d)[:300])
    files = d.get("files") or []
    print(f"      dirs={len(d.get('dirs') or [])} files={len(files)} root_path={d.get('root_path')}")
    if files:
        f0 = files[0]
        check("file 有 name/size/kind/url", all(k in f0 for k in ("name", "size", "kind", "url")), str(f0))
        check("url 走 /view", f0["url"].startswith("/view?"), f0["url"])
        img = next((f for f in files if f["kind"] == "image"), None)
        if img:
            check("图片有 thumb", "preview=webp" in (img.get("thumb") or ""), str(img.get("thumb")))

    print("== GET /roundabout/view/files (input) ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=input")
    check("返回 200", st == 200, f"status={st} {body[:200]}")

    print("== 分页 ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&offset=0&limit=5")
    p1 = json.loads(body)
    check("返回 200", st == 200, f"status={st} {body[:200]}")
    check("limit 生效", len(p1.get("files") or []) <= 5, str(len(p1.get("files") or [])))
    check("回 total", isinstance(p1.get("total"), int), str(p1.get("total")))
    check("回 offset/limit", p1.get("offset") == 0 and p1.get("limit") == 5, str((p1.get("offset"), p1.get("limit"))))
    check("回 dir_count", isinstance(p1.get("dir_count"), int), str(p1.get("dir_count")))
    if (p1.get("total") or 0) > 5:
        check("has_more 为真", p1.get("has_more") is True, str(p1.get("has_more")))
        st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&offset=5&limit=5")
        p2 = json.loads(body)
        n1 = {f["name"] for f in p1["files"]}
        n2 = {f["name"] for f in p2["files"]}
        check("第二页不与第一页重复", not (n1 & n2), str(sorted(n1 & n2))[:120])
        check("第二页 offset 回显 5", p2.get("offset") == 5, str(p2.get("offset")))
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&limit=99999")
    p3 = json.loads(body)
    check("limit 上限钳制到 500", p3.get("limit") == 500, str(p3.get("limit")))
    st, _ = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&offset=-1")
    check("负 offset 被拒(400)", st == 400, f"status={st}")
    st, _ = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&limit=abc")
    check("非数字 limit 被拒(400)", st == 400, f"status={st}")

    print("== 排序 ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&limit=50")
    s1 = json.loads(body)
    mt = [f["mtime"] for f in s1.get("files") or []]
    check("默认按时间倒序", mt == sorted(mt, reverse=True), f"{mt[:4]}")
    check("回显 sort/order", s1.get("sort") == "mtime" and s1.get("order") == "desc", str((s1.get("sort"), s1.get("order"))))
    check("dirs 排在最前且不参与分页", isinstance(s1.get("dir_count"), int))
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&limit=50&sort=name&order=asc")
    s2 = json.loads(body)
    nm = [f["name"].lower() for f in s2.get("files") or []]
    check("sort=name 升序", nm == sorted(nm), f"{nm[:4]}")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&limit=50&sort=mtime&order=asc")
    s3 = json.loads(body)
    mt3 = [f["mtime"] for f in s3.get("files") or []]
    check("order=asc 时最旧的在前", mt3 == sorted(mt3), f"{mt3[:4]}")
    st, _ = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&sort=size")
    check("非法 sort 被拒(400)", st == 400, f"status={st}")
    st, _ = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&order=random")
    check("非法 order 被拒(400)", st == 400, f"status={st}")

    print("== 路径穿越防护 ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=output&path=../../custom_nodes")
    d2 = json.loads(body)
    check("被拒绝(400)", st == 400, f"status={st} {body[:200]}")
    check("错误码 bad_path", (d2.get("error") or {}).get("code") == "bad_path", str(d2)[:200])

    print("== 非法 root ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/files?root=etc")
    check("被拒绝(400)", st == 400, f"status={st}")

    print("== GET /roundabout/view/tasks ==")
    st, body = await loop.run_in_executor(None, get, "/roundabout/view/tasks")
    d3 = json.loads(body)
    check("返回 200", st == 200, f"status={st} {body[:200]}")
    tasks = d3.get("tasks") or []
    check("3 条任务", len(tasks) == 3, str(len(tasks)))
    by = {t["id"]: t for t in tasks}
    check("processing→in_progress", by["task-aaaaaaaaaaaa"]["status"] == "in_progress", str(by.get("task-aaaaaaaaaaaa")))
    check("succeeded→completed", by["task-bbbbbbbbbbbb"]["status"] == "completed", str(by.get("task-bbbbbbbbbbbb")))
    check("failed→failed", by["task-cccccccccccc"]["status"] == "failed", str(by.get("task-cccccccccccc")))
    check("成功任务 url 绝对化", str(by["task-bbbbbbbbbbbb"].get("url", "")).startswith("http"), str(by["task-bbbbbbbbbbbb"].get("url")))
    check("失败任务带 error", by["task-cccccccccccc"].get("error") == "boom")
    check("有 elapsed", isinstance(by["task-aaaaaaaaaaaa"].get("elapsed"), (int, float)))

    print("== tasks 附带 ComfyUI 队列 ==")
    q = d3.get("queue")
    check("有 queue 字段", isinstance(q, dict), str(q)[:200])
    if isinstance(q, dict):
        check("queue 有 running/pending", "running" in q and "pending" in q, str(q)[:200])
        print(f"      reachable={q.get('reachable')} running={len(q.get('running') or [])} pending={len(q.get('pending') or [])}")

    print("== 产物链接 ==")
    from gateway.pipeline import _comfy_output_root  # noqa: E402

    out_root = _comfy_output_root()
    probe = f"{out_root}\\_viewer_smoke_probe.png"
    with open(probe, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
    try:
        t4 = task_store.create("task-dddddddddddd", model="z-image-turbo")
        task_store.complete(t4.id, {"data": [{"path": probe}]})
        t5 = task_store.create("task-eeeeeeeeeeee", model="boogu-image-edit-turbo")
        task_store.complete(t5.id, {"data": [{"url": "https://example.com/a.png"}]})
        t6 = task_store.create("task-ffffffffffff", model="z-image")
        task_store.complete(t6.id, {"data": [{"path": r"C:\temp\not_in_comfy.png"}]})

        st, body = await loop.run_in_executor(None, get, "/roundabout/view/tasks")
        by2 = {t["id"]: t for t in json.loads(body)["tasks"]}
        local = str(by2["task-dddddddddddd"].get("url", ""))
        check("磁盘路径翻译成 /view 地址", "/view?filename=_viewer_smoke_probe.png&type=output" in local, local)
        check("绝对化带 http 前缀", local.startswith("http"), local)
        check("已是 http 的 url 原样返回", by2["task-eeeeeeeeeeee"].get("url") == "https://example.com/a.png",
              str(by2["task-eeeeeeeeeeee"].get("url")))
        outside = by2["task-ffffffffffff"]
        check("根目录外的路径不拼废链接", "url" not in outside and outside.get("path") == r"C:\temp\not_in_comfy.png",
              str(outside))
    finally:
        Path(probe).unlink(missing_ok=True)

    print("== 任务记录回收 ==")
    from gateway.tasks import RETENTION  # noqa: E402

    stale = task_store.create("task-stale-record", model="z-image")
    task_store.complete(stale.id, {"data": [{"url": "/view?filename=a.png&type=output"}]})
    stale.created_at -= RETENTION + 60
    running = task_store.create("task-still-running", model="z-image")
    running.created_at -= RETENTION + 60
    task_store.create("task-fresh-record", model="z-image")
    check("超过保留期的旧记录被回收", task_store.get("task-stale-record") is None)
    check("未完成的旧任务不被回收", task_store.get("task-still-running") is not None)

    await runner.cleanup()
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    import urllib.error  # noqa: E402

    raise SystemExit(asyncio.run(main()))
