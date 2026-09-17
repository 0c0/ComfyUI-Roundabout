"""拦掉 aiohttp 为「客户端中途断开」打出的那条 ERROR。

（aiohttp 3.13 / ComfyUI 自带环境实测；换 aiohttp 大版本请重新核对 web_protocol.py。）

**为什么会有这条噪音**

aiohttp 的 `web_protocol.RequestHandler._handle_request` 捕获 handler 抛出的任何异常后，
会**先**无条件打一条日志，**再**去判断这条连接是不是早就断了：

    aiohttp/web_protocol.py
      except Exception as exc:
          resp = self.handle_error(request, 500, exc)   # ← 日志在这一步里就写出来了
      ...
      def handle_error(...):
          ...
          else:
              self.log_exception("Error handling request from %s", request.remote, exc_info=exc)
          # some data already got sent, connection is broken
          if request.writer.output_size > 0:
              raise ConnectionError("Response is sent already, ...")   # ← 直到这里才判定
              # 该 ConnectionError 由 start() 降级为 DEBUG「Ignored premature client disconnection」

也就是说：**日志已经落盘，降级判定才发生**。而 SSE / 长轮询（本项目 MCP streamable-http
的 `/mcp` 就是）下「客户端超时重连、旧连接被关掉」是常态，于是每次都会刷一条

    [ERROR] Error handling request from 192.168.1.10
    Traceback (most recent call last): ...

既看不出是哪个 handler，也看不出请求路径，只会让人以为服务端出错了。

**这里做什么**

在 `aiohttp.server` logger 上挂一个过滤器：只有同时满足

1. 是 aiohttp 那条通用消息「Error handling request ...」，且
2. 异常末端确实是「对端把连接关掉了」（ConnectionResetError / ConnectionAbortedError /
   BrokenPipeError，即 aiohttp.ClientConnectionResetError 那一族，或包装过的
   「Cannot write to closing transport」）

才把这条记录**丢弃**（`aiohttp.server` 若已开 DEBUG，则补一条不带 traceback 的精简行）。
**其它任何 ERROR 原样放行** —— 真的 bug 不会被吞掉。这与 aiohttp 自己的判断一致：
它在 `start()` / `finish_response()` 里对同一类异常也是走
`log_debug("Ignored premature client disconnection")`。

设 `ROUNDABOUT_RAW_AIOHTTP_LOGS=true` 可关闭本过滤器，恢复 aiohttp 原始日志。

---

**另一条噪音：无状态模式下的「Terminating session: None」**

MCP SDK 的 `StreamableHTTPSessionManager._handle_stateless_request` 在**每个请求**结束时都
收尾调用 `http_transport.terminate()`（streamable_http_manager.py），而 `terminate()` 里硬写了
一条 `logger.info(f"Terminating session: {self.mcp_session_id}")`（streamable_http.py）——
无状态模式下 `mcp_session_id` 恒为 `None`，于是每来一个 MCP 请求就刷一行

    [INFO] Terminating session: None

agent 连续调工具时就是刷屏。这行信息量为零（没有会话可终止），但**有**真实 session id 的
终止事件（有状态模式的 DELETE / 空闲超时）是有用的，不能整条 INFO 屏蔽掉，所以只丢
`None` 那一种。

设 `ROUNDABOUT_RAW_MCP_LOGS=true` 可关闭本过滤器。

---

**第三条噪音：Proactor 收尾回调的 `_call_connection_lost`（WinError 10054）**

Windows 的 `ProactorEventLoop` 在连接关闭时由
`_ProactorBasePipeTransport._call_connection_lost` 收尾，它会对 socket 调
`shutdown(SHUT_RDWR)`；如果对端已经 RST（浏览器刷新、agent 断连、代理取消任务时
硬断上游），这次 `shutdown` 会抛 `ConnectionResetError`（10054「远程主机强迫关闭了
一个现有的连接」）。该异常沿 `loop.call_exception_handler()` 落到 **`asyncio`
logger**，形如：

    [ERROR] Exception in callback _ProactorBasePipeTransport._call_connection_lost()
    ...
    ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

这是 CPython 在 Windows 上的已知形态（连接本来就在关闭流程里，异常不代表故障），
信息量为零但每次客户端硬断开都刷一条。过滤器判据同时满足才丢弃：

1. traceback 里真的经过 `_call_connection_lost` 帧（只认这一条收尾路径）；
2. 异常（或其链上）属于「对端走了」一族 —— 复用 `_BENIGN_TYPES` /
   `_BENIGN_WINERRORS`，**不含** ConnectionRefusedError（连不上目标仍是真故障）。

其它任何经 `asyncio` logger 的 ERROR 原样放行。

设 `ROUNDABOUT_RAW_ASYNCIO_LOGS=true` 可关闭本过滤器。
"""

