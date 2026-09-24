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

**外部路径**：产物落在 input/output 之外时页面里既没有 /view 地址也读不到缩略图，唯一有意义
的动作是交给操作系统（`POST /roundabout/view/reveal` 拉起文件管理器）。为了不把这个能力开成
「任意路径都能打开」，端点**只接受看板上已存在的卡片 id** —— 可打开的路径集合恒等于 agent
自己钉过的那些；卡片被清空后这条路径也就随之失效。且只在本机访问时真的执行。

端点：
  GET    /roundabout/view/board                   当前看板
  POST   /roundabout/view/board/items             钉入
  DELETE /roundabout/view/board/items/{id}        删单条
  DELETE /roundabout/view/board                   清空（带 label 归档）
  GET    /roundabout/view/board/history           历史归档列表
  GET    /roundabout/view/board/history/{id}      某份归档详情
  POST   /roundabout/view/board/history/{id}/load 把某份归档载入当前看板
  POST   /roundabout/view/reveal                  在系统文件管理器里打开某张卡片指向的路径
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
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
PREVIEW_MAX = 3         # 归档摘要里带上前几张 title —— agent 靠 label 认不出内容时的兜底指纹
MODELS_MAX = 4          # 摘要里带上的模型名个数（同一轮多半就一两个模型，多了是噪音）
# text = 无产物的说明卡（agent 可以钉「这轮做到哪了」这类进度说明，不必非得有文件）
# dir  = 指向 input/output 内的目录：没有可预览的产物，点它跳去文件列表并进入该目录
#
# 注意 kind 只描述「产物是什么」，不描述「它在哪」。input/output 之外的路径（外部目录/外部文件）
# 的 kind 仍按扩展名推（目录因无扩展名落到 file），「外部」这件事由 `ext` 字段单独标记。
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


def _external_target(source: str) -> dict[str, Any] | None:
    """source 是 input/output **之外**的真实本地路径时返回定位信息，否则 None。

    为什么单独认这一类：它们的 /view 地址不存在（`_local_view_url` 只覆盖两个 root），
    缩略图也读不到，卡片在页面上就是一块空白。但「这个产物到底在哪」是个真实需求，
    而页面能做的只有一件事 —— 交给操作系统的文件管理器去打开。
    只认绝对路径：相对路径会按进程 CWD 解析，那是 ComfyUI 的启动目录，不是调用方的语境。
    """
    if not source or "://" in source or "?" in source:
        return None                     # 外链与 `/view?...` 都是页面能自己打开的入口
    if not Path(source).is_absolute():
        return None                     # 判「绝对」要看传入的原文，resolve() 的结果永远是绝对的
    try:
        target = Path(source).resolve()
    except OSError:
        return None
    if not (target.is_dir() or target.is_file()):
        return None
    # 落在这两个 root 里的不算外部：它们有 /view 或目录跳转的正路
    for resolve_root in _ROOTS.values():
        base = resolve_root()
        if not base:
            continue
        try:
            target.relative_to(Path(base).resolve())
            return None
        except ValueError:
            continue
    return {"path": str(target), "is_dir": target.is_dir()}


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
    # 200 行都放不下（理论上只有极限堆卡才会走到）：放到现有内容的下一行，别叠回 (0,0)
    bottom = max((i["y"] + i["h"] for i in _items), default=0.0)
    return 0.0, bottom + GAP_Y


def _covered_by(x: float, y: float, w: float, h: float) -> list[dict[str, Any]]:
    """与给定矩形重叠的已有卡（id+title）——显式坐标会遮挡时的回执警示。

    只在 append **之前**调用（probe 尚不在 _items 里），调用方负责这个顺序。
    """
    return [
        {"id": i["id"], "title": i.get("title", "")}
        for i in _items if _overlaps(x, y, w, h, i)
    ]


