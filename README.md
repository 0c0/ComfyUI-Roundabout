# ComfyUI-Roundabout

> 一个 ComfyUI **custom node**：把本地工作流包装成 **OpenAI 兼容 REST API** + **MCP 工具**（14 个），让 AI agent / 脚本 / 任意 OpenAI 客户端直接调用你本机的图像与视频生成。

**不碰画布、不改代码。** 你在 ComfyUI 里搭好的流程，导出一个 JSON、在 `models.yaml` 写一段参数映射，就变成了一个可被任意客户端调用的 `model`。

不止是「把 ComfyUI 接到 MCP 上」。大模型工作流的**显存分块、分辨率档位、注意力档位**这些「跟着硬件和目标变的经验值」都不写进工作流 JSON，而由网关在渲染前代入 —— 换张卡、换个分辨率，工作流 JSON 一个字都不用改。**只是「配置化」的深浅不同**：分块档位在 `models.yaml`（改 YAML 热加载即生效），分辨率与注意力档位的取值是 `gateway/params.py` 里的常量（改了要重启）。

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
| 参数名从哪来 | 靠节点 schema / 试错 | 网关固定的参数白名单（清单见 WORKFLOWS.md） |
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

**4. 硬件与画质经验值下沉到配置层，换机器不换工作流**

同一份工作流 JSON，在 8 GiB 笔记本卡和 24 GiB 台式卡上最合适的分块参数差好几倍。这些值写死在 JSON 里，换机器就得改图。Roundabout 把它们从工作流里提出来，由网关在渲染前代入：

| 机制 | 跟着什么变 | 网关做什么 | 取值在哪 |
|---|---|---|---|
| `vram_adaptive` | 显卡显存 | 启动时探测显存，取「`min_gb` 不超过本机显存」的最大一档，覆盖分块参数 | `models.yaml` 的 `defaults.vram_tiers` |
| 分辨率档位 | 请求里的 `size` | 把 `576p-16:9` 这类档位名解析成具体宽高（六档 × 五种比例） | `gateway/params.py` 的 `VIDEO_RES_PRESETS`（**代码常量**） |
| 注意力档位 | 请求里的 `attention` | `sparse` / `dense` → `BlockSparseAttention.start_percent` | `gateway/params.py` 的 `ATTENTION_SPARSE_START`（**代码常量**） |

