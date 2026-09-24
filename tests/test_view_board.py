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


def req(method: str, path: str, body: dict | None = None, headers: dict[str, str] | None = None):
    """默认从回环发起（就是「本机访问」）；要模拟远程访问就自己塞转发头。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data else {}
    if headers:
        hdrs.update(headers)
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=hdrs)
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
    ext_file = outside / "外部产物.mp4"
    ext_file.write_bytes(b"\x00" * 8)
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"title": "外部目录", "path": str(outside)},
    )
    itd3 = (json.loads(body) or {}).get("item") or {}
    check("两个 root 之外的目录不当目录卡", itd3.get("kind") == "file", str(itd3.get("kind")))
    check("外部目录保留原路径（可复制）", itd3.get("path") == str(outside), str(itd3)[:200])
    check("外部目录标记 ext 且 is_dir=true",
          (itd3.get("ext") or {}).get("is_dir") is True
          and (itd3.get("ext") or {}).get("path") == str(outside), str(itd3.get("ext")))
    check("外部目录不给 url（页面打不开它）", itd3.get("url") is None, str(itd3.get("url")))
    check("外部目录不出现在 dir 字段里（那不是「点进文件列表」那条路）",
          "dir" not in itd3, str(itd3)[:200])
    ext_dir_id = itd3.get("id")

    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"title": "外部文件", "path": str(ext_file)},
    )
    itf = (json.loads(body) or {}).get("item") or {}
    check("外部文件按扩展名给 kind（图标还能用）", itf.get("kind") == "video", str(itf.get("kind")))
    check("外部文件标记 ext 且 is_dir=false",
          (itf.get("ext") or {}).get("is_dir") is False
          and (itf.get("ext") or {}).get("path") == str(ext_file), str(itf.get("ext")))
    ext_file_id = itf.get("id")

    # 显式 kind=dir 现在也认「本机上的真目录」：判据是「它是不是目录」，不是「它在不在 root 里」
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"title": "显式外部目录", "kind": "dir", "path": str(outside)},
    )
    check("显式 kind=dir 指向外部真目录 -> 放行", st == 200, f"status={st} {body[:200]}")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"title": "谎称目录", "kind": "dir", "path": str(ext_file)},
    )
    check("显式 kind=dir 指向外部文件 -> 400", st == 400, f"status={st} {body[:200]}")

    # 相对路径按**进程 CWD** 解析（ComfyUI 的启动目录），不是调用方的语境 ⇒ 不认
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"title": "相对路径", "path": "some/rel/dir"},
    )
    itr = (json.loads(body) or {}).get("item") or {}
    check("相对路径不当外部", "ext" not in itr, str(itr)[:200])

    print("== 交给系统文件管理器打开 ==")
    # 绝不能在测试里真弹窗口：把真正干活的那个函数换成记录器（handler 是从模块全局取它的）
    calls: list[tuple[str, bool]] = []
    real_reveal = board_mod._reveal
    board_mod._reveal = lambda p, is_dir: calls.append((str(p), is_dir))
    try:
        st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/reveal", {"id": ext_dir_id})
        d = json.loads(body)
        check("本机请求 -> 200", st == 200, f"status={st} {body[:200]}")
        check("回传打开的路径与类型", d.get("path") == str(outside) and d.get("is_dir") is True, str(d)[:200])
        check("目录走「进入该目录」", calls == [(str(outside), True)], str(calls))

        st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/reveal", {"id": ext_file_id})
        check("文件走「定位并选中」", st == 200 and calls[-1] == (str(ext_file), False), f"status={st} {calls}")

        # 反向代理会把 remote 变成 127.0.0.1：带转发头就必须当远程，别被表象骗过
        st, body = await loop.run_in_executor(None, lambda: req(
            "POST", "/roundabout/view/reveal", {"id": ext_dir_id}, {"X-Forwarded-For": "10.0.0.9"}))
        d = json.loads(body)
        check("带转发头（= 远程）-> 403", st == 403, f"status={st} {body[:200]}")
        check("错误码是 reveal_not_local", (d.get("error") or {}).get("code") == "reveal_not_local", str(d)[:200])
        check("被拒时不会真去开窗口", len(calls) == 2, str(calls))

        st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/reveal", {"id": "no-such-card"})
        check("未知卡片 -> 404", st == 404, f"status={st} {body[:200]}")
        st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/reveal", {})
        check("缺 id -> 404", st == 404, f"status={st} {body[:200]}")
        st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/reveal", {"id": itd.get("id")})
        d = json.loads(body)
        check("页面内本来就能看的卡没有本地路径 -> 400", st == 400, f"status={st} {body[:200]}")
        check("错误码是 not_external", (d.get("error") or {}).get("code") == "not_external", str(d)[:200])
        check("这几发都没触发打开", len(calls) == 2, str(calls))

        # 钉下去之后文件没了：别弹一个不存在的路径，明确报 404
        gone = outside / "会被删掉.png"
        gone.write_bytes(b"\x00" * 4)
        st, body = await loop.run_in_executor(
            None, req, "POST", "/roundabout/view/board/items", {"title": "待删", "path": str(gone)},
        )
        gone_id = ((json.loads(body) or {}).get("item") or {}).get("id")
        gone.unlink()
        st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/reveal", {"id": gone_id})
        d = json.loads(body)
        check("路径已不存在 -> 404", st == 404, f"status={st} {body[:200]}")
        check("错误码是 path_missing", (d.get("error") or {}).get("code") == "path_missing", str(d)[:200])
    finally:
        board_mod._reveal = real_reveal
        ext_file.unlink()
        outside.rmdir()

    print("== 落盘 ==")
    check("数据文件已写出", tmp.is_file(), str(tmp))
    if tmp.is_file():
        saved = json.loads(tmp.read_text(encoding="utf-8"))
        check("落盘含 items 与 history", "items" in saved and "history" in saved, str(list(saved)))
        check("落盘 items 数与内存一致", len(saved["items"]) == len(board_mod._items),
              f'{len(saved["items"])} vs {len(board_mod._items)}')

    print("== 归档里的卡片不能打开 ==")
    # 「能打开什么」恒等于**当前看板**上有什么：清空之后就不再触发 —— 等于一条天然的撤销路径
    st, body = await loop.run_in_executor(
        None, req, "DELETE", "/roundabout/view/board?label=%E5%A4%96%E9%83%A8%E5%9C%BA%E6%99%AF", None)
    archived_now = (json.loads(body) or {}).get("archived") or {}
    check("清空到第二份归档", (archived_now.get("count") or 0) > 0, str(archived_now)[:200])
    st, body = await loop.run_in_executor(None, req, "POST", "/roundabout/view/reveal", {"id": ext_dir_id})
    check("已归档的卡片 -> 404", st == 404, f"status={st} {body[:200]}")

    print("== 删除归档 ==")
    st, body = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board/history", None)
    before = json.loads(body)
    n0 = before.get("count") or 0
    drop_id = ((before.get("history") or [{}])[0]).get("id")
    st, body = await loop.run_in_executor(None, req, "DELETE", f"/roundabout/view/board/history/{drop_id}", None)
    d = json.loads(body)
    check("删一份归档 -> 200", st == 200, f"status={st} {body[:200]}")
    check("回传被删掉的卡片数", (d.get("removed") or 0) > 0, str(d)[:200])
    check("剩余归档数 -1", d.get("count") == n0 - 1, f'{d.get("count")} vs {n0}')
    st, body = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board/history", None)
    after = json.loads(body)
    check("列表里已经没有它", drop_id not in [h["id"] for h in (after.get("history") or [])], str(after)[:200])
    check("列表计数同步", after.get("count") == n0 - 1, str(after.get("count")))
    st, _ = await loop.run_in_executor(None, req, "GET", f"/roundabout/view/board/history/{drop_id}", None)
    check("删掉的归档查详情 -> 404", st == 404, f"status={st}")
    st, body = await loop.run_in_executor(None, req, "DELETE", f"/roundabout/view/board/history/{drop_id}", None)
    check("重复删 -> 404", st == 404, f"status={st} {body[:200]}")
    check("删除已落盘", drop_id not in tmp.read_text(encoding="utf-8"), "落盘文件里还留着它")

    print("== 批量钉：一次往返、一次落盘 ==")
    # 落盘是这条链路的吞吐瓶颈：逐张 pin 会把整个 board.json 重写 N 遍（本板满容量时百 KB 级）。
    # 把 _save 换成计数器 —— 「一次落盘」这种承诺只有在能数出次数时才算判据。
    saves: list[int] = []
    real_save = board_mod._save
    board_mod._save = lambda: saves.append(1)
    try:
        n0 = len(board_mod._items)
        st, body = await loop.run_in_executor(
            None, req, "POST", "/roundabout/view/board/items",
            {"items": [
                {"title": "分镜 1", "url": "/view?filename=s1.png&type=output"},
                {"title": "分镜 2", "url": "/view?filename=s2.png&type=output", "note": "转场"},
                {"title": "分镜 3", "note": "纯文本卡"},
            ]},
        )
        d = json.loads(body)
        check("批量 POST -> 200", st == 200, f"status={st} {body[:200]}")
        check("added 报 3", d.get("added") == 3, str(d)[:200])
        check("看板一次多 3 张", len(board_mod._items) == n0 + 3, str(len(board_mod._items)))
        check("整批只落盘一次", len(saves) == 1, f"{len(saves)} 次")
        briefs = d.get("items") or []
        check("回执是精简卡（不带 note / task_id / thumb）",
              bool(briefs) and all(set(b) <= {"id", "title", "kind", "url", "model", "x", "y"} for b in briefs),
              str(briefs)[:200])
        check("回执顺序 = 数组顺序",
              [b.get("title") for b in briefs] == ["分镜 1", "分镜 2", "分镜 3"], str(briefs)[:200])
        # 落位也按数组顺序：第 1 张先占位、后面的避开它 ⇒ 三个坐标互不相同。
        # 若实现改成「先全部构造再统一 append」，三张会抢到同一个空位、这里变红。
        coords = [(b.get("x"), b.get("y")) for b in briefs]
        check("同批按数组顺序落位（坐标互不相同）", len(set(coords)) == 3, str(coords))
    finally:
        board_mod._save = real_save

    print("== 批量：整批原子 ==")
    n_before = len(board_mod._items)
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"items": [{"title": "好卡", "note": "ok"}, {"title": "坏卡"}]},   # 第 2 张既无产物也无 note
    )
    check("批里有不合法项 -> 400", st == 400, f"status={st} {body[:200]}")
    check("整批回滚（看板条目数不变）", len(board_mod._items) == n_before,
          f"{len(board_mod._items)} vs {n_before}")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items",
        {"items": [{"title": "x", "note": "y"}], "url": "/view?filename=q.png&type=output"},
    )
    check("items 与单卡字段同传 -> 400（不替调用方猜哪边生效）", st == 400, f"status={st} {body[:200]}")
    st, body = await loop.run_in_executor(
        None, req, "POST", "/roundabout/view/board/items", {"items": []},
    )
    check("items 为空数组 -> 400", st == 400, f"status={st} {body[:200]}")

    print("== MCP 侧：批量、latest、指纹、剥 thumb ==")
    import mcp_server as mcp_mod  # noqa: PLC0415 - 走到这里才需要，且共用同一份 gateway.board 内存态

    res = await mcp_mod.pin_view_item(items=[{"title": "m1", "note": "a"}, {"title": "m2", "note": "b"}])
    check("MCP 批量钉 -> ok / added", res.get("ok") is True and res.get("added") == 2, str(res)[:200])
    check("MCP 批量回执精简（不带 note）",
          all("note" not in b for b in res.get("items") or []), str(res.get("items"))[:200])
    res = await mcp_mod.pin_view_item(items=[{"title": "m3", "note": "c"}], title="撞车")
    check("MCP 批量与单卡字段同传 -> ok=false（不静默）",
          res.get("ok") is False and "drop the single-card fields" in (res.get("error") or ""), str(res)[:200])

    res = await mcp_mod.get_view_board()
    check("get_view_board 卡片无 thumb",
          bool(res.get("items")) and all("thumb" not in i for i in res["items"]), str(res)[:160])
    check("get_view_board 内联 history（默认 3 份，带指纹）",
          isinstance(res.get("history"), list) and len(res["history"]) >= 1
          and all(k in res["history"][0] for k in ("kinds", "preview", "models")),
          str(res.get("history"))[:200])
    check("内联 history 与 history_count 一致（不多不少）",
          len(res["history"]) == min(3, res["history_count"]),
          f'{len(res.get("history") or [])} vs {res.get("history_count")}')
    res = await mcp_mod.get_view_board(history_limit=0)
    check("history_limit=0 -> 不回 history", "history" not in res, str(list(res))[:200])

    st, body = await loop.run_in_executor(
        None, req, "DELETE", "/roundabout/view/board?label=%E6%89%B9%E9%87%8F%E5%9B%9E%E9%A1%BE", None)
    aid = ((json.loads(body) or {}).get("archived") or {}).get("id")

    res = await mcp_mod.get_view_board_history(aid)
    check("history 详情的卡片无 thumb",
          bool((res.get("archive") or {}).get("items"))
          and all("thumb" not in i for i in res["archive"]["items"]), str(res)[:160])
    check("history 详情同时带指纹（kinds/preview）",
          all(k in (res.get("archive") or {}) for k in ("kinds", "preview", "models")), str(res.get("archive"))[:160])

    res = await mcp_mod.load_view_board()
    check("load_view_board 不传 id -> 载回最近一份", res.get("ok") is True and res.get("loaded") == aid,
          str(res)[:200])
    check("载回的卡片无 thumb", all("thumb" not in i for i in res.get("items") or []), str(res)[:160])

    n_hist = board_mod.history_count()
    for h in list(board_mod._history):
        board_mod.remove_archive(h["id"])
    res = await mcp_mod.load_view_board()
    check("没有任何归档时 -> ok=false 且说清原因",
          res.get("ok") is False and "归档" in (res.get("error") or ""), str(res)[:200])
    board_mod._history.clear()
    check("（收尾）历史清空，后续段不受影响", board_mod.history_count() == 0, f"原有 {n_hist} 份")

    print("== 自动归档名与摘要指纹 ==")
    st, body = await loop.run_in_executor(None, req, "DELETE", "/roundabout/view/board", None)
    arch = (json.loads(body) or {}).get("archived") or {}
    label = arch.get("label") or ""
    check("不给 label 也不再叫「未命名」", label and "未命名" not in label, label)
    check("自动名带张数 + 类别", label.startswith(f"{arch.get('count')} 张 · ") and "image" in label, label)
    check("自动名带时间兜底", ":" in label, label)

    st, body = await loop.run_in_executor(None, req, "GET", "/roundabout/view/board/history", None)
    top = (json.loads(body).get("history") or [{}])[0]
    check("REST 摘要也带指纹", all(k in top for k in ("kinds", "preview", "models")), str(top)[:200])
    check("kinds 已去重", isinstance(top.get("kinds"), list) and len(top["kinds"]) == len(set(top["kinds"])),
          str(top.get("kinds")))
    check("preview 是真实标题（不是空串）",
          bool(top.get("preview")) and all(t.strip() for t in top["preview"]), str(top.get("preview"))[:160])
    check("摘要仍不含完整卡片", "items" not in top, str(list(top))[:200])

    board_mod.pin(title="x", note="y")
    explicit = board_mod.archive("第 2 轮 · 草稿")
    check("显式 label 优先于自动名", (explicit or {}).get("label") == "第 2 轮 · 草稿",
          str((explicit or {}).get("label")))

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
