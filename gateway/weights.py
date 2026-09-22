"""权重索引：文件名 → 下载来源，以及「当前缺哪些权重」的体检。

`weights.yaml`（仓库根）是唯一真源 —— 工作流 JSON 里只有文件名，光看它推不出
下载地址，这份文件补上「repo + repo 内路径 + 目标目录」。

两处用途，**都在非生成路径上**（正常出图的耗时不受影响）：
1. 「权重未安装」的 400 报错里附上可复制的下载命令（`comfy_client` 调用）；
2. 按需体检：REST `GET /roundabout/admin/weights`、MCP `check_weights`。

刻意**不做**启动全量预扫、也不在每发生成前 preflight —— 前者对没下权重机器的日志
全是噪声，后者给每发生成加固定开销，而且重复实现 ComfyUI 自己的 combo 校验
（缺文件时 ComfyUI 的 `/prompt` 本来就会拒，见 `execution.py` 的 `value_not_in_list`）。

索引按 mtime 自动跟随 `weights.yaml` 的改动，与 models.yaml 一样不需要额外操作。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .config import BASE_DIR, settings

log = logging.getLogger("roundabout.weights")

INDEX_FILE = BASE_DIR / "weights.yaml"

# 工作流 JSON 里引用的权重文件：文件名不含路径分隔符
_SAFETENSORS_RE = re.compile(r'"([^"\\/]+\.safetensors)"')

_ENTRIES: dict[str, dict[str, Any]] = {}
_MIRRORS: dict[str, str] = {}
_RENAME: dict[str, str] = {}
_LOADED = False
_MTIME: float | None = None


def reload() -> int:
    """（重新）读盘加载索引。坏文件不抛异常，只降级为空索引 —— 它只服务报错提示。"""
    global _LOADED, _ENTRIES, _MIRRORS, _RENAME, _MTIME
    entries: dict[str, dict[str, Any]] = {}
    mirrors: dict[str, str] = {}
    rename: dict[str, str] = {}
    try:
        raw = yaml.safe_load(INDEX_FILE.read_text(encoding="utf-8")) or {}
        mirrors = {str(k): str(v).rstrip("/") for k, v in (raw.get("mirrors") or {}).items()}
        rename = {str(k): str(v) for k, v in (raw.get("rename") or {}).items()}
        for item in raw.get("files") or []:
            name = str((item or {}).get("file") or "").strip()
            if not name:
                continue
            entries[name] = {
                "file": name,
                "dir": str(item.get("dir") or "").strip("/"),
                "repo": str(item.get("repo") or "").strip("/"),
                "path": str(item.get("path") or "").strip("/"),
                "size_gb": float(item.get("size_gb") or 0.0),
                "referenced": bool(item.get("referenced", True)),
            }
    except FileNotFoundError:
        log.warning("weights.yaml not found at %s (download hints disabled)", INDEX_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("weights.yaml unreadable (%s); download hints disabled", exc)

    try:
        _MTIME = INDEX_FILE.stat().st_mtime
    except OSError:
        _MTIME = None
    _ENTRIES, _MIRRORS, _RENAME, _LOADED = entries, mirrors, rename, True
    return len(entries)


def _ensure() -> None:
    """首次访问或文件有改动时加载。stat 一次的开销只在报错/体检路径上出现。"""
    if not _LOADED:
        reload()
        return
    try:
        mtime = INDEX_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _MTIME:
        reload()


def entry(filename: str) -> dict[str, Any] | None:
    _ensure()
    return _ENTRIES.get(filename)


def rename_to(filename: str) -> str | None:
    """上游文件名 -> 工作流引用的名字（只有个别文件需要改名）。"""
    _ensure()
    return _RENAME.get(filename)


def models_root() -> str:
    """ComfyUI 的 models 根目录（绝对路径优先，拿不到时回落相对 `models`）。

    与 pipeline 取 input/output 根目录同规矩：经 ComfyUI 的 folder_paths 动态获取，
    不写死 —— 每个用户的安装路径不同。报错里给的命令用绝对路径，因为 agent 的 cwd
    未必是 ComfyUI 根目录，相对路径容易落错地方。
    """
    try:
        import folder_paths  # type: ignore

        root = str(getattr(folder_paths, "models_dir", "") or "")
        if root:
            # 统一正斜杠：报错里的命令用反斜杠 + 换行做续行，路径若再带反斜杠会被 shell
            # 当转义符吃掉（bash 下 `-o E:\ai\x` 会落成 `E:aix`）。Windows 的程序本身接受正斜杠。
            return root.replace("\\", "/")
    except Exception:  # noqa: BLE001
        pass
    return "models"


def download_url(filename: str, mirror: str = "hf") -> str | None:
    """拼出下载直链。mirror 取 `weights.yaml` 的 `mirrors` 键（默认 hf-mirror）。"""
    item = entry(filename)
    if not item or not item["repo"] or not item["path"]:
        return None
    _ensure()
    base = mirror if "://" in mirror else _MIRRORS.get(mirror, _MIRRORS.get("hf", ""))
    if not base:
        return None
    # ModelScope 用 master 分支，HF 系用 main
    branch = "master" if "modelscope" in base else "main"
    return f"{base}/{item['repo']}/resolve/{branch}/{item['path']}"


def download_command(
    filename: str, mirror: str = "hf", models_root_override: str | None = None
) -> str | None:
    """给出可直接粘贴执行的一行命令（落盘路径按 ComfyUI 目录习惯拼）。"""
    item = entry(filename)
    url = download_url(filename, mirror)
    if not item or not url:
        return None
    root = (models_root_override or models_root()).rstrip("/")
    dest = f"{root}/{item['dir']}/{item['file']}" if item["dir"] else f"{root}/{item['file']}"
    return f"curl -L -o {dest} \\\n  {url}"


def _one_hint(filename: str, *, mirror: str, models_root_override: str | None) -> str | None:
    """单个文件的下载提示（索引里没有该文件时返回 None）。"""
    item = entry(filename)
    if not item:
        return None
    root = (models_root_override or models_root()).rstrip("/")
    dest_dir = f"{root}/{item['dir']}/" if item["dir"] else f"{root}/"
    lines = [
        f"该文件未安装：{filename}",
        f"  目标目录：{dest_dir}（{item['size_gb']:.2f} GB，来自 {item['repo']}）",
    ]
    cmd = download_command(filename, mirror=mirror, models_root_override=models_root_override)
    if cmd:
        lines.append("  下载：" + cmd.replace("\n", "\n  "))
    upstream = Path(item["path"]).name
    if upstream != item["file"]:
        lines.append(f"  ⚠ 下完需改名：{upstream} → {item['file']}")
    return "\n".join(lines)


def describe_missing(
    filenames: list[str], *, mirror: str = "hf", models_root_override: str | None = None
) -> str | None:
    """把一组缺失的权重文件拼成一段「去下载」提示；一个都不认识时返回 None。

    只认索引里有的文件：LoadImage 之类的输入图、使用者自加工作流的权重都不在索引里，
    此时**不改写**原报错（宁可不提示，也不能给出错的下载地址）。
    """
    hints = [
        h
        for h in (
            _one_hint(f, mirror=mirror, models_root_override=models_root_override) for f in filenames
        )
        if h
    ]
    if not hints:
        return None
    head = "这不是参数写错 —— 是权重文件没装（ComfyUI 把本地已装的列成了候选值，缺的那个不在里面）。"
    tail = (
        "  完整清单与备用源见 README「权重清单」节；\n"
        "  要看当前全部缺失：GET /roundabout/admin/weights（或 MCP 工具 check_weights）。"
    )
    return head + "\n" + "\n".join(hints) + "\n" + tail


# --------------------------------------------------------------------- 体检
def _search_dirs(folder: str) -> list[Path]:
    """某个权重类别在磁盘上的搜索目录（经 ComfyUI 的 folder_paths，含 extra_model_paths）。

    拿不到 folder_paths（独立/远端模式）时回落 `<models_dir>/<folder>`；都没有则空。
    """
    dirs: list[Path] = []
    try:
        import folder_paths  # type: ignore

        dirs = [Path(p) for p in (folder_paths.get_folder_paths(folder) or [])]
    except Exception:  # noqa: BLE001 - KeyError（未注册的类别）/ 无 folder_paths 都走这里
        dirs = []
    if not dirs:
        try:
            import folder_paths  # type: ignore

            root = getattr(folder_paths, "models_dir", "")
            if root:
                dirs = [Path(root) / folder]
        except Exception:  # noqa: BLE001
            dirs = []
    return dirs


def _installed(filename: str, folder: str) -> bool | None:
    """True/False = 已装/未装；None = 无法判定（拿不到搜索目录）。"""
    dirs = _search_dirs(folder)
    if not dirs:
        return None
    for d in dirs:
        try:
            if (d / filename).is_file():
                return True
        except OSError:
            continue
    return False


def scan_workflow_refs(workflows_dir: Path | None = None) -> dict[str, list[str]]:
    """扫 `workflows/*.json`，返回 {权重文件名: [引用它的工作流名]}。

    `example_*` 是接入样本（用使用者自己的 checkpoint），不计入。
    """
    root = Path(workflows_dir or settings.workflows_dir)
    refs: dict[str, list[str]] = {}
    try:
        files = sorted(root.glob("*.json"))
    except OSError:
        return refs
    for p in files:
        if p.name.startswith("example"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for name in sorted(set(_SAFETENSORS_RE.findall(text))):
            refs.setdefault(name, []).append(p.stem)
    return refs


def check(
    workflows_dir: Path | None = None, *, include_unreferenced: bool = False, mirror: str = "hf"
) -> dict[str, Any]:
    """体检：工作流引用的权重里，当前缺哪些 + 每条怎么下。

    返回结构直接可序列化（REST / MCP 共用）。
    """
    _ensure()
    refs = scan_workflow_refs(workflows_dir)

    missing: list[dict[str, Any]] = []
    unknown: list[str] = []
    not_in_index: list[str] = []
    ok_count = 0

    for name, users in sorted(refs.items()):
        item = entry(name)
        if not item:
            # 使用者自加工作流的权重（不在内置索引里）—— 只记录，不给下载指引
            not_in_index.append(name)
            continue
        state = _installed(name, item["dir"] or "")
        if state is None:
            unknown.append(name)
        elif state:
            ok_count += 1
        else:
            missing.append(
                {
                    "file": name,
                    "dir": item["dir"],
                    "repo": item["repo"],
                    "path": item["path"],
                    "size_gb": item["size_gb"],
                    "used_by": users,
                    "command": download_command(name, mirror=mirror),
                    "url": download_url(name, mirror=mirror),
                    "rename_to": rename_to(Path(item["path"]).name),
                }
            )

    payload: dict[str, Any] = {
        "referenced_total": len(refs),
        "installed": ok_count,
        "missing_count": len(missing),
        "missing_size_gb": round(sum(m["size_gb"] for m in missing), 2),
        "missing": missing,
        "undetermined": unknown,
        "not_in_index": not_in_index,
        "index_total": len(_ENTRIES),
        "models_root": models_root(),
    }
    if include_unreferenced:
        payload["unreferenced"] = [
            {"file": e["file"], "dir": e["dir"], "size_gb": e["size_gb"], "repo": e["repo"]}
            for _, e in sorted(_ENTRIES.items())
            if not e["referenced"]
        ]
    return payload


def summary_line(report: dict[str, Any]) -> str:
    """一行摘要（日志与人读用）。"""
    return (
        f"weights: {report['installed']}/{report['referenced_total']} installed, "
        f"missing {report['missing_count']} (~{report['missing_size_gb']} GB)"
    )
