"""冒烟连通性测试统一入口。

仓库只留连通性冒烟：每支都是独立 aiohttp 实例（临时端口）或纯逻辑断言，
不需要正在跑的 ComfyUI、不占 GPU、不落产物。两支轻量守卫例外地留在集里——
`test_admin_structured_merge.py` 锁 /admin/models/structured PUT 的合并语义、
`test_weights_index.py` 锁 weights.yaml ↔ README ↔ workflows 的一致性，
都是历史上真踩过坑、靠机械断言兜底的规则。

    python tests/run_tests.py                 # 全部用例
    python tests/run_tests.py --list          # 只列不跑
    python tests/run_tests.py test_port_map.py    # 指定文件
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent          # 本文件所在（tests/）
ROOT = TESTS.parent                              # 节点目录，子进程在此 cwd 下跑


def collect(explicit: list[str]) -> list[Path]:
    if explicit:
        targets: list[Path] = []
        for item in explicit:
            p = Path(item)
            if not p.is_absolute():
                # 先按 tests/ 下的文件名找，找不到再退回节点目录
                p = (TESTS / item) if (TESTS / item).exists() else (ROOT / item)
            if not p.exists():
                raise SystemExit(f"找不到测试文件：{item}")
            targets.append(p)
        return targets
    return sorted(TESTS.glob("test_*.py"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Roundabout 冒烟连通性测试入口")
    ap.add_argument("files", nargs="*", help="只跑指定文件（默认跑全部）")
    ap.add_argument("--list", action="store_true", help="只列出将执行的文件")
    args = ap.parse_args()

    run = collect(args.files)
    if args.list:
        for p in run:
            print(f"RUN   {p.name}")
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

    print(f"\n===== {len(passed)} passed / {len(failed)} failed =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
