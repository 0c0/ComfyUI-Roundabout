"""view.html **看板前端**的行为不变量守门（零依赖，纯源码静态断言）。

为什么需要：看板有一类「接口全绿、页面却不对」的缺陷，REST 与桩 DOM 都抓不到。
现存的两道守门各有分工、都管不到这里：`test_view_html_structure.py` 只看文档结构，
`test_view_html_scope.py` 只看标识符在不在顶层。而下面这些是**行为顺序**问题：

  1. `loadBoard()` 用签名比对避免每 2 秒的轮询打断用户拖拽，但**空看板的签名恒为空串**
     ⇒ 与初始 `boardSig` 相同 ⇒ 提前 return，`renderBoard` 一次都不跑。
     于是首屏「空看板 + 有历史」时，面板可见却没有「还没有卡片」的提示，只见一块空画布。
     **空态赋值必须发生在早退之前。**
  2. `const first = boardSig === ''` 必须在 `boardSig = sig` **之前**求值：顺序颠倒后
     `first` 恒为 false，首屏永远不会自动取景。
  3. 看历史时不能被轮询覆盖（`if (viewingArchive) return;`）。

这些用源码顺序断言即可覆盖，不必引入 jsdom：判据是「谁在谁前面」，与实现细节无关。

    python tests/test_view_board_page.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEW = ROOT / "web" / "view.html"

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail != "" else ""))


def block_of(text: str, open_tag: str, close_tag: str) -> str:
    assert text.count(open_tag) == 1, f"{open_tag!r} 命中 {text.count(open_tag)} 次"
    assert text.count(close_tag) == 1, f"{close_tag!r} 命中 {text.count(close_tag)} 次"
    return text.split(open_tag, 1)[1].split(close_tag, 1)[0]


def fn_body(src: str, header: str) -> str:
    """按花括号配对取出 header 后面的整个函数体（含大括号）。"""
    i = src.index(header)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    raise AssertionError(f"{header!r} 没有配对的右花括号")


def order(body: str, first: str, second: str) -> bool:
    """first 出现在 second 之前（两者都必须存在）。"""
    assert first in body, f"找不到 {first!r}"
    assert second in body, f"找不到 {second!r}"
    return body.index(first) < body.index(second)


def invariants(script: str) -> dict[str, bool]:
    """把全部不变量算成一张表 —— 自校验时对变异源码重跑同一张表即可。"""
    load_board = fn_body(script, "async function loadBoard()")
    render_board = fn_body(script, "function renderBoard(")
    # 清空按钮的 handler 是赋值形式，body 从箭头函数的大括号起算
    clear_i = script.index("$('boardClearBtn').onclick")
    clear_body = fn_body(script[clear_i:], "onclick = async ()")
    return {
        # 1. 空态同步必须早于「签名相同就返回」
        "空态同步早于签名早退": order(load_board,
                                      "$('boardEmpty').hidden",
                                      "if (sig === boardSig) return;"),
        # 2. first 要按「更新前的 boardSig」判定
        "首屏取景标记在更新 boardSig 之前求值": order(load_board,
                                                      "const first = boardSig === ''",
                                                      "boardSig = sig;"),
        # 3. 看历史时暂停轮询覆盖
        "看历史时 loadBoard 直接返回": "if (viewingArchive) return;" in load_board,
        # 4. 四类产物 + 兜底分支都在
        "renderBoard 覆盖 image/video/audio/text 与兜底":
            all(f"'{k}'" in render_board for k in ("image", "video", "audio", "text"))
            and "FILE_SVG" in render_board,
        # 5. 坐标真的落到 style（不是只塞进 dataset）
        "卡片坐标写进 left/top 样式": "left:${it.x}px" in render_board and "top:${it.y}px" in render_board,
        # 6. 卡片尺寸写进 width/height
        "卡片尺寸写进 width/height 样式":
            "width:${it.w}px" in render_board and "height:${it.h}px" in render_board,
        # 7. 删单条走事件委托（× 是动态生成的，不能逐个绑）
        "删单条挂在 canvasWorld 的事件委托上":
            "const b = e.target.closest('[data-del]')" in script,
        # 8. 清空：先归档（DELETE）再刷新历史，否则归档不出来
        "清空先发 DELETE 再拉历史": order(clear_body, "fetch(`${API}/board${authQ()}`", "loadHistory()"),
        # 9. 删/清之后必须重置签名，否则内容变了也不重绘
        "删除后重置签名": "boardSig = '';" in fn_body(script[script.index("const b = e.target.closest('[data-del]')"):], "=>"),
    }


def red_on(mutated: str, key: str) -> bool:
    """变异后该判据是否变红（判据前提本身消失也算红）。"""
    try:
        return invariants(mutated)[key] is False
    except AssertionError:
        return True


def main() -> int:
    raw = VIEW.read_bytes()
    check("view.html 是 LF 行尾（便于锚点拼接）", b"\r" not in raw)
    html = raw.decode("utf-8")
    script = block_of(html, "<script>", "</script>")

    inv = invariants(script)
    for name, ok in inv.items():
        check(name, ok)

    # ---- 自校验：把两处顺序颠倒过来，对应判据必须变红 ----
    # 注意：renderBoard 里也有同一行空态赋值，变异必须限制在 loadBoard 体内，
    # 否则 replace(..., 1) 会打到前面那个副本、变异空转（这里踩过一次）。
    lb = fn_body(script, "async function loadBoard()")
    lb_moved = lb.replace("  $('boardEmpty').hidden = items.length > 0;\n", "", 1)
    lb_moved = lb_moved.replace(
        "  if (sig === boardSig) return;\n",
        "  if (sig === boardSig) return;\n  $('boardEmpty').hidden = items.length > 0;\n", 1)
    check("自校验：把空态赋值挪到早退之后，判据变红",
          lb_moved != lb and red_on(script.replace(lb, lb_moved, 1), "空态同步早于签名早退"),
          "变异后仍然通过 ⇒ 该判据抓不到回归")

    mutated2 = script.replace(
        "  const first = boardSig === '';\n  boardSig = sig;\n",
        "  boardSig = sig;\n  const first = boardSig === '';\n", 1)
    check("自校验：把 first 挪到更新 boardSig 之后，判据变红",
          mutated2 != script and red_on(mutated2, "首屏取景标记在更新 boardSig 之前求值"),
          "变异后仍然通过 ⇒ 该判据抓不到回归")

    print(f"\n===== {passed} passed / {failed} failed =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
