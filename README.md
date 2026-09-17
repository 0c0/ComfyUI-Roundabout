# ComfyUI-Roundabout

> 一个 ComfyUI **custom node**：把本地工作流包装成 **OpenAI 兼容 REST API** + **MCP 工具**（12 个），让 AI agent / 脚本 / 任意 OpenAI 客户端直接调用你本机的图像与视频生成。

**不碰画布、不改代码。** 你在 ComfyUI 里搭好的流程，导出一个 JSON、在 `models.yaml` 写一段参数映射，就变成了一个可被任意客户端调用的 `model`。

---

## 设计目的

**1. 让 ComfyUI 成为 agent 的 AIGC 后端**

ComfyUI 已经有模型、显存和队列，缺的只是一层 agent 能听懂的接口。Roundabout 把它包成 **OpenAI 兼容 REST**（`base_url` 指过来即可，任何 OpenAI SDK / 客户端零改动接入）和 **MCP 服务**（agent 直接调工具）。本地算力因此变成一个私有、按次调用不计费的图像 / 视频后端。

**2. 嵌入 ComfyUI，无需额外维护**

它是一个 **custom node**，不是一套要单独部署的服务：

- 随 ComfyUI 启动自动加载、随 ComfyUI 退出而结束，**没有独立进程**
- MCP 端点挂在 ComfyUI 自己的端口（`/mcp`），**不额外占端口**
- 复用 ComfyUI 的 Python 解释器与依赖（纯 REST 场景零新增依赖）
- 配置只有 `models.yaml`（模型）与 `.env`（可选）两个文件，改完热加载

没有 docker、没有守护进程、没有第二份 registry 要同步。

**3. 不把 workflow 交给 agent，精准传参省 token**

agent 全程**看不到也用不着**工作流 JSON。它只传语义参数，节点字段的写入由网关在本地按 `models.yaml` 的映射完成。

| | 通用 ComfyUI MCP（把 workflow 整个交给 agent 操作） | Roundabout |
|---|---|---|
| 每次调用携带 | 整份 workflow JSON（或长期占用上下文） | `model` + `prompt` + `size` 等语义参数 |
| 参数名从哪来 | 靠节点 schema / 试错 | 网关固定的 18 个参数白名单 |
| 换模型 | 重新理解另一张图 | 换一个 `model` 字符串 |
| workflow 存哪 | agent 上下文里 | `workflows/` 本地，**永不进上下文** |
| 出错面 | 节点 id / 字段名 / 连线都可能被改坏 | 映射在启动时校验，路径不存在直接拒绝加载 |

本仓库自带工作流的实测体积（按 字符÷3 粗估 token）：

| 工作流 | 节点数 | 字符 | ≈tokens |
|---|---|---|---|
| `image_z_image_turbo` | 10 | 3 209 | ~1.1k |
| `image_flux2_klein_image_edit_turbo` | 19 | 4 244 | ~1.4k |
| `video_minimax_h3` | 33 | 7 751 | ~2.6k |

对应的 Roundabout 调用：

```json
{"model":"z-image-turbo","prompt":"一只戴宇航头盔的柴犬","size":"1024x1024"}
```

约 30 tokens —— **单次生成请求携带的载荷差 40~90 倍**，且工作流越复杂（视频、多参考图）差距越大。

---

## 文档分工

| 文档 | 内容 |
|---|---|
| **README.md**（本文件） | 这个插件是什么、能做什么、怎么装、怎么用 |
| **[WORKFLOWS.md](WORKFLOWS.md)** | 接入**你自己的**工作流：参数映射写法、`models.yaml` 字段、参考样本、排错表 |
| **[API.md](API.md)** | 完整接口契约：REST 端点与字段、MCP 工具、响应结构、鉴权、全部环境变量、排障 |

---

## 功能

**两套接入层，共享同一个引擎**

