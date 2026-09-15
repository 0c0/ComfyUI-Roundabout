# ComfyUI-Roundabout 接口文档

> 一份干净的接口契约。**REST 网关** 与 **MCP 网关** 两套接入层，共享同一份 `models.yaml` 注册表、同一套生成链路、同一个异步任务表。
>
> 当前版本：`1.0.0` ｜ 网关即 ComfyUI 自身（custom node），随 ComfyUI 启动自动加载。
>
> 用法与安装见 [README.md](README.md)；接入自己的工作流见 [WORKFLOWS.md](WORKFLOWS.md)。

---

## 1. 架构总览

```
                          ┌──────────────────────────────────────────────┐
   REST 客户端  ─────────▶│  ComfyUI PromptServer (aiohttp, 端口 8188)    │
   (curl / OpenAI SDK)    │   /v1/* , /roundabout/* , /health            │
                          │                                              │
   MCP 客户端   ──/mcp──▶ │   aiohttp 原生代理 (MCP_SHARE_PORT=true)       │
                          │        │  转发 POST/GET(SSE)/DELETE          │
                          │        ▼                                     │
                          │   内部 uvicorn (127.0.0.1, streamable-http)   │
                          │        端口由系统分配或按映射表指定          │
                          │        = MCP Server (12 tools)               │
                          └───────────────┬──────────────────────────────┘
                                          │ 复用
                          ┌───────────────▼──────────────────────────────┐
                          │  gateway/ : registry / pipeline / task_store  │
                          │  ComfyClient ──▶ 本机 ComfyUI 后端 (/prompt)   │
                          └───────────────────────────────────────────────┘
```

- **REST 网关**：注册在 ComfyUI 自己的 aiohttp 上，监听 ComfyUI 端口（默认 `8188`）。
- **MCP 网关（嵌入）**：在 ComfyUI 进程内以 `streamable-http` 起一个 uvicorn 后端。后端端口按 `MCP_PORT` → `MCP_PORT_MAP` → 系统分配的顺序解析（见 §2.3）——同一台机器同时跑多个 ComfyUI 实例（各自 `--port` 不同）时，各实例的 MCP 后端各占各的端口，不会互抢；配置的端口被别的程序占用时会回落到空闲端口并在日志告警；实际端口在监听就绪后写入启动日志。
  - `MCP_SHARE_PORT=true`（默认）：通过 aiohttp 原生代理把 `/mcp` 挂在 **ComfyUI 同一端口**（`http://host:8188/mcp`）。内部 uvicorn 只绑 `127.0.0.1` 回环。
  - `MCP_SHARE_PORT=false`：客户端直连 `http://MCP_HOST:<MCP_PORT>/mcp`；此时建议用 `MCP_PORT` 或 `MCP_PORT_MAP` 把端口钉死（对外可预期），否则端口由系统分配、以启动日志为准。
- **共用**：模型注册表、生成链路、异步任务表全部共享。通过 MCP 提交的异步任务能在 REST 队列监控里看到，反之亦然。

---

## 2. 部署与配置

### 2.1 Python 依赖（换机器部署必看）

ComfyUI 自带依赖只含 `aiohttp` / `pydantic` / `pyyaml`，**不含 `mcp` 和 `uvicorn`**。这两个包**默认就需要装**（MCP 网关默认开启），否则启动日志报：

```
[Roundabout] MCP gateway: missing dependency 'mcp' in the Python that runs ComfyUI (<...>/python.exe).
Fix with: "<...>/python.exe" -m pip install -r "<...>/ComfyUI-Roundabout/requirements.txt"   (or run install.py from this node folder).
```

> 级别是 **WARNING**：MCP 是「默认开启但非必需」，缺依赖时会被自动跳过，REST 网关照常工作。
> 若你在 `.env` 里显式写了 `MCP_ENABLED=true`（即明确要求启用），同样的内容会以 **ERROR** 打出。

用**运行 ComfyUI 的那个解释器**安装（装到系统 Python 无效）：

```bat
:: <ComfyUI 根> = ComfyUI 安装目录（ComfyUI-aki 便携包形如 X:\...\ComfyUI-aki-v3，
:: 含 python\python.exe 与 ComfyUI\ 两层）
cd /d <ComfyUI 根>
python\python.exe -m pip install mcp uvicorn
:: 若 ComfyUI 用的是系统 Python，改成：python -m pip install mcp uvicorn
```

或一次性按 `requirements.txt` 安装（等价，推荐）：

```bat
python\python.exe -m pip install -r ComfyUI\custom_nodes\ComfyUI-Roundabout\requirements.txt
:: 也可直接执行节点目录下的 install.py（ComfyUI Manager 会在安装节点时自动调用）
```

| 包 | 版本要求 | 说明 |
|---|---|---|
| `mcp` | `>=2.0.0` | **必须 2.x**。代码用 `mcp.server.MCPServer` + `streamable_http_app`，1.x（FastMCP）API 不兼容 |
| `uvicorn` | `>=0.30.0` | `mcp 2.0.0` 已将其列为依赖会自动带上，因 `mcp_server.py` 直接 import 故显式声明 |

> 只要 REST、不启用 MCP（`.env` 里 `MCP_ENABLED=false`）就不需要这两个包。
> 网络超时可加镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`。
> 装完需重启 ComfyUI。

### 2.2 启动 ComfyUI

```bat
"<ComfyUI 根>\python\python.exe" "<ComfyUI 根>\ComfyUI\main.py" ^
  --auto-launch --preview-method auto --disable-cuda-malloc --use-ck-attention
