"""任务看板后端：钉入 / 排布 / 归档 / 回看。

独立 aiohttp 实例（绑 8198 回环），不影响正在运行的 ComfyUI。
看板默认落盘到 节点/.cache/board.json，测试里改指临时文件，避免污染真实看板。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/tests/
NODE = Path(__file__).resolve().parent.parent   # 节点目录
ROOT = NODE.parent.parent                       # ComfyUI 根目录
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from gateway import board as board_mod  # noqa: E402
from gateway.pipeline import _comfy_output_root  # noqa: E402
from gateway.routes import register_routes  # noqa: E402
from gateway.tasks import task_store  # noqa: E402

PORT = 8198
BASE = f"http://127.0.0.1:{PORT}"

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
    if not ok:
        failures += 1


def req(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def main() -> int:
    # 隔离：看板数据写到临时文件，并把内存态清空（模块 import 时已读过真实文件）
    tmp = Path(tempfile.mkdtemp(prefix="rb_board_")) / "board.json"
    board_mod.DATA_FILE = tmp
    board_mod._items.clear()
    board_mod._history.clear()

    app = web.Application()
    register_routes(app)

    # 一条已完成的任务，供 task_id 来源使用（产物是 /view 相对地址）
    t = task_store.create("task-board-0001", model="z-image-turbo")
    task_store.complete(t.id, {"data": [{"url": "/view?filename=from_task.png&type=output"}]})

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    loop = asyncio.get_running_loop()

    print("== 空看板 ==")
    st, body = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board", None)
    d = json.loads(body)
    check("GET 返回 200", st == 200, f"status={st} {body[:200]}")
    check("items 为空", d.get("items") == [], str(d)[:200])
    check("count 为 0", d.get("count") == 0, str(d)[:200])

    print("== 钉入：url 形态 ==")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"title": "分镜 1", "url": "/view?filename=a.png&type=output", "note": "开场", "model": "z-image-turbo"},
    )
    d = json.loads(body)
    check("POST 返回 200", st == 200, f"status={st} {body[:300]}")
    it = d.get("item") or {}
    check("回 item.id", bool(it.get("id")), str(it)[:200])
    check("kind 推断为 image", it.get("kind") == "image", str(it.get("kind")))
    check("url 绝对化成 http", str(it.get("url", "")).startswith("http"), str(it.get("url")))
    check("thumb 带 preview 转码", "preview=webp" in str(it.get("thumb")), str(it.get("thumb")))
    check("title/note/model 落地", it.get("title") == "分镜 1" and it.get("note") == "开场"
          and it.get("model") == "z-image-turbo", str(it))
    check("有画布几何 x/y/w/h", all(k in it for k in ("x", "y", "w", "h")), str(it))
    id1 = it.get("id")

    print("== 自动排布不重叠 ==")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"title": "分镜 2", "url": "/view?filename=b.mp4&type=output"},
    )
    it2 = (json.loads(body) or {}).get("item") or {}
    check("第二条 kind 推断为 video", it2.get("kind") == "video", str(it2.get("kind")))
    a, b = it, it2
    overlap = not (a["x"] + a["w"] <= b["x"] + 1 or b["x"] + b["w"] <= a["x"] + 1
                   or a["y"] + a["h"] <= b["y"] + 1 or b["y"] + b["h"] <= a["y"] + 1)
    check("自动排布两张不重叠", not overlap, f"{a} {b}")

    print("== 显式坐标 ==")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"title": "手工摆", "url": "/view?filename=c.png&type=output", "x": 900, "y": -240, "w": 320, "h": 260},
    )
    it3 = (json.loads(body) or {}).get("item") or {}
    check("x/y 按传入落地", it3.get("x") == 900 and it3.get("y") == -240, str(it3))
    check("w/h 按传入落地", it3.get("w") == 320 and it3.get("h") == 260, str(it3))

    print("== 钉入：磁盘路径 ==")
    out_root = _comfy_output_root()
    fake = str(Path(out_root) / "board_probe.png")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"title": "本地文件", "path": fake},
    )
    it4 = (json.loads(body) or {}).get("item") or {}
    check("磁盘路径翻译成 /view", "/view?filename=board_probe.png&type=output" in str(it4.get("url")),
          str(it4.get("url")))

    print("== 钉入：task_id ==")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"title": "从任务取", "task_id": t.id},
    )
    it5 = (json.loads(body) or {}).get("item") or {}
    check("取到任务产物", "from_task.png" in str(it5.get("url")), str(it5)[:200])
    check("模型名从任务补上", it5.get("model") == "z-image-turbo", str(it5.get("model")))
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"task_id": "no-such-task"},
    )
    check("未知 task_id 返回 404", st == 404, f"status={st} {body[:200]}")

    print("== 纯文本卡与空卡 ==")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"title": "进度", "note": "5 张里已完成 3 张"},
    )
    it6 = (json.loads(body) or {}).get("item") or {}
    check("无产物时为 text 卡", it6.get("kind") == "text" and it6.get("url") is None, str(it6)[:200])
    st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/board/items", {"title": "空"})
    check("既无产物也无 note -> 400", st == 400, f"status={st} {body[:200]}")
    st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/board/items", "not-an-object")
    check("body 非对象 -> 400", st == 400, f"status={st} {body[:200]}")

    print("== 列表与单条删除 ==")
    st, body = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board", None)
    cur = json.loads(body)
    check("看板累计 6 张", cur.get("count") == 6, f"{cur.get('count')} {body[:200]}")
    st, _ = await loop.run_in_executor(None, req, "DELETE", f"/roundabout/view/board/items/{id1}", None)
    check("删单条 200", st == 200, f"status={st}")
    st, body = await loop.run_in_executor(None, req, "DELETE", f"/roundabout/view/board/items/{id1}", None)
    check("重复删 -> 404", st == 404, f"status={st}")

    print("== 清空即归档 ==")
    st, body = await loop.run_in_executor(None, req, "DELETE", "/roundabout/view/board?label=%E7%AC%AC%E4%B8%80%E8%BD%AE", None)
    d = json.loads(body)
    check("清空返回 200", st == 200, f"status={st} {body[:200]}")
    check("cleared 报清掉条数", d.get("cleared") == 5, str(d.get("cleared")))
    arch = d.get("archived") or {}
    check("归档带 label", arch.get("label") == "第一轮", str(arch)[:200])
    check("归档带 count", arch.get("count") == 5, str(arch)[:200])
    st, body = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board", None)
    check("看板已空", json.loads(body).get("count") == 0, body[:200])

    print("== 历史回看 ==")
    st, body = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board/history", None)
    h = json.loads(body)
    check("历史 1 条", h.get("count") == 1, str(h)[:200])
    row = (h.get("history") or [{}])[0]
    check("列表只回摘要不含 items", "items" not in row, str(row)[:200])
    aid = row.get("id")
    st, body = await loop.run_in_executor(None, req, "GET", f"/roundabout/view/board/history/{aid}", None)
    det = json.loads(body)
    check("详情含完整卡片", len((det.get("archive") or {}).get("items") or []) == 5, str(det)[:200])
    check("卡片保留画布坐标", all("x" in i for i in (det.get("archive") or {}).get("items", [])), str(det)[:200])
    st, _ = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board/history/nope", None)
    check("未知归档 -> 404", st == 404, f"status={st}")

    print("== 载入归档 ==")
    st, body = await loop.run_in_executor(None, req, "POST", f"/roundabout/view/board/history/{aid}/load", None)
    check("载入返回 200", st == 200, f"status={st} {body[:200]}")
    check("看板恢复 5 张", len((json.loads(body).get("items") or [])) == 5, body[:200])

    print("== 目录卡 ==")
    # 目录没有扩展名：不特判就会被当成 file，而且会被翻成一个对目录无效的 /view 地址
    # ⇒ 卡片点上去毫无反应（真机上就是这么暴露的）
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"title": "产出根", "path": str(out_root)},
    )
    itd = (json.loads(body) or {}).get("item") or {}
    check("目录落成 kind=dir", itd.get("kind") == "dir", str(itd)[:200])
    check("带 dir 定位 root=output / path=''",
          (itd.get("dir") or {}).get("root") == "output" and (itd.get("dir") or {}).get("path") == "",
          str(itd.get("dir")))
    check("目录卡不给 url（那个地址对目录无效）", itd.get("url") is None, str(itd.get("url")))
    check("目录卡不塞 path 字段（那是「产物在两个 root 之外」的标记）", "path" not in itd, str(itd)[:200])

    sub = Path(tempfile.mkdtemp(dir=str(out_root), prefix="_rb_dir_probe_"))
    try:
        st, body = await loop.run_in_executor(
            None, req, "POST", "/roundabout/view/board/items", {"title": "子目录", "path": str(sub)},
        )
        itd2 = (json.loads(body) or {}).get("item") or {}
        rel = (itd2.get("dir") or {}).get("path") or ""
        check("子目录带相对路径（前端据此跳转）", rel == sub.name, f"{rel!r} vs {sub.name!r}")
    finally:
        sub.rmdir()

    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"title": "假目录", "kind": "dir", "url": "/view?filename=a.png&type=output"},
    )
    check("显式 kind=dir 但并非目录 -> 400", st == 400, f"status={st} {body[:200]}")

    outside = Path(tempfile.mkdtemp(prefix="_rb_outside_"))
    try:
        st, body = await loop.run_in_executor(
            None, req, "POST", "/roundabout/view/board/items", {"title": "外部目录", "path": str(outside)},
        )
        itd3 = (json.loads(body) or {}).get("item") or {}
        check("两个 root 之外的目录不当目录卡", itd3.get("kind") == "file", str(itd3.get("kind")))
        check("外部目录保留原路径（前端据此提示不可预览）", itd3.get("path") == str(outside), str(itd3)[:200])
    finally:
        outside.rmdir()

    print("== 落盘 ==")
    check("数据文件已写出", tmp.is_file(), str(tmp))
    if tmp.is_file():
        saved = json.loads(tmp.read_text(encoding="utf-8"))
        check("落盘含 items 与 history", "items" in saved and "history" in saved, str(list(saved)))
        check("落盘 items 数与内存一致", len(saved["items"]) == len(board_mod._items),
              f'{len(saved["items"])} vs {len(board_mod._items)}')

    print("== 容量上限 ==")
    for i in range(board_mod.MAX_ITEMS + 3):
        board_mod.pin(title=f"x{i}", note="flood")
    check("超出容量丢弃最旧的", len(board_mod._items) == board_mod.MAX_ITEMS,
          f"{len(board_mod._items)}")
    check("最旧的已被挤掉", board_mod._items[0]["title"] != "x0", board_mod._items[0]["title"])

    await runner.cleanup()
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