- **OpenAI 兼容 REST**：`/v1/images/generations`（文生图 / 图生图）、`/v1/images/edits`（multipart 标准编辑）、`/v1/images/remove-background`（去背景）、`/v1/videos/generations`（视频，支持异步）。返回格式可选 `b64_json` / `url` / `file` / `path`，可直接替换 OpenAI 官方地址使用。
- **MCP 服务（12 个工具）**：生成类 `generate_image` / `edit_image` / `remove_background` / `generate_video`，查询类 `list_models` / `get_task` / `cancel_task` / `queue_status` / `get_workflow` / `health`，运维类 `reload` / `get_view_url`。agent 用一组工具就能完成「查模型 → 生成 → 跟踪进度 → 拿产物」全流程。
- **共享端口**：MCP 端点 `/mcp` 直接挂在 ComfyUI 同一端口（`http://<comfyui>:8188/mcp`），不用额外开端口、不用另起进程；REST 与 MCP 共用同一份注册表、生成链路与任务表。

**声明式模型注册**

- `models.yaml` 登记模型：工作流文件 + 参数绑定路径 + 默认值 + 别名 + 能力（文生图 / 图生图 / 视频）。
- 新增、修改模型只改 YAML，`POST /admin/reload` 热加载，**不用重启 ComfyUI、不用写代码**。
- 内置 15 个开箱可用的工作流（11 图像 + 4 视频），也全部可以作为你写映射时的参照。

**看得见、管得了**

- **可视化页面** `http://<comfyui>:8188/roundabout/view`：浏览 `input/` `output/` 资源（缩略图分批懒加载、自适应分页、产物自动同步），右下角悬浮任务面板实时显示 ComfyUI 队列 + 网关任务表，产物一键弹层预览。
- **ComfyUI 内管理面板**：菜单「Roundabout」提供工作流管理（上传 / 校验 / 删除）、模型配置（结构化编辑）、队列监控（含 seed，便于区分批量提交）。
- **任务留痕**：同步与异步生成都记一条（状态 / 耗时 / seed / 产物地址），终态记录保留 6 小时。

---

## 安装

