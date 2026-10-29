from __future__ import annotations

import base64
import io
import json
import os
import re
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from server.bobtoken import chat
from server.catalog import spec_blocks
from server.db import FILES, connect, new_id, now, row_dict
from server.media import is_banner

USAGE_MODEL = "gpt-image-2-vip"
USAGE_GROUP = "image2"
USAGE_SIZE = "1024x1024"
USAGE_QUALITY = "low"


def render_usage_prompt(brief: dict) -> str:
    info = brief["product_info"]
    scenarios = brief["scenarios"]
    shots = brief["detail_shots"]
    payload = json.dumps(brief, ensure_ascii=False, indent=2)
    return f"""product_data.json:
{payload}

Role & Task:
You are an expert commercial product photographer. Based on the uploaded reference image [White-background product photo] and the provided JSON metadata, generate 1 high-definition 3x3 grid (9-panel collage image) showcasing the product in various lifestyle usage scenes and detailed close-ups.

Input Data Reference (from product_data.json):
- Product Name: {info["product_name"]}
- Material & Color: {info["material_and_color"]}
- Style: {info["style"]}
- Key Features: {info["key_features"]}
- Included Accessories: {info["included_accessories"]}

Product Identity Constraint:
- Strictly maintain 100% product identity, shape, structure, color, texture, and proportions from the provided white-background reference photo. Do not distort, alter, or exaggerate the product.

Layout & Grid Rules (3x3 Grid / 9 Panels Total):
- Overall Layout: High-resolution 3x3 grid layout with 9 clean, non-overlapping panels.
- 4 Scenario Main Shots (4 Panels): Show realistic contextual setups and natural interactions featuring the product across these 4 distinct scenarios:
  1. {scenarios["scenario_1"]}
  2. {scenarios["scenario_2"]}
  3. {scenarios["scenario_3"]}
  4. {scenarios["scenario_4"]}
- 5 Usage Detail Shots (5 Panels): High-end macro close-up photography capturing these specific functional and aesthetic details:
  1. {shots[0]}
  2. {shots[1]}
  3. {shots[2]}
  4. {shots[3]}
  5. {shots[4]}

Visual Style & Quality Guidelines:
- Style: Commercial lifestyle photography, high-end ecommerce look, natural soft shadows, shallow depth of field, sharp focus on product.
- Cohesion: All 9 panels must have a harmonious color palette, consistent lighting, and clean background aesthetics matching {scenarios["category"]}.
- Strict Negative Rules: No text, no logos/watermarks, no messy borders, no distorted features, no overlapping panels.

Parameters: --ar 1:1 --v 6.0
"""


def parse_usage_brief(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("场景信息没有整理好")
    try:
        brief = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("场景信息没有整理好") from exc
    info = brief.get("product_info") if isinstance(brief, dict) else None
    scenarios = brief.get("scenarios") if isinstance(brief, dict) else None
    shots = brief.get("detail_shots") if isinstance(brief, dict) else None
    info_keys = ("product_name", "material_and_color", "style", "key_features", "included_accessories")
    scene_keys = ("category", "scenario_1", "scenario_2", "scenario_3", "scenario_4")
    if not isinstance(info, dict) or not isinstance(scenarios, dict) or not isinstance(shots, list):
        raise ValueError("场景信息没有整理好")
    if any(not str(info.get(key) or "").strip() for key in info_keys):
        raise ValueError("场景信息没有整理好")
    if any(not str(scenarios.get(key) or "").strip() for key in scene_keys):
        raise ValueError("场景信息没有整理好")
    clean_shots = [str(item).strip() for item in shots if str(item).strip()]
    if len(clean_shots) < 5:
        raise ValueError("场景信息没有整理好")
    return {
        "product_info": {key: str(info[key]).strip() for key in info_keys},
        "scenarios": {key: str(scenarios[key]).strip() for key in scene_keys},
        "detail_shots": clean_shots[:5],
    }


def product_photo_paths(product_id: str) -> list[Path]:
    with connect() as db:
        rows = db.execute(
            "SELECT path FROM assets WHERE product_id = ? AND kind = 'product' ORDER BY created_at, id",
            (product_id,),
        ).fetchall()
    paths = []
    for row in rows:
        path = Path(row["path"] or "")
        if not path.is_file() or is_banner(path):
            continue
        paths.append(path)
        if len(paths) >= 3:
            break
    return paths


def _product_facts(product_id: str) -> str:
    with connect() as db:
        product = row_dict(db.execute("SELECT title, price, points, profile_text FROM products WHERE id = ?", (product_id,)).fetchone())
    if not product:
        raise ValueError("商品不存在")
    lines = []
    for label, key in (("商品名称", "title"), ("价格", "price"), ("卖点", "points"), ("产品信息", "profile_text")):
        value = (product.get(key) or "").strip()
        if value and value != "页面未提供":
            lines.append(f"{label}: {value}")
    for block in spec_blocks(product_id):
        value = (block.get("value") or "").strip()
        if not value or value == "页面未提供" or value.startswith("页面未提供"):
            continue
        if block["label"] in {"原始链接", "解析后链接", "获取状态"}:
            continue
        lines.append(f"{block['label']}: {value}")
    return "\n".join(lines)[:6000]


def plan_usage(product_id: str) -> dict:
    facts = _product_facts(product_id)
    if not facts:
        raise ValueError("还没有产品信息")
    text = chat(
        [
            {
                "role": "system",
                "content": "你只输出一个 JSON 对象，不要解释。商品事实只能来自用户给出的资料。资料没写的颜色、材质、尺寸、配件和功能，写成 as shown in the reference photos，不要编造。四个场景和五处细节必须是这件商品自己的用法，不能换成别的商品。所有字符串用英文。",
            },
            {
                "role": "user",
                "content": """按这个结构填写：
{
  "product_info": {
    "product_name": "",
    "material_and_color": "",
    "style": "",
    "key_features": "",
    "included_accessories": ""
  },
  "scenarios": {
    "category": "",
    "scenario_1": "",
    "scenario_2": "",
    "scenario_3": "",
    "scenario_4": ""
  },
  "detail_shots": ["", "", "", "", ""]
}

商品资料：
"""
                + facts,
            },
        ],
        timeout=120,
        temperature=0.2,
    )
    return parse_usage_brief(text)


def _jpeg(path: Path) -> bytes:
    from PIL import Image

    with Image.open(path) as image:
        frame = image.convert("RGB")
        frame.thumbnail((1280, 1280))
        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=72)
        return buffer.getvalue()


