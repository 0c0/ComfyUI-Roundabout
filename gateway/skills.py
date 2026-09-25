"""配套 agent-skill 清单（get_skills 的唯一数据源）。

为什么独立成模块：
    skill 清单以前直接写在 `mcp_server.py` 里，而每个条目的 `skill_version` 又必须与
    对应 skill 仓库 SKILL.md frontmatter 的 `skill_version` 一致 —— 一个事实两处手工
    维护，必然漂移；漂移本身还是**静默**的（没有任何地方报错，agent 只是拿到一个过期
    的版本号，据此判断「本地副本不必更新」）。抽到这里之后：
      * 升 skill 只需要改这一处（`SKILLS` 里的 `skill_version`）；
      * `tests/test_skill_version_sync.py` 会把这里的值与本机已装副本的 frontmatter
        对拍，忘了同步就让测试红。

    `mcp_server.py` 太重（拉 ComfyClient / registry / MCPServer），独立成模块也让测试
    可以只 import 这份数据。
"""

from __future__ import annotations

from typing import Any

# skill_version 的口径：**该 skill 当前应有的内容版本**，与 skill 仓库 SKILL.md
# frontmatter 的 skill_version 同义。本地已装副本的 frontmatter 低于此值 = 副本过期，
# 按 install_url 重装即可。官方（source=official）条目版本不可控，恒为 None。
SKILLS: list[dict[str, Any]] = [
    {
        "name": "roundabout",
        "install_url": "https://github.com/0c0/roundabout-skill",
        "purpose": "本 MCP 服务器的总入口用法：模型选择、请求参数、排障、注册/下线工作流。",
        "when_to_use": "拿不准用哪个工具/参数、要注册或排查工作流、或想看某模型能力边界时查它。",
        "source": "roundabout",
        "published": True,
        "skill_version": "1.8.0",
    },
    {
        "name": "h3-playbook",
        "install_url": "https://github.com/0c0/h3-playbook-skill",
        "purpose": "MiniMax H3 单一入口（官方使用手册口径 + 官方 h3-prompt-writing 契约合并版）："
                   "三段式公式、五类模式判定（T2VA/I2VA/FL2VA/L2VA/Ref2VA）、Ref2VA 六段改写格式、"
                   "素材用途标签、时长/分辨率/宽高比/输入上限、踩坑表。",
        "when_to_use": "写 MiniMax H3 / FastH3 视频提示词前必查（含字段级结构与 Ref2VA 六段格式），"
                       "或判断某个需求 H3 能不能做、排查口型/切镜/乱码问题时查它。",
        "source": "roundabout",
        "published": True,
        "skill_version": "2.0.0",
    },
    {
        "name": "qwen-image-prompt-writing",
        "install_url": "https://github.com/0c0/qwen-image2.1-prompt-writing-skill",
        "purpose": "Qwen-Image-2.1 官方 Prompt Enhancer 契约的手写替身：t2i 观察者报告与 edit 改写指令，"
                   "产出 rewritten_prompt + wh_ratio / ratio_follow 结构。",
        "when_to_use": "用网关 qwen-image-2.1 档出图/改图，或要把一句粗糙需求扩写成该模型能吃的描述前必查。",
        "source": "roundabout",
        "published": True,
        "skill_version": "1.0.0",
    },
]

_NOTE = (
    "source 区分两类：roundabout = 本网关维护，official = 模型厂商自己维护（内容以其仓库为准）。"
    "skill_version = 该 skill 当前应有的内容版本，与 skill 仓库 SKILL.md frontmatter 的 "
    "skill_version 同步维护；本地已装副本的 frontmatter 版本若低于此值，说明副本已过期，"
    "重新安装（install_url）即可拿到新版。official 条目版本不可控，恒为 null。"
    "其余专精 skill 不随本仓库发布，不在此列出。安装方式：把 install_url 交给 agent 的 skill "
    "安装流程（裸 URL 即可，无需指定目录）。"
)


def build_skills_payload(server_version: str) -> dict[str, Any]:
    """组装 get_skills 的返回体（server / version / skills[] / note）。"""
    return {
        "server": "comfyui-roundabout",
        "version": server_version,
        "skills": SKILLS,
        "note": _NOTE,
    }
