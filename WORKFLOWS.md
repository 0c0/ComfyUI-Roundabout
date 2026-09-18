# 接入你自己的工作流

Roundabout 不自带任何"工作流导入"功能。它做的只有一件事：

> 读一份 **API 格式**的工作流 JSON，按 `models.yaml` 里声明的映射，把接口参数注进对应节点的 input，然后提交给 ComfyUI。

所以接入自己的流程 = **导出一个 JSON + 写一段映射**。`workflows/` 下的 15 个内置工作流只是可用样本，随时可以删。

---

## 第一步：导出 API 格式

ComfyUI 画布 → `Workflow` → **`Export (API)`** → 存到 `custom_nodes/ComfyUI-Roundabout/workflows/你的名字.json`

- 必须用 **Export (API)**，不是普通 `Export`。UI 格式（带 `nodes` / `last_node_id`）会被直接拒绝：`looks like a UI workflow`。
- 不要手改 JSON 内容，改参数走 `models.yaml`。
- API 格式长这样（键就是节点 id）：
  ```json
  "5": { "inputs": { "width": 1024, "height": 1024, "batch_size": 1 }, "class_type": "EmptyLatentImage" }
  ```

---

## 第二步：把接口参数映射到节点 input

参数名由网关规定，**路径你从 JSON 里抄**。一份 `bindings` 段就是全部工作。

### 可绑定的参数名（白名单）

`prompt` `negative_prompt` `width` `height` `seed` `steps` `cfg` `sampler_name` `scheduler` `denoise` `batch_size` `image` `mask` `filename_prefix` `duration` `fps` `num_frames` `mode` `chunks` `head_chunks` `seq_threshold` `highres_tiling`

**白名单以外的键会被静默忽略**（只在启动日志里留一条 warning）。厂商特有参数用 `workflow_overrides` 传，不要在 `bindings` 里造名字。

### 常见落点

| 参数 | 常见节点与字段 |
|---|---|
| `prompt` | `CLIPTextEncode.text`；`TextEncodeBooguEdit.prompt`；`TextEncodeMageFlowEdit.prompt`；视频链路的 `PrimitiveStringMultiline.value` |
| `negative_prompt` | `CLIPTextEncode.text`（独立负向节点）；`TextEncodeBooguEdit.negative_prompt` |
| `width` / `height` | `EmptyLatentImage`、`EmptySD3LatentImage`、`EmptyFlux2LatentImage` 的 `width` / `height` |
| `seed` | `KSampler.seed`、`RandomNoise.noise_seed` |
| `steps` | `KSampler.steps`、`BasicScheduler.steps`、`Flux2Scheduler.steps` |
| `cfg` | `KSampler.cfg`、`CFGGuider.cfg`、`SamplerCustom.cfg` |
| `sampler_name` | `KSampler.sampler_name`、`KSamplerSelect.sampler_name` |
| `scheduler` | `KSampler.scheduler`、`BasicScheduler.scheduler` |
| `denoise` | `KSampler.denoise`、`BasicScheduler.denoise` |
| `batch_size` | `EmptyLatentImage.batch_size` |
| `image` | `LoadImage.image` |
| `filename_prefix` | `SaveImage.filename_prefix`、`SaveVideo.filename_prefix` |
| `fps` | `CreateVideo.fps`、`VideoCombine.frame_rate` |
| `duration` / `num_frames` | 你自己链路里的时长/帧数节点 |
| `chunks` / `head_chunks` / `seq_threshold` | `MiniMaxChunkFeedForward.chunks` / `.seq_threshold`、`MiniMaxLowVRAMAttention.head_chunks`（KJNodes） |
| `highres_tiling` | `SelfLiftH3Sampler.highres_tiling`（comfyui-SelfLift） |
| `transition_step` | `SelfLiftH3Sampler.transition_step`（comfyui-SelfLift）—— 总步数仍走 `steps`（`BasicScheduler.steps`）。上界是 `steps + extra_steps - 1` 而非 `steps - 1`，原因见后文《SelfLift 的 σ 网格与 `extra_steps`》 |
| `lowres_scale` | `SelfLiftH3Sampler.lowres_scale`（comfyui-SelfLift）—— 一采的相对分辨率（0.25–1.0）。`"auto"` 由网关按目标尺寸反推，见后文《一采分辨率：`lowres_scale` 与 `auto`》 |

### 写映射的三条铁律

1. **别绑被连线覆盖的 input。** 值是 `["62", 0]` 这种数组，表示它接了别的节点的输出；注入字面量会把连线顶掉、把图改坏。只绑 `1024` / `"euler"` 这样的字面量。
   典型坑：编辑类工作流的 `EmptyLatentImage.width` 是从 `GetImageSize` 连过来的，**不要绑宽高**。
