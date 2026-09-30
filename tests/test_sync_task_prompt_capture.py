"""同步生成也要留下「提交图」—— 用返回的 task_id 就能查到自己当时提交了什么。

为什么需要这个测试：
    任务表有三层取用方式（ComfyUI 队列 → history → 网关任务快照）。前两层都依赖
    ComfyUI 自己的记忆：history 会被 ComfyUI 重启/清理抹掉，队列只在跑的时候有。
    第三层（`attach_prompt` 写下的 prompt_id + 工作流快照）是本网关自己的账本，唯一
    不依赖上游状态的证据 —— 而它此前**只有异步视频链路在写**：同步的
    `generate_tracked` / `generate_video_tracked` 提交时压根没传 `on_submit`，
    于是「同步生成 → 拿 task_id 回查提交图」必然拿到 404 `prompt_not_in_queue`。

    这是**静默**缺口：生成成功、回执正常、看板上也有这张卡，只是那张卡永远没有提交图。
    所以要同时锁三层：接线点（两个 tracked 入口都传 on_submit）、透传路径（pipeline
    每个 `_run_once` 都带下去）、消费结果（任务表真的被回填，且 admin 第三层查得到）。

不碰正在运行的 ComfyUI：假 ComfyClient 的队列/history 恒空 —— 命中第三层只可能是
快照，不可能是上游串味；独立 aiohttp 实例绑 8200 回环。

    python tests/test_sync_task_prompt_capture.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

NODE = Path(__file__).resolve().parent.parent   # 节点目录
ROOT = NODE.parent.parent                        # ComfyUI 根目录
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from gateway import admin, handlers  # noqa: E402
from gateway.routes import register_routes  # noqa: E402
from gateway.schemas import ImageGenerationRequest, ImageResponse, VideoGenerationRequest, VideoResponse  # noqa: E402
from gateway.tasks import task_store  # noqa: E402

PORT = 8200
BASE = f"http://127.0.0.1:{PORT}"

# 快照里放一个可辨识的节点，证明存的是「提交的那张图」而不是个空壳 dict
WORKFLOW_SNAPSHOT = {
    "950": {"class_type": "RoundaboutSizeProbe", "inputs": {"images": ["92", 0]}},
    "92": {"class_type": "SaveVideo", "inputs": {}},
}

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
    if not ok:
        failures += 1


# ---------------------------------------------------------------- 假上游
class _FakeComfy:
    """队列与 history 恒空：任何命中都只可能来自网关任务快照。"""

    async def queue(self) -> dict:
        return {"queue_running": [], "queue_pending": []}

    async def history(self, prompt_id: str) -> None:
        return None


def _install_fakes() -> None:
    async def fake_generate(req, client, *, request_id, image_inputs=None,
                            mask_input=None, on_submit=None):
        check("图像同步链路把 on_submit 传进了 pipeline.generate", on_submit is not None)
        if on_submit is not None:
            on_submit("fake-prompt-image-0001", WORKFLOW_SNAPSHOT)
        return ImageResponse(created=1, data=[{"url": "/view?filename=a.png"}], seed=7, size="832x480")

    async def fake_generate_video(req, client, *, request_id, image_inputs=None, on_submit=None):
        check("视频同步链路把 on_submit 传进了 pipeline.generate_video", on_submit is not None)
        if on_submit is not None:
            on_submit("fake-prompt-video-0001", WORKFLOW_SNAPSHOT)
        return VideoResponse(created=1, data=[{"url": "/view?filename=a.mp4"}], seed=7, size="1024x576")

    handlers.generate = fake_generate
    handlers.generate_video = fake_generate_video


# ---------------------------------------------------------------- 静态接线
def _funcs(path: Path) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def _calls_of(node: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == name:
            out.append(sub)
    return out


def _has_on_submit(call: ast.Call) -> bool:
    return any(kw.arg == "on_submit" for kw in call.keywords)


def _on_submit_mentions_task_id(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "on_submit" and "task_id" in ast.unparse(kw.value):
            return True
    return False


def static_checks() -> None:
    print("1. 静态接线（改坏了就没人回填，且不会有任何报错）")
    pl = _funcs(NODE / "gateway" / "pipeline.py")
    for fn_name in ("generate", "generate_video"):
        fn = pl.get(fn_name)
        check(f"pipeline.{fn_name} 有定义", fn is not None)
        if fn is None:
            continue
        arg = next((a for a in fn.args.kwonlyargs if a.arg == "on_submit"), None)
        check(f"pipeline.{fn_name} 接收 on_submit（keyword-only）", arg is not None)
        if arg is not None:
            idx = fn.args.kwonlyargs.index(arg)
            default = fn.args.kw_defaults[idx]
            check(f"pipeline.{fn_name}.on_submit 默认 None",
                  isinstance(default, ast.Constant) and default.value is None,
                  ast.unparse(default) if default is not None else "no default")
        runs = _calls_of(fn, "_run_once")
        check(f"pipeline.{fn_name} 调 _run_once（图像 2 处 / 视频 2 处）", len(runs) == 2, str(len(runs)))
        for i, call in enumerate(runs):
            check(f"pipeline.{fn_name} 第 {i + 1} 处 _run_once 透传 on_submit={{{{on_submit}}}}",
                  _has_on_submit(call))

    hd = _funcs(NODE / "gateway" / "handlers.py")
    for fn_name, callee in (("generate_tracked", "generate"), ("generate_video_tracked", "generate_video"),
                            ("_run_video_task", "generate_video")):
        fn = hd.get(fn_name)
        check(f"handlers.{fn_name} 有定义", fn is not None)
        if fn is None:
            continue
        calls = _calls_of(fn, callee)
        check(f"handlers.{fn_name} 调 {callee} 一次", len(calls) == 1, str(len(calls)))
        for call in calls:
            check(f"handlers.{fn_name} 传了 on_submit=", _has_on_submit(call))
            check(f"handlers.{fn_name} 的 on_submit 绑定到 task_id（防写错变量）",
                  _on_submit_mentions_task_id(call), ast.unparse(call.keywords[-1].value) if call.keywords else "")

    # 回填要有消费者：三层查找的第三层按 id 取任务快照
    for rel, fn_name in (("gateway/admin.py", "queue_workflow"), ("mcp_server.py", "get_workflow")):
        src = (NODE / rel).read_text(encoding="utf-8")
        check(f"{rel}::{fn_name} 第三层读任务表快照", "task_store.get(" in src and "task.workflow" in src)


# ---------------------------------------------------------------- 行为
async def behavior_checks() -> None:
    print("2. 同步链路真的回填（假上游，直接看任务表）")
    _install_fakes()

    img = await handlers.generate_tracked(ImageGenerationRequest(model="qwen-image-2.1", prompt="x"))
    rec = task_store.get(img.task_id)
    check("图像：任务表拿到 task_id 那条记录", rec is not None, str(img.task_id))
    if rec is not None:
        check("图像：prompt_id 已回填", rec.prompt_id == "fake-prompt-image-0001", str(rec.prompt_id))
        check("图像：工作流快照已回填（不是空壳）", rec.workflow == WORKFLOW_SNAPSHOT, str(rec.workflow))

    vid = await handlers.generate_video_tracked(VideoGenerationRequest(model="minimax-h3", prompt="x"))
    rec_v = task_store.get(vid.task_id)
    check("视频：任务表拿到 task_id 那条记录", rec_v is not None, str(vid.task_id))
    if rec_v is not None:
        check("视频：prompt_id 已回填", rec_v.prompt_id == "fake-prompt-video-0001", str(rec_v.prompt_id))
        check("视频：工作流快照已回填（不是空壳）", rec_v.workflow == WORKFLOW_SNAPSHOT, str(rec_v.workflow))

    print("3. 用 task_id 走真实 admin 路由（上游恒空 ⇒ 命中只可能是第三层快照）")
    admin._comfy = _FakeComfy()
    app = web.Application()
    register_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    def get(path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    async def fetch(path: str) -> tuple[int, dict]:
        return await asyncio.get_running_loop().run_in_executor(None, get, path)

    st, js = await fetch(f"/roundabout/admin/queue/workflow/{img.task_id}")
    check("同步图像：task_id 查得到提交图", st == 200, f"status={st} body={js}")
    check("同步图像：来源标记为 task（第三层）", js.get("source") == "task", str(js.get("source")))
    check("同步图像：快照内容与提交时一致", js.get("workflow") == WORKFLOW_SNAPSHOT, str(js.get("workflow")))

    st_v, js_v = await fetch(f"/roundabout/admin/queue/workflow/{vid.task_id}")
    check("同步视频：task_id 查得到提交图", st_v == 200 and js_v.get("source") == "task", f"status={st_v} body={js_v}")

    print("4. 没提交过的任务不许编造快照")
    bare = task_store.create("never-submitted-0001", model="qwen-image-2.1")
    task_store.complete(bare.id, {"created": 1, "data": []})
    st_b, js_b = await fetch(f"/roundabout/admin/queue/workflow/{bare.id}")
    check("未提交任务 → 404 prompt_not_in_queue", st_b == 404, f"status={st_b} body={js_b}")
    check("未提交任务 → 错误码正确",
          (js_b.get("error") or {}).get("code") == "prompt_not_in_queue", str(js_b.get("error")))
    st_n, _ = await fetch("/roundabout/admin/queue/workflow/no-such-task-0001")
    check("不存在的 id → 404", st_n == 404, f"status={st_n}")

    await site.stop()
    await runner.cleanup()


if __name__ == "__main__":
    static_checks()
    asyncio.run(behavior_checks())
    print(f"\n {'=' * 46}\n  {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    raise SystemExit(1 if failures else 0)
