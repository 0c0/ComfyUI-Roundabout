"""路由注册（集中管理，便于部署只同步 gateway/ 包）。

所有 /v1/* 与 /roundabout/* 路由的注册逻辑都放在本文件（属于 gateway/ 包）。
节点根 ComfyUI-Roundabout/__init__.py 只需调用 register_routes(app) 即可。

这样路由的增删改全部发生在 gateway/ 内，部署时只要同步 gateway/ 目录即可，
根 __init__.py 不再随路由变化而改动，避免「漏部署根文件导致轮询端点 404」这类问题。
"""

from __future__ import annotations

import logging

from aiohttp import web

from . import admin, board, handlers, viewer
from .config import settings
from .registry import registry

log = logging.getLogger("roundabout")


def register_routes(app) -> None:
    """在 ComfyUI PromptServer 的 aiohttp app 上注册全部网关路由。

    逐条注册 + 幂等容错：避免「add_routes 中途抛 RouteConflictError 导致后续路由
    全部漏注册」的情况（custom node 被重复加载、或与其他扩展路径冲突）。每条路由
    独立 try，冲突则跳过并告警；其他异常打 error。任意单条失败都不连累其它路由，
    且启动日志可精确定位。
    """
    route_specs = [
        ("POST", "/v1/images/generations", handlers.images_generations),
        ("POST", "/v1/images/edits", handlers.images_edits),
        # 独立去背景端点：无 prompt，模型固定 utility-birefnet-remove-background
        ("POST", "/v1/images/remove-background", handlers.images_remove_background),
        ("POST", "/v1/videos/generations", handlers.videos_generations),
        ("GET", "/v1/videos/tasks/{id}", handlers.video_task),
        # OpenAI 标准异步命名空间：GET /v1/images/tasks/{id} 同样可查（复用 video_task）
        ("GET", "/v1/images/tasks/{id}", handlers.video_task),
        # 取消异步任务（DELETE 语义；两个命名空间等价）
        ("DELETE", "/v1/videos/tasks/{id}", handlers.cancel_video_task),
        ("DELETE", "/v1/images/tasks/{id}", handlers.cancel_video_task),
        ("GET", "/v1/models", handlers.list_models),
        ("GET", "/v1/models/{model_id}", handlers.get_model),
        ("GET", "/v1/images/files/{name}", handlers.get_file),
        ("GET", "/v1/videos/files/{name}", handlers.get_video_file),
        ("GET", "/health", handlers.health),
        ("POST", "/admin/reload", handlers.reload_registry),
        # ---- 管理端点：工作流管理 + 模型配置（自定义设置菜单的后端）----
        ("GET", "/roundabout/admin/state", admin.admin_state),
        ("GET", "/roundabout/admin/queue", admin.queue_status),
        ("GET", "/roundabout/admin/queue/workflow/{prompt_id}", admin.queue_workflow),
        ("GET", "/roundabout/admin/weights", admin.weights_status),
        ("GET", "/roundabout/admin/workflows", admin.list_workflows),
        ("POST", "/roundabout/admin/workflows/upload", admin.upload_workflow),
        ("DELETE", "/roundabout/admin/workflows/{name}", admin.delete_workflow),
        ("GET", "/roundabout/admin/models", admin.get_models_config),
        ("PUT", "/roundabout/admin/models", admin.put_models_config),
        ("GET", "/roundabout/admin/models/structured", admin.get_models_structured),
        ("PUT", "/roundabout/admin/models/structured", admin.put_models_structured),
        ("DELETE", "/roundabout/admin/models/{name}", admin.delete_model),
        # ---- 调用结构自描述：模型 × 字段生效性 + 全局限制（前端渲染表单 / agent 选型）----
        ("GET", "/roundabout/admin/tool-info", admin.get_tool_info),
        # ---- 可视化页面：浏览器直接浏览 input/output 资源 + 任务进度（MCP get_view_url 给出地址）----
        ("GET", "/roundabout/view", viewer.view_page),
        ("GET", "/roundabout/view/files", viewer.list_dir),
        ("GET", "/roundabout/view/tasks", viewer.tasks),
        # ---- 任务看板：agent 把产出钉到画布上，换任务时自己清空（清空即归档，可回看）----
        ("GET", "/roundabout/view/board", board.board),
        ("POST", "/roundabout/view/board/items", board.pin_item),
        ("DELETE", "/roundabout/view/board/items/{id}", board.remove_item),
        ("DELETE", "/roundabout/view/board", board.clear_board),
        ("GET", "/roundabout/view/board/history", board.history),
        ("GET", "/roundabout/view/board/history/{id}", board.history_detail),
        ("POST", "/roundabout/view/board/history/{id}/load", board.history_load),
    ]
    registered = 0
    for method, path, h in route_specs:
        try:
            app.router.add_route(method, path, h)
            registered += 1
        except web.RouteConflictError:
            # 已注册（重复加载节点等情况）：幂等跳过，不报错
            log.warning("roundabout route already exists, skipped: %s %s", method, path)
        except Exception as exc:  # noqa: BLE001
            log.error("roundabout failed to register route %s %s: %s", method, path, exc)
    log.info(
        "OpenAI gateway routes registered on PromptServer: %d/%d (comfy=%s, models=%s, auth=%s)",
        registered,
        len(route_specs),
        settings.comfy_base_url,
        ",".join(registry.names()) or "<none>",
        "on" if settings.auth_enabled else "OFF",
    )
