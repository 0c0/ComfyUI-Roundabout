"""view.html 内联脚本的**顶层作用域**守门（靠 Node 桩环境真跑一遍）。

守的是什么：把一段 JS 插到某个函数左括号后面（`async function loadTasks() {` 紧跟插入内容），
语法合法、`node --check` 不报、`<script>` 也能解析 —— 但整段代码被关进了那个函数体内，
页面里调用它的顶层代码就 `ReferenceError`，页面功能静默失效。Python 侧测不到，
所以这里起一个桩 DOM 把脚本真跑一遍，并断言必需标识符确实挂在顶层。

Node 缺失时跳过（打印 SKIP 并成功退出），不让本地环境差异把回归搞红。

    python tests/test_view_html_scope.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
VIEW = ROOT / "web" / "view.html"
HARNESS = TESTS / "js" / "view_scope_harness.js"

# 托管运行时优先；找不到再退回 PATH 里的 node
NODE_CANDIDATES = (
    r"C:\Users\cctim\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
)

SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)

checks = 0
fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  -> {detail}" if detail else ""))
        fails.append(label)


def find_node() -> str | None:
    for cand in NODE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return shutil.which("node")


def extract_script(html: str) -> str:
    """取页面的最后一个 <script> 块（view.html 只有内联脚本，没有外链）。"""
    blocks = SCRIPT_RE.findall(html)
    assert blocks, "view.html 里找不到 <script> 块"
    return blocks[-1]


def run_harness(node: str, js_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [node, str(HARNESS), str(js_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def main() -> int:
    html = VIEW.read_text(encoding="utf-8")
    script = extract_script(html)
    print(f"提取内联脚本 {len(script)} 字符（{len(script.splitlines())} 行）")

    check("脚本非空且含看板代码", len(script) > 1000 and "renderBoard" in script)

    # 结构性检查：插入块的首行不能与函数左括号挤在**同一行**
    # （`async function loadTasks() {// ----` 这种拼接，就是把整段关进函数体的典型形态；
    #  注意只匹配同一行 —— 正常写法 `function foo() {` 换行后再注释是应该允许的）
    glued = re.findall(r"function\s+\w+\s*\([^)]*\)\s*\{[ \t]*//", script)
    check("没有「函数左括号与注释挤在同一行」的粘连", not glued, f"发现 {len(glued)} 处：{glued[:3]}")

    node = find_node()
    if not node:
        print("  SKIP  node 不可用，跳过真跑检查")
        print(f"\n===== {checks} passed / 0 failed (harness skipped) =====")
        return 0

    tmp = TESTS / "js" / "_view_inline_extracted.js"
    tmp.write_text(script, encoding="utf-8")
    try:
        proc = run_harness(node, tmp)
        out = (proc.stdout + proc.stderr).strip()
        check("桩环境真跑脚本无异常", proc.returncode == 0, out[-200:])
        check("必需标识符都在顶层", "MISSING AT TOP LEVEL" not in out, out[-200:])

        # 自校验：人为把看板块嵌进一个函数里，守门必须变红 —— 不会红的门等于没有
        start = script.find("// ---- 任务看板：无限画布")
        end_marker = "$('boardBackBtn').onclick"
        end = script.find(end_marker)
        if start != -1 and end != -1:
            tail = script.index("\n};\n", end)
            nested = (script[:start] + "(function __nest() {\n"
                      + script[start:tail + 4] + "\n})();\n" + script[tail + 4:])
            tmp2 = TESTS / "js" / "_view_nested_selftest.js"
            tmp2.write_text(nested, encoding="utf-8")
            try:
                red = run_harness(node, tmp2)
                check("人为嵌套后守门会变红（自校验）", red.returncode != 0,
                      "嵌套后仍然通过 ⇒ 这个门抓不到它要抓的东西")
            finally:
                tmp2.unlink(missing_ok=True)
        else:
            check("能定位看板代码段（自校验前置）", False, "找不到看板段起止标记")
    finally:
        tmp.unlink(missing_ok=True)

    if fails:
        print("\n  FAILED:", "; ".join(fails))
    print(f"\n===== {checks - len(fails)} passed / {len(fails)} failed =====")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
