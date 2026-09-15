"""回归测试统一入口。

为什么需要它：仓库里有两类测试混在同一个 `test_*.py` 命名空间下 ——
一类是纯离线的逻辑/接线断言，另一类会**真的驱动 ComfyUI 出图**（占 GPU、落产物）。
`for f in test_*.py; do ...` 这种一把梭的写法会把后者一起扫进去：曾经因此在无人察觉的
情况下跑掉一张 z-image-turbo 512x512 和一次 BiRefNet 去背景。所以把那批用例写进
GPU_TESTS 名单，默认跳过；要跑就显式 `--all`。

    python run_tests.py                 # 全量离线自测（跳过 GPU 用例）
    python run_tests.py --all           # 连 GPU / 依赖实跑 ComfyUI 的用例一起
    python run_tests.py --list          # 只列不跑
    python run_tests.py test_vram_adaptive.py test_save_prefix.py   # 指定文件
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 名字里含这些片段的用例一律不打真实 ComfyUI（真出图/真去背景）
GPU_NAME_HINTS = ("e2e",)

# 补充名单：名字看不出来但同样会占 GPU 的，写这里并给出理由
GPU_TESTS: dict[str, str] = {
    "test_e2e_sync_generation.py": "真提交一次生图（z-image-turbo 512x512）",
    "test_removebg_e2e.py": "真跑 BiRefNet 去背景",
}


def is_gpu(path: Path) -> str:
    """返回跳过理由，非 GPU 用例返回空串。"""
    if path.name in GPU_TESTS:
        return GPU_TESTS[path.name]
    name = path.name.lower()
    if any(hint in name for hint in GPU_NAME_HINTS):
        return "名字含 e2e，默认按真实调用处理"
    return ""


def collect(explicit: list[str]) -> tuple[list[Path], list[tuple[Path, str]]]:
    if explicit:
        targets: list[Path] = []
        for item in explicit:
            p = Path(item)
            p = p if p.is_absolute() else ROOT / p
            if not p.exists():
                raise SystemExit(f"找不到测试文件：{item}")
            targets.append(p)
        return targets, []

    run: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for path in sorted(ROOT.glob("test_*.py")):
        reason = is_gpu(path)
        if reason:
            skipped.append((path, reason))
        else:
            run.append(path)
    return run, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="Roundabout 回归测试入口")
    ap.add_argument("files", nargs="*", help="只跑指定文件（默认跑全部离线用例）")
    ap.add_argument("--all", action="store_true", help="连会驱动真实 ComfyUI 的用例一起跑")
    ap.add_argument("--list", action="store_true", help="只列出将执行/跳过的文件")
    args = ap.parse_args()

    run, skip = collect(args.files)
    if args.all:
        run = sorted(set(run) | {p for p, _ in skip})
        skip = []

    if args.list:
        for p in run:
            print(f"RUN   {p.name}")
        for p, reason in skip:
            print(f"SKIP  {p.name}  ({reason})")
        return 0

    passed, failed = [], []
    for path in run:
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        cost = time.time() - t0
        if proc.returncode == 0:
            passed.append(path.name)
            print(f"PASS  {path.name}  ({cost:.1f}s)")
        else:
            failed.append(path.name)
            print(f"FAIL  {path.name}  (exit {proc.returncode}, {cost:.1f}s)")
            tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                    if any(k in ln for k in ("FAIL", "Error", "Traceback"))]
            for ln in tail[-8:]:
                print(f"        {ln.strip()[:160]}")

    for path, reason in skip:
        print(f"SKIP  {path.name}  ({reason})")

    print(f"\n===== {len(passed)} passed / {len(failed)} failed"
          f"{f' / {len(skip)} skipped' if skip else ''} =====")
    if skip and not args.all:
        print("跳过的用例会真的跑 ComfyUI；确认要跑时加 --all。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
