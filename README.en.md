# ComfyUI-Roundabout

**English** | [中文（原版）](README.md)

> Translation of `README.md` (node 1.46.0). Commands, paths, model names and numbers are kept verbatim; Chinese prompt examples inside `curl` payloads are left as-is so they keep working.

> A ComfyUI **custom node** that wraps your local workflows in an **OpenAI-compatible REST API** plus **MCP tools** (14 of them), so AI agents, scripts and any OpenAI client can drive image and video generation on your own machine.

**No canvas edits, no code changes.** You build a flow in ComfyUI, export it as JSON, write a parameter mapping in `models.yaml`, and it becomes a `model` any client can call.

This is more than "put ComfyUI behind MCP". The values that depend on hardware and target — **VRAM chunking, resolution tiers, attention tiers** — are deliberately kept out of the workflow JSON and injected by the gateway just before rendering. Change the GPU or the resolution and the workflow JSON does not change by a single character. **Only the depth of the configuration differs**: chunking tiers live in `models.yaml` (edit the YAML, hot-reload picks it up), while resolution and attention tier values are constants in `gateway/params.py` (changing those requires a restart).

---

## Design goals

**1. Make ComfyUI the AIGC backend for agents**

ComfyUI already has the models, the VRAM management and the queue; what it lacks is a layer an agent can understand. Roundabout exposes it as **OpenAI-compatible REST** (point your `base_url` here — any OpenAI SDK or client works unchanged) and as an **MCP service** (agents call tools directly). Local compute becomes a private, per-call, unmetered image/video backend.

**2. Embedded in ComfyUI, nothing extra to maintain**

It is a **custom node**, not a separate service you deploy:

