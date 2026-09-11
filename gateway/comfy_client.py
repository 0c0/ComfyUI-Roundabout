"""ComfyUI 原生 HTTP API 客户端（提交 / 轮询 / 取图 / 上传 / 中断）。

只用 REST，不接 websocket —— 少一个长连接就少一类线上故障，
轮询用退避策略把空转开销压下去。

本客户端运行在 ComfyUI 进程内部（网关即 ComfyUI 自身），默认通过 loopback
访问本机 ComfyUI；也可经 COMFY_BASE_URL 指向另一个 ComfyUI 实例。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import aiohttp

from .config import settings
from .errors import APIError, JobCancelled, JobTimeout, UpstreamError

log = logging.getLogger("roundabout.comfy")


class _Resp:
    """把响应体整段读完后再返回，避免 aiohttp 连接在 with 块外被释放导致读不到。"""

    __slots__ = ("status", "raw")

    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self.raw = raw

    def json(self) -> Any:
        return json.loads(self.raw)

    @property
    def content(self) -> bytes:
        return self.raw


# 进程内共享一个 aiohttp 会话（首次请求时在当前事件循环里创建）
_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.comfy_http_timeout, connect=10.0),
            raise_for_status=False,
        )
    return _session


class ComfyClient:
    def __init__(self, base_url: str, http_timeout: float | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        if http_timeout is not None:
            self._timeout = http_timeout
        else:
            self._timeout = settings.comfy_http_timeout

    # ------------------------------------------------------------------ 基础
    async def _request(self, method: str, path: str, **kw: Any) -> _Resp:
        session = get_session()
        try:
            async with session.request(method, self.base_url + path, **kw) as resp:
                raw = await resp.read()
                return _Resp(resp.status, raw)
        except aiohttp.ClientConnectionError as exc:
            raise UpstreamError(
                f"Cannot reach ComfyUI at {self.base_url}: {exc}", code="comfyui_unreachable"
            ) from exc
        except asyncio.TimeoutError as exc:
            raise UpstreamError(
                f"ComfyUI request timed out ({method} {path}).", status_code=504, code="comfyui_timeout"
            ) from exc

    async def ping(self) -> dict[str, Any]:
        resp = await self._request("GET", "/system_stats")
        if resp.status != 200:
            raise UpstreamError(f"ComfyUI health check failed: HTTP {resp.status}")
        return resp.json()

    # ------------------------------------------------------------------ 上传
    async def upload_image(
        self, data: bytes, filename: str, *, image_type: str = "input", content_type: str = "image/png"
    ) -> str:
        """上传参考素材（图/视频/音频）到 ComfyUI input 文件夹，返回 LoadImage/GetVideoComponents/LoadAudio 可直接引用的名字。

        ComfyUI 的 `/upload/image` 路由按文件名落盘、不校验内容类型，因此视频(*.mp4)/音频(*.wav)
        可复用同一路由上传；`content_type` 仅影响 multipart 元数据，按素材真实类型传入即可。
        """
        form = aiohttp.FormData()
        form.add_field("image", data, filename=filename, content_type=content_type)
        form.add_field("type", image_type)
        form.add_field("overwrite", "true")
        resp = await self._request("POST", "/upload/image", data=form)
        if resp.status != 200:
            raise UpstreamError(f"Failed to upload asset to ComfyUI: HTTP {resp.status} {str(resp.raw[:300])}")
        body = resp.json()
        name = body.get("name") or filename
        subfolder = body.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    # ------------------------------------------------------------------ 提交
    async def submit(self, workflow: dict[str, Any], client_id: str | None = None) -> str:
        payload = {"prompt": workflow, "client_id": client_id or str(uuid.uuid4())}
        resp = await self._request("POST", "/prompt", json=payload)

        if resp.status != 200:
            raise UpstreamError(_format_validation_error(resp), status_code=400, code="workflow_invalid")

        body = resp.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise UpstreamError(f"ComfyUI did not return a prompt_id: {body}")
        if body.get("node_errors"):
            log.warning("ComfyUI node_errors on submit: %s", body["node_errors"])
        return str(prompt_id)

    # ------------------------------------------------------------------ 轮询
    async def wait(
        self,
        prompt_id: str,
        *,
        timeout: float,
        grace: float = 0.0,
        poll_interval: float = 1.0,
        poll_interval_max: float = 3.0,
    ) -> dict[str, Any]:
        """轮询直到任务结束，返回该 prompt 的 history 条目。

        超时语义（避免误杀「还在跑但慢」的任务）：
        - 首轮 ``timeout`` 秒内完成 → 正常返回；
        - 超过 ``timeout`` 后先查 ComfyUI 队列：任务仍在 running/pending → 说明
          只是生成慢，**不中断**，进入 ``grace`` 秒宽限期继续等（期间若完成同样返回）；
        - 任务已离开队列且 history 无结果 → 任务已消失（被清队列/异常终止），
          判定真超时：中断并抛 ``JobTimeout``。
        宽限期结束仍活着但未完成 → 同样中断并抛 ``JobTimeout``（防止无限等待）。

        外部取消感知：每轮 history 无结果时都会检查队列活性，任务连续多轮
        不在队列（被用户在 ComfyUI 取消/清队列）→ 立即抛 ``JobCancelled``，
        不必等到超时；任务表随即反映取消状态。
        """
        deadline = time.monotonic() + timeout
        grace_deadline = deadline + max(0.0, grace)
        interval = poll_interval
        missing_rounds = 0  # 连续几轮「无 history 且不在队列」
        while True:
            entry = await self.history(prompt_id)
            if entry:
                status = entry.get("status") or {}
                status_str = status.get("status_str")
                completed = status.get("completed")
                if status_str == "error" or (completed is False and status.get("messages")):
                    detail = _format_exec_error(status)
                    if detail:
                        raise UpstreamError(detail, status_code=502, code="workflow_execution_failed")
                if entry.get("outputs") or completed:
                    return entry

            # history 无结果：查队列判断任务是否仍存活（外部取消/清队列会立即离队）
            still_queued = await self.is_queued(prompt_id)
            if still_queued:
                missing_rounds = 0
            else:
                missing_rounds += 1
                if missing_rounds >= 3:
                    # 连续 3 轮既无 history 又不在队列：被外部取消/清队列/worker 异常
                    raise JobCancelled(prompt_id)

            now = time.monotonic()
            if now >= deadline:
                if not still_queued:
                    # 任务已离开队列且无 history 结果 → 真超时（消失/被清队列/异常终止）
                    await self.interrupt(silent=True)
                    raise JobTimeout(timeout, vanished=True)
                if now >= grace_deadline:
                    # 宽限期结束仍在跑：不再等，中断并报超时
                    await self.interrupt(silent=True)
                    raise JobTimeout(timeout + grace)

            await asyncio.sleep(min(interval, max(0.05, deadline - time.monotonic())))
            interval = min(interval * 1.35, poll_interval_max)

    async def is_queued(self, prompt_id: str) -> bool:
        """任务是否仍在 ComfyUI 队列中（running 或 pending）。

        用于超时判定：任务还在队列 = 生成中未结束，不应判定为超时。
        """
        try:
            info = await self.queue()
        except APIError:
            # 队列查询失败（ComfyUI 不可达）时保守按「仍在队列」处理，避免误杀
            return True
        for key in ("queue_running", "queue_pending"):
            for item in info.get(key) or []:
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                    return True
        return False

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        resp = await self._request("GET", f"/history/{prompt_id}")
        if resp.status != 200:
            return None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None
        return data.get(prompt_id)

    async def queue(self) -> dict[str, Any]:
        """读取 ComfyUI 执行队列：{queue_running: [...], queue_pending: [...]}。

        每个条目为 [number, prompt_id, prompt(workflow), extra_data, outputs_to_execute]
        （server 在返回前已剥离第 6 个敏感字段）。队列监控/管理面板用。
        """
        resp = await self._request("GET", "/queue")
        if resp.status != 200:
            raise UpstreamError(f"Failed to read ComfyUI queue: HTTP {resp.status}")
        return resp.json()

    async def delete_queue(self, prompt_ids: list[str]) -> None:
        """从 ComfyUI 队列移除尚未开始的任务（正在执行的不受影响，需走 interrupt）。"""
        resp = await self._request("POST", "/queue", json={"delete": list(prompt_ids)})
        if resp.status != 200:
            raise UpstreamError(f"Failed to delete ComfyUI queue entries: HTTP {resp.status}")

    # ------------------------------------------------------------------ 取图
    async def fetch_image(self, ref: dict[str, Any]) -> bytes:
        params = {
            "filename": ref.get("filename", ""),
            "subfolder": ref.get("subfolder", "") or "",
            "type": ref.get("type", "output") or "output",
        }
        resp = await self._request("GET", "/view", params=params)
        if resp.status != 200:
            raise UpstreamError(f"Failed to download output image `{params['filename']}`: HTTP {resp.status}")
        return resp.content

    async def interrupt(self, *, silent: bool = False) -> None:
        try:
            await self._request("POST", "/interrupt")
        except Exception as exc:  # noqa: BLE001 - 尽力而为的取消
            if not silent:
                raise
            log.debug("interrupt failed (ignored): %s", exc)


# ---------------------------------------------------------------------- helpers
def collect_images(entry: dict[str, Any], output_node: str | None = None) -> list[dict[str, Any]]:
    """从 history 条目里抽出图片引用；output_node 为空则收集全部输出节点。"""
    outputs: dict[str, Any] = entry.get("outputs") or {}
    refs: list[dict[str, Any]] = []
    nodes = [output_node] if output_node and output_node in outputs else list(outputs.keys())
    for node_id in nodes:
        node_out = outputs.get(node_id) or {}
        for key in ("images", "gifs"):
            for item in node_out.get(key, []) or []:
                if item.get("type") == "temp" and len(nodes) > 1:
                    continue  # 多节点时忽略预览图，只要正式产物
                refs.append(item)
    return refs


def _format_validation_error(resp: _Resp) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return f"ComfyUI rejected the workflow (HTTP {resp.status}): {str(resp.raw[:400])}"

    err = body.get("error") or {}
    parts: list[str] = []
    if err:
        parts.append(str(err.get("message") or err))
        if err.get("details"):
            parts.append(str(err["details"]))
    for node_id, node_err in (body.get("node_errors") or {}).items():
        for e in node_err.get("errors", []):
            parts.append(f"[node {node_id}] {e.get('message')} ({e.get('details')})")
    return "ComfyUI rejected the workflow: " + "; ".join(p for p in parts if p) if parts else str(body)[:400]


def _format_exec_error(status: dict[str, Any]) -> str | None:
    for msg in status.get("messages") or []:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        kind, data = msg[0], msg[1]
        if kind == "execution_error" and isinstance(data, dict):
            return (
                f"ComfyUI execution failed at node {data.get('node_id')} "
                f"({data.get('node_type')}): {data.get('exception_type')}: {data.get('exception_message')}"
            )
        if kind == "execution_interrupted":
            return "ComfyUI execution was interrupted."
    if status.get("status_str") == "error":
        return "ComfyUI execution failed."
    return None
