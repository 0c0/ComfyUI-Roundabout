"""验证 /mcp 共享端口代理（_proxy）对「客户端中途断开」与「上游不可达」的兜底。

背景（真实日志）：

    [INFO]  [Roundabout] MCP gateway: task 672ecf8e... completion notification sent
    [ERROR] Error handling request from 192.168.1.10
    Traceback (most recent call last):
      ...
      File "aiohttp/http_writer.py", line 106, in _writelines
        raise ClientConnectionResetError("Cannot write to closing transport")

`_proxy` 原来没有 try/except，是全项目**唯一**没有异常兜底的请求处理器（REST 路由有
gateway_handler，admin / viewer 也都有）。于是「客户端断开」这种常态被 aiohttp 放大成
一条 ERROR + 整段 traceback（web_protocol._handle_error）。

修复后：客户端断开 → DEBUG；上游后端不可达 → WARNING + 502。

> 本文件只测 `_proxy` 自己这一层，因此整场都**摘掉**了 aiohttp.server 上的降噪过滤器
> （`gateway/log_filters.py`）——否则「没有 ERROR」可能是被过滤器掩盖出来的假绿。
> 过滤器本身由 `test_aiohttp_error_noise.py` 单独覆盖。

本测试断言：
  1. 对照组：裸流式 handler（无兜底）遇到 RST 断开，**必须**产生 ERROR —— 证明场景真实；
  2. 真实 _proxy 遇到同样的断开，**不得**产生任何 ERROR 级日志；
  3. 上游后端不可达时，_proxy 返回 502 且同样不产生 ERROR。

不碰正在运行的 ComfyUI：全部使用系统分配的临时端口，跑完即退。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
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

# 不让节点 __init__.py 去拉嵌入式 MCP，本测试只关心代理层
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


class _Collector(logging.Handler):
    """收集 aiohttp.server 的日志记录，用来断言 ERROR 的有无。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record)

    def reset(self) -> None:
        self.records.clear()

    def errors(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.ERROR]


# ------------------------------------------------------------------ handlers
async def _bare_sse(request):
    """对照组：流式写，但**没有** try/except（等价于修复前的 _proxy）。"""
    from aiohttp import web

    resp = web.StreamResponse(status=200)
    resp.headers["Content-Type"] = "text/event-stream"
    resp.enable_chunked_encoding()
    await resp.prepare(request)
    for i in range(500):
        await resp.write(b"data: %d\n\n" % i)
        await asyncio.sleep(0.02)
    await resp.write_eof()
    return resp


async def _upstream_sse(request):
    """上游（模拟内部 uvicorn 后端）：持续推流，被断开时自己吞掉。

    真实部署里这一层是 uvicorn + MCP SDK；这里吞异常只是为了让断言只盯着代理层。
    """
    from aiohttp import web

    resp = web.StreamResponse(status=200)
    resp.headers["Content-Type"] = "text/event-stream"
    resp.enable_chunked_encoding()
    await resp.prepare(request)
    try:
        for i in range(500):
            await resp.write(b"data: %d\n\n" % i)
            await asyncio.sleep(0.02)
        await resp.write_eof()
    except BaseException:  # noqa: BLE001 - 代理断开后上游也会写失败
        resp._eof_sent = True  # 已终结，别再让 finish_response 重写
    return resp


