"""wait() 不再用「耗时」放弃任务，健康与否完全由 ComfyUI 状态判定。

背景：视频渲染等长任务常跑过旧的 2100s 上限（模型 timeout 1800 + 宽限 300），
被 `JobTimeout` 误杀。改造后 `wait(timeout=0)`（或 `JOB_TIMEOUT=0`）无限等待，
只要 ComfyUI 队列里还查得到该任务（running/pending）就一直等到出结果；
只有「连续 3 轮无 history 且不在队列」才判定任务真的消失（JobCancelled）。
`timeout > 0` 仍保留向后兼容的安全上限。

验证点：
  [1] 无限等待 + 任务一直在队列跑 → 不抛 JobTimeout（持续轮询，靠外层超时证明还活着）
  [2] 无限等待 + 任务若干轮后完成 → 返回 history 条目
  [3] 无限等待 + 任务消失（不在队列且无 history）→ 抛 JobCancelled
  [4] legacy timeout>0 + 一直在队列跑 → 仍按 grace 上限抛 JobTimeout（开关关闭时行为不变）
  [5] legacy timeout>0 + 任务完成 → 返回 history 条目

自测：python test_wait_no_time_limit.py
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = "ComfyUI_Roundabout"

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


def _load_package() -> types.ModuleType:
    if PKG in sys.modules:
        return sys.modules[PKG]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(HERE)]
    sys.modules[PKG] = pkg
    return pkg


_pkg = _load_package()
gw = importlib.import_module(PKG + ".gateway")
cc = importlib.import_module(PKG + ".gateway.comfy_client")
errs = importlib.import_module(PKG + ".gateway.errors")
ComfyClient = cc.ComfyClient
JobTimeout = cc.JobTimeout
JobCancelled = errs.JobCancelled
UpstreamError = errs.UpstreamError


class FakeClient(ComfyClient):
    """用可控序列驱动 history / is_queued，不碰真实 ComfyUI。"""

    def __init__(self, *, history_seq=None, queued_seq=None):
        super().__init__("http://fake-comfy")
        # history 序列：每次调用消费一个元素；耗尽后返回 None（视为未完成）
        self._history = list(history_seq or [])
        # queued 序列：每次调用消费一个；耗尽后默认返回 True（仍在队列生成中）
        self._queued = list(queued_seq if queued_seq is not None else [])
        self.history_calls = 0
        self.queued_calls = 0
        self.interrupts = 0

    async def history(self, prompt_id):
        self.history_calls += 1
        if self._history:
            return self._history.pop(0)
        return None

    async def is_queued(self, prompt_id):
        self.queued_calls += 1
        if self._queued:
            return self._queued.pop(0)
        return True

    async def interrupt(self, *, silent=False):
        self.interrupts += 1


def _run(coro, wall: float):
    """跑 coro，最多 wall 秒；返回 ('done', result) / ('alive', None) / ('raised', exc)。"""

    async def _runner():
        return await asyncio.wait_for(coro, timeout=wall)

    try:
        return "done", asyncio.run(_runner())
    except TimeoutError:  # asyncio.wait_for 超时（3.11+ 即内置 TimeoutError）
        return "alive", None
    except Exception as exc:  # noqa: BLE001 - 测试要捕获具体异常类型
        return "raised", exc


# ----------------------------------------------------------------- [1][2][3] 无限等待
def part_no_limit() -> None:
    print("\n[1-3] 无限等待（timeout=0）：健康与否完全由 ComfyUI 状态判定")

    # [1] 一直在队列跑 → 不放弃（外层 1.2s 超时即证明还在轮询，且从未抛 JobTimeout）
    c1 = FakeClient()  # history 恒 None，queued 恒 True
    status, val = _run(
        c1.wait("p1", timeout=0.0, poll_interval=0.02, poll_interval_max=0.05), wall=1.2
    )
    check(
        "[1] 一直在跑 → 不抛 JobTimeout（持续轮询）",
        status == "alive" and isinstance(val, type(None)),
        f"status={status}, calls={c1.history_calls}",
    )
    check(
        "[1] 确实在持续轮询（多轮 history 查询）",
        c1.history_calls > 5,
        f"history_calls={c1.history_calls}",
    )

    # [2] 跑几轮后完成 → 返回 history 条目
    done_entry = {
        "outputs": {"1": {"images": [{"filename": "x.png"}]}},
        "status": {"status_str": "success", "completed": True},
    }
    c2 = FakeClient(history_seq=[None, None, done_entry])
    status, val = _run(
        c2.wait("p2", timeout=0.0, poll_interval=0.01, poll_interval_max=0.05), wall=2.0
    )
    check(
        "[2] 任务完成 → 返回 history 条目",
        status == "done" and val is done_entry,
        f"status={status}",
    )

    # [3] 消失（不在队列且无 history 连续 3 轮）→ JobCancelled
    c3 = FakeClient(queued_seq=[False, False, False])  # 每次查询都不在队列
    status, val = _run(
        c3.wait("p3", timeout=0.0, poll_interval=0.01, poll_interval_max=0.05), wall=2.0
    )
    check(
        "[3] 任务消失 → 抛 JobCancelled（不是超时误杀）",
        status == "raised" and isinstance(val, JobCancelled),
        f"status={status}, exc={type(val).__name__ if status == 'raised' else ''}",
    )


# ----------------------------------------------------------------- [4][5] legacy 上限
def part_legacy_cap() -> None:
    print("\n[4-5] 向后兼容：timeout>0 仍保留安全上限")

    # [4] 一直在队列跑 + timeout>0 → grace 结束抛 JobTimeout
    c4 = FakeClient()  # 恒在队列
    status, val = _run(
        c4.wait("p4", timeout=0.3, grace=0.2, poll_interval=0.01, poll_interval_max=0.02),
        wall=3.0,
    )
    check(
        "[4] legacy 上限仍在：一直在跑 → 抛 JobTimeout",
        status == "raised" and isinstance(val, JobTimeout),
        f"status={status}, exc={type(val).__name__ if status == 'raised' else ''}",
    )
    check("[4] legacy 上限触发时会中断 ComfyUI", c4.interrupts >= 1, f"interrupts={c4.interrupts}")

    # [5] legacy + 任务完成 → 返回（上限不影响正常完成）
    done_entry = {
        "outputs": {"1": {"images": [{"filename": "y.png"}]}},
        "status": {"status_str": "success", "completed": True},
    }
    c5 = FakeClient(history_seq=[None, done_entry])
    status, val = _run(
        c5.wait("p5", timeout=5.0, grace=1.0, poll_interval=0.01, poll_interval_max=0.02),
        wall=3.0,
    )
    check(
        "[5] legacy 上限下任务正常完成 → 返回 history 条目",
        status == "done" and val is done_entry,
        f"status={status}",
    )


def main() -> int:
    part_no_limit()
    part_legacy_cap()
    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    failed = [(n, d) for ok, n, d in results if not ok]
    print(f"\n==== {passed}/{total} checks passed ====")
    for n, d in failed:
        print(f"  FAIL: {n}  {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
