# -*- coding: utf-8 -*-
"""模型自述参数（models.yaml `params:` 块，v1.43.0）单元测试。

覆盖：声明解析 / bind 并入 bindings（白名单豁免）/ default 并入 defaults /
范围与类型校验 / pipeline 拒绝逻辑的判据（MODEL_SPECIFIC_FIELDS）/
tool-info 字段元数据来自声明。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import toolinfo
from gateway.errors import APIError
from gateway.registry import KNOWN_PARAMS, ModelSpec, ParamSpec, Registry, _parse_param_specs

passed = failed = 0


def check(name: str, cond, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


def make_spec(**param_overrides) -> ModelSpec:
    """构造带声明参数的最小 ModelSpec（不落盘、不依赖 ComfyUI）。"""
    params = {
        "my_custom_param": {  # 故意不在 KNOWN_PARAMS：验证声明即免白名单
            "type": "float", "min": 0, "max": 2, "default": 0.5,
            "bind": "9.inputs.strength", "description": "自定义参数",
        },
        **param_overrides,
    }
    specs, binds, defs = _parse_param_specs("test-model", params)
    return ModelSpec(
        name="test-model",
        workflow_path=Path("x.json"),
        template={"9": {"class_type": "X", "inputs": {"strength": 0.5}}},
        bindings={"steps": ["1.inputs.steps"], **binds},
        param_specs=specs,
        defaults={"steps": 20, **defs},
        capabilities={"text-to-image"},
    )


def main() -> int:
    print("== ParamSpec 解析 ==")
    specs, binds, defs = _parse_param_specs("m", {
        "alpha": {"type": "int", "min": 1, "max": 60, "default": 20, "bind": ["1.inputs.a", "2.inputs.b"]},
        "beta": {"type": "str", "choices": ["x", "y"], "default": "x"},
        "gamma": {},  # 全默认：float / 无范围
    })
    check("三个声明全部解析", set(specs) == {"alpha", "beta", "gamma"})
    check("alpha.bind 列表并入 bindings", binds["alpha"] == ["1.inputs.a", "2.inputs.b"])
    check("default 并入 defaults（beta/gamma）", defs == {"beta": "x", "gamma": None} or defs.get("beta") == "x")
    check("gamma 类型回落 float", specs["gamma"].type == "float")

    print("== 非法声明启动即报 ==")
    for bad, tag in [
        ({"p": {"type": "matrix"}}, "非法 type"),
        ({"p": {"choices": []}}, "空 choices"),
        ({"p": {"min": 5, "max": 1}}, "min>max"),
    ]:
        try:
            _parse_param_specs("m", bad)
            check(f"{tag} 被拒", False)
        except RuntimeError:
            check(f"{tag} 被拒", True)

    print("== ParamSpec.validate ==")
    ps = ParamSpec(name="v", type="float", min=0, max=1.5)
    check("边界 1.5 通过", ps.validate(1.5) == 1.5)
    check("字符串数字收敛 float", ps.validate("0.5") == 0.5)
    for bad in (1.6, -0.1, "abc"):
        try:
            ps.validate(bad)
            check(f"非法值 {bad!r} 被拒", False)
        except APIError:
            check(f"非法值 {bad!r} 被拒", True)
    pc = ParamSpec(name="c", type="str", choices=["a", "b"])
    try:
        pc.validate("z")
        check("choices 外被拒", False)
    except APIError:
        check("choices 外被拒", True)
    pi = ParamSpec(name="i", type="int")
    check("int 收敛", pi.validate("7") == 7)

    print("== 白名单豁免 + defaults 合并（真实 registry.load）==")
    spec = make_spec()
    check("声明 key 不在 KNOWN_PARAMS 也注册成功", "my_custom_param" in spec.param_specs
          and "my_custom_param" not in KNOWN_PARAMS)
    check("声明 bind 并入 bindings", spec.binds("my_custom_param"))
    check("声明 default 并入 defaults", spec.defaults.get("my_custom_param") == 0.5)

    print("== 真实 models.yaml 四档迁移 ==")
    from gateway.config import settings
    r = Registry()
    r.load(settings.models_file, settings.workflows_dir, settings.default_model, settings.workflows_local_dir)
    h3 = r.resolve("minimax-h3")
    edit = r.resolve("minimax-h3-edit")
    lift = r.resolve("minimax-h3-lift")
    lift_edit = r.resolve("minimax-h3-lift-edit")
    check("h3/h3-edit 声明 turbo+teacache",
          set(h3.param_specs) == {"turbo_lora", "teacache_threshold"}
          and set(edit.param_specs) == {"turbo_lora", "teacache_threshold"})
    check("lift 两支只声明 turbo",
          set(lift.param_specs) == {"turbo_lora"} and set(lift_edit.param_specs) == {"turbo_lora"})
    check("h3 bindings 由声明并入", h3.binds("turbo_lora") and h3.binds("teacache_threshold"))
    check("h3 defaults 取自声明（0=关）",
          h3.defaults.get("turbo_lora") == 0 and h3.defaults.get("teacache_threshold") == 0)
    check("teacache max=0.15（0.22 硬拦）", h3.param_specs["teacache_threshold"].max == 0.15)

    print("== pipeline 拒绝判据 ==")
    from gateway.schemas import MODEL_SPECIFIC_FIELDS
    check("MODEL_SPECIFIC_FIELDS 覆盖两参数", set(MODEL_SPECIFIC_FIELDS) == {"turbo_lora", "teacache_threshold"})
    # lift 传 teacache：无声明无绑定 → 拒绝路径成立
    check("lift 对 teacache 无声明无绑定",
          "teacache_threshold" not in lift.param_specs and not lift.binds("teacache_threshold"))

    print("== tool-info 元数据来自声明 ==")
    entry = toolinfo._model_entry(h3)
    f_teacache = next(f for f in entry["fields"] if f["name"] == "teacache_threshold")
    f_turbo = next(f for f in entry["fields"] if f["name"] == "turbo_lora")
    check("teacache applies=true", f_teacache["applies"] is True)
    check("teacache min/max 来自声明", f_teacache.get("min") == 0 and f_teacache.get("max") == 0.15)
    check("teacache default 来自声明", f_teacache.get("default") == 0)
    check("teacache description 来自声明", "步间缓存" in f_teacache.get("description", ""))
    check("turbo max 来自声明", f_turbo.get("max") == 1.5)
    # 未声明档：lift 的 teacache 应标不生效
    entry_lift = toolinfo._model_entry(lift)
    f_lift_tc = next(f for f in entry_lift["fields"] if f["name"] == "teacache_threshold")
    check("lift 的 teacache applies=false", f_lift_tc["applies"] is False)
    check("lift 的 turbo applies=true（声明）",
          next(f for f in entry_lift["fields"] if f["name"] == "turbo_lora")["applies"] is True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
