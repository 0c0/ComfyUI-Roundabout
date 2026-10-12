"""工作流模板 / 实例双层管理（REST / MCP 共用逻辑）。

分层契约：
- 模板层 `workflows/`：仓库跟踪，上游更新会覆盖 —— 只放「从 ComfyUI 导出的原始
  API JSON」，不做本机定制；
- 实例层 `workflows.local/`：gitignored，部署端从模板部署副本后自由改造（换节点、
  调固定参数），永不被仓库更新触碰。

registry 按「实例优先、模板回落」解析（见 registry.resolve_workflow_path）；
本模块提供实例清单（来源 + hash 同步状态）与部署动作，供 admin 端点与
MCP `manage_workflows` 工具共用。
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from .config import settings
from .errors import APIError
from .registry import registry

log = logging.getLogger("roundabout.wfdeploy")

_NAME_RE = re.compile(r"^[\w.\-]+$")  # 文件名白名单：拒绝路径分隔符与目录穿越


def _sha256_file(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot() -> list[dict[str, Any]]:
    """每档工作流的来源与同步状态（registry 当前加载视角）。"""
    out: list[dict[str, Any]] = []
    for spec in registry.all():
        name = spec.workflow_path.name
        template_path = settings.workflows_dir / name
        instance_path = settings.workflows_local_dir / name
        entry: dict[str, Any] = {
            "model": spec.name,
            "workflow": name,
            "source": spec.wf_source,
            "template_hash": _sha256_file(template_path),
            "instance_path": str(instance_path) if instance_path.exists() else None,
        }
        if spec.wf_source == "local":
            entry["instance_hash"] = _sha256_file(instance_path)
        if entry["template_hash"] is not None:
            inst = entry.get("instance_hash")
            if inst is None and instance_path.exists():
                inst = _sha256_file(instance_path)
            # 未部署 = 跟随模板，永远同步；已部署 = 内容一致才算同步
            entry["synced"] = None if inst is None else (inst == entry["template_hash"])
        else:
            entry["synced"] = None
        out.append(entry)
    return out


def deploy(name: str, as_name: str = "", force: bool = False) -> dict[str, Any]:
    """从模板部署一份实例到实例层，并热加载 registry 使其立即生效。

    - `name`：模板文件名（如 `video_minimax_h3.json`）；
    - `as_name`：可选，实例另存名（默认与模板同名，同名 = registry 直接生效）；
    - `force`：目标实例已存在时是否覆盖（默认拒绝，防误毁本机定制）。
    """
    if not name or not _NAME_RE.match(name):
        raise APIError(f"invalid workflow name: {name!r}", status_code=400, param="workflow")
    if as_name and not _NAME_RE.match(as_name):
        raise APIError(f"invalid as_name: {as_name!r}", status_code=400, param="as_name")

    template_path = settings.workflows_dir / name
    if not template_path.is_file():
        raise APIError(
            f"template not found: {template_path}", status_code=404, param="workflow"
        )

    target = settings.workflows_local_dir / (as_name or name)
    if target.exists() and not force:
        raise APIError(
            f"instance already exists: {target} (pass force=true to overwrite)",
            status_code=409,
            param="as_name",
        )

    settings.workflows_local_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, target)
    log.info("workflow deployed: %s -> %s", template_path, target)

    # 热加载：若实例名与某档的 `workflow:` 声明一致，本次部署立即生效
    registry.load(
        settings.models_file,
        settings.workflows_dir,
        settings.default_model,
        settings.workflows_local_dir,
    )
    return {
        "deployed": target.name,
        "template": str(template_path),
        "instance": str(target),
        "reloaded": True,
        "note": "实例层不被仓库更新覆盖；模板更新后可在 GET /admin/workflows 里看 synced=false 感知",
    }
