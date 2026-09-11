"""ComfyUI Manager 安装钩子：安装 Roundabout 所需依赖。

两种触发方式：
  1) ComfyUI Manager 安装/更新本节点时自动执行（约定的 install.py）；
  2) 手动执行：
       "<ComfyUI 的 python>" "本目录/install.py"

关键点：必须用**运行 ComfyUI 的那个解释器**安装，装到系统 python 里无效。
典型路径（ComfyUI-aki 便携包）：E:\\ai\\ComfyUI-aki-v3\\python\\python.exe
"""

from __future__ import annotations

import os
import subprocess
import sys

REQ_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "requirements.txt")


def main() -> int:
    print(f"[Roundabout] installing dependencies with: {sys.executable}")

    # 国内网络可设 PIP_INDEX_URL 加速，例如：
    #   set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
    cmd = [sys.executable, "-m", "pip", "install", "-r", REQ_FILE]
    try:
        subprocess.check_call(cmd)
    except FileNotFoundError as exc:
        print(f"[Roundabout] ERROR: 找不到 pip / 解释器不可执行: {exc}")
        print(f"[Roundabout] 请手动执行: \"{sys.executable}\" -m pip install -r \"{REQ_FILE}\"")
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"[Roundabout] ERROR: pip 安装失败（退出码 {exc.returncode}）。")
        print("[Roundabout] 若是网络超时，可先设置镜像源后重试：")
        print('[Roundabout]   set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple')
        print(f"[Roundabout] 或手动执行: \"{sys.executable}\" -m pip install -r \"{REQ_FILE}\"")
        return 1

    print("[Roundabout] dependencies OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
