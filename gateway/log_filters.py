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
"""

from __future__ import annotations

import logging
import os

AIOHTTP_SERVER_LOGGER = "aiohttp.server"
_ERROR_HANDLING_MSG = "Error handling request"

# 明确属于「对端走了」的异常类型。
# 注意不含 ConnectionRefusedError（那是连不上目标，属真故障，不该降级）。
_BENIGN_TYPES = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

# aiohttp 把「写向已关闭的 transport」包装成 RuntimeError/OSError 的历史路径
_CLOSING_HINTS = ("closing transport", "connection lost")


def client_gone(exc: BaseException) -> bool:
    """异常是否只是「连接被对端关掉」，而不是服务端真的出错。

    - `aiohttp.ClientConnectionResetError` 同时继承 ClientError 与 ConnectionResetError，
      因此落进 ConnectionResetError 分支；
    - Windows 上对端硬断开（RST）时抛的不一定是 ConnectionResetError，也可能是
      ConnectionAbortedError（WinError 10053）或 BrokenPipeError，都要算进来；
    - `ConnectionRefusedError` 不算：那是「连不上」，属于真故障。
    """
    if type(exc) is ConnectionError:  # aiohttp 自己抛的裸 ConnectionError
        return True
    if isinstance(exc, _BENIGN_TYPES):
        return True
    if isinstance(exc, (RuntimeError, OSError)) and not isinstance(exc, ConnectionRefusedError):
        text = str(exc).lower()
        return any(hint in text for hint in _CLOSING_HINTS)
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