2. **一个参数可以绑多个路径**，写成列表即可。例：`seed: ["3.inputs.seed", "9.inputs.noise_seed"]`。
3. **没绑的参数保持工作流里的原值**。`defaults` 只决定"调用方不传时用什么"，照抄当前值就行。

### 找路径的方法

打开 API 格式 JSON：

```json
"3": { "inputs": { "seed": 123456789, "steps": 25, "cfg": 7.5 }, "class_type": "KSampler" }
```

→ 节点 id `3`，字段 `seed` / `steps` / `cfg` → 路径 `3.inputs.seed`、`3.inputs.steps`、`3.inputs.cfg`。

---

## 第三步：登记到 models.yaml

```yaml
models:
  my-model:                                     # 对外暴露的 model 名
    workflow: example_txt2img.json              # workflows/ 下的文件名
    description: 我的 SDXL 文生图                # 面板与 /v1/models 展示
    mode: image                                 # image | video（默认 image）
    capabilities: [text-to-image]                # 纯编辑模型只写 image-to-image
    output_node: '9'                            # 产物在哪个节点（SaveImage/SaveVideo 的 id）
    timeout: 300
    defaults:                                   # 调用方不传时的值
      width: 1024
      height: 1024
      steps: 25
    bindings:                                   # 接口参数 -> 节点路径
      prompt: 6.inputs.text
      negative_prompt: 7.inputs.text
      seed: 3.inputs.seed
      steps: 3.inputs.steps
      cfg: 3.inputs.cfg
      sampler_name: 3.inputs.sampler_name
      scheduler: 3.inputs.scheduler
      denoise: 3.inputs.denoise
      width: 5.inputs.width
      height: 5.inputs.height
      filename_prefix: 9.inputs.filename_prefix
    aliases: [mysd, my-sd]                      # 可选，不区分大小写
```

### 全部字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `workflow` | ✅ | `workflows/` 下的文件名；也可写绝对路径 |
| `bindings` | ✅ | 参数 → 路径（或路径列表）。**启动即校验路径存在**，写错会拒绝加载并指出是哪一条 |
| `output_node` | 建议 | 产物节点 id；不填则从整图里找产物 |
| `prompt` 绑定 | ✅ | 不绑 `prompt` 就必须显式 `promptless: true`（工具类工作流） |
| `mode` | | `image` / `video`，影响走哪套端点与参数校验 |
| `capabilities` | | `text-to-image` / `image-to-image`。**只写 `image-to-image` 的模型会被强制要求传 `image`** |
| `defaults` | | 参数默认值，与顶层 `defaults.params` 合并（模型级优先） |
| `timeout` | | 单任务超时秒数 |
| `aliases` | | 别名列表 |
| `promptless` | | 无提示词的工具类工作流（去背景、超分…） |
| `vram_adaptive` | | `true` 时按本机显存从顶层 `defaults.vram_tiers` 取一档，作为分块参数的默认值（见下节） |
| `references` | | 视频参考槽位拓扑，见下 |
| `sizes` / `mode_choices` | | 面板与接口暴露的候选值 |

**参考样本**：`workflows/example_txt2img.json` 是最小可跑样本（CheckpointLoaderSimple + KSampler + SaveImage），把 `ckpt_name` 换成你自己的 checkpoint 名，配上上面那段 YAML 就能用。

---

## 让用户跑起来的三种方式

### A. 面板点选（推荐）

ComfyUI 菜单 **Roundabout → 工作流管理** → 选择 API 格式 JSON → 勾选 **「同时创建模型条目」** → 上传。

网关会自动分析工作流并生成 `models.yaml` 条目（`POST /roundabout/admin/workflows/upload`，`create_model=1`），然后：

1. 到 **「模型配置」** 标签核对生成的绑定，按第二步的铁律改掉错的那几条
2. **重新加载**（或重启 ComfyUI）
3. 直接调接口验证

自动分析是启发式的，**图像类工作流基本能一次到位**；这几处它必然识别不了，要手工补：

| 场景 | 自动漏掉什么 | 补法 |
|---|---|---|
| 视频工作流 | `width` / `height`（挂在聚合器节点上）、`duration`、`fps` | 照 `models.yaml` 里 MiniMax H3 那几条写 |
| 参考资源（图/视频/音频槽位） | 整个 `references` 段 | 见下节 |
| 你自己造的节点 | 认不出字段名 | 看 JSON 手写路径 |

### B. REST

