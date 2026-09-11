"""去背景独立端点 e2e：独立 aiohttp 实例注册网关路由（8199 回环，不动正在跑的 ComfyUI），
后端 ComfyClient 打真实 ComfyUI（8188），全链路验证 /v1/images/remove-background。

覆盖：
1. 正常去背景：multipart image -> 200 + data[0].url，产物落盘且是带 alpha 的 PNG
2. 缺 image -> 400（requires an input `image`）
3. 打错模型（非 promptless）-> 400
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 从本文件位置反推目录，不写死安装路径：<ComfyUI>/custom_nodes/ComfyUI-Roundabout/test_removebg_e2e.py
NODE = Path(__file__).resolve().parent   # 节点目录
ROOT = NODE.parent.parent                # ComfyUI 根目录（custom_nodes 的上一级）
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from gateway.registry import registry  # noqa: E402
from gateway.routes import register_routes  # noqa: E402

registry.load(NODE / "models.yaml", NODE / "workflows")

PORT = 8199
BASE = f"http://127.0.0.1:{PORT}"

# 找一张真实测试图
SRC = next(
    (ROOT / "output").glob("*.png")
)
PNG = SRC.read_bytes()


def multipart(fields: dict[str, str], files: dict[str, bytes]) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, v in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="in.png"\r\n'
            f"Content-Type: image/png\r\n\r\n".encode() + v + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def post(path: str, content_type: str, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def main() -> int:
    app = web.Application(client_max_size=256 * 1024 * 1024)
    register_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    loop = asyncio.get_running_loop()
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + str(detail)) if detail and not ok else ''}")
        if not ok:
            failures += 1

    print("== 1. 正常去背景 ==")
    ct, body = multipart({"response_format": "url", "filename_prefix": "RemoveBG-e2e"}, {"image": PNG})
    st, raw = await loop.run_in_executor(None, post, "/v1/images/remove-background", ct, body)
    check("返回 200", st == 200, (raw[:300] if st != 200 else ""))
    d = json.loads(raw)
    url = d["data"][0]["url"]
    check("data[0].url 存在", bool(url), d)
    fname = url.split("filename=")[-1].split("&")[0]
    out = ROOT / "output" / fname
    check("产物已落盘(output/)", out.exists(), fname)
    if out.exists():
        head = out.read_bytes()[:33]
        check("PNG 且带 alpha 颜色类型(Ct=6)", head[25] == 6, head.hex())

    print("== 2. 缺 image ==")
    ct, body = multipart({}, {})
    st, raw = await loop.run_in_executor(None, post, "/v1/images/remove-background", ct, body)
    d = json.loads(raw)
    check("返回 400", st == 400, st)
    check("报错提示 image", "image" in json.dumps(d), d)

    print("== 3. 非 promptless 模型被拒 ==")
    ct, body = multipart({"model": "z-image-turbo"}, {"image": PNG})
    st, raw = await loop.run_in_executor(None, post, "/v1/images/remove-background", ct, body)
    check("返回 400", st == 400, (st, raw[:200]))

    await runner.cleanup()
    print("ALL PASS" if not failures else f"{failures} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
