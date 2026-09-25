"""Roundabout 自带的 ComfyUI 节点（工作流模板内部使用，不进 MCP / tool-info）。

节点一览：

MiniMaxH3UnifiedToVideo —— H3 统一聚合节点（t2va / fl2va / ref2va / 帧参考组合）。
  把 comfy_extras 的 MiniMaxH3ImageToVideo（首尾帧）与 MiniMaxH3ReferenceToVideo
  （参考图/视频/音频）合并进一个条件节点：帧走 minimax_keyframes（成为输出的
  第 0 / 最后一帧），参考走 minimimax_refs（仅做条件，输出尺寸仍由画布决定），
  两者可同时存在 —— 首尾帧 + 参考是常见生成形态，原版两节点各自只支持一半。
  组合在模型侧是官方路径：model_base 的 H3 payload 中 keyframes 与 refs 是两个
  独立键（cond_video_latents 直接拼接，PackedLayout 同时收两者），AddGuide 的
  设计用途就是往 ref2va 条件上锚帧。
  呈现层统一走 tokenizer 的 minimax_ref_items 分支（<Picture i> / <Video k> /
  <Audio j> 标签）——与原版 fl2va 的 images= 分支逐 token 等价（同为
  "<Picture N>: " + vision，计数同为 1 起）；帧图排在参考之前，prompt 用
  <Picture 1>/<Picture 2> 指首/尾帧、后续编号指参考图。

RoundaboutCoverResize —— 参考图 cover 式缩放裁剪。
  先按「铺满目标画布」等比放大（lanczos），再居中裁到目标宽高，输出恰好
  width×height。与 ComfyUI 原生 ImageScale（拉伸变形）的区别在于保纵横比；
  与 MiniMaxH3ImageToVideo 对 first_frame 的内部处理（plain stretch，见
  comfy_extras/nodes_minimax_h3.py 的 "geometry anchor: plain stretch to
  canvas"）的区别同上 —— 参考图纵横比与画布不一致时，直接喂会给首帧硬拉
  变形；先过本节点即居中裁剪，构图不变形、输出尺寸仍由聚合节点决定。

工作流接线（H3 / FastH3 六支视频档的第 1 参考槽）：
  LoadImage(参考槽0) -> RoundaboutCoverResize -> 聚合节点 first_frame /
  ref_images.ref_image_0；节点的 width/height 在 models.yaml 里与聚合节点的
  width/height 绑定到同一个请求参数，画布改档位时裁剪目标自动跟随。
"""
import math

import torch

import comfy.utils
import nodes