```bash
curl -X POST http://127.0.0.1:8188/roundabout/admin/workflows/upload \
  -F file=@my_workflow.json \
  -F name=my_workflow.json \
  -F create_model=1 \
  -F model_name=my-model \
  -F 'model_desc=我的工作流'
```

改完映射后热加载，不用重启：

```bash
curl -X POST http://127.0.0.1:8188/admin/reload
```

### C. Agent / 脚本

1. 把 API 格式 JSON 写进 `custom_nodes/ComfyUI-Roundabout/workflows/`
2. 在 `models.yaml` 追加条目
3. `POST /admin/reload`（MCP 工具 `reload`）
4. 用 MCP `generate_image` / `generate_video` 打一发验证；失败就用 `get_workflow` 看渲染后的实际图
5. 用 `list_models` 确认新模型已注册

---

## 视频工作流：`references` 段

参考图 / 参考视频 / 参考音频的槽位不是在 `bindings` 里，而是声明拓扑，让网关**动态删除未上传的槽位节点**（否则模板里引用的示例文件名不存在，会直接报错）：

```yaml
    references:
      aggregator: '136'          # 收参考资源的聚合器节点
      images: ['137', '139']     # LoadImage 节点 id，顺序对应 ref_image_0/1...
      videos: ['153']            # LoadVideo 节点 id，各自的 GetVideoComponents 会级联删除
      audios: ['150']            # LoadAudio 节点 id
```

启动时会校验：`aggregator` 必须存在，各 load 节点必须存在，且聚合器上必须真有 `ref_images.ref_image_0` 这样的 input。

**同一个工作流做「文生视频」和「带图生成」**：槽位本来就可选 —— 请求里不传 `reference_images`，网关就把对应 LoadImage 连节点带输入键一起删掉，模型自然只吃提示词。所以不必为两种模式各维护一份 JSON。`minimax-h3-self-lift` 就是这么用的（传 0 / 1 / 2 张图 = 纯文生 / 首帧 / 首尾帧）：

```yaml
    references:
      aggregator: '136'
      images: ['150', '164']   # 150=首帧槽位，164=尾帧槽位
```

在 ComfyUI 画布上手动跑时，把不需要的 LoadImage 按 **Bypass（Ctrl+B）** 旁路掉同样等效。

三类参考可以同时挂满，`minimax-h3-self-lift-edit`（Ref2VA 权重那支）就是全套槽位：

```yaml
    references:
      aggregator: '136'
      images: ['300', '301', '302', '303', '304', '305']
      videos: ['306', '307', '308']   # 每个 LoadVideo 的 GetVideoComponents 会跟着级联删除
      audios: ['312', '313', '314']
```

传几张就留几个槽：0 图 0 视频 0 音频 = 纯文生，只传 1 张图 = 单图参考，三类混传也照常。**参考视频与它的音轨同源**（同一个 `GetVideoComponents` 的 `video` / `audio` 两个输出），所以网关按成对的方式删 `ref_videos.ref_video_N` 与 `ref_video_audios.ref_video_audio_N`。

### 聚合节点不收 `ref_image_N` 时：`image_keys`

上面那套键名（`ref_images.ref_image_N`）是 Ref2VA 聚合节点的约定。有的聚合节点收的是别的名字 ——
典型是 `MiniMaxH3ImageToVideo`：它的首尾帧是**关键帧**槽 `first_frame` / `last_frame`，
和参考 token 不是一回事。这时用 `image_keys` 逐槽给出真实键名即可（`videos` / `audios` 同理有 `video_keys` / `audio_keys`）：

```yaml
    references:
      aggregator: '105:104'
      images: ['105:200', '105:201']
      image_keys: ['first_frame', 'last_frame']   # 第 1 张=首帧、第 2 张=尾帧
```

要点：

- 键名列表与节点列表**必须等长** —— 数量不一致启动时直接报错，不会让多出来的槽悄悄回落到默认键名。
- 不写 `*_keys` 时行为与以前完全一致（默认 `ref_images.ref_image_N`）。
- 节点 id 含冒号（子图扁平化导出的 `105:200`）照抄，绑定路径按 `.` 切分，冒号不影响解析。
- `fasth3` / `fasth3-edit` 就是这么接的：前者用 `image_keys` 接管首尾帧，后者聚合节点是
  `MiniMaxH3ReferenceToVideo`，仍走默认键名。两支的 SelfLift 放大版
  （`fastvideo-fasth3-self-lift` / `-edit`）接线完全一样 —— 只换了采样链路，`references` 段照抄即可。

**示例提示词的落点**：`prompt` 既可绑到独立的 `PrimitiveStringMultiline` 节点，
也可直接绑聚合节点自己的 `prompt` 输入（`105:104.inputs.prompt`）—— 后者少一个节点，
新工作流推荐这么做。

