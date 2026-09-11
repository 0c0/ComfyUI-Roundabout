"""`MCP_PORT_MAP`（ComfyUI 端口 -> Roundabout 端口）的解析与解析优先级测试。

场景：同一台机器用 `--port` 起多个 ComfyUI 实例，每个实例内都跑着 Roundabout。
端口写死会互相抢，所以支持按实例的 ComfyUI 端口映射出各自的 Roundabout 端口：

    MCP_PORT_MAP=[8188,888],[8189,999]
    → --port 8188 的实例用 888，--port 8189 的实例用 999

优先级：显式 MCP_PORT > MCP_PORT_MAP[本实例端口] > 0（系统分配）。

纯逻辑测试，不起监听、不依赖网络。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/test_port_map.py
NODE = Path(__file__).resolve().parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
CONFIG_PY = str(NODE / "gateway" / "config.py")
for p in (str(ROOT), str(NODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

MAP_RAW = "[8188,888],[8189,999]"
EXPECTED = {8188: 888, 8189: 999}

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


_counter = [0]


def _load_config():
    """每次加载出一个全新的 config 模块。

    用递增的模块名注册进 sys.modules（dataclass 处理时需要能按 __module__ 找回
    自己的命名空间），名字互不相同，因此不会命中解释器的模块缓存。
    """
    _counter[0] += 1
    name = f"rb_config_{_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, CONFIG_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def main() -> int:
    # 先把相关环境变量钉死，挡住节点目录 .env 的覆盖，保证断言可复现
    os.environ["MCP_PORT_MAP"] = MAP_RAW
    os.environ["MCP_PORT"] = ""
    os.environ["MCP_ENABLED"] = ""

    cfg = _load_config()

    # ---- 1. 解析：分隔符随便写，含义都是「成对、左 ComfyUI 右 Roundabout」 ----
    for raw in (
        "[8188,888],[8189,999]",
        "8188:888,8189:999",
        "8188=888;8189=999",
        "8188 888\n8189 999",
        "  [8188, 888] , [8189, 999]  ",
        "8188->888, 8189->999",
    ):
        got = cfg.parse_port_map(raw)
        check(f"解析 {raw!r}", got == EXPECTED, f"got={got}")

    check("空串解析为空映射", cfg.parse_port_map("") == {}, f"got={cfg.parse_port_map('')}")
    check("落单的最后一个数字被丢弃", cfg.parse_port_map("[8188,888],[8189]") == {8188: 888},
          f"got={cfg.parse_port_map('[8188,888],[8189]')}")

    # ---- 2. 从环境变量读进 Settings ----
    check("Settings.mcp_port_map 读到映射", cfg.settings.mcp_port_map == EXPECTED,
          f"got={cfg.settings.mcp_port_map}")

    # ---- 3. 解析优先级 ----
    check("命中 8188 → 888", cfg.resolve_mcp_port(EXPECTED, 0, 8188) == 888)
    check("命中 8189 → 999", cfg.resolve_mcp_port(EXPECTED, 0, 8189) == 999)
    check("未命中的 ComfyUI 端口 → 0（系统分配）",
          cfg.resolve_mcp_port(EXPECTED, 0, 8200) == 0,
          f"got={cfg.resolve_mcp_port(EXPECTED, 0, 8200)}")
    check("显式 MCP_PORT 覆盖映射",
          cfg.resolve_mcp_port(EXPECTED, 7000, 8188) == 7000)
    check("空映射 → 0", cfg.resolve_mcp_port({}, 0, 8188) == 0)

    # ---- 4. 真按本实例的 ComfyUI 端口取值（伪造 server.args.port）----
    fake = types.ModuleType("server")

    class _PromptServer:  # 未绑定前没有 .port 属性，与真实 ComfyUI 一致
        instance = types.SimpleNamespace(app=None)

    fake.PromptServer = _PromptServer  # type: ignore[attr-defined]
    fake.args = types.SimpleNamespace(port=8189)  # type: ignore[attr-defined]
    saved = sys.modules.get("server")
    sys.modules["server"] = fake
    try:
        check("_comfy_port() 读到 --port 8189", cfg._comfy_port() == 8189,
              f"got={cfg._comfy_port()}")
        check("8189 实例解析到 999", cfg.resolve_mcp_port(EXPECTED, 0) == 999,
              f"got={cfg.resolve_mcp_port(EXPECTED, 0)}")
        check("Settings.comfy_port 属性跟随", cfg.settings.comfy_port == 8189,
              f"got={cfg.settings.comfy_port}")
        check("Settings.resolved_mcp_port 属性跟随", cfg.settings.resolved_mcp_port == 999,
              f"got={cfg.settings.resolved_mcp_port}")

        fake.args = types.SimpleNamespace(port=8188)  # type: ignore[attr-defined]
        check("8188 实例解析到 888", cfg.settings.resolved_mcp_port == 888,
              f"got={cfg.settings.resolved_mcp_port}")

        fake.args = types.SimpleNamespace(port=None)  # type: ignore[attr-defined]
        check("拿不到端口时按默认 8188 → 888", cfg.settings.resolved_mcp_port == 888,
              f"got={cfg.settings.resolved_mcp_port}")
    finally:
        if saved is not None:
            sys.modules["server"] = saved
        else:
            sys.modules.pop("server", None)

    # ---- 5. 显式 MCP_PORT 存在时优先于映射（Settings 层面）----
    os.environ["MCP_PORT"] = "7000"
    cfg2 = _load_config()
    check("MCP_PORT=7000 时映射被忽略", cfg2.settings.resolved_mcp_port == 7000,
          f"got={cfg2.settings.resolved_mcp_port}")

    print()
    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
