"""MCP_STATELESS 开关测试。

背景：MCP streamable-http 的会话只存在后端进程内存里，ComfyUI 重启后 agent 拿着
旧 `Mcp-Session-Id` 再请求，SDK 按 MCP 规范回 404 `Session not found`（日志
「Rejected request with unknown or expired session ID」）。SDK 自带
`stateless_http=True`：每个请求独立处理、完全不跟踪会话，旧 session ID 无效与否
根本不进判断——agent 不再怕 ComfyUI 重启。本插件经 `MCP_STATELESS=true` 启用。

验证点：
  [1] 配置：默认 False；MCP_STATELESS=true 解析为 True（缺省容忍 1/true/yes）。
  [2] 行为差异（端到端，真实 uvicorn + 真实 SDK）：
      · 有状态（默认）：伪造 session ID 的 POST → 404（规范行为）；
      · 无状态：同样的伪造 session ID → 不再 404（session 头被整个忽略）。
  [3] 接线：serve_embedded 签名收 stateless；__init__.py 把 settings.mcp_stateless
      传进去；无状态时长任务完成通知不再打「sent」（没有推送通道，只留轮询提示）。

自测：python test_mcp_stateless.py
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = "ComfyUI_Roundabout"

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


def _load_package():
    """把插件目录按 ComfyUI 的加载方式挂进 sys.modules（包名带下划线便于 import）。"""
    if PKG in sys.modules:
        return sys.modules[PKG]
    import types

    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(HERE)]
    sys.modules[PKG] = pkg
    return pkg


# ---------------------------------------------------------------- [1] 配置
def part_config() -> None:
    print("\n[1] 配置解析（gateway/config.py）")
    import importlib

    import os

    cfg = importlib.import_module(PKG + ".gateway.config")

    saved = os.environ.pop("MCP_STATELESS", None)
    try:
        s = cfg.Settings()
        check("默认关闭（会话模式）", s.mcp_stateless is False, repr(s.mcp_stateless))
        for raw in ("true", "1", "yes", "TRUE"):
            os.environ["MCP_STATELESS"] = raw
            s = cfg.Settings()
            check(f"MCP_STATELESS={raw!r} -> True", s.mcp_stateless is True, repr(s.mcp_stateless))
        for raw in ("false", "0", "", "no"):
            os.environ["MCP_STATELESS"] = raw
            s = cfg.Settings()
            check(f"MCP_STATELESS={raw!r} -> False", s.mcp_stateless is False, repr(s.mcp_stateless))
    finally:
        if saved is None:
            os.environ.pop("MCP_STATELESS", None)
        else:
            os.environ["MCP_STATELESS"] = saved


# ------------------------------------------------------ [2] 行为差异（e2e）
def _boot_uvicorn(app, port: int = 0):
    """独立线程跑 uvicorn（与 test_mcp_startup_two_phase 同套路），返回 (server, port)。"""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    bound = server.servers[0].sockets[0].getsockname()[1]
    return server, bound


def _shutdown(server) -> None:
    server.should_exit = True
    time.sleep(0.3)


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "stateless-test", "version": "0"},
    },
}
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
BOGUS_SID = "deadbeef" * 4  # 伪造的旧 session ID：模拟「ComfyUI 重启后 agent 还拿着它」


def part_e2e() -> None:
    print("\n[2] 行为差异（真实 uvicorn + 真实 mcp SDK）")
    from mcp.server import MCPServer

    def _post(port: int, body: dict, extra_headers: dict | None = None):
        import httpx

        with httpx.Client(timeout=10) as client:
            return client.post(
                f"http://127.0.0.1:{port}/mcp",
                content=json.dumps(body),
                headers={**HEADERS, **(extra_headers or {})},
            )

    # —— 对照组：有状态（默认）——
    srv_stateful = MCPServer(name="t-stateful", version="0")
    app = srv_stateful.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")
    server, port = _boot_uvicorn(app)
    try:
        r = _post(port, INITIALIZE)
        sid = r.headers.get("mcp-session-id")
        check("有状态：initialize 成功", r.status_code == 200, f"status={r.status_code}")
        check("有状态：initialize 下发 Mcp-Session-Id", bool(sid), repr(dict(r.headers)))

        stale = _post(port, {"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"Mcp-Session-Id": BOGUS_SID})
        check(
            "有状态：伪造 session ID → 404（规范行为，即用户看到的「session 失效」）",
            stale.status_code == 404,
            f"status={stale.status_code} body={stale.text[:80]}",
        )
    finally:
        _shutdown(server)

    # —— 实验组：无状态 ——
    srv_stateless = MCPServer(name="t-stateless", version="0")
    app = srv_stateless.streamable_http_app(
        streamable_http_path="/mcp", host="127.0.0.1", stateless_http=True
    )
    server, port = _boot_uvicorn(app)
    try:
        r = _post(port, INITIALIZE)
        check("无状态：initialize 成功", r.status_code == 200, f"status={r.status_code}")

        stale = _post(port, {"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"Mcp-Session-Id": BOGUS_SID})
        check(
            "无状态：同一个伪造 session ID → 不再 404（重启无感）",
            stale.status_code != 404,
            f"status={stale.status_code} body={stale.text[:80]}",
        )

        # 无 session 头的裸请求也应被接受（无状态的本质：每个请求独立）
        bare = _post(port, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
        check("无状态：不带 session 头也能处理", bare.status_code == 200, f"status={bare.status_code}")
    finally:
        _shutdown(server)


# ------------------------------------------------------------- [3] 接线
def part_wiring() -> None:
    print("\n[3] 接线（serve_embedded / __init__.py / 通知降级）")
    import mcp_server  # noqa: PLC0415 - 已由上方 import 挂好包

    sig = inspect.signature(mcp_server.serve_embedded)
    check("serve_embedded 有 stateless 形参", "stateless" in sig.parameters, str(list(sig.parameters)))
    check("stateless 默认 False", sig.parameters["stateless"].default is False)

    init_src = (HERE / "__init__.py").read_text(encoding="utf-8")
    check("__init__.py 传入 settings.mcp_stateless", "stateless=settings.mcp_stateless" in init_src)

    src = (HERE / "mcp_server.py").read_text(encoding="utf-8")
    check(
        "无状态时长任务通知不再谎报「sent」",
        'stateless mode has no push' in src and "completion notification sent" in src,
    )
    check(
        "streamable_http_app 收到 stateless_http",
        "stateless_http=stateless" in src,
    )


def main() -> int:
    _load_package()
    part_config()
    part_e2e()
    part_wiring()

    print()
    total, ok = len(results), sum(1 for r in results if r[0])
    for good, name, detail in results:
        if not good:
            print(f"  [FAIL] {name}  {detail}")
    print(f"{ok}/{total} checks passed -> {'ALL PASS' if ok == total else 'FAILED'}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
