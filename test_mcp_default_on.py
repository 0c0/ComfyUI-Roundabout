"""验证 MCP 网关「默认开启」这条路径真的能起来（不依赖完整 ComfyUI）。

为什么需要这个测试：
    MCP_ENABLED 默认值从 false 改成 true 后，这个默认值本身就是产品行为的一部分
    （用户克隆下来、装完依赖、重启 ComfyUI 就应该有 /mcp），而 get 一个 bool 是测
    不出「嵌入式 server 能不能真起来」的。本测试按节点在 ComfyUI 里的真实路径跑一遍：
    伪造 PromptServer.instance.app（真实 aiohttp Application）→ 以包形式导入节点
    __init__.py（会注册 REST 路由 + 拉起嵌入式 MCP + 后端就绪后挂共享端口代理）→ 用真实 HTTP
    打一发 MCP initialize / tools/list。

    刻意把 MCP_ENABLED / MCP_PORT 置空字符串：既挡住 .env 的覆盖，又让 config 走代码
    默认值，因此断言通过 = 「代码默认就是开的、端口默认就是自动分配」这两件事成立。

不碰正在运行的 ComfyUI：入口占用 8199，MCP 内部后端走系统分配的临时端口，跑完即退。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/test_mcp_default_on.py
NODE = Path(__file__).resolve().parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 挡住 .env 里的同名项，强制走 gateway/config.py 的代码默认值
os.environ["MCP_ENABLED"] = ""
os.environ["MCP_PORT"] = ""
os.environ["MCP_PORT_MAP"] = ""

FRONT_PORT = 8199  # 冒充 ComfyUI 对外端口
PATH = "/mcp"

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


async def _post(session, url: str, payload: dict, session_id: str | None = None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    async with session.post(url, json=payload, headers=headers) as resp:
        body = await resp.text()
        return resp.status, dict(resp.headers), body


def _parse_sse(body: str) -> dict | None:
    """streamable-http 的响应是 SSE，取最后一条 data: 后的 JSON。"""
    out = None
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                out = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass
    return out


async def main() -> int:
    from aiohttp import ClientSession, web

    app = web.Application()

    # 伪造 ComfyUI 的 server 模块，只提供 __init__.py 用到的那一点
    server = types.ModuleType("server")

    class _PromptServer:
        instance = None

    _PromptServer.instance = types.SimpleNamespace(app=app)
    server.PromptServer = _PromptServer  # type: ignore[attr-defined]
    sys.modules["server"] = server

    PKG = "ComfyUI-Roundabout"
    spec = importlib.util.spec_from_file_location(
        PKG, str(NODE / "__init__.py"), submodule_search_locations=[str(NODE)]
    )
    assert spec and spec.loader
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = pkg
    # 在运行中的事件循环里导入，_maybe_start_embedded_mcp() 才有 loop 可用
    spec.loader.exec_module(pkg)

    settings = importlib.import_module(PKG + ".gateway.config").settings
    check("config 默认 mcp_enabled=True（未设环境变量）", settings.mcp_enabled is True,
          f"mcp_enabled={settings.mcp_enabled}")
    check("config 默认 mcp_port=0（未显式钉死端口）", settings.mcp_port == 0,
          f"mcp_port={settings.mcp_port}")
    check("config 默认 mcp_port_map 为空（未配映射）", settings.mcp_port_map == {},
          f"mcp_port_map={settings.mcp_port_map}")
    check("无映射时回落到系统分配的 0 端口", settings.resolved_mcp_port == 0,
          f"resolved={settings.resolved_mcp_port}")

    def _routes() -> set:
        return {r.resource.canonical for r in app.router.routes() if r.resource is not None}

    # 代理要等内部后端 bind 成功后才注册（端口由系统分配，事先不知道），故轮询等待
    for _ in range(60):
        if PATH in _routes():
            break
        await asyncio.sleep(0.1)
    check("共享端口代理已注册 /mcp（后端就绪后）", PATH in _routes(),
          str(sorted(_routes()))[:160])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", FRONT_PORT)
    await site.start()

    url = f"http://127.0.0.1:{FRONT_PORT}{PATH}"
    try:
        async with ClientSession() as s:
            # 等内部 uvicorn 起来（默认开启后这是启动路径的一部分）
            for _ in range(50):
                try:
                    st, hdrs, body = await _post(
                        s, url,
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18",
                                    "capabilities": {},
                                    "clientInfo": {"name": "selftest", "version": "0"}}},
                    )
                    break
                except Exception:
                    await asyncio.sleep(0.2)
            else:
                check("经共享端口代理握手 initialize", False, "内部后端未就绪")
                return 1

            check("经共享端口代理握手 initialize", st == 200, f"HTTP {st}")
            init = _parse_sse(body)
            info = (init or {}).get("result", {}).get("serverInfo", {})
            check("响应含 serverInfo", bool(info), json.dumps(info)[:120])

            # 版本号必须与 pyproject.toml 一致（注册表发布用同一个号）
            try:
                import tomllib

                with open(NODE / "pyproject.toml", "rb") as fh:
                    want = tomllib.load(fh)["project"]["version"]
            except Exception:
                want = None
            check("serverInfo.version 与 pyproject.toml 一致",
                  want is not None and info.get("version") == want,
                  f"serverInfo={info.get('version')} pyproject={want}")

            sid = hdrs.get("mcp-session-id")
            if sid:  # 新协议要求先确认 initialized 再调工具
                await _post(s, url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

            st, _, body = await _post(
                s, url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid
            )
            tools = (_parse_sse(body) or {}).get("result", {}).get("tools", [])
            names = sorted(t["name"] for t in tools)
            check("tools/list 返回 12 个工具", len(names) == 12, f"{len(names)} 个: {names}")
    finally:
        await runner.cleanup()

    print()
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    # 嵌入式 MCP 的 uvicorn task 仍在跑，正常退出会挂住，直接结束进程
    sys.stdout.flush()
    os._exit(code)
