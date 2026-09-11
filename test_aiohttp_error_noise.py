"""验证「[ERROR] Error handling request from <ip>」这条噪音被真正处理掉。

背景（真实日志，长任务 / SSE 场景）：

    [INFO]  [Roundabout] req=1b9ee2a7e95c model=minimax-h3-turbo n=1 videos=1 elapsed=148.4s
    [INFO]  [Roundabout] MCP gateway: task 5f6e9843... completion notification sent (status=succeeded)
    [ERROR] Error handling request from 192.168.31.198
    Traceback (most recent call last): ...

这条 ERROR 由 aiohttp 的 `RequestHandler.handle_error` 打出，而它是**先打日志、再判断
连接是否已经断掉**（见 aiohttp/web_protocol.py：`handle_error()` 里先 `log_exception(...)`，
之后才 `if request.writer.output_size > 0: raise ConnectionError(...)`，该 ConnectionError
由 `start()` 降级为 DEBUG「Ignored premature client disconnection」）。所以 SSE / 长轮询下
「客户端超时重连、旧连接被关掉」这种常态，每次都会留下一条 ERROR + 整段 traceback。

本测试覆盖两条防线：

  1. `gateway/log_filters.py` —— 挂在 `aiohttp.server` 上的过滤器，把「Error handling
     request」里末端异常属于客户端断开（ConnectionError 一族 / closing transport）的记录
     降为 DEBUG；**其它异常一律保留 ERROR**（ConnectionRefusedError、ValueError 等）；
  2. `mcp_server._proxy` 的兜底 —— 「客户端在后端回第一个字节前就走」「上游中途断流」
     「任何非连接类异常」都不再逃回 aiohttp。

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
NODE = Path(__file__).resolve().parent
ROOT = NODE.parent.parent
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 不让节点 __init__.py 去拉嵌入式 MCP；也不让 mcp_server 重配 logging
os.environ["MCP_ENABLED"] = "false"
os.environ["MCP_PORT"] = ""
os.environ["MCP_PORT_MAP"] = ""
os.environ["ROUNDABOUT_MCP_EMBEDDED"] = "1"
os.environ.pop("ROUNDABOUT_RAW_AIOHTTP_LOGS", None)

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
    """收集某个 logger 的记录，用来断言 ERROR 的有无。"""

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._logger = logger
        logger.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record)

    def reset(self) -> None:
        self.records.clear()

    def errors(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.ERROR]

    def messages(self, needle: str) -> list[logging.LogRecord]:
        return [r for r in self.records if needle in r.getMessage()]


# ------------------------------------------------------------------ handlers
async def _slow_upstream_sse(request):
    """上游（模拟内部 uvicorn 后端）：**先拖一会儿**再回响应。

    对应真实场景：MCP 客户端同步等一个 148s 的视频任务，等不到就自己断开走了，
    而服务端这时才拿到结果、开始往那条早已关闭的连接写第一批字节。
    """
    from aiohttp import web

    await asyncio.sleep(1.2)
    resp = web.StreamResponse(status=200)
    resp.headers["Content-Type"] = "text/event-stream"
    resp.enable_chunked_encoding()
    try:
        await resp.prepare(request)
        for i in range(200):
            await resp.write(b"data: %d\n\n" % i)
            await asyncio.sleep(0.02)
        await resp.write_eof()
    except BaseException:  # noqa: BLE001 - 代理断开后上游也会写失败
        resp._eof_sent = True
    return resp


# ------------------------------------------------------------------ helpers
async def _serve(app, port: int):
    from aiohttp import web

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def _connect_and_abort(port: int, path: str) -> None:
    """连上去、发完请求立刻 RST（不读任何响应）——客户端在等结果时就走了。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n".encode())
    await writer.drain()
    await asyncio.sleep(0.25)
    writer.transport.abort()
    del reader


# ------------------------------------------------------------------ part 1：日志过滤器
def _record(exc: BaseException | None, msg: str = "Error handling request from %s") -> logging.LogRecord:
    if exc is None:
        exc_info = None
    else:
        try:
            raise exc
        except BaseException:  # noqa: BLE001
            exc_info = sys.exc_info()
    return logging.LogRecord(
        "aiohttp.server", logging.ERROR, __file__, 1, msg, ("192.168.31.198",), exc_info
    )


