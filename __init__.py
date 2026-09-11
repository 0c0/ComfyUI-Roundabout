"""ComfyUI custom node: OpenAI-compatible image/video gateway.

把 roundabout2 的业务（OpenAI Images / Videos API ↔ ComfyUI 工作流）搬进
ComfyUI 自身：作为 custom node 在 PromptServer 上注册 /v1/* 路由，让 ComfyUI
的 HTTP 网关直接监听 OpenAI 风格生成接口（Hermes Agent 等可直接对接）。

启动方式：正常启动 ComfyUI 即可（--listen 监听地址即网关地址）。
  curl http://<host>:<port>/health
  curl -X POST http://<host>:<port>/v1/images/generations -H "Content-Type: application/json" \
       -d '{"model":"sdxl","prompt":"a red fox in snow","size":"1024x1024"}'
"""

import logging

from server import PromptServer

from .gateway.config import settings
from .gateway.registry import registry
from .gateway.routes import register_routes

log = logging.getLogger("roundabout")


class _RoundaboutLogTag(logging.Filter):
    """给 roundabout.* 日志统一加 [Roundabout] 前缀，便于在 ComfyUI 日志中一眼区分/过滤。

    ComfyUI 的日志格式不含 logger 名（控制台只打 %(message)s），网关日志与 ComfyUI
    自身日志混在一起难以排查；部署机上 `grep Roundabout` 即可过滤出全部网关日志。

    挂到 root logger 的 handlers 上（而非 root logger 本身）——Logger.handle 只检查
    产生记录的 logger 自己的 filters，而所有记录最终都会经过 root 的 handlers，
    在 handler 上过滤才能覆盖 roundabout.* 全部子 logger；按 record.name 判断，
    不影响其他模块的日志。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("roundabout"):
            msg = record.msg
            if isinstance(msg, str) and not msg.startswith("[Roundabout]"):
                record.msg = "[Roundabout] " + msg
        return True


def _install_log_tag() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not any(isinstance(f, _RoundaboutLogTag) for f in handler.filters):
            handler.addFilter(_RoundaboutLogTag())


_install_log_tag()

# 前端扩展目录：web/ 下的 .js 会被 ComfyUI 以 /extensions/ComfyUI-Roundabout/ 路径
# 静态托管，并自动加载（见 server.py 的 get_extensions 路由）。
WEB_DIRECTORY = "web"

# ------------------------------------------------------------------ 加载模型注册表
# 失败不阻断 ComfyUI 启动：把错误打日志，路由仍会注册（请求时返回对应错误）。
try:
    registry.load(
        settings.models_file,
        settings.workflows_dir,
        settings.default_model,
    )
except Exception as exc:  # noqa: BLE001
    log.error(
        "OpenAI gateway: failed to load models from %s / %s: %s",
        settings.models_file,
        settings.workflows_dir,
        exc,
    )

# ------------------------------------------------------------------ 注册路由
# 路由注册逻辑集中在 gateway/routes.py（属于 gateway/ 包），本文件只负责调用。
# 这样路由的增删改不再需要改动根 __init__.py，部署时只同步 gateway/ 目录即可。
try:
    register_routes(PromptServer.instance.app)
except Exception as exc:  # noqa: BLE001
    log.error("OpenAI gateway: fatal during route registration: %s", exc)


# ------------------------------------------------------------------ MCP 网关（嵌入模式）
# 若启用（MCP_ENABLED=true），在 ComfyUI 事件循环中后台拉起 MCP streamable-http
# server，与 REST 网关同进程共享 task_store/registry。默认（MCP_SHARE_PORT=true）
# 通过 aiohttp 原生代理把 /mcp 暴露在 ComfyUI 同一端口上，内部 uvicorn 后端只绑
# 127.0.0.1 回环；MCP_SHARE_PORT=false 时客户端直连 http://<host>:8189/mcp。
def _maybe_start_embedded_mcp() -> None:
    if not settings.mcp_enabled:
        return
    import os  # noqa: PLC0415 - 下面 except 分支也要用，故提到 try 外
    import sys  # noqa: PLC0415
    try:
        import asyncio

        os.environ["ROUNDABOUT_MCP_EMBEDDED"] = "1"
        from . import mcp_server

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not loop.is_running():
            log.warning("MCP gateway: no running event loop yet; skip embedded start (MCP_ENABLED is on)")
            return

        # 内部后端：共享端口模式只绑回环（外部入口是 ComfyUI 端口的代理），
        # 独立端口模式按 MCP_HOST 配置绑定。
        backend_host = "127.0.0.1" if settings.mcp_share_port else settings.mcp_host
        loop.create_task(
            mcp_server.serve_embedded(
                host=backend_host, port=settings.mcp_port, path=settings.mcp_path
            )
        )
        if settings.mcp_share_port:
            backend_url = f"http://127.0.0.1:{settings.mcp_port}"
            mcp_server.register_share_port_proxy(PromptServer.instance.app, backend_url, settings.mcp_path)
            log.info(
                "MCP gateway: embedded streamable-http shared on ComfyUI port (internal backend %s%s)",
                backend_url, settings.mcp_path,
            )
        else:
            log.info(
                "MCP gateway: starting embedded streamable-http on http://%s:%s%s",
                settings.mcp_host, settings.mcp_port, settings.mcp_path,
            )
    except ModuleNotFoundError as exc:
        # 最常见失败：ComfyUI 的 python 没装 mcp/uvicorn（ComfyUI 自带依赖不含它们）。
        # 给出可直接照抄的修复命令，避免再去翻文档。
        if exc.name in {"mcp", "uvicorn", "starlette", "httpx", "sse_starlette", "httpx2"}:
            req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
            msg = (
                "MCP gateway: missing dependency '%s' in the Python that runs ComfyUI (%s). "
                "Fix with: \"%s\" -m pip install -r \"%s\"   (or run install.py from this node folder)."
            )
            args = (exc.name, sys.executable, sys.executable, req)
            if os.getenv("MCP_ENABLED"):
                # 用户显式要求启用 MCP 却没装依赖 —— 这是真实错误，报 ERROR。
                log.error(msg, *args)
            else:
                # MCP 现为默认启用，但依赖本身是可选的：没装就当没开，降为 WARNING
                # 并给出两条出路，免得只想用 REST 的用户每次启动都看到 ERROR。
                log.warning(
                    msg + "  (MCP is on by default; set MCP_ENABLED=false to skip it — "
                    "the REST gateway works without these packages)",
                    *args,
                )
        else:
            log.error("MCP gateway: failed to start embedded server: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.error("MCP gateway: failed to start embedded server: %s", exc)


_maybe_start_embedded_mcp()

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
