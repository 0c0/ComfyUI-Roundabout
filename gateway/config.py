"""运行期配置：全部来自环境变量，无外部配置框架依赖。

注意：本节点运行在 ComfyUI 进程内部，因此默认把 ComfyUI 后端指向与 ComfyUI
相同的监听地址与端口（取自 server.args 的 --listen / --port，不再写死 127.0.0.1）。
如需桥接到另一个 ComfyUI 实例，设置环境变量 COMFY_BASE_URL 即可完全覆盖。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 节点根目录（gateway/..）
BASE_DIR = Path(__file__).resolve().parent.parent

log = logging.getLogger("roundabout.config")


def _load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """手写最小 dotenv：仅当同名环境变量未设置时，用 .env 中的值补充。

    不引入额外依赖；通过 shell 显式设置的环境变量始终优先。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if not key:
                    continue
                val = val.strip()
                if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                    val = val[1:-1]
                if key not in os.environ:
                    os.environ[key] = val
    except OSError:
        return


# 应用启动前自动读取节点根目录的 .env（若存在）
_load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return default if v is None or v == "" else v


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_list(key: str) -> list[str]:
    raw = _env(key, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


# 端口映射串里的整数：只关心数字，括号/冒号/等号/箭头等分隔符一律忽略，
# 因此 `[8188,888],[8189,999]`、`8188:888,8189:999`、`8188=888;8189=999`
# 三种写法解析结果完全相同。
_PORT_NUM_RE = re.compile(r"\d+")


def parse_port_map(raw: str) -> dict[int, int]:
    """解析「ComfyUI 端口 -> MCP 端口」映射表。

    语义是**成对**的：每两个数字一组，左边是 ComfyUI 的 `--port`，右边是该实例
    的 Roundabout(MCP) 端口，顺序敏感。举例：
        [8188,888],[8189,999]   → {8188: 888, 8189: 999}
        8188:888, 8189:999      → 同上
    只取串中的整数再两两成组，故分隔符随便写；落单的最后一个数字会被丢弃。
    """
    nums = [int(x) for x in _PORT_NUM_RE.findall(raw or "")]
    if len(nums) % 2:
        log.warning(
            "MCP_PORT_MAP: ignored trailing value without a pair (need comfy_port,mcp_port): %r", raw
        )
        nums = nums[:-1]
    return {nums[i]: nums[i + 1] for i in range(0, len(nums), 2)}


def _env_port_map(key: str) -> dict[int, int]:
    return parse_port_map(_env(key, ""))


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (BASE_DIR / p)


def _resolve_comfy_host() -> str:
    """从 ComfyUI 的 --listen 推导一个客户端可连的 host。

    --listen 可能是单个 IP、逗号分隔的 IP 列表（如 "127.2.2.2,127.3.3.3"），
    或通配符 "0.0.0.0" / "::"（监听所有接口）。客户端回连时必须避开通配符：
    - 命中具体 IP → 直接用该 IP（ComfyUI 已绑定它）。
    - 全是通配符 → 回落 127.0.0.1（loopback 在通配监听下依然可达）。
    """
    try:
        from server import args  # type: ignore

        listen = getattr(args, "listen", None)
    except Exception:  # noqa: BLE001 - server 不可用时回落默认
        listen = None
    if listen:
        for part in str(listen).split(","):
            part = part.strip()
            if part and part not in ("0.0.0.0", "::"):
                return part
    return "127.0.0.1"


def _default_comfy_base_url() -> str:
    """默认指向本机 ComfyUI。host/端口取自 server.args 的 --listen / --port，
    跟随 ComfyUI 的实际监听配置，不再写死 127.0.0.1。"""
    try:
        from server import args  # type: ignore

        port = getattr(args, "port", None) or 8188
        host = _resolve_comfy_host()
        return f"http://{host}:{port}"
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:8188"


def _comfy_port() -> int:
    """本机这个 ComfyUI 实例的监听端口（即 `--port`）。

    节点跑在 ComfyUI 进程内，端口与 ComfyUI 同源：先看 PromptServer 是否已经
    完成绑定（`PromptServer.instance.port`，绑定后才有），否则退回命令行参数
    `server.args.port`（节点导入时就能拿到），都取不到时按 ComfyUI 默认 8188。
    自定义节点是在服务开始监听之前加载的，所以正常情况下走的是第二条。
    """
    try:
        from server import PromptServer  # type: ignore

        bound = getattr(getattr(PromptServer, "instance", None), "port", None)
        if bound:
            return int(bound)
    except Exception:  # noqa: BLE001 - server 不可用时回落
        pass
    try:
        from server import args  # type: ignore

        port = getattr(args, "port", None)
        if port:
            return int(port)
    except Exception:  # noqa: BLE001
        pass
    return 8188


def resolve_mcp_port(
    port_map: dict[int, int], explicit_port: int = 0, comfy_port: int | None = None
) -> int:
    """算出嵌入式 MCP 后端应该绑哪个端口。

    优先级（自上而下，先命中先用）：
      1. `MCP_PORT` > 0：手动钉死一个端口，最强覆盖；
      2. `MCP_PORT_MAP` 命中当前 ComfyUI 端口：例如 8188 的实例用 888、
         8189 的实例用 999 —— 同一台机器跑多个 ComfyUI 实例时各占各的；
      3. 0：交给操作系统分配空闲端口，保证任何配置下都能起来。
    """
    if explicit_port:
        return explicit_port
    port = _comfy_port() if comfy_port is None else comfy_port
    return port_map.get(port, 0)


@dataclass(frozen=True)
class Settings:
    # ---- ComfyUI 后端（默认本机 loopback；可用 COMFY_BASE_URL 覆盖指向远端）----
    comfy_base_url: str = field(
        default_factory=lambda: _env("COMFY_BASE_URL", "").rstrip("/") or _default_comfy_base_url()
    )
    comfy_http_timeout: float = field(default_factory=lambda: _env_float("COMFY_HTTP_TIMEOUT", 30.0))

    # ---- 任务轮询与超时 ----
    job_timeout: float = field(default_factory=lambda: _env_float("JOB_TIMEOUT", 300.0))
    # 超时后先查 ComfyUI 队列确认任务是否仍存活：还在 running/pending 就不中断，
    # 再给一个宽限期（job_grace 秒）等待。任务已离开队列且无 history 才算真超时。
    job_grace: float = field(default_factory=lambda: _env_float("JOB_GRACE", 300.0))
    poll_interval: float = field(default_factory=lambda: _env_float("POLL_INTERVAL", 1.0))
    poll_interval_max: float = field(default_factory=lambda: _env_float("POLL_INTERVAL_MAX", 3.0))
    max_concurrency: int = field(default_factory=lambda: _env_int("MAX_CONCURRENCY", 2))

    # ---- MCP 网关（嵌入 ComfyUI 进程，streamable-http）----
    # 默认开启：MCP 端点直接挂在 ComfyUI 自身端口上，不额外占端口、不额外起进程，
    # 开着不调用就没有任何开销；需要纯 REST 时设 MCP_ENABLED=false 关掉
    # （同时可省掉 mcp / uvicorn 这两个依赖）。
    mcp_enabled: bool = field(default_factory=lambda: _env_bool("MCP_ENABLED", True))
    mcp_host: str = field(default_factory=lambda: _env("MCP_HOST", "127.0.0.1"))
    # 内部 uvicorn 后端端口。留空/0（默认）= 由操作系统分配空闲端口——同一台机器上
    # 同时跑多个 ComfyUI 实例（各自 --port 不同）时，每个实例的 MCP 后端各占各的端口，
    # 不会像写死端口那样后来者绑不上；实际端口在监听成功后由节点写入日志。
    mcp_port: int = field(default_factory=lambda: _env_int("MCP_PORT", 0))
    # 「ComfyUI 端口 -> Roundabout(MCP) 端口」映射，成对书写，顺序敏感：
    #   MCP_PORT_MAP=[8188,888],[8189,999]
    # 启动时按本实例的 ComfyUI 端口取右边那个值来绑定，于是 --port 8188 的实例用 888、
    # --port 8189 的实例用 999，同机多开互不干扰；未命中则回落到系统分配。
    # 优先级低于显式 MCP_PORT（二者同时设置时 MCP_PORT 生效）。
    mcp_port_map: dict[int, int] = field(default_factory=lambda: _env_port_map("MCP_PORT_MAP"))
    mcp_path: str = field(default_factory=lambda: _env("MCP_PATH", "/mcp"))
    # true（默认）：MCP 端点通过 aiohttp 原生代理挂到 ComfyUI 同一端口（如 8188/mcp），
    # 内部 uvicorn 后端只绑定 127.0.0.1 回环；false：客户端直连 MCP_HOST:MCP_PORT。
    mcp_share_port: bool = field(default_factory=lambda: _env_bool("MCP_SHARE_PORT", True))
    # true（默认）：无状态模式——每个请求独立处理、不跟踪会话，agent 不再怕 ComfyUI 重启。
    #   选它当默认，是因为会话在本插件里只有一处用途：给「异步任务完成通知」当推送通道
    #   （且只在 background=pending 这条可选路径上、只在会话还活着的时候有效）。
    #   收益这么窄，却要拿「ComfyUI 一重启、所有工具调用集体报 Session not found」去换。
    # false：标准有状态 streamable-http，保留推送通道；代价是会话存于进程内存，ComfyUI
    #   重启后旧 Mcp-Session-Id 全部失效，客户端必须重新 initialize，否则报
    #   「unknown or expired session ID」。想要推送就显式关掉无状态。
    # 两种模式都由 SDK 原生支持（stateless_http=True），无自研逻辑。
    mcp_stateless: bool = field(default_factory=lambda: _env_bool("MCP_STATELESS", True))

    @property
    def comfy_port(self) -> int:
        """本实例的 ComfyUI 端口（映射表的左值）。"""
        return _comfy_port()

    @property
    def resolved_mcp_port(self) -> int:
        """实际要绑定的 MCP 后端端口：见 resolve_mcp_port() 的优先级说明。"""
        return resolve_mcp_port(self.mcp_port_map, self.mcp_port)

    # ---- 鉴权 ----
    api_keys: list[str] = field(default_factory=lambda: _env_list("OPENAI_GATEWAY_API_KEYS"))
    auth_required: bool = field(default_factory=lambda: _env_bool("OPENAI_GATEWAY_AUTH_REQUIRED", True))

    # ---- 模型 / 工作流（默认指向节点自带的 models.yaml 与 workflows/）----
    models_file: Path = field(default_factory=lambda: _resolve(_env("MODELS_FILE", "models.yaml")))
    workflows_dir: Path = field(default_factory=lambda: _resolve(_env("WORKFLOWS_DIR", "workflows")))
    default_model: str = field(default_factory=lambda: _env("DEFAULT_MODEL", ""))

    # ---- url 模式的产物落盘 ----
    output_dir: Path = field(default_factory=lambda: _resolve(_env("OUTPUT_DIR", ".cache/outputs")))
    output_ttl: float = field(default_factory=lambda: _env_float("OUTPUT_TTL", 3600.0))
    # 留空时由请求自身的 host 补全（见 handlers._absolutize）
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "").rstrip("/"))

    # 注意：ComfyUI 的 input / output / temp 根目录**不**在此处配置。
    # 节点运行在 ComfyUI 进程内，应始终通过 ComfyUI 的 folder_paths 接口在运行时
    # 动态获取（见 pipeline._comfy_input_root / _comfy_output_root）。硬编码这些路径
    # 会让节点无法分发——每个用户的安装路径不同，且环境变量覆盖会随部署漂移。
    # 拿不到 folder_paths 时（如独立/远端模式），相关根目录回落为 None，代码自动降级。

    # ---- 入参限制 ----
    max_n: int = field(default_factory=lambda: _env_int("MAX_N", 4))
    max_input_image_mb: float = field(default_factory=lambda: _env_float("MAX_INPUT_IMAGE_MB", 20.0))
    # 参考视频/音频体积更大，单独放宽上限（base64/URL/本地路径共用解析逻辑）
    max_input_asset_mb: float = field(default_factory=lambda: _env_float("MAX_INPUT_ASSET_MB", 200.0))

    # ---- 负向分流：支持负向提示词的模型，自动把 prompt 内 "no/without/not X" 抽进 negative_prompt ----
    auto_split_negative: bool = field(default_factory=lambda: _env_bool("AUTO_SPLIT_NEGATIVE", True))

    @property
    def auth_enabled(self) -> bool:
        return self.auth_required and bool(self.api_keys)


settings = Settings()
