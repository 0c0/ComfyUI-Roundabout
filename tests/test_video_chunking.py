# -*- coding: utf-8 -*-
"""低显存分块链路护栏（模板兜底 / 档位注入 / 执行链引用 / 高分辨率分块开关）。

为什么需要这个测试：
    「看起来没分块」误导过一次排查。磁盘模板里 `158.chunks` 写的是 1 —— 那是 KJNodes 的
    「关」值（`if chunks == 1: return model` 直接早退），真实值由 `defaults.vram_tiers`
    在提交时按本机显存注入。于是「模板字面值」和「跑起来的值」是两回事：

      · 网关路径：档位注入生效（8 GiB -> chunks=6），日志与提交图都能验证；
      · UI 直跑 / 探不到显存 / 模型未开 `vram_adaptive`：吃的是模板字面值。这几条旁路
        曾经写死 1 / 262144，等于**静默全量不分块**，而且全仓没有任何测试守着 ——
        谁把 1 写回去都不会报错，直到 8 GiB 机器 OOM。

    本测试锁四件事：
      1. 四支视频模板的 158 字面值必须是低显存兜底（chunks>1、seq_threshold 不过高）；
      2. 四支都绑了 chunks / seq_threshold，且档位值**真的**覆盖模板值（双向：低档调高、高档调回 1）；
      3. 158 必须同时被 scheduler 与 guider/sampler 引用 —— 节点在但不在执行链上＝同样没分块；
      4. `highres_tiling` 只在 lift 两支可开（SelfLift 的升采样后分块），且默认关
         （节点 tooltip：只保留第一块的音频预测，会改变画质）。

不碰正在运行的 ComfyUI、不联网、不落产物：纯 JSON + registry 断言。

    python tests/test_video_chunking.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import vram  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.registry import Registry, build_workflow  # noqa: E402

NODE = Path(__file__).resolve().parents[1]
WF = NODE / "workflows"

VIDEO = {
    "minimax-h3": "video_minimax_h3.json",
    "minimax-h3-edit": "video_minimax_h3_edit.json",
    "minimax-h3-lift": "video_minimax_h3_lift.json",
    "minimax-h3-lift-edit": "video_minimax_h3_lift_edit.json",
}

passed = failed = 0


def check(name: str, cond, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


def load_registry(pinned_gb: str | None) -> Registry:
    """按指定显存载入真实 models.yaml（`ROUNDABOUT_VRAM_GB` 钉档，探测结果有缓存需重置）。"""
    if pinned_gb is None:
        os.environ.pop(vram.ENV_KEY, None)
    else:
        os.environ[vram.ENV_KEY] = pinned_gb
    vram.reset_cache()
    r = Registry()
    r.load(settings.models_file, settings.workflows_dir, settings.default_model, settings.workflows_local_dir)
    return r


def main() -> int:
    # ---------------------------------------------------------------- 1. 模板兜底值
    print("1. 模板字面值 = 低显存兜底（旁路不吃亏）")
    for model, fname in VIDEO.items():
        wf = json.loads((WF / fname).read_text(encoding="utf-8"))
        ff = wf.get("158") or {}
        ok_type = ff.get("class_type") == "MiniMaxChunkFeedForward"
        inputs = ff.get("inputs") or {}
        check(
            f"{fname}: 158 是分块节点且字面值 >1 块",
            ok_type and int(inputs.get("chunks") or 1) > 1,
            f"class={ff.get('class_type')} inputs={inputs}",
        )
        check(
            f"{fname}: seq_threshold 不高于 8192（否则长序列也进不了分块）",
            ok_type and int(inputs.get("seq_threshold") or 10**9) <= 8192,
            str(inputs.get("seq_threshold")),
        )

    # ---------------------------------------------------------------- 2. 执行链引用
    print("2. 158 同时在 scheduler 与 guider/sampler 的链上")
    for model, fname in VIDEO.items():
        wf = json.loads((WF / fname).read_text(encoding="utf-8"))
        users = {}
        for nid, node in wf.items():
            if nid == "158":
                continue
            for key, val in (node.get("inputs") or {}).items():
                if isinstance(val, list) and str(val[0]) == "158":
                    users.setdefault(node.get("class_type"), []).append(key)
        check(f"{fname}: BasicScheduler 吃 158 的模型",
              "BasicScheduler" in users, str(users))
        sampler = [c for c in users if c in ("BasicGuider", "SelfLiftH3Sampler")]
        check(f"{fname}: 采样侧的 guider/sampler 吃 158 的模型",
              bool(sampler), str(users))

    # ---------------------------------------------------------------- 3. 档位注入（双向）
    print("3. 档位注入真的覆盖模板值（低档调高 / 高档调回 1）")
    r8 = load_registry("8")
    for model, fname in VIDEO.items():
        spec = r8.resolve(model)
        check(f"{model}: 绑了 chunks / seq_threshold",
              spec.binds("chunks") and spec.binds("seq_threshold"),
              f"{spec.bindings.get('chunks')} / {spec.bindings.get('seq_threshold')}")
        check(f"{model}: 开了 vram_adaptive", spec.vram_adaptive is True, str(spec.vram_adaptive))
        out = build_workflow(spec, {})
        got = out["158"]["inputs"]
        check(f"{model}: 8 GiB 提交图 chunks = 档位值(6)，不是模板值",
              int(got["chunks"]) == 6 and int(got["seq_threshold"]) == 4096,
              str(got))
        check(f"{model}: head_chunks 无绑定（档位表该列不生效，属已知设计）",
              not spec.binds("head_chunks"), str(spec.bindings.get("head_chunks")))

    r48 = load_registry("48")
    for model in VIDEO:
        out = build_workflow(r48.resolve(model), {})
        got = out["158"]["inputs"]
        check(f"{model}: 48 GiB 提交图回到 1 块（大卡不付分块开销）",
              int(got["chunks"]) == 1 and int(got["seq_threshold"]) == 262144,
              str(got))

    # ---------------------------------------------------------------- 4. 高分辨率分块开关
    print("4. highres_tiling：仅 lift 两支可开，默认关")
    r = load_registry("8")
    for model in ("minimax-h3-lift", "minimax-h3-lift-edit"):
        spec = r.resolve(model)
        check(f"{model}: 声明 highres_tiling 并绑到 SelfLift",
              "highres_tiling" in spec.param_specs
              and spec.bindings.get("highres_tiling") == ["125.inputs.highres_tiling"],
              str(spec.bindings.get("highres_tiling")))
        check(f"{model}: 默认 false（会改画质，需显式开）",
              spec.defaults.get("highres_tiling") is False,
              str(spec.defaults.get("highres_tiling")))
        on = build_workflow(spec, {"highres_tiling": True})
        check(f"{model}: 显式开 true 能落到 125",
              on["125"]["inputs"]["highres_tiling"] is True,
              str(on["125"]["inputs"]["highres_tiling"]))
        off = build_workflow(spec, {})
        check(f"{model}: 不传时是 false", off["125"]["inputs"]["highres_tiling"] is False,
              str(off["125"]["inputs"]["highres_tiling"]))
    for model in ("minimax-h3", "minimax-h3-edit"):
        spec = r.resolve(model)
        check(f"{model}: 不声明 highres_tiling（传了应报 400）",
              "highres_tiling" not in spec.param_specs and not spec.binds("highres_tiling"),
              str(spec.param_specs.keys()))

    print(f"\n {'=' * 46}\n  {'ALL CHECKS PASSED' if not failed else str(failed) + ' CHECK(S) FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
