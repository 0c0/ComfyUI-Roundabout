"""验证嵌入模式下 MCP 与节点共用同一份 gateway 模块。

背景：mcp_server.py 早期用顶层名 `gateway.*` 导入，而节点 __init__.py 用相对导入
（`from .gateway...`），同一份代码会在 sys.modules 里留下两个身份，registry /
task_store 各有一份实例。症状是静默的：热加载 models.yaml 后 MCP 侧看不到新模型、
MCP 建的异步任务在 REST 与任务面板里查不到。

本测试按 ComfyUI 的方式把节点当包加载，再导入 mcp_server，检查二者拿到的是同一对象。
不影响正在运行的 ComfyUI（MCP_ENABLED 强制关闭，不绑任何端口）。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/test_mcp_import_identity.py
NODE = Path(__file__).resolve().parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 节点 __init__.py 会读 MCP_ENABLED；关掉以免真的去起嵌入式 MCP 后端
os.environ["MCP_ENABLED"] = "false"


def main() -> int:
    # 伪造 ComfyUI 的 server 模块：节点启动时会给 PromptServer.instance.app 注册路由
    server = types.ModuleType("server")

    class _PromptServer:
        instance = None

    server.PromptServer = _PromptServer  # type: ignore[attr-defined]
    sys.modules["server"] = server

    PKG = "ComfyUI-Roundabout"
    spec = importlib.util.spec_from_file_location(
        PKG, str(NODE / "__init__.py"), submodule_search_locations=[str(NODE)]
    )
    assert spec and spec.loader
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = pkg
    spec.loader.exec_module(pkg)  # register_routes 会因假 PromptServer 抛错，由节点自身 try 吞掉

    mcp = importlib.import_module(PKG + ".mcp_server")

    results: list[bool] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        results.append(ok)
        print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))

    def _mod(sub: str):
        return importlib.import_module(f"{PKG}.gateway.{sub}")

    print("== 模块身份 ==")
    stray = [k for k in sys.modules if k == "gateway" or k.startswith("gateway.")]
    check("没有游离的顶层 gateway", not stray, str(stray)[:160])
    check("registry 同一份", mcp.registry is _mod("registry").registry)
    check("task_store 同一份", mcp.task_store is _mod("tasks").task_store)
    check("settings 同一份", mcp.settings is _mod("config").settings)
    check("ComfyClient 同一类", mcp.ComfyClient is _mod("comfy_client").ComfyClient)

    print("== 注册表可见性（模拟 REST 侧 reload 后 MCP 是否同见）==")
    before = {s.name for s in mcp.registry.all()}
    check("registry 已加载模型", bool(before), f"{len(before)} 个")

    # 走「节点侧」的 registry 实例重新加载；若两份实例，MCP 侧不会看到这次加载
    node_registry = _mod("registry").registry
    assert node_registry is mcp.registry, "前置断言：两者应已是同一实例"
    node_registry.load(
        mcp.settings.models_file, mcp.settings.workflows_dir, mcp.settings.default_model
    )
    after = {s.name for s in mcp.registry.all()}
    check("节点侧重载后 MCP 视图同步", before == after, f"{len(before)} -> {len(after)}")

    print()
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
