"""统一版本统筹入口 —— 一个事实一处维护，其余全部派生。

为什么需要这个工具：
    版本事实此前散在多处、两两手工对齐，靠护栏测试事后兜底：
      * 服务版本：`pyproject.toml` + `API.md` 头部（test_version.py 对拍）；
      * skill 版本：`gateway/skills.py` SKILLS 表 + 各 skill 仓库 SKILL.md frontmatter
        （test_skill_version_sync.py 对拍）。
    护栏能抓住漂移，但同步动作本身靠人记得 —— 实际发生过「改了 SKILL.md 忘了 skills.py」。
    本工具把同步变成**一条命令**：bump 时自动写两处版本号、重生成 skill 版本 manifest、
    跑全套测试；测试语义从「对拍两份手写值」变为「manifest 是否过期」。

单一事实源约定（改版本只许改这里，别处都是生成物）：
      * 服务版本  = `pyproject.toml` 的 `[project] version`
      * skill 版本 = 各 skill 仓库 SKILL.md frontmatter 的 `skill_version`

用法（用带 aiohttp/yaml 的解释器，即 tests 用的那个 `<ComfyUI python>`）：
      python tools/release.py bump 1.28.0   # 发版：写版本号 + gen-manifest + 全套测试
      python tools/release.py gen-manifest  # 只重生成 gateway/skills_manifest.json（改了 skill 内容后）
      python tools/release.py check         # 体检：manifest 是否过期、pyproject 与 API.md 是否一致
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

NODE = Path(__file__).resolve().parent.parent          # .../custom_nodes/ComfyUI-Roundabout
sys.path.insert(0, str(NODE))

from gateway.skills import SKILLS  # noqa: E402

PYPROJECT = NODE / "pyproject.toml"
API_MD = NODE / "API.md"
MANIFEST = NODE / "gateway" / "skills_manifest.json"

SEMVER = re.compile(r"\d+\.\d+\.\d+")


def _frontmatter_version(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^---\s*\n(.*?)\n---\s*$", text, re.S | re.M)
    if not m:
        return None
    v = re.search(r"^skill_version:\s*(\S+)\s*$", m.group(1), re.M)
    return v.group(1) if v else None


def local_skill_version(name: str) -> tuple[Path, str] | tuple[None, None]:
    """找本机已装 skill 副本的 frontmatter 版本（用户级 / 项目级 / SKILLS_DIR 覆盖）。"""
    candidates = [
        Path.home() / ".workbuddy" / "skills" / name,
        NODE.parent.parent / ".workbuddy" / "skills" / name,
    ]
    override = os.environ.get("SKILLS_DIR")
    if override:
        candidates.insert(0, Path(override) / name)
    for c in candidates:
        p = c / "SKILL.md"
        v = _frontmatter_version(p)
        if v is not None:
            return p, v
    return None, None


def collect_versions() -> dict[str, str]:
    """从本机 skill 副本 frontmatter 收集全部自维护 skill 的版本（缺任一副本即失败）。"""
    versions: dict[str, str] = {}
    for entry in SKILLS:
        if entry.get("source") != "roundabout":
            continue
        name = entry["name"]
        path, v = local_skill_version(name)
        if v is None:
            sys.exit(f"[release] 本机找不到 skill `{name}` 的已装副本（~/.workbuddy/skills/{name}/SKILL.md），"
                     f"无法生成 manifest —— 先装副本或设 SKILLS_DIR")
        versions[name] = v
    return versions


def gen_manifest() -> None:
    versions = collect_versions()
    payload = {
        "_comment": "生成物（tools/release.py gen-manifest）：skill 版本源自各 SKILL.md frontmatter，"
                    "手改本文件无效、会被下次生成覆盖。gateway/skills.py 运行期读取本文件。",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skills": versions,
    }
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[gen-manifest] {MANIFEST.name}: {versions}")


def _replace_once(path: Path, pattern: str, repl: str, label: str) -> None:
    s = path.read_text(encoding="utf-8")
    matches = re.findall(pattern, s, re.M)
    if len(matches) != 1:
        sys.exit(f"[bump] {label} 锚点不唯一（{len(matches)} 处），中止：{pattern}")
    path.write_text(re.sub(pattern, repl, s, count=1, flags=re.M), encoding="utf-8", newline="\n")
    print(f"[bump] {path.name} {label}: {matches[0]} -> {repl}")


def bump(new_version: str) -> None:
    if not SEMVER.fullmatch(new_version):
        sys.exit(f"[bump] 版本号必须是 X.Y.Z，收到：{new_version!r}")
    _replace_once(PYPROJECT, r'(?m)^version = "' + SEMVER.pattern + r'"',
                  f'version = "{new_version}"', "pyproject version")
    _replace_once(API_MD, r'(?m)^> 当前版本：`' + SEMVER.pattern + r'`',
                  f'> 当前版本：`{new_version}`', "API.md 当前版本")
    gen_manifest()
    print(f"[bump] 跑全套测试 …")
    r = subprocess.run([sys.executable, str(NODE / "tests" / "run_tests.py")], cwd=str(NODE))
    if r.returncode != 0:
        sys.exit(f"[bump] 测试未全绿 —— 版本号已写入但**不要提交**，先修测试"
                 f"（可 git diff 查看改动 / git checkout 撤销）")
    print(f"[bump] {new_version} 完成：记得重启 ComfyUI（VERSION 由 pyproject 派生，reload 不算），"
          f"然后同一批提交：pyproject.toml / API.md / gateway/skills_manifest.json / SKILL.md 等")


def check() -> None:
    bad = 0
    # 1) manifest 是否过期（与本机 frontmatter 比）
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in SKILLS:
        if entry.get("source") != "roundabout":
            continue
        name = entry["name"]
        path, v = local_skill_version(name)
        if v is None:
            print(f"  [SKIP] {name} 本机无副本")
            continue
        got = data.get("skills", {}).get(name)
        ok = got == v
        print(f"  [{'PASS' if ok else 'FAIL'}] manifest.{name} == frontmatter（{got} vs {v}）")
        bad += 0 if ok else 1
    # 2) SKILLS 表里不该再有手写版本号（防回潮）
    src = (NODE / "gateway" / "skills.py").read_text(encoding="utf-8")
    leaked = re.findall(r'"skill_version":\s*"\d+\.\d+\.\d+"', src)
    ok = not leaked
    print(f"  [{'PASS' if ok else 'FAIL'}] skills.py 无手写 skill_version（回潮 {len(leaked)} 处）")
    bad += 0 if ok else 1
    # 3) pyproject 与 API.md 一致
    py = re.search(r'(?m)^version = "(\d+\.\d+\.\d+)"', PYPROJECT.read_text(encoding="utf-8")).group(1)
    api = re.search(r"(?m)^> 当前版本：`(\d+\.\d+\.\d+)`", API_MD.read_text(encoding="utf-8")).group(1)
    ok = py == api
    print(f"  [{'PASS' if ok else 'FAIL'}] pyproject({py}) == API.md({api})")
    bad += 0 if ok else 1
    print(f"\n {'ALL CHECKS PASSED' if not bad else str(bad) + ' CHECK(S) FAILED'}")
    sys.exit(1 if bad else 0)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "bump" and len(sys.argv) == 3:
        bump(sys.argv[2])
    elif cmd == "gen-manifest":
        gen_manifest()
    elif cmd == "check":
        check()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
