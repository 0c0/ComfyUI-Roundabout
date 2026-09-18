"""结构化模型编辑的合并语义回归测试。

背景：前端设置面板只渲染 workflow / timeout / defaults / bindings / capabilities /
output_node / description / aliases，而 references（参考槽拓扑）、vram_adaptive（显存分档）、
promptless（无提示词工具流）与各类 *_presets 它不认识。若 PUT 按 payload 从零重建，
用户在 /settings 里点一次保存，这些配置就无声消失。本测试锁住「以磁盘条目为基底合并」的语义。

    python tests/test_admin_structured_merge.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.admin import _MODEL_ENTRY_KEYS, merge_model_entry  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


raw = yaml.safe_load((ROOT / "models.yaml").read_text(encoding="utf-8"))
LIFT = raw["models"]["minimax-h3-self-lift"]

# 前端实际会提交的形状：只带它渲染过的键（且 defaults / bindings 总是存在，可能为空）
FRONTEND_PAYLOAD = {
    "name": "minimax-h3-self-lift",
    "workflow": "video_minimax_h3_self_lift.json",
    "description": "改过的描述",
    "mode": "video",
    "capabilities": ["text-to-video", "image-to-video"],
    "output_node": "238",
    "aliases": ["selflift"],
    "timeout": 1800,
    "defaults": {"steps": 8},
    "bindings": {"prompt": "232.inputs.value"},
}

print("== 1. 前端不认识的键必须原样保留 ==")
merged = merge_model_entry(LIFT, FRONTEND_PAYLOAD)
for key in ("references", "vram_adaptive", "motion_presets"):
    check(f"{key} 保留", merged.get(key) == LIFT.get(key), merged.get(key))
check("motion_presets 内容未变（story 8/8、fight 10/8）",
      merged["motion_presets"]["story"] == {"steps": 8, "transition_step": 8}
      and merged["motion_presets"]["fight"] == {"steps": 10, "transition_step": 8},
      merged.get("motion_presets"))
check("references.aggregator 仍是 136", (merged.get("references") or {}).get("aggregator") == "136")
check("vram_adaptive 仍为 true", merged.get("vram_adaptive") is True)

print("\n== 2. 前端提交的键要生效 ==")
check("description 被更新", merged["description"] == "改过的描述")
check("timeout 被更新", merged["timeout"] == 1800)
check("aliases 被整块替换", merged["aliases"] == ["selflift"])
check("bindings 被整块替换（改 binding 能生效）",
      merged["bindings"] == {"prompt": "232.inputs.value"}, merged["bindings"])
check("defaults 被整块替换", merged["defaults"] == {"steps": 8})

print("\n== 3. 空 dict / 空 list 不当作清空 ==")
merged = merge_model_entry(LIFT, {"name": "x", "bindings": {}, "capabilities": [], "defaults": {}})
check("空 bindings 保留原值", merged["bindings"] == LIFT["bindings"], merged["bindings"])
check("空 capabilities 保留原值", merged["capabilities"] == LIFT["capabilities"])
check("空 defaults 保留原值", merged["defaults"] == LIFT["defaults"])

print("\n== 4. 显式 false 要能写进去 ==")
merged = merge_model_entry(LIFT, {"name": "x", "vram_adaptive": False})
check("vram_adaptive=False 生效（False 不等于「没提交」）",
      merged["vram_adaptive"] is False, merged.get("vram_adaptive"))

print("\n== 5. 新模型从空条目起 ==")
merged = merge_model_entry(None, FRONTEND_PAYLOAD)
check("新模型只含提交的键", set(merged) <= set(_MODEL_ENTRY_KEYS), sorted(merged))
check("新模型没有凭空多出 references", "references" not in merged)
check("新模型 description 正常", merged["description"] == "改过的描述")

print("\n== 6. 易丢字段都在白名单里 ==")
for key in ("references", "vram_adaptive", "promptless", "motion_presets", "quality_presets", "style_presets"):
    check(f"_MODEL_ENTRY_KEYS 含 {key}", key in _MODEL_ENTRY_KEYS)

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