# ------------------------------------------------------------------ 看板操作
def _build_item(
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
    """构造一条卡片条目（只造，不 append、不落盘）—— `pin` 与 `pin_many` 共用这套判定。

    产物来源三选一，优先级 **url > path > task_id**（显式优先，任务 id 兜底）：
      - `url`：http(s) 地址 / ComfyUI 的 `/view?...` / input|output 内的磁盘路径
      - `path`：磁盘绝对路径（在 input|output 内翻译成 /view，否则标记为外部路径）
      - `task_id`：网关任务 id，取该任务的产物
    三者都空时退化为纯文本卡（kind=text），让 agent 也能钉进度说明；
    连 note 都没有 → 400（空卡片没有意义）。

    `source` 指向 input/output 内的**目录**时自动落成 kind=dir，并附 `dir: {root, path}`
    供前端「点卡片进入该目录」；显式 `kind=dir` 但路径不是目录 → 400（别钉出点了没反应的卡）。

    `source` 是本机 input/output **之外**的真实路径时附 `ext: {path, is_dir}`，标记为外部：
    页面里读不到它，前端改为「点卡片 → 确认 → 交给系统文件管理器打开」。

    位置：`x`/`y` 给了就摆在那个坐标（画布原点在左上，允许负数），没给就按当前 `_items` 找空位；
    批量钉时是逐张 append 后再造下一张，所以同一批也会自动错开。
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
    ext_target = None if dir_target else _external_target(source)
    if kind == "dir" and not (dir_target or (ext_target or {}).get("is_dir")):
        raise APIError(
            "'kind=dir' needs a 'url'/'path' pointing at a folder"
            " (inside input/output, or a real folder on this machine).",
            param="kind",
        )
    # 目录与外部路径都没有可打开的 /view 地址（那个地址对目录无效），只留定位给前端
    resolved = None if (dir_target or ext_target) else (
        _absolutize_product(_base_for(request), source) if source else None
    )
    if dir_target:
        item_kind = "dir"
    elif ext_target:
        # 外部路径按扩展名给图标；目录没有扩展名 ⇒ file。是否「外部」由 ext 字段表达，不看 kind
        item_kind = _guess_kind(source, "")
    else:
        item_kind = _guess_kind(resolved or source, kind)
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
    # 入参给了尺寸但被 clamp ⇒ 回执标 size_adjusted（w=1 当倍率用的典型手滑，别让 agent 无感）
    size_adjusted = False
    for raw, clamped in ((w, card_w), (h, card_h)):
        if raw in (None, ""):
            continue
        try:
            if abs(float(raw) - clamped) > 0.01:
                size_adjusted = True
        except (TypeError, ValueError):
            size_adjusted = True
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
    if size_adjusted:
        item["size_adjusted"] = True
    # 目录卡：带上可跳转的目标（root + 相对路径），前端点了就切到文件列表并进这个目录
    if dir_target:
        item["dir"] = dir_target
    # 外部路径：页面读不到它，带上定位让前端走「交给系统文件管理器」那条路
    if ext_target:
        item["ext"] = ext_target
    # 产物落在 input/output 之外（自定义输出目录）：地址不可用，只把原路径给用户看
    if source and not resolved and not dir_target:
        item["path"] = source

    return item


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

    判定规则（来源优先级 / 目录卡 / 外部卡 / 坐标）见 `_build_item`；这里只做
    append + 落盘 + 日志。要一次钉多张用 `pin_many`（省落盘、且顺序可控）。
    """
    item = _build_item(
        title=title, url=url, path=path, task_id=task_id, note=note, kind=kind,
        model=model, x=x, y=y, w=w, h=h, request=request,
    )
    # 遮挡警示在 append 前算：自动落位恒为空，显式 x/y 压到已有卡时回执告知 agent「盖住了谁」
    covered = _covered_by(item["x"], item["y"], item["w"], item["h"])
    _items.append(item)
    dropped = 0
    if len(_items) > MAX_ITEMS:
        dropped = len(_items) - MAX_ITEMS
        del _items[:dropped]
    _save()
    log.info("board pinned %s (%s) -> %s%s", item["id"], item["kind"], item["title"],
             f", dropped {dropped} oldest" if dropped else "")
    receipt = dict(item)
    if covered:
        # 只进回执不落库：落库的副本会随画布变化过时（被盖的卡被删/移走后就成了假信息）
        receipt["covered"] = covered
    return receipt


def brief(item: dict[str, Any]) -> dict[str, Any]:
    """卡片的精简表示（批量回执用）。

    单张 pin 回整条 item 是有用的（agent 要 id 与坐标）；一次回 20 张时
    note / thumb / task_id / origin 就成了噪音，只留定位用的那几个字段。
    `covered`（显式坐标盖住了谁）是布局警示，跟坐标一起保留。
    """
    out = {k: item.get(k) for k in ("id", "title", "kind", "url", "model", "x", "y")}
    if item.get("covered"):
        out["covered"] = item["covered"]
    if item.get("size_adjusted"):
        out["size_adjusted"] = True
    return out


_PIN_STR_FIELDS = ("title", "url", "path", "task_id", "note", "kind", "model")
_PIN_NUM_FIELDS = ("x", "y", "w", "h")


def _spec_kwargs(spec: dict[str, Any]) -> dict[str, Any]:
    """批量里的单个卡片对象 → `_build_item` 的关键字参数（未列出的键直接忽略）。"""
    kwargs: dict[str, Any] = {f: spec.get(f) or "" for f in _PIN_STR_FIELDS}
    for f in _PIN_NUM_FIELDS:
        kwargs[f] = spec.get(f)
    return kwargs


def pin_many(specs: list[dict[str, Any]], request: web.Request | None = None) -> dict[str, Any]:
    """一次钉一批卡片，返回 `{items, count, dropped}`。

    与逐张调 `pin()` 的差别只有两条，两条都是为了「按数组顺序排布」能成立：
      - **只落盘一次** —— 逐张调会把整个 board.json 重写 N 遍（满容量时约百 KB 级/次）；
      - **整批原子** —— 中途任一张不合法就整批回滚（`_save()` 在末尾才调，磁盘上不会
        出现「前 3 张钉上了、第 4 张 400」这种半截批次）。

    顺序即数组顺序：第 1 张先 append，第 2 张找空位时自然避开它 —— 所以同批也能
    自己排出「分镜 1..N 横排」，不必手工给坐标，也不会像并发逐张调那样排布乱序。
    """
    if not isinstance(specs, list) or not specs:
        raise APIError("'items' must be a non-empty array of card objects.", param="items")

    start = len(_items)
    built: list[dict[str, Any]] = []
    covered_map: dict[int, list[dict[str, Any]]] = {}
    try:
        for spec in specs:
            if not isinstance(spec, dict):
                raise APIError("Every entry of 'items' must be an object.", param="items")
            built.append(_build_item(**_spec_kwargs(spec), request=request))
            covered_map[id(built[-1])] = _covered_by(built[-1]["x"], built[-1]["y"],
                                                     built[-1]["w"], built[-1]["h"])
            _items.append(built[-1])
    except Exception:
        del _items[start:]          # 本批已 append 的全部撤回，内存回到调用前
        raise

    dropped = 0
    if len(_items) > MAX_ITEMS:
        dropped = len(_items) - MAX_ITEMS
        del _items[:dropped]
    _save()
    log.info("board pinned %d item(s) in one batch%s", len(built),
             f", dropped {dropped} oldest" if dropped else "")
    # covered 只进回执不落库：built 里的 dict 与 _items 同引用，必须拷贝后再注入，
    # 否则内存态被污染、下次 _save() 把过时的 covered 写进盘
    receipts: list[dict[str, Any]] = []
    for i in built:
        r = dict(i)
        c = covered_map.get(id(i)) or []
        if c:
            r["covered"] = c
        receipts.append(r)
    return {"items": receipts, "count": len(_items), "dropped": dropped}


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


# ------------------------------------------------------------------ 归档摘要
def _kinds_of(items: list[dict[str, Any]]) -> list[str]:
    """卡片类别的去重顺序（按首次出现）。"""
    out: list[str] = []
    for i in items:
        k = str(i.get("kind") or "")
        if k and k not in out:
            out.append(k)
    return out


def _kinds_text(kinds: list[str]) -> str:
    """类别列表 → 人类可读的短串：最多列两种，多的折成 `+N`。"""
    if not kinds:
        return ""
    if len(kinds) <= 2:
        return "/".join(kinds)
    return "/".join(kinds[:2]) + f"+{len(kinds) - 2}"


def _auto_label(count: int, kinds: list[str]) -> str:
    """没给 label 时的自动归档名，形如 `7 张 · image/text · 10:24`。

    回看列表里连着几份「未命名 · 10:24:56」是认不出哪份是哪轮的；张数 + 类别是
    一眼就能看出的内容指纹（显式传的 label 优先，这里只是兜底）。
    """
    parts = [f"{count} 张"]
    if kinds:
        parts.append(_kinds_text(kinds))
    parts.append(time.strftime("%H:%M"))
    return " · ".join(parts)


def summarize(entry: dict[str, Any]) -> dict[str, Any]:
    """归档摘要 —— 回答「这是哪一轮」。

    只有 id + label + 时间 + 张数时，agent 认不出内容（label 还可能是人随手写的时间戳），
    只能把每份详情逐个拉出来探测（最多 MAX_HISTORY 次）。所以这里补三样指纹：
    `kinds`（都有哪些类别的卡）、`preview`（前几张 title）、`models`（用过哪些模型）。
    """
    items = [i for i in (entry.get("items") or []) if isinstance(i, dict)]
    titles = [str(i.get("title") or "").strip() for i in items]
    models: list[str] = []
    for i in items:
        m = str(i.get("model") or "").strip()
        if m and m not in models:
            models.append(m)
    return {
        "id": entry.get("id"),
        "label": entry.get("label"),
        "created": entry.get("created"),
        "count": entry.get("count") if entry.get("count") is not None else len(items),
        "kinds": _kinds_of(items),
        "preview": [t for t in titles if t][:PREVIEW_MAX],
        "models": models[:MODELS_MAX],
    }


def summaries(limit: int | None = None) -> list[dict[str, Any]]:
    """归档摘要列表，**新的在前**（页面与 agent 都是这个顺序）。"""
    rows = [summarize(h) for h in reversed(_history)]
    return rows[:limit] if limit else rows


def latest_archive_id() -> str:
    """最近一份归档的 id；一份都没有时回空串（调用方据此给出「还没归档过」的提示）。"""
    return _history[-1]["id"] if _history else ""


def history_count() -> int:
    """归档份数。"""
    return len(_history)


def find_archive(archive_id: str) -> dict[str, Any] | None:
    """按 id 找一份归档；没有就 None（HTTP 层转 404，MCP 层转 ok=false）。"""
    return next((h for h in _history if h["id"] == archive_id), None)


def archive(label: str = "") -> dict[str, Any] | None:
    """把当前看板**归档进历史**并清空。空看板不产生归档。

    不给 label 时按内容自动起名（如 `7 张 · image/text · 10:24`）—— 原先固定是
    `未命名 · 10:24:56`，回看列表里连着几份「未命名」就完全认不出哪份是哪轮。
    """
    if not _items:
        return None
    entry = {
        "id": uuid.uuid4().hex[:12],
        "label": _clip(label, LABEL_MAX) or _auto_label(len(_items), _kinds_of(_items)),
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
    entry = find_archive(archive_id)
    if not entry:
        raise APIError(f"No archived board with id '{archive_id}'.", status_code=404, code="archive_not_found")
    auto = archive() if (archive_current and _items) else None
    _items.clear()
    _items.extend(dict(i) for i in (entry.get("items") or []))
    _save()
    return {"loaded": entry["id"], "count": len(_items), "auto_archived": auto["id"] if auto else None}


def remove_archive(archive_id: str) -> dict[str, Any] | None:
    """删掉某份归档，返回被删那份的摘要；没有这份就 None。

    归档是清空看板时的存档，删掉不可恢复 —— 判定交回调用方（HTTP 层返回 404），
    这里只在真的删成功时落盘。
    """
    for i, entry in enumerate(_history):
        if entry["id"] == archive_id:
            del _history[i]
            _save()
            log.info("board archive removed %s (%s item(s))", archive_id, entry.get("count") or 0)
            return entry
    return None


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


# 单卡 body 里的字段名。批量模式下它们与 `items` 同传就直接拒绝 —— 同传会让人以为
# 「顶层那张也钉了」，实际只有 items 生效，属于会静默骗人的组合。
_SINGLE_KEYS = ("title", "url", "path", "task_id", "note", "kind", "model", "x", "y", "w", "h")


@gateway_handler
async def pin_item(request: web.Request) -> web.Response:
    """钉卡片。

    单张：`{title, url|path|task_id, note?, kind?, model?, x?, y?, w?, h?}`；
    批量：`{items: [ {同上}, ... ]}` —— 一次落盘、按数组顺序排布。
    """
    _authorize(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 非 JSON / 空 body 都归到「body 不是对象」
        payload = None
    body = _json_body(payload)

    if "items" in body:
        given = [k for k in _SINGLE_KEYS if body.get(k) not in (None, "")]
        if given:
            raise APIError(
                "Batch mode takes only 'items'; drop the single-card fields: "
                + ", ".join(given) + ".", param="items",
            )
        result = pin_many(body.get("items"), request=request)
        return web.json_response({
            "ok": True,
            "items": [brief(i) for i in result["items"]],
            "added": len(result["items"]),
            "count": result["count"],
            "dropped": result["dropped"],
        })

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
    """历史归档列表（摘要：id / label / 时间 / 张数 + 类别·标题·模型三样指纹；详情走 history/{id}）。"""
    _authorize(request)
    return web.json_response({"history": summaries(), "count": len(_history)})


@gateway_handler
async def history_detail(request: web.Request) -> web.Response:
    """某份归档的完整内容（含卡片与坐标，可直接画出来）。"""
    _authorize(request)
    archive_id = request.match_info.get("id") or ""
    entry = find_archive(archive_id)
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


@gateway_handler
async def history_remove(request: web.Request) -> web.Response:
    """删掉某份归档（不可恢复 —— 前端会先确认一次再发这条请求）。"""
    _authorize(request)
    archive_id = request.match_info.get("id") or ""
    entry = remove_archive(archive_id)
    if not entry:
        raise APIError(
            f"No archived board with id '{archive_id}'.", status_code=404, code="archive_not_found"
        )
    return web.json_response({
        "ok": True, "id": archive_id, "removed": entry.get("count") or 0, "count": len(_history),
    })


# ------------------------------------------------------------------ 交给操作系统打开
def _is_local(request: web.Request) -> bool:
    """请求是否来自本机。

    「打开文件管理器」只在后端与浏览器同机时才有意义：从局域网另一台机器点，窗口会开在
    **服务器**那台机器上，点的人这边什么也看不到。与其静默无反应，不如明确拒绝并说清原因。
    反向代理会把 remote 变成 127.0.0.1 ⇒ 只要带转发头就一律当远程，别被表象骗过。
    """
    if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
        return False
    try:
        return ipaddress.ip_address(request.remote or "").is_loopback
    except ValueError:
        return False


def _reveal(target: Path, is_dir: bool) -> None:
    """交给操作系统：目录 → 文件管理器进入该目录；文件 → 打开其所在目录并选中它。

    这是同步阻塞调用（等 ShellExecute / xdg-open 起来），必须由调用方放进 executor，
    否则会把 aiohttp 的事件循环按住。
    """
    if sys.platform.startswith("win"):
        if is_dir:
            os.startfile(str(target))                            # noqa: S606 - 进入目录
        else:
            # `/select,` 必须与路径紧邻，且含空格的路径要整体带引号 —— 交给 subprocess
            # 按参数拼（手拼字符串在路径含空格时必断）
            subprocess.Popen(["explorer", "/select,", str(target)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)] if is_dir else ["open", "-R", str(target)])
    else:
        # 桌面环境没有统一的「选中某个文件」入口 ⇒ 一律打开它所在的目录
        subprocess.Popen(["xdg-open", str(target if is_dir else target.parent)])


@gateway_handler
async def reveal(request: web.Request) -> web.Response:
    """在某张看板卡片指向的路径上拉起系统文件管理器。body: {id}。

    只接受**看板上已存在的卡片 id**，不接受任意路径：这样「能打开什么」恒等于 agent 自己
    钉过什么，卡片被清空后这条路径也随之失效 —— 既不用另立一套路径白名单，也不会把端点
    开成「随便什么路径都能打开」。归档里的卡片不算（那些路径已不在当前看板）。
    """
    _authorize(request)
    if not (_is_local(request) or settings.reveal_allow_remote):
        raise APIError(
            "This action opens a window on the machine running ComfyUI,"
            " which is not the machine this request came from."
            " If ComfyUI runs on YOUR machine and you reached this page through a"
            " tunnel/reverse proxy, set REVEAL_ALLOW_REMOTE=1 to allow it.",
            status_code=403, code="reveal_not_local", param="request",
        )
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 非 JSON / 空 body 都归到「body 不是对象」
        payload = None
    item_id = _json_body(payload).get("id") or ""
    item = next((i for i in _items if i["id"] == item_id), None)
    if not item:
        raise APIError(
            f"No board item with id '{item_id}'.", status_code=404, code="board_item_not_found"
        )
    target = (item.get("ext") or {}).get("path") or ""
    if not target:
        raise APIError(
            "That card carries no local path (only cards outside input/output do).",
            param="id", code="not_external",
        )
    path = Path(target)
    if not path.exists():
        raise APIError(f"Path no longer exists: {target}", status_code=404, code="path_missing")
    is_dir = path.is_dir()
    await asyncio.get_running_loop().run_in_executor(None, _reveal, path, is_dir)
    log.info("board revealed %s -> %s (%s)", item_id, target, "dir" if is_dir else "file")
    return web.json_response({"ok": True, "id": item_id, "path": str(path), "is_dir": is_dir})