```

节点在 ComfyUI 启动时自动：加载 `models.yaml` → 注册 `/v1/*` 路由 → 拉起 MCP 后端并挂载代理（`MCP_ENABLED` 默认 `true`；设为 `false` 则跳过）。

> **MCP 地址 = ComfyUI 自身的地址 + `/mcp`**，没有独立端口。ComfyUI 跑在 `192.168.1.10:20003`（经 `--port 20003` 或反代），MCP 端点就是 `http://192.168.1.10:20003/mcp`。
> 共享端口模式下内部后端不对外暴露——它只绑 `127.0.0.1` 回环，端口由 `MCP_PORT` / `MCP_PORT_MAP` 决定（默认交给系统分配），客户端一律只连 ComfyUI 端口。

### 2.3 节点根目录 `.env`（手写最小 dotenv，仅补充未设置的环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MCP_ENABLED` | `true` | 嵌入式 MCP 网关（**默认开**）；设 `false` 关闭，同时可省掉 `mcp`/`uvicorn` 依赖 |
| `MCP_SHARE_PORT` | `true` | true=挂到 ComfyUI 同端口；false=独立端口直连 |
| `MCP_PORT` | `0` | 内部 uvicorn 回环端口；留空/`0` = 由系统分配空闲端口 |
| `MCP_PORT_MAP` | 空 | 固定端口映射，成对书写「ComfyUI 端口,Roundabout 端口」，如 `[8188,888],[8189,999]`；按本实例的 `--port` 取值，未命中回落系统分配；**优先级低于 `MCP_PORT`** |
| `MCP_PATH` | `/mcp` | MCP 端点路径 |
| `MCP_HOST` | `127.0.0.1` | `MCP_SHARE_PORT=false` 时的绑定地址 |
| `MCP_STATELESS` | `false` | **无状态模式**：每个 MCP 请求独立处理、不跟踪会话，ComfyUI 重启后 agent 无需重新 initialize（根治 `unknown or expired session ID`）。代价：异步任务的完成通知**没有推送通道**（通知挂在会话上），客户端改为轮询 `GET /v1/videos/tasks/{id}`。SDK 原生 `stateless_http`，无自研逻辑 |
| `ROUNDABOUT_RAW_AIOHTTP_LOGS` | 空 | 设 `true` 关闭 aiohttp「客户端断开」日志降噪（见 §10 排障），恢复 aiohttp 原始 ERROR |
| `ROUNDABOUT_VRAM_GB` | 空 | 手动钉住显存档位（GiB），用于 `vram_adaptive: true` 的模型（如 `minimax-h3-self-lift`）；不填则启动时自动探测，探测不到就不覆盖工作流自带的分块参数 |
| `COMFY_BASE_URL` | 自动取 ComfyUI `--listen/--port` | 覆盖后端指向（指向另一个 ComfyUI 实例） |
| `COMFY_HTTP_TIMEOUT` | `30` | 后端请求超时（秒） |
| `OPENAI_GATEWAY_API_KEYS` | 空 | 逗号分隔的 Bearer key；**为空则鉴权自动放行** |
| `OPENAI_GATEWAY_AUTH_REQUIRED` | `true` | 是否要求鉴权（仍受 API_KEYS 是否为空约束） |
| `PUBLIC_BASE_URL` | 空 | 产物 `url` 绝对化基准；为空则回落请求 host / `comfy_base_url` |
| `MODELS_FILE` / `WORKFLOWS_DIR` | `models.yaml` / `workflows/` | 模型与工作流位置 |
| `DEFAULT_MODEL` | 取 yaml 内值 | 默认模型 |
| `MAX_CONCURRENCY` | `2` | 并发生成上限 |
| `JOB_TIMEOUT` / `JOB_GRACE` | `300` / `300` | 同步生成超时（秒）+ 宽限。`JOB_TIMEOUT=0` 关闭时间上限，任务是否健康完全由 ComfyUI 队列/历史状态判定（视频等长任务推荐） |
| `POLL_INTERVAL` / `POLL_INTERVAL_MAX` | `1` / `3` | 后端轮询间隔（秒）与退避上限 |
| `OUTPUT_DIR` / `OUTPUT_TTL` | `.cache/outputs` / `3600` | `url` 产物落盘目录与存活期（秒） |
| `MAX_N` | `4` | 单次请求最大张数 |
| `MAX_INPUT_IMAGE_MB` / `MAX_INPUT_ASSET_MB` | `20` / `200` | 输入图片 / 视频音频的体积上限（MB） |
| `AUTO_SPLIT_NEGATIVE` | `true` | 支持负向的模型自动把 `prompt` 内 `no/without/not X` 抽进 `negative_prompt` |

### 2.4 客户端接入（WorkBuddy / Claude Desktop 等）

MCP 客户端配置（`mcp.json`）：

```json
{
  "mcpServers": {
    "comfyui-roundabout": {
      "type": "streamable_http",
      "url": "http://127.0.0.1:8188/mcp",
      "disabled": false
    }
  }
}
```

> `url` 里的主机与端口就是 ComfyUI 的访问地址，路径固定 `/mcp`：
> - 本机默认：`http://127.0.0.1:8188/mcp`
> - 远程/自定义端口（如 ComfyUI 跑在 20003）：换成 `http://192.168.1.10:20003/mcp`，其余不变。
>
> 在连接器管理中对该服务器点「信任」后启用即可。

---

## 3. 鉴权

- **方式**：`Authorization: Bearer <key>`，或 `api-key` / `x-api-key` 头。
- **放行条件**：`OPENAI_GATEWAY_API_KEYS` 为空（或 `AUTH_REQUIRED=false`）时全放行；否则所有非公开路径必须带有效 key（常量时间比较，避免时序侧信道）。
- **公开路径**（无需 key）：`/health`、`/healthz`、`/`、`/docs`、`/openapi.json`、`/redoc`，以及 `/static*` 静态资源。
- 其余全部路径（含 `/v1/*`、`/admin/*`、`/roundabout/*`）受同一 `verify()` 约束。浏览器打开可视化页面无法带自定义头，页面支持 `?key=<api_key>` 查询参数（`get_view_url` 会自动把 key 拼进去）。
- MCP 接口同样受 `verify()` 约束。

---

## 4. 核心概念

- **模型与别名**：`model` 可传注册名或其任一 `aliases`（不区分大小写）。当前默认 `default_model=z-image-turbo`。
- **异步任务（仅视频）**：视频生成可带 `background:"pending"` 或 `async:true`，POST 立即返回 `task` 对象（`status:pending`），后台继续生成；用 `GET /v1/videos/tasks/{id}` 查结果。图片请求始终同步返回。
- **任务记录（同步 + 异步都记）**：**每次真正开工的生成请求都会在网关任务表留一条记录**——异步视频是「排队中 → 执行中 → 终态」，同步生图/编辑/视频则直接落终态，成功后附产物地址。这样 agent 用 MCP 或 REST 生成完，视图页上能看到痕迹与产物入口。**前提是请求通过了前置校验**（模型名写错、参数非法这类 400/404 在进入生成链路前就返回了，不留记录）。
- **记录保留期**：任务记录是进程内存态，终态记录保留 6 小时后自动回收（未完成任务不回收）；重启 ComfyUI 全部清空。
- **产物 url 绝对化**：`response_format=url` 时返回绝对 `http(s)` 地址。基准顺序：`PUBLIC_BASE_URL` → REST 用请求 host / MCP 用 `comfy_base_url`。若填 `b64_json` 则内联 base64；`file`/`path` 返回磁盘绝对路径。
- **seed 回显与监控**：生成响应回显本次实际生效 `seed`；队列监控的 running / pending / tasks 三表均带 `seed` 字段，便于区分批量同 prompt 提交。
- **取消语义**：pending 任务从 ComfyUI 队列移除；running 任务只能 `interrupt`（ComfyUI 无按 id 中断接口，会中断当前正在执行的那个）。本地任务标记为 `cancelled`，且 `complete/fail` 不再覆盖它（防后台协程冲掉）。
- **编辑模型（`image-to-image`）**：`boogu-image-edit` / `boogu-image-edit-turbo` 与 `flux2-klein-image-edit-turbo`（含别名 `flux2-edit-turbo`、`image_flux2_klein_image_edit_turbo`）**必须传 `image`**（不传返回 400 `requires an input \`image\``）。boogu 系列擅长**改写 / 添加图像内的文字**，prompt 里可用「图1」指代输入图；flux2 klein 擅长**语义改写**（换背景/材质、增删物体）。两者输出尺寸都跟随输入图（工作流内缩放到 1MP），因此 `size` 不生效；编辑模型均不继承全局默认负向提示词（全局负向含 text / watermark，会与写字的用途冲突）。
- **跨模型误用**：给文生图模型传 `image` 会被拒绝，错误信息里列出所有支持输入图的模型名，agent 可据此自助换模型。

---

## 5. REST API 参考

### 5.1 图像生成

#### `POST /v1/images/generations`

文生图 / 图生图。请求体（OpenAI 兼容 + ComfyUI 扩展字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `prompt` | string | 正向提示词（必填） |
| `model` | string? | 模型名/别名；空用默认；**视频模型会自动路由到视频链路** |
| `n` | int | 张数，1–`MAX_N`（默认 1） |
| `size` | string? | `"1024x1024"` / `"auto"` / 空=模型默认 |
| `quality` / `style` | string? | `low|medium|high|standard|hd|auto` / `vivid|natural`（映射为提示词后缀） |
| `response_format` | enum? | `b64_json` / `url` / `file` / `path` |
| `negative_prompt` | string? | 反向提示词 |
| `seed` | int? | 不传/`-1` 随机；`0` 与正整数固定 |
| `steps` / `cfg` / `sampler_name` / `scheduler` / `denoise` | 各类型? | 采样精调；`denoise` 为图生图重绘幅度 |
| `image` | string\|string[]? | 图生图输入（base64 / dataURL / URL / 本地路径） |
| `mask` | string? | 局部重绘遮罩 |
| `mode` | string? | 生图模式（部分模型，受 `mode_choices` 约束，如 Ideogram4） |
| `workflow_overrides` | object? | 直接改写节点，如 `{"3.inputs.cfg": 4.5}` |
| `filename_prefix` | string? | 落盘前缀，可含 `/` 建子目录；空则用该模型工作流模板里的前缀（H3 视频统一为 `video/MiniMax_H3`） |

**响应**（200）：

```json
{
  "created": 1700000000,
  "data": [ { "url": "http://host:8188/v1/images/files/xxxx.png", "revised_prompt": null } ],
  "seed": 123456789,
  "usage": null
}
```

#### `POST /v1/images/edits`

OpenAI 标准 multipart 图生图。字段：`image`（文件，可多张）、`mask`（文件，可选）、`prompt`、`model`、`n`、`size`、`quality`、`response_format`、`user`、`negative_prompt`、`seed`、`steps`、`cfg`、`denoise`。

**单图编辑模型**（改图内文字首选 boogu 系列；风格/内容改写首选 flux2 klein）：

| 模型 | 别名 | 默认步数 / cfg / 采样器 | 实测算力（1MP 输入，本机 4090 级） |
|---|---|---|---|
| `boogu-image-edit` | `boogu-edit` | 30 / 3.5 / `dpmpp_2m` + `simple` | 约 3.5 分钟 |
| `boogu-image-edit-turbo` | `boogu-edit-turbo` | 6 / 1 / `euler` + `sgm_uniform` | 约 45 秒 |
| `flux2-klein-image-edit-turbo` | `flux2-edit-turbo` | 6 / 1 / `euler` | 约 40 秒 |

```bash
curl -X POST http://127.0.0.1:8188/v1/images/edits \
  -F model=boogu-image-edit-turbo \
  -F 'prompt=在主图右上角用白色手写体写上「测试」' \
  -F response_format=path \
  -F image=@input/source.png
```

返回落盘路径，如 `{"data":[{"path":"...\\output\\Boogu_image_edit_turbo_00004.png"}],"seed":...}`。
原图内容会保留，只改动 prompt 指定的部分。

#### `POST /v1/images/remove-background`（独立去背景端点）

无 prompt 的工具类端点：只需传 `image`（文件，multipart），模型固定为 `utility-birefnet-remove-background`（BiRefNet 高精度抠图，输出透明 PNG，尺寸跟随输入图）。可选字段：`response_format`（url/b64_json/path，默认 url）、`filename_prefix`（默认 `bg-removed-image`）、`n`。

```bash
curl -X POST http://127.0.0.1:8188/v1/images/remove-background \
  -F response_format=path \
  -F image=@input/source.png
```

返回结构与其他 images 端点一致。`model` 字段可覆盖为其他 **promptless** 工具类模型（传普通生图模型会被 400 拒绝）；缺 `image` 也直接 400。实测约几秒级（4090 级，1MP 输入）。

**promptless 模型**：`models.yaml` 里声明 `promptless: true` 的模型不绑 prompt、请求也不需要 prompt——适合纯工具类工作流（去背景、放大、转格式等）。走通用 `/v1/images/edits` 调用也可以（`prompt` 已改为可选字段），但绑定 prompt 的模型缺 prompt 仍会 400。

**`flux2-klein-image-edit-turbo`（Flux2 Klein 9B）**：Qwen3-VL 文本编码 + ReferenceLatent 单参考图编辑，语义改写能力强（换背景/换材质/增删物体，指令跟随好），不擅长往图里写字。尺寸跟随输入图（`GetImageSize` → `EmptyFlux2LatentImage`），输入会先被 `ImageScaleToTotalPixels` 缩到 1MP，所以 `size` 不生效、输出约 1MP；负向提示词默认空（cfg=1 时负向不参与）。工作流模板由 4 参考图版本经 `flux2_api_refs.py --count 1` 收敛而来（4 参考图原件在 `user/default/workflows/`，其 `92:145.image` 的断链已修复）；**2–4 张参考图已在工作流层实测通过**（52s / 64s / 80s，参考图主体的色彩/物体会被引入画面，prompt 需明确各参考图用途），但网关单次只收一张 `image`，多参考图需用该脚本直接提交 ComfyUI，或等网关支持多图入参。

### 5.2 视频生成

#### `POST /v1/videos/generations`

文生视频 / 参考生视频。

| 字段 | 类型 | 说明 |
|---|---|---|
| `prompt` | string | 正向提示词（必填） |
| `model` | string? | 默认 `minimax-h3`；视频模型 |
| `size` | string? | `<tier>p-<ratio>` 或 `<ratio>@<tier>`，tier∈{`480p`,`720p`,`768p`,`1080p`}，ratio∈{`1:1`,`3:4`,`4:3`,`16:9`,`9:16`}；或直接 `WxH`；空=模型默认 |
| `duration` | float? | 时长 1–15 秒 |
| `fps` | int? | 帧率 |
| `num_frames` | int? | 总帧数（部分工作流用帧数而非时长） |
| `motion` | string? | **命名运动档**（仅 SelfLift 系列）：`story`=文戏（总步数 6 / 过渡步 5）、`fight`=打戏（8 / 6）。不传用模型默认；与 `steps` / `transition_step` 同传时后者胜出 |
| `transition_step` | int? | SelfLift 渐进采样的过渡步（低分切到高分的步位），**需小于 `steps`**；仅 `minimax-h3-self-lift` / `-self-lift-edit` 有效 |
| `seed` / `negative_prompt` / `steps` / `cfg` / `sampler_name` / `scheduler` / `denoise` | 各类型? | 同图像精调 |
| `image` | string\|string[]? | 图生视频输入 |
| `reference_images` | string[]? | 参考图，最多 6，支持 base64/URL/本地路径 |
| `reference_videos` | string[]? | 参考视频，最多 3 |
| `reference_audios` | string[]? | 参考音频，最多 3 |
| `response_format` | enum? | `url`（默认）/ `b64_json` / `file` / `path` |
| `background` | `"pending"`? | **异步**触发：POST 立即返回 task 对象 |
| `async` | bool? | 兼容别名，`true` 等价于 `background:"pending"` |
| `workflow_overrides` / `filename_prefix` | 各? | 同图像 |

**同步响应**（200）：`{ "created", "data":[{ "url" }], "seed", "references":[...] }`。

**异步响应**（200）：

```json
{ "id": "<task_id>", "object": "image_generation.task", "status": "pending", "created_at": 1700000000 }
```

> 视频模型也可打到 `/v1/images/generations` 自动转视频链路。

### 5.3 异步任务查询与取消

#### `GET /v1/videos/tasks/{id}`  /  `GET /v1/images/tasks/{id}`

查询任务状态与产物（两命名空间等价）。

- `queued→pending`、`processing→in_progress`、`succeeded→completed`、`failed→failed`、`cancelled→cancelled`（原样透出）。
- `completed` 时附 `output:{ "data":[...] }`（url 已绝对化）；`failed` 时附 `error:{ "message", "code" }`。

```json
{
  "id": "<task_id>", "object": "image_generation.task", "status": "completed",
  "created_at": 1700000000, "model": "minimax-h3",
  "output": { "data": [ { "url": "http://host:8188/v1/videos/files/xxxx.mp4" } ] }
}
```

#### `DELETE /v1/videos/tasks/{id}`  /  `DELETE /v1/images/tasks/{id}`

取消异步任务。

**响应**（200 / 404）：

```json
{
  "id": "<task_id>", "object": "image_generation.task", "status": "cancelled",
  "created_at": 1700000000, "model": "minimax-h3",
  "prompt_id": "<comfy_prompt_id>", "cancelled": true,
  "detail": "removed_from_queue"
}
```

`detail` 取值：`removed_from_queue`（pending 已从队列移除）/ `interrupted_running_task`（running 已中断）/ `not_in_comfy_queue`（已离队）/ `task_already_finished`（已终态，未取消）/ `cancelled_local_only`（本地标记取消但无 prompt_id）/ `comfy_cancel_failed: <err>`（尽力而为失败，本地取消仍生效）。任务不存在返回 404 `task_not_found`。

### 5.4 模型列表

#### `GET /v1/models`  /  `GET /v1/models/{model_id}`

返回 `{ "object":"list", "data":[ { "id","object":"model","created","owned_by":"comfyui" } ] }`。
该端点只给 id；**能力 / 描述用 MCP 的 `list_models`**（返回 `capabilities` / `description` / `defaults` / `aliases`）。

当前注册（`models.yaml`）：

| 模型 | 模式 | 能力 | 说明 |
|---|---|---|---|
| `z-image` / `z-image-turbo` | image | text-to-image | Z-Image base（30 步）/ Turbo（8 步，默认模型） |
| `boogu-image-base` / `boogu-image-base-4step` / `boogu-image-turbo` | image | text-to-image | Boogu 文生图三档 |
| **`boogu-image-edit`** / **`boogu-image-edit-turbo`** | image | **image-to-image** | 单图编辑，改图内文字首选（见 5.1） |
| **`flux2-klein-image-edit-turbo`** | image | **image-to-image** | Flux2 Klein 9B 单图编辑，语义改写/换背景首选（见 5.1） |
| **`utility-birefnet-remove-background`** | image | **image-to-image**（promptless） | 去背景独立工具，无 prompt，透明 PNG；专属端点 `/v1/images/remove-background` |
| `mage-flow-base` / `mage-flow-turbo` | image | text-to-image | MageFlow 文生图 |
| `minimax-h3` / `-turbo` | video | text-to-video | H3 文生视频（25 / 8 步） |
| `minimax-h3-edit` / `-turbo-edit` | video | text-to-video | H3 参考生视频（支持图/视频/音频参考） |
| `minimax-h3-self-lift` | video | text-to-video / image-to-video | H3 SelfLift 渐进采样（低分 → 高分）；`reference_images` 传 0 / 1 / 2 张 = 文生 / 首帧 / 首尾帧；分块参数按本机显存自动分档；`motion=story/fight` 一键切文戏 / 打戏步数档 |
| `minimax-h3-self-lift-edit` | video | text-to-video / reference-to-video | 同上，改用 Ref2VA 权重；参考槽全套 6 图 + 3 视频 + 3 音频，按请求实际提供的数量裁剪 |

> 完整别名与绑定关系见 `models.yaml`；模型清单与用途对照也见 [README.md](README.md#内置模型)。

### 5.5 产物文件下载

#### `GET /v1/images/files/{name}`  /  `GET /v1/videos/files/{name}`

按文件名取回生成产物（受 `OUTPUT_TTL` 控制，过期 404）。

### 5.6 运维 / 健康

#### `GET /health`

公开路径。返回服务状态、后端可达性、已注册模型、`auth` 开关。后端不可达时 `status:"degraded"`、HTTP 503。

#### `POST /admin/reload`

热加载 `models.yaml` 与工作流，无需重启 ComfyUI。返回 `{ "reloaded":true, "models":[...] }`。

#### `GET /roundabout/view` 及配套接口

可视化页面（详见 [§7.1 可视化页面](#71-可视化页面viewhtml)）：`GET /roundabout/view`（页面）、`GET /roundabout/view/files`（列目录）、`GET /roundabout/view/tasks`（任务进度）。

### 5.7 管理端点（工作流 / 模型配置）

> 均受鉴权约束（除非已放行）。

| 方法 & 路径 | 说明 |
|---|---|
| `GET /roundabout/admin/state` | 配置状态（模型文件/目录/model 列表） |
| `GET /roundabout/admin/queue` | **队列监控合并视图**：ComfyUI running/pending + 网关 tasks，**每个条目含 `seed`** |
| `GET /roundabout/admin/queue/workflow/{prompt_id}` | 三层查找工作流 JSON：队列 → history → 任务快照 |
| `GET /roundabout/admin/workflows` | 列出工作流文件（含校验状态、被哪些模型引用） |
| `POST /roundabout/admin/workflows/upload` | 上传工作流 JSON（multipart；可选 `create_model` 自动建模型条目） |
| `DELETE /roundabout/admin/workflows/{name}` | 删除工作流（被模型引用时 409） |
| `GET /roundabout/admin/models` | 读取 `models.yaml` 原始内容 |
| `PUT /roundabout/admin/models` | 覆写 `models.yaml`（先校验后落盘，坏配置返回 422 不覆盖） |
| `GET /roundabout/admin/models/structured` | 结构化模型配置（替代原始 YAML 编辑） |
| `PUT /roundabout/admin/models/structured` | 写结构化模型配置 |
| `DELETE /roundabout/admin/models/{name}` | 删除模型 |

#### 队列监控响应（`/roundabout/admin/queue`）

```json
{
  "server_time": 1700000000, "comfy_reachable": true,
  "running":  [ { "number":1, "prompt_id":"...", "created":..., "elapsed":12.3, "node_count":180, "seed":12345, "outputs_to_execute":[] } ],
  "pending":  [ { ...同结构, "seed":67890 } ],
  "running_count": 1, "pending_count": 1,
  "tasks":    [ { "id":"...", "status":"processing", "code":null, "model":"minimax-h3",
                  "request_id":"...", "prompt_id":"...", "seed":12345, "has_workflow":true,
                  "created":..., "elapsed":42.1 } ]
}
```

`seed` 由 `workflow_seed()` 从提交时工作流快照读取：优先匹配各模型 `bindings.seed` 路径，命中即返回；否则退回采样器节点，再扫描常见 `noise_seed/seed/rand_seed` 字段。

---

## 6. 响应模型（schemas）

- **ImageResponse / VideoResponse**：`created`(int) + `data`(list, 每项 `url`/`b64_json`/`path`/`revised_prompt`) + `seed`(int|int[]|null) + `usage`(null) + `references`(视频参考图回显)。
- **异步 task 对象**：`id` / `object:"image_generation.task"` / `status` / `created_at` / `model` / (`output`|`error`)。
- **错误**：`{ "error": { "message": "...", "code": "...", "param": "..." } }`，HTTP 状态对应 4xx/5xx（如 `400` 参数错误、`404` task_not_found、`422` 配置校验失败、`500` 内部错误）。

---

## 7. MCP 网关（12 个工具）

**默认启用**（`MCP_ENABLED` 默认 `true`）：装好 `mcp` / `uvicorn`、重启 ComfyUI 即可用，不需要任何配置。

传输：`streamable-http`，端点 `/mcp`（共享端口挂在 ComfyUI 端口，或 `MCP_HOST:<MCP_PORT>` 独立，端口由 `MCP_PORT` / `MCP_PORT_MAP` 决定）。与 REST 完全互通。

工具分三类：**生成**（`generate_image` / `edit_image` / `remove_background` / `generate_video`）、**查询与控制**（`list_models` / `get_task` / `cancel_task` / `queue_status` / `get_workflow` / `health`）、**运维**（`reload` / `get_view_url`）。

| 工具 | 说明 |
|---|---|
| `list_models` | 列出可用模型及其能力 / 模式 / 默认参数 / 别名 |
| `generate_image` | 文生图；支持 `negative_prompt`/`seed`/`size`/`steps`/`cfg`/`workflow_overrides`(JSON 字符串)/`mode`；返回 OpenAI 风格响应（含 `seed` 回显，url 已绝对化）；视频模型自动转视频链路。**编辑图片不要用本工具**（用 `edit_image` / `remove_background`） |
| `edit_image` | 编辑已有图片（独立工具）：`prompt` + `image`（路径/URL/dataURL/base64）；`model` 默认 `flux2-klein-image-edit-turbo`（语义改写），改图内文字传 `boogu-image-edit-turbo` / `boogu-image-edit`；传文生图/视频模型会被 400 拒绝并列出可用编辑模型 |
| `generate_video` | 文生视频 / 参考生视频；参数 `prompt`/`model`(默认 `minimax-h3`)/`duration`/`fps`/`size`/`seed`/`negative_prompt`/`reference_images|videos|audios`/`background`；`background:"pending"` 异步，再查 `get_task` |
| `remove_background` | 图片去背景（BiRefNet，独立工具，无需提示词）；参数 `image`(本地路径/URL/dataURL/base64)、`response_format`(默认 url)、`filename_prefix` |
| `get_task` | 查询异步任务状态与产物（含 `prompt_id`、是否有工作流快照、`output`、`error`） |
| `cancel_task` | 取消任务：`task_id` 非空取消指定任务（pending 移出队列 / running 中断）；**空则中断 ComfyUI 当前执行任务** |
| `queue_status` | 队列监控：ComfyUI running/pending + 网关 tasks（与 REST `/roundabout/admin/queue` 同源） |
| `get_workflow` | 三层查找工作流 JSON：队列 → history → 任务快照 |
| `reload` | 热加载 `models.yaml` |
| `health` | 网关与 ComfyUI 后端健康状态 |
| `get_view_url` | 返回可视化页面地址（`{url}`，浏览器直接打开）：浏览 input/output 资源 + 实时任务进度。用户问「生成的东西在哪看」「给我查看页面」时调用，把 `url` 原样给用户 |

### 7.1 可视化页面（view.html）

`get_view_url` 返回形如 `http://127.0.0.1:8188/roundabout/view` 的地址，浏览器打开即可：

- **资源浏览**：切换 Output / Input 根目录，面包屑进入子目录；图片走 ComfyUI `/view` 的 webp 缩略图，视频内联播放，音频点开弹层播放，点击弹出大图/播放器。文件**默认按修改时间倒序**（刚生成的最靠前，目录始终排在最前），底部有首页/上一页/下一页/末页。

**每页条数按视口高度自适应**：列数由网格宽度（`minmax(148px, 1fr)` + 12px 间距）决定，行高 = 列宽（1:1 缩略图）+ 文字区（首屏后从真实节点量取），可见高度 = 视口高 − sticky header − 面板标题栏 − 面板内边距 − 分页条预留。行数 × 列数即一页条数（上限 500，即后端 `limit` 上限），所以大屏可能一页 40~60 条、小窗口只有十几条。窗口 resize、旋转屏幕、展开/收起右下角任务面板（会改变网格宽度）都会在布局动画结束后重新测量，并把 `offset` 对齐到新页边界重取，位置大致不变。前端会把自己算出的 `limit` 一并传给 `/files`，未传时后端仍按默认 120。
- **产物自动同步**：每 4 秒轻量探测当前目录（只取第一条，几十字节）——**有新产物就自动刷新**，不用手点刷新。若你正在翻页或开着大图，则不打断，改为左下角出现半透明胶囊「发现 N 个新产物 · 查看」，点它回到第一页。标签页切到后台时暂停探测。
- **任务进度**：默认收起为右下角半透明悬浮按钮（`任务进度 · N 条`），点开向上展开面板；有任务在跑时按钮上出现呼吸蓝点。展开后每 2 秒轮询：顶部是 ComfyUI 队列（执行中 / 排队计数），下面是**网关任务表**（同步与异步生成都在内：状态 排队中 / 执行中 / 已完成 / 失败 / 已取消、模型、耗时、任务 id）；成功后附「查看产物」链接，失败显示错误摘要，最多渲染最近 50 条。

> 队列区与任务表是两层信息：任务表记录「谁提交了什么、成了什么」，队列反映「此刻 ComfyUI 在算什么」。
> 后端不可达时队列区标记不可达，任务表照常显示。

支撑端点：

| 端点 | 说明 |
|---|---|
| `GET /roundabout/view` | 页面本身（读 `web/view.html`） |
| `GET /roundabout/view/files?root=output\|input&path=子/目录&offset=0&limit=120&sort=mtime&order=desc` | 列目录，返回 `crumbs`/`dirs`(全量，按名升序在前)/`files(name,size,mtime,kind,url,thumb)` + 分页元信息 `total`/`dir_count`/`offset`/`limit`/`has_more`/`sort`/`order`。`limit` 默认 120、上限 500；`sort=mtime`(默认，新的在前)\|`name`，`order=desc`(默认)\|`asc`；扫描硬上限 20000 条，超出置 `truncated` |
| `GET /roundabout/view/tasks` | 任务快照：`tasks[]`（`status`(OpenAI 枚举) / `model` / `elapsed` / `prompt_id` / `url`(已绝对化) / `path`(产物在 input/output 之外时给原路径，不可点) / `error`）+ `queue`（ComfyUI `running`/`pending` 的 prompt_id 列表，`reachable` 标记）。产物地址基准：`PUBLIC_BASE_URL` → 请求 origin，保证远程浏览器可打开；结果是磁盘路径时自动翻译成 `/view?filename=..&type=..` |

> 目录解析走 `folder_paths`，根目录固定为 input/output 两个，`..` 穿越返回 400 `bad_path`。
> 鉴权开启时浏览器无法带自定义头，页面支持 `?key=<api_key>`；`get_view_url` 会自动把 key 拼进返回的 url。
> 页面与前端资源改动只需**强刷浏览器**；新增/修改 Python 路由需重启 ComfyUI。

### 7.2 完成推送（替代轮询）

MCP 异步视频任务完成时，服务端通过标准 `notifications/message`（`LoggingMessageNotification`）向保持连接的客户端主动推送，无需轮询：

```json
{
  "level": "info", "logger": "roundabout.mcp",
  "data": { "task_id":"...", "status":"completed", "model":"minimax-h3",
            "prompt_id":"...", "url":"http://host:8188/v1/videos/files/xxxx.mp4" }
}
```

错误完成时 `data` 含 `error` 字段，状态为 `failed` / `cancelled`。

---

## 8. 调用示例

### 8.1 REST 文生图（同步）

```bash
curl -X POST http://127.0.0.1:8188/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <key>" \
  -d '{"model":"z-image-turbo","prompt":"a red fox in snow","size":"1024x1024","seed":42}'
```

### 8.2 REST 视频生成（异步 + 轮询/推送）

```bash
# 提交（异步）
TASK=$(curl -s -X POST http://127.0.0.1:8188/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"minimax-h3-turbo","prompt":"a cat walking in rain","duration":5,"background":"pending"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 查询
curl -s http://127.0.0.1:8188/v1/videos/tasks/$TASK

# 取消（如需）
curl -X DELETE http://127.0.0.1:8188/v1/videos/tasks/$TASK
```

### 8.3 MCP 客户端（伪代码）

```
call generate_video(prompt="a cat walking in rain", model="minimax-h3-turbo",
                    duration=5, background="pending")  → 返回 {id, status:pending}
# 保持 SSE 连接，等待 notifications/message 推送；或轮询 get_task(id)
```

---

## 9. 注意事项

- **共享端口实现细节**：MCP 的 `streamable-http` 是 Starlette(ASGI)，ComfyUI 网关是 aiohttp，二者无法同端口直挂。最终采用「回环 uvicorn 后端 + aiohttp 原生代理」：客户端只连 ComfyUI 端口的 `/mcp`，代理原样转发 POST/GET(SSE)/DELETE 到内部回环后端（`127.0.0.1`）。<br>**注册时序**：aiohttp 的路由表在 `AppRunner.setup()` 里冻结，之后任何 `add_route` 都抛 `RuntimeError: Cannot register a resource into frozen router`；而后端端口要等 uvicorn bind 成功才知道（可能是 `MCP_PORT=0` 由系统分配，也可能因端口被占而回落）。因此代理路由在**节点 import 期**就注册好（此时必定早于冻结），转发目标先留空、后端就绪后回填端口；这期间到达的 `/mcp` 请求会先等 `READY_WAIT_TIMEOUT`（10s），等到才开始转发，超时则回 `503` + `Retry-After`。
- **取消 running 任务的副作用**：ComfyUI 无按 id 中断接口，`interrupt` 会中断**当前正在执行**的任务；并发 >1 时需谨慎。
- **异步任务内存态**：任务表为进程内存，重启 ComfyUI 后未完成任务丢失（可选凭 task_id 缺失判定失败重试）。终态记录保留 6 小时。
- **图片无异步**：只有视频生成支持 `background/async` 异步模式；图片请求始终同步返回（但仍会在任务表留一条终态记录）。
- **参考素材上限**：MiniMax H3 系列最多 6 图 / 3 视频 / 3 音频；未上传的参考节点由网关提交前自动删除。

## 10. 排障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动日志 `MCP gateway: failed to start embedded server: No module named 'mcp'` | ComfyUI 的 Python 未装 `mcp`/`uvicorn`（其自带依赖不含） | 见 [2.1 Python 依赖](#21-python-依赖换机器部署必看)，用运行 ComfyUI 的解释器装，装完重启 |
| `/mcp` 404 / 连不上 | URL 用了内部回环端口；或 MCP 被显式关闭（`.env` 里 `MCP_ENABLED=false`）；或后端端口绑定失败（启动日志出现 `backend server stopped`） | URL 用 **ComfyUI 对外端口** + `/mcp`；删掉该行或改成 `true` 后重启 |
| 日志反复出现 `Error handling request from <客户端IP>`，后面拖一整段 traceback | aiohttp 在 handler 抛异常时**先打这条 ERROR、再判断连接是否已断**（`web_protocol.handle_error`），所以 SSE 场景下「客户端超时/重连把旧连接关掉」这种常态每次都会刷一条。**看 traceback 最后一行**：<br>· `ClientConnectionResetError: Cannot write to closing transport`（或 `ConnectionResetError` / `ConnectionAbortedError` / `BrokenPipeError`）→ 就是客户端断开，**属常态，不是故障**<br>· `ClientConnectorError: Cannot connect to host 127.0.0.1:<port>` → 内部 MCP 后端没起来，代理转发失败 | 前者：已由两层兜住——`mcp_server._proxy` 把这类断开降为 DEBUG，`gateway/log_filters.py` 再给 `aiohttp.server` 挂一个过滤器把这类记录降级（只降「客户端断开」，**真异常照旧报 ERROR**）。更新代码即可；想看原始日志设 `ROUNDABOUT_RAW_AIOHTTP_LOGS=true`<br>后者：查启动日志 `ComfyUI port <N> -> MCP backend port <M>`，核对 `MCP_PORT` / `MCP_PORT_MAP` 与本实例 `--port` 是否对得上 |
| 启动日志 `MCP gateway: on_ready callback failed: Cannot register a resource into frozen router` | 代理路由注册晚于 ComfyUI 冻结 aiohttp 路由表（旧版本在拿到后端端口后才 `add_route`） | 更新到当前代码即可（路由改为 import 期注册、端口后回填）。若仍出现，说明有别的节点/代码在 ComfyUI 起服务后才 import 本节点，或改用 `MCP_SHARE_PORT=false` 走独立端口 |
| ComfyUI 重启后 agent 侧报 `Rejected request with unknown or expired session ID: <hex>` | **不是故障，也与端口无关**。MCP streamable-http 的会话只存在后端进程内存里（`Mcp-Session-Id` → `_server_instances`），ComfyUI 重启即全部清空；agent 仍拿着旧 session ID 发请求，SDK 按 MCP 规范回 404 `Session not found` 并打这条 INFO（logger `mcp.server.streamable_http_manager`）。URL 走的是 ComfyUI 固定端口 `/mcp`，所以请求能到达服务器——若真是端口问题，症状会是「连接被拒」而不是「session 未知」 | agent 侧重新 initialize 即可（多数 MCP 客户端下次调用时自动重连；不自动重连的，重启该 agent 的 MCP 连接）。想让 agent **彻底不怕 ComfyUI 重启**：设 `MCP_STATELESS=true`（无状态模式，见环境变量表；代价是完成通知推送改为轮询任务状态） |
| 日志反复出现 `Created new transport with session ID: <hex>` | 请求**没带 `Mcp-Session-Id`**（或带了已失效的），SDK 就为每个这样的请求新建一个会话——`streamable_http_manager.py` 有状态路径的「New session case」，INFO 级。典型原因是客户端不保存 initialize 响应里的 `Mcp-Session-Id`、每次工具调用都重新 initialize / 新建连接（即客户端按无状态方式在用它）。**注意**：SDK 默认 `session_idle_timeout=None`，空闲会话**不会被回收**，会话及其后台任务会持续堆积（`_server_instances` + 每会话一个 `run_server` task），不会自己释放 | 服务端无 bug，是客户端没有复用会话。能改客户端就让它保存并回带 `Mcp-Session-Id`；改不动就设 `MCP_STATELESS=true`——服务端不再建任何会话，这条日志消失，也顺带根治上面那条 `unknown or expired session ID`（代价同上：完成通知改为轮询） |
| MCP 握手成功但工具列表为空 | 后端 uvicorn 未起来（上一行日志） | 同上；确认启动日志出现 `MCP gateway: embedded streamable-http shared on ComfyUI port` |
| 访问远程 IP 不通（如 `:20003`）但本机 `127.0.0.1` 正常 | ComfyUI 未加 `--listen 0.0.0.0`，或反代未放行 SSE（`text/event-stream`）长连接 | 启动加 `--listen 0.0.0.0`；反代关闭缓冲、放行 `Accept: text/event-stream` |
| 生成后 `url` 是相对路径 | 未设 `PUBLIC_BASE_URL` 且请求 host 不可达客户端 | 设 `PUBLIC_BASE_URL=http://<对外地址>` |
| MCP `list_models` 看不到刚加的模型 / 新改的 `models.yaml` | 旧版 `mcp_server.py` 用绝对导入 `gateway.*`，与节点侧相对导入形成**两份 registry** | 升级代码后重启（已修，见 `test_mcp_import_identity.py`）；平时加模型后 `POST /admin/reload` 即可 |
| MCP 创建的异步任务在 `/v1/videos/tasks/{id}` 或任务面板里查不到 | 同上（`task_store` 也是两份） | 同上 |
| 视图页看不到刚生成的产物 | 页面每 4 秒探测一次；或标签页在后台（暂停探测）；或产物落在 input/output 之外 | 手动点「刷新」；确认产物目录是 ComfyUI 的 output |
| 任务面板一直空 | 后端未重启（旧代码只在异步视频时记任务）；或标签页后台 | `handlers.py` / `tasks.py` / `viewer.py` 改动需重启 ComfyUI |
