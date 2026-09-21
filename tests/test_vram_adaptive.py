"""按显卡显存自动分档调参（`vram_adaptive`）与 LoadImage 插拔测试。

背景：MiniMax H3 SelfLift 工作流的权重合计 ~65 GiB，靠节点级分块在显存吃紧的卡上
跑起来。分块参数（`chunks` / `head_chunks` / `seq_threshold` / `highres_tiling`）的合适
取值只取决于显存大小，写死在 JSON 里换机器就得手改。现在在 models.yaml 声明
`vram_adaptive: true`，加载时按本机显存从 `defaults.vram_tiers` 取一档作为默认值
（显存探测见 gateway/vram.py）。
⚠️ 2026-09-21 起只有 `chunks` / `seq_threshold` 还有节点绑定（KJNodes 的 FFN 分块）；
`head_chunks`（原 MiniMaxLowVRAMAttention）与 `highres_tiling`（原 SelfLiftH3Sampler）
两个消费者都已撤除，档位值仍会进 spec.defaults 但**不会注入任何工作流** —— 下面的断言
按这个口径写。

同一工作流还兼作文生视频与带图生成：两个 LoadImage 可插拔 —— 请求里不传图，
网关提交前把对应节点删掉（= 文生视频）；传 1 张用首帧；传 2 张首尾都用。

验证点：
  [1] select_tier：档位选取（含容差、边界、探测失败）
  [2] normalize_tiers：格式校验与排序
  [3] total_vram_gb：环境变量钉住 / 非法值
  [4] registry 端到端：真实 models.yaml + 临时档位表 → 默认值落位、优先级、不外溢
  [5] LoadImage 插拔：传 0 / 1 / 2 张时节点与 aggregator 输入键的增删

自测：python tests/test_vram_adaptive.py
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent  # <ComfyUI>
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.disable(logging.CRITICAL)  # 静音被测模块的 normal 日志，只留断言输出

WF = "video_minimax_h3_lift.json"
TIERS = [
    {"min_gb": 48, "chunks": 1, "head_chunks": 4, "seq_threshold": 262144, "highres_tiling": False},
    {"min_gb": 24, "chunks": 2, "head_chunks": 8, "seq_threshold": 16384, "highres_tiling": True},
    {"min_gb": 8, "chunks": 6, "head_chunks": 24, "seq_threshold": 4096, "highres_tiling": True},
    {"min_gb": 0, "chunks": 8, "head_chunks": 28, "seq_threshold": 4096, "highres_tiling": True},
]

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def main() -> int:
    from gateway.registry import KNOWN_PARAMS, Registry, build_workflow
    from gateway import pipeline, vram
    from gateway.schemas import VideoGenerationRequest

    saved_env = os.environ.get(vram.ENV_KEY)

    print("\n[1] select_tier：档位选取")
    check("8.0 GiB -> 8 档", vram.select_tier(8.0, TIERS) == {
        "chunks": 6, "head_chunks": 24, "seq_threshold": 4096, "highres_tiling": True})
    check("24.0 GiB -> 24 档", vram.select_tier(24.0, TIERS)["chunks"] == 2)
    check("24.5 GiB -> 24 档（不越级到 48）", vram.select_tier(24.5, TIERS)["chunks"] == 2)
    check("48.0 GiB -> 48 档", vram.select_tier(48.0, TIERS)["chunks"] == 1)
    check("80.0 GiB 封顶 48 档", vram.select_tier(80.0, TIERS)["chunks"] == 1)
    check(
        "11.99 GiB（12G 卡实测值）容差落到 8 档而非更低档",
        vram.select_tier(11.99, TIERS)["chunks"] == 6,
        "显卡报的可用量普遍略低于标称",
    )
    check("8.4 GiB 仍留在 8 档", vram.select_tier(8.4, TIERS)["chunks"] == 6)
    check("7.6 GiB 因容差仍算 8 档", vram.select_tier(7.6, TIERS)["chunks"] == 6)
    check("2.0 GiB 落到 0 档兜底", vram.select_tier(2.0, TIERS)["chunks"] == 8)
    check("None（探测失败）-> 空字典，不覆盖", vram.select_tier(None, TIERS) == {})
    check("档位表为空 -> 空字典", vram.select_tier(8.0, []) == {})
    check("返回值不含 min_gb 自身", "min_gb" not in vram.select_tier(8.0, TIERS))

    print("\n[2] normalize_tiers：格式校验")
    norm = vram.normalize_tiers(list(reversed(TIERS)))
    check("按 min_gb 升序排列", [e["min_gb"] for e in norm] == [0, 8, 24, 48])
    check("空值 -> 空列表", vram.normalize_tiers(None) == [] and vram.normalize_tiers("") == [])
    for bad, label in (
        ({"min_gb": 8}, "非列表"),
        ([{"chunks": 2}], "缺 min_gb"),
        ([{"min_gb": "abc"}], "min_gb 非数字"),
        (["8"], "条目非映射"),
    ):
        try:
            vram.normalize_tiers(bad)
            check(f"拒绝：{label}", False, "未抛错")
        except RuntimeError:
            check(f"拒绝：{label}", True)

    print("\n[3] total_vram_gb：环境变量钉住")
    os.environ[vram.ENV_KEY] = "16"
    vram.reset_cache()
    check("ROUNDABOUT_VRAM_GB=16 生效", vram.total_vram_gb() == 16.0, f"got={vram.total_vram_gb()}")
    check("结果被缓存", vram.total_vram_gb() == 16.0)
    os.environ[vram.ENV_KEY] = "not-a-number"
    vram.reset_cache()
    check("非法值被忽略并返回 None", vram.total_vram_gb() is None)
    os.environ.pop(vram.ENV_KEY, None)
    vram.reset_cache()
    native = vram.total_vram_gb()
    check("去掉覆盖后回落到真实探测（无卡则为 None）", native is None or native > 0, f"got={native}")

    print("\n[4] registry 端到端：默认值落位与优先级")
    check("chunks/head_chunks/seq_threshold/highres_tiling 已是可注入参数",
          {"chunks", "head_chunks", "seq_threshold", "highres_tiling"} <= KNOWN_PARAMS)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "workflows").mkdir()
        shutil.copy(HERE / "workflows" / WF, tmpdir / "workflows" / WF)
        # 用真实的 SelfLift 工作流，但档位表换成 [1] 里那三档，便于断言
        (tmpdir / "models.yaml").write_text(
            "default_model: t\n"
            "defaults:\n"
            "  params:\n"
            "    negative_prompt: nope\n"
            "  vram_tiers:\n"
            + "".join(
                f"    - min_gb: {t['min_gb']}\n"
                + "".join(f"      {k}: {json.dumps(t[k])}\n" for k in
                          ("chunks", "head_chunks", "seq_threshold", "highres_tiling"))
                for t in TIERS
            )
            + "models:\n"
            "  t:\n"
            f"    workflow: {WF}\n"
            "    mode: video\n"
            "    output_node: '92'\n"
            "    vram_adaptive: true\n"
            "    references:\n"
            "      aggregator: '136'\n"
            "      images:\n"
            "      - '137'\n"
            "      - '139'\n"
            "      image_keys:\n"
            "      - first_frame\n"
            "      - last_frame\n"
            "    bindings:\n"
            "      prompt: 138.inputs.value\n"
            "      seed: 129.inputs.noise_seed\n"
            "      chunks: 158.inputs.chunks\n"
            "      seq_threshold: 158.inputs.seq_threshold\n"
            "  plain:\n"
            f"    workflow: {WF}\n"
            "    mode: video\n"
            "    bindings:\n"
            "      prompt: 138.inputs.value\n",
            encoding="utf-8",
        )

        for gb, expect in ((8.0, (6, 24, 4096)), (24.0, (2, 8, 16384)), (48.0, (1, 4, 262144))):
            os.environ[vram.ENV_KEY] = str(gb)
            vram.reset_cache()
            reg = Registry()
            reg.load(tmpdir / "models.yaml", tmpdir / "workflows", "t")
            spec = reg.resolve("t")
            got = (spec.defaults.get("chunks"), spec.defaults.get("head_chunks"),
                   spec.defaults.get("seq_threshold"))
            check(f"{gb:g} GiB -> 默认值 {expect[0]}/{expect[1]}/{expect[2]}", got == expect, f"got={got}")
            check(f"{gb:g} GiB -> vram_tier 记录在 spec 上", bool(spec.vram_tier))
            plain = reg.resolve("plain")
            check(f"{gb:g} GiB -> 未声明 vram_adaptive 的模型不受影响",
                  plain.defaults.get("chunks") is None and plain.vram_tier == {})

        # 注入后确实写进工作流节点（2026-09-21：head_chunks 已无节点，见 [6]）
        wf = build_workflow(reg.resolve("t"), {"prompt": "p", "chunks": 1,
                                              "seq_threshold": 262144}, None)
        check("分块参数注入到 158",
              wf["158"]["inputs"]["chunks"] == 1 and wf["158"]["inputs"]["seq_threshold"] == 262144,
              wf["158"]["inputs"])
        check("模板未被注入污染", reg.resolve("t").template["158"]["inputs"]["chunks"] == 1)

        # 回归：pipeline 组装出的 values 是一份白名单，永远不会带 chunks / seq_threshold 这些键，
        # 所以档位值必须由 spec.defaults 兜住。早期只有「调用方主动传参」才注入，结果是
        # list_models 报 6/24、真正提交给 ComfyUI 的却还是模板里写死的 2/8。
        for gb, expect in ((8.0, (6, 4096)), (24.0, (2, 16384))):
            os.environ[vram.ENV_KEY] = str(gb)
            vram.reset_cache()
            reg_d = Registry()
            reg_d.load(tmpdir / "models.yaml", tmpdir / "workflows", "t")
            wf_d = build_workflow(reg_d.resolve("t"), {"prompt": "p"}, None)
            got_d = (wf_d["158"]["inputs"]["chunks"], wf_d["158"]["inputs"]["seq_threshold"])
            check(f"{gb:g} GiB -> 不传分块参数也按档位注入 {expect}", got_d == expect, f"got={got_d}")

        os.environ[vram.ENV_KEY] = "48"
        vram.reset_cache()
        wf_x = build_workflow(reg.resolve("t"), {"prompt": "p", "chunks": 9}, None)
        check("请求参数仍然压过档位值", wf_x["158"]["inputs"]["chunks"] == 9, f"got={wf_x['158']['inputs']['chunks']}")

        # 模型自己写的 defaults 覆盖档位值
        text = (tmpdir / "models.yaml").read_text(encoding="utf-8").replace(
            "    vram_adaptive: true\n", "    vram_adaptive: true\n    defaults:\n      chunks: 3\n")
        (tmpdir / "models.yaml").write_text(text, encoding="utf-8")
        os.environ[vram.ENV_KEY] = "48"
        vram.reset_cache()
        reg2 = Registry()
        reg2.load(tmpdir / "models.yaml", tmpdir / "workflows", "t")
        s = reg2.resolve("t")
        check("模型自带 defaults 覆盖档位值", s.defaults.get("chunks") == 3, f"got={s.defaults.get('chunks')}")
        check("其余档位值仍来自显存", s.defaults.get("head_chunks") == 4)

        print("\n[5] LoadImage 插拔：0 / 1 / 2 张图")
        spec = reg2.resolve("t")

        def _dangling(wf: dict) -> list[str]:
            """节点被删掉后，别处还指向它的连线就是悬空连接，提交会被 ComfyUI 拒。"""
            bad = []
            for nid, node in wf.items():
                for key, val in (node.get("inputs") or {}).items():
                    if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str) and val[0] not in wf:
                        bad.append(f"{nid}.{key}->{val[0]}")
            return bad

        def _reachable(wf: dict, out: str) -> set[str]:
            """从输出节点反向可达的节点集合；不可达的节点是白算的孤儿。"""
            seen: set[str] = set()
            stack = [out]
            while stack:
                cur = stack.pop()
                if cur in seen or cur not in wf:
                    continue
                seen.add(cur)
                for val in (wf[cur].get("inputs") or {}).values():
                    if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                        stack.append(val[0])
            return seen

        for n, keep in ((0, ()), (1, ("137",)), (2, ("137", "139"))):
            wf2 = build_workflow(spec, {"prompt": "p"}, None)
            req = VideoGenerationRequest(prompt="p", reference_images=(["a.png"] * n) or None)
            dropped = pipeline._prune_unused_references(wf2, spec, req)
            present = tuple(nid for nid in ("137", "139") if nid in wf2)
            keys = sorted(k for k in wf2["136"]["inputs"] if k in ("first_frame", "last_frame"))
            check(f"传 {n} 张图 -> 保留 {keep or '（无）'}", present == keep, f"dropped={dropped}")
            check(f"传 {n} 张图 -> aggregator 输入键同步", keys == ["first_frame", "last_frame"][:n],
                  f"keys={keys}")
            check(f"传 {n} 张图 -> 聚合节点 136 始终保留", "136" in wf2)
            check(f"传 {n} 张图 -> 无悬空连线", not _dangling(wf2), "; ".join(_dangling(wf2)))
            roots = [nid for nid, nd in wf2.items()
                     if nd.get("class_type") in ("SaveVideo", "SaveImage", "PreviewImage")]
            seen: set[str] = set()
            for root in roots:
                seen |= _reachable(wf2, root)
            orphans = sorted(set(wf2) - seen, key=int)
            check(f"传 {n} 张图 -> 无不可达孤儿节点", not orphans, f"orphans={orphans}")

        print("\n[6] 真实 models.yaml：基础两支分块自适应 + 稀疏注意力（sol-attn）接线")
        os.environ[vram.ENV_KEY] = "8"
        vram.reset_cache()
        reg_real = Registry()
        reg_real.load(HERE / "models.yaml", HERE / "workflows", "z-image")
        for name, ff_node in (("minimax-h3", "158"), ("minimax-h3-edit", "158")):
            sp = reg_real.resolve(name)
            check(f"{name}: vram_adaptive 已声明", sp.vram_adaptive is True and bool(sp.vram_tier))
            check(f"{name}: 可注入绑定只剩 chunks / seq_threshold",
                  {"chunks", "seq_threshold"} <= set(sp.bindings)
                  and not ({"head_chunks", "highres_tiling"} & set(sp.bindings)), sorted(sp.bindings))
            wf_r = build_workflow(sp, {"prompt": "p"}, None)
            check(f"{name}: 稀疏节点 159 = BlockSparseAttention(sol-attn)，链路 156 -> 159 -> 158",
                  wf_r["159"]["class_type"] == "BlockSparseAttention"
                  and wf_r["159"]["inputs"]["selection"] == "sol-attn"
                  and wf_r["159"]["inputs"]["model"] == ["156", 0]
                  and wf_r[ff_node]["inputs"]["model"] == ["159", 0],
                  wf_r["159"]["inputs"])
            check(f"{name}: 链路中已无 MiniMaxLowVRAMAttention（与稀疏硬互斥）",
                  all(nd.get("class_type") != "MiniMaxLowVRAMAttention" for nd in wf_r.values()))
            check(f"{name}: 8GB 档注入 158 chunks=6、seq_threshold=4096",
                  wf_r["158"]["inputs"]["chunks"] == 6
                  and wf_r["158"]["inputs"]["seq_threshold"] == 4096,
                  wf_r["158"]["inputs"])
            check(f"{name}: 注入后无悬空连线",
                  all(not isinstance(v, list) or v[0] in wf_r
                      for node in wf_r.values() for v in (node.get("inputs") or {}).values()
                      if isinstance(v, list)))

    if saved_env is not None:
        os.environ[vram.ENV_KEY] = saved_env
    else:
        os.environ.pop(vram.ENV_KEY, None)
    vram.reset_cache()

    print()
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
