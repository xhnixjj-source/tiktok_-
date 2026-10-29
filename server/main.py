from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.board import _delete_variants, add_graph_node, add_step, attach_scene_graph, board_payload, drop_step, place_person_node, prepare_variant_graph, remove_node, save_board, update_graph_node, variant_selection
from server.bobtoken import _REJECTED, _image_part, configured, review_selling_prompt, translate_spoken, voice_enabled
from server.checks import DURATIONS, all_passed, allowed_duration, build_prompt, evaluate, list_white_assets, load_variant_bundle, rules_path, save_check
from server.db import FILES, connect, dumps, file_url, init_db, new_id, now, row_dict
from server.media import is_banner
from server.jobs import dismiss_job, fail_interrupted, list_jobs, submit_job
from server.storyboard import draft_storyboard, save_storyboard, storyboard_video_plan
from server.usage import prepare_usage_prompt
from server.worker import kick_generation, release_held, start_worker

app = FastAPI(title="带货视频")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5174", "http://127.0.0.1:5174"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup() -> None:
    init_db()
    fail_interrupted()
    FILES.mkdir(parents=True, exist_ok=True)
    app.mount("/files", StaticFiles(directory=FILES), name="files")
    start_worker()


class ProductIn(BaseModel):
    url: str = ""


class WhiteGenerate(BaseModel):
    prompt: str = ""
    asset_ids: list[str] = []


class ProductPatch(BaseModel):
    title: str | None = None
    price: str | None = None
    points: str | None = None


class ScenePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    active: bool | None = None


class BoardIn(BaseModel):
    nodes: list[dict]
    edges: list[dict]


class SceneAttach(BaseModel):
    scene_id: str


class StepIn(BaseModel):
    step: str


class NodeIn(BaseModel):
    type: str
    source: str
    text: str = ""


class NodePatch(BaseModel):
    text: str | None = None
    scene_id: str | None = None
    hit_id: str | None = None


class ShotsIn(BaseModel):
    shots: list[dict]


class SteerIn(BaseModel):
    steer: str = ""
    voiceover: bool = True


class DurationIn(BaseModel):
    duration_sec: int


class SpeechTags(BaseModel):
    tags: list[str] = []


class TemplatePick(BaseModel):
    template_id: str


class StoryboardIn(BaseModel):
    duration: int | None = None
    shot_count: int | None = None
    selected_ids: list[str] | None = None
    shots: list[dict] | None = None


def _scene_out(row: dict) -> dict:
    return {**row, "image": file_url(row["image_path"]), "active": bool(row["active"])}


@app.get("/scenes")
def list_scenes(include_inactive: int = 0):
    with connect() as db:
        sql = "SELECT * FROM scenes ORDER BY created_at DESC" if include_inactive else "SELECT * FROM scenes WHERE active = 1 ORDER BY created_at DESC"
        rows = [row_dict(row) for row in db.execute(sql)]
    return [_scene_out(row) for row in rows]


@app.post("/scenes")
async def create_scene(name: str = Form(...), description: str = Form(...), image: UploadFile = File(...)):
    description = description.strip()
    name = name.strip()
    if not name or not description or not image.filename:
        raise HTTPException(400, "场景需要名称、图片和描述词")
    scene_id = new_id("scene")
    dest = FILES / "scenes" / f"{scene_id}{Path(image.filename).suffix or '.img'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await image.read())
    if dest.stat().st_size < 32:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "图片是空的")
    with connect() as db:
        db.execute(
            "INSERT INTO scenes (id, name, image_path, description, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (scene_id, name, str(dest), description, now()),
        )
        row = row_dict(db.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone())
    return _scene_out(row)


@app.patch("/scenes/{scene_id}")
async def update_scene(scene_id: str, name: str = Form(None), description: str = Form(None), active: int | None = Form(None), image: UploadFile | None = File(None)):
    with connect() as db:
        current = row_dict(db.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone())
    if not current:
        raise HTTPException(404, "场景不存在")
    next_name = current["name"] if name is None else name.strip()
    next_description = current["description"] if description is None else description.strip()
    next_active = current["active"] if active is None else int(active)
    if not next_name or not next_description:
        raise HTTPException(400, "场景需要名称、图片和描述词")
    image_path = current["image_path"]
    if image and image.filename:
        dest = FILES / "scenes" / f"{scene_id}{Path(image.filename).suffix or '.img'}"
        dest.write_bytes(await image.read())
        image_path = str(dest)
    with connect() as db:
        db.execute(
            "UPDATE scenes SET name = ?, description = ?, active = ?, image_path = ? WHERE id = ?",
            (next_name, next_description, next_active, image_path, scene_id),
        )
        row = row_dict(db.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone())
    return _scene_out(row)


@app.post("/products")
def create_product(body: ProductIn):
    url = body.url.strip()
    if url:
        with connect() as db:
            rows = [row_dict(row) for row in db.execute("SELECT id, status, title, source_url FROM products")]
        matches = [row for row in rows if (row.get("source_url") or "").strip().rstrip("/") == url.rstrip("/")]
        if matches:
            chosen = next((row for row in matches if (row.get("title") or "") not in ("", "Security Check")), matches[0])
            return {"id": chosen["id"], "status": chosen["status"]}
    product_id = new_id("prod")
    status = "collecting" if url else "needs_input"
    with connect() as db:
        db.execute(
            "INSERT INTO products (id, source_url, status, created_at) VALUES (?, ?, ?, ?)",
            (product_id, url, status, now()),
        )
    return {"id": product_id, "status": status}


@app.delete("/products/{product_id}")
def delete_product(product_id: str):
    _delete_product(product_id)
    return {"ok": True}


@app.post("/products/{product_id}/refresh")
def refresh_product(product_id: str, body: ProductIn):
    return _queue_product_refresh(product_id, body.url)


class MaterialExport(BaseModel):
    ids: list[str] = []


def _safe_folder_name(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80].strip(" .")


def _zip_folder_name(title: str, product_id: str, used: set[str]) -> str:
    base = _safe_folder_name(title) or product_id
    name = base
    if name in used:
        name = f"{base}-{product_id[-6:]}"
    while name in used:
        name = f"{name}-{product_id[-6:]}"
    used.add(name)
    return name


def _named_image_bytes(rows: list[dict]) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for row in rows:
        source = Path(row.get("path") or "")
        if not source.is_file():
            continue
        suffix = source.suffix.lower() if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} else ".jpg"
        files.append((f"{len(files) + 1}{suffix}", source.read_bytes()))
    return files