- loads and exits with ComfyUI, **no separate process**
- the MCP endpoint hangs off ComfyUI's own port (`/mcp`) — **no extra port**
- reuses ComfyUI's Python interpreter and dependencies (zero new dependencies for the REST-only case)
- configuration is just two files, `models.yaml` (models) and `.env` (optional), both hot-reloaded. Model-specific parameters can be **declared** in a `params:` block inside `models.yaml` (type/min/max/default/bind, fully data-driven: declaring a parameter removes the need for the `KNOWN_PARAMS` allowlist, `bind`/`default` are merged automatically, and tool-info metadata comes from the declaration — v1.43.0). Machine-local models and local fine-tunes go in `models.local.yaml` (gitignored, merge semantics in `models.local.yaml.example`, never overwritten by upstream updates — v1.44.0). There is also an optional `device_weights.yaml` (per-machine weight overrides, gitignored — see [Multi-machine deployment](#multi-machine-deployment-weight-overrides))

No docker, no daemon, no second registry to keep in sync.

**3. The workflow never goes to the agent — semantic parameters save tokens**

The agent **never sees and never needs** the workflow JSON. It sends semantic parameters; the gateway writes node fields locally according to the `models.yaml` mapping.

| | Generic ComfyUI MCP (hands the whole workflow to the agent) | Roundabout |
|---|---|---|
| Per-call payload | the entire workflow JSON (or a permanently occupied context) | `model` + `prompt` + `size` and similar semantic parameters |
| Where parameter names come from | node schema / trial and error | a fixed parameter allowlist in the gateway (see WORKFLOWS.md) |
| Switching models | re-understand another graph | change a `model` string |
| Where the workflow lives | in the agent context | locally in `workflows/`, **never in context** |
| Failure surface | node ids, field names and links can all be corrupted | mappings are validated at startup; a missing path refuses to load |

Measured size of the bundled workflows (token estimate as chars ÷ 3):

| Workflow | Nodes | Chars | ≈tokens |
|---|---|---|---|
| `image_z_image_turbo` | 10 | 3 209 | ~1.1k |
| `image_flux2_klein_image_edit_turbo` | 19 | 4 244 | ~1.4k |
| `video_minimax_h3` | 33 | 7 751 | ~2.6k |

The matching Roundabout call:

```json
{"model":"z-image-turbo","prompt":"一只戴宇航头盔的柴犬","size":"1024x1024"}
```

Roughly 30 tokens — **40 to 90 times less payload per generation request**, and the gap grows with workflow complexity (video, multiple reference images).

**4. Hardware and quality experience lives in configuration, so a new machine does not mean new workflows**

The same workflow JSON needs chunking parameters that differ by several times between an 8 GiB laptop GPU and a 24 GiB desktop card. Hard-coded in the JSON, that means editing the graph on every machine. Roundabout lifts those values out of the workflow and injects them before rendering:

| Mechanism | Varies with | What the gateway does | Where the values live |
|---|---|---|---|
| `vram_adaptive` | GPU VRAM | probes VRAM at startup, picks the largest tier whose `min_gb` fits, overrides chunking parameters | `defaults.vram_tiers` in `models.yaml` |
| Resolution tiers | `size` in the request | resolves tier names such as `576p-16:9` into concrete width/height (six tiers × five aspect ratios) | `VIDEO_RES_PRESETS` in `gateway/params.py` (**code constant**) |
| Attention tiers | `attention` in the request | `sparse` / `dense` → for `minimax-h3` sets the Spark node's `enabled` (boolean); for the other three it sets `BlockSparseAttention.start_percent` | `ATTENTION_SPARSE_START` / `ATTENTION_SPARK_ENABLED` in `gateway/params.py` (**code constants**) |

The first row is YAML plus hot reload; the last two are code constants, so changing them means restarting ComfyUI. None of the three ever enters the workflow JSON — there is always exactly one workflow template. Details in [GPU-adaptive parameters](#gpu-adaptive-parameters-vram_adaptive).

---

## Documentation map

| Document | Content |
|---|---|
| **README.md** (this file) | what the plugin is, what it does, how to install and use it |
| **[WORKFLOWS.md](WORKFLOWS.md)** | connecting **your own** workflows: parameter mapping, `models.yaml` fields, examples, troubleshooting table |
| **[API.md](API.md)** | the complete interface contract: REST endpoints and fields, MCP tools, response shapes, auth, every environment variable, troubleshooting |

---

## Companion skills

The three documents in this repository are **written for humans**; the companion skills are the **agent-facing** rendition of the same knowledge — the calling conventions, registration flow and weight pitfalls compressed into a manual an agent can load directly. An agent with the skill installed does not need to read the docs first, and will not invent parameters from a stale impression.

| skill | Content | When to use it |
|---|---|---|
| [`roundabout-skill`](https://github.com/HJH-AILab/roundabout-skill) | the plugin's **main entry point**: pick a model, generate over REST / MCP, connect or retire a workflow, download weights, operate and troubleshoot, check "does the doc match the implementation" | when an agent has to drive the local ComfyUI, or edit `models.yaml` / add a workflow |
| [`h3-playbook-skill`](https://github.com/HJH-AILab/h3-playbook-skill) | the **single source** for MiniMax H3 (official manual wording merged with the official h3-prompt-writing contract): the three-part formula, the five mode decisions (T2VA / I2VA / FL2VA / L2VA / Ref2VA), the six-part Ref2VA rewrite format, material-purpose tags, duration and aspect-ratio limits, pitfall table | writing or editing H3 prompts, or judging whether H3 can do a given job; when chasing lip-sync, shot breaks or garbled output |
| [`qwen-image2.1-prompt-writing-skill`](https://github.com/HJH-AILab/qwen-image2.1-prompt-writing-skill) | a hand-written stand-in for the official Qwen-Image-2.1 Prompt Enhancer contract: t2i observer reports and edit rewrite instructions, producing `rewritten_prompt` plus the `wh_ratio` / `ratio_follow` structure | when generating or editing through the gateway's `qwen-image-2.1` tier, or before expanding a rough request into a description that model accepts |

Installing means dropping the directory into the agent's skills directory (directory name matching the skill's `name`):

```bash
git clone https://github.com/HJH-AILab/roundabout-skill.git <agent skills dir>/roundabout
git clone https://github.com/HJH-AILab/h3-playbook-skill.git <agent skills dir>/h3-playbook
git clone https://github.com/HJH-AILab/qwen-image2.1-prompt-writing-skill.git <agent skills dir>/qwen-image-prompt-writing
```

**One-line install** (hand the line to the agent; where things land is its convention):

```text
安装这个skill https://github.com/HJH-AILab/roundabout-skill
安装这个skill https://github.com/HJH-AILab/h3-playbook-skill
安装这个skill https://github.com/HJH-AILab/qwen-image2.1-prompt-writing-skill
```

> All three are maintained from this repository (marked `source="roundabout"` in `MCP get_skills`).
> The official H3 writing guide (formerly `h3-prompt-writing`) was **merged into h3-playbook**
> in playbook v2.0.0 (including its 2026-08-11 Tips update; `references/base-en.txt` /
> `ref-en.txt` are byte-for-byte copies of the official text) and is no longer recommended as a
> standalone skill — when the official repository changes, diff and sync it into the playbook.
> All three discover ComfyUI's path and port by probing first and asking only if that fails;
> they contain **no hard-coded LAN addresses** and work on another machine as-is.
> Their boundaries do not overlap: `roundabout` covers the gateway side only (how to call, how to
> register, how to debug); the other two cover the model side (one for H3, one for Qwen-Image-2.1).

---

## Features

**Two access layers, one shared engine**

- **OpenAI-compatible REST**: `/v1/images/generations` (text-to-image / image-to-image), `/v1/images/edits` (standard multipart editing), `/v1/images/remove-background` (background removal), `/v1/videos/generations` (video, async supported). Response format selectable as `b64_json` / `url` / `file` / `path`, so it drops in as a replacement for the official OpenAI URL.
- **MCP service (19 tools)**: generation — `generate_image` / `edit_image` / `remove_background` / `process_image` / `generate_speech` / `generate_video`; query — `get_tool_info` / `list_models` / `get_task` / `cancel_task` / `queue_status` / `get_workflow` / `health` / `check_weights`; operations — `reload` / `get_view_url` / `get_skills` / `manage_workflows`; board — `view_board` (single entry point dispatching on action: pin / remove / clear / get / history / load). One tool set covers the whole loop: list models → check weights → generate → track progress → pin to the board.

- **Shared port**: the MCP endpoint `/mcp` hangs off ComfyUI's own port (`http://<comfyui>:8188/mcp`) — no extra port, no extra process; REST and MCP share one registry, one generation path and one task table.

**Declarative model registration**

- Register a model in `models.yaml`: workflow file + parameter binding paths + defaults + aliases + capabilities (text-to-image / image-to-image / video).
- Adding or changing a model is a YAML edit plus `POST /admin/reload` to hot-reload — **no ComfyUI restart, no code**.
- 15 workflows ship ready to use (8 image + 6 video + 1 utility), and all of them double as references when you write your own mapping.

**Adapts to the machine**

- **VRAM adaptation** (`vram_adaptive`): VRAM is probed at startup and the chunking tier for the H3 family is chosen automatically, so 8 / 12 / 24 GiB cards share one workflow.
- Chunking parameters can be overridden per request, and the tier table can be changed without touching code.

**Visible, and manageable**

- **Visual page** `http://<comfyui>:8188/roundabout/view`: three tabs at the top — **Output / Input / Board** — for browsing `input/` and `output/` (thumbnails lazy-loaded in batches, adaptive paging, artefacts synced automatically, video tiles carry a play badge), with a floating task panel bottom right showing the ComfyUI queue plus the gateway task table in real time and a one-click overlay preview for artefacts.
- **Task board**: a tab in that page — an infinite canvas that **fills the viewport**, where an agent pins its output and arranges it semantically (shot order / A-B comparison / grouped by character) so the user sees everything at a glance. A pinned directory becomes a clickable **directory card** (jumping straight to it); artefacts outside `input/` and `output/` become **external cards** labelled "external" (clicking one asks first, then hands it to the system file manager — local access only). Clearing archives the board into history, where you can **read it back** at any time or delete a single entry — and while reviewing you can click "replace the current board with this one" (with confirmation; the current content is archived automatically). On the agent side, `get_view_board` reads what is currently on the board (including a summary of the last few archived rounds and content fingerprints `kinds`/`preview`/`models`), and `load_view_board` continues a specific round (pass the ID the user copied; without one you get the most recent round). Pinning several items at once goes through the `items` array — one round trip, one write, laid out strictly in array order. **Every history entry can "copy ID"** — the user pastes that string to the agent, which is equivalent to naming the round to continue (no need to scan the history list first); unnamed archives get an automatic content-based name from the backend (e.g. "7 items · image/text · 10:24").
- **Management panel inside ComfyUI**: the "Roundabout" menu offers workflow management (upload / validate / delete), model configuration (structured editing) and queue monitoring (including seed, so batch submissions can be told apart).
- **Task records**: synchronous and asynchronous generations both leave a row (status / duration / seed / artefact URL); final-state records are kept for 6 hours.

---

## Installation

**Option 1 — install from a Git URL with ComfyUI-Manager** (recommended). With [ComfyUI-Manager](https://github.com/ltdrdata/ComfyUI-Manager) installed, open **Custom Nodes Manager**, pick **Install via Git URL** from the top-right menu and enter:

```
https://github.com/HJH-AILab/ComfyUI-Roundabout
```

Then press **Restart**. The node package declares its metadata according to the Comfy Registry spec (`pyproject.toml`), so ComfyUI-Manager can read the name and version; being a git repository, you can also check for and pull updates from the Manager later.

> This repository is **not published to the Comfy Registry**, so it does not show up in the search box and there is no `comfy node install` command — use the Git URL route above.

**Option 2 — clone manually**:

1. Put it into ComfyUI's `custom_nodes/` directory:

   ```bash
   cd ComfyUI/custom_nodes
   git clone https://github.com/HJH-AILab/ComfyUI-Roundabout.git
   ```

2. Install the dependencies — **the MCP gateway is on by default, so this step is required by default**:

   ```bash
   <ComfyUI>/python/python.exe -m pip install -r custom_nodes/ComfyUI-Roundabout/requirements.txt
   # i.e. mcp>=2.0.0 (2.x required; the 1.x FastMCP API is incompatible) + uvicorn>=0.30.0
   ```

   > REST only, no MCP: set `MCP_ENABLED=false` in `.env` and you can skip both packages.

3. Configuration (optional): copy `.env.example` to `.env` and adjust as needed. **MCP is already enabled by default**, nothing to set; to turn it off, change `MCP_ENABLED` to `false`.

4. **Get the model weights**: the repository ships no weights. The 16 bundled workflows reference 26 weight files (the list adds 3 LoRAs that no bundled workflow references — 29 rows / ~177 GB in total), all from official HuggingFace repositories such as `Comfy-Org`; per-file download commands and target directories are in [Weight list](#weight-list-every-dependency-of-the-bundled-workflows) below. **The image tier alone is ~103 GB**, so you can start with just that family.

   > **The video tier additionally needs two third-party node packs**: KJNodes (`MiniMaxChunkFeedForward`, used by the whole video tier) and `comfyui-SelfLift` (the latent upscale node `SelfLiftH3LatentLift` used by both lift variants). See [Third-party node dependencies](#third-party-node-dependencies).

5. Restart ComfyUI. Success shows as `OpenAI gateway routes registered ... models=...` in the startup log.

---

## Usage

### 1. ComfyUI panel (a human working inside ComfyUI)

Menu bar **Roundabout** →

| Tab | Purpose |
|---|---|
| Workflow management | lists every JSON in `workflows/` with its validation state; **upload your own API-format workflow** (optionally generating the model entry automatically) |
| Model configuration | structured editing of `models.yaml` (bindings, defaults, aliases, capabilities); saving validates, and a broken config is never written |
| Queue monitoring | ComfyUI running / pending plus the gateway task table, each row with seed and duration |

### 2. REST API (scripts / OpenAI clients)

> The four most common minimal examples follow; the full field tables, response shapes and error codes are in **[API.md](API.md)**.

**Text to image**

```bash
curl -X POST http://127.0.0.1:8188/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"一只戴宇航头盔的柴犬，赛博朋克霓虹灯背景","size":"1024x1024","response_format":"path"}'
```

**Edit an image** (change background / material / write text into the picture)

```bash
curl -X POST http://127.0.0.1:8188/v1/images/edits \
  -F model=flux2-klein-image-edit-turbo \
  -F 'prompt=把背景换成星空，保留主体不变' \
  -F response_format=path \
  -F image=@input/source.png
```

**Remove background** (transparent PNG, no prompt)

```bash
curl -X POST http://127.0.0.1:8188/v1/images/remove-background \
  -F image=@input/source.png -F response_format=path
```

**Video** (for long jobs prefer the async route, then poll the task)

```bash
# submit
curl -X POST http://127.0.0.1:8188/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"minimax-h3","size":"576p-16:9","steps":8,"prompt":"a cat walking in rain","duration":5,"background":"pending"}'
# fetch the result (the id comes from the previous response)
curl http://127.0.0.1:8188/v1/videos/tasks/<id>
```

`size` accepts the tier preset `<tier>p-<ratio>`: tier ∈ `480p` / `576p` / `720p` / `768p` / `1080p` / `1440p`, ratio ∈ `1:1` / `3:4` / `4:3` / `16:9` / `9:16` (e.g. `768p-16:9` = 1360×768, `1080p-16:9` = 1920×1088, `1440p-16:9` = 2560×1440). The reversed form `<ratio>@<tier>p` (e.g. `9:16@768p`) and a plain `WxH` are accepted too. Omitting it or passing `auto` uses the model default.

⚠️ **`1440p` is a big-VRAM tier** (2560×1440 ≈ 3.6× the pixels of 768p): an 8 GB card **cannot run it directly**. For a larger frame, prefer `minimax-h3-lift` (768p canvas × 1.875 = 2528×1440).

### 3. MCP (for agents)

**MCP is enabled by default** — install the dependencies and restart ComfyUI and the endpoint is at `http://<comfyui>:8188/mcp`, with no configuration at all.

**Streamable HTTP** (recommended: same port as ComfyUI, no separate process):

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

**stdio** (the MCP client spawns the process; for clients without HTTP support):

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

> Host and port in the URL are whatever you use to reach ComfyUI; the path is always `/mcp`. For remote access switch to `http://192.168.1.10:8188/mcp` and change nothing else.
> Tool parameters, completion pushes (`notifications/message`, which require `MCP_STATELESS=false` explicitly) and assistant-side notes are in [API.md §7](API.md).

**Typical agent flow**: `list_models` to see what exists → `generate_image` / `generate_video` to produce → for async jobs poll with `get_task` (stateless by default; server-side pushes only with `MCP_STATELESS=false`) → hand the artefact URL to the user → `get_view_url` for the visual page.
> The agent-facing manual (calling conventions, registration and retirement, weight pitfalls, troubleshooting) is the [companion skills](#companion-skills) above — an agent with the skill installed does not need to read this repository's docs.

### 4. Visual page

Open `http://<comfyui>:8188/roundabout/view` in a browser (the MCP tool `get_view_url` returns the same address too):

- browse `input/` and `output/` (newest modification time first, adaptive paging, thumbnails lazy-loaded in batches)
- new artefacts trigger a refresh hint without interrupting the image you are looking at
- a floating "task progress" panel bottom right: ComfyUI queue + gateway tasks + one-click artefact preview

---

## Built-in models

The default is `default_model = z-image-turbo`. `model` accepts a registered name or any alias (case-insensitive); omitted means the default model.

| Type | Model | Purpose |
|---|---|---|
| text-to-image | `z-image-turbo` | fast, 8 steps (default) |
| text-to-image | `z-image` | 30 steps, high quality |
| text-to-image | `boogu-image-turbo` / `boogu-image-base-4step` | 4-step quick preview |
| text-to-image | `boogu-image-base` | 30 steps, high quality |
| text-to-image / multi-image edit | `qwen-image-2.1` | 25 steps, **text-to-image and multi-reference editing in one branch**: with no reference image it is pure text-to-image (native 2K tier, default 1024x1024, `size` passed through); with 1–6 reference images (`reference_images`) it is multi-image editing, output size following the first reference |
| image edit | `flux2-klein-image-edit-turbo` | first choice for semantic rewrites: new background / new material / add or remove objects (the `edit_image` default); **multi-reference** up to 4 images |
| image edit | `boogu-image-edit` / `boogu-image-edit-turbo` | good at rewriting or adding **text inside the image**, 30 / 6 steps |
| image utility | `utility-birefnet-remove-background` | BiRefNet matting, transparent PNG (no prompt) |
| video | `minimax-h3` / `minimax-h3-edit` | MiniMax H3 (base / edit, both 20 steps): base takes **first and last frame** (`first_frame`/`last_frame`, cover-cropped), edit takes **references** (6 images + 3 videos + 3 audios); low-VRAM chunking adapts via `vram_adaptive` |
| video | `minimax-h3-lift` | **base skeleton + deterministic upscale**: 20 native sampling steps → learned latent lift (1344x768 × scale 1.875 = 2528x1440), zero composition re-roll, strongest texture; supports first/last frame (`first_frame`/`last_frame` with 0 / 1 / 2 images = text-to-video / first frame / first and last frame, cover-cropped); output lands in `video/H3_Lift`; `scale` is a request parameter (default 1.875 → 2528x1440); fine-tuning `rho` goes through `workflow_overrides` (`910.inputs.rho`) |
| video | `minimax-h3-lift-edit` | as above but on the **edit skeleton**: Ref2VA weights plus the full reference set of 6 images / 3 videos / 3 audios. ⚠️ **not calibrated**: 30 steps (aligned with edit), and the combination of upscale and references has not been A/B tested |
| **audio (TTS)** | `qwen3-tts-voice-design` | **text to speech + voice design** (Qwen3-TTS 1.7B VoiceDesign): `POST /v1/audio/speech`, `input` = the text to read, `instructions` = the voice description (age/gender/pace/character…); the mp3 lands in `audio/`. Depends on the custom node **TTS-Audio-Suite** (the engine runs on Shared Runtime by default, and the runtime is prepared on the first call) |

- Editing models **require an input image**: single-image models take `image`; **multi-image models** (`flux2-klein-image-edit-turbo` 4 images / `qwen-image-2.1` 6 images) take `reference_images` filled in order, and a single image can also be passed as `image` (equivalent to slot 1). Output size follows the input image (boogu / klein scale to 1 MP; qwen does not resample by default and follows the first reference), in which case `size` has no effect — `qwen-image-2.1` is **text-to-image and editing in one branch**, so passing no reference at all means pure text-to-image, and only that branch uses `size`.
- Passing `image` to a text-to-image model is rejected, and the error lists every model that does accept an input image.
- Aliases and binding paths are in `models.yaml` and [API.md §5.5](API.md).

---

## Weight list (every dependency of the bundled workflows)

**This repository contains no weight files** (size and licensing). The 16 bundled workflows reference **26** weight files, roughly **170 GB** in total (about 103 GB image tier / 67 GB video tier); the two tables below have **29 rows**, the extra 3 being LoRAs **no bundled workflow references** (2 Acc LoRAs + 1 hyperflow LoRA), listed for reference only — counted, that is ~177 GB. A missing file reports as `value not in list: <field>: <filename>` — the gateway **rewrites that error into an actionable download instruction** (which directory the file goes into, what the `curl` command is), so an agent can simply follow it. You can also check what is missing up front: `GET /roundabout/admin/weights`, or the MCP tool `check_weights` (read-only, no GPU). The per-file sources are the tables below; the same data lives in `weights.yaml`, which the gateway and the check read. (`workflows/example_txt2img.json` is an integration sample that uses your own checkpoint and is not counted above.)

These files live almost entirely in the **official HuggingFace `Comfy-Org` repositories** (a few come from the model vendor or the community, as marked in the tables). In mainland China, point the endpoint at a mirror — repo IDs and in-repo paths stay identical:

```bash
set HF_ENDPOINT=https://hf-mirror.com          # Windows
export HF_ENDPOINT=https://hf-mirror.com       # Linux / macOS
```

> **⚠ Exactly one file must be renamed after download** (the most common snag):
>
> | Upstream filename | Name the bundled workflows use |
> |---|---|
> | `minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors` | `minimax_h3_latent_upscaler_3d_fp16.safetensors` (`_conv_v1` dropped) |
>
> For every other file the download name equals the name in use, so just drop it into the target directory.

### Directory cheat sheet

All weights go into the matching subdirectory of `<ComfyUI>/models/` (the `models/` directly under the ComfyUI root):

| Target directory | Contents |
|---|---|
| `models/diffusion_models/` | main models (UNET) |
| `models/text_encoders/` | text encoders |
| `models/vae/` | VAE (the video tier also has an audio VAE) |
| `models/loras/` | LoRAs and distilled acceleration weights |
| `models/checkpoints/` | full checkpoints such as SAM3 |
| `models/latent_upscale_models/` | SelfLift latent upscale weights |
| `models/background_removal/` | BiRefNet matting |

### Image tier (18 files, ~103 GB)

| File | Target directory | Size | Source (HF repo) | In-repo path |
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
| `flux-2-klein-9b-kv-fp8.safetensors` | `diffusion_models/` | 9.82 GB | `black-forest-labs/FLUX.2-klein-9b-kv-fp8` | repository root |
| `flux2-vae.safetensors` | `vae/` | 0.34 GB | `Comfy-Org/vae-text-encorder-for-flux-klein-9b` | `split_files/vae/` |
| `qwen_image_2.1_int8_convrot.safetensors` | `diffusion_models/` | 7.26 GB | `Comfy-Org/Qwen-Image-2.1` | `diffusion_models/` |
| `qwen3vl_8b_int8_convrot.safetensors` | `text_encoders/` | 9.35 GB | `Comfy-Org/Qwen-Image-2.1` | `text_encoders/` |
| `qwen_image_2.1_vae_bf16.safetensors` | `vae/` | 0.68 GB | `Comfy-Org/Qwen-Image-2.1` | `vae/` |
| `birefnet.safetensors` | `background_removal/` | 0.44 GB | `Comfy-Org/BiRefNet` | `background_removal/` |
| `seedvr2_7b_int8_convrot.safetensors` | `diffusion_models/` | 7.76 GB | `Comfy-Org/SeedVR2` | `diffusion_models/` |
| `seedvr2_ema_vae_fp16.safetensors` | `vae/` | 0.47 GB | `Comfy-Org/SeedVR2` | `vae/` |
| `sam3.1_multiplex_fp16.safetensors` | `checkpoints/` | 1.63 GB | `Comfy-Org/sam3.1` | `checkpoints/` |

**Shared files and easy confusions:**

- `flux1_vae_bf16.safetensors` is the VAE the whole Z-Image / Boogu family needs — **you do not have to hunt for `ae.safetensors`**: the two are the fp32 / bf16 copies of the same FLUX.1 autoencoder (244/244 tensors **bit-identical** in bf16), and ComfyUI loads VAE as bf16 by default (`working_dtypes = [bf16, fp32]`) — reading `ae` just down-casts it to bf16, and the two decode **pixel for pixel identically** (measured on the same latent: `max|Δ| = 0`, `0.0000%` differing pixels at 8-bit). The bf16 copy is also less than half the size.
- `qwen3vl_8b_fp8_scaled.safetensors` is shared by the whole Boogu family and `flux2-klein-image-edit-turbo`: one file serves three models.
- `flux-2-klein-9b-kv-fp8.safetensors` comes from the official Black Forest Labs repository (not Comfy-Org); Comfy-Org only hosts its VAE and text-encoder repository (`vae-text-encorder-for-flux-klein-9b`, the official spelling).
- Qwen-Image 2.1's text encoder is `qwen3vl_8b_int8_convrot.safetensors`, which shares a name prefix with Boogu's `qwen3vl_8b_fp8_scaled.safetensors` but is **a different file from a different repository** — do not substitute one for the other; download whichever branch you run. The same repository also carries a whole "PE" encoder line (`qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors` / `..._pe_i2i...`); the bundled workflows **do not use** it.
- To save VRAM you can switch to a smaller quantisation (`nvfp4` / `pruned_int8_convrot` and friends live in the same repository and directory), but you **must also change the filename inside the workflow JSON**.

### Video tier (12 rows: 9 referenced by bundled workflows + 3 for reference, ~114 GB)

| File | Target directory | Size | Source (HF repo) | In-repo path |
|---|---|---|---|---|
| `minimax_h3_fl2va_pruned_w6a8.safetensors` | `diffusion_models/` | 15.98 GB | `Comfy-Org/MiniMax-H3` | `diffusion_models/` |
| `minimax_h3_ref2va_pruned_w6a8.safetensors` | `diffusion_models/` | 15.98 GB | `Comfy-Org/MiniMax-H3` | `diffusion_models/` |
| `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | `text_encoders/` | 27.14 GB | `Comfy-Org/MiniMax-H3` | `text_encoders/` |
| `minimax_h3_video_vae_int8_convrot.safetensors` | `vae/` | 2.81 GB | `Comfy-Org/MiniMax-H3` | `vae/` |
| `minimax_h3_audio_vae_fp32.safetensors` | `vae/` | 0.61 GB | `Comfy-Org/MiniMax-H3` | `vae/` |
| `MiniMax-H3-FL2VA-Acc-8Step.safetensors` | `loras/` | 1.37 GB | `alibaba-pai/MiniMax-H3-Acc-LoRAs` | repository root |
| `MiniMax-H3-Ref2VA-Acc-8Step.safetensors` | `loras/` | 1.37 GB | `alibaba-pai/MiniMax-H3-Acc-LoRAs` | repository root |
| `minimax_h3_hyperflow_8step_v1.0_comfyui_bf16.safetensors` | `loras/` | 3.93 GB | `drbaph/MiniMax-H3-Turbo-Lora-ComfyUI` | repository root (the file is still on disk, but no bundled workflow references it) |
| `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | `loras/` | 1.82 GB | `Comfy-Org/MiniMax-H3` | `loras/` |
| `minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors` | `loras/` | 1.82 GB | `lightx2v/Minimax-h3-Turbo` | repository root |
| `minimax_h3_latent_upscaler_3d_fp16.safetensors` | `latent_upscale_models/` | 0.69 GB | `LBH-123-AI/Minimax_h3_latent_Upscaler` | `minimax_h3_latent_upscaler_3d_conv_v1/` (**needs renaming**) |

**Three pitfalls in the video tier:**

- **`minimax_h3_hyperflow_8step_v1.0_comfyui_bf16.safetensors` is a community conversion** (the original lives in `videorebirth/hyperflow` as `minimax_h3_hyperflow_8step_v1.0.safetensors`, no `_comfyui_bf16` suffix). While remapping keys the conversion folded the endpoint adapter `endpoint_time_embedder.*` into `time_embedder.proj_in/proj_out`, and on this machine the **endpoint conditioning does not reach the model** (frames bloom in a non-single-shot way). For the full effect you need the upstream original plus a dedicated node pack, `Addis-Pulse-Studio/ComfyUI-HyperFlow`. The bundled workflows currently load the conversion.
- **The Acc LoRAs use diffusers naming and a plain loader cannot apply them** (see the `minimax-h3-turbo` row in [Built-in models](#built-in-models)). For genuine acceleration use the **ComfyUI-named** files under `loras/` in `Comfy-Org/MiniMax-H3`, e.g. `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`.

### One-shot download

Run this in the **ComfyUI root** (`models/` sits right under the current directory). It is grouped by family, so download only the families you need. In-repo paths match HuggingFace; besides `hf-mirror`, **ModelScope** works as a fallback source (same repository — replace `$B/<repo>/resolve/main/<path>` with `https://modelscope.cn/models/<repo>/resolve/master/<path>`).

```bash
B=https://hf-mirror.com          # switch to https://huggingface.co for the official endpoint


# ---------- Z-Image (z-image / z-image-turbo, ~20 GB) ----------
curl -L -o models/diffusion_models/z_image_int8_convrot.safetensors \
  $B/Comfy-Org/z_image/resolve/main/split_files/diffusion_models/z_image_int8_convrot.safetensors
curl -L -o models/diffusion_models/z_image_turbo_int8_convrot.safetensors \
  $B/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_int8_convrot.safetensors
curl -L -o models/text_encoders/qwen_3_4b.safetensors \
  $B/Comfy-Org/z_image/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors

# ---------- Boogu (text-to-image + image-to-image, ~45 GB) ----------
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

# ---------- FLUX.2 Klein image editing + BiRefNet matting (~11 GB) ----------
curl -L -o models/diffusion_models/flux-2-klein-9b-kv-fp8.safetensors \
  $B/black-forest-labs/FLUX.2-klein-9b-kv-fp8/resolve/main/flux-2-klein-9b-kv-fp8.safetensors
curl -L -o models/vae/flux2-vae.safetensors \
  $B/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/vae/flux2-vae.safetensors
curl -L -o models/background_removal/birefnet.safetensors \
  $B/Comfy-Org/BiRefNet/resolve/main/background_removal/birefnet.safetensors

# ---------- Qwen-Image 2.1 (text-to-image + multi-image editing, ~17 GB) ----------
curl -L -o models/diffusion_models/qwen_image_2.1_int8_convrot.safetensors \
  $B/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors
curl -L -o models/text_encoders/qwen3vl_8b_int8_convrot.safetensors \
  $B/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors
curl -L -o models/vae/qwen_image_2.1_vae_bf16.safetensors \
  $B/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors

# ---------- MiniMax H3 shared backbone (needed by the whole H3 family, ~81 GB) ----------
curl -L -o models/diffusion_models/minimax_h3_fl2va_pruned_w6a8.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_w6a8.safetensors
curl -L -o models/diffusion_models/minimax_h3_ref2va_pruned_w6a8.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_w6a8.safetensors
curl -L -o models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors
curl -L -o models/vae/minimax_h3_video_vae_int8_convrot.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors
curl -L -o models/vae/minimax_h3_audio_vae_fp32.safetensors \
  $B/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors

# ---------- video tier only (pick what you need; all of it is ~30 GB) ----------
# Acc LoRAs for the two turbo variants
curl -L -o models/loras/MiniMax-H3-FL2VA-Acc-8Step.safetensors \
  $B/alibaba-pai/MiniMax-H3-Acc-LoRAs/resolve/main/MiniMax-H3-FL2VA-Acc-8Step.safetensors
curl -L -o models/loras/MiniMax-H3-Ref2VA-Acc-8Step.safetensors \
  $B/alibaba-pai/MiniMax-H3-Acc-LoRAs/resolve/main/MiniMax-H3-Ref2VA-Acc-8Step.safetensors
# SelfLift upscale weights (rename after download)
curl -L -o models/latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16.safetensors \
  $B/LBH-123-AI/Minimax_h3_latent_Upscaler/resolve/main/minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors
```

> If `curl` is a nuisance you can use the HuggingFace CLI (`pip install -U huggingface_hub`):
> ```bash
> hf download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_pruned_w6a8.safetensors \
>   --local-dir models
> ```
> Note that it **preserves the in-repo directory structure** (Comfy-Org's `split_files/` prefix is not a ComfyUI directory), so you still have to move the file to the target location from the tables above — which is why `curl` is the default here.

### Workflow → weights

| Workflow | Required weights → target directory |
|---|---|
| `z-image` / `z-image-turbo` | `diffusion_models/` `z_image_int8_convrot.safetensors`, `z_image_turbo_int8_convrot.safetensors` · `text_encoders/` `qwen_3_4b.safetensors` · `vae/` `flux1_vae_bf16.safetensors` |
| `boogu-image-base` / `-base-4step` / `-turbo` | `diffusion_models/` `boogu_image_base_fp8_scaled.safetensors`, `boogu_image_turbo_hotfix_int8_convrot.safetensors` · `loras/` `boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux1_vae_bf16.safetensors` |
| `boogu-image-edit` / `-edit-turbo` | `diffusion_models/` `boogu_image_edit_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux1_vae_bf16.safetensors` · (turbo also needs) `loras/` `boogu_image_turbo_hotfix_lora_rank_128_bf16.safetensors` |
| `flux2-klein-image-edit-turbo` | `diffusion_models/` `flux-2-klein-9b-kv-fp8.safetensors` · `text_encoders/` `qwen3vl_8b_fp8_scaled.safetensors` · `vae/` `flux2-vae.safetensors` |
| `qwen-image-2.1` | `diffusion_models/` `qwen_image_2.1_int8_convrot.safetensors` · `text_encoders/` `qwen3vl_8b_int8_convrot.safetensors` · `vae/` `qwen_image_2.1_vae_bf16.safetensors` |
| `utility-birefnet-remove-background` | `background_removal/` `birefnet.safetensors` |
| `minimax-h3` / `minimax-h3-edit` | `diffusion_models/` `minimax_h3_fl2va_pruned_w6a8.safetensors`, `minimax_h3_ref2va_pruned_w6a8.safetensors` · `text_encoders/` `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` · `vae/` `minimax_h3_video_vae_int8_convrot.safetensors`, `minimax_h3_audio_vae_fp32.safetensors` |
| `minimax-h3-lift` / `-lift-edit` | same as `minimax-h3` / `minimax-h3-edit`, plus `latent_upscale_models/` `minimax_h3_latent_upscaler_3d_fp16.safetensors` (does **not** need `loras/`) |

### Third-party node dependencies

> Apart from the lift family (`minimax-h3-lift*`) and the two base entries `minimax-h3` / `-edit` (low-VRAM chunking nodes), every node these workflows use comes from **ComfyUI core** (`comfy_extras/`); no third-party custom node pack is required. An older ComfyUI will be missing `MiniMaxH3UnifiedToVideo` (bundled with Roundabout and loaded with the package) / `LoadBackgroundRemovalModel` / `Flux2Scheduler` / `TextEncodeQwenImage21` and similar nodes.
> The lift family additionally needs one third-party pack: `comfyui-SelfLift` (`SelfLiftH3LatentLift` plus the upscale weights under `latent_upscale_models/`).
> KJNodes' `MiniMaxChunkFeedForward` is needed for FFN chunking in **the entire video tier**. Low VRAM is handled in two stages — attention tier → `MiniMaxChunkFeedForward`: the attention tier of `minimax-h3` is **`Spark-H3` (a locally built SM86 wheel that depends on `custom_nodes/ComfyUI-Spark-H3` plus a Spark-patched comfy-kitchen)**, while the other three (`-edit` / `-lift` / `-lift-edit`) still use `BlockSparseAttention`'s `sol-attn` (training-free, keeping roughly 16 % of the key blocks).
> ⛔ **Do not wire KJNodes' `MiniMaxLowVRAMAttention` into the video tier**: it replaces `block.forward`, while `BlockSparseAttention`'s block patch unconditionally passes an `attention=` keyword (`comfy/ldm/minimax/model.py`); the signatures do not match, and the measured result is a `TypeError`. The two are **mutually exclusive**, which is why the original LowVRAM node of the four base entries was removed on 2026-09-21 (the `head_chunks` tier values lost their consumer and remain in the table only for easy restoration).
> The sparse nodes' parameters (`tau` / `min_tokens` / `dense_blocks` …) are not on the injectable allowlist; `tau` in particular is keyed `selection.tau` (with a dot), which a path parser that splits on `.` cannot address, so tuning it means editing the workflow JSON and running `/admin/reload`. **The "faster ↔ higher quality" trade-off does have a first-class request parameter**, though: `attention` (`sparse` by default / `dense` disables sparsity for dense quality). Where it lands depends on the model: for `minimax-h3` it sets the Spark node's `enabled` (`dense` = the node passes the model through untouched), for the other three it sets `BlockSparseAttention.start_percent` (`1.0` is equivalent to dense throughout, because `percent_to_sigma(1.0) = 0`). **This tier exists only for the four base entries** (`minimax-h3` / `-edit` / `-lift` / `-lift-edit`); passing `attention` to any other model returns 400.
> Every video-tier workflow uses `comfy kitchen attention` for `ModelAttentionBackend` (comfy_kitchen's INT8 implementation, saving VRAM). Switching to `pytorch attention` means changing that node's value in the workflow template; it is not on the `bindings` allowlist, but you **can** change it at request time with `workflow_overrides` (`{"156.inputs.attention": "pytorch attention"}`) — `workflow_overrides` injects at arbitrary paths and is independent of the allowlist.
> ⚠️ **Minimum comfy_kitchen version: `>= 0.2.37`**. `0.2.36` carries a stack-overflow regression in the w4a8 dequantisation kernel — with quantised weights plus a LoRA patch (`turbo_lora`), sampling step 2 kills the process (measured 2026-10-08, 2/2 reproducible; fixed in `0.2.37`). `0.2.35` and earlier do not support the `K=8064` layer in ref2va weights (sampling fails with `ValueError: K=8064 must be divisible by 16…`). `0.2.36` is the only version that fails both ways, so pin `0.2.37` or newer.
> Most of the weights above are `int8_convrot` quantisations and work out of the box only on a machine that already has weights of the same name; when switching to your own models, change the filename in the workflow JSON accordingly.

---

## Connecting your own workflows

The bundled workflows are samples and can be deleted at any time. Connecting your own flow means **exporting an API-format JSON** plus **writing a parameter mapping in `models.yaml`** — hot-reloaded, no code changes.

Three steps:

1. ComfyUI canvas → `Workflow` → **`Export (API)`** (API format is required, not the regular Export) → save it into `workflows/`
2. Add an entry in `models.yaml`: `workflow` / `mode` / `capabilities` / `output_node` / `bindings` (request parameter → node path). **If it is only for this machine and should stay out of the repository**, write it into `models.local.yaml` (gitignored, [(syntax)](models.local.yaml.example) identical to models.yaml)
3. `POST /admin/reload` or the "reload" button in the panel, then send one request to verify

**The full guide is [WORKFLOWS.md](WORKFLOWS.md)** — parameter allowlist, common landing-spot table, how to find paths, the three hard rules (especially: **do not bind an input that a link overrides**), all `models.yaml` fields, the `references` section, and a troubleshooting table.

The smallest runnable sample: `workflows/example_txt2img.json` (CheckpointLoaderSimple + KSampler + SaveImage — just replace `ckpt_name` with your own).
Step-by-step instructions for the three entry points (panel / REST / agent) are included too.

---

## Configuration

The full environment variable list is in **[API.md §2.3](API.md)**; the common ones:

### Multi-machine deployment: weight overrides

When one repository runs on different hardware, quantised weights (w6a8 and friends) carry **a per-step dequantisation tax on a large-VRAM machine that does not need offloading — making them slower than the full-precision version**, while a low-VRAM machine has to use the quantised one. The solution is to move "which weight file" out of the repository and into the machine:

1. copy `device_weights.example.yaml` to `device_weights.yaml` (gitignored — never committed, never synced by a push);
2. declare the overrides for this machine's hardware (e.g. on a 4090, point `unet_name` of node 127 in `video_minimax_h3.json` back at the full bf16);
3. restart or call `/roundabout/admin/reload`; the startup log echoes `device override: ...` line by line.

The same idea covers the model registry: machine-local models and local fine-tunes go into `models.local.yaml` (gitignored, see [models.local.yaml.example](models.local.yaml.example)), and upstream updates to `models.yaml` never overwrite your entries.

Values ending in `.safetensors` are checked for existence in ComfyUI's model directories and log a startup warning when missing (ComfyUI itself still hard-fails at submit time as a backstop). Not creating this file means zero overrides and behaviour identical to a single-machine deployment.



| Environment variable | Default | Description |
|---|---|---|
| `MCP_ENABLED` | `true` | embedded MCP gateway (on by default; set `false` to turn it off, which also lets you drop the `mcp`/`uvicorn` dependencies) |
| `MCP_SHARE_PORT` | `true` | MCP hangs off ComfyUI's own port (`/mcp`); `false` uses a dedicated port from `MCP_PORT` |
| `MCP_PORT` | `0` | internal loopback backend port; empty/`0` = let the system pick a free port (the actual port appears in the startup log) |
| `MCP_PORT_MAP` | empty | fixed port mapping, written as pairs "ComfyUI port → Roundabout port": `[8188,888],[8189,999]`. With several instances on one machine each takes its own port from its `--port`; a miss falls back to a system-assigned port; if `MCP_PORT` is set, that wins |
| `MCP_STATELESS` | `true` | stateless MCP (default): requests are handled independently, no session tracking, so **ComfyUI can restart freely and the agent never reconnects**. Setting `false` returns to stateful mode, i.e. the push channel needed for "async task completed" notifications — at the cost of every old session becoming invalid after a restart, so all calls fail until the client handshakes again |
| `DEFAULT_MODEL` | value in `models.yaml` | default model (takes precedence over the YAML) |
| `OPENAI_GATEWAY_API_KEYS` | empty | authentication is only enabled when set (comma-separated for multiple keys) |
| `PUBLIC_BASE_URL` | empty | base used to absolutise artefact `url`s — **required for remote deployments** |
| `MAX_CONCURRENCY` | `2` | concurrent generation limit |
| `JOB_TIMEOUT` / `OUTPUT_TTL` | `300` / `3600` | job timeout (seconds; `0` = no limit, decided by ComfyUI state alone) / lifetime of `url` artefacts (seconds) |
| `ROUNDABOUT_VRAM_GB` | empty | pin the VRAM tier manually (GiB) for models using GPU-adaptive parameters (e.g. `minimax-h3-lift`); unset means auto-detect, and if detection fails the workflow's own values are left untouched |

### GPU-adaptive parameters (`vram_adaptive`)

Large-model workflows (the H3 family totals ~65 GiB of weights) run on VRAM-constrained cards thanks to node-level chunking, and the right chunking values depend only on VRAM size. Add `vram_adaptive: true` to a model in `models.yaml` and the gateway probes local VRAM at startup, picks the largest tier from `defaults.vram_tiers` whose `min_gb` fits, and uses it as that model's chunking defaults:

```yaml
defaults:
  vram_tiers:
    - min_gb: 24        # 24 ~ 31 GiB (32 GiB and above has its own tier)
      chunks: 2
      seq_threshold: 16384
      head_chunks: 8         # ← no node bound at present (see the third-party dependency section)
      highres_tiling: true   # ← no node bound at present
    # ... 12 / 8 / 0 tiers
models:
  minimax-h3-lift:
    vram_adaptive: true
    bindings:
      chunks: 158.inputs.chunks
      seq_threshold: 158.inputs.seq_threshold
```

- Precedence: **tier value < the model's own `defaults` < request parameters** (passing `chunks` and friends in a request overrides for that single job).
- If VRAM cannot be detected (CPU-only / no torch) nothing is overridden, exactly as if `vram_adaptive` were not declared.
- The tier table lives in YAML, so changing tiers needs no code; `ROUNDABOUT_VRAM_GB` pins it manually.

### Several ComfyUI instances on one machine

Roundabout starts with the ComfyUI process and every instance gets its own MCP backend. To make the ports predictable (firewall rules, readable logs), map "ComfyUI port → Roundabout port" one to one:

```env
MCP_PORT_MAP=[8188,888],[8189,999]
```

Now the `--port 8188` instance uses `888` and the `--port 8189` instance uses `999`, with no interference. Running without a mapping does not conflict either (ports are assigned automatically); a mapping is simply more controllable. If a mapped port happens to be taken, Roundabout falls back to a free port and logs a warning — the endpoint keeps working.

---

## Directory layout

```
ComfyUI-Roundabout/
├── __init__.py            # ComfyUI node entry point (starts the gateway, loads models)
├── mcp_server.py          # MCP service (18 tools) + shared-port embedded startup
├── gateway/               # REST gateway
│   ├── config.py          # configuration (.env / environment variables)
│   ├── registry.py        # models.yaml parsing, binding validation, hot reload
│   ├── pipeline.py        # generation path (parameter validation → workflow rendering → submit → collect artefacts)
│   ├── handlers.py        # REST endpoints
│   ├── routes.py          # route table
│   ├── admin.py           # workflow / model configuration endpoints
│   ├── viewer.py          # backend for the asset browser and task progress page
│   ├── analyze.py         # automatic parameter-mapping analysis when uploading a workflow
│   ├── tasks.py           # task table
│   ├── vram.py            # VRAM probing + low-VRAM chunking tier selection (vram_adaptive)
│   ├── log_filters.py     # downgrades aiohttp "client disconnected" ERRORs to DEBUG
│   └── ...
├── web/                   # frontend (visual board page)
├── workflows/             # API-format workflows (example_txt2img.json is the integration sample)
├── models.yaml            # model registry
├── models.local.yaml.example  # template for machine-local model entries (copy to models.local.yaml; gitignored, never overwritten by upstream)
├── weights.yaml           # weights → download source index (data behind the download hints in errors and the weights check)
├── pyproject.toml         # node package metadata (read by ComfyUI-Manager and friends)
├── requirements.txt       # mcp + uvicorn (needed by default because MCP is on; skippable if MCP is off)
├── tests/                 # smoke connectivity tests (run_tests.py is the entry point; excluded from distributions via .comfyignore)
├── WORKFLOWS.md           # connecting your own workflows
└── API.md                 # complete interface documentation
```

---

## Tests

The repository keeps only **smoke connectivity** cases: the service starts, the MCP tools are all
registered, the endpoints exist, parameter overrides land — plus two lightweight guards for pitfalls
that were actually hit (`test_weights_index` locks the weight-table
consistency). Everything runs offline on temporary ports; no running ComfyUI is required.

```bash
<ComfyUI>/python/python.exe tests/run_tests.py                # all cases
<ComfyUI>/python/python.exe tests/run_tests.py --list         # only list the files that would run
<ComfyUI>/python/python.exe tests/run_tests.py test_port_map.py   # run a single file
```

## License

[MIT](LICENSE)
