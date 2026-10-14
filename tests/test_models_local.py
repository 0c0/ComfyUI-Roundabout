"""本机模型登记扩展层 models.local.yaml（gitignored，模式同 workflows.local/）。

用临时目录构造最小 models.yaml + workflows/ + models.local.yaml，锁七条规则：

  1. local 文件不存在 → 行为与历史版本完全一致；
  2. local 新模型名 → 追加注册（本机专属模型不被上游更新覆盖）；
  3. local 同名模型 → 条目浅合并：只覆盖 local 写的键，其余继承 git 版；
  4. 覆盖后 defaults 优先级仍成立（条目 defaults > params 声明 default）；
  5. 顶层键（如 default_model）被 local 覆盖；
  6. local 文件顶层非 mapping / models 非 mapping → 启动报错；
  7. .gitignore 含 models.local.yaml（防误提交）。

    python tests/test_models_local.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.registry import Registry  # noqa: E402

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
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "placeholder", "steps": 20, "strength": 0.0}},
    "92": {"class_type": "SaveImage", "inputs": {"filename_prefix": "img/out"}},
}
MODELS_YAML = """\
defaults:
  vram_tiers: []
models:
  base-model:
    workflow: wf.json
    mode: image
    output_node: '92'
    bindings:
      prompt: 6.inputs.text
      steps: 6.inputs.steps
    defaults:
      steps: 20
    description: repo base entry
"""


def setup(base: Path, local_yaml: str | None = None) -> Path:
    wf_dir = base / "workflows"
    wf_dir.mkdir()
    (wf_dir / "wf.json").write_text(__import__("json").dumps(WF), encoding="utf-8")
    (base / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    if local_yaml is not None:
        (base / "models.local.yaml").write_text(local_yaml, encoding="utf-8")
    return wf_dir


def load(base: Path, wf_dir: Path) -> Registry:
    reg = Registry()
    reg.load(base / "models.yaml", wf_dir, workflows_local_dir=base / "workflows.local")
    return reg


print("== 1. local 不存在 → 行为不变")
with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    wf_dir = setup(base)
    reg = load(base, wf_dir)
    check("只有 base-model", reg.names() == ["base-model"], reg.names())
    check("defaults 生效", reg.resolve("base-model").defaults["steps"] == 20)

print("== 2. local 新模型 → 追加")
with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    wf_dir = setup(base, """\
models:
  my-local-model:
    workflow: wf.json
    mode: image
    output_node: '92'
    bindings:
      prompt: 6.inputs.text
    description: "[本机] trial"
""")
    reg = load(base, wf_dir)
    check("两个模型都在", set(reg.names()) == {"base-model", "my-local-model"}, reg.names())
    spec = reg.resolve("my-local-model")
    check("local 模型 description", spec.description == "[本机] trial", spec.description)
    check("local 模型 wf_source=template（同 wf.json）", spec.wf_source == "template")

print("== 3. 同名条目浅合并（只覆盖写的键）")
with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    wf_dir = setup(base, """\
models:
  base-model:
    defaults:
      steps: 8
    timeout: 120
""")
    reg = load(base, wf_dir)
    spec = reg.resolve("base-model")
    check("覆盖的 defaults.steps", spec.defaults["steps"] == 8, spec.defaults.get("steps"))
    check("未写的 description 继承 git 版", spec.description == "repo base entry", spec.description)
    check("未写的 bindings 继承", "steps" in spec.bindings)
    check("timeout 生效", spec.timeout == 120)

print("== 4. 条目 defaults 仍覆盖 params 声明")
with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    wf_dir = setup(base, """\
models:
  base-model:
    params:
      turbo_lora:
        type: float
        min: 0
        max: 1.5
        default: 0
        bind: 6.inputs.strength
""")
    reg = load(base, wf_dir)
    spec = reg.resolve("base-model")
    check("local 声明并入 param_specs", "turbo_lora" in spec.param_specs)
    check("声明 default 并入 defaults", spec.defaults.get("turbo_lora") == 0)

print("== 5. 顶层键覆盖")
with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    wf_dir = setup(base, "default_model: base-model\n")
    reg = load(base, wf_dir)
    check("default_model 被 local 覆盖", reg.default_model == "base-model", reg.default_model)

print("== 6. 非法 local 文件 → 报错")
for label, content in [
    ("顶层非 mapping", "- just\n- a list\n"),
    ("models 非 mapping", "models:\n  - a\n  - b\n"),
]:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        wf_dir = setup(base, content)
        try:
            load(base, wf_dir)
            check(f"{label} → 报错", False, "no exception")
        except RuntimeError as e:
            check(f"{label} → 报错", "models.local.yaml" in str(e), str(e))

print("== 7. .gitignore 防误提交")
gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("gitignore 含 models.local.yaml", "models.local.yaml" in gi)
check("example 模板存在", (ROOT / "models.local.yaml.example").exists())

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
