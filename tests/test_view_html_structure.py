"""view.html 的**文档结构**守门：成块的 CSS / JS 必须落在自己该在的块里。

守的是什么：把一整块 CSS 插到 `</style>` **之后**（而不是之前）。这样写没有非法字符、
`node --check` 不报、接口测试全绿，但按 HTML 规范「head 里出现非空白文本」会直接结束 head、
把这段文本挪进 body 开头 —— 页面顶部就原样显示这段 CSS（第一行正好是那行 `/* ---- ... ---- */`
注释），而且这些规则一条也不会生效。历史上真出过：看板的整块样式表就是这么被插在
`</style>` 后面的，看板一直裸样式运行，同时页面顶部糊着一大段样式文本。

为什么接口测试抓不到：REST 断言看的是 JSON、jsdom 作用域桩跑的是 JS，两边都不看文档结构。
所以这里从结构上守：

  1. `</style>` 与 `</head>` 之间、`</head>` 与 `<body>` 之间不得有非空行；
  2. 按解析器视角，head 内不得有裸文本，`<style>` / `<script>` / `<title>` 之外不得有 CSS 形态文本；
  3. 正例：看板的关键选择器必须在 `<style>` 内 —— 漏搬（而不是漏写）也能被抓住。

Node 与 jsdom 都不是必需：纯标准库 HTMLParser 即可复现。

    python tests/test_view_html_structure.py
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEW = ROOT / "web" / "view.html"

# 块外的样式文本：CSS 注释，或「选择器 + 声明块」形态
CSS_LIKE_RE = re.compile(r"/\*|[\w.#\[][^{}<>]*\{[^{}<>]*\}")

# <style> 内必须存在（漏搬到块外时这里会缺）
STYLE_MUST_HAVE = (".canvas-wrap {", ".canvas-wrap.grabbing", ".canvas-world {", ".board-card {",
                   ".bc-del {", ".history {", ".viewing-bar {")
# <script> 内必须存在
SCRIPT_MUST_HAVE = ("function renderBoard", "function loadBoard", "boardSignature")

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail != "" else ""))


class LooseText(HTMLParser):
    """收集 <style>/<script>/<title> 之外的非空白文本（带行号与上下文）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = {"style": 0, "script": 0, "title": 0}
        self.in_head = False
        self.loose: list[tuple[int, str, str]] = []   # (行号, 上下文, 文本)
        self.css_like: list[tuple[int, str]] = []     # (行号, 文本)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.depth:
            self.depth[tag] += 1
        elif tag == "head":
            self.in_head = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.depth:
            self.depth[tag] = max(0, self.depth[tag] - 1)
        elif tag == "head":
            self.in_head = False

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        if any(self.depth.values()):      # 在块内 —— 这是它的正常位置
            return
        where = "head 内" if self.in_head else "body 内"
        self.loose.append((self.getpos()[0], where, data.strip()[:70].replace("\n", " ⏎ ")))
        if CSS_LIKE_RE.search(data):
            self.css_like.append((self.getpos()[0], data.strip()[:70].replace("\n", " ⏎ ")))


def between(text: str, start: str, end: str) -> list[str]:
    """取 start 之后、end 之前的非空行（start / end 必须唯一命中）。"""
    assert text.count(start) == 1, f"{start!r} 命中 {text.count(start)} 次"
    assert text.count(end) == 1, f"{end!r} 命中 {text.count(end)} 次"
    i, j = text.index(start) + len(start), text.index(end)
    assert i < j, f"{start!r} 在 {end!r} 之后"
    return [ln for ln in text[i:j].split("\n") if ln.strip()]


def block_of(text: str, open_tag: str, close_tag: str) -> str:
    assert text.count(open_tag) == 1, f"{open_tag!r} 命中 {text.count(open_tag)} 次"
    assert text.count(close_tag) == 1, f"{close_tag!r} 命中 {text.count(close_tag)} 次"
    return text.split(open_tag, 1)[1].split(close_tag, 1)[0]


def main() -> int:
    raw = VIEW.read_bytes()
    check("view.html 是 LF 行尾（便于锚点拼接）", b"\r" not in raw)
    html = raw.decode("utf-8")
    style = block_of(html, "<style>", "</style>")
    script = block_of(html, "<script>", "</script>")

    # ---- 1. 块边界之间不得有内容（本次事故的直接判据）----
    stray = between(html, "</style>", "</head>")
    check("</style> 与 </head> 之间没有非空行", not stray,
          f"{len(stray)} 行，首个：{stray[0][:60]!r}" if stray else "")
    check("</head> 与 <body> 之间没有非空行", not between(html, "</head>", "<body"))

    # ---- 2. 解析器视角：head 内不得有裸文本；块外不得有样式文本 ----
    # （body 里的正常文案不算问题，所以「游离文本」只对 head 判定；块外用 CSS 形态判定）
    p = LooseText()
    p.feed(html)
    in_head = [(n, t) for n, w, t in p.loose if w == "head 内"]
    check("head 内没有游离文本（按规范会提前结束 head）", not in_head,
          "; ".join(f"L{n} {t}" for n, t in in_head[:3]))
    check("style/script/title 之外没有 CSS 形态文本", not p.css_like,
          "; ".join(f"L{n} {t}" for n, t in p.css_like[:3]))

    # ---- 3. 正例：关键片段确实在各自的块里（防漏搬、防误删）----
    missing = [s for s in STYLE_MUST_HAVE if s not in style]
    check("<style> 内含看板关键选择器", not missing, f"缺 {missing}")
    missing_js = [s for s in SCRIPT_MUST_HAVE if s not in script]
    check("<script> 内含看板关键函数", not missing_js, f"缺 {missing_js}")
    check("<style> 只有一对且不为空", len(style.strip()) > 500, f"{len(style)} 字符")

    # ---- 4. 自校验：把看板 CSS 再搬回 </style> 后面，判据必须变红 ----
    marker = "  /* ---- 任务看板"
    if marker in style:
        head, rest = style.split(marker, 1)
        board = marker + rest
        mutated = (html.replace(f"<style>{style}</style>",
                                f"<style>{head}</style> {board}", 1))
        m_stray = between(mutated, "</style>", "</head>")
        mp = LooseText()
        mp.feed(mutated)
        m_missing = [s for s in STYLE_MUST_HAVE if s not in block_of(mutated, "<style>", "</style>")]
        check("人为搬回块外后守门会变红（自校验）",
              bool(m_stray) and bool(mp.css_like) and bool(m_missing),
              f"stray={len(m_stray)} css={len(mp.css_like)} missing={len(m_missing)}")
        check("自校验构造出的泄漏文本与线上症状一致（首行是那行注释）",
              bool(m_stray) and m_stray[0].lstrip().startswith("/* ---- 任务看板"),
              m_stray[0][:60] if m_stray else "(无)")
    else:
        check("能定位看板 CSS 段（自校验前置）", False, f"<style> 里找不到 {marker!r}")

    print(f"\n===== {passed} passed / {failed} failed =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
