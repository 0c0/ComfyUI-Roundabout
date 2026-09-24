"""OpenAI Images API 的请求/响应模型。

标准字段完全对齐 OpenAI；额外字段作为「扩展」保留（extra=allow 兜底），
这样 Hermes 只发标准字段能跑通，需要精调时也能透传 ComfyUI 专属参数。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    # ---------- OpenAI 标准字段 ----------
    # prompt 不在 schema 层强制必填：promptless 工具类模型（如去背景）没有提示词。
    # 是否真需要 prompt 由 pipeline 按模型是否绑定 prompt 校验（缺了报 400）。
    prompt: str | None = Field(None, description="正向提示词；promptless 工具类模型可省略")
    model: str | None = Field(None, description="映射到某个 ComfyUI workflow 模板")
    n: int = Field(1, ge=1, description="生成张数")
    size: str | None = Field(None, description='如 "1024x1024" / "auto"')
    style: str | None = Field(None, description="vivid / natural（可在 models.yaml 里映射为提示词后缀）")
    response_format: Literal["b64_json", "url", "file", "path"] | None = Field(
        None, description='产物形态，默认 "url"（可打开的地址）；需要内联字节才传 b64_json'
    )
    user: str | None = Field(None)
    background: str | None = None
    output_format: str | None = None
    moderation: str | None = None

    # ---------- 扩展字段：ComfyUI 精调 ----------
    negative_prompt: str | None = Field(None, description="反向提示词")
    seed: int | None = Field(None, description="随机种子；不传 / -1 随机，0 与正整数固定")
    steps: int | None = Field(None, ge=1, le=200)
    cfg: float | None = Field(None, ge=0, le=100)
    sampler_name: str | None = None
    scheduler: str | None = None
    denoise: float | None = Field(None, ge=0.0, le=1.0, description="图生图重绘幅度")

    # 模型专属动态模式（如 Ideogram4 的 Quality / Default / Turbo）
    mode: str | None = Field(None, description="生图模式；仅部分模型支持，由 models.yaml 的 mode_choices 约束")

    # 图生图输入：base64 / dataURL / http(s) URL。给 generations 端点用的扩展入口。
    # 语义是「重绘的基图」，只收单张；多图编辑请用 reference_images（语义是参考素材）。
    # 传数组会在 pipeline 里报 400 并指路 —— 早期版本会静默丢弃第二张起，这里不再容忍。
    image: str | list[str] | None = Field(
        None, description="图生图基图（单张）。多图编辑请改用 reference_images"
    )
    mask: str | None = Field(None, description="局部重绘遮罩")

    # 多图编辑的参考图：每项可为 dataURL / base64 / http(s) URL / 本地路径
    # （绝对路径，或相对 ComfyUI input 目录的路径）。
    # 按顺序接入模型工作流的参考槽；槽数上限由 models.yaml 的 references 段决定，
    # 未提供的槽由网关在提交前剪掉（未上传即删除）。
    reference_images: list[str] | None = Field(
        None, description="多图编辑的参考图，按序接入工作流参考槽（数量上限由模型决定）"
    )

    # 直接改写工作流节点，形如 {"3.inputs.cfg": 4.5}
    workflow_overrides: dict[str, Any] | None = None

    # 保存文件名前缀（透传）：ComfyUI SaveImage 的 filename_prefix。
    # 不传则保留工作流模板自带的前缀（如 Z-Image-Turbo）；传入则原样作为前缀（可含 "/" 建子目录）。
    filename_prefix: str | None = Field(None, description="保存文件名前缀；不传则用工作流模板自带的前缀")


class ImageDatum(BaseModel):
    # 单个生成产物的数据描述：按 response_format 输出 url / b64_json / path
    b64_json: str | None = None
    url: str | None = None
    path: str | None = None  # response_format=file/path 时返回的生成产物磁盘绝对路径
    revised_prompt: str | None = None


class ImageResponse(BaseModel):
    created: int
    data: list[ImageDatum]
    usage: dict[str, Any] | None = None
    # 回显本次实际使用的种子：n=1 时为整数；n>1（非 batch）时为整数列表（每项对应一张图）。
    # 不传 seed 时网关随机生成，这里回显随机出的实际值，便于复现。
    seed: int | list[int] | None = None
    # 本次请求在网关任务表里的 id —— 同步链路也留一条记录，于是「生成完再钉卡」可以
    # 只传 `task_id` 让后端自己反查产物，不必由调用方拼地址。异步链路同 id 见
    # GET /v1/videos/tasks/{id}（那边回执的顶层字段名叫 `id`）。
    task_id: str | None = None


# ============================================================================
#  视频生成：OpenAI 没有标准 video 端点，这里沿用 images 的结构（Hermes 视频
#  Provider 可直接对齐），产物默认 url（视频 base64 体积过大）。
# ============================================================================
class VideoGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    # ---------- 标准字段 ----------
    prompt: str = Field(..., description="正向提示词")
    model: str | None = Field(None, description="映射到某个 ComfyUI 视频 workflow 模板")
    n: int = Field(1, ge=1, description="生成视频条数（视频模型多数只支持 1）")
    size: str | None = Field(
        None,
        description=(
            '分辨率预设键 `<tier>p-<ratio>` 或 `<ratio>@<tier>p`，tier∈{480p,576p,720p,768p,1080p,1440p}，'
            'ratio∈{1:1,3:4,4:3,16:9,9:16}（如 "720p-16:9" / "1080p-16:9" / "9:16@480p"）；'
            '1440p 需大显存（8GB 直接生跑不动，改用 lift 放大）；'
            '也可直接写 WxH（如 "1280x720"）。auto/None 用模型默认。'
        ),
    )
    style: str | None = Field(None, description="vivid / natural")
    response_format: Literal["b64_json", "url", "file", "path"] | None = Field(None, description="视频默认 url")
    user: str | None = None

    # ---------- 视频专属 ----------
    duration: float | None = Field(None, description="视频时长（秒），允许 1–15")
    fps: int | None = Field(None, description="帧率")
    num_frames: int | None = Field(None, description="总帧数（部分工作流用帧数而非时长）")

    # ---- 注意力档位（仅 base 四支 H3 视频档；FastH3 恒稀疏、无档位）----
    # sol-attn 稀疏更快、显存更省；画质与致密高度一致，差异只在高频细节。
    # dense 档把 BlockSparseAttention.start_percent 顶到 1.0（percent_to_sigma(1.0)=0
    # ⇒ 每一步都判 dense）等效全程关闭稀疏 —— 换回致密画质，耗时回满。
    attention: Literal["sparse", "dense"] | None = Field(
        None,
        description=(
            "注意力档位：`sparse`（默认，块稀疏加速，更快、更省显存）/"
            "`dense`（关闭稀疏，画质优先，更慢、更吃显存）。不传则保持工作流模板默认（稀疏）。"
            "仅 base 四支 H3 视频档（minimax-h3 / -edit / -lift / -lift-edit）支持；"
            "FastH3 两支恒定稀疏（其 vsa 与蒸馏权重配对训练，无 dense 对照，传了报 400），"
            "其它模型传了同样报 400。"
        ),
    )
    # ---- H3 Lift 确定性放大（minimax-h3-lift 两支）----
    scale: float | None = Field(
        None,
        ge=1.0,
        le=4.0,
        description=(
            "放大倍率（仅 minimax-h3-lift / -lift-edit）：输出 = 768p 画布 × scale，"
            "默认 1.875 → 2520x1440。其它模型传了报 400。"
        ),
    )
    # rho / w_min / w_max 仍不设请求字段：默认（0.0 / 0.5 / 1.0）写死在工作流模板，
    # 精调用厂商通用透传 workflow_overrides，如 {"910.inputs.rho": 0.3}。

    # ---------- 扩展字段：ComfyUI 精调 ----------
    negative_prompt: str | None = Field(None, description="反向提示词")
    seed: int | None = Field(None, description="随机种子；不传 / -1 随机，0 与正整数固定")
    steps: int | None = Field(None, ge=1, le=200)
    cfg: float | None = Field(None, ge=0, le=100)
    sampler_name: str | None = None
    scheduler: str | None = None
    denoise: float | None = Field(None, ge=0.0, le=1.0, description="图生视频重绘幅度")

    # 图生视频输入：base64 / dataURL / http(s) URL
    image: str | list[str] | None = Field(None, description="图生视频输入图")

    # 参考资源（OpenAI 风格扩展字段）：每项可为 dataURL / base64 / http(s) URL / 本地路径。
    # 本地路径支持「绝对路径」或「相对 ComfyUI input 目录的相对路径」（匹配原生 loader 约定）；
    # 落到 input 目录内则免转存、原地引用，否则读字节后上传副本。
    # 网关在提交工作流前，会按实际提供的数量删除多余的参考节点（未上传即删除）。
    # 数量上限由模型工作流决定（MiniMax H3：最多 6 图 / 3 视频 / 3 音频）。
    reference_images: list[str] | None = Field(None, description="参考图（最多 6 张），支持 base64/http(s)/本地路径（绝对路径或相对 input 目录）")
    reference_videos: list[str] | None = Field(None, description="参考视频（最多 3 个），支持 base64/http(s)/本地路径（绝对路径或相对 input 目录）")
    reference_audios: list[str] | None = Field(None, description="参考音频（最多 3 个），支持 base64/http(s)/本地路径（绝对路径或相对 input 目录）")

    # 直接改写工作流节点，形如 {"3.inputs.cfg": 4.5}
    workflow_overrides: dict[str, Any] | None = None

    # 保存文件名前缀（透传）：ComfyUI VHS/SaveImage 的 filename_prefix。
    # 不传则保留工作流模板自带的前缀（H3 视频统一为 video/MiniMax_H3）；传入则原样作为前缀（可含 "/" 建子目录）。
    filename_prefix: str | None = Field(None, description="保存文件名前缀；不传则用工作流模板自带的前缀")

    # ---- 异步任务模式（对齐 OpenAI 标准异步范式）----
    # OpenAI 官方用 background: "pending" 触发异步：POST 立即返回 task 对象（id + status: pending），
    # 客户端轮询 GET /v1/images/tasks/{id} 直到 status: "completed"，结果在 output.data[]。
    # 为向后兼容，保留 async: true（自定义别名）也触发异步。
    background: str | None = Field(
        None,
        description='OpenAI 标准异步开关："pending" 异步立即返回 task 对象；"opaque"/"transparent"/缺省 同步',
    )
    # 自定义别名（向后兼容）：{"async": true} 等价于 background="pending"
    async_mode: bool = Field(False, alias="async", description="异步任务模式兼容别名：true 等价于 background=pending")

    @property
    def is_async(self) -> bool:
        """是否进入异步任务模式（OpenAI 标准或自定义别名任一触发）。"""
        return self.background == "pending" or bool(self.async_mode)


class VideoResponse(BaseModel):
    created: int
    data: list[ImageDatum]  # 复用：b64_json / url / revised_prompt
    usage: dict[str, Any] | None = None
    # 回显本次实际使用的种子：n=1 时为整数；n>1（非 batch）时为整数列表（每项对应一条视频）。
    # 不传 seed 时网关随机生成，这里回显随机出的实际值，便于复现。
    seed: int | list[int] | None = None
    # 回显已接入的「image 类」参考素材（按 response_format 输出 url / path / b64_json）。
    # 仅当请求携带 reference_images 时存在；reference_videos / reference_audios 仅接入不回显。
    references: list[dict[str, Any]] | None = None
    # 同 ImageResponse.task_id：本次同步请求在网关任务表里的 id，可交给 pin_view_item 反查产物。
    task_id: str | None = None


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "comfyui"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelCard]
