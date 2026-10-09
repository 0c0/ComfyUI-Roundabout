"""device_weights.yaml 机器本地权重覆盖的装载逻辑。

用临时目录构造最小 models.yaml + workflow + 覆盖表，锁三条规则：

  1. 有 device_weights.yaml → 对应节点 inputs 被替换，启动日志回显；
  2. 覆盖声明的节点 id 在工作流里不存在 → 装载直接抛错（不静默跳过）；
  3. 没有该文件 → 行为与历史版本完全一致（零覆盖）。

    python tests/test_device_weights.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.registry import registry, build_workflow  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


WF = {
    "127": {"class_type": "UNETLoader", "inputs": {"unet_name": "default_weight.safetensors"}},
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


def setup(dirpath: Path, device_yaml: str | None) -> None:
    (dirpath / "wf.json").write_text(json.dumps(WF), encoding="utf-8")
    (dirpath / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    if device_yaml is not None:
        (dirpath / "device_weights.yaml").write_text(device_yaml, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)

        # ---- 1. 有覆盖 → inputs 被替换 ----
        d1 = base / "on"; d1.mkdir()
        setup(d1, """\
overrides:
  - workflow: wf.json
    node: "127"
    set:
      unet_name: full_bf16.safetensors
""")
        registry.load(d1 / "models.yaml", d1, "")
        spec = registry.resolve("test-model")
        wf = build_workflow(spec, {"prompt": "t"}, None)
        check("覆盖后 unet_name 被替换", wf["127"]["inputs"]["unet_name"] == "full_bf16.safetensors")

        # ---- 2. 节点不存在 → 装载抛错 ----
        d2 = base / "badnode"; d2.mkdir()
        setup(d2, """\
overrides:
  - workflow: wf.json
    node: "999"
    set:
      unet_name: x.safetensors
""")
        try:
            registry.load(d2 / "models.yaml", d2, "")
            check("节点不存在时装载报错", False, "no exception")
        except RuntimeError as exc:
            check("节点不存在时装载报错", "999" in str(exc), str(exc))

        # ---- 3. 非法 set → 装载抛错 ----
        d3 = base / "badset"; d3.mkdir()
        setup(d3, """\
overrides:
  - workflow: wf.json
    node: "127"
    set: {}
""")
        try:
            registry.load(d3 / "models.yaml", d3, "")
            check("空 set 时装载报错", False, "no exception")
        except RuntimeError as exc:
            check("空 set 时装载报错", "set" in str(exc), str(exc))

        # ---- 4. 无文件 → 零覆盖 ----
        d4 = base / "clean"; d4.mkdir()
        setup(d4, None)
        registry.load(d4 / "models.yaml", d4, "")
        spec = registry.resolve("test-model")
        wf = build_workflow(spec, {"prompt": "t"}, None)
        check("无覆盖文件时保持默认", wf["127"]["inputs"]["unet_name"] == "default_weight.safetensors")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
