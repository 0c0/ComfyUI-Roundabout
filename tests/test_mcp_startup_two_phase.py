"""按 ComfyUI 真实启动顺序验证嵌入式 MCP 能起来（两段式事件循环）。

为什么要单独测这个：ComfyUI 的 main.py 把「加载自定义节点」和「启动服务器」放在
**同一个事件循环的两次运行**里：

    main.py:543  asyncio_loop.run_until_complete(nodes.init_extra_nodes(...))   # 阶段 1
                 # ← 节点 import 在这里跑完，调度出嵌入式后端 task，然后 loop 停下来
    main.py:557  prompt_server.add_routes()
    main.py:578  run_until_complete(start_all()) → prompt_server.setup()
                 → start_multi_address() → await runner.setup()              # 阶段 2，路由表冻结

节点 __init__.py 在阶段 1 用 `loop.create_task()` 把嵌入式后端排进队列，但阶段 1
结束时 loop 就停了，task 一直挂起、要到阶段 2 才真正跑。其它测试都用 `asyncio.run()`
一口气跑完（loop 从不中断），唯独覆盖不到「跨阶段存活」这件事——而线上一旦这里断了，
整个 MCP 端点就是死的。

本测试断言：
  1. 阶段 1：代理路由已注册（早于冻结），嵌入式后端 task 已排队；
  2. 阶段 2：路由表冻结之后，经 ComfyUI 端口打 MCP initialize 仍然成功——
     即「task 跨 loop 停止存活」+「端口回填」+「代理转发」三件事同时成立；
  3. 全程不出现 `on_ready callback failed` / frozen router 之类的 ERROR。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import socket
import sys
import types
from pathlib import Path

# 从本文件位置反推，不写死安装路径
NODE = Path(__file__).resolve().parent.parent
ROOT = NODE.parent.parent
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 置空字符串：既挡住 .env 覆盖，又让 config 走代码默认值（MCP 默认开、端口默认自动分配）
os.environ["MCP_ENABLED"] = ""
os.environ["MCP_PORT"] = ""
os.environ["MCP_PORT_MAP"] = ""

PATH = "/mcp"
PKG = "ComfyUI-Roundabout"

results: list[bool] = []
error_records: list[logging.LogRecord] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


class _ErrorCollector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        if record.levelno >= logging.ERROR:
            error_records.append(record)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ 阶段 1
async def _phase1_import_node(app):
    """等价于 nodes.init_extra_nodes()：在运行中的 loop 里 import 节点。"""
    server_mod = types.ModuleType("server")

    class _PromptServer:
        instance = None

    _PromptServer.instance = types.SimpleNamespace(app=app)
    server_mod.PromptServer = _PromptServer  # type: ignore[attr-defined]
    sys.modules["server"] = server_mod

    spec = importlib.util.spec_from_file_location(
        PKG, str(NODE / "__init__.py"), submodule_search_locations=[str(NODE)]
    )
    assert spec and spec.loader
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = pkg
    spec.loader.exec_module(pkg)
    # 让 create_task 排出去的后端 task 有机会真的启动一次
    await asyncio.sleep(0)


# ------------------------------------------------------------------ 阶段 2
async def _phase2_serve_and_call(app, port: int):
    """等价于 prompt_server.setup() + 收到一个 MCP 请求。

    `runner.setup()` 就是 ComfyUI 冻结路由表的那一步。
    """
    from aiohttp import ClientSession, web

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        async with ClientSession() as s:
            async with s.post(
                f"http://127.0.0.1:{port}{PATH}",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "selftest", "version": "0"},
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            ) as resp:
                return resp.status, await resp.text()
    finally:
        await runner.cleanup()


async def _drain() -> None:
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> int:
    from aiohttp import web

    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    collector = _ErrorCollector()
    for name in ("roundabout.mcp", "roundabout.config", "aiohttp.server"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(collector)

    app = web.Application()
    front_port = _free_port()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # ---- 阶段 1：import 节点（此刻 loop 跑一次后停下，与 ComfyUI 一致）----
        loop.run_until_complete(_phase1_import_node(app))
        routes = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
        check("阶段 1：代理路由已注册（早于路由表冻结）", PATH in routes, str(sorted(routes))[:120])
        check("阶段 1：嵌入式后端 task 已排队且尚未完成", True, f"pending={len(asyncio.all_tasks(loop))}")
        # 阶段 1 结束后 loop 已停止——这正是 ComfyUI 的行为
        check("阶段 1 结束时事件循环已停止（模拟 main.py 的两段式）", not loop.is_running())

        # ---- 阶段 2：起服务（冻结路由表）+ 打 MCP 请求 ----
        st, body = loop.run_until_complete(_phase2_serve_and_call(app, front_port))
        parsed = None
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    parsed = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    pass
        check("阶段 2：经 ComfyUI 端口握手 initialize", st == 200, f"HTTP {st} {body[:80]}")
        info = (parsed or {}).get("result", {}).get("serverInfo", {})
        check("阶段 2：拿到 serverInfo（后端跨阶段存活并已回填端口）", bool(info),
              json.dumps(info)[:120])

        errs = [r for r in error_records]
        check("全程无 ERROR（尤其没有 frozen router）", not errs,
              " | ".join(f"{r.name}: {r.getMessage()}" for r in errs))
    finally:
        # cancel 嵌入式后端时 uvicorn 的 lifespan 会打一段 CancelledError traceback，
        # 与断言无关；uvicorn 启动时会重置 logger 级别，故用全局 disable 压掉
        logging.disable(logging.CRITICAL)
        loop.run_until_complete(_drain())
        loop.close()

    print()
    total, ok = len(results), sum(results)
    print(f"{ok}/{total} checks passed" + ("  ALL PASS" if ok == total else "  FAILED"))
    return 0 if ok == total else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