def part_filter(log_filters) -> None:
    from aiohttp import ClientConnectionResetError

    flt = log_filters.AiohttpDisconnectNoiseFilter()

    def suppressed(exc: BaseException, msg: str = "Error handling request from %s") -> tuple[bool, str]:
        """过滤器返回 False = 记录被丢掉（不落盘）。"""
        rec = _record(exc, msg)
        kept = flt.filter(rec)
        return not kept, rec.levelname

    check(
        "节点 import 时已自动装上过滤器",
        any(
            isinstance(f, log_filters.AiohttpDisconnectNoiseFilter)
            for f in logging.getLogger("aiohttp.server").filters
        ),
        "见 __init__.py 的 install_aiohttp_disconnect_noise_filter()",
    )
    before = len(logging.getLogger("aiohttp.server").filters)
    log_filters.install_aiohttp_disconnect_noise_filter()
    check(
        "重复安装幂等",
        len(logging.getLogger("aiohttp.server").filters) == before,
        f"{before} filter(s)",
    )

    # —— 该丢掉的：这些都是「对端把连接关掉了」——
    for exc, label in (
        (ConnectionResetError("Connection lost"), "ConnectionResetError"),
        (ClientConnectionResetError("Cannot write to closing transport"), "ClientConnectionResetError"),
        (ConnectionAbortedError(10053), "ConnectionAbortedError(WinError 10053)"),
        (BrokenPipeError(32), "BrokenPipeError"),
        (RuntimeError("Cannot write to closing transport"), "RuntimeError(closing transport)"),
        (ConnectionError("Response is sent already"), "裸 ConnectionError"),
    ):
        ok, level = suppressed(exc)
        check(f"丢弃：{label}", ok, level)

    # —— 不该动的：真故障 / 与连接无关 ——
    for exc, label in (
        (ConnectionRefusedError(10061), "ConnectionRefusedError(连不上后端)"),
        (ValueError("bad header value"), "ValueError"),
        (RuntimeError("dictionary changed size during iteration"), "RuntimeError(无关)"),
    ):
        ok, level = suppressed(exc)
        check(f"保留 ERROR：{label}", not ok, level)

    # —— 只处理 aiohttp 那条通用消息，handler 自己打的 ERROR 不动 ——
    ok, level = suppressed(ConnectionResetError("Connection lost"), msg="unexpected error proxying GET /mcp: x")
    check("保留 ERROR：非 aiohttp 通用消息", not ok, level)

    # —— 非 ERROR 级别不参与 ——
    rec = _record(ConnectionResetError("Connection lost"))
    rec.levelno = logging.WARNING
    rec.levelname = "WARNING"
    check("过滤器只处理 ERROR 级", flt.filter(rec) and rec.exc_info is not None, rec.levelname)

    # —— 真的把记录从 aiohttp.server 上拦掉了（端到端过一次 logger）----
    srv = logging.getLogger("aiohttp.server")
    sink = _Collector(srv)
    try:
        try:
            raise ClientConnectionResetError("Cannot write to closing transport")
        except BaseException:  # noqa: BLE001
            srv.error("Error handling request from %s", "192.168.31.198", exc_info=True)
        try:
            raise ValueError("real bug")
        except BaseException:  # noqa: BLE001
            srv.error("Error handling request from %s", "192.168.31.198", exc_info=True)
        lines = [r for r in sink.records if "Error handling request" in r.getMessage()]
        kept_errors = [r for r in lines if r.levelno >= logging.ERROR]
        check(
            "端到端：断开被拦下（只剩一条 DEBUG 精简行）、真异常照旧落盘",
            any("ERROR suppressed" in r.getMessage() for r in lines)
            and len(kept_errors) == 1
            and "real bug" in str(kept_errors[0].exc_info[1]),
            " | ".join(f"{r.levelname} {r.getMessage()[:70]}" for r in lines),
        )
    finally:
        srv.removeHandler(sink)


