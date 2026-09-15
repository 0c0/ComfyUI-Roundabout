"""验证无状态模式下「[INFO] Terminating session: None」这条刷屏被丢掉。

背景（无状态模式 MCP_STATELESS=true 后的真实日志）：

    [INFO] Terminating session: None
    [INFO] Terminating session: None
    [INFO] Terminating session: None     # agent 每调一次工具就一条

源头是 MCP SDK：`StreamableHTTPSessionManager._handle_stateless_request` **每个请求**收尾都调
`http_transport.terminate()`，而 `terminate()` 里硬写了一条
`logger.info(f"Terminating session: {self.mcp_session_id}")`——无状态模式下 `mcp_session_id`
恒为 `None`，于是这行信息量为零的日志成了刷屏源。

不能整条 INFO 屏蔽：有状态模式下「Terminating session: <hex>」是真实事件（DELETE 请求、
空闲超时），有用。所以过滤器只丢 session id 为 `None` 的那一种。

验证点：
  [1] 过滤器判定：只丢 `Terminating session: None` 的 INFO；真实 session id、其它消息、
      非 INFO 级别、其它 logger 的记录全部放行。
  [2] 走真实 logger：装好后打三条（None / 真 id / 其它），只有 None 那条消失
      —— 用「真 id 与其它两条被收到」证明 INFO 级别是开着的，不是被级别挡掉假绿。
  [3] 幂等 + 逃生开关：重复安装不叠加；`ROUNDABOUT_RAW_MCP_LOGS=true` 时不装。
  [4] 接线：__init__.py 真的调了 install_mcp_stateless_terminate_noise_filter()。

自测：python tests/test_mcp_log_noise.py
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

NODE = Path(__file__).resolve().parent.parent
ROOT = NODE.parent.parent
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 逃生开关必须在安装前是干净的
os.environ.pop("ROUNDABOUT_RAW_MCP_LOGS", None)

MCP_LOGGER = "mcp.server.streamable_http"
results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def load_log_filters():
    """按文件路径加载，避免拉进节点 __init__（那需要 ComfyUI 的 server 模块）。"""
    spec = importlib.util.spec_from_file_location(
        "_rb_log_filters", NODE / "gateway" / "log_filters.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Collector(logging.Handler):
    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        logger.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _record(level: int, msg: str, name: str = MCP_LOGGER) -> logging.LogRecord:
    return logging.LogRecord(name=name, level=level, pathname=__file__, lineno=0,
                             msg=msg, args=(), exc_info=None)


def part_verdict(lf) -> None:
    print("[1] 过滤器判定")
    flt = lf.McpStatelessTerminateNoiseFilter()
    check("丢弃 INFO Terminating session: None",
          not flt.filter(_record(logging.INFO, "Terminating session: None")))
    check("保留 INFO Terminating session: <真实 id>",
          flt.filter(_record(logging.INFO, "Terminating session: 6f3a91c2e4b7")))
    check("保留同 logger 的其它 INFO",
          flt.filter(_record(logging.INFO, "Created new transport with session ID: ab12")))
    check("保留非 INFO 级别的同消息",
          flt.filter(_record(logging.WARNING, "Terminating session: None")))
    check("保留其它 logger 的同消息",
          flt.filter(_record(logging.INFO, "Terminating session: None",
                             name="mcp.server.streamable_http_manager")))


def part_real_logger(lf) -> None:
    print("[2] 走真实 logger")
    logger = logging.getLogger(MCP_LOGGER)
    saved_level, saved_prop = logger.level, logger.propagate
    logger.setLevel(logging.INFO)  # 不设就继承 root 的 WARNING，INFO 会被级别挡掉 → 假绿
    logger.propagate = False
    col = _Collector(logger)
    try:
        check("安装返回 True", lf.install_mcp_stateless_terminate_noise_filter() is True)
        logger.info("Terminating session: None")
        logger.info("Terminating session: 6f3a91c2e4b7")
        logger.info("Created new transport with session ID: ab12")
        msgs = [r.getMessage() for r in col.records]
        check("None 那条没落盘", "Terminating session: None" not in msgs, f"收到 {len(msgs)} 条")
        check("真 id 那条仍在", "Terminating session: 6f3a91c2e4b7" in msgs, f"收到 {len(msgs)} 条")
        check("其它 INFO 仍在", "Created new transport with session ID: ab12" in msgs)
    finally:
        logger.removeHandler(col)
        logger.setLevel(saved_level)
        logger.propagate = saved_prop


def part_idempotent_and_switch(lf) -> None:
    print("[3] 幂等 / 逃生开关")
    logger = logging.getLogger(MCP_LOGGER)
    lf.install_mcp_stateless_terminate_noise_filter()
    installed = [f for f in logger.filters if isinstance(f, lf.McpStatelessTerminateNoiseFilter)]
    check("重复安装不叠加", len(installed) == 1, f"{len(installed)} 个实例")

    os.environ["ROUNDABOUT_RAW_MCP_LOGS"] = "true"
    try:
        check("逃生开关下不安装", lf.install_mcp_stateless_terminate_noise_filter() is False)
    finally:
        os.environ.pop("ROUNDABOUT_RAW_MCP_LOGS", None)


def part_wiring() -> None:
    print("[4] 接线")
    src = (NODE / "__init__.py").read_text(encoding="utf-8")
    check("__init__.py 调了安装函数", "install_mcp_stateless_terminate_noise_filter()" in src)


def main() -> int:
    lf = load_log_filters()
    part_verdict(lf)
    part_real_logger(lf)
    part_idempotent_and_switch(lf)
    part_wiring()
    failed = results.count(False)
    print(f"\n{len(results) - failed} passed / {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
