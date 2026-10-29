from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from server.checks import allowed_duration, list_white_assets
from server.db import connect

MODEL = "doubao-seedance-2-0-fast-260128"
OMNI_MODEL = "gemini-omni-1.1-flash-preview"
_TERMINAL = {"succeeded", "success", "completed", "failed", "cancelled", "canceled", "error"}
_OK = {"succeeded", "success", "completed"}


def video_model() -> str:
    return os.environ.get("BOBTOKEN_VIDEO_MODEL", MODEL).strip() or MODEL


def _is_omni() -> bool:
    return video_model() == OMNI_MODEL


def video_key() -> str:
    if _is_omni():
        key = os.environ.get("TOAPIS_API_KEY", "").strip()
        if not key:
            raise RuntimeError("没有配置 TOAPIS_API_KEY")
        return key
    key = os.environ.get("BOBTOKEN_VIDEO_API_KEY", "").strip() or os.environ.get("BOBTOKEN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有配置 BOBTOKEN_VIDEO_API_KEY")
    return key


def video_base() -> str:
    if _is_omni():
        return os.environ.get("TOAPIS_BASE_URL", "https://toapis.cn/v1").strip().rstrip("/")
    base = os.environ.get("BOBTOKEN_BASE_URL", "https://www.bobtoken.cloud/v1").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    return base


def _note(variant_id: str, text: str) -> None:
    with connect() as db:
        db.execute(
            "UPDATE variants SET qa_note = ? WHERE id = ? AND status = 'running'",
            (text[:1000], variant_id),
        )


