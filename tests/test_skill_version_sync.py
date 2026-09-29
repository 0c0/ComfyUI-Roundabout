"""get_skills 的 skill_version 必须与 skill 仓库同步 —— 一个事实不许两处维护。

为什么需要这个测试：
    每个 skill 条目的 `skill_version` 是给 agent 判「本地副本过没过期」用的，它必须与对应
    skill 仓库 SKILL.md frontmatter 的 `skill_version` 一致。以前这份清单连同版本号一起写
    死在 `mcp_server.py` 里，与 skill 仓库各改各的 —— 于是 roundabout skill 的内容已经到
    1.2.0，get_skills 还报 1.0.0，而漂移是**静默**的：不报错、不改结构，agent 只是据此得出
    「你的副本不必更新」的错误结论。

    修法（v2，现行）：版本号不再手写 —— `tools/release.py gen-manifest` 扫本机 skill 副本
    frontmatter 生成 `gateway/skills_manifest.json`，`gateway/skills.py` 运行期读取注入。
    本测试拦三件事：manifest 过期（frontmatter 变了没重新生成）、skills.py 手写版本号回潮、
    mcp_server 内联清单回潮。忘了跑 gen-manifest ⇒ 这里变红。

不碰正在运行的 ComfyUI，不联网：纯文件 / AST 断言。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/tests/test_skill_version_sync.py
NODE = Path(__file__).resolve().parent.parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gateway.skills import SKILLS, build_skills_payload  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
    if not ok:
        failures += 1


def local_skill_dirs(name: str) -> list[Path]:
    """本机可能装副本的位置（用户级 / 项目级，外加 SKILLS_DIR 覆盖）。"""
    candidates = [
        Path.home() / ".workbuddy" / "skills" / name,
        ROOT / ".workbuddy" / "skills" / name,
    ]
    override = os.environ.get("SKILLS_DIR")
    if override:
        candidates.insert(0, Path(override) / name)
    return [c / "SKILL.md" for c in candidates]


def frontmatter_version(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^---\s*\n(.*?)\n---\s*$", text, re.S | re.M)
    if not m:
        return None
    v = re.search(r"^skill_version:\s*(\S+)\s*$", m.group(1), re.M)
    return v.group(1) if v else None


# 1. 结构：每个条目该有的字段都在，且取值合法
required = {"name", "install_url", "purpose", "when_to_use", "source", "published", "skill_version"}
for entry in SKILLS:
    n = entry.get("name", "<unnamed>")
    check(f"{n} 字段齐全", required.issubset(entry.keys()), str(sorted(set(entry.keys()))))
    check(f"{n} install_url 是 github 链接", str(entry.get("install_url", "")).startswith("https://github.com/"),
          str(entry.get("install_url")))
    sv = entry.get("skill_version")
    if entry.get("source") == "official":
        check(f"{n} official 条目版本恒为 None", sv is None, repr(sv))
    else:
        check(f"{n} 自维护条目必须有版本号", isinstance(sv, str) and re.fullmatch(r"\d+\.\d+\.\d+", sv) is not None,
              repr(sv))

names = [e["name"] for e in SKILLS]
check("skill 名不重复", len(names) == len(set(names)), str(names))

# 2. 版本号 vs 本机已装副本：两者必须一致（任一处先改了，另一处必须跟上）
matched = 0
for entry in SKILLS:
    if entry.get("source") != "roundabout":
        continue
    name = entry["name"]
    declared = entry.get("skill_version")
    found = [(p, frontmatter_version(p)) for p in local_skill_dirs(name)]
    found = [(p, v) for p, v in found if v is not None]
    if not found:
        print(f"  [SKIP] {name} 本机没装副本，跳过对拍")
        continue
    path, local = found[0]
    matched += 1
    check(f"{name} 声明版本 == 本地副本 frontmatter（{declared} vs {local}）",
          declared == local,
          f"declared={declared} local={local} —— manifest 过期：跑 "
          f"`python tools/release.py gen-manifest` 重新生成（源 = SKILL.md frontmatter）；副本来自 {path}")
check("至少一个自维护 skill 完成对拍（否则这条守不住）", matched >= 1, f"matched={matched}")

# 2b. manifest 生成物在位 + skills.py 无手写版本号（防回潮）
mf_path = NODE / "gateway" / "skills_manifest.json"
try:
    mf = json.loads(mf_path.read_text(encoding="utf-8"))
    mf_ok, mf_err = True, ""
except (OSError, ValueError) as e:
    mf_ok, mf_err, mf = False, str(e), {}
check("gateway/skills_manifest.json 存在且为合法 JSON", mf_ok, mf_err)
if mf_ok:
    need = {e["name"] for e in SKILLS if e.get("source") == "roundabout"}
    check("manifest 覆盖全部自维护 skill", need <= set(mf.get("skills", {})),
          f"缺 {sorted(need - set(mf.get('skills', {})))}")
    check("manifest 有生成标记（_comment）", bool(mf.get("_comment")))
leaked = re.findall(r'"skill_version":\s*"\d+\.\d+\.\d+"',
                    (NODE / "gateway" / "skills.py").read_text(encoding="utf-8"))
check("skills.py 无手写 skill_version（回潮 0 处）", not leaked, str(leaked))

# 3. mcp_server 只许引用共用数据源，不许再内联一份清单/版本号
src = (NODE / "mcp_server.py").read_text(encoding="utf-8", errors="replace")
check("mcp_server 不再内联 skill_version", '"skill_version"' not in src)
check("mcp_server 走 build_skills_payload", "build_skills_payload(VERSION)" in src)

# 4. payload 组装不丢字段
payload = build_skills_payload("9.9.9")
check("payload 结构完整",
      payload["server"] == "comfyui-roundabout" and payload["version"] == "9.9.9"
      and isinstance(payload["skills"], list) and payload["skills"] and payload["note"],
      str(payload)[:120])

print(f"\n {'=' * 46}\n  {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
raise SystemExit(1 if failures else 0)