from __future__ import annotations

import asyncio
import logging
import os

AIOHTTP_SERVER_LOGGER = "aiohttp.server"
_ERROR_HANDLING_MSG = "Error handling request"

# 明确属于「对端走了」的异常类型。
# 注意不含 ConnectionRefusedError（那是连不上目标，属真故障，不该降级）。
_BENIGN_TYPES = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

# Windows 上「对端把连接搞没了」的 winerror。
# 10053 / 10054 / 10058 多数情形已经由 ConnectionAbortedError / ConnectionResetError 覆盖，
# 这里额外认 **995 ERROR_OPERATION_ABORTED**：对端 RST 之后，挂起的 WSARecv / WSASend 会
# 以这个码收尾，而 Python 只把它映射成裸 `OSError` —— 既不属于 ConnectionError 族，消息里
# 也没有任何 "connection lost" / "closing transport" 字样，只看顶层类型必然漏判。
_BENIGN_WINERRORS = frozenset({995, 10053, 10054, 10058})

# aiohttp 把「写向已关闭的 transport」包装成 RuntimeError/OSError 的历史路径
_CLOSING_HINTS = ("closing transport", "connection lost")


def _exception_chain(exc: BaseException):
    """沿 `__cause__` / `__context__` 展开异常链（防环）。"""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__


def exception_chain_names(exc: BaseException) -> str:
    """异常链的类型名，用 ` <- ` 串起来 —— 给日志用：外层常常只是个包装器。"""
    return " <- ".join(type(item).__name__ for item in _exception_chain(exc))


def client_gone(exc: BaseException) -> bool:
    """异常（或它所包装的底层异常）是否只是「连接被对端关掉」，而不是服务端真的出错。

    判据要看**整条异常链**，不能只看最外层：代理里读上游字节时抛出来的常常是个包装器，
    真凶挂在 `__cause__` 上。实测形态 —— 顶层是 `OSError`(WinError 995)，
    `__cause__` 是 aiohttp 读流被取消时的 `asyncio.CancelledError`。

    - `aiohttp.ClientConnectionResetError` 同时继承 ClientError 与 ConnectionResetError，
      因此落进 ConnectionResetError 分支；
    - Windows 上对端硬断开（RST）时抛的不一定是 ConnectionResetError，也可能是
      ConnectionAbortedError（WinError 10053）/ BrokenPipeError / 裸 OSError(995)，都要算进来；
    - `asyncio.CancelledError` 算「对方走了」：代理自身从不取消任何任务，出现取消只可能是
      请求的生命周期已经结束（连接断了被 aiohttp / 事件循环收尾，或进程关停）；
    - `ConnectionRefusedError` 一票否决：那是「连不上目标」，属于真故障，哪怕链上还有
      别的断开痕迹也不能降级。
    """
    chain = list(_exception_chain(exc))
    if any(isinstance(item, ConnectionRefusedError) for item in chain):
        return False
    for item in chain:
        if type(item) is ConnectionError:  # aiohttp 自己抛的裸 ConnectionError
            return True
        if isinstance(item, _BENIGN_TYPES):
            return True
        if isinstance(item, asyncio.CancelledError):
            return True
        if getattr(item, "winerror", None) in _BENIGN_WINERRORS:
            return True
        if isinstance(item, (RuntimeError, OSError)):
            text = str(item).lower()
            if any(hint in text for hint in _CLOSING_HINTS):
                return True
    return False