def _product_markdown(product: dict, specs: list[dict]) -> str:
    title = (product.get("title") or "").strip() or "未命名商品"
    lines = [f"<!-- product: {product['id']} -->", f"# {title}", ""]

    def add(label: str, value: str) -> None:
        text = (value or "").strip()
        if not text:
            return
        lines.extend([f"## {label}", "", text, ""])

    add("价格", product.get("price") or "")
    add("商品链接", product.get("source_url") or "")
    add("标准产品信息", product.get("profile_text") or "")
    add("卖点", product.get("points") or "")
    for block in specs:
        if block["label"] in {"中文品名", "核心卖点"}:
            continue
        add(block["label"], block["value"])
    add("页面原文", product.get("page_text") or "")
    if len(lines) == 3:
        lines.extend(["系统里还没有整理好的产品信息。", ""])
    return "\n".join(lines).rstrip() + "\n"


def _zip_write(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name)
    info.flag_bits |= 0x800
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, payload)


def _export_one_material(product_id: str, archive: zipfile.ZipFile, used: set[str]) -> str:
    from server.catalog import spec_blocks
    from server.media import is_banner

    with connect() as db:
        product = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
        assets = [row_dict(row) for row in db.execute("SELECT kind, path FROM assets WHERE product_id = ? ORDER BY created_at", (product_id,))]
    if not product:
        raise HTTPException(404, "商品不存在")
    photos = [item for item in assets if item["kind"] == "product" and item.get("path") and not is_banner(item["path"])]
    whites = [item for item in assets if item["kind"] == "white" and item.get("path")]
    details = [item for item in assets if item["kind"] == "detail" and item.get("path")]
    usages = [item for item in assets if item["kind"] == "usage" and item.get("path")]
    folder = "TikTok商品素材/" + _zip_folder_name(product.get("title") or "", product_id, used)
    for dirname, rows in (("商品图片", photos), ("白底图", whites), ("细节图", details), ("使用场景图", usages)):
        _zip_write(archive, f"{folder}/{dirname}/", b"")
        for filename, payload in _named_image_bytes(rows):
            _zip_write(archive, f"{folder}/{dirname}/{filename}", payload)
    _zip_write(archive, f"{folder}/产品信息.MD", _product_markdown(product, spec_blocks(product_id)).encode("utf-8"))
    return folder


@app.post("/products/export-materials")
def export_materials(body: MaterialExport):
    ids = []
    for item in body.ids:
        product_id = (item or "").strip()
        if product_id and product_id not in ids:
            ids.append(product_id)
    if not ids:
        raise HTTPException(400, "先勾选商品")
    buffer = io.BytesIO()
    used: set[str] = set()
    try:
        with zipfile.ZipFile(buffer, "w") as archive:
            for product_id in ids:
                _export_one_material(product_id, archive, used)
    except OSError as exc:
        raise HTTPException(400, f"导出失败：{exc.strerror or exc}") from exc
    filename = quote("TikTok商品素材.zip")
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"materials.zip\"; filename*=UTF-8''{filename}",
            "X-Export-Count": str(len(ids)),
        },
    )


@app.get("/products")
def list_products():
    with connect() as db:
        rows = [row_dict(row) for row in db.execute("SELECT * FROM products ORDER BY created_at DESC")]
        for row in rows:
            covers = db.execute(
                "SELECT path FROM assets WHERE product_id = ? AND kind = 'product' ORDER BY created_at",
                (row["id"],),
            ).fetchall()
            chosen = next((item["path"] for item in covers if item["path"] and not is_banner(item["path"])), "")
            if not chosen and covers:
                chosen = covers[0]["path"]
            row["cover"] = file_url(chosen)
    return rows


@app.get("/products/{product_id}")
def get_product(product_id: str):
    with connect() as db:
        product = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
        assets = [row_dict(row) for row in db.execute("SELECT * FROM assets WHERE product_id = ?", (product_id,))]
    if not product:
        raise HTTPException(404, "商品不存在")
    for asset in assets:
        asset["image"] = file_url(asset["path"])
        asset["meta"] = json.loads(asset["meta_json"] or "{}")
    from server.catalog import spec_blocks

    return {**product, "assets": assets, "specs": spec_blocks(product_id)}


@app.patch("/products/{product_id}")
def patch_product(product_id: str, body: ProductPatch):
    with connect() as db:
        current = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
        if not current:
            raise HTTPException(404, "商品不存在")
        title = current["title"] if body.title is None else body.title
        price = current["price"] if body.price is None else body.price
        points = current["points"] if body.points is None else body.points
        db.execute("UPDATE products SET title = ?, price = ?, points = ?, status = 'ready' WHERE id = ?", (title, price, points, product_id))
    return get_product(product_id)


@app.post("/products/{product_id}/images")
async def upload_product_image(product_id: str, image: UploadFile = File(...), label: str = Form("产品图")):
    return await _upload_asset(product_id, "product", label, image)


@app.post("/products/{product_id}/white-images")
async def upload_white_images(product_id: str, images: list[UploadFile] = File(...)):
    _require_product(product_id)
    payloads: list[tuple[str, bytes]] = []
    for image in images:
        if not image.filename:
            continue
        payload = await image.read()
        if len(payload) < 32:
            raise HTTPException(400, "图片是空的")
        payloads.append((image.filename, payload))
    if not payloads:
        raise HTTPException(400, "需要图片")
    root = FILES / "whites" / product_id
    root.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        for filename, payload in payloads:
            asset_id = new_id("asset")
            dest = root / f"{asset_id}{Path(filename).suffix or '.img'}"
            dest.write_bytes(payload)
            db.execute(
                "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, 'white', ?, ?, '{}', ?)",
                (asset_id, product_id, Path(filename).stem[:40] or "白底图", str(dest), now()),
            )
    return board_payload(product_id)


