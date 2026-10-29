from __future__ import annotations

from server.checks import default_shots
from server.db import connect, dumps, file_url, loads, new_id, now, row_dict


def bind_product_photos(product_id: str) -> None:
    from server.media import is_banner

    with connect() as db:
        photos = [
            row["id"]
            for row in db.execute("SELECT id, path FROM assets WHERE product_id = ? AND kind = 'product'", (product_id,))
            if row["path"] and not is_banner(row["path"])
        ]
        variants = [row["id"] for row in db.execute("SELECT id FROM variants WHERE product_id = ?", (product_id,))]
        for variant_id in variants:
            for photo_id in photos:
                db.execute("INSERT OR IGNORE INTO variant_assets (variant_id, asset_id) VALUES (?, ?)", (variant_id, photo_id))


def create_variant(product_id: str, scene_id: str) -> dict:
    with connect() as db:
        scene = row_dict(db.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()) if scene_id else None
        if scene_id and not scene:
            raise LookupError("场景不存在")
        count = db.execute(
            "SELECT COUNT(*) AS n FROM variants WHERE product_id = ? AND scene_id = ?",
            (product_id, scene_id),
        ).fetchone()["n"]
        variant_id = new_id("var")
        label = f"版本 {count + 1}"
        db.execute(
            """
            INSERT INTO variants (id, product_id, scene_id, label, status, round, check_json, adopted, created_at)
            VALUES (?, ?, ?, ?, 'draft', 0, '[]', 0, ?)
            """,
            (variant_id, product_id, scene_id, label, now()),
        )
        for shot in default_shots(scene["description"] if scene else ""):
            db.execute(
                """
                INSERT INTO shots (id, variant_id, ordinal, t_start, t_end, action, voiceover, camera)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("shot"), variant_id, shot["ordinal"], shot["t_start"], shot["t_end"], shot["action"], shot["voiceover"], shot["camera"]),
            )
        variant = row_dict(db.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone())
    bind_product_photos(product_id)
    return variant


_STEPS = ("scene", "facts", "model")
_GRAPH_KINDS = ("scene", "facts", "person", "voice", "note", "viral", "output", "storyboard")
_STORY_PORTS = ("product", "viral", "person", "scene")
_SIDE_TYPES = {"voice", "note", "viral", "facts", "asset"}


def _load_doc(db, product_id: str) -> dict:
    stored = row_dict(db.execute("SELECT document_json FROM boards WHERE product_id = ?", (product_id,)).fetchone())
    doc = loads(stored["document_json"], {}) if stored else {}
    if not isinstance(doc, dict):
        doc = {}
    return {
        "nodes": doc.get("nodes") or [],
        "edges": doc.get("edges") or [],
        "steps": [step for step in doc.get("steps") or [] if step in _STEPS],
        "graph": 1 if doc.get("graph") else 0,
    }


def _store_doc(db, product_id: str, doc: dict) -> None:
    db.execute(
        """
        INSERT INTO boards (product_id, document_json, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET document_json = excluded.document_json, updated_at = excluded.updated_at
        """,
        (product_id, dumps({"nodes": doc.get("nodes") or [], "edges": doc.get("edges") or [], "steps": doc.get("steps") or [], "graph": doc.get("graph") or 0}), now()),
    )


def add_step(product_id: str, step: str) -> dict:
    if step not in _STEPS:
        raise ValueError("这一步不存在")
    kind = {"scene": "scene", "facts": "facts", "model": "person"}[step]
    fixed = {"scene": "slot:scene", "facts": f"facts:{product_id}", "model": "slot:model"}[step]
    return add_graph_node(product_id, kind, f"product:{product_id}", fixed_id=fixed)


def drop_step(product_id: str, step: str) -> None:
    with connect() as db:
        doc = _load_doc(db, product_id)
        doc["steps"] = [item for item in doc["steps"] if item != step]
        if step == "model":
            doc["nodes"] = [node for node in doc["nodes"] if not _is_person_node(node) and node.get("id") != "slot:model"]
            doc["edges"] = [edge for edge in doc["edges"] if edge.get("source") != "slot:model" and edge.get("target") != "slot:model" and not _edge_touches_missing(edge, doc["nodes"])]
        _store_doc(db, product_id, doc)


def add_graph_node(product_id: str, kind: str, source: str, *, fixed_id: str = "", text: str = "") -> dict:
    if kind not in _GRAPH_KINDS:
        raise ValueError("这一步不存在")
    board_payload(product_id)
    with connect() as db:
        doc = _load_doc(db, product_id)
        known = {node.get("id") for node in doc["nodes"]}
        if source and source not in known:
            raise LookupError("上一个节点不存在")
        if fixed_id and fixed_id in known:
            doc = None
        else:
            scene_id = _nearest_scene_id(doc, source) if kind == "output" else ""
    if doc is None:
        return board_payload(product_id)
    if kind == "output":
        variant = create_variant(product_id, scene_id)
        node = _variant_node(variant, {"id": scene_id, "name": variant["label"]}, None, 0, 0)
        node_id = variant["id"]
    else:
        node_id = fixed_id or new_id("n")
        node = _blank_node(kind, node_id, product_id, text)
    with connect() as db:
        doc = _load_doc(db, product_id)
        node["position"] = _next_position(doc, source)
        doc["nodes"].append(node)
        source_node = _node_by_id(doc, source)
        if source and kind == "storyboard":
            _attach_story_edge(doc, source, node_id)
        elif source and (source_node or {}).get("type") != "storyboard":
            doc["edges"].append({"id": f"{source}-{node_id}", "source": source, "target": node_id, "type": "smoothstep"})
        doc["graph"] = 1
        _store_doc(db, product_id, doc)
    return board_payload(product_id)


def update_graph_node(product_id: str, node_id: str, text: str | None = None, scene_id: str | None = None, hit_id: str | None = None) -> dict:
    board_payload(product_id)
    with connect() as db:
        doc = _load_doc(db, product_id)
        found = False
        for node in doc["nodes"]:
            if node.get("id") != node_id:
                continue
            found = True
            data = dict(node.get("data") or {})
            if text is not None:
                data["text"] = text
                data["label"] = text.strip()[:24] or data.get("label") or ""
            if scene_id is not None:
                scene = row_dict(db.execute("SELECT * FROM scenes WHERE id = ? AND active = 1", (scene_id,)).fetchone())
                if not scene:
                    raise LookupError("场景不存在或已停用")
                image = file_url(scene["image_path"])
                data.update({"sceneId": scene["id"], "label": scene["name"], "description": scene["description"], "image": image, "images": [image] if image else []})
                node["type"] = "scene"
            if hit_id is not None:
                hit = row_dict(db.execute("SELECT * FROM hits WHERE id = ? AND product_id = ?", (hit_id, product_id)).fetchone())
                if not hit:
                    raise LookupError("爆款素材不存在")
                prompt = (hit.get("shot_prompt") or "").strip()
                data.update({
                    "hitId": hit["id"],
                    "label": hit["name"],
                    "text": prompt or "这条视频的分镜还没整理好",
                    "video": file_url(hit.get("path") or "") if hit.get("kind") == "video" else "",
                })
                node["type"] = "viral"
            node["data"] = data
        if not found:
            raise LookupError("节点不存在")
        doc["graph"] = 1
        _store_doc(db, product_id, doc)
    return board_payload(product_id)


def attach_scene_graph(product_id: str, scene_id: str) -> dict:
    board_payload(product_id)
    variant = create_variant(product_id, scene_id)
    with connect() as db:
        scene = row_dict(db.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone())
        doc = _load_doc(db, product_id)
        scene_node_id = f"scene:{scene_id}"
        slot = next((node for node in doc["nodes"] if node.get("id") == "slot:scene"), None)
        if slot:
            slot["id"] = scene_node_id
            slot["type"] = "scene"
            slot["data"] = _scene_node(scene, None, 0, 0)["data"]
            for edge in doc["edges"]:
                if edge.get("target") == "slot:scene":
                    edge["target"] = scene_node_id
                    edge["id"] = f"{edge.get('source')}-{scene_node_id}"
                if edge.get("source") == "slot:scene":
                    edge["source"] = scene_node_id
                    edge["id"] = f"{scene_node_id}-{edge.get('target')}"
        elif scene_node_id not in {node.get("id") for node in doc["nodes"]}:
            node = _scene_node(scene, None, 520, 40)
            doc["nodes"].append(node)
            doc["edges"].append({"id": f"product:{product_id}-{scene_node_id}", "source": f"product:{product_id}", "target": scene_node_id, "type": "smoothstep"})
        if variant["id"] not in {node.get("id") for node in doc["nodes"]}:
            doc["nodes"].append(_variant_node(variant, scene, None, 900, 40))
            doc["edges"].append({"id": f"{scene_node_id}-{variant['id']}", "source": scene_node_id, "target": variant["id"], "type": "smoothstep"})
        doc["graph"] = 1
        _store_doc(db, product_id, doc)
    return board_payload(product_id)


def place_person_node(product_id: str, asset: dict, node_id: str = "") -> None:
    board_payload(product_id)
    with connect() as db:
        doc = _load_doc(db, product_id)
        fresh = _asset_node(asset, None, 520, 320)
        if node_id:
            replaced = False
            for node in doc["nodes"]:
                if node.get("id") != node_id:
                    continue
                node.clear()
                node.update(fresh)
                node["id"] = asset["id"]
                replaced = True
            if replaced:
                for edge in doc["edges"]:
                    if edge.get("target") == node_id:
                        edge["target"] = asset["id"]
                        edge["id"] = f"{edge.get('source')}-{asset['id']}"
                    if edge.get("source") == node_id:
                        edge["source"] = asset["id"]
                        edge["id"] = f"{asset['id']}-{edge.get('target')}"
        else:
            doc["nodes"] = [node for node in doc["nodes"] if not _is_person_node(node) and node.get("id") != "slot:model"]
            kept = {node.get("id") for node in doc["nodes"]}
            doc["edges"] = [edge for edge in doc["edges"] if edge.get("source") in kept and edge.get("target") in kept]
            doc["nodes"].append(fresh)
            doc["edges"].append({"id": f"product:{product_id}-{asset['id']}", "source": f"product:{product_id}", "target": asset["id"], "type": "smoothstep"})
        doc["graph"] = 1
        _store_doc(db, product_id, doc)


def prepare_variant_graph(variant_id: str) -> str:
    with connect() as db:
        variant = row_dict(db.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone())
        if not variant:
            return {"voice": "", "notes": []}
        product_id = variant["product_id"]
        doc = _load_doc(db, product_id)
        related = {node.get("id") for node in input_nodes(doc, variant_id)}
        voices = []
        notes = []
        people = []
        for node in doc["nodes"]:
            if node.get("id") not in related or node.get("id") == variant_id:
                continue
            data = node.get("data") or {}
            text = (data.get("text") or "").strip()
            if node.get("type") == "voice" and text:
                voices.append(text)
            if node.get("type") == "note" and text:
                notes.append(text)
            if _is_person_node(node):
                people.append(data.get("assetId") or node.get("id"))
        db.execute("DELETE FROM variant_assets WHERE variant_id = ? AND asset_id IN (SELECT id FROM assets WHERE kind = 'person')", (variant_id,))
        for asset_id in people:
            if db.execute("SELECT id FROM assets WHERE id = ? AND kind = 'person'", (asset_id,)).fetchone():
                db.execute("INSERT OR IGNORE INTO variant_assets (variant_id, asset_id) VALUES (?, ?)", (variant_id, asset_id))
    return {"voice": "\n".join(voices), "notes": notes}


def remove_node(product_id: str, node_id: str) -> dict:
    board_payload(product_id)
    with connect() as db:
        doc = _load_doc(db, product_id)
        if node_id == f"product:{product_id}":
            raise ValueError("商品卡不能删")
        removed = next((node for node in doc["nodes"] if node.get("id") == node_id), None)
        if not removed:
            raise LookupError("节点不存在")
        variant = db.execute("SELECT id, status FROM variants WHERE id = ? AND product_id = ?", (node_id, product_id)).fetchone()
        if variant and variant["status"] == "running":
            raise ValueError("这一版正在生成，先留着")
        doc["nodes"] = [node for node in doc["nodes"] if node.get("id") != node_id]
        doc["edges"] = [edge for edge in doc["edges"] if edge.get("source") != node_id and edge.get("target") != node_id]
        if variant:
            _delete_variants(db, [node_id])
        else:
            asset = db.execute("SELECT id, kind FROM assets WHERE id = ? AND product_id = ?", (node_id, product_id)).fetchone()
            if asset and asset["kind"] == "person":
                db.execute("DELETE FROM variant_assets WHERE asset_id = ?", (node_id,))
                db.execute("DELETE FROM assets WHERE id = ?", (node_id,))
            elif asset and asset["kind"] == "reference":
                db.execute("DELETE FROM variant_assets WHERE asset_id = ?", (node_id,))
                db.execute("DELETE FROM assets WHERE id = ?", (node_id,))
            elif asset and asset["kind"] == "product":
                raise ValueError("商品图留在商品卡里")
        doc["graph"] = 1
        _store_doc(db, product_id, doc)
    return board_payload(product_id)


def _edge_touches_missing(edge: dict, nodes: list[dict]) -> bool:
    known = {node.get("id") for node in nodes}
    return edge.get("source") not in known or edge.get("target") not in known


def _is_person_node(node: dict) -> bool:
    data = node.get("data") or {}
    return data.get("kind") == "person" or data.get("step") == "model"


def _node_by_id(doc: dict, node_id: str) -> dict | None:
    return next((node for node in doc.get("nodes") or [] if node.get("id") == node_id), None)


def story_port(node: dict | None) -> str:
    if not node:
        return ""
    if node.get("type") == "product":
        return "product"
    if node.get("type") == "viral":
        return "viral"
    if node.get("type") == "scene":
        return "scene"
    if node.get("type") == "asset" and (node.get("data") or {}).get("kind") == "person":
        return "person"
    return ""


def _attach_story_edge(doc: dict, source: str, node_id: str) -> None:
    port = story_port(_node_by_id(doc, source))
    if not port:
        return
    taken = any(edge.get("target") == node_id and edge.get("targetHandle") == port for edge in doc["edges"])
    if taken:
        return
    doc["edges"].append({
        "id": f"{source}-{node_id}-{port}",
        "source": source,
        "target": node_id,
        "targetHandle": port,
        "type": "smoothstep",
    })


def _story_edges(nodes: list[dict], edges: list[dict]) -> list[dict]:
    by_id = {node.get("id"): node for node in nodes}
    seen: set[tuple[str, str]] = set()
    cleaned = []
    for edge in edges:
        source = by_id.get(edge.get("source"))
        target = by_id.get(edge.get("target"))
        if source and source.get("type") == "storyboard":
            continue
        if target and target.get("type") == "storyboard":
            handle = edge.get("targetHandle") or story_port(source)
            if handle not in _STORY_PORTS or story_port(source) != handle:
                continue
            key = (str(edge.get("target")), handle)
            if key in seen:
                continue
            seen.add(key)
            edge = {**edge, "targetHandle": handle, "id": edge.get("id") or f"{edge.get('source')}-{edge.get('target')}-{handle}"}
        cleaned.append(edge)
    return cleaned


def read_graph(product_id: str) -> dict:
    board_payload(product_id)
    with connect() as db:
        return _load_doc(db, product_id)


def write_node_data(product_id: str, node_id: str, data: dict) -> dict:
    with connect() as db:
        doc = _load_doc(db, product_id)
        found = False
        for node in doc["nodes"]:
            if node.get("id") != node_id:
                continue
            if node.get("type") != "storyboard":
                raise ValueError("这不是分镜头生成节点")
            node["data"] = data
            found = True
        if not found:
            raise LookupError("节点不存在")
        doc["graph"] = 1
        _store_doc(db, product_id, doc)
    return board_payload(product_id)


def _blank_node(kind: str, node_id: str, product_id: str, text: str) -> dict:
    if kind == "scene":
        return {"id": node_id, "type": "slot", "data": {"step": "scene", "title": "场景图", "hint": "选一张已有的，或新建"}}
    if kind == "person":
        return {"id": node_id, "type": "slot", "data": {"step": "model", "title": "模特", "hint": "选一个，可以再接一个"}}
    if kind == "facts":
        return {"id": node_id, "type": "facts", "data": {"title": "产品信息约束", "price": "", "points": "写页面上有的卖点", "custom": False}}
    if kind == "voice":
        return {"id": node_id, "type": "voice", "data": {"label": "口播", "text": text}}
    if kind == "note":
        return {"id": node_id, "type": "note", "data": {"label": "自定义", "text": text}}
    if kind == "viral":
        return {"id": node_id, "type": "viral", "data": {"label": "爆款复刻", "hitId": "", "text": "选一条已整理好的分镜"}}
    if kind == "storyboard":
        return {
            "id": node_id,
            "type": "storyboard",
            "data": {"label": "分镜头生成", "duration": 15, "shotCount": 4, "shots": [], "videoPrompt": ""},
        }
    raise ValueError("这一步不存在")


def _next_position(doc: dict, source: str) -> dict:
    source_node = next((node for node in doc["nodes"] if node.get("id") == source), None)
    origin = (source_node or {}).get("position") or {"x": 40, "y": 80}
    used = sum(1 for edge in doc["edges"] if edge.get("source") == source)
    return {"x": float(origin.get("x", 0)) + 380, "y": float(origin.get("y", 0)) + used * 200}


def _nearest_scene_id(doc: dict, source: str) -> str:
    nodes = {node.get("id"): node for node in doc["nodes"]}
    incoming: dict[str, list[str]] = {}
    for edge in doc["edges"]:
        incoming.setdefault(edge.get("target"), []).append(edge.get("source"))
    seen = set()
    stack = [source]
    while stack:
        current = stack.pop()
        if not current or current in seen or current not in nodes:
            continue
        seen.add(current)
        data = nodes[current].get("data") or {}
        if data.get("sceneId"):
            return data["sceneId"]
        stack.extend(incoming.get(current, []))
    return ""


def input_nodes(doc: dict, variant_id: str) -> list[dict]:
    nodes = {node.get("id"): node for node in doc.get("nodes") or []}
    incoming: dict[str, list[str]] = {}
    for edge in doc.get("edges") or []:
        incoming.setdefault(edge.get("target"), []).append(edge.get("source"))
    upstream: set[str] = set()
    stack = [variant_id]
    while stack:
        current = stack.pop()
        if not current or current in upstream or current not in nodes:
            continue
        upstream.add(current)
        stack.extend(incoming.get(current, []))
    chosen = [nodes[node_id] for node_id in upstream]
    seen = set(upstream)
    for edge in doc.get("edges") or []:
        pairs = ((edge.get("source"), edge.get("target")), (edge.get("target"), edge.get("source")))
        for node_id, other_id in pairs:
            if node_id not in upstream or other_id in seen or other_id not in nodes:
                continue
            other = nodes[other_id]
            if other.get("type") not in _SIDE_TYPES:
                continue
            if other.get("type") == "asset" and (other.get("data") or {}).get("kind") != "person":
                continue
            seen.add(other_id)
            chosen.append(other)
    return chosen


def variant_selection(variant_id: str) -> dict:
    with connect() as db:
        variant = row_dict(db.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone())
        if not variant:
            return {"facts": False, "scene": False, "viral": []}
        doc = _load_doc(db, variant["product_id"])
        prompts = {
            row["id"]: row_dict(row)
            for row in db.execute("SELECT id, name, shot_prompt FROM hits WHERE product_id = ?", (variant["product_id"],))
        }
    facts = False
    scene = False
    viral: list[dict] = []
    for node in input_nodes(doc, variant_id):
        data = node.get("data") or {}
        if node.get("type") == "facts":
            facts = True
        if node.get("type") == "scene":
            scene = True
        if node.get("type") == "viral":
            hit = prompts.get(data.get("hitId") or "") or {}
            text = (hit.get("shot_prompt") or "").strip()
            if text:
                viral.append({"name": hit.get("name") or "", "shot_prompt": text})
    return {"facts": facts, "scene": scene, "viral": viral}


def _related_ids(doc: dict, origin: str) -> set[str]:
    nodes = {node.get("id"): node for node in doc["nodes"]}
    neighbors: dict[str, list[str]] = {}
    for edge in doc["edges"]:
        neighbors.setdefault(edge.get("source"), []).append(edge.get("target"))
        neighbors.setdefault(edge.get("target"), []).append(edge.get("source"))
    seen = set()
    stack = [origin]
    while stack:
        current = stack.pop()
        if not current or current in seen or current not in nodes:
            continue
        if current != origin and nodes[current].get("type") == "variant":
            continue
        seen.add(current)
        stack.extend(neighbors.get(current, []))
    return seen


def _refresh_nodes(product: dict, photos: list[dict], nodes: list[dict], variants: list[dict], scenes: dict, hits: dict | None = None, assets: dict | None = None) -> list[dict]:
    hits = hits or {}
    variant_map = {item["id"]: item for item in variants}
    refreshed = []
    seen_product = False
    for node in nodes:
        if node.get("type") == "white":
            continue
        if node.get("type") == "product":
            fresh = _product_node(product, photos, node.get("position"), _white_assets(assets), _usage_assets(assets), _kind_assets(assets, "detail"))
            fresh["position"] = node.get("position") or fresh["position"]
            refreshed.append(fresh)
            seen_product = True
            continue
        if node.get("type") == "variant":
            variant = variant_map.get(node.get("id"))
            if not variant:
                continue
            scene = scenes.get(variant["scene_id"]) or {"id": variant["scene_id"], "name": variant["label"]}
            fresh = _variant_node(variant, scene, node.get("position"), 0, 0)
            fresh["position"] = node.get("position") or fresh["position"]
            fresh["data"]["prompt"] = variant.get("prompt_text") or ""
            refreshed.append(fresh)
            continue
        if node.get("type") == "facts" and not (node.get("data") or {}).get("custom"):
            data = dict(node.get("data") or {})
            profile = (product.get("profile_text") or "").strip()
            data["title"] = product.get("title") or "未命名"
            data["price"] = product.get("price") or "未填价格"
            data["points"] = profile or (product.get("points") or "").strip() or "还没写卖点"
            data["standard"] = bool(profile)
            node = {**node, "data": data}
        if node.get("type") == "viral":
            data = dict(node.get("data") or {})
            hit = hits.get(data.get("hitId") or "")
            if hit:
                data["label"] = hit.get("name") or data.get("label") or "爆款复刻"
                data["text"] = (hit.get("shot_prompt") or "").strip() or "这条视频的分镜还没整理好"
                data["video"] = file_url(hit.get("path") or "") if hit.get("kind") == "video" else ""
            node = {**node, "data": data}
        if node.get("type") == "scene":
            scene = scenes.get((node.get("data") or {}).get("sceneId"))
            if scene:
                data = dict(node.get("data") or {})
                image = file_url(scene["image_path"])
                data.update({"label": scene["name"], "description": scene["description"], "image": image, "images": [image] if image else []})
                node = {**node, "data": data}
        if node.get("type") == "asset" and assets:
            asset = assets.get(node.get("id"))
            if asset and asset.get("kind") == "person":
                data = dict(node.get("data") or {})
                images = _asset_images(asset)
                data["image"] = images[0] if images else ""
                data["images"] = images
                node = {**node, "data": data}
        refreshed.append(node)
    if not seen_product:
        refreshed.insert(0, _product_node(product, photos, None, _white_assets(assets), _usage_assets(assets), _kind_assets(assets, "detail")))
    return refreshed


def _delete_variants(db, variant_ids: list[str]) -> None:
    for variant_id in variant_ids:
        db.execute("DELETE FROM shots WHERE variant_id = ?", (variant_id,))
        db.execute("DELETE FROM variant_assets WHERE variant_id = ?", (variant_id,))
        db.execute("DELETE FROM variants WHERE id = ?", (variant_id,))


def board_payload(product_id: str) -> dict:
    with connect() as db:
        product = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
        if not product:
            raise LookupError("商品不存在")
    bind_product_photos(product_id)
    with connect() as db:
        variants = [row_dict(row) for row in db.execute("SELECT * FROM variants WHERE product_id = ? ORDER BY created_at", (product_id,))]
        templates = [row_dict(row) for row in db.execute("SELECT id, name, builtin FROM prompt_templates ORDER BY builtin DESC, created_at")]
        default_template = next((item for item in templates if item.get("builtin")), templates[0] if templates else None)
        for variant in variants:
            chosen = variant.get("template_id") or (default_template or {}).get("id") or ""
            named = next((item for item in templates if item["id"] == chosen), default_template)
            variant["template_id"] = (named or {}).get("id") or ""
            variant["template_name"] = (named or {}).get("name") or ""
        scene_ids = list(dict.fromkeys(item["scene_id"] for item in variants))
        scenes = []
        if scene_ids:
            marks = ",".join("?" for _ in scene_ids)
            scenes = [row_dict(row) for row in db.execute(f"SELECT * FROM scenes WHERE id IN ({marks})", scene_ids)]
        assets = [row_dict(row) for row in db.execute("SELECT * FROM assets WHERE product_id = ? ORDER BY created_at", (product_id,))]
        links = [row_dict(row) for row in db.execute(
            """
            SELECT variant_assets.* FROM variant_assets
            JOIN variants ON variants.id = variant_assets.variant_id
            WHERE variants.product_id = ?
            """,
            (product_id,),
        )]
        doc = _load_doc(db, product_id)
        hit_rows = [row_dict(row) for row in db.execute("SELECT id, name, shot_prompt, kind, path FROM hits WHERE product_id = ?", (product_id,))]
    from server.media import is_banner

    photos = [asset for asset in assets if asset["kind"] == "product" and asset.get("path") and not is_banner(asset["path"])]
    asset_map = {asset["id"]: asset for asset in assets}
    if doc["graph"]:
        white_ids = {node.get("id") for node in doc["nodes"] if node.get("type") == "white"}
        edges = [edge for edge in doc["edges"] if edge.get("source") not in white_ids and edge.get("target") not in white_ids]
        stored_nodes = [node for node in doc["nodes"] if node.get("type") != "white"]
        if white_ids:
            doc["nodes"] = stored_nodes
            doc["edges"] = edges
            with connect() as db:
                _store_doc(db, product_id, doc)
        nodes = _refresh_nodes(product, photos, stored_nodes, variants, {scene["id"]: scene for scene in scenes}, {hit["id"]: hit for hit in hit_rows}, asset_map)
        return {"product": _public_product(product), "nodes": nodes, "edges": edges}
    positions = {node.get("id"): node.get("position") for node in doc["nodes"] if node.get("id")}
    steps = list(doc["steps"])
    model = next((asset for asset in reversed(assets) if asset["kind"] == "person"), None)
    references = [asset for asset in assets if asset["kind"] == "reference"]
    nodes = []
    edges = []
    product_node_id = f"product:{product_id}"
    nodes.append(_product_node(product, photos, positions.get(product_node_id), _white_assets(asset_map), _usage_assets(asset_map), _kind_assets(asset_map, "detail")))
    for index, asset in enumerate(references):
        nodes.append(_asset_node(asset, positions.get(asset["id"]), 40, 560 + index * 180))
    scene_by_id = {scene["id"]: scene for scene in scenes}
    grouped: dict[str, list[dict]] = {}
    for variant in variants:
        grouped.setdefault(variant["scene_id"], []).append(variant)
    ordered = []
    for step in steps:
        if step not in ordered:
            ordered.append(step)
    if scene_ids and "scene" not in ordered:
        ordered.append("scene")
    if model and "model" not in ordered:
        ordered.append("model")
    cursor_y = 40
    for step in ordered:
        if step == "facts":
            facts_id = f"facts:{product_id}"
            nodes.append(_facts_node(product, positions.get(facts_id), 520, cursor_y))
            edges.append({"id": f"{product_node_id}-{facts_id}", "source": product_node_id, "target": facts_id, "type": "smoothstep"})
            cursor_y += 220
        elif step == "scene":
            if not scene_ids:
                nodes.append(_slot_node("scene", "场景图", "选一张已有的，或新建", positions.get("slot:scene"), 520, cursor_y))
                edges.append({"id": f"{product_node_id}-slot:scene", "source": product_node_id, "target": "slot:scene", "type": "smoothstep"})
                cursor_y += 220
            else:
                for column, scene_id in enumerate(scene_ids):
                    scene = scene_by_id.get(scene_id)
                    if not scene:
                        continue
                    scene_node_id = f"scene:{scene_id}"
                    nodes.append(_scene_node(scene, positions.get(scene_node_id), 520 + column * 380, cursor_y))
                    edges.append({"id": f"{product_node_id}-{scene_node_id}", "source": product_node_id, "target": scene_node_id, "type": "smoothstep"})
                    for row, variant in enumerate(grouped.get(scene_id, [])):
                        nodes.append(_variant_node(variant, scene, positions.get(variant["id"]), 900 + column * 380, cursor_y + row * 160))
                        edges.append({"id": f"{scene_node_id}-{variant['id']}", "source": scene_node_id, "target": variant["id"], "type": "smoothstep"})
                cursor_y += 280
        elif step == "model":
            if model:
                nodes.append(_asset_node(model, positions.get(model["id"]), 520, cursor_y))
                edges.append({"id": f"{product_node_id}-{model['id']}", "source": product_node_id, "target": model["id"], "type": "smoothstep"})
                for variant in variants:
                    edges.append({"id": f"{model['id']}-{variant['id']}", "source": model["id"], "target": variant["id"], "type": "smoothstep"})
            else:
                nodes.append(_slot_node("model", "模特", "可以不选，只留一个", positions.get("slot:model"), 520, cursor_y))
                edges.append({"id": f"{product_node_id}-slot:model", "source": product_node_id, "target": "slot:model", "type": "smoothstep"})
            cursor_y += 240
    visible = {node["id"] for node in nodes}
    for link in links:
        if link["asset_id"] in visible and link["variant_id"] in {item["id"] for item in variants}:
            edge_id = f"{link['asset_id']}-{link['variant_id']}"
            if not any(edge["id"] == edge_id for edge in edges):
                edges.append({"id": edge_id, "source": link["asset_id"], "target": link["variant_id"], "type": "smoothstep"})
    with connect() as db:
        current = _load_doc(db, product_id)
        if not current["graph"]:
            current["nodes"] = nodes
            current["edges"] = edges
            current["graph"] = 1
            _store_doc(db, product_id, current)
    return {"product": _public_product(product), "nodes": nodes, "edges": edges}


def save_board(product_id: str, nodes: list[dict], edges: list[dict]) -> dict:
    with connect() as db:
        known_variants = {row["id"]: row["scene_id"] for row in db.execute("SELECT id, scene_id FROM variants WHERE product_id = ?", (product_id,))}
        known_assets = {row["id"]: row["kind"] for row in db.execute("SELECT id, kind FROM assets WHERE product_id = ?", (product_id,))}
        doc = _load_doc(db, product_id)
        doc["nodes"] = nodes
        doc["edges"] = _story_edges(nodes, edges)
        doc["graph"] = 1
        _store_doc(db, product_id, doc)
        db.execute("DELETE FROM variant_assets WHERE variant_id IN (SELECT id FROM variants WHERE product_id = ?)", (product_id,))
        for edge in edges:
            source = edge.get("source") or ""
            target = edge.get("target") or ""
            if target not in known_variants or source not in known_assets:
                continue
            if known_assets.get(source) == "product":
                continue
            db.execute("INSERT OR IGNORE INTO variant_assets (variant_id, asset_id) VALUES (?, ?)", (target, source))
    bind_product_photos(product_id)
    return board_payload(product_id)


def _pos(saved, x: float, y: float) -> dict:
    if isinstance(saved, dict) and "x" in saved and "y" in saved:
        return {"x": saved["x"], "y": saved["y"]}
    return {"x": x, "y": y}


def _slot_node(step: str, title: str, hint: str, saved, x: float, y: float) -> dict:
    return {
        "id": f"slot:{step}",
        "type": "slot",
        "position": _pos(saved, x, y),
        "data": {"step": step, "title": title, "hint": hint},
    }


def _generated_ids(shots: list[dict]) -> list[str]:
    ids = []
    for item in shots:
        meta = loads(item.get("meta_json") or "{}", {})
        if isinstance(meta, dict) and meta.get("generated"):
            ids.append(item["id"])
    return ids


def _prior_white_id(shots: list[dict]) -> str:
    generated = _generated_ids(shots)
    if generated:
        return generated[-1]
    return shots[-1]["id"] if shots else ""


def _product_node(product: dict, photos: list[dict], saved, whites: list[dict] | None = None, usages: list[dict] | None = None, details: list[dict] | None = None) -> dict:
    from server.usage import product_info_text

    shots = whites or []
    scenes = usages or []
    closeups = details or []
    return {
        "id": f"product:{product['id']}",
        "type": "product",
        "deletable": True,
        "position": _pos(saved, 40, 80),
        "data": {
            "label": product.get("title") or "这个商品",
            "count": len(photos),
            "images": [file_url(photo["path"]) for photo in photos],
            "photoIds": [photo["id"] for photo in photos],
            "profile": product_info_text(product),
            "whites": [file_url(item["path"]) for item in shots],
            "whiteIds": [item["id"] for item in shots],
            "generatedWhiteIds": _generated_ids(shots),
            "priorWhiteId": _prior_white_id(shots),
            "details": [file_url(item["path"]) for item in closeups],
            "detailIds": [item["id"] for item in closeups],
            "usages": [file_url(item["path"]) for item in scenes],
            "usageIds": [item["id"] for item in scenes],
        },
    }


def _facts_node(product: dict, saved, x: float, y: float) -> dict:
    profile = (product.get("profile_text") or "").strip()
    points = profile or (product.get("points") or "").strip()
    return {
        "id": f"facts:{product['id']}",
        "type": "facts",
        "position": _pos(saved, x, y),
        "data": {
            "title": product.get("title") or "未命名",
            "price": product.get("price") or "未填价格",
            "points": points or "还没写卖点",
            "standard": bool(profile),
        },
    }


def _kind_assets(assets: dict | None, kind: str) -> list[dict]:
    rows = [asset for asset in (assets or {}).values() if asset.get("kind") == kind and asset.get("path")]
    rows.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
    return rows


def _white_assets(assets: dict | None) -> list[dict]:
    return _kind_assets(assets, "white")


def _usage_assets(assets: dict | None) -> list[dict]:
    return _kind_assets(assets, "usage")


def _asset_images(asset: dict) -> list[str]:
    if asset.get("kind") == "reference" or not asset.get("path"):
        return []
    meta = loads(asset.get("meta_json") or "{}", {})
    photos = [file_url(asset["path"])]
    if asset.get("kind") == "person":
        for path in meta.get("references") or []:
            url = file_url(path)
            if url and url not in photos:
                photos.append(url)
    return [url for url in photos if url]


def _asset_node(asset: dict, saved, x: float, y: float) -> dict:
    meta = loads(asset.get("meta_json") or "{}", {})
    images = _asset_images(asset)
    return {
        "id": asset["id"],
        "type": "asset",
        "position": _pos(saved, x, y),
        "data": {
            "kind": asset["kind"],
            "personId": meta.get("person_id") or "",
            "kindLabel": {"product": "产品图", "person": "模特", "reference": "参考视频"}.get(asset["kind"], asset["kind"]),
            "label": asset["label"],
            "image": images[0] if images else "",
            "images": images,
            "detail": meta.get("detail") or meta.get("transcript") or "",
        },
    }


def _scene_node(scene: dict, saved, x: float, y: float) -> dict:
    return {
        "id": f"scene:{scene['id']}",
        "type": "scene",
        "position": _pos(saved, x, y),
        "data": {
            "sceneId": scene["id"],
            "label": scene["name"],
            "description": scene["description"],
            "image": file_url(scene["image_path"]),
            "images": [file_url(scene["image_path"])] if scene.get("image_path") else [],
            "active": bool(scene["active"]),
        },
    }


def _variant_node(variant: dict, scene: dict, saved, x: float, y: float) -> dict:
    return {
        "id": variant["id"],
        "type": "variant",
        "deletable": variant["status"] != "running",
        "position": _pos(saved, x, y),
        "data": {
            "sceneId": scene["id"],
            "label": variant["label"],
            "status": variant["status"],
            "adopted": bool(variant["adopted"]),
            "round": variant["round"],
            "templateId": variant.get("template_id") or "",
            "templateName": variant.get("template_name") or "",
        },
    }


def _public_product(product: dict) -> dict:
    from server.catalog import spec_blocks

    return {
        "id": product["id"],
        "source_url": product["source_url"],
        "title": product["title"],
        "price": product["price"],
        "points": product["points"],
        "profile_text": product.get("profile_text") or "",
        "page_text": product["page_text"],
        "status": product["status"],
        "detail": product["detail"],
        "specs": spec_blocks(product["id"]),
    }