class RoundaboutCoverResize:
    """image [B,H,W,C] -> 等比铺满 + 居中裁剪 -> 恰好 [B,height,width,C]。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "width": ("INT", {
                    "default": 1344, "min": 64, "max": 8192, "step": 32,
                    "tooltip": "目标画布宽（与聚合节点的 width 绑定到同一请求参数）。"}),
                "height": ("INT", {
                    "default": 768, "min": 64, "max": 8192, "step": 32,
                    "tooltip": "目标画布高（与聚合节点的 height 绑定到同一请求参数）。"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "resize"
    CATEGORY = "roundabout"

    def resize(self, image: torch.Tensor, width: int, height: int):
        b, h, w, c = image.shape
        if w < 1 or h < 1:
            raise ValueError("RoundaboutCoverResize: empty image")
        # cover：等比放大到「短边贴满」目标画布（scale 取 max，铺满后必有溢出可裁）
        scale = max(width / w, height / h)
        tw = max(width, round(w * scale))
        th = max(height, round(h * scale))
        up = comfy.utils.common_upscale(
            image.movedim(-1, 1), tw, th, "lanczos", "disabled").movedim(1, -1)
        top = max(0, (th - height) // 2)
        left = max(0, (tw - width) // 2)
        return (up[:, top:top + height, left:left + width, :].contiguous(),)


# ---------------------------------------------------------------- H3 统一聚合节点
# 复用 comfy_extras.nodes_minimax_h3 的内部函数（帧/参考的缩放与 latent 编码逻辑
# 必须与原版两节点逐字节一致，避免同一能力两套口径）。这些是下划线私有名：
# ComfyUI 升级若改动签名，这里会在节点加载期直接炸出来，不会静默错。
from comfy_extras.nodes_minimax_h3 import (  # noqa: E402
    CANVAS_MULTIPLE,
    FPS,
    REF_IMAGE_SHORT_EDGE,
    _empty_av_latent,
    _encode_ref_audio,
    _resize,
    adapt_canvas,
)
from comfy_api.latest import io  # noqa: E402
import node_helpers  # noqa: E402


class MiniMaxH3UnifiedToVideo(io.ComfyNode):
    """t2va / fl2va / ref2va / 首尾帧+参考 组合的统一条件节点。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3UnifiedToVideo",
            display_name="MiniMax H3 Unified to Video (Roundabout)",
            category="model/conditioning/minimax",
            description=(
                "Unified MiniMax H3 conditioning: prompt / first+last keyframes / "
                "reference images-videos-audios, freely combinable. Keyframes become "
                "actual output frames; references are conditioning only (output size "
                "follows width/height). Prompt with <Picture i> / <Video k> / <Audio j>: "
                "when both frames and references are given, <Picture 1>/<Picture 2> are "
                "the first/last frame and later numbers are references."
            ),
            inputs=[
                io.Clip.Input("clip"),
                io.Vae.Input("vae", optional=True,
                             tooltip="Video VAE. Required for keyframes and for reference images/videos to condition the latent."),
                io.Vae.Input("audio_vae", optional=True,
                             tooltip="Audio VAE. Required for reference audio / video soundtracks to condition the latent."),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Int.Input("width", default=1344, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("height", default=768, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("length", default=124, min=5, max=3600, step=17,
                             tooltip="Frame count at 24 fps, snapped up to the model's 17k+5 grid (124 = ~5s; trained range is ~124-362, longer is untested)"),
                io.Image.Input("first_frame", optional=True),
                io.Image.Input("last_frame", optional=True),
                io.Combo.Input("ref_image_size", options=["match", "max"], default="match",
                    tooltip="Reference image sizing. 'match' scales each ref (down only, keeping aspect) to the generation's pixel area; 'max' uses the reference pipeline's 2048px short edge for best identity fidelity. Reference tokens ride through every sampling step, so 'max' can be several times slower."),
                io.Autogrow.Input("ref_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref_image", tooltip="Reference image (downscaled to 2048 short edge if larger, never upscaled)"),
                        prefix="ref_image_", min=0, max=9)),
                io.Autogrow.Input("ref_videos", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref_video", tooltip="Reference video frames at 24 fps (2-15s)"),
                        prefix="ref_video_", min=0, max=3)),
                io.Autogrow.Input("ref_video_audios", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Audio.Input("ref_video_audio", tooltip="Soundtrack of the same-numbered reference video"),
                        prefix="ref_video_audio_", min=0, max=3)),
                io.Autogrow.Input("ref_audios", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Audio.Input("ref_audio", tooltip="Standalone reference audio"),
                        prefix="ref_audio_", min=0, max=3)),
            ],
            outputs=[io.Conditioning.Output(display_name="positive"), io.Latent.Output()],
        )

    @classmethod
    def execute(cls, clip, prompt, width, height, length, ref_image_size="match",
                vae=None, audio_vae=None, first_frame=None, last_frame=None,
                ref_images=None, ref_videos=None, ref_video_audios=None,
                ref_audios=None) -> io.NodeOutput:
        latent, frame_count = _empty_av_latent(width, height, length)

        # ---- 首尾帧（fl2va 语义）：几何锚定 + 成为实际输出帧 ----
        # 首帧 plain stretch（geometry anchor）、尾帧 cover（follower），与原版一致。
        keyframes = []
        frame_items = []  # 进 <Picture> 呈现的帧图
        if first_frame is not None:
            if vae is None:
                raise ValueError("first_frame anchoring needs the vae input")
            img = _resize(first_frame[:1], width, height, "disabled")
            frame_items.append({"type": "image", "data": img})
            keyframes.append({"resolved_frame_index": 0, "image": img})
        if last_frame is not None:
            if vae is None:
                raise ValueError("last_frame anchoring needs the vae input")
            img = _resize(last_frame[:1], width, height, "center")
            frame_items.append({"type": "image", "data": img})
            keyframes.append({"resolved_frame_index": frame_count - 1, "image": img})

        # ---- 参考（ref2va 语义）：conditioning only，输出尺寸仍由画布决定 ----
        ref_items = []   # tokenizer 呈现，按请求顺序
        ref_blocks = []  # DiT payload，同序
        for img in (ref_images or {}).values():
            if img is None:
                continue
            h, w = img.shape[1], img.shape[2]
            if ref_image_size == "match":
                scale = min(1.0, math.sqrt((width * height) / (w * h)))
            else:
                scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
            tw = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            th = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            resized = _resize(img[:1], tw, th, "disabled")
            ref_items.append({"type": "image", "data": resized})
            if vae is not None:
                z = vae.encode(resized)
                ref_blocks.append({"kind": "image", "latent_h": th // 16, "latent_w": tw // 16, "latent": z})

        ref_video_audios = ref_video_audios or {}
        for name, video_frames in (ref_videos or {}).items():
            if video_frames is None:
                continue
            soundtrack = ref_video_audios.get("ref_video_audio_" + name.rsplit("_", 1)[-1])
            vh, vw = video_frames.shape[1], video_frames.shape[2]
            cw, ch = adapt_canvas(vw, vh)
            if vw * vh < cw * ch:
                cw = max(CANVAS_MULTIPLE, round(vw / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
                ch = max(CANVAS_MULTIPLE, round(vh / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            frames = _resize(video_frames, cw, ch, "disabled")
            if frames.shape[0] > frame_count:
                frames = frames[:frame_count]
            n = frames.shape[0]
            if n < 5:
                raise ValueError("MiniMax H3 reference videos need at least 5 frames (~0.2s at 24 fps)")
            while n % 17 != 5:
                n -= 1
            frames = frames[:n]
            if soundtrack is not None:
                ref_items.append({"type": "audio"})
            sample_idx = list(range(0, frames.shape[0], FPS // 2))
            qwen_frames = frames[sample_idx]
            ref_items.append({"type": "video", "data": qwen_frames,
                              "timestamps": [i / 2.0 for i in range(len(sample_idx))]})
            if vae is None:
                continue
            z = vae.encode(frames)
            audio_latent, ref_audio_t = (None, 0)
            if soundtrack is not None and audio_vae is not None:
                audio_latent, ref_audio_t = _encode_ref_audio(audio_vae, soundtrack)
            ref_blocks.append({"kind": "video_audio" if ref_audio_t else "video",
                               "latent_t": z.shape[2], "latent_h": ch // 16, "latent_w": cw // 16,
                               "ref_audio_t": ref_audio_t, "latent": z, "audio_latent": audio_latent})

        for audio in (ref_audios or {}).values():
            if audio is None:
                continue
            ref_items.append({"type": "audio"})
            if audio_vae is not None:
                audio_latent, ref_audio_t = _encode_ref_audio(audio_vae, audio)
                ref_blocks.append({"kind": "audio", "ref_audio_t": ref_audio_t, "audio_latent": audio_latent})

        # ---- tokenize：统一走 minimax_ref_items 分支 ----
        # tokenizer 里 ref_items 优先于 images=（互斥），且两条分支对 image 的呈现
        # 逐 token 等价（"<Picture N>: " + vision，N 从 1 起）。帧图排最前：纯帧时
        # 与原版 fl2va 完全一致；帧+参考时 <Picture 1/2> 是帧、后续编号是参考。
        items = frame_items + ref_items
        tokens = clip.tokenize(prompt, minimax_ref_items=items) if items else clip.tokenize(prompt)
        cond = clip.encode_from_tokens_scheduled(tokens)

        if keyframes:
            for kf in keyframes:
                kf["latent"] = vae.encode(kf.pop("image"))
            cond = node_helpers.conditioning_set_values(cond, {"minimax_keyframes": keyframes})
        if ref_blocks:
            cond = node_helpers.conditioning_set_values(cond, {"minimax_refs": ref_blocks})
        return io.NodeOutput(cond, latent)


NODE_CLASS_MAPPINGS = {
    "RoundaboutCoverResize": RoundaboutCoverResize,
    "MiniMaxH3UnifiedToVideo": MiniMaxH3UnifiedToVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RoundaboutCoverResize": "Cover Resize (Roundabout)",
    "MiniMaxH3UnifiedToVideo": "MiniMax H3 Unified to Video (Roundabout)",
}