class AiohttpDisconnectNoiseFilter(logging.Filter):
    """丢掉 aiohttp「Error handling request」中属于客户端断开的 ERROR 记录。

    注意不能只把 `record.levelno` 改成 DEBUG 了事：级别判定发生在过滤器**之前**
    （`Logger.isEnabledFor` → `Logger.handle` → `self.filter`），改成 DEBUG 的记录照样
    会被已经过关的 handler 打出来。所以这里直接返回 False 丢弃，只在 `aiohttp.server`
    本来就开着 DEBUG 时补一条不带 traceback 的精简行。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR or not record.exc_info:
            return True
        exc = record.exc_info[1]
        if not client_gone(exc):
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 拿不到原始消息就别动它
            return True
        # 只处理 aiohttp 那条通用 ERROR；handler 自己打的 ERROR 一律放行
        if _ERROR_HANDLING_MSG not in message:
            return True
        if record.name == AIOHTTP_SERVER_LOGGER and logging.getLogger(
            AIOHTTP_SERVER_LOGGER
        ).isEnabledFor(logging.DEBUG):
            # 这一条自身是 DEBUG 级，会再次经过本过滤器并在开头直接放行，不会递归。
            logging.getLogger(AIOHTTP_SERVER_LOGGER).debug(
                "%s (client disconnected; ERROR suppressed)", message
            )
        return False


def install_aiohttp_disconnect_noise_filter() -> bool:
    """给 aiohttp.server logger 装上降噪过滤器（幂等）。返回是否已生效。"""
    if os.getenv("ROUNDABOUT_RAW_AIOHTTP_LOGS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    logger = logging.getLogger(AIOHTTP_SERVER_LOGGER)
    if not any(isinstance(f, AiohttpDisconnectNoiseFilter) for f in logger.filters):
        logger.addFilter(AiohttpDisconnectNoiseFilter())
    return True


MCP_STREAMABLE_LOGGER = "mcp.server.streamable_http"
_TERMINATE_PREFIX = "Terminating session: "
_NO_SESSION = "None"


class McpStatelessTerminateNoiseFilter(logging.Filter):
    """丢掉无状态模式下 SDK 每个请求都打的「Terminating session: None」。

    只按消息内容判定：真实 session id 的终止（有状态模式）原样放行。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != MCP_STREAMABLE_LOGGER:
            return True
        if record.levelno != logging.INFO:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 拿不到原始消息就别动它
            return True
        if not message.startswith(_TERMINATE_PREFIX):
            return True
        return message[len(_TERMINATE_PREFIX):].strip() != _NO_SESSION


def install_mcp_stateless_terminate_noise_filter() -> bool:
    """给 mcp.server.streamable_http logger 装上降噪过滤器（幂等）。返回是否已生效。"""
    if os.getenv("ROUNDABOUT_RAW_MCP_LOGS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    logger = logging.getLogger(MCP_STREAMABLE_LOGGER)
    if not any(isinstance(f, McpStatelessTerminateNoiseFilter) for f in logger.filters):
        logger.addFilter(McpStatelessTerminateNoiseFilter())
    return True


ASYNCIO_LOGGER = "asyncio"
_CALL_CONNECTION_LOST = "_call_connection_lost"


class AsyncioProactorDisconnectNoiseFilter(logging.Filter):
    """丢掉 Proactor 连接收尾回调 `_call_connection_lost` 里的对端断开异常。

    CPython 的 `_ProactorBasePipeTransport._call_connection_lost` 在收尾时调
    `sock.shutdown(SHUT_RDWR)`，对端已 RST 时这条 `shutdown` 自己会抛
    ConnectionResetError(10054)——连接本来就在关闭流程里，异常不代表故障。
    判据（必须同时满足）：

    1. traceback 经过 `_call_connection_lost` 帧 —— 只认这一条收尾路径，
       别处抛出的断开异常（handler 内部、代理读流等）一律不动；
    2. 异常（或其链上）落在「对端走了」一族（`_BENIGN_TYPES` /
       `_BENIGN_WINERRORS`）。ConnectionRefusedError 不在族内，真故障照旧可见。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR or not record.exc_info:
            return True
        exc, tb = record.exc_info[1], record.exc_info[2]
        if exc is None or tb is None:
            return True
        while tb is not None and tb.tb_frame.f_code.co_name != _CALL_CONNECTION_LOST:
            tb = tb.tb_next
        if tb is None:  # 不是连接收尾回调这条路径，与本过滤器无关
            return True
        for item in _exception_chain(exc):
            if isinstance(item, _BENIGN_TYPES) or getattr(item, "winerror", None) in _BENIGN_WINERRORS:
                return False
        return True


def install_asyncio_proactor_disconnect_noise_filter() -> bool:
    """给 asyncio logger 装上降噪过滤器（幂等）。返回是否已生效。"""
    if os.getenv("ROUNDABOUT_RAW_ASYNCIO_LOGS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    logger = logging.getLogger(ASYNCIO_LOGGER)
    if not any(isinstance(f, AsyncioProactorDisconnectNoiseFilter) for f in logger.filters):
        logger.addFilter(AsyncioProactorDisconnectNoiseFilter())
    return True
