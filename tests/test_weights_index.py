"""weights.yaml 与 README／工作流的一致性守护。

`weights.yaml` 是「权重文件 → 下载来源」的唯一真源，网关据此在「权重未安装」的报错里
给出下载命令、在体检端点里列缺失。它有三处必须同步的东西，全靠本测试兜住：

  1. `workflows/*.json` 实际引用的权重 —— 少一条，那道工作流缺文件时就给不出下载指引；
  2. `README.md` 的两张权重表 —— 那是人读的一份，漂移了就会把使用者指向错的仓库/路径
     （历史上就有过：Qwen 三条的 repo 内路径在表里写成「根目录」，实际在子目录下）；
  3. `referenced` 标记 —— 标错会让体检把「根本没人用」的权重当必装项报缺失。

    python tests/test_weights_index.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WEIGHTS_YAML = ROOT / "weights.yaml"
README = ROOT / "README.md"
WORKFLOWS = ROOT / "workflows"

# 工作流 JSON 里引用的权重文件名（不含路径分隔符）
REF_RE = re.compile(r'"([^"\\/]+\.safetensors)"')
# README 权重表行：| `file` | `dir/` | 1.23 GB | `repo` | 路径列 |
ROW_RE = re.compile(
    r"^\|\s*`([^`]+\.safetensors)`\s*\|\s*`([^`]+)/`\s*\|\s*([\d.]+) GB\s*\|\s*"
    r"`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*$"
)

passed = failed = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


# ------------------------------------------------------------------ 载入
doc = yaml.safe_load(WEIGHTS_YAML.read_text(encoding="utf-8")) or {}
entries = {e["file"]: e for e in (doc.get("files") or []) if e.get("file")}
rename = doc.get("rename") or {}

refs: dict[str, list[str]] = {}
for p in sorted(WORKFLOWS.glob("*.json")):
    if p.name.startswith("example"):
        continue
    for name in sorted(set(REF_RE.findall(p.read_text(encoding="utf-8")))):
        refs.setdefault(name, []).append(p.stem)

readme_text = README.read_text(encoding="utf-8")
rows: dict[str, tuple[str, str, str, str]] = {}  # file -> (dir, size, repo, 路径列)
section = "图像档"
for line in readme_text.splitlines():
    if line.startswith("### 图像档"):
        section = "图像档"
    elif line.startswith("### 视频档"):
        section = "视频档"
    m = ROW_RE.match(line)
    if m:
        rows[m.group(1)] = (m.group(2), m.group(3), m.group(4), m.group(5))

print("=== [1] 索引自身 ===")
check("weights.yaml 有条目", bool(entries), len(entries))
dups = len(doc.get("files") or []) - len(entries)
check("无重复 file 条目", dups == 0, f"重复 {dups}")
for name, e in entries.items():
    check(f"{name} 有 dir/repo/path", bool(e.get("dir") and e.get("repo") and e.get("path")), e)

print("\n=== [2] 工作流实际引用 ⊆ 索引 ===")
unknown = sorted(set(refs) - set(entries))
check("引用全部能在索引里查到", not unknown, f"缺 {unknown}")
n_ref_true = sum(1 for e in entries.values() if e.get("referenced", True))
check("referenced=true 的条数 == 实际引用数", n_ref_true == len(refs), f"{n_ref_true} vs {len(refs)}")

print("\n=== [3] referenced 标记与事实一致 ===")
for name, e in entries.items():
    marked = e.get("referenced", True)
    used = name in refs
    check(f"{name} 标记{'被引用' if marked else '未被引用'}与事实一致", marked == used,
          "标记与 workflows/*.json 不符 —— 改了工作流引用就要同步这个标记")

print("\n=== [4] 改名表与实际相符 ===")
needs_rename = {
    Path(e["path"]).name: name for name, e in entries.items() if Path(e["path"]).name != name
}
check("需要改名的条目都在 rename 段里", set(needs_rename) <= set(rename), needs_rename)
check("rename 段没有多余条目", set(rename) <= set(needs_rename), set(rename) - set(needs_rename))
for src, dst in needs_rename.items():
    check(f"rename 映射正确 {src} -> {dst}", rename.get(src) == dst, rename.get(src))

print("\n=== [5] README 两张表与索引一致 ===")
check("README 表行数 == 索引条目数", len(rows) == len(entries), f"表 {len(rows)} / 索引 {len(entries)}")
check("README 表文件集合 == 索引文件集合", set(rows) == set(entries),
      f"仅表有 {sorted(set(rows) - set(entries))}；仅索引有 {sorted(set(entries) - set(rows))}")
for name, e in sorted(entries.items()):
    row = rows.get(name)
    if not row:
        continue
    rdir, rsize, rrepo, rpath = row
    check(f"{name} 目标目录一致", rdir == e["dir"], f"表 {rdir} / 索引 {e['dir']}")
    check(f"{name} 仓库一致", rrepo == e["repo"], f"表 {rrepo} / 索引 {e['repo']}")
    check(f"{name} 体积一致", abs(float(rsize) - float(e["size_gb"])) < 0.005,
          f"表 {rsize} / 索引 {e['size_gb']}")
    # 剥掉反引号与括注（如「（需改名）」），只留 repo 内路径本身
    rpath_clean = re.sub(r"[（(].*?[）)]", "", rpath.replace("`", "")).strip()
    if "根目录" in rpath_clean:
        check(f"{name} 表写「根目录」则路径无子目录", "/" not in e["path"],
              f"表=根目录 / 索引 path={e['path']}")
    else:
        check(f"{name} repo 内路径前缀一致", e["path"].startswith(rpath_clean.strip("/")),
              f"表 {rpath_clean} / 索引 {e['path']}")

print(f"\n===== {passed} passed / {failed} failed =====")
sys.exit(1 if failed else 0)