def _multipart(prompt: str, images: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )

    add_field("model", USAGE_MODEL)
    add_field("group", USAGE_GROUP)
    add_field("prompt", prompt)
    add_field("size", USAGE_SIZE)
    add_field("n", "1")
    add_field("quality", USAGE_QUALITY)
    add_field("response_format", "b64_json")
    for name, payload in images:
        chunks.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"{name}\"\r\n"
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def product_info_text(product: dict) -> str:
    profile = (product.get("profile_text") or "").strip()
    if profile:
        return profile[:6000]
    lines = []
    points = (product.get("points") or "").strip()
    if points and points != "还没写卖点":
        lines.append(points)
    from server.catalog import spec_blocks

    for block in spec_blocks(product.get("id") or ""):
        value = (block.get("value") or "").strip()
        if not value or value == "页面未提供" or value.startswith("页面未提供"):
            continue
        if block["label"] in {"原始链接", "解析后链接", "获取状态", "中文品名"}:
            continue
        lines.append(f"{block['label']}：{value}")
    return "\n".join(lines).strip()[:6000]


def fetch_relay_image(prompt: str, paths: list[Path], *, failure: str, empty: str) -> bytes:
    key = os.environ.get("IMAGE_RELAY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有配置图片中转站密钥")
    base = os.environ.get("IMAGE_RELAY_BASE_URL", "http://127.0.0.1:3000").strip().rstrip("/")
    images = []
    for index, path in enumerate(paths, start=1):
        try:
            payload = _jpeg(path)
        except Exception:
            payload = path.read_bytes()
        if len(payload) >= 1000:
            images.append((f"ref-{index}.jpg", payload))
    if not images:
        raise ValueError(empty)
    body, content_type = _multipart(prompt, images)
    request = urllib.request.Request(
        base + "/v1/images/edits",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:180]
        raise RuntimeError(f"{failure}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"图片中转站连不上：{exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("图片生成超时") from exc
    data = (payload or {}).get("data") or []
    if not data:
        raise RuntimeError(f"{failure}：没有返回图片")
    encoded = data[0].get("b64_json") or ""
    if encoded:
        return base64.b64decode(encoded)
    url = data[0].get("url") or ""
    if not url:
        raise RuntimeError(f"{failure}：没有返回图片")
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def fetch_usage_image(prompt: str, paths: list[Path]) -> bytes:
    return fetch_relay_image(prompt, paths[:3], failure="场景图生成失败", empty="还没有商品图")


def save_usage_image(product_id: str, payload: bytes, brief: dict) -> str:
    if len(payload) < 1000:
        raise RuntimeError("场景图是空的")
    root = FILES / "usages" / product_id
    root.mkdir(parents=True, exist_ok=True)
    asset_id = new_id("asset")
    dest = root / f"{asset_id}.png"
    dest.write_bytes(payload)
    with connect() as db:
        db.execute(
            "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, 'usage', ?, ?, ?, ?)",
            (asset_id, product_id, "使用场景图", str(dest), json.dumps(brief, ensure_ascii=False), now()),
        )
    return asset_id


REF_LIMIT = 3
DETAIL_LEAD = (
    "请先根据我提供的商品信息和图片，生成一组同一商品的多视角参考图，包括正面、背面、左右侧面、45度角和细节特写。"
    "所有图片必须保持商品的外形、比例、颜色、材质、图案、Logo、结构及配件完全一致，不得随意增加、删除或改变商品细节。"
    "多视角图生成完成后，将它们与原始商品图一起作为后续所有电商图片的商品身份参考，固定商品一致性，再开始生成正式的主图、详情页或A+图片。"
    "原图中无法确认的隐藏结构不要擅自编造。"
    "这次只输出一张图，把正面、背面、左侧面、右侧面、45度角和细节特写排在同一张纯白背景上，每个视角单独一格，格子之间和画面边缘都留干净白边。"
    "不要加标题、卖点、价格、水印、边框或场景。"
)


def detail_prompt(profile: str) -> str:
    return f"{DETAIL_LEAD}\n{(profile or '').strip()}"


def _require_prompt(product_id: str, prompt: str) -> str:
    with connect() as db:
        product = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
    if not product or not product_info_text(product):
        raise ValueError("还没有产品信息")
    text = (prompt or "").strip()
    if not text:
        raise ValueError("请填写提示词")
    return text


def _reference_paths(product_id: str, asset_ids: list[str], kinds: set[str], *, wrong: str) -> list[Path]:
    if len(asset_ids) > REF_LIMIT:
        raise ValueError("最多留 3 张参考图")
    with connect() as db:
        rows = {
            row["id"]: row_dict(row)
            for row in db.execute("SELECT id, path, kind FROM assets WHERE product_id = ?", (product_id,))
        }
    paths = []
    for asset_id in asset_ids:
        row = rows.get(asset_id)
        if not row or row["kind"] not in kinds:
            raise ValueError(wrong)
        path = Path(row["path"] or "")
        if path.is_file():
            paths.append(path)
    if not paths:
        raise ValueError("至少留一张参考图")
    return paths


def _save_generated(product_id: str, kind: str, label: str, folder: str, payload: bytes, meta: dict) -> str:
    if len(payload) < 1000:
        raise RuntimeError(f"{label}是空的")
    root = FILES / folder / product_id
    root.mkdir(parents=True, exist_ok=True)
    asset_id = new_id("asset")
    dest = root / f"{asset_id}.png"
    dest.write_bytes(payload)
    with connect() as db:
        db.execute(
            "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (asset_id, product_id, kind, label, str(dest), json.dumps(meta, ensure_ascii=False), now()),
        )
    return asset_id


def _replace_kind(product_id: str, kind: str) -> None:
    with connect() as db:
        rows = [row_dict(row) for row in db.execute("SELECT id, path FROM assets WHERE product_id = ? AND kind = ?", (product_id, kind))]
        if rows:
            marks = ",".join("?" for _ in rows)
            ids = [row["id"] for row in rows]
            db.execute(f"DELETE FROM variant_assets WHERE asset_id IN ({marks})", ids)
            db.execute("DELETE FROM assets WHERE product_id = ? AND kind = ?", (product_id, kind))
    for row in rows:
        path = Path(row["path"] or "")
        if path.is_file():
            path.unlink(missing_ok=True)


def generate_white_image(product_id: str, prompt: str, asset_ids: list[str]) -> str:
    text = _require_prompt(product_id, prompt)
    paths = _reference_paths(product_id, asset_ids, {"product", "white"}, wrong="参考图不存在")
    payload = fetch_relay_image(text, paths, failure="白底图生成失败", empty="至少留一张参考图")
    return _save_generated(product_id, "white", "白底图", "whites", payload, {"generated": True})


def generate_detail_image(product_id: str, prompt: str, asset_ids: list[str]) -> str:
    text = _require_prompt(product_id, prompt)
    paths = _reference_paths(product_id, asset_ids, {"product", "white"}, wrong="细节图只能参考商品图或白底图")
    payload = fetch_relay_image(text, paths, failure="细节图生成失败", empty="至少留一张参考图")
    _replace_kind(product_id, "detail")
    return _save_generated(product_id, "detail", "细节图", "details", payload, {"generated": True})


def generate_usage_image(product_id: str, prompt: str, asset_ids: list[str]) -> str:
    text = _require_prompt(product_id, prompt)
    paths = _reference_paths(product_id, asset_ids, {"detail"}, wrong="使用场景图只能参考细节图")
    payload = fetch_relay_image(text, paths, failure="场景图生成失败", empty="至少留一张参考图")
    return save_usage_image(product_id, payload, {"prompt": text})


def prepare_usage_prompt(product_id: str) -> str:
    return render_usage_prompt(plan_usage(product_id))


def generate_usage_sheet(product_id: str) -> str:
    with connect() as db:
        detail_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM assets WHERE product_id = ? AND kind = 'detail' ORDER BY created_at, id",
                (product_id,),
            )
        ]
    return generate_usage_image(product_id, prepare_usage_prompt(product_id), detail_ids[:REF_LIMIT])
