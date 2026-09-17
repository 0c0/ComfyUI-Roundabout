"""验证 Proactor 收尾回调的「_call_connection_lost + WinError 10054」噪音被拦掉。

背景（真实日志，本地 ComfyUI，Windows ProactorEventLoop）：

    [ERROR] Exception in callback _ProactorBasePipeTransport._call_connection_lost()
    handle: <Handle _ProactorBasePipeTransport._call_connection_lost()>
    Traceback (most recent call last):
      File "...\\asyncio\\events.py", line 89, in _run
        self._context.run(self._callback, *self._args)
      File "...\\asyncio\\proactor_events.py", line 165, in _call_connection_lost
        self._sock.shutdown(socket.SHUT_RDWR)
    ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

成因：连接关闭流程里，CPython 的 `_ProactorBasePipeTransport._call_connection_lost`
对 socket 调 `shutdown(SHUT_RDWR)`；对端已经 RST（浏览器刷新 / agent 断连 / 代理取消
任务硬断上游）时这条 `shutdown` 自己抛 10054，沿 `loop.call_exception_handler()`
落到 **`asyncio` logger**。连接本来就在关闭流程里，异常不代表故障。

本测试覆盖三条防线：

  1. `gateway/log_filters.py` 的 `AsyncioProactorDisconnectNoiseFilter` —— 只丢
     「traceback 经过 `_call_connection_lost` 帧 **且** 异常属『对端走了』一族」的
     ERROR；其余（ValueError / 别处抛的断开 / ConnectionRefusedError）原样放行；
  2. 端到端过一次真实 `asyncio` logger（collector 收集），复现真实日志形态；
  3. 接线：节点 `__init__.py` 在 import 期真的调了安装函数，且幂等、有逃生开关。

不碰正在运行的 ComfyUI，不起网络服务。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

NODE = Path(__file__).resolve().parent.parent
ROOT = NODE.parent.parent
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.pop("ROUNDABOUT_RAW_ASYNCIO_LOGS", None)

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def _call_connection_lost(exc):
    """与 CPython 收尾回调同名：过滤器按栈帧名定位这条路径。"""
    raise exc


def _handle_request(exc):
    """别处的断开异常（如 handler 内部），不该被本过滤器吞。"""
    raise exc


def _raise_in_frame(exc: BaseException, frame_name: str):
    """在指定名字的栈帧里抛出异常，返回 (type, exc, tb) —— 模拟 CPython 的收尾路径。"""
    frames = {"_call_connection_lost": _call_connection_lost, "_handle_request": _handle_request}
    try:
        frames[frame_name](exc)
    except BaseException:  # noqa: BLE001
        return sys.exc_info()


class _Collector(logging.Handler):
    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        logger.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record)


def main() -> int:
    import importlib.util

    # 直接按文件路径加载 gateway/log_filters.py，不拉起整个节点包
    # （那要喂假的 PromptServer / 注册表，本测试只关心过滤器本身；
    #   import 期接线由下面的源码断言覆盖。）
    spec = importlib.util.spec_from_file_location(
        "log_filters_under_test", str(NODE / "gateway" / "log_filters.py")
    )
    assert spec and spec.loader
    lf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lf)
    flt = lf.AsyncioProactorDisconnectNoiseFilter()

    def suppressed(exc: BaseException, frame: str = "_call_connection_lost") -> bool:
        """返回 True = 记录被丢掉（不落盘）。"""
        rec = logging.LogRecord(
            "asyncio", logging.ERROR, __file__, 1,
            "Exception in callback %s()", ("_ProactorBasePipeTransport._call_connection_lost",),
            _raise_in_frame(exc, frame),
        )
        return not flt.filter(rec)

    # —— 该丢掉的：收尾路径上「对端走了」一族的断开异常 ——
    reset = ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
    if not hasattr(reset, "winerror"):  # 非 Windows  runner 上手工补，贴近真实形态
        reset.winerror = 10054
    check("丢弃：ConnectionResetError(WinError 10054)", suppressed(reset))

    aborted = ConnectionAbortedError(10053, "你主机中的软件中止了一个已建立的连接")
    if not hasattr(aborted, "winerror"):
        aborted.winerror = 10053
    check("丢弃：ConnectionAbortedError(WinError 10053)", suppressed(aborted))

    op_aborted = OSError(995, "由于线程退出或应用程序请求，I/O 操作已中止")
    op_aborted.winerror = 995
    check("丢弃：裸 OSError(WinError 995)", suppressed(op_aborted))

    wrapped = RuntimeError("transport bookkeeping failed")
    wrapped.__cause__ = reset
    check("丢弃：外层包装器，真凶挂在 __cause__", suppressed(wrapped))

    # —— 不该动的 ——
    check("保留 ERROR：ValueError（真 bug）", not suppressed(ValueError("bad state")))
    check(
        "保留 ERROR：同样异常但抛在别处（_handle_request 帧）",
        not suppressed(ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接"), frame="_handle_request"),
    )
    refused = ConnectionRefusedError(10061, "由于目标计算机积极拒绝，无法连接")
    refused.winerror = 10061
    check("保留 ERROR：ConnectionRefusedError（连不上目标）", not suppressed(refused))

    rec_no_exc = logging.LogRecord("asyncio", logging.ERROR, __file__, 1, "plain message", (), None)
    check("保留 ERROR：无 exc_info 的记录", flt.filter(rec_no_exc))

    # —— 逃生开关 ——
    os.environ["ROUNDABOUT_RAW_ASYNCIO_LOGS"] = "true"
    try:
        check("逃生开关：安装函数返回 False（不生效）",
              lf.install_asyncio_proactor_disconnect_noise_filter() is False)
    finally:
        os.environ.pop("ROUNDABOUT_RAW_ASYNCIO_LOGS", None)

    # —— 接线：节点 import 期真的装上了（与 aiohttp / MCP 过滤器同一姿势）----
    src = (NODE / "__init__.py").read_text(encoding="utf-8")
    check("__init__.py 调了安装函数",
          "install_asyncio_proactor_disconnect_noise_filter()" in src)

    # —— 安装幂等：连装两次只留一个实例 ——
    logger = logging.getLogger(lf.ASYNCIO_LOGGER)
    lf.install_asyncio_proactor_disconnect_noise_filter()
    lf.install_asyncio_proactor_disconnect_noise_filter()
    check("安装幂等（重复调用不叠加）",
          sum(isinstance(f, lf.AsyncioProactorDisconnectNoiseFilter) for f in logger.filters) == 1,
          f"{len(logger.filters)} filter(s)")

    # —— 端到端：真实 logger 形态（CPython 默认异常 handler 的打日志姿势）----
    asyncio_logger = logging.getLogger("asyncio")
    sink = _Collector(asyncio_logger)
    try:
        try:
            raise reset
        except BaseException:  # noqa: BLE001
            asyncio_logger.error(
                "Exception in callback %s",
                "<Handle _ProactorBasePipeTransport._call_connection_lost()>",
                exc_info=True,
            )
        try:
            raise ValueError("real bug")
        except BaseException:  # noqa: BLE001
            asyncio_logger.error("Exception in callback %s", "<Handle something.blew_up()>", exc_info=True)
        errs = [r for r in sink.records if r.levelno >= logging.ERROR]
        kept = [r for r in errs if "real bug" in str(r.exc_info[1])]
        check("端到端：收尾断开被拦、真异常照旧落盘", len(errs) == 1 and len(kept) == 1,
              f"errors={len(errs)}")
    finally:
        asyncio_logger.removeHandler(sink)

    print()
    total, ok = len(results), sum(results)
    print(f"{ok}/{total} checks passed" + ("  ALL PASS" if ok == total else "  FAILED"))
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
