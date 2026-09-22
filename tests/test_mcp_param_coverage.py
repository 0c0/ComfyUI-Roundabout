"""MCP 工具参数覆盖度守护测试。

背景：网关有两条入参路径 —— REST 走 `gateway/schemas.py` 的 pydantic 模型，
MCP 走 `mcp_server.py` 里**逐个手写的函数签名**。没有任何东西保证二者对齐，
于是发生过这种事：`filename_prefix`（落盘前缀）在 REST 端早就透传了，
MCP 的 `generate_image` / `generate_video` 却一直没暴露 —— 更糟的是 MCP SDK 的
参数模型是 pydantic 默认 `extra="ignore"`，**客户端传了不报错、直接丢弃**，
表现成「参数看起来支持，实际毫无作用」，很难在实测里定位。

本测试把「当前有意不暴露的字段」快照成清单：
- REST 新增字段而 MCP 漏了 → 立刻红（提醒同步）
- 顺手把字段补进 MCP → 不红（清单是子集语义，无需维护）
- 加了形参却忘了传给请求构造 → 立刻红（这是本 bug 的原始形态）

    python tests/test_mcp_param_coverage.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.schemas import ImageGenerationRequest, VideoGenerationRequest  # noqa: E402

MCP_SRC = ROOT / "mcp_server.py"
REQUEST_MODELS = (ImageGenerationRequest, VideoGenerationRequest)

# MCP SDK 按注解注入、不属于请求字段的形参
SDK_PARAMS = {"ctx"}

# 工具 → 它最终构造的请求模型
TOOL_MODEL = {
    "generate_image": ImageGenerationRequest,
    "edit_image": ImageGenerationRequest,
    "remove_background": ImageGenerationRequest,
    "generate_video": VideoGenerationRequest,
}

# 必须暴露的字段（少了就是回归）
REQUIRED = {
    "generate_image": {"prompt", "model", "size", "seed", "steps",
                       "negative_prompt", "workflow_overrides", "filename_prefix"},
    "edit_image": {"prompt", "image", "model", "filename_prefix"},
    "remove_background": {"image", "filename_prefix"},
    "generate_video": {"prompt", "model", "duration", "fps", "size", "seed",
                       "negative_prompt", "reference_images", "reference_videos",
                       "reference_audios", "background", "filename_prefix", "attention",
                       "scale"},
}

# 有意不暴露的快照（子集语义：missing 必须是它的子集）
# 维护方式：只在这里**补/加**字段，不要为了让它变短而删 —— 空集合永远合法。
ALLOWED_MISSING = {
    # 省略标准占位参数 + 采样器类精调（MCP 面向对话式调用，保持 schema 精简）
    "generate_image": {"style", "user", "background", "output_format", "moderation",
                       "sampler_name", "scheduler", "denoise"},
    "edit_image": {"size", "style", "user", "background", "output_format",
                   "moderation", "steps", "cfg", "sampler_name", "scheduler",
                   "denoise", "mode"},
    # 去背景是 promptless 工具，只留最小面
    "remove_background": {"prompt", "model", "n", "size", "style", "user",
                          "background", "output_format", "moderation", "negative_prompt",
                          "seed", "steps", "cfg", "sampler_name", "scheduler",
                          # 去背景是 promptless 单图工具，多图参考对它没有意义
                          "reference_images",
                          "denoise", "mode", "mask", "workflow_overrides"},
    "generate_video": {"n", "style", "user", "num_frames", "cfg",
                       "sampler_name", "scheduler", "denoise", "image",
                       "workflow_overrides", "async_mode"},
}

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
#  解析 mcp_server.py：取每个 @mcp.tool 函数的形参 / 默认值 / 请求构造实参
# ============================================================================
def collect_tools(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tools: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = any(
            isinstance((d.func if isinstance(d, ast.Call) else d), ast.Attribute)
            and (d.func if isinstance(d, ast.Call) else d).attr == "tool"
            for d in node.decorator_list
        )
        if not is_tool:
            continue
        name = node.name
        for d in node.decorator_list:
            if isinstance(d, ast.Call):
                for kw in d.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        name = kw.value.value
        tools[name] = node
    return tools


def param_names(fn: ast.FunctionDef) -> list[str]:
    a = fn.args
    return [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def param_defaults(fn: ast.FunctionDef) -> dict[str, ast.expr]:
    """形参 → 默认值 AST（无默认值的不收录）。"""
    a = fn.args
    out: dict[str, ast.expr] = {}
    pos = [*a.posonlyargs, *a.args]
    for p, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
        out[p.arg] = d
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        if d is not None:
            out[p.arg] = d
    return out


def request_kwargs(fn: ast.FunctionDef) -> set[str]:
    """函数体里凡是构造「请求模型」的调用，收集其关键字实参名。"""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {m.__name__ for m in REQUEST_MODELS}:
                names |= {kw.arg for kw in node.keywords if kw.arg}
    return names


# ============================================================================
tools = collect_tools(MCP_SRC)

print("=== [1] 工具存在性 ===")
for tool in TOOL_MODEL:
    check(f"{tool} 已注册", tool in tools)
check("MCP 工具总数 >= 4（生成类）", len(tools) >= 4, f"实际 {len(tools)}: {sorted(tools)}")

print("\n=== [2] 必备字段在签名里 ===")
for tool, required in REQUIRED.items():
    if tool not in tools:
        check(f"{tool} 签名检查", False, "工具缺失")
        continue
    actual = set(param_names(tools[tool]))
    missing = sorted(required - actual)
    check(f"{tool} 必备字段齐全", not missing, f"缺 {missing}")

print("\n=== [3] 形参名不得拼错（必须都是请求模型的字段）===")
for tool, model in TOOL_MODEL.items():
    if tool not in tools:
        continue
    fields = set(model.model_fields)
    stray = sorted(set(param_names(tools[tool])) - SDK_PARAMS - fields)
    check(f"{tool} 无越界形参", not stray, f"不在 {model.__name__} 里: {stray}")

print("\n=== [4] 没有预期外的遗漏（REST 有、MCP 没有）===")
for tool, model in TOOL_MODEL.items():
    if tool not in tools:
        continue
    missing = set(model.model_fields) - set(param_names(tools[tool])) - SDK_PARAMS
    unexpected = sorted(missing - ALLOWED_MISSING[tool])
    check(f"{tool} 无新增遗漏", not unexpected,
          f"新增未暴露字段 {unexpected} —— 要么补进签名，要么加进 ALLOWED_MISSING 快照")

print("\n=== [5] 透传闭环：形参必须真的传进请求构造 ===")
for tool in TOOL_MODEL:
    if tool not in tools:
        continue
    fn = tools[tool]
    params = set(param_names(fn)) - SDK_PARAMS
    wired = request_kwargs(fn)
    dangling = sorted(params - wired)
    check(f"{tool} 形参全部落进请求", not dangling,
          f"收了参数却没传下去: {dangling}（本 bug 的原始形态）")

print("\n=== [6] filename_prefix 的默认值必须可省略 ===")
for tool in TOOL_MODEL:
    if tool not in tools:
        continue
    d = param_defaults(tools[tool]).get("filename_prefix")
    ok = isinstance(d, ast.Constant) and d.value in ("", None)
    check(f"{tool}.filename_prefix 默认可省略", ok,
          f"默认值={ast.dump(d) if d is not None else '(无默认值 → 变成必填)'}")

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