def _queue_image(product_id: str, kind: str, label: str, body: WhiteGenerate):
    try:
        submit_job(
            product_id,
            kind,
            label,
            {"prompt": body.prompt, "asset_ids": [item for item in body.asset_ids if (item or "").strip()]},
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return board_payload(product_id)


@app.get("/jobs")
def get_jobs(product_id: str = ""):
    return list_jobs(product_id)


@app.post("/jobs/{job_id}/dismiss")
def post_dismiss_job(job_id: str):
    try:
        dismiss_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/products/{product_id}/white-images/generate")
def create_generated_white(product_id: str, body: WhiteGenerate):
    _require_product(product_id)
    return _queue_image(product_id, "white", "白底图", body)


@app.delete("/products/{product_id}/white-images/{asset_id}")
def delete_white_image(product_id: str, asset_id: str):
    _require_product(product_id)
    with connect() as db:
        row = row_dict(db.execute("SELECT id, path, kind FROM assets WHERE id = ? AND product_id = ?", (asset_id, product_id)).fetchone())
        if not row or row["kind"] != "white":
            raise HTTPException(404, "白底图不存在")
        db.execute("DELETE FROM variant_assets WHERE asset_id = ?", (asset_id,))
        db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    _unlink_owned([row["path"]], FILES / "whites")
    return board_payload(product_id)


@app.post("/products/{product_id}/detail-images/generate")
def create_generated_detail(product_id: str, body: WhiteGenerate):
    _require_product(product_id)
    return _queue_image(product_id, "detail", "细节图", body)


@app.post("/products/{product_id}/usage-images/prompt")
def usage_prompt(product_id: str):
    _require_product(product_id)
    try:
        prompt = prepare_usage_prompt(product_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"prompt": prompt}


@app.post("/products/{product_id}/usage-images")
def create_usage_image(product_id: str, body: WhiteGenerate):
    _require_product(product_id)
    return _queue_image(product_id, "usage", "使用场景图", body)


@app.delete("/products/{product_id}/usage-images/{asset_id}")
def delete_usage_image(product_id: str, asset_id: str):
    _require_product(product_id)
    with connect() as db:
        row = row_dict(db.execute("SELECT id, path, kind FROM assets WHERE id = ? AND product_id = ?", (asset_id, product_id)).fetchone())
        if not row or row["kind"] != "usage":
            raise HTTPException(404, "使用场景图不存在")
        db.execute("DELETE FROM variant_assets WHERE asset_id = ?", (asset_id,))
        db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    _unlink_owned([row["path"]], FILES / "usages")
    return board_payload(product_id)


@app.post("/products/{product_id}/people")
async def upload_person(product_id: str, image: UploadFile = File(...), label: str = Form("出镜人")):
    return await _upload_asset(product_id, "person", label, image)


class PersonAttach(BaseModel):
    person_id: str
    node_id: str = ""


def _person_out(row: dict, images: list[str] | None = None) -> dict:
    photos = images if images is not None else [file_url(row["image_path"])]
    return {**row, "image": file_url(row["image_path"]), "images": photos, "active": bool(row["active"])}


def _people_rows(include_inactive: int) -> list[dict]:
    with connect() as db:
        sql = "SELECT * FROM people ORDER BY created_at DESC" if include_inactive else "SELECT * FROM people WHERE active = 1 ORDER BY created_at DESC"
        rows = [row_dict(row) for row in db.execute(sql)]
        grouped: dict[str, list[str]] = {}
        if rows:
            marks = ",".join("?" for _ in rows)
            for photo in db.execute(
                f"SELECT person_id, path FROM person_photos WHERE person_id IN ({marks}) ORDER BY ordinal",
                [row["id"] for row in rows],
            ):
                grouped.setdefault(photo["person_id"], []).append(file_url(photo["path"]))
    return [_person_out(row, grouped.get(row["id"]) or [file_url(row["image_path"])]) for row in rows]


@app.get("/people")
def list_people(include_inactive: int = 0):
    return _people_rows(include_inactive)


@app.post("/people")
async def create_person(name: str = Form(...), image: UploadFile = File(...), note: str = Form("")):
    name = name.strip()
    if not name or not image.filename:
        raise HTTPException(400, "人物需要名字和图片")
    person_id = new_id("person")
    dest = FILES / "people" / f"{person_id}{Path(image.filename).suffix or '.img'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await image.read())
    if dest.stat().st_size < 32:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "图片是空的")
    with connect() as db:
        db.execute(
            "INSERT INTO people (id, name, image_path, note, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (person_id, name, str(dest), note.strip(), now()),
        )
        db.execute(
            "INSERT INTO person_photos (id, person_id, path, ordinal, created_at) VALUES (?, ?, ?, 1, ?)",
            (new_id("photo"), person_id, str(dest), now()),
        )
        row = row_dict(db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone())
    return _person_out(row, [file_url(str(dest))])


@app.patch("/people/{person_id}")
async def update_person(person_id: str, name: str = Form(None), note: str = Form(None), active: int | None = Form(None), image: UploadFile | None = File(None)):
    with connect() as db:
        current = row_dict(db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone())
    if not current:
        raise HTTPException(404, "人物不存在")
    next_name = current["name"] if name is None else name.strip()
    next_note = current["note"] if note is None else note.strip()
    next_active = current["active"] if active is None else int(active)
    if not next_name:
        raise HTTPException(400, "人物需要名字")
    image_path = current["image_path"]
    if image and image.filename:
        dest = FILES / "people" / f"{person_id}{Path(image.filename).suffix or '.img'}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(await image.read())
        image_path = str(dest)
    with connect() as db:
        db.execute(
            "UPDATE people SET name = ?, note = ?, active = ?, image_path = ? WHERE id = ?",
            (next_name, next_note, next_active, image_path, person_id),
        )
        row = row_dict(db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone())
    return _person_out(row)


@app.post("/products/{product_id}/cast")
def attach_person(product_id: str, body: PersonAttach):
    _require_product(product_id)
    with connect() as db:
        person = row_dict(db.execute("SELECT * FROM people WHERE id = ? AND active = 1", (body.person_id,)).fetchone())
        if not person:
            raise HTTPException(400, "人物素材不存在")
        if not body.node_id:
            db.execute("DELETE FROM variant_assets WHERE asset_id IN (SELECT id FROM assets WHERE product_id = ? AND kind = 'person')", (product_id,))
            db.execute("DELETE FROM assets WHERE product_id = ? AND kind = 'person'", (product_id,))
        asset_id = new_id("asset")
        photos = [
            photo["path"]
            for photo in db.execute("SELECT path FROM person_photos WHERE person_id = ? ORDER BY ordinal", (person["id"],))
        ]
        references = [path for path in photos if path and path != person["image_path"]]
        db.execute(
            "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, 'person', ?, ?, ?, ?)",
            (asset_id, product_id, person["name"], person["image_path"], dumps({"person_id": body.person_id, "references": references}), now()),
        )
        asset = row_dict(db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone())
        if not body.node_id:
            for row in db.execute("SELECT id FROM variants WHERE product_id = ?", (product_id,)):
                db.execute("INSERT OR IGNORE INTO variant_assets (variant_id, asset_id) VALUES (?, ?)", (row["id"], asset_id))
    place_person_node(product_id, asset, body.node_id)
    return board_payload(product_id)


