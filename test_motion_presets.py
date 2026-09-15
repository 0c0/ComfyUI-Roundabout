"""motion 命名档（文戏 / 打戏）回归测试。

要点：本测试直接调用 gateway.pipeline.build_video_values —— 即生成请求真正走的那条组装路径，
不在测试里复刻顺序。早期 test_vram_adaptive.py 因为自己把参数递进 values、绕开了真实路径，
出现过「51 项全绿但线上一个都没生效」的假绿，这里刻意避免重蹈覆辙。

    python test_motion_presets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gateway.errors import APIError  # noqa: E402
from gateway.pipeline import build_video_values  # noqa: E402
from gateway.registry import Registry, build_workflow  # noqa: E402
from gateway.schemas import VideoGenerationRequest  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


registry = Registry()
registry.load(ROOT / "models.yaml", ROOT / "workflows", "z-image")

LIFT = registry.resolve("minimax-h3-self-lift")
EDIT = registry.resolve("minimax-h3-self-lift-edit")
IMAGE = registry.resolve("z-image")

STORY = {"steps": 6, "transition_step": 5}
FIGHT = {"steps": 8, "transition_step": 6}


def values_for(spec=LIFT, prompt="x", **kw):
    """走真实组装路径（含预设展开与显式入参覆盖）。"""
    return build_video_values(VideoGenerationRequest(prompt=prompt, **kw), spec, prompt, None)


def nodes_for(req_kw, spec=LIFT):
    """组装 → 注入节点，返回 (基本调度器步数, SelfLift 过渡步)。"""
    wf = build_workflow(spec, values_for(spec, **req_kw))
    return wf["124"]["inputs"]["steps"], wf["235"]["inputs"]["transition_step"]


print("== 1. 注册解析 ==")
for spec in (LIFT, EDIT):
    check(f"{spec.name}: story / fight 档已声明",
          spec.motion_presets.get("story") == STORY and spec.motion_presets.get("fight") == FIGHT,
          spec.motion_presets)
    check(f"{spec.name}: transition_step 绑定到 235",
          spec.bindings.get("transition_step") == ["235.inputs.transition_step"],
          spec.bindings.get("transition_step"))
check("z-image 未声明 motion 档", not IMAGE.motion_presets)

print("\n== 2. 组装路径：命名档取值 ==")
v = values_for(motion="story")
check("story -> steps / transition_step", (v["steps"], v["transition_step"]) == (6, 5),
      (v["steps"], v["transition_step"]))
v = values_for(motion="fight")
check("fight -> steps / transition_step", (v["steps"], v["transition_step"]) == (8, 6),
      (v["steps"], v["transition_step"]))
check("story 大小写不敏感", values_for(motion="STORY")["steps"] == 6)
check("首尾空格被裁掉", values_for(motion=" story ")["steps"] == 6)
check("未传 motion 时默认走 story 档（6 / 5）",
      (values_for()["steps"], values_for()["transition_step"]) == (6, 5),
      (values_for()["steps"], values_for()["transition_step"]))
check("不传 motion 与显式 motion=story 完全等价", values_for() == values_for(motion="story"))
check("默认档由 defaults.motion 声明，不是把数值抄两遍",
      LIFT.defaults.get("motion") == "story" and EDIT.defaults.get("motion") == "story",
      (LIFT.defaults.get("motion"), EDIT.defaults.get("motion")))

print("\n== 3. 显式入参优先于命名档 ==")
v = values_for(motion="story", steps=9)
check("story + steps=9 -> 9 / 5", (v["steps"], v["transition_step"]) == (9, 5),
      (v["steps"], v["transition_step"]))
v = values_for(motion="story", transition_step=4)
check("story + transition_step=4 -> 6 / 4", (v["steps"], v["transition_step"]) == (6, 4),
      (v["steps"], v["transition_step"]))
v = values_for(motion="story", steps=9, transition_step=7)
check("两个都显式 -> 9 / 7", (v["steps"], v["transition_step"]) == (9, 7),
      (v["steps"], v["transition_step"]))
v = values_for(transition_step=4)
check("只传 transition_step=4（步数按默认档 story）-> 6 / 4", (v["steps"], v["transition_step"]) == (6, 4),
      (v["steps"], v["transition_step"]))

print("\n== 4. 落到工作流节点（端到端） ==")
check("story -> 124.steps=6 / 235.transition_step=5", nodes_for({"motion": "story"}) == (6, 5),
      nodes_for({"motion": "story"}))
check("fight -> 124.steps=8 / 235.transition_step=6", nodes_for({"motion": "fight"}) == (8, 6),
      nodes_for({"motion": "fight"}))
check("不传 -> 默认档 story 落到节点 6 / 5", nodes_for({}) == (6, 5), nodes_for({}))
check("self-lift-edit 不传也是 6 / 5", nodes_for({}, EDIT) == (6, 5), nodes_for({}, EDIT))
check("story + steps=9 -> 9 / 5", nodes_for({"motion": "story", "steps": 9}) == (9, 5),
      nodes_for({"motion": "story", "steps": 9}))
check("self-lift-edit 同样生效（story）", nodes_for({"motion": "story"}, EDIT) == (6, 5),
      nodes_for({"motion": "story"}, EDIT))

print("\n== 5. 分档参数不受影响 ==")
v = values_for(motion="story")
tier_keys = {"chunks", "head_chunks", "seq_threshold", "highres_tiling"}
check("显存档位仍在 defaults 里（未被 motion 打乱）",
      tier_keys.issubset(LIFT.defaults.keys()), sorted(tier_keys - set(LIFT.defaults)))
wf = build_workflow(LIFT, values_for(motion="story"))
check("分块参数照旧注入",
      all(wf[n]["inputs"][k] == LIFT.defaults[k]
          for n, k in (("219", "chunks"), ("220", "head_chunks"), ("219", "seq_threshold"), ("235", "highres_tiling"))),
      [(n, k, wf[n]["inputs"][k]) for n, k in (("219", "chunks"), ("220", "head_chunks"))])

print("\n== 6. 错误处理 ==")
for label, fn, needle in (
    ("拼错的档位报错并列出可选值",
     lambda: values_for(motion="typo"), "story, fight"),
    ("未声明 motion 的模型传 motion 报错",
     lambda: values_for(IMAGE, motion="story"), "no `motion` presets"),
    ("transition_step 等于 steps 时报错（须 <= steps-1）",
     lambda: values_for(motion="story", steps=5), "必须满足"),
    ("transition_step 超过上限时报错",
     lambda: values_for(steps=8, transition_step=8), "必须满足"),
):
    try:
        fn()
        check(label, False, "没有报错")
    except APIError as exc:
        check(label, needle in str(exc), str(exc)[:100])

print("\n== 7. 边界值（1 与 steps-1） ==")
v = values_for(steps=8, transition_step=1)
check("transition_step=1 合法", v["transition_step"] == 1)
v = values_for(steps=8, transition_step=7)
check("transition_step=steps-1 合法", v["transition_step"] == 7)
check("story 档 6/5 本身满足约束", (STORY["transition_step"] < STORY["steps"]))
check("fight 档 8/6 本身满足约束", (FIGHT["transition_step"] < FIGHT["steps"]))

print("\n== 8. 默认档与档表不许漂移 ==")
# defaults 里同时写「档名」和「同值的数值」时，两处一旦不同步就会误导后来的人
# （档表改了、数值没改，读 YAML 的人以为默认是旧的）。这里把它变成受检不变量。
for spec in (LIFT, EDIT):
    default_name = spec.defaults.get("motion")
    check(f"{spec.name}: defaults.motion={default_name!r} 指向已声明的档",
          default_name in spec.motion_presets, sorted(spec.motion_presets))
    preset = spec.motion_presets.get(default_name) or {}
    for key, want in preset.items():
        if key in spec.defaults:
            check(f"{spec.name}: defaults.{key}={spec.defaults[key]} 与 {default_name} 档一致",
                  spec.defaults[key] == want, f"defaults={spec.defaults[key]} 档表={want}")
    check(f"{spec.name}: defaults.motion 不进 bindings（不会被当参数注入节点）",
          "motion" not in spec.bindings)

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
