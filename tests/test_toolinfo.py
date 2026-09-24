"""调用结构自描述（`gateway/toolinfo.py`）的守护测试。

背景：MCP 工具描述与 REST 文档都是手写散文，模型一多必然失同步 —— 历史上 MCP 的
`edit_image` 描述里就长期挂着「这类模型可以不传 image，只用 reference_images」这句
**错口径**（真相是：0 输入会被 400 挡掉，纯 t2i 由单列文生图模型承担）。

处置不是再写一遍散文去对齐，而是把「调用面」变成**从运行期状态推导的结构**：
本测试守着这个推导结果的几条不变量 —— 一旦有人给模型加了绑定、换了字段约束、
或改了参考槽拓扑，而未在自描述里正确体现，这里立刻红。

    python tests/test_toolinfo.py
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# toolinfo 只依赖 gateway 内部模块，但 gateway/__init__ 会把包挂到 ComfyUI 的
# PromptServer 上；测试环境没有它，故用「只设 __path__ 的空包」绕过执行 __init__。
if "rb_gateway" not in sys.modules:
    pkg = types.ModuleType("rb_gateway")
    pkg.__path__ = [str(ROOT / "gateway")]
    sys.modules["rb_gateway"] = pkg

from rb_gateway.config import settings  # noqa: E402
from rb_gateway.registry import registry  # noqa: E402
from rb_gateway.schemas import ImageGenerationRequest, VideoGenerationRequest  # noqa: E402
from rb_gateway import toolinfo  # noqa: E402

registry.load(settings.models_file, settings.workflows_dir, settings.default_model)

PAYLOAD = toolinfo.build()
MODELS = {m["name"]: m for m in PAYLOAD["models"]}

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


# ============================================================================
print("=== [1] 覆盖度：每个在册模型都要有自己的一块 ===")
check("模型数与 registry 一致",
      len(PAYLOAD["models"]) == len(registry.names()),
      f"{len(PAYLOAD['models'])} vs {len(registry.names())}")
check("模型块顺序与 registry 一致",
      [m["name"] for m in PAYLOAD["models"]] == registry.names())
check("counts.models 自洽",
      PAYLOAD["counts"]["models"] == len(PAYLOAD["models"]))
check("字段顺序清单非空", all(PAYLOAD["field_order"].values()))

print("\n=== [2] 逐模型字段清单 = 对应请求模型的全部字段 ===")
# 图像档给图像字段、视频档给视频字段；漏一个字段前端就渲染不出那个输入框。
for name, entry in MODELS.items():
    spec = registry.resolve(name)
    want = VideoGenerationRequest if spec.is_video else ImageGenerationRequest
    got = [f["name"] for f in entry["fields"]]
    check(f"{name} 字段集完整", got == list(want.model_fields),
          f"缺 {sorted(set(want.model_fields) - set(got))} / 多 {sorted(set(got) - set(want.model_fields))}")

print("\n=== [3] applies / inactive_reason 契约 ===")
# 生效就必须无原因，不生效就必须有原因 —— 否则前端显示「可用 + 一段解释为什么不可用」。
for name, entry in MODELS.items():
    bad = []
    for f in entry["fields"]:
        if f["applies"] and f.get("inactive_reason"):
            bad.append(f"{f['name']}(生效却有原因)")
        if not f["applies"] and not f.get("inactive_reason"):
            bad.append(f"{f['name']}(不生效却无原因)")
    check(f"{name} 契约自洽", not bad, bad)

print("\n=== [4] 区间 / 枚举 / 必填 与 schema 逐字段对齐 ===")
for name, entry in MODELS.items():
    spec = registry.resolve(name)
    model = VideoGenerationRequest if spec.is_video else ImageGenerationRequest
    diff = []
    for f in entry["fields"]:
        fi = model.model_fields[f["name"]]
        if f.get("required") != fi.is_required():
            diff.append(f"{f['name']}.required")
        if f.get("default") != (None if fi.is_required() else fi.default) and not fi.is_required():
            diff.append(f"{f['name']}.default")
    check(f"{name} 必填/默认值对齐", not diff, diff)

print("\n=== [5] 关键语义（历史踩过的错口径）===")
# 5.1 promptless 工具档必须标出 prompt 不生效
bir = MODELS.get("utility-birefnet-remove-background")
if bir:
    prompt = [f for f in bir["fields"] if f["name"] == "prompt"][0]
    check("promptless 档 prompt 标为不生效", prompt["applies"] is False, prompt)

# 5.2 编辑档的可编辑性：klein 4 槽 / qwen 合并档 6 槽，且 slot 数为 0 的模型不得标 applies
for name, slots in (("flux2-klein-image-edit-turbo", 4), ("qwen-image-2.1", 6)):
    e = MODELS.get(name)
    if not e:
        continue
    ri = [f for f in e["fields"] if f["name"] == "reference_images"][0]
    check(f"{name} 参考槽={slots}", e["reference_slots"]["images"] == slots, e["reference_slots"])
    check(f"{name} reference_images 生效", ri["applies"] is True, ri)

for name, entry in MODELS.items():
    if entry["reference_slots"]["images"] == 0:
        ri = [f for f in entry["fields"] if f["name"] == "reference_images"][0]
        check(f"{name} 无槽 → reference_images 不生效", ri["applies"] is False, ri)

# 5.3 图像档的 `image` 单图入口：生效性 == 是否声明了 image 绑定。
#     `qwen-image-2.1` 是刻意的反例 —— 文生与多图编辑同支、有 6 个参考槽，却没有 image 绑定：
#     参考槽一旦被 binding 保护就删不掉，纯文生那一支会把模板占位图 `__REF_1__.png` 当参考喂进去。
#     （`image` 改由网关落成第 1 张参考图，`pipeline._ref_images`。）
for name, entry in MODELS.items():
    spec = registry.resolve(name)
    if spec.is_video:
        continue
    img = [f for f in entry["fields"] if f["name"] == "image"][0]
    check(f"{name} image 生效性 = 声明了 image 绑定", img["applies"] is spec.binds("image"), img)

qwen = MODELS["qwen-image-2.1"]
check("qwen-image-2.1 文生 + 编辑双能力",
      set(qwen["capabilities"]) == {"text-to-image", "image-to-image"}, qwen["capabilities"])
check("qwen-image-2.1 image 不生效（改由网关落成第 1 张参考图）",
      [f for f in qwen["fields"] if f["name"] == "image"][0]["applies"] is False)
check("qwen-image-2.1 接受负向提示词（绑定层）",
      [f for f in qwen["fields"] if f["name"] == "negative_prompt"][0]["applies"] is True)
# 注意：applies 只表示「有 binding」。它真的参与计算还要 cfg > 1 —— 该档模板取 cfg=1，
# ComfyUI 的 cfg1 优化会整条跳过负向分支（实测同 seed 只改负向：cfg=1 ⇒ MAE 0.0000、
# cfg=4 ⇒ 23.5）。self-description 目前没有表达这种「绑定有效但本次不参与」的位置。

# 5.4 attention：base 四支生效、fast 两支不生效（vsa 与蒸馏权重配对，无 dense 对照）
for name in ("minimax-h3", "minimax-h3-edit", "minimax-h3-lift", "minimax-h3-lift-edit"):
    e = MODELS.get(name)
    if e:
        f = [x for x in e["fields"] if x["name"] == "attention"][0]
        check(f"{name} attention 生效", f["applies"] is True, f)
for name in ("fasth3", "fasth3-edit"):
    e = MODELS.get(name)
    if e:
        f = [x for x in e["fields"] if x["name"] == "attention"][0]
        check(f"{name} attention 不生效", f["applies"] is False, f)

# 5.5 scale 只在视频档存在，且只对声明了 scale 绑定的 lift 两支生效
for name, entry in MODELS.items():
    hit = [x for x in entry["fields"] if x["name"] == "scale"]
    spec = registry.resolve(name)
    if not spec.is_video:
        check(f"{name}（图像档）无 scale 字段", not hit, hit)
        continue
    check(f"{name} scale 生效性 = 声明了 scale 绑定", hit[0]["applies"] is spec.binds("scale"), hit[0])

print("\n=== [6] 全局块 ===")
s = PAYLOAD["surface"]
check("max_n 与 settings 一致", s["max_n"] == settings.max_n, s["max_n"])
check("种子上限 = SAFE_SEED", s["seed"]["max"] == 2**53 - 1, s["seed"])
check("尺寸档位齐全", s["video_sizes"]["tiers"] == sorted([480, 576, 720, 768, 1080, 1440]),
      s["video_sizes"]["tiers"])
check("尺寸比例齐全",
      s["video_sizes"]["ratios"] == sorted(["1:1", "3:4", "4:3", "16:9", "9:16"]),
      s["video_sizes"]["ratios"])
check("尺寸预设表覆盖全部档位",
      set(s["video_sizes"]["presets"]) == {str(t) for t in s["video_sizes"]["tiers"]})
check("端点表两套都有", set(PAYLOAD["endpoints"]) == {"rest", "mcp"})
# duration 的 1–15 是网关硬闸（pipeline 400），不在 pydantic 约束上 ⇒ 必须显式声明，
# 否则只看字段清单的调用者无从得知这个上限。
check("surface 声明 duration 1–15",
      s["video"]["duration_seconds"] == {"min": 1, "max": 15}, s.get("video"))
check("compact 也带 duration 上限",
      toolinfo.compact()["surface"]["duration_seconds"] == {"min": 1, "max": 15})

print("\n=== [6.1] 端点清单不得与实际工具集漂移 ===")
# 这份清单是手写的（路由表与 MCP 工具集都在别的文件里），所以必须机器核对 ——
# 否则「加了工具忘了登记」会让自描述文档变成假话。
import ast  # noqa: E402

tree = ast.parse((ROOT / "mcp_server.py").read_text(encoding="utf-8"))
registered: set[str] = set()
for node in tree.body:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        for kw in dec.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                registered.add(kw.value.value)
declared = set(PAYLOAD["endpoints"]["mcp"])
check("端点清单与 @mcp.tool 注册集完全一致",
      declared == registered,
      f"清单缺 {sorted(registered - declared)} / 清单多 {sorted(declared - registered)}")
check("MCP 工具数为 16", len(registered) == 16, sorted(registered))

rest_src = (ROOT / "gateway" / "routes.py").read_text(encoding="utf-8")
for path in PAYLOAD["endpoints"]["rest"]:
    url = path.split(" ", 1)[1]
    check(f"REST 端点已登记 {url}", url in rest_src)

print("\n=== [7] compact 视图 ===")
c = toolinfo.compact()
check("compact 覆盖全部模型", len(c["models"]) == len(registry.names()))
one = toolinfo.compact(model="minimax-h3")
check("compact 可单模型查询", len(one["models"]) == 1, one["models"])
check("compact 去掉字段 description",
      all("description" not in f for f in one["models"][0]["fields"]))
nofields = toolinfo.compact(model="minimax-h3", include_fields=False)
check("compact 可省字段清单", "fields" not in nofields["models"][0])
check("compact 比完整版小", len(str(c)) < len(str(PAYLOAD)))

# compact 是本接口交给 agent 的那一份 —— 它必须**保留 `applies`**。
# 早期实现用 _walk_request_fields 单独拼字段表，把 applies 整个丢了：返回了一份
# 没有答案的字段清单（「这个字段能不能用」正是调用者来问的问题）。这里守住它。
miss = [(m["name"], f["name"]) for m in c["models"] for f in m.get("fields", [])
        if "applies" not in f]
check("compact 字段都带 applies", not miss, miss)

drift = []
for m in c["models"]:
    full = {f["name"]: f for f in MODELS[m["name"]]["fields"]}
    for f in m.get("fields", []):
        if full[f["name"]]["applies"] != f["applies"]:
            drift.append((m["name"], f["name"]))
check("compact 的 applies 与完整版逐字段一致", not drift, drift)
check("compact 不重复契约约束（生效不带原因）",
      not [f for m in c["models"] for f in m.get("fields", [])
           if f["applies"] and f.get("inactive_reason")])

print("\n=== [8] 文档里的工具清单不漂移 ===")
# 工具数是手写进 README / API.md 的。历史上漏改过两次（加 get_skills、加 get_tool_info），
# 当时没有测试守 —— 改代码忘改文档，或文档写了不存在的工具，都只能靠人眼发现。
# 这里把它变成机械可查：以 `toolinfo._endpoints()["mcp"]`（已与 @mcp.tool 注册集断言相等）
# 为唯一真源，核对三处文档表述。
TOOLS = set(PAYLOAD["endpoints"]["mcp"])

_readme = (ROOT / "README.md").read_text(encoding="utf-8")
_api = (ROOT / "API.md").read_text(encoding="utf-8")

_line = next((ln for ln in _readme.splitlines() if "MCP 服务（" in ln), "")
check("README 能找到 MCP 工具清单行", bool(_line), _line[:60])
_m = re.search(r"MCP 服务（(\d+) 个工具）", _line)
check("README 工具计数与实际一致",
      bool(_m) and int(_m.group(1)) == len(TOOLS),
      f"README={_m.group(1) if _m else None} 实际={len(TOOLS)}")
_doc = set(re.findall(r"`([a-z_]+)`", _line))
check("README 清单与工具集逐名一致", _doc == TOOLS,
      f"缺 {sorted(TOOLS - _doc)} / 多 {sorted(_doc - TOOLS)}")

check("API.md 架构图的工具计数与实际一致",
      f"= MCP Server ({len(TOOLS)} tools)" in _api, len(TOOLS))
check("API.md §7 标题的工具计数与实际一致",
      f"## 7. MCP 网关（{len(TOOLS)} 个工具）" in _api, len(TOOLS))

_missing = [t for t in sorted(TOOLS) if f"| `{t}` |" not in _api]
check("API.md 每个工具都有表格行", not _missing, _missing)

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