### SelfLift 的 σ 网格与 `extra_steps`

SelfLift 把一次采样拆成「低分前缀 + 高分收尾」，两阶段共用一条 σ 网格（`BasicScheduler(steps)` 生成），
再由 `H3SigmaRefiner`（`ComfyUI-YCNodes-MiniMax-H3`）在网格尾部补 `extra_steps` 个点：

- **最终 NFE = `steps + extra_steps`** —— `steps` 只是低分阶段的步数。
- 高分阶段至少留 2 步，只给 1 步会明显发灰发软。
- 于是 `transition_step` 的合法上界是 `steps + extra_steps - 1`，**不是 `steps - 1`**。
- `extra_steps` / `start_at_sigma` **不在可注入白名单里**（见上）——它们是工作流 JSON 里写死的标定值，
  写进 `bindings` 是死链、写进 `defaults` 也只供网关做范围校验。改步数请用
  `skill: selflift-progressive-upscale` 的方法重算这两个数。

| 工作流 | `extra_steps` | `start_at_sigma` | 默认 `steps` / `transition_step` |
|---|---|---|---|
| `minimax-h3-self-lift` / `-edit` | 2 | 0.9 | 8 / 8（走 `motion_presets`：story 8/8、fight 10/8） |
| `fastvideo-fasth3-self-lift` / `-edit` | 2 | 0.9 | 8 / 8 |

> σ 标定**不能跨族照抄**：fasth3 链上多了 `MiniMaxH3SigmaShift(shift_video=10)`，σ 网格被挤向高噪端；
> 旧蒸馏档（extra=1 / σ0.7，为 4 步 turbo LoRA 而设）也已于 2026-09-18 随 LoRA 一并淘汰，三族统一为 2 / 0.9。

### 一采分辨率：`lowres_scale` 与 `auto`

`lowres_scale` 决定一采（低分前缀）跑在多大的画布上：`low_latent = round(dim_latent * L / 2) * 2`。

**它是相对比例，同一个数值在不同目标尺寸下含义不同**：`0.70` 在 1920x1088 上得到
1344x768（正好 H3 原生画布），在 2560x1440 上却是 1792x992（超原生 33%）。而画质只跟低分的
**绝对**分辨率有关 —— 低分长边越过 1344 会掉宽谱细节、**并且**织出规则的斜向假网格
（两种失效各自独立，实测曲线见 `skill: selflift-progressive-upscale`）。

所以 `fastvideo-fasth3-self-lift*` 与 SelfLift 两支（`minimax-h3-self-lift*`，无 LoRA 标定）的默认档写成 `auto`，由网关按目标尺寸反推：

```
L = min(84 / max(W, H)_latent, 0.70)        # 84 = 1344 / 16
```

| 目标 | `auto` | 低分 latent / 像素 |
|---|---|---|
| 1920x1088（1080p） | 0.70 | 84x48 = 1344x768 |
| 2560x1440（2K） | 0.525 | 84x48 = 1344x768 |
| 3840x2160（4K） | 0.35 | 84x48 = 1344x768 |

- 反推必须等 `width` / `height` 定稿，所以在 `pipeline.build_video_values` 里、尺寸解析
  **之后**才算（实现见 `gateway/lowres.py`）；`build_workflow` 另留一道安全网，避免字符串
  `"auto"` 被塞进节点的 FLOAT 输入框。
- 上限 0.70 兼任「至少放大 1.43 倍」的下限：目标本身小于原生画布时也保持这个相对放大率，
  不退化成恒等放大。尺寸未知时退回 0.70。
- 旧蒸馏 LoRA 版曾默认模板字面值 `0.40`；换为无 LoRA 标定后（2026-09-18）已默认 `auto`。
- 显式给 `> 0.70` 不拦（属于调用方的选择），但网关会打一条告警说明代价。
- 一采分辨率是**相对**的，所以「L 的最优值」不能跨目标尺寸照搬，这是它被做成 auto 的原因。

---

## 大模型按显卡自动调参：`vram_adaptive`

像 MiniMax H3 SelfLift 这种权重几十 GiB 的工作流，靠节点级分块把激活张量切小才能在显存吃紧的卡上跑。分块参数的合适取值**只取决于显存大小**，写死在 JSON 里换台机器就得手改，所以做成档位自动填：

