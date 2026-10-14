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

# 按算力命名的 Spark 节点家族后缀（comfyui_nodes.py：TARGET_CAPABILITY 按 arch 拆类）。
# 匹配「SM+数字且后面不跟数字」，避免咬进更长的串。
_ARCH_RE = re.compile(r"SM(?:1[02]\d|89|86|75)(?![0-9])")
_CAP_SUFFIX = lambda cap: str(cap[0] * 10 + cap[1])  # (8,6)->"86" (12,0)->"120"


def _local_arch() -> str | None:
    """本机 GPU 的算力后缀（如 "86"），探测失败返回 None。"""
    try:
        import torch

        if torch.cuda.is_available():
            return _CAP_SUFFIX(torch.cuda.get_device_capability(0))
    except Exception:  # noqa: BLE001 — torch 缺失/无卡都不该炸清单
        pass
    return None


def arch_hints(text: str) -> list[str]:
    """模板文本里的算力命名 vs 本机 GPU：不匹配时给部署提醒。

    Spark 家族节点按 arch 拆类且运行时硬校验 capability（不匹配直接报错），
    所以跨卡部署必须换后缀 —— 这里把提醒做进 list/deploy 的返回里。
    """
    found = sorted(set(_ARCH_RE.findall(text)))
    if not found:
        return []
    local = _local_arch()
    if local is None:
        return [
            "模板含按算力命名的节点（%s），无法探测本机 GPU 架构 —— 跨卡部署时用 replace 替换后缀"
            % ", ".join(found)
        ]
    if f"SM{local}" in found:
        return []
    return [
        "模板节点按 %s 编译，本机 GPU 是 SM%s —— 部署时用 replace 把后缀换成 SM%s，"
        "否则节点运行时 capability 校验会直接报错" % (", ".join(found), local, local)
    ]


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
        try:
            hints = arch_hints(template_path.read_text(encoding="utf-8"))
        except OSError:
            hints = []
        if hints:
            entry["hints"] = hints
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
    registry.load_from_settings()
    try:
        hints = arch_hints(template_path.read_text(encoding="utf-8"))
    except OSError:
        hints = []
    resp: dict[str, Any] = {
        "deployed": target.name,
        "template": str(template_path),
        "instance": str(target),
        "reloaded": True,
        "note": "实例层不被仓库更新覆盖；模板更新后可在 GET /admin/workflows 里看 synced=false 感知",
    }
    if hints:
        resp["hints"] = hints
    return resp
