"""Roundabout 自带的 ComfyUI 节点（工作流模板内部使用，不进 MCP / tool-info）。

当前只有一个节点：

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
import torch

import comfy.utils


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


NODE_CLASS_MAPPINGS = {
    "RoundaboutCoverResize": RoundaboutCoverResize,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RoundaboutCoverResize": "Cover Resize (Roundabout)",
}
