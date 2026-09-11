"""ComfyUI-Roundabout.gateway —— 把 roundabout2 的 OpenAI 兼容网关业务搬进 ComfyUI 进程内。

子模块（按需导入，避免无谓的 import 链）：
  config        运行期配置（环境变量）
  errors        OpenAI 规范错误对象
  registry      模型注册表 + 工作流 JSON 路径注入引擎
  comfy_client  ComfyUI 原生 API 客户端（aiohttp）
  params        尺寸/种子/预设/输入图解析
  schemas       OpenAI Images / Videos 请求响应模型（pydantic）
  auth          Bearer 鉴权
  store         url 模式临时产物存储 + TTL 清理
  pipeline      生成流水线
  handlers      aiohttp 路由处理器（由节点 __init__.py 显式导入并注册）
"""

from .config import settings  # noqa: F401
from .registry import registry  # noqa: F401

__all__ = ["settings", "registry"]
