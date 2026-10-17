"""`/v1/images/edits` 多部分解析守卫：图片分部按字段名/文件名/Content-Type 识别。

回归点：liteLLM（以及 OpenAI 官方 SDK 的多图写法）把图片发成 `image[]`，
旧实现只认裸 `image`，其余分部一律 `await part.text()` —— 二进制进 UTF-8 解码，
`UnicodeDecodeError` 冒到 HTTP 500，客户端只看到 "Internal server error"。

不需要 ComfyUI、不占 GPU：只把解析器挂到独立 aiohttp 实例（临时端口）上，
发五种形态的 multipart 请求，断言解析结果。

    python tests/test_multipart_image_fields.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

NODE = Path(__file__).resolve().parent.parent
ROOT = NODE.parent.parent
for p in (str(NODE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from gateway.handlers import _read_image_multipart  # noqa: E402

BOUNDARY = "----roundabout-test-boundary"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-body" * 8


def part(name: str, value: bytes, filename: str | None = None, ctype: str | None = None) -> bytes:
    head = [f'--{BOUNDARY}', f'Content-Disposition: form-data; name="{name}"'
            + (f'; filename="{filename}"' if filename else "")]
    if ctype:
        head.append(f"Content-Type: {ctype}")
    return ("\r\n".join(head) + "\r\n\r\n").encode() + value + b"\r\n"


def multipart(*parts: bytes) -> bytes:
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


async def echo(request: web.Request) -> web.Response:
    fields, images, mask = await _read_image_multipart(request)
    return web.json_response({
        "fields": fields,
        "images": [{"len": len(b), "sha": hashlib.sha256(b).hexdigest()[:12]} for b in images],
        "mask": None if mask is None else {"len": len(mask)},
    })


async def main() -> int:
    app = web.Application()
    app.router.add_post("/parse", echo)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"

    def post(body: bytes):
        req = urllib.request.Request(
            base + "/parse", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {"raw": e.read().decode("utf-8", "replace")[:200]}

    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail and not ok else ''}")
        if not ok:
            failures += 1

    loop = asyncio.get_running_loop()
    sha = hashlib.sha256(PNG).hexdigest()[:12]

    print("== 1) 标准 `image` + 文本字段 ==")
    body = multipart(
        part("model", b"flux3-klein-edit"),
        part("prompt", b"turn the square blue"),
        part("size", b"512x512"),
        part("image", PNG, filename="in.png", ctype="image/png"),
    )
    st, d = await loop.run_in_executor(None, post, body)
    check("返回 200", st == 200, f"status={st} {d}")
    check("1 张图", len(d.get("images") or []) == 1, str(d.get("images")))
    check("图片字节完整", (d.get("images") or [{}])[0].get("sha") == sha)
    check("文本字段完整", d.get("fields", {}).get("prompt") == "turn the square blue", str(d.get("fields")))

    print("== 2) `image[]` 多图（liteLLM / OpenAI SDK 形态）==")
    body = multipart(
        part("model", b"flux3-klein-edit3"),
        part("prompt", b"merge them"),
        part("image[]", PNG, filename="a.png", ctype="image/png"),
        part("image[]", PNG + b"x", filename="b.png", ctype="image/png"),
    )
    st, d = await loop.run_in_executor(None, post, body)
    check("返回 200（旧实现此处 500）", st == 200, f"status={st} {d}")
    check("2 张图", len(d.get("images") or []) == 2, str(d.get("images")))
    check("第一张字节完整", (d.get("images") or [{}])[0].get("sha") == sha)
    check("提示词未被图片挤掉", d.get("fields", {}).get("prompt") == "merge them", str(d.get("fields")))

    print("== 3) 未知字段名但带文件名/图片类型 ==")
    body = multipart(
        part("model", b"utility-seedvr2-upscale"),
        part("image_1", PNG, filename="ref.png", ctype="image/png"),
    )
    st, d = await loop.run_in_executor(None, post, body)
    check("返回 200", st == 200, f"status={st} {d}")
    check("按图片处理", len(d.get("images") or []) == 1, str(d.get("images")))

    print("== 4) mask 单独走 ==")
    body = multipart(
        part("prompt", b"inpaint"),
        part("image", PNG, filename="in.png", ctype="image/png"),
        part("mask", PNG + b"mask", filename="m.png", ctype="image/png"),
    )
    st, d = await loop.run_in_executor(None, post, body)
    check("返回 200", st == 200, f"status={st} {d}")
    check("1 张图 + mask", len(d.get("images") or []) == 1 and d.get("mask") is not None, str(d))
    check("mask 没有混进图片列表", d.get("mask", {}).get("len") == len(PNG) + 4, str(d.get("mask")))

    print("== 5) 纯文本 multipart（无图片）==")
    body = multipart(part("model", b"z-image-turbo"), part("prompt", b"a red square"))
    st, d = await loop.run_in_executor(None, post, body)
    check("返回 200", st == 200, f"status={st} {d}")
    check("无图片", not (d.get("images") or []), str(d.get("images")))
    check("字段照旧", d.get("fields", {}).get("model") == "z-image-turbo", str(d.get("fields")))

    await site.stop()
    await runner.cleanup()

    print(f"\n===== {'OK' if failures == 0 else str(failures) + ' Fehler'} =====")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