第一行改 YAML、热加载即生效；后两行是代码常量，改了要重启 ComfyUI。三者都不进工作流 JSON—— 工作流永远只有一份模板。细节见[按显卡自动调参](#按显卡自动调参vram_adaptive)。

---

## 文档分工

| 文档 | 内容 |
|---|---|
| **README.md**（本文件） | 这个插件是什么、能做什么、怎么装、怎么用 |
| **[WORKFLOWS.md](WORKFLOWS.md)** | 接入**你自己的**工作流：参数映射写法、`models.yaml` 字段、参考样本、排错表 |
| **[API.md](API.md)** | 完整接口契约：REST 端点与字段、MCP 工具、响应结构、鉴权、全部环境变量、排障 |

---

## 配套 skills

本仓库的三份文档是**给人读**的；配套 skill 是同一套知识的**给 agent 读**版本 —— 把调用姿势、注册流程、权重坑位压成 agent 能直接加载的操作手册。装了 skill 的 agent 不必先通读文档，也不会照着过期印象乱传参。

| skill | 内容 | 什么时候用 |
|---|---|---|
| [`roundabout-skill`](https://github.com/0c0/roundabout-skill) | 本插件的**总入口**：选模型、走 REST / MCP 生成、把工作流接进网关或下线、权重下载、排障运维、核对「文档与实现是否一致」 | agent 要驱动本机 ComfyUI 出图出视频，或要改 `models.yaml` / 加工作流时 |
| [`h3-playbook-skill`](https://github.com/0c0/h3-playbook-skill) | MiniMax H3 的官方口径：提示词公式、三类生成模式（文生 / 首尾帧 / 全能参考）的写法差异、素材用途标签、时长与宽高比边界 | 写 / 改 H3 提示词，或判断某个需求 H3 能不能做时 |
| [`qwen-image2.1-prompt-writing-skill`](https://github.com/0c0/qwen-image2.1-prompt-writing-skill) | Qwen-Image-2.1 官方 Prompt Enhancer 契约的手写替身：t2i 观察者报告与 edit 改写指令，产出 `rewritten_prompt` + `wh_ratio` / `ratio_follow` 结构 | 用网关 `qwen-image-2.1` 档出图 / 改图，或要把一句粗糙需求扩写成该模型能吃的描述前必查 |
| [`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing) **（MiniMax 官方）** | H3 提示词写作：T2VA / I2VA / FL2VA / L2VA 的最终结构，以及 Ref2VA 六段改写格式 | 写 H3 提示词时与上面的 playbook 配合：playbook 管能不能做，这份管字段与段落怎么写 |

装法就是把这个目录放进 agent 的 skills 目录（目录名与 skill 的 `name` 保持一致）：

```bash
git clone https://github.com/0c0/roundabout-skill.git <agent 的 skills 目录>/roundabout
git clone https://github.com/0c0/h3-playbook-skill.git <agent 的 skills 目录>/h3-playbook
git clone https://github.com/0c0/qwen-image2.1-prompt-writing-skill.git <agent 的 skills 目录>/qwen-image-prompt-writing
```

第四份 `h3-prompt-writing` 是**模型官方 skill**，它**不是独立仓库**，而是 MiniMax-H3 仓库里的一个子目录（`skills/h3-prompt-writing`），所以没有对应的整仓 clone 命令 —— 按下面的 tree 链接进去取该目录即可（该仓体量不小，别整仓克隆）。

**一句话安装**（把这句丢给 agent 即可，装在哪、怎么落位由它按自家约定处理）：

```text
安装这个skill https://github.com/0c0/roundabout-skill
安装这个skill https://github.com/0c0/h3-playbook-skill
安装这个skill https://github.com/0c0/qwen-image2.1-prompt-writing-skill
安装这个skill https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
```

> 前三份由本仓库维护（`MCP get_skills` 里标 `source="roundabout"`），最后一份是 **MiniMax 官方**（`source="official"`）——用的时候留意：它的内容更新由 MiniMax 决定，我们不跟进。
> 都按「先自己探、探不到再问」的方式取 ComfyUI 路径与端口，**不含硬编的内网地址**，换机器可直接用。
> 边界互不重叠：`roundabout` 只管网关这一侧（怎么调用、怎么注册、怎么排障）；剩余三份只管模型侧，其中 H3 占两份（playbook 管能力边界与能不能做，writing 管提示词字段与段落怎么写），Qwen-Image-2.1 占一份。

---

## 功能

**两套接入层，共享同一个引擎**

- **OpenAI 兼容 REST**：`/v1/images/generations`（文生图 / 图生图）、`/v1/images/edits`（multipart 标准编辑）、`/v1/images/remove-background`（去背景）、`/v1/videos/generations`（视频，支持异步）。返回格式可选 `b64_json` / `url` / `file` / `path`，可直接替换 OpenAI 官方地址使用。
- **MCP 服务（20 个工具）**：生成类 `generate_image` / `edit_image` / `remove_background` / `generate_video`，查询类 `get_tool_info` / `list_models` / `get_task` / `cancel_task` / `queue_status` / `get_workflow` / `health` / `check_weights`，运维类 `reload` / `get_view_url` / `get_skills`，看板类 `pin_view_item` / `clear_view_board` / `get_view_board` / `get_view_board_history` / `load_view_board`。agent 用一组工具就能完成「查模型 → 体检权重 → 生成 → 跟踪进度 → 钉到看板」全流程。
- **共享端口**：MCP 端点 `/mcp` 直接挂在 ComfyUI 同一端口（`http://<comfyui>:8188/mcp`），不用额外开端口、不用另起进程；REST 与 MCP 共用同一份注册表、生成链路与任务表。

**声明式模型注册**

- `models.yaml` 登记模型：工作流文件 + 参数绑定路径 + 默认值 + 别名 + 能力（文生图 / 图生图 / 视频）。
- 新增、修改模型只改 YAML，`POST /admin/reload` 热加载，**不用重启 ComfyUI、不用写代码**。
- 内置 15 个开箱可用的工作流（8 图像 + 6 视频 + 1 工具），也全部可以作为你写映射时的参照。

**按机器自适应**

- **显存自适应**（`vram_adaptive`）：启动时探测显存，自动为 H3 系列选取分块档位，8 / 12 / 24 GiB 卡共用同一份工作流。
- 分块参数可在单次请求里显式覆盖，改档表不用动代码。

**看得见、管得了**

- **可视化页面** `http://<comfyui>:8188/roundabout/view`：顶部 **Output / Input / 看板** 三个标签页，浏览 `input/` `output/` 资源（缩略图分批懒加载、自适应分页、产物自动同步、视频格带播放角标），右下角悬浮任务面板实时显示 ComfyUI 队列 + 网关任务表，产物一键弹层预览。
- **任务看板**：页面里的一个标签页，一块**铺满视口**的无限画布，agent 把产出钉上去并按语义排布（分镜顺序 / A/B 对照 / 按角色分组），用户一眼看全；钉进来的目录会变成可点的**目录卡**（一下跳到该目录），落在 `input/` `output/` 之外的产物变成标着「外部」的**外部卡**（点它先确认，再交给系统文件管理器打开 —— 仅本机访问时有效）；清空即归档进历史，随时**只读回看**或删掉某一份 —— 回看时若想把某份变回当前看板，点「用这份替换当前看板」（先确认，当前内容会自动归档）；agent 侧读当前板上有什么用 `get_view_board`、续接上一轮用 `load_view_board`；**历史里每一份都能「复制 ID」** —— 用户把这串 ID 粘给 agent，就等于指定了续接哪一轮（agent 不必先扫一遍历史列表去猜）。
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

4. **准备模型权重**：仓库不含权重。内置 15 个工作流共引用 20 个权重文件（清单另列 3 个非内置引用的 LoRA，共 23 行 / 约 205 GB），全部来自 HuggingFace 上的 `Comfy-Org` 等官方仓库，逐条的下载命令与存放目录见下方[权重清单](#权重清单内置工作流的全部依赖)。**只想跑图像档的话约 77 GB**，可以先只下这一族。

   > **视频档还需要两个第三方节点包**：KJNodes（`MiniMaxChunkFeedForward`，6 支视频档全用）与 `comfyui-SelfLift`（lift 两支的 latent 上采样节点 `SelfLiftH3LatentLift`）。清单见下方[第三方节点依赖](#第三方节点依赖)。

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
  -d '{"model":"minimax-h3","size":"576p-16:9","steps":8,"prompt":"a cat walking in rain","duration":5,"background":"pending"}'
# 查结果（id 来自上一步返回）
curl http://127.0.0.1:8188/v1/videos/tasks/<id>
```

`size` 支持档位预设 `<tier>p-<ratio>`：tier ∈ `480p` / `576p` / `720p` / `768p` / `1080p` / `1440p`，ratio ∈ `1:1` / `3:4` / `4:3` / `16:9` / `9:16`（如 `768p-16:9` = 1360×768、`1080p-16:9` = 1920×1088、`1440p-16:9` = 2560×1440）；也接受反向写法 `<ratio>@<tier>p`（如 `9:16@768p`）与直接 `WxH`。不传或 `auto` 用模型默认。

⚠️ **`1440p` 是大显存档**（2560×1440 ≈ 768p 的 3.6 倍像素）：8GB 卡**直接生跑不动**，要更大画面请优先走 `minimax-h3-lift`（768p 画布 × 1.875 = 2520×1440）。

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

> URL 里的主机端口 = 你访问 ComfyUI 的地址，路径固定 `/mcp`。远程访问换成 `http://192.168.1.10:8188/mcp` 即可，其余不变。
> 工具参数、完成推送（`notifications/message`，需显式 `MCP_STATELESS=false`）与助手侧注意事项见 [API.md §7](API.md)。

**agent 典型流程**：`list_models` 看有什么 → `generate_image` / `generate_video` 生成 → 异步任务用 `get_task` 轮询（默认无状态模式；设了 `MCP_STATELESS=false` 才可等服务端推送）→ 产物地址交给用户 → `get_view_url` 给出可视化页面。
> agent 侧的操作手册（调用姿势、注册与下档、权重坑位、排障）见上方[配套 skills](#配套-skills) —— 装了 skill 的 agent 不必通读本仓库文档。

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
| 文生图 / 多图编辑 | `qwen-image-2.1` | 40 步，**文生与多图参考编辑同一支**：不带参考图即纯文生（原生 2K 档位，默认 1024x1024，尺寸直传 `size`）；带 1–6 张参考图（`reference_images`）即多图编辑，输出尺寸跟随第 1 张参考图 |
| 图像编辑 | `flux2-klein-image-edit-turbo` | 语义改写首选：换背景 / 换材质 / 增删物体（`edit_image` 默认）；**多图参考**最多 4 张 |
| 图像编辑 | `boogu-image-edit` / `boogu-image-edit-turbo` | 擅长改写 / 添加**图内文字**，30 步 / 6 步 |
| 图像工具 | `utility-birefnet-remove-background` | BiRefNet 抠图，输出透明 PNG（无提示词） |
| 视频 | `minimax-h3` / `minimax-h3-edit` | MiniMax H3（base / edit 均 30 步），支持 6 图 + 3 视频 + 3 音频参考；低显存分块按档位自适应（`vram_adaptive`） |
| 视频 | `minimax-h3-lift` | **base 骨架 + 确定性放大**：30 步原生采样 → 学习式 latent lift（1344x768 × scale 1.875 = 2520x1440），构图零重掷、纹理最强；支持首尾帧（`reference_images` 传 0 / 1 / 2 张 = 文生 / 首帧 / 首尾帧）；产物落 `video/H3_Lift`；`scale` 是请求参数（默认 1.875 → 2520x1440）；`rho` 精调走 `workflow_overrides`（`910.inputs.rho`） |
| 视频 | `minimax-h3-lift-edit` | 同上，改用 **edit 骨架**：Ref2VA 权重 + 参考槽全套 6 图 / 3 视频 / 3 音频。⚠️ **未标定**：步数 30（随 edit 统一），放大与参考的组合效果没做过 A/B |
| 视频 | `fasth3` | FastVideo FastH3 8 步蒸馏档；文生 / 首尾帧生视频（`reference_images` 传 0 / 1 / 2 张 = 文生 / 首帧 / 首尾帧）。首尾帧走**关键帧**槽 `first_frame` / `last_frame`，与自成一族的 `minimax-h3` 走参考图槽不是一条路。**定位草稿 / 快周转**：官方口径 8 步最优、改步数掉质量，且 09-20 分频实测其高频段整体过量（**不是 49/50 步的无损替代**），要最大质量用 `minimax-h3` |
| 视频 | `fasth3-edit` | 同权重改用 Ref2VA 聚合节点做参考生视频，参考槽全套 6 图 + 3 视频 + 3 音频；产物落 `video/FastH3`（不混进 `video/MiniMax_H3`）。⚠️ **占位档**：官方未蒸馏 Ref2VA，与 `fasth3` 共用同一份 fl2v 权重，参考效果未标定 |

- 编辑类模型**必须传图**：单图模型传 `image`；**多图模型**（`flux2-klein-image-edit-turbo` 4 张 / `qwen-image-2.1` 6 张）传 `reference_images` 按序喂槽，单图也可以直接传 `image`（等价第 1 张）。输出尺寸跟随输入图（boogu / klein 缩放到 1MP；qwen 按官方默认不做重采样，直接跟随第 1 张参考图），此时 `size` 不生效 —— `qwen-image-2.1` 是**文生与编辑同一支**，一张参考图都不传就是纯文生，那一支才用 `size`。
- 给文生图模型传 `image` 会被拒绝，错误信息里会列出所有支持输入图的模型名。
- 别名与绑定路径见 `models.yaml` 与 [API.md §5.4](API.md)。

---

## 权重清单（内置工作流的全部依赖）

**本仓库不包含任何权重文件**（体积与许可原因）。内置的 17 个工作流共引用 **22 个**权重文件，合计约 **215 GB**（图像档约 94 GB / 视频档约 121 GB）；下面两张清单表共 **25 行**，另 3 行是**当前无内置工作流引用**的 LoRA（2 个 Acc LoRA + 1 个 hyperflow LoRA），仅作参考，计入则约 222 GB。缺文件时报错形如 `value not in list: <字段>: <文件名>` —— 网关会把这条报错**改写成可执行的下载指引**（该文件放哪个目录、`curl` 命令是什么），agent 收到即可照做；也可以主动体检当前缺哪些：`GET /roundabout/admin/weights`，或 MCP 工具 `check_weights`（只读、不占 GPU）。逐条来源即下方清单，同源数据在 `weights.yaml`，由网关与体检读取。（`workflows/example_txt2img.json` 是接入样本，用你自己的 checkpoint，不计入这 22 个。）

这些文件基本都在 **HuggingFace 的 Comfy-Org 官方仓库**里（少数为模型原厂或社区仓库，已在表中标注）。国区建议把端点换成镜像，repo ID 与 repo 内路径完全一致：

```bash
set HF_ENDPOINT=https://hf-mirror.com          # Windows
export HF_ENDPOINT=https://hf-mirror.com       # Linux / macOS
```

> **⚠ 只有一个文件必须下载后改名**（最常见的卡点）：
>
> | 上游文件名 | 本仓库工作流引用的名字 |
> |---|---|
> | `minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors` | `minimax_h3_latent_upscaler_3d_fp16.safetensors`（去掉 `_conv_v1`） |
>
> 其余文件的下载名 = 使用名，下完直接放进目标目录即可。

### 目录速查

所有权重放进 `<ComfyUI>/models/` 下对应子目录（`models/` 就是 ComfyUI 根目录下那个）：

| 目标目录 | 放什么 |
|---|---|
| `models/diffusion_models/` | 主模型（UNET） |
| `models/text_encoders/` | 文本编码器 |
| `models/vae/` | VAE（视频档另有音频 VAE） |
| `models/loras/` | LoRA 与蒸馏加速权重 |
| `models/latent_upscale_models/` | SelfLift 的 latent 上采样权重 |
| `models/background_removal/` | BiRefNet 抠图 |

### 图像档（15 个文件，约 94 GB）

| 文件 | 目标目录 | 体积 | 下载源（HF repo） | repo 内路径 |
|---|---|---|---|---|
| `z_image_int8_convrot.safetensors` | `diffusion_models/` | 6.20 GB | `Comfy-Org/z_image` | `split_files/diffusion_models/` |
| `z_image_turbo_int8_convrot.safetensors` | `diffusion_models/` | 6.20 GB | `Comfy-Org/z_image_turbo` | `split_files/diffusion_models/` |
| `qwen_3_4b.safetensors` | `text_encoders/` | 8.05 GB | `Comfy-Org/z_image` | `split_files/text_encoders/` |
| `boogu_image_base_fp8_scaled.safetensors` | `diffusion_models/` | 10.31 GB | `Comfy-Org/Boogu-Image` | `diffusion_models/` |
| `boogu_image_turbo_hotfix_int8_convrot.safetensors` | `diffusion_models/` | 11.37 GB | `Comfy-Org/Boogu-Image` | `diffusion_models/` |
| `boogu_image_edit_int8_convrot.safetensors` | `diffusion_models/` | 11.37 GB | `Comfy-Org/Boogu-Image` | `diffusion_models/` |
| `boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors` | `loras/` | 1.35 GB | `Comfy-Org/Boogu-Image` | `loras/` |
| `qwen3vl_8b_fp8_scaled.safetensors` | `text_encoders/` | 10.59 GB | `Comfy-Org/Boogu-Image` | `text_encoders/` |
| `flux1_vae_bf16.safetensors` | `vae/` | 0.17 GB | `Comfy-Org/Boogu-Image` | `vae/` |
| `flux-2-klein-9b-kv-fp8.safetensors` | `diffusion_models/` | 9.82 GB | `black-forest-labs/FLUX.2-klein-9b-kv-fp8` | 根目录 |
| `flux2-vae.safetensors` | `vae/` | 0.34 GB | `Comfy-Org/vae-text-encorder-for-flux-klein-9b` | `split_files/vae/` |
| `qwen_image_2.1_int8_convrot.safetensors` | `diffusion_models/` | 7.26 GB | `Comfy-Org/Qwen-Image-2.1` | `diffusion_models/` |
| `qwen3vl_8b_int8_convrot.safetensors` | `text_encoders/` | 9.35 GB | `Comfy-Org/Qwen-Image-2.1` | `text_encoders/` |
| `qwen_image_2.1_vae_bf16.safetensors` | `vae/` | 0.68 GB | `Comfy-Org/Qwen-Image-2.1` | `vae/` |
| `birefnet.safetensors` | `background_removal/` | 0.44 GB | `Comfy-Org/BiRefNet` | `background_removal/` |

**几个共用 / 易混点：**

- `flux1_vae_bf16.safetensors` 就是 Z-Image / Boogu 全系要的那份 VAE，**不用再去找 `ae.safetensors`**：两者是同一个 FLUX.1 Autoencoder 的 fp32 / bf16 副本（244/244 张量在 bf16 下**逐位相同**），而 ComfyUI 默认就按 bf16 加载 VAE（`working_dtypes = [bf16, fp32]`）—— 读 `ae` 会先下转成 bf16，两者解码**逐像素相同**（同一 latent 实测 `max|Δ| = 0`、8-bit 下差异像素 `0.0000%`）。bf16 那份体积还不到一半。
- `qwen3vl_8b_fp8_scaled.safetensors` 被 Boogu 全系与 `flux2-klein-image-edit-turbo` 共用，下一个文件够三个模型用。
- `flux-2-klein-9b-kv-fp8.safetensors` 来自 Black Forest Labs 官方仓库（不在 Comfy-Org）；Comfy-Org 只提供了它的 VAE 与文本编码器仓库（`vae-text-encorder-for-flux-klein-9b`，官方拼写如此）。
- Qwen-Image 2.1 的文本编码器是 `qwen3vl_8b_int8_convrot.safetensors`，与 Boogu 用的 `qwen3vl_8b_fp8_scaled.safetensors` **同名不同文件、不同仓库**，别互相顶替 —— 跑哪支就下哪支。同仓库里另有 `qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors` / `..._pe_i2i...` 一路「PE」编码器，内置工作流**不用**它。
- 想省显存可以换更小的量化版（`nvfp4` / `pruned_int8_convrot` 等，同仓库同目录下有），但**要同步改工作流 JSON 里的文件名**。

### 视频档（10 行：7 个内置引用 + 3 个参考项，约 128 GB）

| 文件 | 目标目录 | 体积 | 下载源（HF repo） | repo 内路径 |
|---|---|---|---|---|
| `minimax_h3_fl2va_int8_convrot.safetensors` | `diffusion_models/` | 34.04 GB | `Comfy-Org/MiniMax-H3` | `diffusion_models/` |
| `minimax_h3_ref2va_int8_convrot.safetensors` | `diffusion_models/` | 34.04 GB | `Comfy-Org/MiniMax-H3` | `diffusion_models/` |
| `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | `text_encoders/` | 27.14 GB | `Comfy-Org/MiniMax-H3` | `text_encoders/` |
| `minimax_h3_video_vae_int8_convrot.safetensors` | `vae/` | 2.81 GB | `Comfy-Org/MiniMax-H3` | `vae/` |
| `minimax_h3_audio_vae_fp32.safetensors` | `vae/` | 0.61 GB | `Comfy-Org/MiniMax-H3` | `vae/` |
| `MiniMax-H3-FL2VA-Acc-8Step.safetensors` | `loras/` | 1.37 GB | `alibaba-pai/MiniMax-H3-Acc-LoRAs` | 根目录 |
| `MiniMax-H3-Ref2VA-Acc-8Step.safetensors` | `loras/` | 1.37 GB | `alibaba-pai/MiniMax-H3-Acc-LoRAs` | 根目录 |
| `minimax_h3_hyperflow_8step_v1.0_comfyui_bf16.safetensors` | `loras/` | 3.93 GB | `drbaph/MiniMax-H3-Turbo-Lora-ComfyUI` | 根目录（文件仍在盘上，但没有任何内置工作流引用它） |
| `fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors` | `diffusion_models/` | 22.13 GB | `Comfy-Org/FastVideo-FastH3` | `diffusion_models/` |
| `minimax_h3_latent_upscaler_3d_fp16.safetensors` | `latent_upscale_models/` | 0.69 GB | `LBH-123-AI/Minimax_h3_latent_Upscaler` | `minimax_h3_latent_upscaler_3d_conv_v1/`（**需改名**） |

**视频档的三个坑：**

- **`fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors` 跑的是 pruned 形态**，对应 `fasth3` / `fasth3-edit` 两支；`minimax-h3` 系列（含 lift）用的是**完整版** `minimax_h3_*_int8_convrot.safetensors`。两者不能互换，LoRA 也会跟着不匹配（见下一条）。
- **`minimax_h3_hyperflow_8step_v1.0_comfyui_bf16.safetensors` 是社区转换版**（原始版在 `videorebirth/hyperflow`，文件名 `minimax_h3_hyperflow_8step_v1.0.safetensors`，无 `_comfyui_bf16` 后缀）。转换版在重映射 key 时把端点适配器 `endpoint_time_embedder.*` 并进了 `time_embedder.proj_in/proj_out`，本机实测**端点条件进不了模型**（画面出现非单段式晕开）—— 想用完整效果需上游原版 + 专用节点包 `Addis-Pulse-Studio/ComfyUI-HyperFlow`。内置工作流目前挂的是转换版。
- **Acc LoRA 是 diffusers 命名，普通加载器挂不上**（详见[内置模型](#内置模型)里 `minimax-h3-turbo` 那行）。若要真正加速，改用 `Comfy-Org/MiniMax-H3` 的 `loras/` 下那些 **ComfyUI 命名**的，如 `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`。

### 一键下载

在 **ComfyUI 根目录**执行（`models/` 就在当前目录下）。按族分组，只下你要用的族即可。仓库内路径与 HF 一致；`hf-mirror` 之外，**ModelScope** 也可作备用源（同一份仓库，把 `$B/<repo>/resolve/main/<路径>` 换成 `https://modelscope.cn/models/<repo>/resolve/master/<路径>` 即可）。

```bash
B=https://hf-mirror.com          # 走官方就换成 https://huggingface.co

# ---------- Z-Image（z-image / z-image-turbo，约 20 GB）----------
curl -L -o models/diffusion_models/z_image_int8_convrot.safetensors \
  $B/Comfy-Org/z_image/resolve/main/split_files/diffusion_models/z_image_int8_convrot.safetensors
curl -L -o models/diffusion_models/z_image_turbo_int8_convrot.safetensors \
  $B/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_int8_convrot.safetensors
curl -L -o models/text_encoders/qwen_3_4b.safetensors \
  $B/Comfy-Org/z_image/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors

# ---------- Boogu（文生图 + 图生图，约 45 GB）----------
curl -L -o models/diffusion_models/boogu_image_base_fp8_scaled.safetensors \
  $B/Comfy-Org/Boogu-Image/resolve/main/diffusion_models/boogu_image_base_fp8_scaled.safetensors
curl -L -o models/diffusion_models/boogu_image_turbo_hotfix_int8_convrot.safetensors \
  $B/Comfy-Org/Boogu-Image/resolve/main/diffusion_models/boogu_image_turbo_hotfix_int8_convrot.safetensors
curl -L -o models/diffusion_models/boogu_image_edit_int8_convrot.safetensors \
  $B/Comfy-Org/Boogu-Image/resolve/main/diffusion_models/boogu_image_edit_int8_convrot.safetensors
curl -L -o models/loras/boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors \
  $B/Comfy-Org/Boogu-Image/resolve/main/loras/boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors
curl -L -o models/text_encoders/qwen3vl_8b_fp8_scaled.safetensors \
  $B/Comfy-Org/Boogu-Image/resolve/main/text_encoders/qwen3vl_8b_fp8_scaled.safetensors
curl -L -o models/vae/flux1_vae_bf16.safetensors \
  $B/Comfy-Org/Boogu-Image/resolve/main/vae/flux1_vae_bf16.safetensors

# ---------- FLUX.2 Klein 图像编辑 + BiRefNet 抠图（约 11 GB）----------
curl -L -o models/diffusion_models/flux-2-klein-9b-kv-fp8.safetensors \
  $B/black-forest-labs/FLUX.2-klein-9b-kv-fp8/resolve/main/flux-2-klein-9b-kv-fp8.safetensors
curl -L -o models/vae/flux2-vae.safetensors \
  $B/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/vae/flux2-vae.safetensors
curl -L -o models/background_removal/birefnet.safetensors \
  $B/Comfy-Org/BiRefNet/resolve/main/background_removal/birefnet.safetensors

# ---------- Qwen-Image 2.1（文生图 + 多图编辑，约 17 GB）----------
curl -L -o models/diffusion_models/qwen_image_2.1_int8_convrot.safetensors \
  $B/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors
curl -L -o models/text_encoders/qwen3vl_8b_int8_convrot.safetensors \
  $B/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors
curl -L -o models/vae/qwen_image_2.1_vae_bf16.safetensors \
  $B/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors

# ---------- MiniMax H3 共用主干（H3 全系都要，约 99 GB）----------
curl -L -o models/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors
curl -L -o models/diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors
curl -L -o models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors
curl -L -o models/vae/minimax_h3_video_vae_int8_convrot.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors
curl -L -o models/vae/minimax_h3_audio_vae_fp32.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors

# ---------- 视频档专用（按需选，全选约 30 GB）----------
# turbo 两支的 Acc LoRA
curl -L -o models/loras/MiniMax-H3-FL2VA-Acc-8Step.safetensors \
  $B/alibaba-pai/MiniMax-H3-Acc-LoRAs/resolve/main/MiniMax-H3-FL2VA-Acc-8Step.safetensors
curl -L -o models/loras/MiniMax-H3-Ref2VA-Acc-8Step.safetensors \
  $B/alibaba-pai/MiniMax-H3-Acc-LoRAs/resolve/main/MiniMax-H3-Ref2VA-Acc-8Step.safetensors
# fasth3 权重
curl -L -o models/diffusion_models/fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors \
  $B/Comfy-Org/FastVideo-FastH3/resolve/main/diffusion_models/fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors
# SelfLift 上采样权重（下完改名）
curl -L -o models/latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16.safetensors \
  $B/LBH-123-AI/Minimax_h3_latent_Upscaler/resolve/main/minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors
```

> 嫌 `curl` 麻烦也可以用 HuggingFace CLI（`pip install -U huggingface_hub`）：
> ```bash
> hf download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors \
>   --local-dir models
> ```
> 注意它**会保留仓库内的目录结构**（Comfy-Org 的 `split_files/` 前缀不是 ComfyUI 的目录），下完还得手动挪到上表的目标位置 —— 所以这里默认给 `curl`。

### 工作流 → 权重对照

| 工作流 | 需要的权重 → 目标目录 |
|---|---|
| `z-image` / `z-image-turbo` | `diffusion_models/` `z_image_int8_convrot.safetensors`、`z_image_turbo_int8_convrot.safetensors` · `text_encoders/` `qwen_3_4b.safetensors` · `vae/` `flux1_vae_bf16.safetensors` |
| `boogu-image-base` / `-base-4step` / `-turbo` | `diffusion_models/` `boogu_image_base_fp8_scaled.safetensors`、`boogu_image_turbo_hotfix_int8_convrot.safetensors` · `loras/` `boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux1_vae_bf16.safetensors` |
| `boogu-image-edit` / `-edit-turbo` | `diffusion_models/` `boogu_image_edit_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux1_vae_bf16.safetensors` ·（turbo 另需）`loras/` `boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors` |
| `flux2-klein-image-edit-turbo` | `diffusion_models/` `flux-2-klein-9b-kv-fp8.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux2-vae.safetensors` |
| `qwen-image-2.1` | `diffusion_models/` `qwen_image_2.1_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_8b_int8_convrot.safetensors` · `vae/` `qwen_image_2.1_vae_bf16.safetensors` |
| `utility-birefnet-remove-background` | `background_removal/` `birefnet.safetensors` |
| `minimax-h3` / `minimax-h3-edit` | `diffusion_models/` `minimax_h3_fl2va_int8_convrot.safetensors`、`minimax_h3_ref2va_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` · `vae/` `minimax_h3_video_vae_int8_convrot.safetensors`、`minimax_h3_audio_vae_fp32.safetensors` |
| `minimax-h3-lift` / `-lift-edit` | 同 `minimax-h3` / `minimax-h3-edit`，另需 `latent_upscale_models/` `minimax_h3_latent_upscaler_3d_fp16.safetensors`（**不需要** `loras/`） |
| `fasth3` / `fasth3-edit` | `diffusion_models/` `fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` · `vae/` `minimax_h3_video_vae_int8_convrot.safetensors`、`minimax_h3_audio_vae_fp32.safetensors` |

### 第三方节点依赖

> 这些工作流用到的节点**除 lift 系列（`minimax-h3-lift*`）、基础两支 `minimax-h3` / `-edit`、以及 FastH3 两支（它们都接了低显存分块节点）外，全部来自 ComfyUI 核心**（`comfy_extras/`），不需要装任何第三方 custom node 包；ComfyUI 版本太老会缺 `MiniMaxH3ReferenceToVideo` / `LoadBackgroundRemovalModel` / `Flux2Scheduler` / `TextEncodeQwenImage21` 等节点。
> lift 系列额外依赖一个第三方节点包：`comfyui-SelfLift`（`SelfLiftH3LatentLift` + `latent_upscale_models/` 下的上采样权重）。
> 需要 KJNodes 的 `MiniMaxChunkFeedForward` 做 FFN 分块：**全部 6 支视频档**。低显存都走两级 —— `BlockSparseAttention`（comfy 核心节点，省 attention）→ `MiniMaxChunkFeedForward`：base 四支（`minimax-h3` / `-edit` / `minimax-h3-lift*`）的稀疏档位是 `sol-attn`（training-free，约保留 16% key block），FastH3 两支是 `vsa`（其权重按 10% cube 稀疏训练）。
> ⛔ **不要在这 6 支里接 KJNodes 的 `MiniMaxLowVRAMAttention`**：它替换 `block.forward`，而 `BlockSparseAttention` 的 block patch 会无条件补传 `attention=` 关键字（`comfy/ldm/minimax/model.py`），签名对不上 ⇒ 实测 `TypeError`。两者**硬互斥**，因此 base 四支原先的 LowVRAM 节点已于 2026-09-21 撤除（`head_chunks` 档位值随之失去消费者，保留在表里仅为复原方便）。
> 稀疏节点的参数（`tau` / `min_tokens` / `dense_blocks` …）不在可注入白名单；其中 `tau` 的键名是 `selection.tau`（含点号），按 `.` 切分的路径解析寻址不到，要调只能改工作流 JSON 再 `/admin/reload`。**但「更快 ↔ 更高质量」这一档有正式请求参数**：`attention`（`sparse` 默认 / `dense` 关闭稀疏换致密画质）。它内部落到 `BlockSparseAttention.start_percent` —— `1.0` 因 `percent_to_sigma(1.0) = 0` 而等效全程致密。**这一档只给 base 四支**（`minimax-h3` / `-edit` / `-lift` / `-lift-edit`）；FastH3 两支恒定稀疏 —— 它的 `vsa` 与蒸馏权重配对训练，关掉不是「更高画质」而是脱离训练分布，传 `attention` 会报 400。
> 全部视频档的 `ModelAttentionBackend` 统一用 `comfy kitchen attention`（comfy_kitchen 的 INT8 实现，省显存）。要换 `pytorch attention` 得改工作流模板里那个节点的值；它不在 `bindings` 白名单里，但**可以**用 `workflow_overrides` 在运行时点改（`{"156.inputs.attention": "pytorch attention"}`）—— `workflow_overrides` 是任意路径注入，与白名单无关。
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
| `ROUNDABOUT_VRAM_GB` | 空 | 手动钉住显存档位（GiB），用于按显卡自动调参的模型（如 `minimax-h3-lift`）；不填则自动探测，探测不到就不覆盖工作流自带的值 |

### 按显卡自动调参（`vram_adaptive`）

大模型工作流（H3 一支的权重合计 ~65 GiB）靠节点级分块在显存吃紧的卡上跑，而分块参数的合适取值只取决于显存大小。在 `models.yaml` 给模型加一行 `vram_adaptive: true`，网关启动时探测本机显存，从 `defaults.vram_tiers` 取「`min_gb` 不超过本机显存」的最大一档，作为该模型分块参数的默认值：

```yaml
defaults:
  vram_tiers:
    - min_gb: 24        # 24 ~ 31 GiB（32 GiB 及以上有单独的档）
      chunks: 2
      seq_threshold: 16384
      head_chunks: 8         # ← 当前无节点绑定（见第三方节点依赖节）
      highres_tiling: true   # ← 当前无节点绑定
    # ... 12 / 8 / 0 各档
models:
  minimax-h3-lift:
    vram_adaptive: true
    bindings:
      chunks: 158.inputs.chunks
      seq_threshold: 158.inputs.seq_threshold
```

- 优先级：**档位值 < 模型自己写的 `defaults` < 请求参数**（请求里传 `chunks` 等可按单次任务覆盖）。
- 探测不到显存（纯 CPU / 无 torch）时不覆盖，行为与不声明 `vram_adaptive` 一致。
- 档位表在 YAML 里，改档位不用动代码；`ROUNDABOUT_VRAM_GB` 可手动钉住。

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
├── mcp_server.py          # MCP 服务（19 工具）+ 共享端口嵌入启动
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
├── weights.yaml           # 权重 → 下载来源索引（报错里的下载指引与体检的数据源）
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
| `test_e2e_sync_generation.py` | 真提交一次生图（z-image-turbo 512x512，真实占用 GPU） |
| `test_removebg_e2e.py` | 真跑 BiRefNet 去背景 |

`tests/run_tests.py` 默认跳过它们（名字含 `e2e`，或列在脚本顶部的 `GPU_TESTS` 里），要跑得显式加 `--all`。
其余用例全部离线、用系统分配的临时端口，不需要 ComfyUI 在跑。

## 许可

[MIT](LICENSE)
