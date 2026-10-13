# -*- coding: utf-8 -*-
"""护栏：gateway 逻辑层禁止模型名硬编码（v1.43.1）。

架构约定（v1.43.0 起三层解耦）：模型能力判定一律走数据
（`spec.bindings` / `spec.param_specs` / `spec.references` / models.yaml 声明），
Python 代码不得出现具体模型名（如 `minimax-h3`）——包括字符串常量、
f-string 文本、错误消息与 Field description。加新模型时旧代码一个字不用改，
也不存在「忘了改某处文案」的过期清单。

例外：docstring（面向开发者的机制说明，示例名允许）；`#` 行注释本就不进 AST。
models.yaml / workflows/*.json 是数据层，天然不受限。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 逻辑层文件：任何字符串常量（含 f-string 文本段）不得出现模型名
SCAN_FILES = [
    "gateway/pipeline.py",
    "gateway/toolinfo.py",
    "gateway/params.py",
    "gateway/schemas.py",
    "gateway/registry.py",
    "gateway/wfdeploy.py",
]

# 触发词：模型名前缀。出现即说明有代码在「认识」某个具体模型。
MARKERS = ("minimax", "boogu-image", "qwen-image", "z-image", "flux2-klein", "seedvr2", "birefnet", "sam3")

passed = failed = 0


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集 docstring 节点的 id（Module/Class/Function 的首个语句）。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                out.add(id(node.body[0].value))
    return out


def _literal_strings(node: ast.AST):
    """产出所有字符串字面量（Constant str + f-string 的文本段）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value
        elif isinstance(sub, ast.JoinedStr):
            for v in sub.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    yield v.value


def main() -> int:
    global passed, failed
    for rel in SCAN_FILES:
        path = ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        docstrings = _docstring_nodes(tree)
        hits = []
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for s in _literal_strings(node):
                    low = s.lower()
                    for m in MARKERS:
                        if m in low:
                            hits.append(f"L{node.lineno}: …{s[max(0, low.find(m) - 20):low.find(m) + 40]}…")
                            break
        if hits:
            failed += 1
            print(f"  [FAIL] {rel}: {len(hits)} 处模型名硬编码")
            for h in hits[:8]:
                print(f"         {h}")
        else:
            passed += 1
            print(f"  [PASS] {rel}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
