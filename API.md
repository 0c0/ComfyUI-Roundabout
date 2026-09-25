# ComfyUI-Roundabout 接口文档

> 一份干净的接口契约。**REST 网关** 与 **MCP 网关** 两套接入层，共享同一份 `models.yaml` 注册表、同一套生成链路、同一个异步任务表。
>
> 当前版本：`1.20.1` ｜ 网关即 ComfyUI 自身（custom node），随 ComfyUI 启动自动加载。
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
                          │        = MCP Server (16 tools)               │
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
- **自适应层**：`gateway/` 下除注册表与生成链路外，还有两个「跟着机器 / 请求变的参数」模块 —— `vram.py`（按显存档位选分块参数）、`params.py`（请求参数白名单与校验）。它们不改工作流文件，只在渲染前覆盖节点字段。**只有分块档位来自 `models.yaml`**（改完 `POST /admin/reload` 生效）；白名单与档位取值是 `params.py` 里的常量，改了要重启。细节见 [README.md](README.md#配置)。

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

> **MCP 地址 = ComfyUI 自身的地址 + `/mcp`**，没有独立端口。ComfyUI 跑在 `192.168.1.10:8188`（默认端口，或经反代），MCP 端点就是 `http://192.168.1.10:8188/mcp`。
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
| `MCP_STATELESS` | `true` | **无状态模式（默认）**：每个 MCP 请求独立处理、不跟踪会话，ComfyUI 重启后 agent 无需重新 initialize（根治 `unknown or expired session ID`）。设成 `false` 即为标准有状态 streamable-http，那是唯一能拿到「异步任务完成通知」推送的模式（通知挂在会话上）——代价是 ComfyUI 一重启旧会话全失效，客户端没重新握手前所有调用都报错。两种模式都由 SDK 原生 `stateless_http` 支持，无自研逻辑 |
| `ROUNDABOUT_RAW_AIOHTTP_LOGS` | 空 | 设 `true` 关闭 aiohttp「客户端断开」日志降噪（见 §10 排障），恢复 aiohttp 原始 ERROR |
| `ROUNDABOUT_RAW_MCP_LOGS` | 空 | 设 `true` 关闭 MCP 日志降噪：无状态模式下 SDK 每个请求收尾都打一条 `[INFO] Terminating session: None`（agent 连续调工具即刷屏），默认丢弃 |
| `ROUNDABOUT_VRAM_GB` | 空 | 手动钉住显存档位（GiB），用于 `vram_adaptive: true` 的模型（如 `minimax-h3-lift`）；不填则启动时自动探测，探测不到就不覆盖工作流自带的分块参数 |
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
> - 远程访问（如局域网内另一台机）：换成 `http://192.168.1.10:8188/mcp`，其余不变。
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
- **编辑模型（`image-to-image`）**：`boogu-image-edit` / `boogu-image-edit-turbo` 与 `flux2-klein-image-edit-turbo`（含别名 `flux2-edit-turbo`、`image_flux2_klein_image_edit_turbo`）**必须传图**（不传返回 400，文案统一是 `requires an input `image`` —— 多图档只传 `reference_images` 也算通过）；`qwen-image-2.1` 是**文生与多图编辑同一支**：6 个参考槽（超上限 400），**一张参考图都不传即纯文生**，是唯一不要求传图的图像模型。它没有 `image` 绑定，但保留了 `image` 单图入口 —— 由网关当作第 1 张参考图接入，等价 `reference_images[0]`（之所以不能给它配绑定：槽一旦被绑定保护就删不掉，纯文生那一支会把模板占位图当参考喂进去）。**klein 的 `image` 与 `reference_images[0]` 落在同一个参考槽上**：先按 `reference_images[i]` 逐槽写入、再套 `image` 的绑定，同节点后者覆盖前者 —— 所以**两者同时传时 `image` 静默失效**（要改第 1 张就写 `reference_images[0]`，别用 `image` 去覆盖它）。boogu 系列擅长**改写 / 添加图像内的文字**，prompt 里可用「图1」指代输入图；flux2 klein 与 qwen 擅长**语义改写**（换背景/材质、增删物体）；klein 与 qwen 都能吃多张参考做组合，输出尺寸跟随输入图（boogu / klein 缩放到 1MP；qwen 按官方默认不重采样、直接跟随第 1 张参考图），此时 `size` 不生效（qwen 只在纯文生那一支用 `size`）；编辑模型均不继承全局默认负向提示词（全局负向含 text / watermark，会与写字的用途冲突）。
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
| `style` | string? | `vivid|natural`（在 models.yaml 的 `style_presets` 里映射为提示词后缀） |
| `response_format` | enum? | `url`（**默认**，图像与视频一致；局域网内直接给可打开的地址）/ `b64_json`（内联字节）/ `file` / `path` |
| `negative_prompt` | string? | 反向提示词。**只在 `cfg > 1` 时参与计算**：`cfg = 1` 时 ComfyUI 走 cfg1 优化、整条负向分支根本不执行 —— 传了不报错也不生效。实测同一张图（同 seed 同 prompt）只改负向：`cfg=1` ⇒ MAE `0.0000`，`cfg=4` ⇒ `23.5`（阳性对照）。模板默认 `cfg=1` 的模型（如 `qwen-image-2.1`）要负向起作用就得抬 `cfg`。自动负向分流（`AUTO_SPLIT_NEGATIVE`）同理 |
| `seed` | int? | 不传/`-1` 随机；`0` 与正整数固定 |
| `steps` / `cfg` / `sampler_name` / `scheduler` / `denoise` | 各类型? | 采样精调；`denoise` 为图生图重绘幅度 |
| `image` | string? | 图生图**基图（单张）**，base64 / dataURL / URL / 本地路径。传数组返回 400（改走 `reference_images`）。有 `image` 绑定的模型（klein / boogu）由绑定写进模型声明的落点，**与 `reference_images[0]` 是同一个参考槽的两个入口**（见 §4），两者同时传时 `image` 被静默覆盖；没有 `image` 绑定的模型（`qwen-image-2.1`）由网关把它当作第 1 张参考图接入 |
| `reference_images` | string[]? | **多图参考**输入（仅声明了 `references` 的图像模型：`qwen-image-2.1` 6 张 / `flux2-klein-image-edit-turbo` 4 张）；按序对应参考槽，超过该模型槽数返回 400。**klein / boogu 编辑档至少要有一张输入**（`image` 或 `reference_images`，谁都不给返回 400 `requires an input image`）；`qwen-image-2.1` 例外 —— 不给参考即纯文生 |
| `mask` | string? | 局部重绘遮罩 |
| `mode` | string? | 生图模式；仅当模型在 `models.yaml` 声明了 `mode_choices` 时生效（当前内置模型均未声明） |
| `workflow_overrides` | object? | 直接改写节点，如 `{"3.inputs.cfg": 4.5}` |
| `filename_prefix` | string? | 落盘前缀，可含 `/` 建子目录；空则用该模型工作流模板里的前缀（H3 视频统一为 `video/MiniMax_H3`） |

**响应**（200）：

```json
{
  "created": 1700000000,
  "data": [ { "url": "http://host:8188/v1/images/files/xxxx.png", "revised_prompt": null } ],
  "seed": 123456789,
  "usage": null,
  "task_id": "1700000000-a1b2c3"
}
```

> **同步回执也带 `task_id`**：同步链路同样在网关任务表留一条记录，把它交给
> `POST /roundabout/view/board/items` 的 `task_id` 来源即可钉卡，不必自己拼产物地址。
> 任务表记录 6 小时后过期（见 §错误处理）。

#### `POST /v1/images/edits`

OpenAI 标准 multipart 图生图。字段：`image`（文件，可多张）、`mask`（文件，可选）、`prompt`、`model`、`n`、`size`、`response_format`、`user`、`negative_prompt`、`seed`、`steps`、`cfg`、`denoise`。

**单图编辑模型**（改图内文字首选 boogu 系列；风格/内容改写首选 flux2 klein）：

| 模型 | 别名 | 默认步数 / cfg / 采样器 |
|---|---|---|
| `boogu-image-edit` | `boogu-edit` | 30 / 3.5 / `dpmpp_2m` + `simple` |
| `boogu-image-edit-turbo` | `boogu-edit-turbo` | 6 / 1 / `euler` + `sgm_uniform` |
| `flux2-klein-image-edit-turbo` | `flux2-edit-turbo` | 6 / 1 / `euler` |

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

返回结构与其他 images 端点一致。`model` 字段可覆盖为其他 **promptless** 工具类模型（传普通生图模型会被 400 拒绝）；缺 `image` 也直接 400。

**promptless 模型**：`models.yaml` 里声明 `promptless: true` 的模型不绑 prompt、请求也不需要 prompt——适合纯工具类工作流（去背景、放大、转格式等）。走通用 `/v1/images/edits` 调用也可以（`prompt` 已改为可选字段），但绑定 prompt 的模型缺 prompt 仍会 400。

**`flux2-klein-image-edit-turbo`（Flux2 Klein 9B）**：Qwen3-VL 文本编码 + ReferenceLatent 链式参考编辑（网关侧开到 **4 个参考槽**），语义改写能力强（换背景/换材质/增删物体，指令跟随好），不擅长往图里写字。尺寸跟随输入图（`GetImageSize` → `EmptyFlux2LatentImage`），输入会先被 `ImageScaleToTotalPixels` 缩到 1MP，所以 `size` 不生效、输出约 1MP；负向提示词默认空（cfg=1 时负向不参与）。工作流模板由 4 参考图版本经 `flux2_api_refs.py --count 1` 收敛而来（4 参考图原件在 `user/default/workflows/`，其 `92:145.image` 的断链已修复）；**2–4 张参考图已在工作流层实测通过**（参考图主体的色彩/物体会被引入画面，prompt 需明确各参考图用途），网关侧对应 `reference_images` 的 4 个参考槽（`image` 绑在第 1 槽，只收单张）。

### 5.2 视频生成

#### `POST /v1/videos/generations`

文生视频 / 参考生视频。

| 字段 | 类型 | 说明 |
|---|---|---|
| `prompt` | string | 正向提示词（必填） |
| `model` | string? | 默认 `minimax-h3`；视频模型 |
| `size` | string? | `<tier>p-<ratio>` 或 `<ratio>@<tier>`，tier∈{`480p`,`576p`,`720p`,`768p`,`1080p`,`1440p`}，ratio∈{`1:1`,`3:4`,`4:3`,`16:9`,`9:16`}；或直接 `WxH`；空=模型默认。**所有视频尺寸都对齐到 32 的倍数**（latent 16 倍下采样后还要过 DiT 2×2 patch，奇数 latent 会炸 shape）：`720p` 实际 736、`768p-16:9` = 1376×768，`WxH` 自动 round 到 32。注意模型默认画布 1344×768 是 **7:4**，不等于 `768p-16:9`（1376×768）。`1440p-16:9` = 2560×1440，需大显存（8GB 直接生跑不动，改用 lift 放大） |
| `duration` | float? | 时长 1–15 秒 |
| `fps` | int? | 帧率 |
| `num_frames` | int? | 总帧数（部分工作流用帧数而非时长） |
| `attention` | `"sparse"`\|`"dense"`? | **注意力档位**（更快 ↔ 更高质量，仅 base 四支 H3 视频档）：`sparse`（默认，块稀疏加速，更快、更省显存；画质与致密高度一致、差异只在高频细节）/ `dense`（关闭稀疏，画质优先，耗时回满）。不传 = 保持模板默认（稀疏）。**FastH3 两支恒稀疏**（其 `vsa` 与蒸馏权重配对训练，关掉不是更高画质而是脱离训练分布），传了报 400；其它模型同样报 400 |
| `output_size` | string? | **期望输出尺寸**（仅 `minimax-h3-lift` / `-lift-edit`）：格式同 `size`，网关反推放大倍率 `scale = 输出短边 / 画布短边`（输出保持画布宽高比，比例偏差 >5% 报 400 —— 那是换构图不是放大）。与 `scale` 互斥；其它模型传了报 400（输出尺寸就是 `size`）。实际输出尺寸见响应 `size` 回显 |
| `scale` | float? | **放大倍率**（仅 `minimax-h3-lift` / `-lift-edit`）：输出 = 画布 × scale，默认 1.875 → 2520x1440。与 `output_size` 互斥；其它模型传了报 400 |
| `workflow_overrides` | object? | 厂商特有参数的通用透传（不单设请求字段），如 `{"910.inputs.rho": 0.3}`。`minimax-h3-lift` 的可调项：`910.inputs.rho`（SelfLift-zero 像素锚阻尼，默认 0=纯学习 lift 纹理最强；调高会压高频细节）、`910.inputs.w_min`/`w_max`（阻尼强度上下限，默认 0.5/1.0）。放大倍率 `scale` 已是正式请求参数，不必走透传 |
| `seed` / `negative_prompt` / `steps` / `cfg` / `sampler_name` / `scheduler` / `denoise` | 各类型? | 同图像精调 |
| `image` | string\|string[]? | 图生视频输入（视频档不收，传了 400 并指路：帧用 `first_frame`/`last_frame`，参考图用 `reference_images`） |
| `first_frame` | string? | **首帧图**（声明 frame_params 的模型：`minimax-h3` / `-lift` / `fasth3`）：成为输出第 1 帧，按画布 size 做 **cover 等比铺满 + 居中裁剪**（不变形）；支持 base64/URL/本地路径。无帧槽的模型传了报 400。⚠️ **模型侧跟随度（09-24 实测）**：base 系 keyframe 需要**足够步数**（8 步不跟随、30 步完美跟随，lift 产物为证）；fasth3 在 **576p 档 keyframe 失效**（768p 正常）—— 要首帧严格跟随：base 系 ≥30 步、fasth3 ≥768p |
| `last_frame` | string? | **尾帧图**（同 `first_frame` 三支）：成为输出最后 1 帧，同 cover 裁剪。统一节点拓扑下**可与 `reference_images` 同传**（帧槽是帧语义，参考槽是 conditioning 语义，各走各的槽） |
| `reference_images` | string[]? | 参考图（最多 6），支持 base64/URL/本地路径；与首尾帧可同传（v1.17 统一节点拓扑） |
| `reference_videos` | string[]? | 参考视频，最多 3：视频编辑 / 动作 / 运镜参考。槽位在 H3 六支全部接入；**FL2VA 权重（minimax-h3 / -lift）对视频参考的消费未标定**，视频编辑主口径走 Ref2VA 权重（`minimax-h3-edit` / `-lift-edit`）。无视频槽的模型（fasth3）传了 400 |
| `reference_audios` | string[]? | 参考音频，最多 3：音频复用 / 音色节奏参考。同上，主口径走 Ref2VA 两支 |
| `response_format` | enum? | `url`（默认）/ `b64_json` / `file` / `path` |
| `background` | `"pending"`? | **异步**触发：POST 立即返回 task 对象 |
| `async` | bool? | 兼容别名，`true` 等价于 `background:"pending"` |
| `workflow_overrides` / `filename_prefix` | 各? | 同图像 |

> **外部来源的参考素材会被复制进 ComfyUI 的 `input/`**：`http(s)` / `dataURL` / `base64`，以及**不在 `input/` 目录下**的本地路径（含 `output/`、`temp/`）都要先转存；命名形如 `{请求id}_ref{img|vid|aud}_{槽位序号}.{ext}`（如 `18ae9470d11a-0_refvid_0.mp4`，其中 `18ae9470d11a-0` 是 `请求id-批次号`）。**已经在 `input/` 内的文件免转存、沿用原名**。输入图与 mask 同理，命名为 `{请求id}_src.{ext}` / `{请求id}_mask.{ext}`。这些副本会留在 `input/` 里，需要时自行清理。

**同步响应**（200）：`{ "created", "data":[{ "url" }], "seed", "references":[...], "size":"2528x1440" }`（`size` = 实际输出尺寸；lift 档为 latent × scale 的精确换算）。

**异步响应**（200）：

```json
{ "id": "<task_id>", "object": "image_generation.task", "status": "pending", "created_at": 1700000000 }
```

> 视频模型也可打到 `/v1/images/generations` 自动转视频链路。

### 5.3 异步任务查询与取消

#### `GET /v1/videos/tasks/{id}`  /  `GET /v1/images/tasks/{id}`

查询任务状态与产物（两命名空间等价）。

- `queued→pending`、`processing→in_progress`、`succeeded→completed`、`failed→failed`、`cancelled→cancelled`（原样透出）。
- `completed` 时附 `output:{ "data":[...] }`（url 已绝对化）与顶层 `size`（实际输出尺寸 "WxH"，与同步响应同位 —— 不必再去 ffprobe 产物）；`failed` 时附 `error:{ "message", "code" }`。未终态两者都没有。

```json
{
  "id": "<task_id>", "object": "image_generation.task", "status": "completed",
  "created_at": 1700000000, "model": "minimax-h3-lift",
  "output": { "data": [ { "url": "http://host:8188/v1/videos/files/xxxx.mp4" } ] },
  "size": "2528x1440"
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
| **`flux2-klein-image-edit-turbo`** | image | **image-to-image** | Flux2 Klein 9B **多图参考**编辑（最多 4 张），语义改写/换背景首选（见 5.1） |
| **`qwen-image-2.1`** | image | text-to-image / **image-to-image** | Qwen-Image 2.1：**文生与多图参考编辑同一支**（25 步，Qwen3-VL 文本编码）。不带参考图即纯文生，原生 2K 档位、默认 1024x1024（`size` 直传，支持非方图）；带 1–6 张参考图（`reference_images`）即多图编辑，输出尺寸跟随第 1 张参考图 |
| **`utility-birefnet-remove-background`** | image | **image-to-image**（promptless） | 去背景独立工具，无 prompt，透明 PNG；专属端点 `/v1/images/remove-background` |
| `minimax-h3` | video | text-to-video / first-last-frame | H3 首尾帧生视频（base 30 步）：`first_frame` / `last_frame` 传 0 / 1 / 2 张 = 文生 / 首帧 / 首尾帧（按画布 cover 裁剪）。草稿传 `size:"576p-16:9"` + `steps:8`，交付用默认 1344x768@30（网关无 `quality` 分档 —— 它只能表达 size + steps，与直接传参等价） |
| `minimax-h3-edit` | video | text-to-video / reference-to-video | H3 参考生视频，30 步（支持图/视频/音频参考） |
| `minimax-h3-lift` | video | text-to-video / first-last-frame | base 骨架 + 确定性放大：`size` = 768p 第一采画布（默认 1344x768），latent lift × `scale`（默认 1.875 → 输出 2520x1440）；`first_frame` / `last_frame` 传 0 / 1 / 2 张 = 文生 / 首帧 / 首尾帧（按画布 cover 裁剪）；分块参数按本机显存自动分档 |
| `minimax-h3-lift-edit` | video | text-to-video / reference-to-video | 同上，改用 edit 骨架（Ref2VA 权重，30 步）；参考槽全套 6 图 + 3 视频 + 3 音频，按请求实际提供的数量裁剪 |
| `fasth3` | video | text-to-video / first-last-frame | FastVideo FastH3 8 步蒸馏档；`first_frame` / `last_frame` 传 0 / 1 / 2 张 = 文生 / 首帧 / 首尾帧（按画布 cover 裁剪，走关键帧槽，见 [WORKFLOWS.md](WORKFLOWS.md)）。定位**草稿 / 快周转**，非 49/50 步的等价替代 |
| `fasth3-edit` | video | text-to-video / reference-to-video | FastH3 参考生视频；参考槽全套 6 图 + 3 视频 + 3 音频，按请求实际提供的数量裁剪。⚠️ **占位档**：官方未蒸馏 Ref2VA，与 `fasth3` 共用同一份 fl2v 权重 |

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
| `GET /roundabout/admin/weights` | **权重体检**：内置工作流引用的权重里当前缺哪些，每条的下载命令与目标目录（`?unreferenced=1` 附带当前无工作流引用的条目，`?mirror=modelscope` 换下载源） |
| `GET /roundabout/admin/tool-info` | **调用结构自描述**：逐模型的字段生效性（类型 / 区间 / 枚举 / 默认值 / 是否生效 + 不生效原因）、参考槽数量、别名、生效默认值，加全局限制（张数上限、尺寸档位表、种子上限）。`?view=compact` 取裁剪版，`&model=<名>` 限定单模型，`&fields=0` 省掉字段清单 |
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

- **ImageResponse / VideoResponse**：`created`(int) + `data`(list, 每项 `url`/`b64_json`/`path`/`revised_prompt`) + `seed`(int|int[]|null) + `usage`(null) + `task_id`(同步链路在任务表的 id，供钉卡反查) + `references`(视频参考图回显) + `size`(实际输出尺寸 "WxH")。
- **异步 task 对象**：`id` / `object:"image_generation.task"` / `status` / `created_at` / `model` / (`output`|`error`) / `size`（仅 `completed` 且该次生成有尺寸回显时出现，取值同同步响应的 `size`）。
- **错误**：`{ "error": { "message": "...", "code": "...", "param": "..." } }`，HTTP 状态对应 4xx/5xx（如 `400` 参数错误、`404` task_not_found、`422` 配置校验失败、`500` 内部错误）。
- **权重缺失会被改写成下载指引**：ComfyUI 的 combo 校验拒掉提交时（`value_not_in_list`，报错形态 `Value not in list (unet_name: 'x.safetensors' not in [...])`），网关把该 400 的 `message` 补成「文件名 + 目标目录 + `curl` 命令」。数据来自仓库根的 `weights.yaml`；索引里没有的文件（如 `LoadImage` 的输入图）**保持原报错不变**，不会给出错的下载地址。

---

## 7. MCP 网关（16 个工具）

**默认启用**（`MCP_ENABLED` 默认 `true`）：装好 `mcp` / `uvicorn`、重启 ComfyUI 即可用，不需要任何配置。

传输：`streamable-http`，端点 `/mcp`（共享端口挂在 ComfyUI 端口，或 `MCP_HOST:<MCP_PORT>` 独立，端口由 `MCP_PORT` / `MCP_PORT_MAP` 决定）。与 REST 完全互通。

工具分四类：**生成**（`generate_image` / `edit_image` / `remove_background` / `generate_video`）、**查询与控制**（`get_tool_info` / `list_models` / `get_task` / `cancel_task` / `queue_status` / `get_workflow` / `health` / `check_weights`）、**运维**（`reload` / `get_view_url` / `get_skills`）、**看板**（`view_board`，单入口按 `action` 分发：pin / remove / clear / get / history / load）。

> 工具的 `description` **只保留一句话定位**；参数细节（逐模型生效性、区间、枚举、默认值、参考槽
> 数量、尺寸档位）一律查 `get_tool_info`。该工具与 REST 的 `/roundabout/admin/tool-info` 同源，
> 都由 `gateway/toolinfo.py` 从**运行期状态**推导（`registry` 的绑定与 `references` 拓扑、
> `schemas.py` 的字段约束、`params.py` 的档位表、`settings` 的上限），因此不写散文、不会漂移。

| 工具 | 说明 |
|---|---|
| `get_tool_info` | **调用结构自描述**（只读、不占 GPU）：逐模型列出每个请求字段的类型 / 区间 / 枚举 / 默认值 / **对本模型是否生效与原因**，加参考槽数量、张数上限、尺寸档位与种子上限。可选 `model` 限定单模型、`include_fields=false` 省掉字段清单。**拿不准「这个模型能不能传某参数」「该用哪个模型」时先调它** |
| `list_models` | 列出可用模型及其能力 / 模式 / 默认参数 / 别名 |
| `generate_image` | 文生图；支持 `negative_prompt`/`seed`/`size`/`steps`/`cfg`/`workflow_overrides`(JSON 字符串)/`filename_prefix`/`mode`；返回 OpenAI 风格响应（含 `seed` 回显，url 已绝对化）；视频模型自动转视频链路。**编辑图片不要用本工具**（用 `edit_image` / `remove_background`） |
| `edit_image` | 编辑已有图片（独立工具）：`prompt` + `image`（路径/URL/dataURL/base64）；`model` 默认 `flux2-klein-image-edit-turbo`（语义改写），改图内文字传 `boogu-image-edit-turbo` / `boogu-image-edit`；**多图参考**传 `reference_images`（klein 4 槽 / qwen 6 槽）；传文生图/视频模型会被 400 拒绝并列出可用编辑模型 |
| `generate_video` | 文生视频 / 首尾帧生视频 / 参考生视频；参数 `prompt`/`model`(默认 `minimax-h3`)/`duration`/`fps`/`size`/`seed`/`negative_prompt`/`first_frame`/`last_frame`/`reference_images|videos|audios`（按模型槽位生效，帧与参考可同传）/`steps`/`attention`/`output_size`/`scale`（仅 lift，互斥）/`filename_prefix`/`response_format`/`background`；`background:"pending"` 异步，再查 `get_task` |
| `remove_background` | 图片去背景（BiRefNet，独立工具，无需提示词）；参数 `image`(本地路径/URL/dataURL/base64)、`response_format`(默认 url)、`filename_prefix` |
| `get_task` | 查询异步任务状态与产物（含 `prompt_id`、是否有工作流快照、`output`、`error`）；同步回执里的 `task_id` 同样能查 |
| `cancel_task` | 取消任务：`task_id` 非空取消指定任务（pending 移出队列 / running 中断）；**空则中断 ComfyUI 当前执行任务** |
| `queue_status` | 队列监控：ComfyUI running/pending + 网关 tasks（与 REST `/roundabout/admin/queue` 同源） |
| `get_workflow` | 三层查找工作流 JSON：队列 → history → 任务快照 |
| `reload` | 热加载 `models.yaml` |
| `health` | 网关与 ComfyUI 后端健康状态 |
| `get_view_url` | 返回可视化页面地址（`{url}`，浏览器直接打开）：浏览 input/output 资源 + 实时任务进度。用户问「生成的东西在哪看」「给我查看页面」时调用，把 `url` 原样给用户 |
| `check_weights` | **权重体检**（只读、不占 GPU）：列出内置工作流当前缺失的权重文件与每条的下载命令，避免等到 `generate*` 报 400 才发现权重没下 |
| `get_skills` | 返回配套 agent-skills 的清单与安装地址（`roundabout` / `h3-playbook` / `qwen-image-prompt-writing`），每条带 `source`（本网关维护 / 模型官方维护）与 `skill_version`（该 skill 当前应有的内容版本）。本地已装 skill 的 frontmatter `skill_version` 低于此值 ⇒ 副本过期，按 `install_url` 重装 |
| `view_board` | 看板**唯一操作入口**，按 `action` 分发（顶部无限画布）：<br>**`pin`** — 钉卡：产物来源 `url` / `path` / `task_id` 三选一（优先级依次降低），`x`/`y` 给坐标（不给则自动排到空位，**自动排布永不遮挡**；显式坐标压到已有卡时回执带 `covered`），`w`/`h` 定尺寸（**px 单位**，40–2000，超界压回且回 `size_adjusted:true`），`note` 可写说明；`path` 在 input/output 内的目录 → **目录卡**，input/output **之外**的真实路径 → **外部卡**（`ext:{path,is_dir}`）。**批量传 `items: [{...}, ...]`**（一次落盘、按数组顺序排布；批量时同传单卡字段返回 400）。返回 `view_url`；**要不要把页面地址给用户由 agent 自行判断**<br>**`remove`** — 删**一张**卡（`id` 必填；删单张不必清整板重钉），id 不存在回 `{ok:false, code:"board_item_not_found"}`<br>**`clear`** — 清空看板并**归档进历史**（`label` 命名；不给则按内容自动命名，如「7 张 · image/text · 10:24」）<br>**`get`** — 读**当前看板**卡片清单 + **内联最近 3 份归档摘要**（`history_limit` 可调、0 = 不要），钉东西前先看一眼免得钉重<br>**`history`** — 列历史归档（摘要带指纹 `kinds` / `preview` / `models`）；传 `archive_id` 返回该份完整卡片<br>**`load`** — 把归档**载回**当前看板（**替换**语义，当前非空先自动归档、回 `auto_archived`）；`archive_id` **优先用用户从页面「历史」复制的 12 位十六进制**，留空 = 载回最近一份；id 不存在回 `archive_not_found`（两者都不是工具异常） |

> `get_skills` 的清单与 `skill_version` 单一数据源在 `gateway/skills.py`：bump skill 内容版本时
> 同步那里，`tests/test_skill_version_sync.py` 会拿它与本机已装 skill 的 frontmatter 对拍
> （历史上版本号曾与 skill 仓库各改各的，结果是静默漂移——agent 永远以为本地副本不过期）。

> **错误形态（MCP）**：「目标不存在」一族（`task_not_found` / `prompt_not_in_queue` /
> `archive_not_found`，含钉卡时给未知 `task_id`）**不抛工具异常**，统一回
> `{ok:false, error, code, status:404}` —— agent 按同一结构解析即可，不必逐工具写分支。
> 参数非法（如 `items` 非数组、批量同传单卡字段）与上游/内部错误**照常抛异常**：那些是 bug，不伪装成业务失败。

> ⚠️ MCP 工具的形参是**逐个手写**的，与 REST 的 pydantic 请求模型是两条独立路径，二者并不自动对齐。
> MCP SDK 的参数模型沿用 pydantic 默认的 `extra="ignore"`：**传入未声明的字段不报错、被直接丢弃**。
> 所以遇到「文档里有的参数传了却没生效」时，先核对本表 —— 参数名不在上面就是被静默吃掉了。
> 覆盖度由 `tests/test_mcp_param_coverage.py` 守护（REST 新增字段而 MCP 漏暴露、或加了形参忘了透传，都会直接测试失败）。
> 另注：MCP 未暴露的字段仍可走 REST 端点传（两条路径最终汇入同一个 pipeline）。
> 增删 MCP 工具要同步 `tests/test_mcp_default_on.py` 的 `tools/list` 计数断言、本节的表与计数、
> 以及 `toolinfo._endpoints()["mcp"]`（后者由 `tests/test_toolinfo.py` 核对）。

### 7.1 可视化页面（view.html）

`get_view_url` 返回形如 `http://127.0.0.1:8188/roundabout/view` 的地址，浏览器打开即可：

- **资源浏览**：顶部分三个标签页 **Output / Input / 看板**（看板与资源列表互斥显示，`看板` 标签上带卡片数角标）；资源页里切换 Output / Input 根目录，面包屑进入子目录；图片走 ComfyUI `/view` 的 webp 缩略图，视频内联播放（缩略图上叠播放角标），音频点开弹层播放，点击弹出大图/播放器。文件**默认按修改时间倒序**（刚生成的最靠前，目录始终排在最前），底部有首页/上一页/下一页/末页。

**每页条数按视口高度自适应**：列数由网格宽度（`minmax(148px, 1fr)` + 12px 间距）决定，行高 = 列宽（1:1 缩略图）+ 文字区（首屏后从真实节点量取），可见高度 = 视口高 − sticky header − 面板标题栏 − 面板内边距 − 分页条预留。行数 × 列数即一页条数（上限 500，即后端 `limit` 上限），所以大屏可能一页 40~60 条、小窗口只有十几条。窗口 resize、旋转屏幕、展开/收起右下角任务面板（会改变网格宽度）都会在布局动画结束后重新测量，并把 `offset` 对齐到新页边界重取，位置大致不变。前端会把自己算出的 `limit` 一并传给 `/files`，未传时后端仍按默认 120。
- **产物自动同步**：每 4 秒轻量探测当前目录（只取第一条，几十字节）——**有新产物就自动刷新**，不用手点刷新。若你正在翻页或开着大图，则不打断，改为左下角出现半透明胶囊「发现 N 个新产物 · 查看」，点它回到第一页。标签页切到后台时暂停探测。
- **任务进度**：默认收起为右下角半透明悬浮按钮（`任务进度 · N 条`），点开向上展开面板；有任务在跑时按钮上出现呼吸蓝点。展开后每 2 秒轮询：顶部是 ComfyUI 队列（执行中 / 排队计数），下面是**网关任务表**（同步与异步生成都在内：状态 排队中 / 执行中 / 已完成 / 失败 / 已取消、模型、耗时、任务 id）；成功后附「查看产物」链接，失败显示错误摘要，最多渲染最近 50 条。

> 队列区与任务表是两层信息：任务表记录「谁提交了什么、成了什么」，队列反映「此刻 ComfyUI 在算什么」。
> 后端不可达时队列区标记不可达，任务表照常显示。

- **任务看板**：`看板` 标签页里一块可平移 / 缩放的**无限画布**（高度按视口铺满），由 agent 把产出**钉**上去（可按语义排布：分镜顺序、A/B 对照、按角色分组），用户在页面上一眼看全，不必 agent 逐个把文件拉出来。视频卡缩略图上叠播放角标、点开走同一个灯箱；**目录卡**（agent 钉 input/output 内的目录）点一下就切到资源列表并**进入该目录**；**外部卡**（产物落在 input/output 之外）左上角标「外部」，点它 → 确认 → 交给系统文件管理器打开（仅本机访问时有效，见下）；没有可预览产物的卡片（纯文本卡等）点了会给一条提示，不会毫无反应；右上角 × 移除单张。agent 换任务时调 `view_board(action="clear")` 清空 —— **清空即归档**，随时可在页面「历史」里回看，或**直接删掉某份归档**（删前会确认，不可恢复）。**回看归档是只读的**（`×` 与「清空并归档」在这个模式下连同消失）；要把某份变回当前看板，在回看时点「用这份替换当前看板」（先过确认层，当前内容自动归档），或由 agent 调 `view_board(action="load")`；agent 想看当前板上有哪些卡片用 `view_board(action="get")`。**历史里每一份都有「复制 ID」**：把这串 ID 粘给 agent，就等于指定「接着这一轮继续」—— 他直接载入，不必先列一遍历史摘要去猜哪份是你要的（用户看得见名字、张数与时间，他给的总比自己猜准）。复制优先走 `navigator.clipboard`，用局域网 IP 打开的非安全上下文自动回退 `execCommand`；两条都不通时把 ID 摊在提示里让人手抄，不做成「点了没反应」。看板落在节点 `.cache/board.json`，重启 ComfyUI 后仍在。agent 一次钉多张走 `view_board(action="pin")` 的 `items` 数组：一次往返、一次落盘，且严格按数组顺序排布（逐张调不但慢，并发时位置还会乱）。不给名字的归档由后端**按内容自动命名**（张数 + 类别 + 时间），列表摘要另带 `kinds`/`preview`/`models` 指纹 —— 人和 agent 都一眼认得出该续哪一份。
- **外部路径与文件管理器**：产物落在 ComfyUI 的 input/output 之外时，页面既拿不到 `/view` 地址也读不到缩略图，唯一有意义的动作就是交给操作系统 —— 点外部卡会在跑 ComfyUI 那台机器上拉起文件管理器（目录直接进入，文件定位并选中）。两个边界：①**只在从本机（回环）打开页面时执行**，从局域网别的机器点会收到一句「窗口开不到你这边」的明确提示，而不是静默无反应；②接口只认**当前看板上的卡片 id**，可打开的路径集合恒等于 agent 钉过的那些，卡片被清空后那条路径也随之失效。

支撑端点：

| 端点 | 说明 |
|---|---|
| `GET /roundabout/view` | 页面本身（读 `web/view.html`） |
| `GET /roundabout/view/files?root=output\|input&path=子/目录&offset=0&limit=120&sort=mtime&order=desc` | 列目录，返回 `crumbs`/`dirs`(全量，按名升序在前)/`files(name,size,mtime,kind,url,thumb)` + 分页元信息 `total`/`dir_count`/`offset`/`limit`/`has_more`/`sort`/`order`。`limit` 默认 120、上限 500；`sort=mtime`(默认，新的在前)\|`name`，`order=desc`(默认)\|`asc`；扫描硬上限 20000 条，超出置 `truncated` |
| `GET /roundabout/view/tasks` | 任务快照：`tasks[]`（`status`(OpenAI 枚举) / `model` / `elapsed` / `prompt_id` / `url`(已绝对化) / `path`(产物在 input/output 之外时给原路径，不可点) / `error`）+ `queue`（ComfyUI `running`/`pending` 的 prompt_id 列表，`reachable` 标记）。产物地址基准：`PUBLIC_BASE_URL` → 请求 origin，保证远程浏览器可打开；结果是磁盘路径时自动翻译成 `/view?filename=..&type=..` |
| `GET /roundabout/view/board` | 当前看板：`items[]`（`id`/`title`/`kind`(image\|video\|audio\|file\|dir\|text)/`url`/`thumb`/`note`/`model`/`x`/`y`/`w`/`h`/`origin`/`task_id`，目录卡另有 `dir:{root,path}` 且 `url=null`；外部路径另有 `ext:{path,is_dir}` 且 `url=null`）+ `count` + `history_count`。前端每 2 秒轮询 |
| `POST /roundabout/view/board/items` | 钉卡片：单张用顶层字段，**批量**用 `{items: [{...}, ...]}`（一次落盘、按数组顺序排布；`items` 与顶层单卡字段同传返回 400，不静默择一）。产物来源 `url` \| `path` \| `task_id` **三选一**（优先级依次降低，未知 task_id 返回 404 `task_not_found`）；`x`/`y` 给坐标（不给则按网格自动找空位，**自动排布永不遮挡已有卡**；显式坐标原样摆放、压到已有卡时回执带 `covered:[{id,title}]`——后钉的显示在上层）、`w`/`h` 定尺寸（**单位 px，有效范围 40–2000**，超出被静默压回边界且回执带 `size_adjusted:true`）、`note` 写说明；都没有则退化为纯文本卡（`kind=text`），全空返回 400。`url`/`path` 指向 input/output 内的**目录**时自动落成 `kind=dir` 并附 `dir:{root,path}`（显式传 `kind=dir` 但并非目录返回 400）。`path` 是本机 input/output **之外**的绝对路径且真实存在时附 `ext:{path,is_dir}` 标为外部（`kind` 仍按扩展名推、`url=null`、原路径留在 `path` 便于复制）；相对路径不认（会按进程 CWD 解析，语境不对）。`covered` 只进回执不落库 |
| `DELETE /roundabout/view/board/items/{id}` | 删单条（卡片右上角 ×）。不存在返回 404 `board_item_not_found` |
| `DELETE /roundabout/view/board?label=xxx` | 清空当前看板并**归档**（`label` 给这份归档命名；缺省按内容自动命名 = 张数 + 类别 + 时间，如 `7 张 · image/text · 10:24`）。空看板不产生归档；返回 `cleared` 与 `archived` |
| `GET /roundabout/view/board/history` | 历史归档摘要列表（`id`/`label`/`created`/`count` + 内容指纹 `kinds`/`preview`/`models`，新的在前；最多保留 20 份） |
| `GET /roundabout/view/board/history/{id}` | 某份归档的完整卡片（含坐标，可直接画出来），顺带返回当前看板快照。不存在返回 404 `archive_not_found` |
| `DELETE /roundabout/view/board/history/{id}` | 删掉某份归档（**不可恢复**，页面先弹确认再发）。返回 `removed`（被删的卡片数）与剩余 `count`；不存在返回 404 `archive_not_found` |
| `POST /roundabout/view/board/history/{id}/load` | 把某份归档载回当前看板（页面入口在**回看归档时**的「用这份替换当前看板」，先过确认层；agent 走 `view_board(action="load")`）。当前看板非空会**先自动归档**，不静默覆盖（`auto_archived` 回该归档 id） |
| `POST /roundabout/view/reveal` | 在某张看板卡片指向的路径上拉起系统文件管理器（目录进入 / 文件定位选中）。body `{id}`，**只认当前看板上的卡片 id**（归档里的不算）—— 能打开什么恒等于 agent 钉过什么，卡片清空后该路径随之失效。**默认仅本机访问时执行**：非回环或带 `X-Forwarded-For`/`X-Real-IP` 一律 403 `reveal_not_local`（远程点也只会开在服务器那台，说不清不如拒绝）；页面经隧道/反代部署、但服务器就是用户自己的机器时，设环境变量 `REVEAL_ALLOW_REMOTE=1` 放开；卡片不带 `ext` 返回 400 `not_external`；路径已不存在返回 404 `path_missing` |

> 目录解析走 `folder_paths`，根目录固定为 input/output 两个，`..` 穿越返回 400 `bad_path`。
> 鉴权开启时浏览器无法带自定义头，页面支持 `?key=<api_key>`；`get_view_url` 会自动把 key 拼进返回的 url。
> 页面与前端资源改动只需**强刷浏览器**；新增/修改 Python 路由需重启 ComfyUI。

### 7.2 完成推送（替代轮询，仅在 `MCP_STATELESS=false` 时可用）

> 默认的**无状态模式没有推送通道**：通知挂在会话上，而无状态模式不建会话。想在异步任务完成时
> 收到通知，就得显式设 `MCP_STATELESS=false` 换回有状态模式（代价见 §2.3 的环境变量表与 §10 排障速查）。
> 默认模式下请轮询 `get_task(id)`——它不依赖任何会话，反而更稳（客户端重连也不会丢）。

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
  -d '{"model":"minimax-h3","size":"576p-16:9","steps":8,"prompt":"a cat walking in rain","duration":5,"background":"pending"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 查询
curl -s http://127.0.0.1:8188/v1/videos/tasks/$TASK

# 取消（如需）
curl -X DELETE http://127.0.0.1:8188/v1/videos/tasks/$TASK
```

### 8.3 MCP 客户端（伪代码）

```
call generate_video(prompt="a cat walking in rain", model="minimax-h3", size="576p-16:9",
                    steps=8, duration=5, background="pending")  → 返回 {id, status:pending}
# 默认（无状态模式）：轮询 get_task(id)，不依赖会话，客户端重连也不会丢
# 若显式设了 MCP_STATELESS=false：可保持 SSE 连接，等服务端 notifications/message 推送
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
| ComfyUI 重启后 agent 侧报 `Rejected request with unknown or expired session ID: <hex>`（仅 `MCP_STATELESS=false` 的有状态模式） | **不是故障，也与端口无关**。MCP streamable-http 的会话只存在后端进程内存里（`Mcp-Session-Id` → `_server_instances`），ComfyUI 重启即全部清空；agent 仍拿着旧 session ID 发请求，SDK 按 MCP 规范回 404 `Session not found` 并打这条 INFO（logger `mcp.server.streamable_http_manager`）。URL 走的是 ComfyUI 固定端口 `/mcp`，所以请求能到达服务器——若真是端口问题，症状会是「连接被拒」而不是「session 未知」 | agent 侧重新 initialize 即可（多数 MCP 客户端下次调用时自动重连；不自动重连的，重启该 agent 的 MCP 连接）。**默认的无状态模式不会出现这条**——见到它就说明有人把 `MCP_STATELESS` 显式设成了 `false`（为了换异步完成推送）：要么接受「重启后手动重连」，要么去掉这个配置改回默认 |
| 日志反复出现 `Created new transport with session ID: <hex>`（仅 `MCP_STATELESS=false` 的有状态模式） | 请求**没带 `Mcp-Session-Id`**（或带了已失效的），SDK 就为每个这样的请求新建一个会话——`streamable_http_manager.py` 有状态路径的「New session case」，INFO 级。典型原因是客户端不保存 initialize 响应里的 `Mcp-Session-Id`、每次工具调用都重新 initialize / 新建连接（即客户端按无状态方式在用它）。**注意**：SDK 默认 `session_idle_timeout=None`，空闲会话**不会被回收**，会话及其后台任务会持续堆积（`_server_instances` + 每会话一个 `run_server` task），不会自己释放 | 服务端无 bug，是客户端没有复用会话。能改客户端就让它保存并回带 `Mcp-Session-Id`；改不动就设 `MCP_STATELESS=true`（**已经是默认值**）——服务端不再建任何会话，这条日志消失，也顺带根治上面那条 `unknown or expired session ID`（代价同上：完成通知改为轮询） |
| MCP 握手成功但工具列表为空 | 后端 uvicorn 未起来（上一行日志） | 同上；确认启动日志出现 `MCP gateway: embedded streamable-http shared on ComfyUI port` |
| 访问远程 IP 不通（如 `:8188`）但本机 `127.0.0.1` 正常 | ComfyUI 未加 `--listen 0.0.0.0`，或反代未放行 SSE（`text/event-stream`）长连接 | 启动加 `--listen 0.0.0.0`；反代关闭缓冲、放行 `Accept: text/event-stream` |
| 生成后 `url` 是相对路径 | 未设 `PUBLIC_BASE_URL` 且请求 host 不可达客户端 | 设 `PUBLIC_BASE_URL=http://<对外地址>` |
| MCP `list_models` 看不到刚加的模型 / 新改的 `models.yaml` | 旧版 `mcp_server.py` 用绝对导入 `gateway.*`，与节点侧相对导入形成**两份 registry** | 升级代码后重启（已修，见 `tests/test_mcp_import_identity.py`）；平时加模型后 `POST /admin/reload` 即可 |
| MCP 创建的异步任务在 `/v1/videos/tasks/{id}` 或任务面板里查不到 | 同上（`task_store` 也是两份） | 同上 |
| 生成报 400 `Value not in list (vae_name: '...')`，但工作流 JSON 里已经是新文件名 | **工作流模板是进程启动时读进内存的** —— 改了 `workflows/*.json` 后进程仍持旧图（`registry.load` 期 `json.loads`，之后不再读盘）。该报错指的就是内存里那份旧值 | `POST /admin/reload`（只重读 yaml + workflows，不用重启）。改动 `.py` 才需要重启 |
| 视图页看不到刚生成的产物 | 页面每 4 秒探测一次；或标签页在后台（暂停探测）；或产物落在 input/output 之外 | 手动点「刷新」；确认产物目录是 ComfyUI 的 output |
| 任务面板一直空 | 后端未重启（旧代码只在异步视频时记任务）；或标签页后台 | `handlers.py` / `tasks.py` / `viewer.py` 改动需重启 ComfyUI |
| 生成报 400 `ComfyUI rejected the workflow: ... Value not in list (...)` | **权重文件没装**，不是参数写错 —— ComfyUI 的 combo 校验把本地已装的列成了候选值 | 该报错已被网关改写成下载指引（文件名 + 目标目录 + `curl`）；想知道还缺哪些用 `GET /roundabout/admin/weights` 或 MCP `check_weights`。逐条来源见 [README 权重清单](README.md) |