# ------------------------------------------------------------------ helpers
async def _serve(app, port: int):
    from aiohttp import web

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def _connect_and_rst(port: int, path: str, hold: float = 0.4) -> bytes:
    """连上去、读一点、然后 RST 硬断开（等价于客户端进程消失）。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n".encode()
    )
    await writer.drain()
    try:
        data = await asyncio.wait_for(reader.read(80), timeout=3)
    except asyncio.TimeoutError:
        data = b""
    await asyncio.sleep(hold)
    writer.transport.abort()
    return data


async def main() -> int:
    from aiohttp import ClientSession, web

    srv_collector = _Collector()
    srv_logger = logging.getLogger("aiohttp.server")
    srv_logger.setLevel(logging.DEBUG)
    srv_logger.addHandler(srv_collector)

    # 代理自己的 WARNING/DEBUG 走 roundabout.mcp，另挂一个收集器
    gw_collector = _Collector()
    gw_logger = logging.getLogger("roundabout.mcp")
    gw_logger.setLevel(logging.DEBUG)
    gw_logger.addHandler(gw_collector)

    # 加载节点包（与节点 __init__.py 同一条真实路径）
    app_placeholder = web.Application()
    server_mod = types.ModuleType("server")

    class _PromptServer:
        instance = None

    _PromptServer.instance = types.SimpleNamespace(app=app_placeholder)
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

    # 关掉 MCP_ENABLED 时 __init__.py 不拉 mcp_server，本测试只关心代理层，显式导入
    mcp_server = importlib.import_module(PKG + ".mcp_server")
    log_filters = importlib.import_module(PKG + ".gateway.log_filters")
    check("register_share_port_proxy 可用", callable(mcp_server.register_share_port_proxy))

    # ---- 对照组：裸 handler + RST —— 必须产生 ERROR（证明场景真实存在）----
    # 节点 import 时已在 aiohttp.server 上装了降噪过滤器（gateway/log_filters.py），
    # 对照组要证明的是「没有这层兜底时确实会刷 ERROR」，所以先把它摘掉，跑完再装回。
    saved_filters = list(srv_logger.filters)
    srv_logger.filters = [
        f for f in saved_filters if not isinstance(f, log_filters.AiohttpDisconnectNoiseFilter)
    ]
    srv_collector.reset()
    bare_port = _free_port()
    bare_app = web.Application()
    bare_app.router.add_route("*", PATH, _bare_sse)
    bare_runner = await _serve(bare_app, bare_port)
    got = await _connect_and_rst(bare_port, PATH)
    await asyncio.sleep(1.2)
    bare_errs = srv_collector.errors()
    check("对照组：客户端读到数据后 RST 断开", len(got) > 0, f"{len(got)} bytes")
    check(
        "对照组：无兜底 handler 产生 'Error handling request'",
        any("Error handling request" in str(r.getMessage()) for r in bare_errs),
        f"{len(bare_errs)} error(s)",
    )
    check(
        "对照组：异常正是写向已关闭连接",
        any(
            getattr(r, "exc_info", None)
            and "reset" in type(r.exc_info[1]).__name__.lower()
            for r in bare_errs
        ),
        ", ".join(type(r.exc_info[1]).__name__ for r in bare_errs if r.exc_info),
    )
    await bare_runner.cleanup()

    # ---- 场景 A：真实 _proxy + 客户端中途断开 —— 不得产生 ERROR ----
    srv_collector.reset()
    gw_collector.reset()
    up_port, front_port = _free_port(), _free_port()
    up_app = web.Application()
    up_app.router.add_route("*", PATH, _upstream_sse)
    up_runner = await _serve(up_app, up_port)

    front_app = web.Application()
    mcp_server.register_share_port_proxy(front_app, f"http://127.0.0.1:{up_port}", PATH)
    front_runner = await _serve(front_app, front_port)

    got = await _connect_and_rst(front_port, PATH)
    await asyncio.sleep(1.5)
    proxy_errs = srv_collector.errors()
    check("代理：客户端读到数据后 RST 断开", len(got) > 0, f"{len(got)} bytes")
    check(
        "代理：客户端断开不再产生 ERROR",
        not proxy_errs,
        " | ".join(f"{r.levelname} {r.getMessage()}" for r in proxy_errs),
    )
    check(
        "代理：断开确实走了兜底分支（降为 DEBUG）",
        any("closed the" in str(r.getMessage()) for r in gw_collector.records),
        " | ".join(f"{r.levelname} {r.getMessage()}" for r in gw_collector.records),
    )
    await front_runner.cleanup()
    await up_runner.cleanup()

    # ---- 场景 B：上游后端不可达 —— 502，且不打 ERROR ----
    srv_collector.reset()
    gw_collector.reset()
    dead_port, front_port = _free_port(), _free_port()
    front_app = web.Application()
    mcp_server.register_share_port_proxy(front_app, f"http://127.0.0.1:{dead_port}", PATH)
    front_runner = await _serve(front_app, front_port)

    async with ClientSession() as s:
        async with s.post(
            f"http://127.0.0.1:{front_port}{PATH}",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        ) as resp:
            status, body = resp.status, await resp.text()
    await asyncio.sleep(0.6)
    unreach_errs = srv_collector.errors()
    check("上游不可达：返回 502", status == 502, f"HTTP {status} {body[:60]}")
    check(
        "上游不可达：不打 ERROR（降为 WARNING）",
        not unreach_errs,
        " | ".join(f"{r.levelname} {r.getMessage()}" for r in unreach_errs),
    )
    check(
        "上游不可达：给出可操作的 WARNING",
        any("unreachable" in str(r.getMessage()) for r in gw_collector.records),
        " | ".join(f"{r.levelname} {r.getMessage()}" for r in gw_collector.records),
    )
    await front_runner.cleanup()

    # 恢复降噪过滤器（对照组起就摘掉了，见上）：A/B 两段刻意在「没有过滤器」的前提下跑，
    # 这样断言的是 _proxy 自己的兜底是否成立，而不是被日志过滤器掩盖过去。
    srv_logger.filters = saved_filters

    print()
    total, ok = len(results), sum(results)
    print(f"{ok}/{total} checks passed" + ("  ALL PASS" if ok == total else "  FAILED"))
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