```yaml
defaults:
  vram_tiers:                 # 顶层 defaults，全局共用
    - min_gb: 48
      chunks: 1
      head_chunks: 4
      seq_threshold: 262144
      highres_tiling: false
    - min_gb: 24
      chunks: 2
      head_chunks: 8
      seq_threshold: 16384
      highres_tiling: true
    - min_gb: 8
      chunks: 6
      head_chunks: 24
      seq_threshold: 4096
      highres_tiling: true
    - min_gb: 0              # 兜底档，任何显存都命中
      chunks: 8
      head_chunks: 28
      seq_threshold: 4096
      highres_tiling: true
models:
  minimax-h3-self-lift:
    vram_adaptive: true
    bindings:
      chunks: 219.inputs.chunks
      head_chunks: 220.inputs.head_chunks
      seq_threshold: 219.inputs.seq_threshold
      highres_tiling: 235.inputs.highres_tiling
```

- **选档规则**：取 `min_gb` 不超过本机显存的最大一档；匹配留 0.6 GiB 容差（显卡报的可用量普遍略低于标称值，如 12G 卡报 12282 MiB = 11.99 GiB）。
- **优先级**：档位值 < 模型自己的 `defaults` < 请求参数。想固定某个值，就写进该模型的 `defaults`（如 `chunks: 3`），请求里仍可按次覆盖。
- **探测不到显存**（纯 CPU / 无 torch）时不覆盖，行为等同没开这个开关；`ROUNDABOUT_VRAM_GB=24` 可手动钉住。
- 档位表在 YAML 里，**改参数不用动代码**；本机命中哪一档见启动日志 `model ... 显存 X GiB -> 分块档位 {...}`。

---

## 按叙事节奏切参数档：`motion_presets`

有些参数**必须成对调整**，只改一个反而更糟。SelfLift 的「总步数 + 过渡步」就是典型：文戏 8 / 8、打戏 10 / 8（**默认文戏**）。与其每次记两个数字，不如打包成命名档，请求里写一个词即可：

```json
{"model": "minimax-h3-self-lift", "prompt": "...", "duration": 5, "motion": "story"}
```

```yaml
models:
  minimax-h3-self-lift:
    defaults:
      motion: story           # 默认档；不传 motion 时落回这里
    motion_presets:
      story:                  # 文戏：对话、静态、慢动作（默认）
        steps: 6
        transition_step: 5
      fight:                  # 打戏：奔跑、追逐、快节奏
        steps: 8
        transition_step: 6
    bindings:
      transition_step: 235.inputs.transition_step   # 档位值靠 bindings 才注得进节点
```

- **默认档只写档名**（`defaults.motion: story`），数值一律从档表取——避免「档表改了、defaults 里那串数字没改」的两处漂移。
- 档里的键就是普通参数名，**必须先在 `bindings` 里绑好**，否则档位值算得出来却写不到节点上。
- **优先级**：模型 `defaults` < 命名档 < 同请求里的显式参数。`{"motion": "story", "steps": 9}` 得到 9 / 5，方便在档位基础上微调。
- 未声明 `motion_presets` 的模型收到 `motion` 会**报错并列出可用值**，不会静默忽略——档名拼错能立刻发现。

---

## 排错表

| 症状 | 原因 | 处理 |
|---|---|---|
| 启动报 `looks like a UI workflow` | 导出成了 UI 格式 | 重新 `Export (API)` |
| 启动报 `binding path(s) not found in <file>` | 节点 id 或字段名写错 | 按报错里列的路径回 JSON 核对 |
| 启动报 `a prompt binding is required` | 没绑 `prompt` | 绑上，或加 `promptless: true` |
| 启动报 `output_node 'X' not in workflow` | `output_node` 指向了不存在的节点 | 填 SaveImage / SaveVideo 的 id |
| 启动报 `references.aggregator ... not found` | `references` 里的 id 写错 | 对照 JSON 修正 |
| 生成时报 `value not in list: ckpt_name ...` | 工作流引用的模型文件不在 `models/` 里 | 放进对应目录（checkpoint → `models/checkpoints/`，unet → `models/diffusion_models/`，CLIP → `models/text_encoders/`，VAE → `models/vae/`，LoRA → `models/loras/`） |
| 提示词传了但图没变 | `prompt` 绑到了没生效的节点 | 顺着采样器的 `positive` 连线找真正承载文本的节点 |
| 传了 `width/height` 但尺寸不变 | 绑到了被连线覆盖的 input | 改绑 `EmptyLatentImage` 这类字面量节点 |
| 传了 `image` 报 `does not support image-to-image` | 模型没声明 `image-to-image`，或没绑 `image` | 两个都补上 |
| 产物 URL 过一段时间 404 | `OUTPUT_TTL`（默认 3600 秒）到期 | 提高 `OUTPUT_TTL`，或改用 `response_format=path` |