@app.delete("/products/{product_id}/cast")
def clear_cast(product_id: str):
    _require_product(product_id)
    with connect() as db:
        db.execute("DELETE FROM variant_assets WHERE asset_id IN (SELECT id FROM assets WHERE product_id = ? AND kind = 'person')", (product_id,))
        db.execute("DELETE FROM assets WHERE product_id = ? AND kind = 'person'", (product_id,))
    drop_step(product_id, "model")
    return board_payload(product_id)


@app.post("/products/{product_id}/reference")
async def add_reference(product_id: str, url: str = Form(""), video: UploadFile | None = File(None)):
    _require_product(product_id)
    asset_id = new_id("asset")
    dest = FILES / "references" / asset_id / "source.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    source_url = url.strip()
    if video and video.filename:
        dest.write_bytes(await video.read())
        source_url = ""
    elif not source_url:
        raise HTTPException(400, "需要视频链接或文件")
    meta = {"pending": True, "source_url": source_url, "detail": "正在准备抽帧和口播"}
    with connect() as db:
        db.execute(
            "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, 'reference', ?, ?, ?, ?)",
            (asset_id, product_id, "参考视频", str(dest if dest.exists() else ""), dumps(meta), now()),
        )
    return {"id": asset_id}


@app.get("/boards/{product_id}")
def get_board(product_id: str):
    try:
        return board_payload(product_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.put("/boards/{product_id}")
def put_board(product_id: str, body: BoardIn):
    _require_product(product_id)
    return save_board(product_id, body.nodes, body.edges)


@app.post("/boards/{product_id}/nodes")
def post_node(product_id: str, body: NodeIn):
    _require_product(product_id)
    try:
        return add_graph_node(product_id, body.type, body.source, text=body.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/boards/{product_id}/nodes/{node_id:path}")
def patch_node(product_id: str, node_id: str, body: NodePatch):
    _require_product(product_id)
    try:
        return update_graph_node(product_id, node_id, body.text, body.scene_id, body.hit_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


def _storyboard_call(product_id: str, fn):
    _require_product(product_id)
    try:
        return fn()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/boards/{product_id}/nodes/{node_id}/storyboard")
def put_storyboard(product_id: str, node_id: str, body: StoryboardIn):
    return _storyboard_call(
        product_id,
        lambda: save_storyboard(product_id, node_id, body.duration, body.shot_count, body.selected_ids, body.shots),
    )


@app.post("/boards/{product_id}/nodes/{node_id}/storyboard/draft")
def post_storyboard_draft(product_id: str, node_id: str, body: StoryboardIn):
    return _storyboard_call(
        product_id,
        lambda: draft_storyboard(product_id, node_id, body.duration, body.shot_count, body.selected_ids),
    )


@app.post("/boards/{product_id}/nodes/{node_id}/storyboard/generate")
def post_storyboard_generate(product_id: str, node_id: str, body: StoryboardIn):
    def queue():
        submit_job(
            product_id,
            "storyboard",
            "分镜图",
            {
                "duration": body.duration,
                "shot_count": body.shot_count,
                "selected_ids": body.selected_ids,
                "shots": body.shots,
            },
            node_id=node_id,
        )
        return board_payload(product_id)

    return _storyboard_call(product_id, queue)


@app.post("/boards/{product_id}/nodes/{node_id}/storyboard/video")
def post_storyboard_video(product_id: str, node_id: str, body: StoryboardIn):
    def queue():
        prompt, images, seconds = storyboard_video_plan(product_id, node_id, body.duration, body.selected_ids, body.shots)
        submit_job(
            product_id,
            "storyboard_video",
            "分镜视频",
            {"prompt": prompt, "images": [str(path) for path in images], "seconds": seconds},
            node_id=node_id,
        )
        return board_payload(product_id)

    return _storyboard_call(product_id, queue)


@app.post("/boards/{product_id}/steps")
def post_step(product_id: str, body: StepIn):
    _require_product(product_id)
    try:
        return add_step(product_id, body.step)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/boards/{product_id}/nodes/{node_id:path}")
def delete_node(product_id: str, node_id: str):
    _require_product(product_id)
    try:
        return remove_node(product_id, node_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/boards/{product_id}/scenes")
def attach_scene(product_id: str, body: SceneAttach):
    _require_product(product_id)
    with connect() as db:
        scene = row_dict(db.execute("SELECT * FROM scenes WHERE id = ? AND active = 1", (body.scene_id,)).fetchone())
    if not scene:
        raise HTTPException(400, "场景不存在或已停用")
    try:
        return attach_scene_graph(product_id, body.scene_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/variants/{variant_id}")
def get_variant(variant_id: str):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    bundle["rules"] = rules_path().read_text(encoding="utf-8")
    if bundle["variant"].get("output_path"):
        bundle["variant"]["video"] = f"/variants/{variant_id}/video"
    return bundle


@app.put("/variants/{variant_id}/shots")
def put_shots(variant_id: str, body: ShotsIn):
    if not load_variant_bundle(variant_id):
        raise HTTPException(404, "版本不存在")
    with connect() as db:
        db.execute("DELETE FROM shots WHERE variant_id = ?", (variant_id,))
        for index, shot in enumerate(body.shots, start=1):
            db.execute(
                "INSERT INTO shots (id, variant_id, ordinal, t_start, t_end, action, voiceover, camera) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id("shot"), variant_id, index, float(shot.get("t_start", 0)), float(shot.get("t_end", 0)), shot.get("action") or "", shot.get("voiceover") or "", shot.get("camera") or ""),
            )
        db.execute("UPDATE variants SET status = 'draft' WHERE id = ? AND status != 'running'", (variant_id,))
    return get_variant(variant_id)


@app.patch("/variants/{variant_id}/template")
def update_template(variant_id: str, body: TemplatePick):
    if not load_variant_bundle(variant_id):
        raise HTTPException(404, "版本不存在")
    with connect() as db:
        chosen = row_dict(db.execute("SELECT id FROM prompt_templates WHERE id = ?", (body.template_id,)).fetchone())
        if not chosen:
            raise HTTPException(400, "模板不存在")
        db.execute("UPDATE variants SET template_id = ? WHERE id = ?", (chosen["id"], variant_id))
    return get_variant(variant_id)


@app.patch("/variants/{variant_id}/steer")
def update_steer(variant_id: str, body: SteerIn):
    if not load_variant_bundle(variant_id):
        raise HTTPException(404, "版本不存在")
    with connect() as db:
        db.execute(
            "UPDATE variants SET steer = ?, voiceover = ? WHERE id = ?",
            (body.steer.strip(), 1 if body.voiceover else 0, variant_id),
        )
    return get_variant(variant_id)


@app.patch("/variants/{variant_id}/duration")
def update_duration(variant_id: str, body: DurationIn):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    if bundle["variant"]["status"] in {"queued", "running"}:
        raise HTTPException(400, "这一版正在生成，时长先不要改")
    if body.duration_sec not in DURATIONS:
        raise HTTPException(400, "时长只能是 10、15、30、45 或 60 秒")
    with connect() as db:
        db.execute(
            "UPDATE variants SET duration_sec = ?, status = CASE WHEN TRIM(COALESCE(prompt_text, '')) != '' AND status = 'passed' THEN 'draft' ELSE status END WHERE id = ?",
            (body.duration_sec, variant_id),
        )
    return get_variant(variant_id)


@app.post("/variants/{variant_id}/check")
def check_variant(variant_id: str):
    copy = prepare_variant_graph(variant_id)
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    bundle["selection"] = variant_selection(variant_id)
    bundle["voice_text"] = copy.get("voice") or ""
    bundle["notes"] = copy.get("notes") or []
    shots = bundle["shots"]
    bundle["source_shots"] = [dict(shot) for shot in shots]
    keep_voice = voice_enabled(bundle)
    spoken = "\n".join(part for part in [bundle["voice_text"], *bundle["notes"]] if part)
    if keep_voice and spoken and shots:
        shots = [dict(shots[0], voiceover=((shots[0].get("voiceover") or "") + "\n" + spoken).strip()), *shots[1:]]
    if not keep_voice:
        shots = [dict(shot, voiceover="") for shot in shots]
    bundle["shots"] = shots
    whites = [item for item in list_white_assets(bundle["product"].get("id") or "") if _image_part(Path(item.get("path") or ""))]
    if not whites:
        raise HTTPException(400, "还没有白底图")
    bundle["whites"] = whites
    prompt_zh = ""
    if configured():
        try:
            prompt, prompt_zh, checks, status, round_no = review_selling_prompt(bundle)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        prompt = _with_library(
            bundle["product"].get("id") or "",
            build_prompt(bundle["product"], bundle["scene"], shots, bundle["assets"]),
        )
        steer = (bundle["variant"].get("steer") or "").strip()
        if steer:
            prompt += "\n优化要求：" + steer
        if not keep_voice:
            prompt += "\n不要写口播。"
        prompt += f"\nTotal duration: {allowed_duration(bundle['variant'].get('duration_sec'))} seconds."
        checks = evaluate(bundle["product"], bundle["scene"], shots, bundle["assets"], prompt)
        previous = bundle["variant"]["status"]
        round_no = bundle["variant"]["round"] + (0 if previous == "needs_human" else 1)
        passed = all_passed(checks)
        if passed:
            status = "passed"
        elif round_no >= 3 or previous == "needs_human":
            status = "needs_human"
        else:
            status = "draft"
    save_check(variant_id, prompt, checks, status, round_no, prompt_zh)
    scene = bundle["scene"]
    if (prompt or "").strip():
        from server.catalog import save_generated_prompt

        save_generated_prompt(bundle["product"].get("id") or "", scene.get("id") or "", scene.get("name") or "", prompt, prompt_zh)
    if status == "passed":
        if configured():
            with connect() as db:
                db.execute("UPDATE variants SET qa_note = '' WHERE id = ? AND qa_note = ?", (variant_id, _REJECTED))
    elif configured():
        with connect() as db:
            db.execute("UPDATE variants SET qa_note = ? WHERE id = ?", (_REJECTED, variant_id))
    return get_variant(variant_id)


@app.post("/variants/{variant_id}/generate")
def generate_variant(variant_id: str):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    status = bundle["variant"]["status"]
    prompt = (bundle["variant"].get("prompt_text") or "").strip()
    if status in {"queued", "running"}:
        raise HTTPException(400, "正在生成视频")
    if not (status == "passed" or (status in {"hold", "failed", "returned"} and prompt)):
        raise HTTPException(400, "自检通过后才能生成")
    if not prompt:
        raise HTTPException(400, "还没有提示词")
    seconds = allowed_duration(bundle["variant"].get("duration_sec"))
    if seconds < 4 or seconds > 15:
        raise HTTPException(400, "Seedance 2.0 Fast 单次只支持 15 秒以内。请把时长改成 10 秒或 15 秒，再检查一次提示词。")
    from server.seedance import OMNI_MODEL, video_model

    note = "正在提交 Gemini Omni，固定 10 秒、720p" if video_model() == OMNI_MODEL else "正在提交 Seedance 2.0 Fast"
    with connect() as db:
        db.execute(
            "UPDATE variants SET status = 'queued', output_path = '', qa_note = ?, speech_text = '', speech_note = '', speech_ok = NULL WHERE id = ?",
            (note, variant_id),
        )
    kick_generation(variant_id)
    return get_variant(variant_id)


@app.post("/variants/{variant_id}/adopt")
def adopt_variant(variant_id: str):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    with connect() as db:
        db.execute(
            "UPDATE variants SET adopted = 0 WHERE product_id = ? AND scene_id = ?",
            (bundle["variant"]["product_id"], bundle["variant"]["scene_id"]),
        )
        db.execute("UPDATE variants SET adopted = 1 WHERE id = ?", (variant_id,))
    return get_variant(variant_id)


@app.get("/speeches")
def list_speeches():
    with connect() as db:
        rows = db.execute(
            """
            SELECT variants.id, variants.label, variants.status, variants.product_id, variants.scene_id,
                   variants.speech_text, variants.speech_note, variants.speech_ok, variants.speech_tags,
                   products.title AS product_title, COALESCE(scenes.name, '') AS scene_name
            FROM variants
            JOIN products ON products.id = variants.product_id
            LEFT JOIN scenes ON scenes.id = variants.scene_id
            WHERE TRIM(COALESCE(variants.output_path, '')) != ''
            ORDER BY variants.created_at DESC
            """
        ).fetchall()
    items = []
    for row in rows:
        item = row_dict(row)
        item["video"] = f"/variants/{item['id']}/video"
        item["tags"] = _speech_tags(item.pop("speech_tags", ""))
        items.append(item)
    return items


@app.post("/speeches")
async def upload_speech(product_id: str = Form(...), file: UploadFile = File(...), name: str = Form("")):
    _require_product(product_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".m4v"}:
        raise HTTPException(400, "成片请上传 mp4、mov 或 webm")
    payload = await file.read()
    if len(payload) < 32:
        raise HTTPException(400, "文件是空的")
    variant_id = new_id("var")
    dest = FILES / "outputs" / f"{variant_id}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    label = name.strip() or Path(file.filename or "").stem or "成片"
    with connect() as db:
        db.execute(
            """
            INSERT INTO variants (id, product_id, scene_id, label, status, round, check_json, adopted, output_path, created_at)
            VALUES (?, ?, '', ?, 'returned', 0, '[]', 0, ?, ?)
            """,
            (variant_id, product_id, label[:80], str(dest), now()),
        )
    return next(item for item in list_speeches() if item["id"] == variant_id)


@app.post("/variants/{variant_id}/speech")
def check_speech(variant_id: str):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    path = Path(bundle["variant"].get("output_path") or "")
    if not path.is_file():
        raise HTTPException(400, "还没有成片")
    from server.media import spoken_text

    try:
        transcript = spoken_text(path).strip()
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if not transcript:
        note, ok = "没有识别到口播", None
    else:
        try:
            note, ok = translate_spoken(transcript), None
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
    with connect() as db:
        db.execute(
            "UPDATE variants SET speech_text = ?, speech_ok = ?, speech_note = ? WHERE id = ?",
            (transcript[:4000], ok, note[:2000], variant_id),
        )
    return get_variant(variant_id)


@app.post("/variants/{variant_id}/speech/approve")
def approve_speech(variant_id: str):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    if not (bundle["variant"].get("speech_text") or "").strip():
        raise HTTPException(400, "先提取口播")
    with connect() as db:
        db.execute("UPDATE variants SET speech_ok = 1 WHERE id = ?", (variant_id,))
    return get_variant(variant_id)


@app.post("/variants/{variant_id}/speech/reject")
def reject_speech(variant_id: str):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    if not (bundle["variant"].get("output_path") or "").strip():
        raise HTTPException(400, "还没有成片")
    with connect() as db:
        db.execute("UPDATE variants SET speech_ok = 0 WHERE id = ?", (variant_id,))
    return get_variant(variant_id)


@app.patch("/variants/{variant_id}/speech/tags")
def update_speech_tags(variant_id: str, body: SpeechTags):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    tags = _clean_tags(body.tags)
    with connect() as db:
        db.execute("UPDATE variants SET speech_tags = ? WHERE id = ?", (json.dumps(tags, ensure_ascii=False), variant_id))
    return get_variant(variant_id)


@app.delete("/speeches/{variant_id}")
def delete_speech(variant_id: str):
    bundle = load_variant_bundle(variant_id)
    if not bundle:
        raise HTTPException(404, "版本不存在")
    variant = bundle["variant"]
    path = Path(variant.get("output_path") or "")
    _remove_film(path)
    if path.stem:
        _remove_film(FILES / "speech" / f"{path.stem}.wav")
    if (variant.get("scene_id") or "").strip():
        with connect() as db:
            db.execute(
                "UPDATE variants SET output_path = '', speech_text = '', speech_note = '', speech_ok = NULL, speech_tags = '[]' WHERE id = ?",
                (variant_id,),
            )
    else:
        with connect() as db:
            _delete_variants(db, [variant_id])
    return {"ok": True}


def _speech_tags(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [text for text in (" ".join(str(item).split()) for item in data) if text]


def _clean_tags(tags: list[str]) -> list[str]:
    clean: list[str] = []
    for tag in tags:
        text = " ".join(str(tag).split())
        if not text or text in clean:
            continue
        if len(text) > 40:
            raise HTTPException(400, "标签最多 40 个字")
        clean.append(text)
    if len(clean) > 12:
        raise HTTPException(400, "标签最多 12 个")
    return clean


def _remove_film(path: Path) -> None:
    try:
        resolved = path.resolve()
        root = FILES.resolve()
    except OSError:
        return
    if resolved.is_file() and (resolved == root or root in resolved.parents):
        resolved.unlink(missing_ok=True)


@app.post("/variants/{variant_id}/hold")
def hold_variant(variant_id: str):
    with connect() as db:
        db.execute("UPDATE variants SET status = 'hold', qa_note = '与场景或分镜不一致，留到下一晚' WHERE id = ?", (variant_id,))
    return get_variant(variant_id)


@app.get("/variants/{variant_id}/video")
def variant_video(variant_id: str):
    from fastapi.responses import FileResponse

    bundle = load_variant_bundle(variant_id)
    if not bundle or not bundle["variant"].get("output_path"):
        raise HTTPException(404, "还没有成片")
    path = Path(bundle["variant"]["output_path"])
    if not path.exists():
        raise HTTPException(404, "成片文件不在")
    return FileResponse(path)


@app.get("/scheduler")
def get_scheduler():
    with connect() as db:
        return row_dict(db.execute("SELECT open, probed FROM scheduler WHERE id = 1").fetchone())


@app.post("/scheduler/open")
def open_scheduler():
    release_held()
    return get_scheduler()


@app.post("/scheduler/close")
def close_scheduler():
    with connect() as db:
        db.execute("UPDATE scheduler SET open = 0")
    return get_scheduler()


@app.post("/scheduler/mark-probed")
def mark_probed():
    with connect() as db:
        db.execute("UPDATE scheduler SET probed = 1")
    return get_scheduler()


async def _upload_asset(product_id: str, kind: str, label: str, image: UploadFile):
    _require_product(product_id)
    if not image.filename:
        raise HTTPException(400, "需要图片")
    asset_id = new_id("asset")
    dest = FILES / "products" / product_id / f"{asset_id}{Path(image.filename).suffix or '.img'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await image.read())
    with connect() as db:
        db.execute(
            "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, ?, ?, ?, '{}', ?)",
            (asset_id, product_id, kind, label, str(dest), now()),
        )
    if kind == "product":
        from server.board import bind_product_photos
        bind_product_photos(product_id)
    return {"id": asset_id, "image": file_url(str(dest))}


class TemplateIn(BaseModel):
    name: str
    body: str


def _template_out(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "body": row["body"], "builtin": int(row["builtin"] or 0)}


@app.get("/templates")
def list_templates():
    with connect() as db:
        rows = [row_dict(row) for row in db.execute("SELECT * FROM prompt_templates ORDER BY builtin DESC, created_at")]
    return [_template_out(row) for row in rows]


@app.post("/templates")
def create_template(body: TemplateIn):
    name = body.name.strip()
    text = body.body.strip()
    if not name or not text:
        raise HTTPException(400, "模板需要名称和正文")
    template_id = new_id("tpl")
    with connect() as db:
        db.execute(
            "INSERT INTO prompt_templates (id, name, body, builtin, created_at) VALUES (?, ?, ?, 0, ?)",
            (template_id, name[:40], text, now()),
        )
        row = row_dict(db.execute("SELECT * FROM prompt_templates WHERE id = ?", (template_id,)).fetchone())
    return _template_out(row)


@app.patch("/templates/{template_id}")
def update_template_body(template_id: str, body: TemplateIn):
    name = body.name.strip()
    text = body.body.strip()
    if not name or not text:
        raise HTTPException(400, "模板需要名称和正文")
    with connect() as db:
        current = row_dict(db.execute("SELECT * FROM prompt_templates WHERE id = ?", (template_id,)).fetchone())
        if not current:
            raise HTTPException(404, "模板不存在")
        db.execute("UPDATE prompt_templates SET name = ?, body = ? WHERE id = ?", (name[:40], text, template_id))
        row = row_dict(db.execute("SELECT * FROM prompt_templates WHERE id = ?", (template_id,)).fetchone())
    return _template_out(row)


@app.delete("/templates/{template_id}")
def delete_template(template_id: str):
    with connect() as db:
        current = row_dict(db.execute("SELECT * FROM prompt_templates WHERE id = ?", (template_id,)).fetchone())
        if not current:
            raise HTTPException(404, "模板不存在")
        if int(current["builtin"] or 0):
            raise HTTPException(400, "内置模板不能删除")
        db.execute("UPDATE variants SET template_id = '' WHERE template_id = ?", (template_id,))
        db.execute("DELETE FROM prompt_templates WHERE id = ?", (template_id,))
    return {"ok": True}


class PromptIn(BaseModel):
    name: str
    body: str
    body_zh: str = ""
    tag: str = ""
    product_id: str = ""


def _library_rows(table: str) -> list[dict]:
    with connect() as db:
        rows = [
            row_dict(row)
            for row in db.execute(
                f"""
                SELECT t.*, COALESCE(p.title, '') AS product_title
                FROM {table} t
                LEFT JOIN products p ON p.id = t.product_id
                ORDER BY t.created_at DESC
                """
            )
        ]
    if table == "hits":
        for row in rows:
            row["file"] = file_url(row.get("path") or "")
    return rows


def _claim_product(db, table: str, row_id: str, product_id: str) -> str:
    product_id = (product_id or "").strip()
    if not product_id:
        return ""
    if not db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone():
        raise HTTPException(400, "商品不存在")
    if table == "prompts":
        db.execute("UPDATE prompts SET product_id = '' WHERE product_id = ? AND id != ? AND COALESCE(origin, 'manual') = 'manual'", (product_id, row_id))
    return product_id


def _with_library(product_id: str, prompt: str) -> str:
    from server.catalog import page_facts

    with connect() as db:
        bound = row_dict(db.execute("SELECT body FROM prompts WHERE product_id = ? AND COALESCE(origin, 'manual') = 'manual'", (product_id,)).fetchone())
        hits = [row_dict(row) for row in db.execute("SELECT name, note FROM hits WHERE product_id = ? ORDER BY created_at", (product_id,))]
    extra = []
    facts = page_facts(product_id)
    if facts:
        extra.append("Page facts already read from the product page. Use only these. Do not invent sizes, weights, or claims, and do not mention fields the page did not provide:\n" + facts)
    if bound and (bound.get("body") or "").strip():
        extra.append("Prompt library, bound to this product only. Do not invent claims beyond the product notes: " + bound["body"].strip())
    for hit in hits:
        text = " ".join(part for part in (hit.get("name") or "", hit.get("note") or "") if part).strip()
        if text:
            extra.append("Viral reference, camera and action only. Do not copy its face, voice, music, or claims: " + text)
    return prompt if not extra else prompt + "\n" + "\n".join(extra)


@app.get("/prompts")
def list_prompts():
    return _library_rows("prompts")


@app.post("/prompts")
def create_prompt(body: PromptIn):
    name = body.name.strip()
    text = body.body.strip()
    if not name or not text:
        raise HTTPException(400, "提示词需要名称和正文")
    prompt_id = new_id("prompt")
    with connect() as db:
        product_id = _claim_product(db, "prompts", prompt_id, body.product_id)
        db.execute(
            "INSERT INTO prompts (id, name, body, body_zh, tag, product_id, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, 'manual', ?)",
            (prompt_id, name, text, body.body_zh.strip(), body.tag.strip(), product_id, now()),
        )
    return next(item for item in _library_rows("prompts") if item["id"] == prompt_id)


@app.patch("/prompts/{prompt_id}")
def update_prompt(prompt_id: str, body: PromptIn):
    name = body.name.strip()
    text = body.body.strip()
    if not name or not text:
        raise HTTPException(400, "提示词需要名称和正文")
    with connect() as db:
        if not db.execute("SELECT id FROM prompts WHERE id = ?", (prompt_id,)).fetchone():
            raise HTTPException(404, "提示词不存在")
        product_id = _claim_product(db, "prompts", prompt_id, body.product_id)
        db.execute(
            "UPDATE prompts SET name = ?, body = ?, body_zh = ?, tag = ?, product_id = ? WHERE id = ?",
            (name, text, body.body_zh.strip(), body.tag.strip(), product_id, prompt_id),
        )
    return next(item for item in _library_rows("prompts") if item["id"] == prompt_id)


@app.delete("/prompts/{prompt_id}")
def delete_prompt(prompt_id: str):
    with connect() as db:
        db.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
    return {"ok": True}


def _save_hit_file(hit_id: str, upload: UploadFile) -> tuple[str, str]:
    suffix = Path(upload.filename or "").suffix.lower()
    video = suffix in {".mp4", ".mov", ".webm"}
    image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if not video and not image:
        raise HTTPException(400, "爆款素材用图片或视频")
    dest = FILES / "hits" / f"{hit_id}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return str(dest), "video" if video else "image"


async def _remember_shots(hit_id: str, refresh: bool = False) -> None:
    from starlette.concurrency import run_in_threadpool

    from server.bobtoken import save_hit_shots

    try:
        await run_in_threadpool(save_hit_shots, hit_id, refresh)
    except Exception:
        return


async def _download_hit(url: str, dest: Path) -> None:
    from starlette.concurrency import run_in_threadpool

    from server.media import download_video

    try:
        await run_in_threadpool(download_video, url, dest)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/hits")
def list_hits():
    return _library_rows("hits")


@app.post("/hits")
async def create_hit(name: str = Form(...), note: str = Form(""), product_id: str = Form(""), source_url: str = Form(""), file: UploadFile | None = File(None)):
    name = name.strip()
    source_url = source_url.strip()
    if not name or not ((file and file.filename) or source_url):
        raise HTTPException(400, "爆款素材需要名称，以及文件或视频链接")
    hit_id = new_id("hit")
    note_text = note.strip()
    if source_url:
        dest = str(FILES / "hits" / f"{hit_id}.mp4")
        kind = "video"
        if source_url not in note_text:
            note_text = "\n".join(part for part in (note_text, source_url) if part)
        await _download_hit(source_url, Path(dest))
    else:
        dest, kind = _save_hit_file(hit_id, file)
        payload = await file.read()
        if len(payload) < 32:
            raise HTTPException(400, "文件是空的")
        Path(dest).write_bytes(payload)
    with connect() as db:
        bound = _claim_product(db, "hits", hit_id, product_id)
        db.execute(
            "INSERT INTO hits (id, name, note, path, kind, product_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hit_id, name, note_text, dest, kind, bound, now()),
        )
    if kind == "video":
        await _remember_shots(hit_id)
    return next(item for item in _library_rows("hits") if item["id"] == hit_id)


@app.patch("/hits/{hit_id}")
async def update_hit(hit_id: str, name: str = Form(None), note: str = Form(None), product_id: str = Form(None), source_url: str = Form(""), file: UploadFile | None = File(None)):
    with connect() as db:
        current = row_dict(db.execute("SELECT * FROM hits WHERE id = ?", (hit_id,)).fetchone())
    if not current:
        raise HTTPException(404, "爆款素材不存在")
    next_name = current["name"] if name is None else name.strip()
    next_note = current["note"] if note is None else note.strip()
    if not next_name:
        raise HTTPException(400, "爆款素材需要名称")
    path, kind = current["path"], current["kind"]
    source_url = source_url.strip()
    if source_url:
        path, kind = str(FILES / "hits" / f"{hit_id}.mp4"), "video"
        if source_url not in next_note:
            next_note = "\n".join(part for part in (next_note, source_url) if part)
        await _download_hit(source_url, Path(path))
    elif file and file.filename:
        path, kind = _save_hit_file(hit_id, file)
        payload = await file.read()
        if len(payload) < 32:
            raise HTTPException(400, "文件是空的")
        Path(path).write_bytes(payload)
    with connect() as db:
        bound = current["product_id"] if product_id is None else _claim_product(db, "hits", hit_id, product_id)
        db.execute(
            "UPDATE hits SET name = ?, note = ?, path = ?, kind = ?, product_id = ? WHERE id = ?",
            (next_name, next_note, path, kind, bound, hit_id),
        )
    if kind == "video" and (source_url or (file and file.filename)):
        await _remember_shots(hit_id, refresh=True)
    return next(item for item in _library_rows("hits") if item["id"] == hit_id)


@app.delete("/hits/{hit_id}")
def delete_hit(hit_id: str):
    with connect() as db:
        db.execute("DELETE FROM hits WHERE id = ?", (hit_id,))
    return {"ok": True}


def _delete_product(product_id: str) -> None:
    with connect() as db:
        if not db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone():
            raise HTTPException(404, "商品不存在")
        variant_ids = [row["id"] for row in db.execute("SELECT id FROM variants WHERE product_id = ?", (product_id,))]
        asset_ids = [row["id"] for row in db.execute("SELECT id FROM assets WHERE product_id = ?", (product_id,))]
        hit_paths = [row["path"] for row in db.execute("SELECT path FROM hits WHERE product_id = ?", (product_id,)) if row["path"]]
        output_paths = [row["output_path"] for row in db.execute("SELECT output_path FROM variants WHERE product_id = ?", (product_id,)) if row["output_path"]]
        _delete_variants(db, variant_ids)
        db.execute("DELETE FROM assets WHERE product_id = ?", (product_id,))
        db.execute("DELETE FROM hits WHERE product_id = ?", (product_id,))
        db.execute("DELETE FROM prompts WHERE product_id = ?", (product_id,))
        db.execute("DELETE FROM boards WHERE product_id = ?", (product_id,))
        db.execute("DELETE FROM product_specs WHERE product_id = ?", (product_id,))
        db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    shutil.rmtree(FILES / "products" / product_id, ignore_errors=True)
    shutil.rmtree(FILES / "whites" / product_id, ignore_errors=True)
    shutil.rmtree(FILES / "details" / product_id, ignore_errors=True)
    shutil.rmtree(FILES / "usages" / product_id, ignore_errors=True)
    for asset_id in asset_ids:
        shutil.rmtree(FILES / "references" / asset_id, ignore_errors=True)
    _unlink_owned(hit_paths, FILES / "hits")
    _unlink_owned(output_paths, FILES / "outputs")


def _queue_product_refresh(product_id: str, url: str) -> dict:
    link = url.strip()
    if not link.startswith(("http://", "https://")):
        raise HTTPException(400, "请填写 Amazon 或 1688 的商品链接")
    with connect() as db:
        if not db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone():
            raise HTTPException(404, "商品不存在")
        db.execute("DELETE FROM assets WHERE product_id = ? AND kind = 'product'", (product_id,))
        db.execute("DELETE FROM product_specs WHERE product_id = ?", (product_id,))
        db.execute(
            """
            UPDATE products
            SET source_url = ?, title = '', price = '', points = '', page_text = '', profile_text = '', status = 'collecting', detail = ''
            WHERE id = ?
            """,
            (link, product_id),
        )
    shutil.rmtree(FILES / "products" / product_id, ignore_errors=True)
    return {"id": product_id, "status": "collecting"}


def _unlink_owned(paths: list[str], root: Path) -> None:
    base = root.resolve()
    for raw in paths:
        target = Path(raw)
        try:
            target.resolve().relative_to(base)
        except ValueError:
            continue
        target.unlink(missing_ok=True)


def _require_product(product_id: str) -> None:
    with connect() as db:
        if not db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone():
            raise HTTPException(404, "商品不存在")
