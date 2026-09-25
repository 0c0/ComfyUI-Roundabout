"""回归：mcp_server.py 的两个导入分支（嵌入包相对导入 / stdio 顶层导入）必须同名同集。

v1.16.0 曾把 `build_skills_payload` 只加进 stdio 分支，嵌入 ComfyUI 的分支漏了
⇒ 8188 上 get_skills 运行期 NameError（stdio 自测通过、测试全绿，唯独线上挂）。
本测试做静态 AST 对称断言，双分支任一侧多出/缺失导入名即红。
"""

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = (_ROOT / "mcp_server.py").read_text(encoding="utf-8")


def _branch_imports(test_node: ast.If, take_body: bool) -> set[str]:
    """取 if/else 分支里的顶层 from-import 名字集合（不递归进内层块）。"""
    stmts = test_node.body if take_body else test_node.orelse
    names: set[str] = set()
    for s in stmts:
        if isinstance(s, ast.ImportFrom) and s.module and s.module.startswith("gateway"):
            for a in s.names:
                names.add(a.asname or a.name)
    return names


def test_import_branch_parity() -> None:
    tree = ast.parse(_SRC)
    ifs = [n for n in ast.walk(tree) if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "__package__"]
    assert len(ifs) == 1, f"应恰好存在一个 `if __package__:` 分支，实得 {len(ifs)} 个"
    node = ifs[0]
    embedded = _branch_imports(node, take_body=True)
    standalone = _branch_imports(node, take_body=False)
    assert embedded, "嵌入分支应含 gateway 相对导入"
    assert standalone, "stdio 分支应含 gateway 顶层导入"
    only_embedded = embedded - standalone
    only_standalone = standalone - embedded
    assert only_embedded == set(), f"嵌入分支多出的导入（stdio 分支没有，运行期必 NameError）：{sorted(only_embedded)}"
    assert only_standalone == set(), f"stdio 分支多出的导入（嵌入分支没有，运行期必 NameError）：{sorted(only_standalone)}"


if __name__ == "__main__":
    test_import_branch_parity()
    print("PASS test_mcp_import_parity")
    sys.exit(0)
