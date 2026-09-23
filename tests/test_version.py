"""版本号一致性守护。

版本号只有一个真源：`pyproject.toml` 的 `[project] version`。运行期的 `mcp_server.VERSION`
以及 `health` / `tool_info` 响应里的 `version` 都由它派生（`_package_version()` 现读文件），
所以只需要守住三件容易被忘的事：

  1. `API.md` 顶部的「当前版本」与 pyproject 同步 —— 那是人读的一份。历史上真出过：
     加 `get_tool_info` 接口、合并 qwen 两支工作流这两批功能提交都忘了升版本号；
  2. `mcp_server.py` 不许把版本号硬编码回去 —— 一硬编码就出现第二个真源，改了 pyproject
     却不改它，`health` 就报旧版本；
  3. 版本号形如 `X.Y.Z`，且本仓库的纪律是**新功能随同批提交升 minor、纯修复升 patch**。

    python tests/test_version.py
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PYPROJECT = ROOT / "pyproject.toml"
API_MD = ROOT / "API.md"
MCP_SERVER = ROOT / "mcp_server.py"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# API.md 顶部：> 当前版本：`1.3.0` ｜ 网关即 ComfyUI 自身（custom node）……
API_VERSION_RE = re.compile(r"当前版本：`([^`]+)`")
# 任何 `VERSION = "x"` / `VERSION = 'x'` 形态都算硬编码
HARDCODED_RE = re.compile(r"""^\s*VERSION\s*=\s*["']""", re.M)

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail != "" else ""))


def main() -> int:
    pyproject_version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    check("pyproject 版本号形如 X.Y.Z", bool(SEMVER_RE.match(pyproject_version)), pyproject_version)

    api_text = API_MD.read_text(encoding="utf-8")
    hits = API_VERSION_RE.findall(api_text)
    check("API.md 声明了「当前版本」", len(hits) >= 1, hits)
    check("API.md 只在一处声明版本（防多处漂移）", len(hits) == 1, hits)
    if hits:
        check(
            "API.md 的版本与 pyproject 一致",
            hits[0] == pyproject_version,
            f"API.md={hits[0]} pyproject={pyproject_version}",
        )
        check("API.md 声明的版本号形如 X.Y.Z", bool(SEMVER_RE.match(hits[0])), hits[0])

    src = MCP_SERVER.read_text(encoding="utf-8")
    check("mcp_server.VERSION 由 pyproject 派生", "VERSION = _package_version()" in src)
    check("mcp_server 里没有硬编码的 VERSION", not HARDCODED_RE.search(src),
          HARDCODED_RE.search(src).group(0).strip() if HARDCODED_RE.search(src) else "")

    print(f"\n===== {passed} passed / {failed} failed =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
