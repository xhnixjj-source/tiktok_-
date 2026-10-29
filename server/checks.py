from __future__ import annotations

import re
from pathlib import Path

from server.db import connect, loads, row_dict

TARGET_SECONDS = 15.0
DURATIONS = (10, 15, 30, 45, 60)


def allowed_duration(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 15
    return number if number in DURATIONS else 15
def _wants_subtitle(text: str) -> bool:
    lowered = text.lower()
    markers = ("加字幕", "显示字幕", "屏幕文字", "on-screen text", "add subtitle", "caption overlay")
    return any(marker.lower() in lowered for marker in markers)


def default_shots(description: str) -> list[dict]:
    scene = description.strip()
    return [
        {"ordinal": 1, "t_start": 0, "t_end": 3, "camera": "手入画", "action": f"先给钩子。场景只取：{scene}", "voiceover": ""},
        {"ordinal": 2, "t_start": 3, "t_end": 10, "camera": "产品动作", "action": "商品外观锁定产品图。动作发生在该场景里。", "voiceover": ""},
        {"ordinal": 3, "t_start": 10, "t_end": 15, "camera": "收束", "action": "商品停在画面中央。不要在画面上写字幕。", "voiceover": ""},
    ]


def build_prompt(product: dict, scene: dict, shots: list[dict], assets: list[dict]) -> str:
    lines = [
        "Ecommerce selling video. English generation instructions.",
        rules_path().read_text(encoding="utf-8").strip(),
        f"Product title: {product.get('title') or 'unknown'}.",
        f"Price: {product.get('price') or 'unknown'}.",
        "Use only selling points present in the product notes below. Do not invent claims.",
        f"Product notes: {product.get('points') or '(none)'}",
        f"Scene image is attached. Scene instruction: {scene.get('description') or ''}",
        "Keep the product appearance locked to the attached product images.",
        "Do not clone any face, voice, or music from a reference video. Reference video, if attached, supplies camera and action only.",
        "Do not render subtitles or on-screen text. Subtitles are added after generation.",
    ]
    if any(item.get("kind") == "person" for item in assets):
        lines.append("A presenter image is attached. Use that person only. Do not copy a face from the reference video.")
    for asset in assets:
        if asset.get("kind") != "reference":
            continue
        meta = loads(asset.get("meta_json") or "{}", {})
        transcript = (meta.get("transcript") or "").strip()
        if transcript:
            lines.append("Reference transcript, camera and action only. Do not copy claims, face, voice, or music: " + transcript[:1500])
    for shot in shots:
        lines.append(
            f"Shot {shot['ordinal']} ({shot['t_start']}-{shot['t_end']}s), camera {shot.get('camera') or ''}: {shot.get('action') or ''}. Voiceover: {shot.get('voiceover') or 'none'}."
        )
    return "\n".join(lines)


def evaluate(product: dict, scene: dict, shots: list[dict], assets: list[dict], prompt: str, require_scene: bool | None = None) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    description = (scene.get("description") or "").strip()
    if require_scene is None:
        require_scene = bool(scene.get("id") or description or scene.get("image_path"))
    has_photo = any(item.get("kind") == "product" and item.get("path") for item in assets)
    add("已绑定产品图", has_photo, "" if has_photo else "把商品图连到这一格")
    has_scene = bool(scene.get("image_path") and description)
    add("场景有图也有描述词", (not require_scene) or has_scene, "" if (not require_scene) or has_scene else "场景缺图或描述词")
    duration = round(sum(float(shot["t_end"]) - float(shot["t_start"]) for shot in shots), 2)
    add("分镜时长合计 15 秒", abs(duration - TARGET_SECONDS) < 0.2, f"当前合计 {duration} 秒")
    add("每一镜都有动作", all((shot.get("action") or "").strip() for shot in shots), "补上空的动作")
    shot_text = " ".join(f"{shot.get('action') or ''} {shot.get('voiceover') or ''}" for shot in shots)
    add("生成提示词不含字幕要求", not _wants_subtitle(shot_text), "字幕留到成片之后再叠")
    points = product.get("points") or ""
    voice = " ".join((shot.get("voiceover") or "") for shot in shots)
    numbers = set(re.findall(r"\d+(?:\.\d+)?", voice))
    allowed = set(re.findall(r"\d+(?:\.\d+)?", f"{points} {product.get('price') or ''} {product.get('title') or ''}"))
    invented = sorted(numbers - allowed)
    add("口播里的数字都能在商品资料里找到", not invented, "、".join(invented))
    if voice.strip() and not points.strip():
        add("没有商品卖点时口播留空", False, "先填写来自页面的卖点，或清空口播")
    else:
        add("没有商品卖点时口播留空", True, "")
    add("提示词带有场景描述词", (not require_scene) or (description in prompt), "")
    return checks


def all_passed(checks: list[dict]) -> bool:
    return all(item["ok"] for item in checks)


def list_white_assets(product_id: str) -> list[dict]:
    if not product_id:
        return []
    with connect() as db:
        return [
            row_dict(row)
            for row in db.execute(
                "SELECT * FROM assets WHERE product_id = ? AND kind = 'white' ORDER BY created_at, id",
                (product_id,),
            )
            if (row["path"] or "").strip()
        ]


def load_variant_bundle(variant_id: str) -> dict | None:
    with connect() as db:
        variant = row_dict(db.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone())
        if not variant:
            return None
        product = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (variant["product_id"],)).fetchone())
        scene = row_dict(db.execute("SELECT * FROM scenes WHERE id = ?", (variant["scene_id"],)).fetchone())
        shots = [row_dict(row) for row in db.execute("SELECT * FROM shots WHERE variant_id = ? ORDER BY ordinal", (variant_id,))]
        assets = [
            row_dict(row)
            for row in db.execute(
                """
                SELECT assets.* FROM assets
                JOIN variant_assets ON variant_assets.asset_id = assets.id
                WHERE variant_assets.variant_id = ?
                """,
                (variant_id,),
            )
        ]
    variant["check"] = loads(variant.get("check_json") or "[]", [])
    return {"variant": variant, "product": product or {}, "scene": scene or {}, "shots": shots, "assets": assets}


def save_check(variant_id: str, prompt: str, checks: list[dict], status: str, round_no: int, prompt_zh: str = "") -> None:
    with connect() as db:
        db.execute(
            "UPDATE variants SET prompt_text = ?, prompt_zh = ?, check_json = ?, status = ?, round = ? WHERE id = ?",
            (prompt, (prompt_zh or "").strip(), __import__("json").dumps(checks, ensure_ascii=False), status, round_no, variant_id),
        )


def rules_path() -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / "seedance-rules.md"
