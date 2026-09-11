"""验证 /mcp 共享端口代理「路由先注册、端口后回填」这条时序（真实故障回归）。

背景（真实日志）：

    [WARNING] [Roundabout] MCP gateway: on_ready callback failed:
    Cannot register a resource into frozen router.

成因是时序，而不是配置。aiohttp 的路由表在 `AppRunner.setup()` 里就被冻结
（`web_runner._make_server` → `app.freeze()`），此后任何 `add_route` 都抛
`RuntimeError: Cannot register a resource into frozen router`
（`web_urldispatcher.UrlDispatcher.register_resource`）。而 ComfyUI 的启动顺序是

    main.py:543  run_until_complete(nodes.init_extra_nodes(...))   # 节点 import，可以注册
    main.py:557  prompt_server.add_routes()
    main.py:578  start_all() → prompt_server.setup() → runner.setup()  # 路由表在此冻结

`runner.setup()` 远早于 uvicorn 后端 bind 完成（取端口还得轮询 ≥50ms），所以
「等 on_ready 拿到端口后再 add_route」必然撞上已冻结的路由表 —— 结果是 /mcp 根本
没挂上，客户端在 ComfyUI 端口上拿到 404，只在启动日志里留一句 WARNING。

修复：路由在节点 import 期就注册（必定早于冻结），转发目标用 BackendTarget 占位，
后端就绪后只回填 `.url`，不再碰路由表；就绪前到达的请求会先等一小会儿
（READY_WAIT_TIMEOUT），避免 MCP 客户端在启动窗口内连一次就永久失败。

本测试断言：
  1. 后端始终不来 → 等 READY_WAIT_TIMEOUT 后回 503 + Retry-After；
  2. 无路由的 app 冻结后再注册 → RuntimeError（把这条约束钉住，防止回退）；
  3. 完整时序：冻结前注册（无目标）→ 冻结路由表 → 真起嵌入式后端 → **在后端就绪前
     就发请求**（代理应当等它，而不是回 503）→ 回填端口 → initialize 拿到 serverInfo。

不碰正在运行的 ComfyUI：全部使用系统分配的临时端口，跑完即退。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import os
import socket
import sys
import types
from pathlib import Path

# 从本文件位置反推，不写死安装路径
NODE = Path(__file__).resolve().parent
ROOT = NODE.parent.parent
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 不让节点 __init__.py 自己去拉嵌入式 MCP：本测试自己按需拉起
os.environ["MCP_ENABLED"] = "false"
os.environ["MCP_PORT"] = ""
os.environ["MCP_PORT_MAP"] = ""

PATH = "/mcp"
results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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


async def _serve(app, port: int):
    """启动 aiohttp app —— `runner.setup()` 会冻结路由表，这正是被测时序的关键一步。"""
    from aiohttp import web

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def _load_mcp_server():
    """按节点的真实路径导入 mcp_server（需先伪造 ComfyUI 的 server 模块）。"""
    from aiohttp import web

    server_mod = types.ModuleType("server")

    class _PromptServer:
        instance = None

    _PromptServer.instance = types.SimpleNamespace(app=web.Application())
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
    return importlib.import_module(PKG + ".mcp_server")


INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "selftest", "version": "0"},
    },
}
INIT_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


async def main() -> int:
    from aiohttp import ClientSession, web

    # 收尾时 cancel 掉 uvicorn 后端，lifespan 会打印一段 CancelledError traceback，
    # 与断言无关，压掉以免干扰阅读
    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)

    mcp_server = _load_mcp_server()
    check(
        "register_share_port_proxy 返回可回填的 BackendTarget",
        hasattr(mcp_server.register_share_port_proxy(web.Application(), path=PATH), "url"),
    )

    # ---- 场景 A：后端始终不来 —— 等到超时后回 503，而不是 500 / 连空地址 ----
    port_a = _free_port()
    app_a = web.Application()
    mcp_server.register_share_port_proxy(app_a, path=PATH, ready_timeout=0.3)
    runner_a = await _serve(app_a, port_a)
    async with ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port_a}{PATH}", json=INIT_PAYLOAD,
                          headers=INIT_HEADERS) as resp:
            st_a, body_a = resp.status, await resp.text()
            retry_a = resp.headers.get("Retry-After")
    check("A：等待超时后 → 503（明确信号）", st_a == 503, f"HTTP {st_a} {body_a[:60]}")
    check("A：带 Retry-After，客户端可自愈", retry_a is not None, f"Retry-After={retry_a}")
    await runner_a.cleanup()

    # ---- 场景 A2：把约束钉住 —— 冻结后再注册必然失败（这就是原来的报错） ----
    frozen_app = web.Application()
    frozen_runner = await _serve(frozen_app, _free_port())
    late_err = ""
    try:
        mcp_server.register_share_port_proxy(frozen_app, "http://127.0.0.1:1", PATH)
    except RuntimeError as exc:
        late_err = str(exc)
    check(
        "A2：冻结后注册 → RuntimeError(frozen router)（钉住约束）",
        "frozen" in late_err.lower(),
        late_err or "未抛异常",
    )
    await frozen_runner.cleanup()

    # ---- 场景 B：完整线上时序 —— 先注册 → 冻结 → 起后端 → 回填 → 打通 ----
    front_port = _free_port()
    front_app = web.Application()
    target = mcp_server.register_share_port_proxy(front_app, path=PATH)  # ① 冻结前注册
    front_runner = await _serve(front_app, front_port)                   # ② 路由表冻结
    check("B：冻结后目标仍为空（尚未回填）", target.url is None, str(target.url))

    # ③ 起真实嵌入式 MCP 后端，端口交给系统分配，就绪后回填（与 __init__.py 一致）
    ready: asyncio.Future = asyncio.get_running_loop().create_future()

    def _on_backend_ready(actual_port) -> None:
        if actual_port:
            target.url = f"http://127.0.0.1:{actual_port}"
        if not ready.done():
            ready.set_result(actual_port)

    backend_task = asyncio.ensure_future(
        mcp_server.serve_embedded(
            host="127.0.0.1", port=0, path=PATH, on_ready=_on_backend_ready
        )
    )
    try:
        # ④ 后端还没就绪就发请求：代理应当「等」而不是回 503（MCP 客户端只连一次）
        url = f"http://127.0.0.1:{front_port}{PATH}"
        async with ClientSession() as s:
            async with s.post(url, json=INIT_PAYLOAD, headers=INIT_HEADERS) as resp:
                st, body = resp.status, await resp.text()
        check("B：启动窗口内的请求被等到后端就绪（非 503）", st == 200, f"HTTP {st} {body[:80]}")
        info = (_parse_sse(body) or {}).get("result", {}).get("serverInfo", {})
        check("B：响应含 serverInfo（真的通到 MCP 后端）", bool(info), json.dumps(info)[:120])

        actual = await asyncio.wait_for(ready, timeout=30)
        check("B：后端回传真实端口", bool(actual), f"port={actual}")
        check(
            "B：回填后目标指向后端（未再动路由表）",
            target.url == f"http://127.0.0.1:{actual}",
            str(target.url),
        )
    finally:
        # cancel 嵌入式后端时 uvicorn 的 lifespan 会打一段 CancelledError traceback，
        # 与断言无关；uvicorn 启动时会重置 logger 级别，故用全局 disable 压掉
        logging.disable(logging.CRITICAL)
        backend_task.cancel()
        try:
            await backend_task
        except asyncio.CancelledError:
            pass
        await front_runner.cleanup()

    print()
    total, ok = len(results), sum(results)
    print(f"{ok}/{total} checks passed" + ("  ALL PASS" if ok == total else "  FAILED"))
    return 0 if ok == total else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    # 嵌入式后端的 uvicorn 可能有残留线程，直接结束进程避免挂住
    os._exit(code)
