"""验证「同一台机器跑多个 ComfyUI 实例」时，Roundabout 的 MCP 内部后端不抢端口。

背景（真实故障）：嵌入式 MCP 后端原来默认写死绑 `127.0.0.1:8189`。本机用
`--port` 起了第二个 ComfyUI 实例后，它的 Roundabout 同样去绑 8189 —— 已被第一个
实例占用，绑不上，第二个实例的 MCP 端点就废了。

现在的端口策略（gateway.config.resolve_mcp_port）：
  1. 显式 `MCP_PORT` > 0 → 用它；
  2. `MCP_PORT_MAP` 按本实例 ComfyUI 端口映射（如 [8188,888],[8189,999]）→ 用映射值；
  3. 都没命中 → 0，交给操作系统分配。
绑定成功后经 on_ready 回传真实端口。另外，配好的端口若已被占用，默认回落到系统
分配的端口（fallback_auto），保证端点不因端口冲突而失效；fallback_auto=False 时
才回传 None。

本测试断言：
  1. 两个后端都用默认配置（port=0）时能同时起来，且落在**不同**端口；
  2. 两个端口都能完成 MCP initialize（真的在服务，不是占着不发）；
  3. 端口被占用时，默认回落到另一个可用端口并照常服务；
  4. fallback_auto=False 时，端口被占用回传 None，且不把进程带崩。

不碰正在运行的 ComfyUI：默认场景全部用系统分配的临时端口，跑完即退。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/test_mcp_multi_instance.py
NODE = Path(__file__).resolve().parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 置空字符串：既挡住 .env 的覆盖，又让 config 走代码默认值
os.environ["MCP_ENABLED"] = ""
os.environ["MCP_PORT"] = ""
os.environ["MCP_PORT_MAP"] = ""

PATH = "/mcp"
results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


async def _initialize(port: int, path: str = PATH):
    """往某个已就绪的后端端口打一发 MCP initialize。"""
    from aiohttp import ClientSession

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "multi-instance-selftest", "version": "0"},
        },
    }
    async with ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}{path}", json=payload, headers=headers) as resp:
            return resp.status, await resp.text()


class _Backend:
    """起一个嵌入式 MCP 后端，记录它请求的端口与实际绑定的端口。"""

    def __init__(self, mcp_server, requested_port: int, fallback_auto: bool = True) -> None:
        self._mcp = mcp_server
        self.requested = requested_port
        self.fallback_auto = fallback_auto
        self.actual: int | None = None
        self._evt = asyncio.Event()

    def _on_ready(self, port) -> None:
        self.actual = port
        self._evt.set()

    def start(self) -> None:
        # 与节点 __init__.py 完全同一条路径
        asyncio.ensure_future(
            self._mcp.serve_embedded(
                host="127.0.0.1",
                port=self.requested,
                path=PATH,
                on_ready=self._on_ready,
                fallback_auto=self.fallback_auto,
            )
        )

    async def wait(self, timeout: float = 20.0):
        await asyncio.wait_for(self._evt.wait(), timeout=timeout)
        return self.actual


async def main() -> int:
    from aiohttp import web

    # 伪造 ComfyUI 的 server 模块，让节点 __init__.py 能以包形式加载
    # （顺带跑一遍真实启动路径，等于「实例 1」已经起来了）
    app = web.Application()
    server_mod = types.ModuleType("server")

    class _PromptServer:
        instance = None

    _PromptServer.instance = types.SimpleNamespace(app=app)
    server_mod.PromptServer = _PromptServer  # type: ignore[attr-defined]
    sys.modules["server"] = server_mod

    PKG = "ComfyUI-Roundabout"
    spec = importlib.util.spec_from_file_location(
        PKG, str(NODE / "__init__.py"), submodule_search_locations=[str(NODE)]
    )
    assert spec and spec.loader
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = pkg
    spec.loader.exec_module(pkg)

    mcp_server = sys.modules[PKG + ".mcp_server"]
    settings = sys.modules[PKG + ".gateway.config"].settings

    check("MCP_PORT 默认 0（未显式钉死端口）", settings.mcp_port == 0, f"mcp_port={settings.mcp_port}")
    check("MCP_PORT_MAP 默认空（未配映射）", settings.mcp_port_map == {},
          f"mcp_port_map={settings.mcp_port_map}")

    # ---- 场景 A：两个实例都用默认配置 —— 必须能并存 ----
    a = _Backend(mcp_server, 0)
    b = _Backend(mcp_server, 0)
    a.start()
    b.start()
    pa = await a.wait()
    pb = await b.wait()
    check("实例 A 拿到端口", pa not in (None, 0), f"port={pa}")
    check("实例 B 拿到端口", pb not in (None, 0), f"port={pb}")
    check("两个实例落在不同端口", pa != pb, f"{pa} vs {pb}")

    for tag, port in (("A", pa), ("B", pb)):
        try:
            st, body = await _initialize(port)
        except Exception as exc:  # noqa: BLE001
            st, body = None, repr(exc)
        check(f"实例 {tag} 可完成 MCP initialize", st == 200 and "serverInfo" in body, f"HTTP {st}")

    # ---- 场景 B：端口被占用 —— 默认回落到别的可用端口，端点照常服务 ----
    c = _Backend(mcp_server, 0)
    c.start()
    pc = await c.wait()
    check("实例 C 在分配到的端口就绪", pc not in (None, 0), f"port={pc}")

    d = _Backend(mcp_server, pc)  # 故意抢 C 已占用的端口
    d.start()
    try:
        pd = await d.wait(timeout=15.0)
    except asyncio.TimeoutError:
        pd = "TIMEOUT"
    check("端口被占时实例 D 回落到其他端口", pd not in (None, 0, pc, "TIMEOUT"),
          f"requested={pc} actual={pd}")
    if isinstance(pd, int):
        try:
            st, body = await _initialize(pd)
        except Exception as exc:  # noqa: BLE001
            st, body = None, repr(exc)
        check("回落后的实例 D 仍能完成 MCP initialize",
              st == 200 and "serverInfo" in body, f"HTTP {st}")

    # ---- 场景 C：禁止回落 —— 必须优雅回传 None，而不是炸进程 ----
    e = _Backend(mcp_server, pc, fallback_auto=False)
    e.start()
    try:
        pe = await e.wait(timeout=15.0)
    except asyncio.TimeoutError:
        pe = "TIMEOUT"
    check("禁止回落时实例 E 回传 None（不崩）", pe is None, f"reported={pe}")

    print()
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    # 嵌入式 uvicorn task 仍在跑，正常退出会挂住，直接结束进程
    sys.stdout.flush()
    os._exit(code)
