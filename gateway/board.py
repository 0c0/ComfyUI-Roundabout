"""任务看板（view.html 顶部那块无限画布）的后端。

**它解决什么**：agent 出一堆图/视频时，逐个把文件拉给用户看既乱又费事；改为 agent 把产出
**钉到一块画布上**（可按语义排布：分镜顺序、A/B 对照、按角色分组），用户在页面上一眼看全。
切换任务时 agent 主动清空，清空的内容**归档进历史**，随时可回看。

与右下角「任务进度」面板的分工：那边是网关任务表的自动流水（排队/执行中/失败），
这边是 **agent 主动挑选后钉上来**的成果 —— 只有 agent 认为值得看的才出现。

**画布**：每张卡片有 x/y（可选 w/h）。不传坐标时后端按网格找空位自动排（尊重已显式摆放的卡片）。
坐标允许负数，前端是一个可平移/缩放的无限画布。

**持久化**：落在 `节点/.cache/board.json`（已在 .gitignore 里，属运行期数据）。重启 ComfyUI
后看板与历史都还在 —— 历史本来就是给人/给下一个会话回看用的。

端点：
  GET    /roundabout/view/board                   当前看板
  POST   /roundabout/view/board/items             钉入
  DELETE /roundabout/view/board/items/{id}        删单条
  DELETE /roundabout/view/board                   清空（带 label 归档）
  GET    /roundabout/view/board/history           历史归档列表
  GET    /roundabout/view/board/history/{id}      某份归档详情
  POST   /roundabout/view/board/history/{id}/load 把某份归档载入当前看板
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from aiohttp import web

from .admin import gateway_handler
from .config import settings
from .errors import APIError
from .tasks import task_store
from .viewer import _ROOTS, _absolutize_product, _authorize, _kind

log = logging.getLogger("roundabout.board")

# ---- 容量 ----
MAX_ITEMS = 60          # 当前看板容量，超出丢最旧的
MAX_HISTORY = 20        # 历史归档份数，超出丢最旧的
TITLE_MAX = 120
NOTE_MAX = 400
LABEL_MAX = 80
# text = 无产物的说明卡（agent 可以钉「这轮做到哪了」这类进度说明，不必非得有文件）
# dir  = 指向 input/output 内的目录：没有可预览的产物，点它跳去文件列表并进入该目录
KINDS = ("image", "video", "audio", "file", "dir", "text")

# ---- 画布几何：不传坐标时按这套网格找空位 ----
CARD_W = 200
CARD_H = 168
GAP_X = 16
GAP_Y = 16
MAX_COLS = 6            # 自动排布一行的槽位数，超出换行
COORD_LIMIT = 100000    # 坐标绝对值上限（防手滑填出天文数字把画布撑飞）
SIZE_MIN, SIZE_MAX = 40, 2000

DATA_FILE = Path(__file__).resolve().parent.parent / ".cache" / "board.json"

_items: list[dict[str, Any]] = []
_history: list[dict[str, Any]] = []


# ------------------------------------------------------------------ 落盘
def _load() -> None:
    """启动时读回看板与历史。文件坏了/不存在就当空看板，不连带启动失败。"""
    global _items, _history
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("board data unreadable (%s), starting empty: %s", DATA_FILE, exc)
        return
    if not isinstance(data, dict):
        return
    _items = [i for i in (data.get("items") or []) if isinstance(i, dict)][-MAX_ITEMS:]
    _history = [h for h in (data.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:]
    log.info("board loaded: %d item(s), %d archived board(s)", len(_items), len(_history))


def _save() -> None:
    """变更后落盘。写临时文件再原子替换，避免中途崩掉留下半截 JSON。"""
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": 1, "items": _items, "history": _history}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(DATA_FILE)
    except Exception as exc:  # noqa: BLE001 - 落盘失败不该让生成/看板操作失败
        log.warning("board save failed: %s", exc)


_load()


# ------------------------------------------------------------------ 工具
def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    """数值参数：空/非法 → 默认；否则钳到 [lo, hi]。"""
    if value is None or value == "":
        return default
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise APIError(f"'{value}' is not a number.", param="geometry")
    return max(lo, min(hi, n))


def _base_for(request: web.Request | None) -> str:
    """产物地址前缀。

    浏览器打开页面时用**请求 origin**（不是 comfy_base_url），否则远程访问者的链接会被
    钉到 127.0.0.1 上打不开；MCP 工具没有 request，退回 public_base_url / comfy_base_url。
    """
    if request is not None:
        return (settings.public_base_url or str(request.url.origin())).rstrip("/")
    return (settings.public_base_url or settings.comfy_base_url).rstrip("/")


def _product_of_task(task_id: str) -> tuple[str, str | None]:
    """任务 id → (产物原文, 模型名)。任务不存在直接 404，别让 agent 以为钉成功了。"""
    task = task_store.get(task_id)
    if not task:
        raise APIError(
            f"Unknown task id '{task_id}'.", status_code=404, code="task_not_found", param="task_id"
        )
    data = (task.result or {}).get("data") or []
    datum = data[0] if data else {}
    product = datum.get("url") or datum.get("path")
    return (product if isinstance(product, str) else ""), task.model


def _name_of(url: str) -> str:
    """产出地址 → 文件名。

    ComfyUI 的 `/view` 把真文件名放在查询串里（`/view?filename=a.mp4&type=output`），
    路径后缀永远是 `/view`，直接取 suffix 会把 mp4 判成「无扩展名」。
    """
    m = re.search(r"[?&]filename=([^&]+)", url or "")
    raw = unquote(m.group(1)) if m else (url or "").split("?", 1)[0]
    return Path(raw).name or ""


def _guess_kind(url: str | None, explicit: str) -> str:
    if explicit:
        if explicit not in KINDS:
            raise APIError(f"'kind' must be one of {list(KINDS)}.", param="kind")
        return explicit
    if not url:
        return "text"
    ext = Path(_name_of(url)).suffix.lower()
    return _kind(ext) if ext else "file"


def _thumb_for(url: str | None, kind: str) -> str | None:
    if kind != "image" or not url:
        return None
    # preview 转码只有 ComfyUI 原生 /view 支持；外链就拿原图当缩略图
    return f"{url}&preview=webp;70" if "/view?" in url else url


def _dir_target(source: str) -> dict[str, str] | None:
    """source 指向 input/output 内的**目录**时返回 `{"root":.., "path":..}`（相对于该 root），否则 None。

    不判的话会出两个问题：目录没有扩展名 ⇒ 被 `_guess_kind` 当成 `file`；而 `_local_view_url`
    又把它翻成一个对目录无效的 `/view?filename=子目录` 地址 ⇒ 卡片点上去毫无反应。
    先在这里认出来，前端才能「点卡片进入该目录」。
    """
    if not source or "://" in source or "?" in source:
        return None                     # 外链与 `/view?...` 都是文件粒度入口，不可能是目录
    try:
        target = Path(source).resolve()
    except OSError:
        return None
    if not target.is_dir():
        return None
    for root, resolve_root in _ROOTS.items():
        base = resolve_root()
        if not base:
            continue
        try:
            rel = target.relative_to(Path(base).resolve()).as_posix()
        except ValueError:
            continue
        return {"root": root, "path": "" if rel == "." else rel}
    return None


def _overlaps(x: float, y: float, w: float, h: float, item: dict[str, Any]) -> bool:
    """矩形相交判定（留 1px 容差，避免贴边摆放被判重叠）。"""
    return not (
        x + w <= item["x"] + 1 or item["x"] + item["w"] <= x + 1
        or y + h <= item["y"] + 1 or item["y"] + item["h"] <= y + 1
    )


def _auto_slot(w: float, h: float) -> tuple[float, float]:
    """行优先扫描第一个放得下的空位，跳过已被（显式或自动）占用的区域。"""
    for row in range(200):
        for col in range(MAX_COLS):
            x, y = col * (CARD_W + GAP_X), row * (CARD_H + GAP_Y)
            if not any(_overlaps(x, y, w, h, i) for i in _items):
                return x, y
    return 0.0, 0.0


# ------------------------------------------------------------------ 看板操作
def pin(
    *,
    title: str = "",
    url: str = "",
    path: str = "",
    task_id: str = "",
    note: str = "",
    kind: str = "",
    model: str = "",
    x: Any = None,
    y: Any = None,
    w: Any = None,
    h: Any = None,
    request: web.Request | None = None,
) -> dict[str, Any]:
    """钉一张卡片到画布，返回落库后的条目。

    产物来源三选一，优先级 **url > path > task_id**（显式优先，任务 id 兜底）：
      - `url`：http(s) 地址 / ComfyUI 的 `/view?...` / input|output 内的磁盘路径
      - `path`：磁盘绝对路径（在 input|output 内才翻译成 /view，否则只回显原文、不可点）
      - `task_id`：网关任务 id，取该任务的产物
    三者都空时退化为纯文本卡（kind=text），让 agent 也能钉进度说明；
    连 note 都没有 → 400（空卡片没有意义）。

    `source` 指向 input/output 内的**目录**时自动落成 kind=dir，并附 `dir: {root, path}`
    供前端「点卡片进入该目录」；显式 `kind=dir` 但路径不是那种目录 → 400（别钉出点了没反应的卡）。

    位置：`x`/`y` 给了就摆在那个坐标（画布原点在左上，允许负数），没给就自动找空位。
    """
    source = url or path
    origin = "url" if url else ("path" if path else ("task_id" if task_id else "none"))
    task_model: str | None = None
    if task_id:
        # 显式给了 url/path 时 task_id 只用来补模型名，不抢产物
        product, task_model = _product_of_task(task_id)
        if not source:
            source = product
            origin = "task_id"

    dir_target = _dir_target(source)
    if kind == "dir" and not dir_target:
        raise APIError(
            "'kind=dir' needs a 'url'/'path' pointing at a folder inside input/output.",
            param="kind",
        )
    # 目录没有可打开的 /view 地址（那个地址对目录无效），只留 dir 定位给前端跳转
    resolved = None if dir_target else (
        _absolutize_product(_base_for(request), source) if source else None
    )
    item_kind = "dir" if dir_target else _guess_kind(resolved or source, kind)
    title_clean = _clip(title, TITLE_MAX)
    if not title_clean:
        # 没标题就退回文件名 / 任务号，避免页面上一排「未命名」
        name = _name_of(source or "")
        title_clean = (name or task_id or _clip(note, TITLE_MAX) or "产出")[:TITLE_MAX]

    note_clean = _clip(note, NOTE_MAX)
    if not source and not note_clean:
        raise APIError(
            "Nothing to pin: pass one of url / path / task_id, or a note.",
            param="title",
        )

    card_w = _num(w, CARD_W, SIZE_MIN, SIZE_MAX)
    card_h = _num(h, CARD_H, SIZE_MIN, SIZE_MAX)
    # 两个坐标都给了才算「显式摆放」；只给一个是手滑，退回自动排布
    if x is not None and x != "" and y is not None and y != "":
        card_x = _num(x, 0, -COORD_LIMIT, COORD_LIMIT)
        card_y = _num(y, 0, -COORD_LIMIT, COORD_LIMIT)
    else:
        card_x, card_y = _auto_slot(card_w, card_h)

    item: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "title": title_clean,
        "kind": item_kind,
        "url": resolved,
        "thumb": _thumb_for(resolved, item_kind),
        "note": note_clean,
        "model": _clip(model or task_model or "", 60),
        "created": time.time(),
        "origin": origin,
        "task_id": task_id or None,
        "x": card_x,
        "y": card_y,
        "w": card_w,
        "h": card_h,
    }
    # 目录卡：带上可跳转的目标（root + 相对路径），前端点了就切到文件列表并进这个目录
    if dir_target:
        item["dir"] = dir_target
    # 产物落在 input/output 之外（自定义输出目录）：地址不可用，只把原路径给用户看
    if source and not resolved and not dir_target:
        item["path"] = source

    _items.append(item)
    dropped = 0
    if len(_items) > MAX_ITEMS:
        dropped = len(_items) - MAX_ITEMS
        del _items[:dropped]
    _save()
    log.info("board pinned %s (%s) -> %s%s", item["id"], item_kind, title_clean,
             f", dropped {dropped} oldest" if dropped else "")
    return item


def snapshot() -> list[dict[str, Any]]:
    """当前看板（浅拷贝，避免调用方改到内部状态）。"""
    return [dict(i) for i in _items]


def remove(item_id: str) -> bool:
    for i, item in enumerate(_items):
        if item["id"] == item_id:
            del _items[i]
            _save()
            return True
    return False


def archive(label: str = "") -> dict[str, Any] | None:
    """把当前看板**归档进历史**并清空。空看板不产生归档。"""
    if not _items:
        return None
    entry = {
        "id": uuid.uuid4().hex[:12],
        "label": _clip(label, LABEL_MAX) or f"未命名 · {time.strftime('%H:%M:%S')}",
        "created": time.time(),
        "count": len(_items),
        "items": [dict(i) for i in _items],
    }
    _history.append(entry)
    if len(_history) > MAX_HISTORY:
        del _history[:len(_history) - MAX_HISTORY]
    _items.clear()
    _save()
    log.info("board archived %s (%d item(s))", entry["id"], entry["count"])
    return entry


def load_archived(archive_id: str, archive_current: bool = True) -> dict[str, Any]:
    """把某份归档载入当前看板。当前看板非空时先自动归档，避免内容被静默覆盖。"""
    entry = next((h for h in _history if h["id"] == archive_id), None)
    if not entry:
        raise APIError(f"No archived board with id '{archive_id}'.", status_code=404, code="archive_not_found")
    auto = archive() if (archive_current and _items) else None
    _items.clear()
    _items.extend(dict(i) for i in (entry.get("items") or []))
    _save()
    return {"loaded": entry["id"], "count": len(_items), "auto_archived": auto["id"] if auto else None}


# ------------------------------------------------------------------ 端点
def _json_body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise APIError("Request body must be a JSON object.", param="body")
    return payload


@gateway_handler
async def board(request: web.Request) -> web.Response:
    """当前看板（前端轮询）。"""
    _authorize(request)
    items = snapshot()
    return web.json_response({
        "server_time": time.time(),
        "items": items,
        "count": len(items),
        "history_count": len(_history),
    })


@gateway_handler
async def pin_item(request: web.Request) -> web.Response:
    """钉一张卡片。body: {title, url|path|task_id, note?, kind?, model?, x?, y?, w?, h?}。"""
    _authorize(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 非 JSON / 空 body 都归到「body 不是对象」
        payload = None
    body = _json_body(payload)
    item = pin(
        title=body.get("title") or "",
        url=body.get("url") or "",
        path=body.get("path") or "",
        task_id=body.get("task_id") or "",
        note=body.get("note") or "",
        kind=body.get("kind") or "",
        model=body.get("model") or "",
        x=body.get("x"), y=body.get("y"), w=body.get("w"), h=body.get("h"),
        request=request,
    )
    return web.json_response({"ok": True, "item": item, "count": len(_items)})


@gateway_handler
async def remove_item(request: web.Request) -> web.Response:
    """删单条（画布卡片右上角的 ×）。"""
    _authorize(request)
    item_id = request.match_info.get("id") or ""
    if not remove(item_id):
        raise APIError(f"No board item with id '{item_id}'.", status_code=404, code="board_item_not_found")
    return web.json_response({"ok": True, "id": item_id, "count": len(_items)})


@gateway_handler
async def clear_board(request: web.Request) -> web.Response:
    """清空并归档：`?label=xxx` 或 body {label} 给这份归档起个名字（回看时好认）。"""
    _authorize(request)
    label = request.query.get("label") or ""
    if not label:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - DELETE 不带 body 是常态
            payload = None
        if isinstance(payload, dict):
            label = payload.get("label") or ""
    entry = archive(label)
    return web.json_response({
        "ok": True,
        "cleared": entry["count"] if entry else 0,
        "archived": entry,
        "count": len(_items),
    })


@gateway_handler
async def history(request: web.Request) -> web.Response:
    """历史归档列表（只回摘要，不含卡片内容；详情走 history/{id}）。"""
    _authorize(request)
    return web.json_response({
        "history": [
            {"id": h["id"], "label": h["label"], "created": h["created"], "count": h["count"]}
            for h in reversed(_history)
        ],
        "count": len(_history),
    })


@gateway_handler
async def history_detail(request: web.Request) -> web.Response:
    """某份归档的完整内容（含卡片与坐标，可直接画出来）。"""
    _authorize(request)
    archive_id = request.match_info.get("id") or ""
    entry = next((h for h in _history if h["id"] == archive_id), None)
    if not entry:
        raise APIError(f"No archived board with id '{archive_id}'.", status_code=404, code="archive_not_found")
    return web.json_response({"archive": entry, "board": board_snapshot_payload()})


def board_snapshot_payload() -> dict[str, Any]:
    """当前看板快照（history_detail 顺带返回，省前端一次请求）。"""
    items = snapshot()
    return {"items": items, "count": len(items)}


@gateway_handler
async def history_load(request: web.Request) -> web.Response:
    """把某份归档载入当前看板（当前非空会自动归档，不静默覆盖）。"""
    _authorize(request)
    archive_id = request.match_info.get("id") or ""
    result = load_archived(archive_id)
    return web.json_response({"ok": True, **result, "items": snapshot()})
