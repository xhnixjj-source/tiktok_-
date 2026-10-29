from __future__ import annotations

import json
import threading
from pathlib import Path

from server.board import read_graph, story_port, write_node_data
from server.bobtoken import _image_part, chat
from server.db import FILES, connect, file_url, loads, new_id, now, row_dict
from server.usage import fetch_relay_image, product_info_text

_EMPTY_VIRAL = {"", "选一条已整理好的分镜", "这条视频的分镜还没整理好"}
_REF_CAP = 16
_FAST_REFS = 8
_FAST_MODEL = "Seedance 2.0 Fast · 480p"


def _duration(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 15
    return min(60, max(1, number))


def _count(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 4
    return min(12, max(1, number))


def _node(doc: dict, node_id: str) -> dict:
    node = next((item for item in doc.get("nodes") or [] if item.get("id") == node_id), None)
    if not node or node.get("type") != "storyboard":
        raise LookupError("节点不存在")
    return node


def _inputs(doc: dict, node_id: str) -> dict[str, dict | None]:
    nodes = {item.get("id"): item for item in doc.get("nodes") or []}
    found: dict[str, dict | None] = {"product": None, "viral": None, "person": None, "scene": None}
    for edge in doc.get("edges") or []:
        if edge.get("target") != node_id:
            continue
        handle = edge.get("targetHandle") or ""
        source = nodes.get(edge.get("source"))
        if handle in found and story_port(source) == handle:
            found[handle] = source
    return found


def _viral_text(node: dict | None) -> str:
    if not node:
        return ""
    data = node.get("data") or {}
    text = (data.get("text") or "").strip()
    hit_id = data.get("hitId") or ""
    if hit_id:
        with connect() as db:
            hit = db.execute("SELECT shot_prompt FROM hits WHERE id = ?", (hit_id,)).fetchone()
        if hit and (hit["shot_prompt"] or "").strip():
            text = hit["shot_prompt"].strip()
    if text in _EMPTY_VIRAL:
        return ""
    return text


def _even(duration: int, count: int) -> list[float]:
    raw = [round(duration / count, 1) for _ in range(count)]
    raw[-1] = round(duration - sum(raw[:-1]), 1)
    if raw[-1] <= 0:
        raw[-1] = 0.1
    return raw


def _scale(weights: list[float], duration: int) -> list[float]:
    total = sum(weights) or 1
    scaled = [round(duration * weight / total, 1) for weight in weights]
    scaled[-1] = round(duration - sum(scaled[:-1]), 1)
    if scaled[-1] <= 0:
        scaled[-1] = 0.1
    return scaled


def _parse_shots(text: str) -> list[dict]:
    raw = (text or "").strip()
    if "```" in raw:
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.split("```", 1)[0]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("分镜提示词没有按镜返回")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("分镜提示词没有按镜返回") from exc
    shots = data.get("shots") if isinstance(data, dict) else None
    if not isinstance(shots, list) or not shots:
        raise ValueError("分镜提示词没有按镜返回")
    return [item for item in shots if isinstance(item, dict)]


def _candidate_paths(product_id: str, inputs: dict[str, dict | None]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    with connect() as db:
        if inputs.get("product"):
            rows = db.execute(
                "SELECT id, path, kind FROM assets WHERE product_id = ? AND kind IN ('white', 'detail', 'usage') ORDER BY created_at, id",
                (product_id,),
            )
            for row in rows:
                path = Path(row["path"] or "")
                if path.is_file():
                    paths[row["id"]] = path
        person = inputs.get("person")
        if person:
            asset_id = person.get("id") or ""
            row = db.execute(
                "SELECT id, path FROM assets WHERE id = ? AND product_id = ? AND kind = 'person'",
                (asset_id, product_id),
            ).fetchone()
            if row and Path(row["path"] or "").is_file():
                paths[row["id"]] = Path(row["path"])
        scene = inputs.get("scene")
        if scene:
            scene_id = (scene.get("data") or {}).get("sceneId") or ""
            row = db.execute("SELECT id, image_path FROM scenes WHERE id = ?", (scene_id,)).fetchone()
            if row and Path(row["image_path"] or "").is_file():
                paths[f"scene:{row['id']}"] = Path(row["image_path"])
    return paths


def _selected_paths(product_id: str, inputs: dict, selected_ids: list[str]) -> list[Path]:
    catalog = _candidate_paths(product_id, inputs)
    chosen = []
    for item in selected_ids:
        path = catalog.get(item)
        if path is None:
            raise ValueError("参考图不存在")
        chosen.append(path)
    if len(chosen) > _REF_CAP:
        raise ValueError("勾选的参考图太多，一次最多 16 张")
    return chosen


def _base_data(node: dict, duration: int, shot_count: int, selected_ids: list[str] | None) -> dict:
    data = dict(node.get("data") or {})
    data["label"] = "分镜头生成"
    data["duration"] = duration
    data["shotCount"] = shot_count
    if selected_ids is not None:
        data["selectedIds"] = selected_ids
    return data


def _product_text(product_id: str) -> str:
    with connect() as db:
        product = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
    if not product:
        raise LookupError("商品不存在")
    return product_info_text(product)


def draft_storyboard(product_id: str, node_id: str, duration_value, count_value, selected_ids: list[str] | None) -> dict:
    doc = read_graph(product_id)
    node = _node(doc, node_id)
    inputs = _inputs(doc, node_id)
    if not any(inputs.values()):
        raise ValueError("先接上素材")
    stored = node.get("data") or {}
    duration = _duration(duration_value if duration_value is not None else stored.get("duration"))
    shot_count = _count(count_value if count_value is not None else stored.get("shotCount"))
    viral = _viral_text(inputs.get("viral"))
    locked = not viral
    chosen = list(selected_ids) if selected_ids is not None else list(stored.get("selectedIds") or [])
    catalog = _candidate_paths(product_id, inputs)
    chosen = [item for item in chosen if item in catalog]
    if selected_ids is None and not stored.get("selectedIds"):
        chosen = list(catalog)
    person_on = any(item == (inputs.get("person") or {}).get("id") for item in chosen)
    scene_on = any(item.startswith("scene:") for item in chosen)
    profile = _product_text(product_id)
    count_line = (
        f"没有可用的爆款分镜。请写成 {shot_count} 镜，总时长 {duration} 秒。"
        if locked
        else f"下面是爆款的分镜和运镜。镜数跟这段里的镜头数一致，不要合并，也不要加镜头。只采用景别、运镜和动作，不要人脸、声音、音乐和卖点。总时长 {duration} 秒，按原来各镜的长短比例分配。\n{viral}"
    )
    person_line = "模特已勾选，人物外观锁参考图。" if person_on else "没有勾选模特。不要出现可识别的代言人，手可以入画。"
    scene_line = "场景已勾选，背景锁参考图。" if scene_on else "没有勾选场景。背景保持简单，不要编造具体地点。"
    asked = chat(
        [
            {
                "role": "system",
                "content": "你写电商分镜的生图提示词。只输出 JSON，不要解释。格式是 {\"shots\":[{\"seconds\":3,\"prompt\":\"中文\"}]}。",
            },
            {
                "role": "user",
                "content": (
                    f"产品信息，不要编造里面没有的尺寸、材质、功效和价格：\n{profile or '没有产品信息'}\n\n"
                    f"{count_line}\n{person_line}\n{scene_line}\n\n"
                    "每一镜的 prompt 用中文，描述这一格里的画面，不要写成单独出图。"
                    "写清商品外观、人物、场景、景别、运镜、这一镜的秒数，以及和上一镜怎么接。"
                    "这些分镜最后会排在同一张图上。参考图只锁商品、人物和场景。"
                ),
            },
        ],
        timeout=120,
    )
    parsed = _parse_shots(asked)
    if locked:
        parsed = parsed[:shot_count]
        while len(parsed) < shot_count:
            parsed.append({"prompt": ""})
        seconds = _even(duration, shot_count)
    else:
        parsed = parsed[:12]
        weights = [max(float(item.get("seconds") or 0), 0.1) for item in parsed]
        seconds = _scale(weights, duration)
    shots = []
    for index, item in enumerate(parsed, start=1):
        shots.append({
            "ordinal": index,
            "seconds": seconds[index - 1],
            "prompt": str(item.get("prompt") or "").strip(),
            "image": "",
            "assetId": "",
            "error": "",
        })
    data = _base_data(node, duration, len(shots) if not locked else shot_count, chosen)
    data["shots"] = shots
    data["image"] = ""
    data["assetId"] = ""
    data["error"] = ""
    data["videoPrompt"] = ""
    data["videoError"] = ""
    data["clip"] = ""
    data["clipStatus"] = ""
    data["clipError"] = ""
    data["clipModel"] = ""
    return write_node_data(product_id, node_id, data)


def save_storyboard(product_id: str, node_id: str, duration_value, count_value, selected_ids, shots) -> dict:
    doc = read_graph(product_id)
    node = _node(doc, node_id)
    data = dict(node.get("data") or {})
    data["label"] = "分镜头生成"
    if duration_value is not None:
        data["duration"] = _duration(duration_value)
    if count_value is not None:
        data["shotCount"] = _count(count_value)
    if selected_ids is not None:
        data["selectedIds"] = [str(item) for item in selected_ids if str(item).strip()]
    if shots is not None:
        previous = {int(item.get("ordinal") or 0): item for item in data.get("shots") or [] if isinstance(item, dict)}
        cleaned = []
        for index, item in enumerate(shots, start=1):
            if not isinstance(item, dict):
                continue
            ordinal = int(item.get("ordinal") or index)
            old = previous.get(ordinal) or {}
            cleaned.append({
                "ordinal": ordinal,
                "seconds": float(item.get("seconds") or old.get("seconds") or 0),
                "prompt": str(item.get("prompt") if item.get("prompt") is not None else old.get("prompt") or "").strip(),
                "image": str(item.get("image") or old.get("image") or ""),
                "assetId": str(item.get("assetId") or old.get("assetId") or ""),
                "error": str(item.get("error") or ""),
            })
        data["shots"] = cleaned
    return write_node_data(product_id, node_id, data)


def _drop_replaced(product_id: str, node_id: str, keep: set[str]) -> None:
    with connect() as db:
        rows = [
            row_dict(row)
            for row in db.execute("SELECT id, path, meta_json FROM assets WHERE product_id = ? AND kind = 'storyboard'", (product_id,))
        ]
        for row in rows:
            meta = loads(row.get("meta_json") or "{}", {})
            if not isinstance(meta, dict) or meta.get("node_id") != node_id or row["id"] in keep:
                continue
            db.execute("DELETE FROM assets WHERE id = ?", (row["id"],))
            path = Path(row.get("path") or "")
            if path.is_file():
                path.unlink(missing_ok=True)


def _save_frame(product_id: str, node_id: str, payload: bytes, prompt: str) -> tuple[str, str]:
    if len(payload) < 1000:
        raise RuntimeError("分镜图是空的")
    root = FILES / "storyboards" / product_id
    root.mkdir(parents=True, exist_ok=True)
    asset_id = new_id("asset")
    dest = root / f"{asset_id}.png"
    dest.write_bytes(payload)
    with connect() as db:
        db.execute(
            "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, 'storyboard', ?, ?, ?, ?)",
            (asset_id, product_id, "分镜图", str(dest), json.dumps({"node_id": node_id, "prompt": prompt}, ensure_ascii=False), now()),
        )
    return asset_id, str(dest)


def _sheet_prompt(shots: list[dict]) -> str:
    lines = [
        "只输出一张 1:1 的图。把下面每一个分镜各放一格，按镜序从左到右、从上到下排列。",
        "格子数量等于分镜数量，不要凑成固定九宫格，也不要多出空格。",
        "格子之间和画面边缘留干净白边。每一格只放这一镜的画面。",
        "不要在画面里写字、标题、镜号、秒数、价格或水印。",
        "所有格子里是同一个商品。勾了模特或场景时，人物和背景也保持一致。",
        "参考图只用来锁商品、人物和场景。",
    ]
    for shot in shots:
        lines.append(f"第 {shot['ordinal']} 镜，{shot['seconds']} 秒：{shot['prompt']}")
    return "\n".join(lines)


def _video_prompt(profile: str, shots: list[dict], sheet: str) -> str:
    content: list[dict] = [{
        "type": "text",
        "text": (
            "下面这张图里有全部分镜。根据这张图和每一镜的提示词，写一条整片视频提示词。"
            "按镜序写切换，锁住同一个商品、同一个人、同一个场景。"
            "不要写字幕，不要解释，用中文。\n"
            f"产品信息：{profile or '没有产品信息'}"
        ),
    }]
    sheet_path = Path(sheet)
    if sheet_path.is_file():
        part = _image_part(sheet_path)
        if part:
            content.append(part)
    for shot in shots:
        content.append({
            "type": "text",
            "text": f"第 {shot['ordinal']} 镜，{shot['seconds']} 秒。分镜头提示词：{shot['prompt']}",
        })
    return chat(
        [
            {"role": "system", "content": "你写整片带货视频的提示词。按镜序写切换。不要写字幕。只输出提示词。"},
            {"role": "user", "content": content},
        ],
        timeout=120,
    ).strip()


def generate_storyboard(product_id: str, node_id: str, duration_value, count_value, selected_ids, shots) -> dict:
    doc = read_graph(product_id)
    node = _node(doc, node_id)
    inputs = _inputs(doc, node_id)
    if not any(inputs.values()):
        raise ValueError("先接上素材")
    incoming = shots if shots is not None else (node.get("data") or {}).get("shots") or []
    if not incoming:
        raise ValueError("先整理分镜")
    cleaned = []
    for index, item in enumerate(incoming, start=1):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("每一镜都要有提示词")
        cleaned.append({
            "ordinal": int(item.get("ordinal") or index),
            "seconds": float(item.get("seconds") or 0),
            "prompt": prompt,
        })
    if not cleaned:
        raise ValueError("先整理分镜")
    if len(cleaned) > 12:
        raise ValueError("一次最多 12 镜")
    stored = node.get("data") or {}
    duration = _duration(duration_value if duration_value is not None else stored.get("duration"))
    weights = [max(shot["seconds"], 0.1) for shot in cleaned]
    seconds = _scale(weights, duration)
    for shot, value in zip(cleaned, seconds):
        shot["seconds"] = value
    chosen = [str(item) for item in (selected_ids if selected_ids is not None else stored.get("selectedIds") or [])]
    paths = _selected_paths(product_id, inputs, chosen) if chosen else []
    image = str(stored.get("image") or "")
    asset_id = str(stored.get("assetId") or "")
    error = ""
    video = str(stored.get("videoPrompt") or "")
    video_error = str(stored.get("videoError") or "")
    sheet_path = ""
    if paths:
        prompt = _sheet_prompt(cleaned)
        try:
            payload = fetch_relay_image(prompt, paths, failure="分镜图生成失败", empty="至少留一张参考图")
            asset_id, sheet_path = _save_frame(product_id, node_id, payload, prompt)
            image = file_url(sheet_path)
            error = ""
            video = ""
            video_error = ""
            stored["clip"] = ""
            stored["clipStatus"] = ""
            stored["clipError"] = ""
            stored["clipModel"] = ""
            _drop_replaced(product_id, node_id, {asset_id})
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
            _drop_replaced(product_id, node_id, {asset_id} if asset_id else set())
    else:
        video = ""
        video_error = ""
    if sheet_path:
        try:
            video = _video_prompt(_product_text(product_id), cleaned, sheet_path)
        except RuntimeError as exc:
            video_error = str(exc)
    data = _base_data(node, duration, _count(count_value if count_value is not None else stored.get("shotCount")), chosen)
    data["image"] = image
    data["assetId"] = asset_id
    data["error"] = error
    data["shotCount"] = len(cleaned) if _viral_text(inputs.get("viral")) else data["shotCount"]
    data["shots"] = cleaned
    data["videoPrompt"] = video
    data["videoError"] = video_error
    data["clip"] = str(stored.get("clip") or "")
    data["clipStatus"] = str(stored.get("clipStatus") or "")
    data["clipError"] = str(stored.get("clipError") or "")
    data["clipModel"] = str(stored.get("clipModel") or "")
    return write_node_data(product_id, node_id, data)


def _merge_story(product_id: str, node_id: str, patch: dict) -> dict:
    doc = read_graph(product_id)
    node = _node(doc, node_id)
    data = dict(node.get("data") or {})
    data.update(patch)
    return write_node_data(product_id, node_id, data)


def _sheet_file(product_id: str, asset_id: str) -> Path:
    with connect() as db:
        row = db.execute(
            "SELECT path FROM assets WHERE id = ? AND product_id = ? AND kind = 'storyboard'",
            (asset_id, product_id),
        ).fetchone()
    path = Path(row["path"]) if row and row["path"] else Path()
    if not path.is_file():
        raise ValueError("先生成分镜图")
    return path


def _film_prompt(shots: list[dict], video_prompt: str) -> str:
    lines = [
        "生成一条竖屏 9:16 带货视频。",
        "参考图按顺序看：前面的图锁商品、人物和场景，最后一张是分镜图。",
        "按分镜图从左到右、从上到下的格子顺序拍摄，格子之间的切换跟每一镜的提示词一致。",
        "不要字幕、价格和水印，不要配旁白和音乐。",
    ]
    if video_prompt.strip():
        lines.append("整片提示词：\n" + video_prompt.strip())
    lines.append("每一镜：")
    for shot in shots:
        lines.append(f"第 {shot['ordinal']} 镜，{shot['seconds']} 秒：{shot['prompt']}")
    return "\n".join(lines)


def _finish_clip(product_id: str, node_id: str, prompt: str, images: list[Path], seconds: int) -> None:
    from server.seedance import render_fast_clip

    dest = FILES / "storyboards" / product_id / f"{node_id}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{node_id}.part.mp4")
    part.unlink(missing_ok=True)
    try:
        render_fast_clip(prompt, images, seconds, part)
        part.replace(dest)
    except Exception as exc:
        part.unlink(missing_ok=True)
        message = str(exc).strip() or "视频没有生成"
        _merge_story(product_id, node_id, {"clipStatus": "failed", "clipError": message})
        return
    _merge_story(
        product_id,
        node_id,
        {"clip": file_url(str(dest)), "clipStatus": "done", "clipError": "", "clipModel": _FAST_MODEL},
    )


def storyboard_video_plan(product_id: str, node_id: str, duration_value, selected_ids, shots) -> tuple[str, list[Path], int]:
    doc = read_graph(product_id)
    node = _node(doc, node_id)
    stored = node.get("data") or {}
    if stored.get("clipStatus") == "running":
        raise ValueError("正在生成视频")
    if not stored.get("image") or not stored.get("assetId"):
        raise ValueError("先生成分镜图")
    incoming = shots if shots is not None else stored.get("shots") or []
    cleaned = []
    for index, item in enumerate(incoming, start=1):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("每一镜都要有提示词")
        cleaned.append({
            "ordinal": int(item.get("ordinal") or index),
            "seconds": float(item.get("seconds") or 0),
            "prompt": prompt,
        })
    if not cleaned:
        raise ValueError("先整理分镜")
    seconds = _duration(duration_value if duration_value is not None else stored.get("duration"))
    if seconds < 4 or seconds > 15:
        raise ValueError("Seedance 2.0 Fast 一次只支持 4 到 15 秒。请把节点上的时长改到这个范围，再生成视频。")
    inputs = _inputs(doc, node_id)
    chosen = [str(item) for item in (selected_ids if selected_ids is not None else stored.get("selectedIds") or [])]
    paths = _selected_paths(product_id, inputs, chosen) if chosen else []
    if len(paths) > _FAST_REFS:
        raise ValueError("勾选的参考图太多，便宜模型一次最多 8 张，分镜图会另外带上")
    sheet = _sheet_file(product_id, str(stored.get("assetId") or ""))
    images = [path for path in paths if path.resolve() != sheet.resolve()]
    images.append(sheet)
    prompt = _film_prompt(cleaned, str(stored.get("videoPrompt") or ""))
    return prompt, images, seconds


def start_storyboard_video(product_id: str, node_id: str, duration_value, selected_ids, shots) -> dict:
    prompt, images, seconds = storyboard_video_plan(product_id, node_id, duration_value, selected_ids, shots)
    board = _merge_story(product_id, node_id, {"clipStatus": "running", "clipError": "", "clipModel": _FAST_MODEL})
    threading.Thread(
        target=_finish_clip,
        args=(product_id, node_id, prompt, images, seconds),
        name=f"story-video-{node_id}",
        daemon=True,
    ).start()
    return board