# ------------------------------------------------------------------ part 2/3：端到端
async def main() -> int:
    from aiohttp import ClientSession, web

    srv_logger = logging.getLogger("aiohttp.server")
    srv_logger.setLevel(logging.DEBUG)
    srv_logger.propagate = False
    srv_collector = _Collector(srv_logger)

    gw_logger = logging.getLogger("roundabout.mcp")
    gw_logger.setLevel(logging.DEBUG)
    gw_logger.propagate = False
    gw_collector = _Collector(gw_logger)

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

    mcp_server = importlib.import_module(PKG + ".mcp_server")
    log_filters = importlib.import_module(PKG + ".gateway.log_filters")

    print("\n[1] 日志过滤器（gateway/log_filters.py）")
    part_filter(log_filters)

    # ---- 场景 A：客户端在「后端回第一个字节之前」就走了 ----
    print("\n[2] 端到端：客户端在后端回包前断开")
    srv_collector.reset()
    gw_collector.reset()
    up_port, front_port = _free_port(), _free_port()
    up_app = web.Application()
    up_app.router.add_route("*", PATH, _slow_upstream_sse)
    up_runner = await _serve(up_app, up_port)

    front_app = web.Application()
    mcp_server.register_share_port_proxy(front_app, f"http://127.0.0.1:{up_port}", PATH)
    front_runner = await _serve(front_app, front_port)

    await _connect_and_abort(front_port, PATH)
    await asyncio.sleep(2.0)  # 等上游那 1.2s 的延迟过去、代理真的尝试写一次
    errs = srv_collector.errors()
    check(
        "A：客户端等不及离开 → 不打 ERROR",
        not errs,
        " | ".join(f"{r.levelname} {r.getMessage()}" for r in errs),
    )
    check(
        "A：走了兜底分支（降为 DEBUG 且带上请求路径）",
        any(PATH in r.getMessage() for r in gw_collector.records),
        " | ".join(f"{r.levelname} {r.getMessage()}" for r in gw_collector.records),
    )
    await front_runner.cleanup()
    await up_runner.cleanup()

    # ---- 场景 B：代理内部出现「与连接无关」的真异常 ----
    print("\n[3] 端到端：非连接类异常不再逃回 aiohttp")
    srv_collector.reset()
    gw_collector.reset()
    up_port, front_port = _free_port(), _free_port()
    up_app = web.Application()
    up_app.router.add_route("*", PATH, _slow_upstream_sse)
    up_runner = await _serve(up_app, up_port)

    front_app = web.Application()
    mcp_server.register_share_port_proxy(front_app, f"http://127.0.0.1:{up_port}", PATH)
    front_runner = await _serve(front_app, front_port)

    # 注入点：只让「打到代理（front_port）上的响应」在 prepare 时抛一个与连接无关的
    # RuntimeError，模拟代理内部真出 bug。上游自己的 prepare 必须正常，否则注入的异常
    # 会先被上游吞掉，测不到代理这一层。
    real_prepare = web.StreamResponse.prepare
    state = {"raised": False}
    front_suffix = f":{front_port}"

    async def _boom_prepare(self, request):
        # 只炸一次：之后 500 响应自己的 prepare 必须正常，否则异常会跑到 aiohttp 的
        # finish_response 之外（那里只兜 ConnectionError），连接会被直接掐断。
        if not state["raised"] and (request.host or "").endswith(front_suffix):
            state["raised"] = True
            raise RuntimeError("injected upstream bug")
        return await real_prepare(self, request)

    web.StreamResponse.prepare = _boom_prepare
    try:
        async with ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{front_port}{PATH}") as resp:
                status = resp.status
                await resp.read()
    finally:
        web.StreamResponse.prepare = real_prepare
    await asyncio.sleep(0.5)

    errs = srv_collector.errors()
    check("B：注入的异常确实触发了", state["raised"], "prepare 抛了一次")
    check("B：客户端拿到 500（而不是连接被打断）", status == 500, f"HTTP {status}")
    check(
        "B：aiohttp 不再打 'Error handling request'",
        not errs,
        " | ".join(f"{r.levelname} {r.getMessage()}" for r in errs),
    )
    check(
        "B：代理自己打了带上下文的 ERROR",
        any(
            "unexpected error proxying" in r.getMessage() and "injected upstream bug" in r.getMessage()
            for r in gw_collector.errors()
        ),
        " | ".join(r.getMessage() for r in gw_collector.records if r.levelno >= logging.WARNING),
    )
    await front_runner.cleanup()
    await up_runner.cleanup()

    print()
    total, ok = len(results), sum(results)
    print(f"{ok}/{total} checks passed" + ("  ALL PASS" if ok == total else "  FAILED"))
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