def _data_url(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError("白底图文件不在")
    raw = path.read_bytes()
    if len(raw) < 32:
        raise RuntimeError("白底图是空的")
    if len(raw) > 8_000_000:
        raise RuntimeError("白底图超过 8MB，请换一张更小的再生成")
    if raw.startswith(b"\x89PNG"):
        mime = "image/png"
    elif raw.startswith(b"RIFF"):
        mime = "image/webp"
    elif raw.startswith(b"GIF8"):
        mime = "image/gif"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _image_file(path: Path) -> tuple[bytes, str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\x89PNG"):
        return raw, "image/png", "product.png"
    if raw.startswith(b"RIFF"):
        return raw, "image/webp", "product.webp"
    if raw.startswith(b"GIF8"):
        return raw, "image/gif", "product.gif"
    return raw, "image/jpeg", "product.jpg"


def _upload_public_image(path: Path) -> str:
    raw, mime, filename = _image_file(path)
    if len(raw) > 10_000_000:
        raise RuntimeError("白底图超过 10MB，Gemini Omni 传不上去")
    boundary = "selling" + uuid.uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            raw,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        video_base() + "/uploads/images",
        data=body,
        headers={
            "Authorization": f"Bearer {video_key()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"白底图上传失败 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"白底图上传失败：{exc.reason}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    url = data.get("url") if isinstance(data, dict) else ""
    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError("白底图上传后没有返回地址")
    return url


def _white_layout(path: Path) -> tuple[int, int, float] | None:
    """Count internal white gutters. A multi-angle detail sheet has both horizontal and vertical gutters."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError("无法读取白底图，请安装 Pillow。") from exc
    try:
        image = Image.open(path).convert("L")
    except (OSError, UnidentifiedImageError):
        return None
    width, height = image.size
    if width < 8 or height < 8:
        return None
    step = max(1, min(width, height) // 400)
    sample_w = list(range(0, width, step))
    sample_h = list(range(0, height, step))
    pixels = image.load()

    def stats(values: list[int]) -> tuple[float, float]:
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return mean, variance ** 0.5

    rows = [stats([pixels[x, y] for x in sample_w]) for y in sample_h]
    cols = [stats([pixels[x, y] for y in sample_h]) for x in sample_w]

    def bands(lines: list[tuple[float, float]]) -> int:
        count = len(lines)
        margin = int(count * 0.06)
        quiet = [
            margin <= index < count - margin and mean >= 210 and deviation <= 18
            for index, (mean, deviation) in enumerate(lines)
        ]
        groups = 0
        index = margin
        while index < count - margin:
            if not quiet[index]:
                index += 1
                continue
            start = index
            while index < count - margin and quiet[index]:
                index += 1
            before = lines[start - 1][1] if start > 0 else 0
            after = lines[index][1] if index < count else 0
            if 1 <= index - start <= 20 and before >= 20 and after >= 20:
                groups += 1
        return groups

    border = [pixels[x, y] for y in (0, height - 1) for x in sample_w]
    border.extend(pixels[x, y] for x in (0, width - 1) for y in sample_h)
    white_border = sum(1 for value in border if value >= 245) / len(border)
    return bands(rows), bands(cols), white_border


def _omni_references(bundle: dict) -> list[tuple[str, Path]]:
    product_id = (bundle.get("product") or {}).get("id") or ""
    whites = [item for item in list_white_assets(product_id) if (item.get("path") or "").strip()]
    ranked: list[tuple[dict, tuple[int, int, float]]] = []
    for item in whites:
        layout = _white_layout(Path(item["path"]))
        if layout is not None:
            ranked.append((item, layout))
    sheets = [
        (index, item, layout)
        for index, (item, layout) in enumerate(ranked)
        if layout[0] >= 1 and layout[1] >= 1
    ]
    plains = [
        (index, item, layout)
        for index, (item, layout) in enumerate(ranked)
        if layout[0] + layout[1] <= 1 and layout[2] >= 0.85
    ]
    if not plains or not sheets:
        missing = []
        if not plains:
            missing.append("一张完整商品白底图")
        if not sheets:
            missing.append("一张多角度和产品细节的白底图")
        raise RuntimeError("白底图里还缺" + "和".join(missing) + "。这两张都要有，其他商品详情图不会提交。")
    _index, sheet, _layout = min(sheets, key=lambda item: (-(item[2][0] + item[2][1]), item[0]))
    plain_choices = [item for item in plains if item[1]["path"] != sheet["path"]]
    if not plain_choices:
        raise RuntimeError("白底图里还缺一张完整商品白底图。多角度细节图不能代替它。")
    _index, plain, _layout = min(plain_choices, key=lambda item: (item[2][0] + item[2][1], item[0]))
    return [
        ("white-background product photo", Path(plain["path"])),
        ("multi-angle and product-detail sheet on white", Path(sheet["path"])),
    ]


def _omni_prompt(prompt: str, seconds: int, references: list[tuple[str, Path]]) -> str:
    lines = ["Reference images in order:"]
    lines.extend(f"{index}. {label}" for index, (label, _) in enumerate(references, 1))
    lines.append("Keep the product identical to the white-background photo. Use the second image only for the other angles and close-up details.")
    if seconds != 10:
        lines.append(f"Output one 10-second 9:16 video. The storyboard below is written for {seconds} seconds; compress it into 10 seconds.")
    return "\n".join(lines) + "\n\n" + prompt


def _bob_key() -> str:
    key = os.environ.get("BOBTOKEN_VIDEO_API_KEY", "").strip() or os.environ.get("BOBTOKEN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有配置 BOBTOKEN_VIDEO_API_KEY")
    return key


def _bob_base() -> str:
    base = os.environ.get("BOBTOKEN_BASE_URL", "https://www.bobtoken.cloud/v1").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    return base


def _request_json(method: str, url: str, payload: dict | None = None, timeout: int = 120, auth: str | None = None, channel: str = "") -> dict:
    key = auth or video_key()
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        fast = channel == "fast" or not _is_omni()
        if "model_not_found" in detail or "No available channel" in detail:
            if fast:
                raise RuntimeError("BobToken 当前密钥没有 Seedance 2.0 Fast 通道。请在网关给这个分组加上模型 doubao-seedance-2-0-fast-260128。") from exc
            raise RuntimeError("ToAPIs 当前密钥没有 Gemini Omni 1.1。") from exc
        name = "Seedance" if fast else "Gemini Omni"
        raise RuntimeError(f"{name} 返回 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Seedance 连不上：{exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Seedance 超时") from exc


def _nested(data: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    inner = data.get("data")
    if isinstance(inner, dict):
        return _nested(inner, keys)
    return None


def _plain_video_url(data: Any) -> str | None:
    preferred: list[str] = []
    fallback: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    if key in {"video_url", "output_url"} or value.lower().split("?", 1)[0].endswith(".mp4"):
                        preferred.append(value)
                    else:
                        fallback.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if preferred:
        return preferred[0]
    return fallback[0] if fallback else None


def _fail_text(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("error", "message", "fail_reason", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _fail_text(value)
                if nested:
                    return nested
        inner = data.get("data")
        if isinstance(inner, (dict, list)):
            nested = _fail_text(inner)
            if nested:
                return nested
    return "Seedance 没有生成成片"


def render_variant(variant_id: str, output: Path) -> tuple[bool, str]:
    from server.bobtoken import voice_enabled
    from server.checks import load_variant_bundle

    bundle = load_variant_bundle(variant_id)
    if not bundle:
        return False, "版本不存在"
    prompt = (bundle["variant"].get("prompt_text") or "").strip()
    if not prompt:
        return False, "还没有提示词"
    seconds = allowed_duration(bundle["variant"].get("duration_sec"))
    if seconds < 4 or seconds > 15:
        return False, "Seedance 2.0 Fast 单次只支持 4 到 15 秒。请改成 10 秒或 15 秒，再检查一次提示词。"
    whites = [item for item in list_white_assets(bundle["product"].get("id") or "") if (item.get("path") or "").strip()]
    if not whites:
        return False, "先上传白底图，再生成视频"
    image = Path(whites[0]["path"])
    if _is_omni():
        try:
            references = _omni_references(bundle)
        except RuntimeError as exc:
            return False, str(exc)
        urls: list[str] = []
        try:
            for _, path in references:
                urls.append(_upload_public_image(path))
        except RuntimeError as exc:
            return False, str(exc)
        payload = {
            "model": OMNI_MODEL,
            "prompt": _omni_prompt(prompt, seconds, references),
            "duration": 10,
            "resolution": "720p",
            "aspect_ratio": "9:16",
            "image_urls": urls,
        }
        submit_url = video_base() + "/videos"
        label = "Gemini Omni"
        _note(variant_id, f"正在提交 Gemini Omni，固定 10 秒、720p，参考图 {len(urls)} 张")
    else:
        try:
            picture = _data_url(image)
        except RuntimeError as exc:
            return False, str(exc)
        payload = {
            "model": video_model(),
            "prompt": prompt,
            "images": [picture],
            "duration": seconds,
            "metadata": {
                "ratio": os.environ.get("BOBTOKEN_VIDEO_RATIO", "9:16").strip() or "9:16",
                "resolution": os.environ.get("BOBTOKEN_VIDEO_RESOLUTION", "480p").strip() or "480p",
                "generate_audio": voice_enabled(bundle),
                "watermark": False,
            },
        }
        submit_url = video_base() + "/video/generations"
        label = "Seedance 2.0 Fast"
        _note(variant_id, "正在把提示词和白底图提交给 Seedance 2.0 Fast")
    try:
        submitted = _request_json("POST", submit_url, payload)
    except RuntimeError as exc:
        return False, str(exc)
    task_id = _nested(submitted, ("id", "task_id", "taskId"))
    if not task_id:
        return False, f"{label} 没有返回任务编号：" + json.dumps(submitted, ensure_ascii=False)[:400]
    _note(variant_id, f"已提交 {label}，任务 {task_id}")
    deadline = time.time() + int(os.environ.get("BOBTOKEN_VIDEO_TIMEOUT", "900") or "900")
    interval = int(os.environ.get("BOBTOKEN_VIDEO_INTERVAL", "8") or "8")
    final: dict | None = None
    while True:
        try:
            status_url = submit_url + "/" + task_id
            data = _request_json("GET", status_url, timeout=60)
        except RuntimeError as exc:
            return False, str(exc)
        status = (_nested(data, ("status", "state")) or "unknown").lower()
        _note(variant_id, f"{label} 正在生成 · 任务 {task_id} · {status}")
        if status in _TERMINAL:
            final = data
            break
        if time.time() >= deadline:
            return False, f"{label} 超时，任务 {task_id}，最后状态 {status}"
        time.sleep(max(2, interval))
    if (_nested(final or {}, ("status", "state")) or "").lower() not in _OK:
        return False, _fail_text(final)
    video_url = _plain_video_url(final)
    if not video_url:
        return False, f"{label} 已完成，但没有返回视频地址"
    _note(variant_id, "正在下载成片")
    try:
        request = urllib.request.Request(video_url, method="GET")
        with urllib.request.urlopen(request, timeout=300) as response:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return False, f"成片下载失败 {exc.code}：{detail}"
    except urllib.error.URLError as exc:
        return False, f"成片下载失败：{exc.reason}"
    except TimeoutError:
        return False, "成片下载超时"
    if not output.is_file() or output.stat().st_size < 32:
        return False, "成片文件是空的"
    return True, task_id


def render_fast_clip(prompt: str, images: list[Path], seconds: int, output: Path) -> None:
    """Seedance 2.0 Fast at 480p. This stays on the cheap model even when 成片 uses Gemini Omni."""
    if not images:
        raise RuntimeError("没有可提交的图片")
    if seconds < 4 or seconds > 15:
        raise RuntimeError("Seedance 2.0 Fast 一次只支持 4 到 15 秒")
    pictures = [_data_url(path) for path in images]
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "images": pictures,
        "duration": seconds,
        "metadata": {
            "ratio": "9:16",
            "resolution": "480p",
            "generate_audio": False,
            "watermark": False,
        },
    }
    submit_url = _bob_base() + "/video/generations"
    submitted = _request_json("POST", submit_url, payload, auth=_bob_key(), channel="fast")
    task_id = _nested(submitted, ("id", "task_id", "taskId"))
    if not task_id:
        raise RuntimeError("Seedance 没有返回任务编号：" + json.dumps(submitted, ensure_ascii=False)[:400])
    deadline = time.time() + int(os.environ.get("BOBTOKEN_VIDEO_TIMEOUT", "900") or "900")
    interval = int(os.environ.get("BOBTOKEN_VIDEO_INTERVAL", "8") or "8")
    final: dict | None = None
    while True:
        data = _request_json("GET", submit_url + "/" + task_id, timeout=60, auth=_bob_key(), channel="fast")
        status = (_nested(data, ("status", "state")) or "unknown").lower()
        if status in _TERMINAL:
            final = data
            break
        if time.time() >= deadline:
            raise RuntimeError(f"Seedance 超时，任务 {task_id}，最后状态 {status}")
        time.sleep(max(2, interval))
    if (_nested(final or {}, ("status", "state")) or "").lower() not in _OK:
        raise RuntimeError(_fail_text(final))
    video_url = _plain_video_url(final)
    if not video_url:
        raise RuntimeError("Seedance 已完成，但没有返回视频地址")
    request = urllib.request.Request(video_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"成片下载失败 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"成片下载失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("成片下载超时") from exc
    if not output.is_file() or output.stat().st_size < 32:
        raise RuntimeError("成片文件是空的")