**方式一 · 用 ComfyUI-Manager 从 Git URL 安装**（推荐）——装好 [ComfyUI-Manager](https://github.com/ltdrdata/ComfyUI-Manager) 后，打开 **Custom Nodes Manager**，右上角菜单选 **Install via Git URL**，填入：

```
https://github.com/0c0/ComfyUI-Roundabout
```

装完点 **Restart**。节点包已按 Comfy Registry 规范声明元数据（`pyproject.toml`），ComfyUI-Manager 能从中识别名称与版本；因为是 git 仓库，之后也可以直接在 Manager 里检查/拉取更新。

> 本仓库**未发布到 Comfy Registry**，所以搜索框里搜不到、也没有 `comfy node install` 这条命令，请走上面的 Git URL 安装。

**方式二 · 手动克隆**：

1. 放进 ComfyUI 的 `custom_nodes/` 目录：

   ```bash
   cd ComfyUI/custom_nodes
   git clone https://github.com/0c0/ComfyUI-Roundabout.git
   ```

2. 安装依赖 —— **MCP 网关默认开启，所以默认需要这一步**：

   ```bash
   <ComfyUI>/python/python.exe -m pip install -r custom_nodes/ComfyUI-Roundabout/requirements.txt
   # 即 mcp>=2.0.0（必须 2.x，1.x 的 FastMCP API 不兼容） + uvicorn>=0.30.0
   ```

   > 只要 REST、不要 MCP：在 `.env` 写 `MCP_ENABLED=false`，这两个包可以不装。

3. 配置（可选）：把 `.env.example` 复制成 `.env`，按需修改。**MCP 默认已启用**，不用额外设置；要关掉就把 `MCP_ENABLED` 改成 `false`。

4. 准备模型权重：内置工作流用到的文件见下方[权重清单](#权重清单内置工作流的全部依赖)，**仓库不含权重**。

5. 重启 ComfyUI。启动日志出现 `OpenAI gateway routes registered ... models=...` 即成功。

---

## 使用方式

### 1. ComfyUI 面板（人在 ComfyUI 里操作）

菜单栏 **Roundabout** →

| 页签 | 用途 |
|---|---|
| 工作流管理 | 列出 `workflows/` 下所有 JSON 与校验状态；**上传自己的 API 格式工作流**（可勾选自动生成模型条目） |
| 模型配置 | 结构化编辑 `models.yaml`（改绑定、默认值、别名、能力），保存即校验，坏配置不会落盘 |
| 队列监控 | ComfyUI running / pending + 网关任务表，每条含 seed 与耗时 |

### 2. REST 接口（脚本 / OpenAI 客户端）

> 下面是四个最常用的最小示例；完整字段表、响应结构与错误码见 **[API.md](API.md)**。

**文生图**

```bash
curl -X POST http://127.0.0.1:8188/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"一只戴宇航头盔的柴犬，赛博朋克霓虹灯背景","size":"1024x1024","response_format":"path"}'
```

**编辑图片**（换背景 / 换材质 / 图内写字）

```bash
curl -X POST http://127.0.0.1:8188/v1/images/edits \
  -F model=flux2-klein-image-edit-turbo \
  -F 'prompt=把背景换成星空，保留主体不变' \
  -F response_format=path \
  -F image=@input/source.png
```

**去背景**（透明 PNG，无需提示词）

```bash
curl -X POST http://127.0.0.1:8188/v1/images/remove-background \
  -F image=@input/source.png -F response_format=path
```

**视频**（长任务建议异步，再轮询任务）

```bash
# 提交
curl -X POST http://127.0.0.1:8188/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"minimax-h3-turbo","prompt":"a cat walking in rain","duration":5,"background":"pending"}'
# 查结果（id 来自上一步返回）
curl http://127.0.0.1:8188/v1/videos/tasks/<id>
```

`size` 支持档位预设 `<tier>p-<ratio>`：tier ∈ `480p` / `720p` / `768p` / `1080p`，ratio ∈ `1:1` / `3:4` / `4:3` / `16:9` / `9:16`（如 `768p-16:9` = 1360×768、`1080p-16:9` = 1920×1088）；也接受反向写法 `<ratio>@<tier>p`（如 `9:16@768p`）与直接 `WxH`。不传或 `auto` 用模型默认。

### 3. MCP（给 agent 用）

**MCP 默认启用**——装好依赖、重启 ComfyUI，端点就在 `http://<comfyui>:8188/mcp`，无需任何配置。

**Streamable HTTP**（推荐，走 ComfyUI 同端口，无独立进程）：

```json
{
  "mcpServers": {
    "comfyui-roundabout": {
      "type": "streamable_http",
      "url": "http://127.0.0.1:8188/mcp"
    }
  }
}
```

**stdio**（由 MCP 客户端拉起进程，适合不支持 HTTP 的客户端）：

```json
{
  "mcpServers": {
    "comfyui-roundabout": {
      "command": "<ComfyUI>/python/python.exe",
      "args": ["<ComfyUI>/custom_nodes/ComfyUI-Roundabout/mcp_server.py"]
    }
  }
}
```

> URL 里的主机端口 = 你访问 ComfyUI 的地址，路径固定 `/mcp`。远程访问换成 `http://192.168.1.10:20003/mcp` 即可，其余不变。
> 工具参数、完成推送（`notifications/message`，需显式 `MCP_STATELESS=false`）与助手侧注意事项见 [API.md §7](API.md)。

**agent 典型流程**：`list_models` 看有什么 → `generate_image` / `generate_video` 生成 → 异步任务用 `get_task` 轮询（默认无状态模式；设了 `MCP_STATELESS=false` 才可等服务端推送）→ 产物地址交给用户 → `get_view_url` 给出可视化页面。

### 4. 可视化页面

浏览器打开 `http://<comfyui>:8188/roundabout/view`（MCP 工具 `get_view_url` 也会返回这个地址）：

- 浏览 `input/` 与 `output/`（按修改时间倒序、自适应分页、缩略图分批懒加载）
- 有新产物自动提示刷新，不打断你正在看的图
- 右下角「任务进度」悬浮面板：ComfyUI 队列 + 网关任务 + 一键查看产物

---

## 内置模型

默认 `default_model = z-image-turbo`。`model` 可传注册名或任一别名（不区分大小写）；不传则用默认模型。

| 类型 | 模型 | 用途 |
|---|---|---|
| 文生图 | `z-image-turbo` | 8 步快速（默认） |
| 文生图 | `z-image` | 30 步高质量 |
| 文生图 | `boogu-image-turbo` / `boogu-image-base-4step` | 4 步极速预览 |
| 文生图 | `boogu-image-base` | 30 步高质量 |
| 文生图 | `mage-flow-base` / `mage-flow-turbo` | MageFlow 30 步 / 4 步 |
| 图像编辑 | `flux2-klein-image-edit-turbo` | 语义改写首选：换背景 / 换材质 / 增删物体（`edit_image` 默认） |
| 图像编辑 | `boogu-image-edit` / `boogu-image-edit-turbo` | 擅长改写 / 添加**图内文字**，30 步 / 6 步 |
| 图像工具 | `utility-birefnet-remove-background` | BiRefNet 抠图，输出透明 PNG（无提示词） |
| 视频 | `minimax-h3` / `minimax-h3-edit` | MiniMax H3（base 30 步 / edit 25 步），支持 6 图 + 3 视频 + 3 音频参考 |
| 视频 | `minimax-h3-turbo` / `minimax-h3-turbo-edit` | 8 步快速版 |
| 视频 | `minimax-h3-self-lift` | SelfLift 渐进采样（低分辨率 NFE + 高分辨率 NFE）；文生 / 首尾帧生视频靠 `reference_images` 插拔；分块参数按本机显存自动分档；`motion=story/fight` 一键切文戏 / 打戏步数档（默认 `story`） |
| 视频 | `minimax-h3-self-lift-edit` | 同上，改用 Ref2VA 权重，参考槽保留全套 6 图 + 3 视频 + 3 音频 |

- 编辑类模型**必须传 `image`**；输出尺寸跟随输入图（工作流内缩放到 1MP），`size` 不生效。
- 给文生图模型传 `image` 会被拒绝，错误信息里会列出所有支持输入图的模型名。
- 别名、绑定路径与实测耗时见 `models.yaml` 与 [API.md §5.4](API.md)。

---

## 权重清单（内置工作流的全部依赖）

**本仓库不包含任何权重文件**（体积与许可原因）。内置工作流引用的 26 个文件如下，放到 ComfyUI 对应目录即可；缺文件时报错是 `value not in list: <字段>: <文件名>`。

| 工作流 | 需要的权重 → 目标目录 |
|---|---|
| `z-image` / `z-image-turbo` | `diffusion_models/` `z_image_int8_convrot.safetensors`、`z_image_turbo_int8_convrot.safetensors` · `text_encoders/` `qwen_3_4b.safetensors` · `vae/` `ae.safetensors` |
| `boogu-image-base` / `-base-4step` / `-turbo` | `diffusion_models/` `boogu_image_base_fp8_scaled.safetensors`、`boogu_image_turbo_hotfix_int8_convrot.safetensors` · `loras/` `boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux1_vae_bf16.safetensors` |
| `boogu-image-edit` / `-edit-turbo` | `diffusion_models/` `boogu_image_edit_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `ae.safetensors` ·（turbo 另需）`loras/` `boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors` |
| `flux2-klein-image-edit-turbo` | `diffusion_models/` `flux-2-klein-9b-kv-fp8.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux2-vae.safetensors` |
| `mage-flow-base` / `mage-flow-turbo` | `diffusion_models/` `mage_flow_int8_convrot.safetensors`、`mage_flow_turbo_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_4b_bf16.safetensors` · `vae/` `mage_flow_vae_bf16.safetensors` |
| `utility-birefnet-remove-background` | `background_removal/` `birefnet.safetensors` |
| `minimax-h3` / `minimax-h3-edit` | `diffusion_models/` `minimax_h3_fl2va_int8_convrot.safetensors`、`minimax_h3_ref2va_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` · `vae/` `minimax_h3_video_vae_fp16.safetensors`、`minimax_h3_audio_vae_fp32.safetensors` |
| `minimax-h3-turbo` / `minimax-h3-turbo-edit` | 同上，另需 `loras/` `MiniMax-H3-FL2VA-Acc-8Step.safetensors` 或 `MiniMax-H3-Ref2VA-Acc-8Step.safetensors` |
| `minimax-h3-self-lift` | 同上，另需 `loras/` `minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors` · `latent_upscale_models/` `minimax_h3_latent_upscaler_3d_fp16.safetensors` |
| `minimax-h3-self-lift-edit` | 同上，另需 `loras/` `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` · `latent_upscale_models/` `minimax_h3_latent_upscaler_3d_fp16.safetensors` |

> 这些工作流用到的节点**除 SelfLift 两支（`-self-lift` / `-edit`）外全部来自 ComfyUI 核心**（`comfy_extras/`），不需要装任何第三方 custom node 包；ComfyUI 版本太老会缺 `MiniMaxH3ReferenceToVideo` / `LoadBackgroundRemovalModel` / `Flux2Scheduler` 等节点。
> SelfLift 那四支额外依赖两个第三方节点包：`comfyui-SelfLift`（`SelfLiftH3Sampler` + `latent_upscale_models/` 目录下的上采样权重）与 `ComfyUI-KJNodes`（`MiniMaxChunkFeedForward` / `MiniMaxLowVRAMAttention`）。
> 上述权重多为 `int8_convrot` 量化版，只在你已具备同名权重的机器上开箱即用；换成自己的模型时，同步改工作流 JSON 里的文件名即可。

---

## 接入自己的工作流

内置工作流只是样本，随时可以删。接入自己的流程 = **导出 API 格式 JSON** + **在 `models.yaml` 写一段参数映射**，支持热加载，不用改代码。

三步：

1. ComfyUI 画布 → `Workflow` → **`Export (API)`**（必须是 API 格式，不是普通 Export）→ 存进 `workflows/`
2. 在 `models.yaml` 加一条：`workflow` / `mode` / `capabilities` / `output_node` / `bindings`（接口参数 → 节点路径）
3. `POST /admin/reload` 或面板点「重新加载」，然后打一发接口验证

**完整指南见 [WORKFLOWS.md](WORKFLOWS.md)** —— 参数白名单、常见落点表、找路径的方法、三条铁律（尤其：**别绑被连线覆盖的 input**）、`models.yaml` 全字段、`references` 段、排错表。

最小可跑参考样本：`workflows/example_txt2img.json`（CheckpointLoaderSimple + KSampler + SaveImage，把 `ckpt_name` 换成你自己的即可）。
另有三种落地路径（面板 / REST / Agent）的逐步说明。

---

## 配置

完整环境变量清单见 **[API.md §2.3](API.md)**，常用项：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MCP_ENABLED` | `true` | 嵌入式 MCP 网关（默认开；设 `false` 关掉，同时可省掉 `mcp`/`uvicorn` 依赖） |
| `MCP_SHARE_PORT` | `true` | MCP 挂到 ComfyUI 同端口（`/mcp`）；`false` 则用 `MCP_PORT` 独立端口 |
| `MCP_PORT` | `0` | 内部回环后端端口；留空/`0` = 系统分配空闲端口（实际端口见启动日志） |
| `MCP_PORT_MAP` | 空 | 固定端口映射，按「ComfyUI 端口 → Roundabout 端口」成对写：`[8188,888],[8189,999]`。同机多开时按各实例的 `--port` 取各自端口；未命中回落系统分配；设了 `MCP_PORT` 则以其为准 |
| `MCP_STATELESS` | `true` | 无状态 MCP（默认）：请求独立处理、不跟踪会话，**ComfyUI 随便重启 agent 都不用重连**。设 `false` 才换回有状态模式，也就是要「异步任务完成通知」的推送通道——代价是重启后旧会话全失效，客户端没重新握手前所有调用都报错 |
| `DEFAULT_MODEL` | `models.yaml` 内值 | 默认模型（优先级高于 YAML） |
| `OPENAI_GATEWAY_API_KEYS` | 空 | 填了才启用鉴权（逗号分隔多 key） |
| `PUBLIC_BASE_URL` | 空 | 产物 `url` 绝对化基准，**远程部署必填** |
| `MAX_CONCURRENCY` | `2` | 并发生成上限 |
| `JOB_TIMEOUT` / `OUTPUT_TTL` | `300` / `3600` | 任务超时（秒，`0`=关闭上限仅由 ComfyUI 状态判定）/`url` 产物存活期（秒） |
| `ROUNDABOUT_VRAM_GB` | 空 | 手动钉住显存档位（GiB），用于按显卡自动调参的模型（如 `minimax-h3-self-lift`）；不填则自动探测，探测不到就不覆盖工作流自带的值 |

### 按显卡自动调参（`vram_adaptive`）

大模型工作流（SelfLift 权重合计 ~65 GiB）靠节点级分块在显存吃紧的卡上跑，而分块参数的合适取值只取决于显存大小。在 `models.yaml` 给模型加一行 `vram_adaptive: true`，网关启动时探测本机显存，从 `defaults.vram_tiers` 取「`min_gb` 不超过本机显存」的最大一档，作为该模型分块参数的默认值：

```yaml
defaults:
  vram_tiers:
    - min_gb: 24        # 24 GiB 及以上
      chunks: 2
      head_chunks: 8
      seq_threshold: 16384
      highres_tiling: true
    # ... 12 / 8 / 0 各档
models:
  minimax-h3-self-lift:
    vram_adaptive: true
    bindings:
      chunks: 219.inputs.chunks
      head_chunks: 220.inputs.head_chunks
      seq_threshold: 219.inputs.seq_threshold
      highres_tiling: 235.inputs.highres_tiling
```

- 优先级：**档位值 < 模型自己写的 `defaults` < 请求参数**（请求里传 `chunks` 等可按单次任务覆盖）。
- 探测不到显存（纯 CPU / 无 torch）时不覆盖，行为与不声明 `vram_adaptive` 一致。
- 档位表在 YAML 里，改档位不用动代码；`ROUNDABOUT_VRAM_GB` 可手动钉住。

### 按剧情节奏切档（`motion_presets`）

SelfLift 那两支有个经验值：**文戏的总步数要调低、过渡步跟着调低；打戏两个都调高**——两个数必须成对改，只动一个容易出问题。于是把它们打包成命名档，请求里写一个词就行：

```json
{"model": "minimax-h3-self-lift", "prompt": "...", "duration": 5, "motion": "story"}
```

| `motion` | 总步数（`124.steps`） | 过渡步（`235.transition_step`） | 适用 |
|---|---|---|---|
| `story` | 6 | 5 | 文戏：对话、静态、慢动作（**默认**） |
| `fight` | 8 | 6 | 打戏：奔跑、追逐、快节奏 |
| 不传 | 6 | 5 | 等同 `story` |

- 默认档由 `defaults.motion: story` 声明——只写档名、不重复抄数值，改档表即改默认，两处不会漂移。
- 工作流模板里的 `steps` / `transition_step` 字面值也同步成默认档的 6 / 5（在画布上手跑就是文戏档）；`tests/test_motion_presets.py` 会断言「档表 = `defaults` = 模板」三者一致。
- 档位表写在 `models.yaml` 的 `motion_presets` 里，改档不用动代码；别的模型想加同款机制，照样声明一份即可。
- 要精调时直接传底层参数，**显式入参优先于命名档**：`{"motion": "story", "steps": 9}` → 9 / 5。
- 档位只有 `minimax-h3-self-lift` 与 `-self-lift-edit` 声明（只有 `SelfLiftH3Sampler` 有「过渡步」这个概念）；30 步的 `-max` 那两支是质量档、不切档。给没有档位的模型传 `motion` 会直接报错并提示不支持，不会静默忽略。
- 不想记节点号又不想加档位时，也可以直接点名改节点：`workflow_overrides: {"124.inputs.steps": 6, "235.inputs.transition_step": 5}`。

```yaml
models:
  minimax-h3-self-lift:
    defaults:
      motion: story        # 默认档（等同 6 / 5）
      steps: 6             # 与 story 档同值，仅兜底
      transition_step: 5
    motion_presets:
      story:
        steps: 6
        transition_step: 5
      fight:
        steps: 8
        transition_step: 6
    bindings:
      transition_step: 235.inputs.transition_step
```

### 同机跑多个 ComfyUI 实例

Roundabout 随 ComfyUI 进程启动，每个实例都有自己的 MCP 后端。如果想让端口可预期（防火墙放行、日志好认），用映射表把「ComfyUI 端口 → Roundabout 端口」一一对上：

```env
MCP_PORT_MAP=[8188,888],[8189,999]
```

于是 `--port 8188` 的实例用 `888`、`--port 8189` 的实例用 `999`，互不干扰。不配映射也不会冲突（自动分配空闲端口），配了映射更可控；映射端口恰好被别的程序占用时会自动回落到空闲端口并在日志里告警，端点不会因此失效。

---

## 目录结构

```
ComfyUI-Roundabout/
├── __init__.py            # ComfyUI 节点入口（启动网关、加载模型）
├── mcp_server.py          # MCP 服务（12 工具）+ 共享端口嵌入启动
├── gateway/               # REST 网关
│   ├── config.py          # 配置（.env / 环境变量）
│   ├── registry.py        # models.yaml 解析、绑定校验、热加载
│   ├── pipeline.py        # 生成链路（参数校验 → 工作流渲染 → 提交 → 收集产物）
│   ├── handlers.py        # REST 端点
│   ├── routes.py          # 路由表
│   ├── admin.py           # 工作流 / 模型配置管理端点
│   ├── viewer.py          # 资源浏览与任务进度页面后端
│   ├── analyze.py         # 上传工作流时的参数映射自动分析
│   ├── tasks.py           # 任务表
│   ├── vram.py            # 显存探测 + 低显存分块档位选取（vram_adaptive）
│   ├── log_filters.py     # 把 aiohttp「客户端断开」的 ERROR 降级为 DEBUG
│   └── ...
├── web/                   # 前端（可视化页面 + 设置面板）
├── workflows/             # API 格式工作流（example_txt2img.json 为接入样本）
├── models.yaml            # 模型注册表
├── pyproject.toml         # 节点包元数据（供 ComfyUI-Manager 等抓取识别）
├── requirements.txt       # mcp + uvicorn（MCP 默认启用故默认需要；关掉 MCP 可不装）
├── tests/                 # 回归测试（run_tests.py 为入口；分发时由 .comfyignore 排除）
├── WORKFLOWS.md           # 接入自己的工作流
└── API.md                 # 完整接口文档
```

---

## 测试

```bash
<ComfyUI>/python/python.exe tests/run_tests.py                # 全量离线自测（默认跳过会驱动 ComfyUI 的用例）
<ComfyUI>/python/python.exe tests/run_tests.py --list         # 只列出将执行 / 跳过的文件
<ComfyUI>/python/python.exe tests/run_tests.py test_vram_adaptive.py   # 跑单个文件
<ComfyUI>/python/python.exe tests/run_tests.py --all          # 连 GPU 用例一起（会真出图、真去背景）
node tests/test_view_frontend.cjs                             # 前端 jsdom（需 node + jsdom）
```

**用例分两类**，别用 `for f in tests/test_*.py` 一把梭——那会把下面两个也扫进去，它们会**真的占用 GPU 并落产物**：

| 用例 | 行为 |
|---|---|
| `test_e2e_sync_generation.py` | 真提交一次生图（z-image-turbo 512x512，约 10–30s GPU） |
| `test_removebg_e2e.py` | 真跑 BiRefNet 去背景 |

`tests/run_tests.py` 默认跳过它们（名字含 `e2e`，或列在脚本顶部的 `GPU_TESTS` 里），要跑得显式加 `--all`。
其余用例全部离线、用系统分配的临时端口，不需要 ComfyUI 在跑。

## 许可

[MIT](LICENSE)
