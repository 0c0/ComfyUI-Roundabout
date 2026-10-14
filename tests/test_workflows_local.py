"""工作流模板 / 实例双层解析（workflows.local/ 优先、模板回落）。

用临时目录构造最小 models.yaml + workflows/ + workflows.local/，锁五条规则：

  1. 实例层为空 → 走模板，wf_source=template（历史行为零变化）；
  2. 实例层有同名文件 → 实例生效，wf_source=local；
  3. wfdeploy.deploy 从模板落实例并热加载生效；已有实例且未 force → 拒绝；
  4. force=true 覆盖已有实例；
  5. weights.scan_workflow_refs 同名文件以实例层为准。

    python tests/test_workflows_local.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import wfdeploy, weights  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.registry import registry  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


WF_TEMPLATE = {
    "127": {"class_type": "UNETLoader", "inputs": {"unet_name": "template_weight.safetensors"}},
    "138": {"class_type": "CLIPTextEncode", "inputs": {"value": "placeholder"}},
    "92": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": "video/out"}},
}
WF_LOCAL = {
    "127": {"class_type": "UNETLoader", "inputs": {"unet_name": "local_weight.safetensors"}},
    "138": {"class_type": "CLIPTextEncode", "inputs": {"value": "placeholder"}},
    "92": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": "video/out"}},
}
MODELS_YAML = """\
defaults:
  vram_tiers: []
models:
  test-model:
    workflow: wf.json
    mode: image
    output_node: '92'
    bindings:
      prompt: 138.inputs.value
"""


def setup(base: Path) -> tuple[Path, Path]:
    wf_dir = base / "workflows"
    local_dir = base / "workflows.local"
    wf_dir.mkdir()
    (wf_dir / "wf.json").write_text(json.dumps(WF_TEMPLATE), encoding="utf-8")
    (base / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    return wf_dir, local_dir


def main() -> int:
    old = (settings.workflows_dir, settings.workflows_local_dir, settings.models_file, settings.default_model)
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wf_dir, local_dir = setup(base)
            # settings 是 frozen dataclass：测试内用 object.__setattr__ 打补丁，finally 里还原
            object.__setattr__(settings, "workflows_dir", wf_dir)
            object.__setattr__(settings, "workflows_local_dir", local_dir)
            object.__setattr__(settings, "models_file", base / "models.yaml")
            object.__setattr__(settings, "default_model", "")

            # ---- 1. 实例层为空 → 模板回落 ----
            registry.load(settings.models_file, wf_dir, "", local_dir)
            spec = registry.resolve("test-model")
            check("无实例时走模板", spec.wf_source == "template", spec.wf_source)
            check("无实例时加载模板内容",
                  spec.template["127"]["inputs"]["unet_name"] == "template_weight.safetensors")

            # ---- 2. 实例层命中 → 实例优先 ----
            local_dir.mkdir()
            (local_dir / "wf.json").write_text(json.dumps(WF_LOCAL), encoding="utf-8")
            registry.load(settings.models_file, wf_dir, "", local_dir)
            spec = registry.resolve("test-model")
            check("有实例时走实例", spec.wf_source == "local", spec.wf_source)
            check("实例内容生效",
                  spec.template["127"]["inputs"]["unet_name"] == "local_weight.safetensors")

            # ---- 2b. load_from_settings：启动路径不得漏实例层 ----
            # 回归护栏（2026-10-14 反馈）：__init__.py / mcp_server 曾手写 3 参漏传
            # workflows_local_dir，重启后全部静默落模板。生产调用点已收敛到
            # load_from_settings()，此用例锁「它必须与显式 4 参 load 同效」。
            registry.load_from_settings()
            spec = registry.resolve("test-model")
            check("load_from_settings 实例优先", spec.wf_source == "local", spec.wf_source)
            check("load_from_settings 实例内容生效",
                  spec.template["127"]["inputs"]["unet_name"] == "local_weight.safetensors")

            # ---- 3. deploy：已有实例未 force → 拒绝 ----
            try:
                wfdeploy.deploy("wf.json")
                check("已有实例未 force 拒绝", False, "no exception")
            except Exception as exc:
                check("已有实例未 force 拒绝", "409" in str(exc) or "already exists" in str(exc), str(exc))

            # ---- 4. force=true 覆盖，且热加载后仍指向实例 ----
            # 先改实例内容，force 重部署应还原为模板内容
            (local_dir / "wf.json").write_text(json.dumps(WF_LOCAL), encoding="utf-8")
            result = wfdeploy.deploy("wf.json", force=True)
            check("force 覆盖部署成功", result["deployed"] == "wf.json", str(result))
            spec = registry.resolve("test-model")
            check("重部署后热加载生效且内容=模板",
                  spec.wf_source == "local"
                  and spec.template["127"]["inputs"]["unet_name"] == "template_weight.safetensors",
                  f"{spec.wf_source} {spec.template['127']['inputs']['unet_name']}")

            # ---- 5. snapshot 的同步状态 ----
            snap = {e["model"]: e for e in wfdeploy.snapshot()}["test-model"]
            check("重部署后 synced=true（内容=模板）", snap["synced"] is True, str(snap))
            (local_dir / "wf.json").write_text(json.dumps(WF_LOCAL), encoding="utf-8")
            registry.load(settings.models_file, wf_dir, "", local_dir)
            snap = {e["model"]: e for e in wfdeploy.snapshot()}["test-model"]
            check("实例被改后 synced=false", snap["synced"] is False, str(snap))

            # ---- 6. weights 扫描：同名以实例为准 ----
            refs = weights.scan_workflow_refs(wf_dir, local_dir)
            check("权重扫描跟随实例层", refs.get("local_weight.safetensors") == ["wf"], str(refs))

            # ---- 7. 路径穿越拒绝 ----
            try:
                wfdeploy.deploy("../models.yaml")
                check("路径穿越拒绝", False, "no exception")
            except Exception as exc:
                check("路径穿越拒绝", "invalid workflow name" in str(exc), str(exc))
    finally:
        object.__setattr__(settings, "workflows_dir", old[0])
        object.__setattr__(settings, "workflows_local_dir", old[1])
        object.__setattr__(settings, "models_file", old[2])
        object.__setattr__(settings, "default_model", old[3])
        # 还原全局 registry（其他测试依赖真实配置）
        registry.load(settings.models_file, settings.workflows_dir, settings.default_model, settings.workflows_local_dir)

    # ---- hints：跨卡算力命名提醒（v1.42.1）----
    from gateway import wfdeploy as _wfd

    check("ARCH 正则命中 SM86", _wfd._ARCH_RE.findall("MiniMaxH3SparkAttentionSM86") == ["SM86"], "")
    check("ARCH 正则不咬长串", _wfd._ARCH_RE.findall("SM862 SM89") == ["SM89"], "")
    _old_arch = _wfd._local_arch
    try:
        _wfd._local_arch = lambda: "89"
        hints = _wfd.arch_hints("class_type: XxxSM86")
        check("不匹配 → 出提醒且含目标后缀", bool(hints) and "SM89" in hints[0], str(hints)[:120])
        _wfd._local_arch = lambda: "86"
        check("匹配 → 无提醒", _wfd.arch_hints("class_type: XxxSM86") == [], "")
        check("无算力命名 → 无提醒", _wfd.arch_hints("no arch here") == [], "")
    finally:
        _wfd._local_arch = _old_arch

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
