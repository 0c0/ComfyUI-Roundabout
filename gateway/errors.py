"""OpenAI 规范的错误对象。

OpenAI 客户端（含 Hermes Agent 内置的 OpenAI Image Provider）解析失败时
依赖 `{"error": {...}}` 结构，因此所有异常出口都必须走这里。
"""

from __future__ import annotations

from typing import Any

from aiohttp import web


class APIError(Exception):
    """可直接序列化为 OpenAI error 结构的业务异常。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        err_type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.err_type = err_type
        self.code = code
        self.param = param

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.err_type,
                "param": self.param,
                "code": self.code,
            }
        }


class AuthError(APIError):
    def __init__(self, message: str = "Incorrect API key provided.", code: str = "invalid_api_key") -> None:
        super().__init__(message, status_code=401, err_type="invalid_request_error", code=code)


class ModelNotFound(APIError):
    def __init__(self, model: str, available: list[str]) -> None:
        hint = ", ".join(available[:20]) or "<empty registry>"
        super().__init__(
            f"The model `{model}` does not exist. Available models: {hint}",
            status_code=404,
            err_type="invalid_request_error",
            code="model_not_found",
            param="model",
        )


class UpstreamError(APIError):
    """ComfyUI 侧的失败（连接不上 / 校验不过 / 执行报错）。"""

    def __init__(self, message: str, *, status_code: int = 502, code: str = "comfyui_error") -> None:
        super().__init__(message, status_code=status_code, err_type="api_error", code=code)


class JobTimeout(APIError):
    def __init__(self, seconds: float, *, vanished: bool = False) -> None:
        shown = f"{seconds:.1f}" if seconds < 10 else f"{seconds:.0f}"
        if vanished:
            detail = (
                "The task left the ComfyUI queue without producing a result "
                "(cleared, cancelled, or the worker died)."
            )
        else:
            detail = (
                "The task was still running in the ComfyUI queue when the grace period ended. "
                "Increase the per-model `timeout`/`JOB_TIMEOUT`/`JOB_GRACE` if the workflow is expected to be slow, "
                "or set `JOB_TIMEOUT=0` to disable the time cap entirely and let ComfyUI task status decide."
            )
        super().__init__(
            f"Image generation timed out after {shown}s. {detail}",
            status_code=504,
            err_type="api_error",
            code="job_timeout",
        )


class JobCancelled(APIError):
    """任务在 ComfyUI 侧被外部取消 / 清队列（非网关超时触发）。"""

    def __init__(self, prompt_id: str) -> None:
        super().__init__(
            f"Task `{prompt_id}` was cancelled or cleared in ComfyUI before completion.",
            status_code=409,
            err_type="api_error",
            code="job_cancelled",
        )


def error_response(exc: APIError) -> web.Response:
    return web.json_response(exc.payload(), status=exc.status_code)
