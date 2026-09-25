"""工具描述 / get_skills / get_tool_info / 文档 四面口径同步守卫。

为什么需要这个测试：
    网关的「同一句话」散在四个面上：MCP 工具 schema（mcp_server.py 的 Field description）、
    get_tool_info（toolinfo.py 运行期从 models.yaml 推导）、get_skills（gateway/skills.py）、
    人读文档（README / API.md / WORKFLOWS.md）。语义一变（如 v1.17.0 废除「帧与参考互斥」），
    极易出现 API.md 已写「帧与参考可同传」、MCP 工具描述还挂着「fl2va 档不收 reference_images」
    的静默漂移 —— agent 拿到的是过时口径，然后照着错。

    两层防线：
    1. **禁语清单（STALE_PHRASES）**：每次语义变更后，把被废弃的表述**逐字**登记进来
      （写明退役于哪个版本、现行口径是什么），扫描全部载体 —— 旧口径从此无法复活。
       ⚠️ 语义变更时这是**必做的收尾步骤**，与「升版本号」同级。
    2. **get_skills 跨面 parity**：SKILLS 清单里的每个 skill 必须同时出现在
       SERVER_INSTRUCTIONS（MCP 连接时唯一的自动推送入口）/ README skill 表 / API.md 工具表；
       SERVER_INSTRUCTIONS 里出现的 skill 仓库 URL 反过来也必须是 SKILLS 里登记过的 ——
       增删 skill 漏改任何一面都会红。纯静态、不联网、不碰运行中的 ComfyUI。

    版本号单源已有 tests/test_version.py 守，skill_version 对拍已有 tests/test_skill_version_sync.py
    守，这里不重复。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

NODE = Path(__file__).resolve().parent.parent

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
    if not ok:
        failures += 1


# ---------------------------------------------------------------- 1. 禁语清单
# 每条 = (被退役的逐字表述, 说明：退役于哪个版本 + 现行口径)。
# 语义变更时必须把旧表述登记到这里 —— 这是收尾步骤，不是可选项。
STALE_PHRASES: list[tuple[str, str]] = [
    ("fl2va 档不收",
     "v1.17.0 起统一节点（MiniMaxH3UnifiedToVideo）+ 槽位前缀语义：帧与参考可同传，"
     "视频档参考图一律走 reference_images（按模型槽位生效）"),
    ("帧与参考互斥",
     "v1.17.0 互斥口径废除：帧用 first_frame/last_frame，参考走 reference_*，两者可同传"),
    ("首尾帧与参考图互斥", "同上，v1.17.0 起两者可同传"),
    ("不支持参考图", "现行口径：视频档参考图按模型槽位生效（reference_images/videos/audios）"),
    ("不接受参考", "现行口径：参考素材按模型槽位生效，无槽字段才 400"),
    ("FL2VA checkpoints do not consume reference video/audio",
     "v1.20.0 起删除该括注：FL2VA 音频/视频参考消费未标定 ≠ 不消费，"
     "报错文案不应对未标定能力下结论"),
]

# 扫描载体：语义表述可能落盘的所有位置。
SCAN_TARGETS = [
    *NODE.glob("*.py"),
    *NODE.glob("*.md"),
    *NODE.glob("*.yaml"),
    *(NODE / "gateway").glob("*.py"),
    *(NODE / "tests").glob("*.py"),
    *(NODE / "workflows").glob("*.json"),
]


def test_stale_phrases() -> None:
    hits: list[str] = []
    for path in SCAN_TARGETS:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for phrase, why in STALE_PHRASES:
            if phrase in text:
                for i, line in enumerate(text.splitlines(), 1):
                    if phrase in line:
                        hits.append(f"{path.relative_to(NODE)}:{i} [{phrase}] {line.strip()[:100]}")
    # 本文件自己的 docstring / 清单定义不算命中（禁语清单必须在场才能扫别人）
    hits = [h for h in hits if not h.startswith("tests" + str(Path("\\test_info_sync.py")).replace("\\", "/"))
            and "test_info_sync.py" not in h]
    check("全载体无退役表述（STALE_PHRASES）", not hits, f"{len(hits)} 处命中：\n    " + "\n    ".join(hits))


# ------------------------------------------------- 2. get_skills 跨面 parity
sys.path.insert(0, str(NODE))
from gateway.skills import SKILLS  # noqa: E402


def test_skills_parity() -> None:
    mcp_src = (NODE / "mcp_server.py").read_text(encoding="utf-8", errors="replace")
    readme = (NODE / "README.md").read_text(encoding="utf-8", errors="replace")
    api_md = (NODE / "API.md").read_text(encoding="utf-8", errors="replace")

    for entry in SKILLS:
        name, url = entry["name"], entry["install_url"]
        check(f"skill [{name}] 在 SERVER_INSTRUCTIONS 里有推荐", name in mcp_src)
        check(f"skill [{name}] 的 install_url 在 SERVER_INSTRUCTIONS 里", url in mcp_src)
        check(f"skill [{name}] 的 install_url 在 README skill 表里", url in readme)
        check(f"skill [{name}] 在 API.md 工具表里", name in api_md)

    # 反向：SERVER_INSTRUCTIONS 推荐的 skill 仓库 URL 必须都在 SKILLS 登记过
    # （删 skill 忘了删 SERVER_INSTRUCTIONS 的推荐语 = agent 永远收到过时推荐）。
    urls_in_instructions = set(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+", mcp_src))
    registered = {e["install_url"] for e in SKILLS}
    stray = urls_in_instructions - registered
    check("SERVER_INSTRUCTIONS 的 skill URL 都在 SKILLS 清单里", not stray, str(sorted(stray)))


# --------------------------------------- 3. models.yaml 每个模型都有 description
# get_tool_info 运行期把这些 description 吐给 agent —— 空描述 = agent 只能靠猜。
def test_model_descriptions() -> None:
    data = yaml.safe_load((NODE / "models.yaml").read_text(encoding="utf-8"))
    models = data.get("models", {}) if isinstance(data, dict) else {}
    check("models.yaml 有 models 段且非空", bool(models))
    missing = [k for k, v in models.items() if not (v or {}).get("description")]
    check("每个模型都写了 description（get_tool_info 的唯一来源）", not missing, str(missing))


if __name__ == "__main__":
    test_stale_phrases()
    test_skills_parity()
    test_model_descriptions()
    print(f"\n {'=' * 46}\n  {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    raise SystemExit(1 if failures else 0)
