import asyncio
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ["SELLING_VIDEO_WORKER"] = "0"
os.environ.pop("BOBTOKEN_API_KEY", None)
os.environ.pop("BOBTOKEN_VIDEO_API_KEY", None)
os.environ.pop("BOBTOKEN_VIDEO_MODEL", None)
os.environ.pop("TOAPIS_API_KEY", None)
os.environ.pop("TOAPIS_BASE_URL", None)

TMP = Path(tempfile.mkdtemp())
import server.db as db

db.DATA = TMP
db.FILES = TMP / "files"
db.DB_PATH = TMP / "app.sqlite"

import httpx

from server.db import init_db
from server.main import app


def png() -> io.BytesIO:
    return io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64)


def attach_white(product_id: str) -> None:
    uploaded = request(
        "POST",
        f"/products/{product_id}/white-images",
        files=[("images", ("white.png", png(), "image/png"))],
    )
    if uploaded.status_code != 200:
        raise AssertionError(uploaded.text)


def _png_file(image) -> io.BytesIO:
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    payload.seek(0)
    return payload


def _plain_white_png() -> io.BytesIO:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 240), "white")
    ImageDraw.Draw(image).rectangle((70, 60, 170, 180), fill="black")
    return _png_file(image)


def _detail_sheet_png() -> io.BytesIO:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 240), "white")
    pen = ImageDraw.Draw(image)
    for row in range(3):
        for col in range(3):
            x = 8 + col * 78
            y = 8 + row * 78
            pen.rectangle((x, y, x + 70, y + 70), fill="black")
    return _png_file(image)


def _closeup_png() -> io.BytesIO:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 240), "black")
    ImageDraw.Draw(image).rectangle((40, 40, 200, 200), fill="white")
    return _png_file(image)


def _upload_white(variant_id: str, name: str, payload: io.BytesIO) -> None:
    with db.connect() as conn:
        product_id = conn.execute("SELECT product_id FROM variants WHERE id = ?", (variant_id,)).fetchone()["product_id"]
    uploaded = request(
        "POST",
        f"/products/{product_id}/white-images",
        files=[("images", (name, payload, "image/png"))],
    )
    if uploaded.status_code != 200:
        raise AssertionError(uploaded.text)


def request(method: str, url: str, **kwargs):
    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(_go())


def brief_json(content):
    text = content[0]["text"] if isinstance(content, list) else content
    return json.loads(str(text).split("\n\n上一稿")[0])


def sectioned(description: str, voice: str = "VOZ: Mira este producto de nogal.") -> str:
    spoken = f"VOICE\n{voice}\n" if voice else ""
    return (
        "FORMAT\nVertical 9:16. Total 15 seconds. No subtitles.\n"
        f"SCENE\n{description}\nEnglish direction: the same place.\n"
        "PRODUCT\nAppearance locked to the attached product image.\n"
        "CAST\nNo named presenter.\n"
        "SHOTS\n0-3s | hook | show the product.\n"
        + spoken
    )


class FlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_one_product_picks_a_scene_and_a_person(self):
        scene = request(
            "POST",
            "/scenes",
            data={"name": "窗边", "description": "只取窗边的光，不取图里的杯子"},
            files={"image": ("s.png", png(), "image/png")},
        ).json()
        person = request(
            "POST",
            "/people",
            data={"name": "林"},
            files={"image": ("face.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("POST", f"/products/{product['id']}/images", data={"label": "正面"}, files={"image": ("a.png", png(), "image/png")})
        request("POST", f"/products/{product['id']}/images", data={"label": "侧面"}, files={"image": ("b.png", png(), "image/png")})
        waiting = request("GET", f"/boards/{product['id']}").json()
        self.assertFalse(any(node["id"].startswith("slot:") for node in waiting["nodes"]))
        product_card = next(node for node in waiting["nodes"] if node["type"] == "product")
        self.assertIs(product_card["deletable"], False)
        blocked = request("DELETE", f"/boards/{product['id']}/nodes/{product_card['id']}")
        self.assertEqual(blocked.status_code, 400)
        scene_step = request("POST", f"/boards/{product['id']}/steps", json={"step": "scene"}).json()
        self.assertTrue(any(node["id"] == "slot:scene" for node in scene_step["nodes"]))
        facts = request("POST", f"/boards/{product['id']}/steps", json={"step": "facts"}).json()
        self.assertTrue(any(edge["source"] == product_card["id"] and edge["target"] == f"facts:{product['id']}" for edge in facts["edges"]))
        cleared = request("DELETE", f"/boards/{product['id']}/nodes/facts:{product['id']}").json()
        self.assertFalse(any(node["type"] == "facts" for node in cleared["nodes"]))
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        self.assertTrue(any(edge["source"] == f"product:{product['id']}" and edge["target"] == f"scene:{scene['id']}" for edge in board["edges"]))
        self.assertFalse(any(node["id"] == "slot:scene" for node in board["nodes"]))
        self.assertFalse(any(node["id"] == "slot:model" for node in board["nodes"]))
        product_nodes = [node for node in board["nodes"] if node["type"] == "product"]
        self.assertEqual(len(product_nodes), 1)
        self.assertEqual(product_nodes[0]["data"]["count"], 2)
        self.assertEqual(sum(node["type"] == "asset" and node["data"]["kind"] == "product" for node in board["nodes"]), 0)
        self.assertTrue(any(node["type"] == "scene" and node["data"]["sceneId"] == scene["id"] for node in board["nodes"]))
        cast = request("POST", f"/products/{product['id']}/cast", json={"person_id": person["id"]}).json()
        people = [node for node in cast["nodes"] if node["type"] == "asset" and node["data"]["kind"] == "person"]
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["data"]["personId"], person["id"])
        self.assertFalse(any(node["id"] == "slot:model" for node in cast["nodes"]))
        again = request("POST", f"/products/{product['id']}/cast", json={"person_id": person["id"]}).json()
        self.assertEqual(sum(node["type"] == "asset" and node["data"]["kind"] == "person" for node in again["nodes"]), 1)
        voiced = request("POST", f"/boards/{product['id']}/nodes", json={"type": "voice", "source": f"scene:{scene['id']}", "text": "只说窗边"}).json()
        voice_id = next(node["id"] for node in voiced["nodes"] if node["type"] == "voice")
        note = request("POST", f"/boards/{product['id']}/nodes", json={"type": "note", "source": voice_id, "text": "自定义一句"}).json()
        self.assertTrue(any(node["type"] == "note" for node in note["nodes"]))
        removed = request("DELETE", f"/boards/{product['id']}/nodes/scene:{scene['id']}").json()
        self.assertFalse(any(node["id"] == f"scene:{scene['id']}" for node in removed["nodes"]))
        self.assertTrue(any(node["id"] == voice_id for node in removed["nodes"]))
        self.assertTrue(any(node["type"] == "note" for node in removed["nodes"]))

    def test_scene_requires_image_and_description(self):
        missing = request("POST", "/scenes", data={"name": "开箱", "description": " "}, files={"image": ("a.png", png(), "image/png")})
        self.assertEqual(missing.status_code, 400)
        created = request(
            "POST",
            "/scenes",
            data={"name": "窗边", "description": "只取窗边木桌和白天的光，不取图里的商品"},
            files={"image": ("room.png", png(), "image/png")},
        )
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()["image"])
        extra = request(
            "POST",
            "/scenes",
            data={"name": "对比", "description": "左右并列，只取桌面，不取原来的包装"},
            files={"image": ("b.png", png(), "image/png")},
        )
        self.assertEqual(extra.status_code, 200)
        disabled = request("PATCH", f"/scenes/{extra.json()['id']}", data={"active": "0"})
        self.assertEqual(disabled.status_code, 200)
        renamed = request("PATCH", f"/scenes/{created.json()['id']}", data={"name": "窗边书桌", "description": "只取窗边木桌和白天的光，不取图里的杯子"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["description"], "只取窗边木桌和白天的光，不取图里的杯子")
        active_ids = {item["id"] for item in request("GET", "/scenes").json()}
        self.assertNotIn(extra.json()["id"], active_ids)
        all_ids = {item["id"] for item in request("GET", "/scenes?include_inactive=1").json()}
        self.assertIn(extra.json()["id"], all_ids)

    def test_board_check_generate_and_adopt(self):
        scene = request(
            "POST",
            "/scenes",
            data={"name": "开箱", "description": "双手打开盒盖，暖色桌面，不复制图里的商品"},
            files={"image": ("box.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "音乐盒", "price": "USD 19", "points": "胡桃木盒身"})
        request(
            "POST",
            f"/products/{product['id']}/images",
            data={"label": "闭合"},
            files={"image": ("p.png", png(), "image/png")},
        )
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        product_node = next(node["id"] for node in board["nodes"] if node["type"] == "product")
        scene_node = next(node["id"] for node in board["nodes"] if node["type"] == "scene")
        self.assertTrue(any(edge["source"] == product_node and edge["target"] == scene_node for edge in board["edges"]))
        self.assertTrue(any(edge["source"] == scene_node and edge["target"] == variant_id for edge in board["edges"]))
        request("POST", f"/boards/{product['id']}/nodes", json={"type": "voice", "source": scene_node, "text": "胡桃木"})
        again = request("GET", f"/boards/{product['id']}").json()
        self.assertEqual(sum(node["type"] == "product" for node in again["nodes"]), 1)

        failed = request("POST", f"/variants/{variant_id}/generate")
        self.assertEqual(failed.status_code, 400)
        attach_white(product["id"])
        checked = request("POST", f"/variants/{variant_id}/check").json()
        self.assertEqual(checked["variant"]["status"], "passed", checked["variant"].get("check"))
        self.assertIn("胡桃木", checked["variant"]["prompt_text"])
        queued = request("POST", f"/variants/{variant_id}/generate").json()
        self.assertEqual(queued["variant"]["status"], "queued")
        adopted = request("POST", f"/variants/{variant_id}/adopt").json()
        self.assertEqual(adopted["variant"]["adopted"], 1)
        self.assertEqual(adopted["variant"]["status"], "queued")

        shots = checked["shots"]
        shots[0]["action"] = "画面上加字幕：限时优惠"
        dirty = request("PUT", f"/variants/{variant_id}/shots", json={"shots": shots}).json()
        self.assertEqual(dirty["variant"]["status"], "draft")
        for _ in range(3):
            dirty = request("POST", f"/variants/{variant_id}/check").json()
        self.assertEqual(dirty["variant"]["status"], "needs_human")
        blocked = request("POST", f"/variants/{variant_id}/generate")
        self.assertEqual(blocked.status_code, 400)

    def test_scheduler_holds_until_probed(self):
        with db.connect() as conn:
            conn.execute("UPDATE scheduler SET open = 0, probed = 0")
        state = request("POST", "/scheduler/open").json()
        self.assertEqual(state["open"], 1)
        self.assertEqual(state["probed"], 0)
        closed = request("POST", "/scheduler/close").json()
        self.assertEqual(closed["open"], 0)
        probed = request("POST", "/scheduler/mark-probed").json()
        self.assertEqual(probed["probed"], 1)

    def test_night_queue_returns_video_to_the_same_variant(self):
        scene = request(
            "POST",
            "/scenes",
            data={"name": "夜灯", "description": "只取床头灯光，不取图里的其他商品"},
            files={"image": ("n.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "灯", "price": "10", "points": "暖光"})
        request("POST", f"/products/{product['id']}/images", data={"label": "灯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        attach_white(product["id"])
        self.assertEqual(request("POST", f"/variants/{variant_id}/check").json()["variant"]["status"], "passed")
        request("POST", f"/variants/{variant_id}/generate")
        request("POST", "/scheduler/open")
        import server.media as media
        from server.worker import _night_generate

        media.run_generation = lambda variant_id, output: (output.write_bytes(b"mp4") or True, "ok")
        _night_generate()
        self.assertEqual(request("GET", f"/variants/{variant_id}").json()["variant"]["status"], "queued")
        request("POST", "/scheduler/mark-probed")
        request("POST", "/scheduler/open")
        _night_generate()
        self.assertEqual(_wait_status(variant_id, "returned"), "returned")
        self.assertTrue(request("GET", f"/variants/{variant_id}").json()["variant"]["video"])

    def test_night_queue_runs_two_and_leaves_the_third(self):
        with db.connect() as conn:
            conn.execute("UPDATE scheduler SET open = 1, probed = 1")
        ids = [_queue_variant(f"并列{index}") for index in range(3)]
        import server.media as media
        from server.worker import _night_generate

        started: list[str] = []
        gate = threading.Event()

        def fake(variant_id, output):
            started.append(variant_id)
            gate.wait(3)
            output.write_bytes(b"mp4")
            return True, "ok"

        media.run_generation = fake
        _night_generate()
        deadline = time.time() + 3
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(sorted(started), sorted(ids[:2]))
        self.assertEqual(request("GET", f"/variants/{ids[2]}").json()["variant"]["status"], "queued")
        gate.set()
        self.assertEqual(_wait_status(ids[0], "returned"), "returned")
        self.assertEqual(_wait_status(ids[1], "returned"), "returned")
        self.assertEqual(request("GET", f"/variants/{ids[2]}").json()["variant"]["status"], "queued")

    def test_playwright_keeps_page_facts_and_blank_missing_points(self):
        root = TMP / "shop"
        root.mkdir(exist_ok=True)
        (root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 1500)
        (root / "index.html").write_text(
            "<!doctype html><html><head><meta charset='utf-8'><title>胡桃木音乐盒</title></head><body><p>售价 $19.00</p><img src='pic.png'></body></html>",
            encoding="utf-8",
        )
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root), **kwargs)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            product = request("POST", "/products", json={"url": f"http://127.0.0.1:{server.server_address[1]}/"}).json()
            from server.media import fetch_product

            fetch_product(product["id"])
            saved = request("GET", f"/products/{product['id']}").json()
        finally:
            server.shutdown()
        self.assertEqual(saved["title"], "胡桃木音乐盒")
        self.assertIn("19", saved["price"])
        self.assertEqual(saved["points"], "")
        self.assertIn("售价", saved["page_text"])
        self.assertEqual(saved["status"], "ready")
        self.assertTrue(any(item["kind"] == "product" for item in saved["assets"]))

    def test_prompt_and_hit_each_bind_one_product(self):
        first = request("POST", "/products", json={"url": ""}).json()
        second = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{first['id']}", json={"title": "甲", "price": "1", "points": "甲的卖点"})
        kept = request("POST", "/prompts", json={"name": "甲的说法", "body": "只强调甲的卖点", "product_id": first["id"]}).json()
        moved = request("POST", "/prompts", json={"name": "改挂到甲", "body": "甲只留这一条", "product_id": first["id"]}).json()
        listed = request("GET", "/prompts").json()
        self.assertEqual(next(item["product_id"] for item in listed if item["id"] == kept["id"]), "")
        self.assertEqual(next(item["product_id"] for item in listed if item["id"] == moved["id"]), first["id"])
        rebound = request("PATCH", f"/prompts/{kept['id']}", json={"name": "甲的说法", "body": "只强调甲的卖点", "body_zh": "中文卖点", "tag": "优质", "product_id": second["id"]}).json()
        self.assertEqual(rebound["product_id"], second["id"])
        self.assertEqual(rebound["body_zh"], "中文卖点")
        self.assertEqual(rebound["tag"], "优质")
        scene = request(
            "POST",
            "/scenes",
            data={"name": "提示词对应", "description": "只取桌面，不取图里的其他商品"},
            files={"image": ("p.png", png(), "image/png")},
        ).json()
        request("POST", f"/products/{second['id']}/images", data={"label": "图"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{second['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        attach_white(second["id"])
        checked = request("POST", f"/variants/{variant_id}/check").json()
        self.assertIn("只强调甲的卖点", checked["variant"]["prompt_text"])
        stored = request("GET", "/prompts").json()
        generated = next(item for item in stored if item["product_id"] == second["id"] and item["origin"] == "generated")
        self.assertEqual(generated["scene_name"], "提示词对应")
        self.assertIn("只强调甲的卖点", generated["body"])
        self.assertEqual(next(item for item in stored if item["id"] == rebound["id"])["origin"], "manual")
        hit = request(
            "POST",
            "/hits",
            data={"name": "甲的爆款", "note": "只取推近", "product_id": first["id"]},
            files={"file": ("clip.png", png(), "image/png")},
        ).json()
        other = request(
            "POST",
            "/hits",
            data={"name": "改挂爆款", "note": "只取拉远", "product_id": first["id"]},
            files={"file": ("clip.mp4", b"0" * 64, "video/mp4")},
        ).json()
        hits = request("GET", "/hits").json()
        self.assertEqual(next(item["product_id"] for item in hits if item["id"] == hit["id"]), first["id"])
        self.assertEqual(next(item["product_id"] for item in hits if item["id"] == other["id"]), first["id"])
        self.assertTrue(other["file"])

    def test_hit_link_downloads_onto_the_selected_product(self):
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "链接商品", "price": "10", "points": "暖光"})

        def fake(_url, dest):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"0" * 80)

        from unittest.mock import patch

        with patch("server.media.download_video", side_effect=fake):
            saved = request(
                "POST",
                "/hits",
                data={"name": "链接爆款", "note": "只取推近", "product_id": product["id"], "source_url": "https://www.tiktok.com/@shop/video/1234567890123456789"},
            ).json()
        self.assertEqual(saved["product_id"], product["id"])
        self.assertEqual(saved["kind"], "video")
        self.assertIn("1234567890123456789", saved["note"])
        listed = request("GET", "/hits").json()
        self.assertEqual(sum(item["product_id"] == product["id"] for item in listed), 1)

    def test_bobtoken_reviews_then_saves_one_prompt_for_the_scene(self):
        description = "只取超市冷柜通道的冷白光，不取货架上的原商品"
        scene = request(
            "POST",
            "/scenes",
            data={"name": "冷柜", "description": description},
            files={"image": ("aisle.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "冷柜灯", "price": "USD 19", "points": "胡桃木"})
        request("POST", f"/products/{product['id']}/images", data={"label": "灯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        scene_node = next(node["id"] for node in board["nodes"] if node["type"] == "scene")
        request("POST", f"/boards/{product['id']}/nodes", json={"type": "voice", "source": scene_node, "text": "胡桃木"})
        calls = []

        def fake(messages, timeout=180):
            calls.append(messages)
            if "译成中文" in messages[0]["content"]:
                return "中文译文：冷柜通道"
            if len(calls) == 1:
                return f"ENVIRONMENT\n{description}\nDRAFT"
            return f"ENVIRONMENT\n{description}\nREVISED"

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        os.environ["BOBTOKEN_BASE_URL"] = "https://relay.example/v1/"
        os.environ["BOBTOKEN_MODEL"] = "demo-model"
        try:
            from server.bobtoken import endpoint, model

            self.assertEqual(endpoint(), "https://relay.example/v1/chat/completions")
            self.assertEqual(model(), "demo-model")
            attach_white(product["id"])
            with patch("server.bobtoken.chat", side_effect=fake):
                checked = request("POST", f"/variants/{variant_id}/check").json()
                request("POST", f"/variants/{variant_id}/check")
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
            os.environ.pop("BOBTOKEN_BASE_URL", None)
            os.environ.pop("BOBTOKEN_MODEL", None)
        self.assertEqual(checked["variant"]["status"], "passed", checked["variant"].get("check"))
        self.assertEqual(len(calls), 6)
        self.assertEqual(checked["variant"]["prompt_zh"], "中文译文：冷柜通道")
        self.assertIn("PRODUCT IDENTITY AND PRODUCT COUNT LOCK", calls[0][0]["content"])
        self.assertIn("You are a video prompt quality-control editor", calls[1][0]["content"])
        second = calls[1][-1]["content"]
        second_text = second[0]["text"] if isinstance(second, list) else second
        self.assertIn("DRAFT", second_text)
        payload = brief_json(calls[0][-1]["content"])
        self.assertEqual(payload["scene"]["description"], description)
        self.assertNotIn("people", payload)
        self.assertNotIn("viral", payload)
        self.assertEqual(payload["voice"], "胡桃木")
        self.assertIn("REVISED", checked["variant"]["prompt_text"])
        self.assertNotIn("DRAFT", checked["variant"]["prompt_text"])
        stored = request("GET", "/prompts").json()
        generated_rows = [item for item in stored if item["product_id"] == product["id"] and item["origin"] == "generated"]
        self.assertEqual(len(generated_rows), 2)
        generated = generated_rows[0]
        self.assertEqual(generated["scene_name"], "冷柜")
        self.assertIn("REVISED", generated["body"])
        self.assertEqual(generated["body_zh"], "中文译文：冷柜通道")
        self.assertEqual({item["id"] for item in generated_rows}.__len__(), 2)

    def test_bobtoken_third_failure_is_not_stored(self):
        scene = request(
            "POST",
            "/scenes",
            data={"name": "未通过", "description": "只取收银台台面，不取图里的商品"},
            files={"image": ("desk.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "台面灯", "price": "10", "points": "暖光"})
        request("POST", f"/products/{product['id']}/images", data={"label": "灯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            attach_white(product["id"])
            with patch("server.bobtoken.chat", return_value="画面上加字幕，优惠 999"):
                checked = request("POST", f"/variants/{variant_id}/check").json()
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(checked["variant"]["status"], "passed")
        self.assertIn("画面上加字幕", checked["variant"]["prompt_text"])
        stored = request("GET", "/prompts").json()
        self.assertTrue(any(item["product_id"] == product["id"] and item["origin"] == "generated" for item in stored))

    def test_decimal_shot_times_are_not_treated_as_invented(self):
        from server.bobtoken import _extra_failures

        bundle = {
            "product": {"id": "", "title": "灯", "price": "10", "points": "暖光"},
            "scene": {"description": "只取桌面"},
            "assets": [],
        }
        failures = _extra_failures("0.0-3.0 到 15.0 秒。只取桌面。\nVOZ: Mira este producto ahora.", bundle)
        self.assertFalse(any("数字" in item for item in failures))

    def test_excel_row_attaches_to_the_same_link_and_page_specs_are_stored(self):
        product = request("POST", "/products", json={"url": "https://vt.tiktok.com/ZS9TESTLINK/"}).json()
        from openpyxl import Workbook

        from server.catalog import import_workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "产品详情"
        sheet.append(["封面"])
        sheet.append(["说明"])
        sheet.append([None])
        sheet.append(["序号", "中文品名", "获取状态", "原文标题", "品牌 / 型号", "类别", "核心卖点", "规格参数汇总", "产品尺寸 / 各型号尺寸", "包装尺寸", "重量（类型 / 单位）", "颜色 / 型号 / 规格选项", "包装内容", "适用场景", "卖家", "原始链接", "解析后链接", "缺失项与访问说明"])
        sheet.append([1, "测试扳手", "部分获取", "Llave de prueba", "品牌未获取", "手动工具", "12件套", "驱动：1/4英寸", "15 cm", "页面未提供", "页面未提供", "12件", "收纳盒", "汽修", "Mary", "https://vt.tiktok.com/ZS9TESTLINK/", "https://shop.tiktok.com/mx/pdp/1730000000001", "缺重量"])
        path = TMP / "sample.xlsx"
        book.save(path)
        self.assertEqual(import_workbook(path), 1)
        saved = request("GET", f"/products/{product['id']}").json()
        labels = [item["label"] for item in saved["specs"]]
        self.assertEqual(labels[0], "中文品名")
        self.assertIn("核心卖点", labels)
        self.assertIn("产品尺寸", labels)
        self.assertEqual(saved["points"], "12件套")
        again = import_workbook(path)
        self.assertEqual(again, 1)
        self.assertEqual(sum(item["source_url"] == "https://vt.tiktok.com/ZS9TESTLINK/" for item in request("GET", "/products").json()), 1)

        import server.media as media

        original = media._read_page
        html = """<html><head><title>胡桃木音乐盒</title></head><body><p>售价 $19.00</p>
        <script type="application/json">{"desc_detail":"胡桃木盒身","specifications":[{"name":"尺寸","value":"12 cm"},{"name":"包装尺寸","value":"20 cm"}],"shop_name":"Mary"}</script></body></html>"""
        media._read_page = lambda url: ("胡桃木音乐盒", "售价 $19.00", [], html)
        try:
            fresh = request("POST", "/products", json={"url": "http://shop.example/item"}).json()
            media.fetch_product(fresh["id"])
            crawled = request("GET", f"/products/{fresh['id']}").json()
        finally:
            media._read_page = original
        self.assertEqual(crawled["points"], "胡桃木盒身")
        values = {item["label"]: item["value"] for item in crawled["specs"]}
        self.assertEqual(values["产品尺寸"], "12 cm")
        self.assertEqual(values["包装尺寸"], "20 cm")
        self.assertEqual(values["卖家"], "Mary")
        self.assertNotIn("待补", values)

    def test_output_without_scene_still_checks(self):
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "台灯", "price": "10", "points": "暖光"})
        request("POST", f"/products/{product['id']}/images", data={"label": "灯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("GET", f"/boards/{product['id']}").json()
        product_node = next(node["id"] for node in board["nodes"] if node["type"] == "product")
        made = request("POST", f"/boards/{product['id']}/nodes", json={"type": "output", "source": product_node}).json()
        variant_id = next(node["id"] for node in made["nodes"] if node["type"] == "variant")
        attach_white(product["id"])
        checked = request("POST", f"/variants/{variant_id}/check").json()
        self.assertEqual(checked["variant"]["status"], "passed", checked["variant"].get("check"))
        self.assertTrue(checked["variant"]["prompt_text"])

    def test_standard_profile_drops_numbers_the_page_did_not_give(self):
        product = request("POST", "/products", json={"url": ""}).json()
        with db.connect() as conn:
            conn.execute(
                "UPDATE products SET title = ?, page_text = ?, points = '', status = 'ready' WHERE id = ?",
                ("胡桃木音乐盒", "胡桃木盒身", product["id"]),
            )
        from server.bobtoken import apply_standard_profile

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            with patch("server.bobtoken.chat", return_value='{"card":"功率 900 瓦","selling_points":"功率 900 瓦"}'):
                apply_standard_profile(product["id"])
            missed = request("GET", f"/products/{product['id']}").json()
            self.assertFalse(missed.get("profile_text"))
            self.assertEqual(missed["points"], "")
            with patch("server.bobtoken.chat", return_value='{"card":"胡桃木音乐盒，盒身是胡桃木","selling_points":"胡桃木盒身"}'):
                apply_standard_profile(product["id"])
            saved = request("GET", f"/products/{product['id']}").json()
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(saved["profile_text"], "胡桃木音乐盒，盒身是胡桃木")
        self.assertEqual(saved["points"], "胡桃木盒身")
        kept = request("POST", "/products", json={"url": ""}).json()
        with db.connect() as conn:
            conn.execute(
                "UPDATE products SET title = ?, page_text = ?, points = ?, status = 'ready' WHERE id = ?",
                ("胡桃木音乐盒", "胡桃木盒身", "原卖点", kept["id"]),
            )
        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            with patch("server.bobtoken.chat", return_value='{"card":"胡桃木音乐盒","selling_points":"胡桃木盒身"}'):
                apply_standard_profile(kept["id"])
            again = request("GET", f"/products/{kept['id']}").json()
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(again["points"], "原卖点")
        self.assertEqual(again["profile_text"], "胡桃木音乐盒")
        sized = request("POST", "/products", json={"url": ""}).json()
        with db.connect() as conn:
            conn.execute(
                "UPDATE products SET title = ?, page_text = ?, status = 'ready' WHERE id = ?",
                ("缝纫机", "Dimensiones: 20 cm × 10 cm × 19 cm。Peso 0.65 kg", sized["id"]),
            )
        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            with patch("server.bobtoken.chat", return_value='{"card":"尺寸 20 cm × 10 cm × 19 cm。重量 0.65 kg。功率 900 瓦","selling_points":"迷你","size":"20 cm × 10 cm × 19 cm","package_size":"","weight":"90 g"}'):
                apply_standard_profile(sized["id"])
            measured = request("GET", f"/products/{sized['id']}").json()
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        values = {item["label"]: item["value"] for item in measured["specs"]}
        self.assertEqual(values["产品尺寸"], "20 cm × 10 cm × 19 cm")
        self.assertNotIn("重量", values)
        self.assertIn("20 cm", measured["profile_text"])
        self.assertNotIn("900", measured["profile_text"])

    def test_check_brief_uses_only_the_connected_combination(self):
        description = "只取厨房台面的日光，不取台面上的杯子"
        other = "只取车内杯架，不取屏幕里的画面"
        scene = request(
            "POST",
            "/scenes",
            data={"name": "厨房", "description": description},
            files={"image": ("kitchen.png", png(), "image/png")},
        ).json()
        elsewhere = request(
            "POST",
            "/scenes",
            data={"name": "杯架", "description": other},
            files={"image": ("car.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "滤杯", "price": "20", "points": "仅此卖点标记"})
        request("POST", f"/products/{product['id']}/images", data={"label": "杯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": elsewhere["id"]})
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO hits (id, name, note, path, kind, product_id, shot_prompt, created_at) VALUES (?, ?, '', '', 'video', ?, ?, ?)",
                ("hit_on", "厨房运镜", product["id"], "镜头从左往右横移，再推近到手部", "t1"),
            )
            conn.execute(
                "INSERT INTO hits (id, name, note, path, kind, product_id, shot_prompt, created_at) VALUES (?, ?, '', '', 'video', ?, ?, ?)",
                ("hit_off", "另一条运镜", product["id"], "不要出现这句俯拍全景标记", "t2"),
            )
        made = request("POST", f"/boards/{product['id']}/nodes", json={"type": "viral", "source": variant_id}).json()
        viral_id = next(node["id"] for node in made["nodes"] if node["type"] == "viral")
        request("PATCH", f"/boards/{product['id']}/nodes/{viral_id}", json={"hit_id": "hit_on"})

        def fake(messages, timeout=360):
            return sectioned(description, "VOZ: Mira este producto de cocina.")

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            attach_white(product["id"])
            with patch("server.bobtoken.chat", side_effect=fake) as chat:
                checked = request("POST", f"/variants/{variant_id}/check").json()
                brief = chat.call_args_list[0].args[0][-1]["content"]
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(checked["variant"]["status"], "passed", checked["variant"].get("check"))
        payload = brief_json(brief)
        self.assertEqual(payload["scene"]["description"], description)
        self.assertEqual(payload["viral"][0]["shot_prompt"], "镜头从左往右横移，再推近到手部")
        self.assertNotIn("product", payload)
        self.assertNotIn("people", payload)
        self.assertNotIn("不要出现这句俯拍全景标记", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn(other, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("仅此卖点标记", json.dumps(payload, ensure_ascii=False))
        self.assertIn(description, checked["variant"]["prompt_text"])

    def test_steer_can_drop_voiceover_and_keep_a_constraint(self):
        description = "只取书桌台面的日光，不取闭合的笔记本"
        scene = request(
            "POST",
            "/scenes",
            data={"name": "书桌约束", "description": description},
            files={"image": ("desk.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "台灯", "price": "10", "points": "暖光"})
        request("POST", f"/products/{product['id']}/images", data={"label": "灯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        saved = request(
            "PATCH",
            f"/variants/{variant_id}/steer",
            json={"steer": "商品始终在画面中央", "voiceover": False},
        ).json()
        self.assertEqual(saved["variant"]["voiceover"], 0)
        self.assertEqual(saved["variant"]["steer"], "商品始终在画面中央")

        def fake(messages, timeout=360):
            return sectioned(description, "") + "商品始终在画面中央。"

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            attach_white(product["id"])
            with patch("server.bobtoken.chat", side_effect=fake) as chat:
                checked = request("POST", f"/variants/{variant_id}/check").json()
                brief = chat.call_args_list[0].args[0][-1]["content"]
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(checked["variant"]["status"], "passed", checked["variant"].get("check"))
        payload = brief_json(brief)
        self.assertFalse(payload["voiceover_enabled"])
        self.assertNotIn("voice", payload)
        self.assertEqual(payload["steer"], "商品始终在画面中央")
        self.assertIn("不要写 VOICEOVER", chat.call_args_list[0].args[0][0]["content"])
        self.assertNotIn("VOZ:", checked["variant"]["prompt_text"])
        from server.bobtoken import _extra_failures

        blocked = _extra_failures(
            f"{description}\nVOZ: Mira este producto ahora.",
            {"product": {"id": "", "title": "台灯", "price": "10", "points": "暖光"}, "scene": {"description": description}, "assets": [], "variant": {"voiceover": 0}},
        )
        self.assertTrue(any("不要口播" in item for item in blocked))

    def test_duration_choice_is_saved_and_sent_with_the_prompt(self):
        description = "只取阳台的日光，不取远处的楼"
        scene = request(
            "POST",
            "/scenes",
            data={"name": "阳台时长", "description": description},
            files={"image": ("balcony.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "杯", "price": "8", "points": "轻"})
        request("POST", f"/products/{product['id']}/images", data={"label": "杯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        self.assertEqual(request("GET", f"/variants/{variant_id}").json()["variant"]["duration_sec"], 15)
        saved = request("PATCH", f"/variants/{variant_id}/duration", json={"duration_sec": 30}).json()
        self.assertEqual(saved["variant"]["duration_sec"], 30)
        rejected = request("PATCH", f"/variants/{variant_id}/duration", json={"duration_sec": 12})
        self.assertEqual(rejected.status_code, 400)

        def fake(messages, timeout=360):
            return sectioned(description)

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            attach_white(product["id"])
            with patch("server.bobtoken.chat", side_effect=fake) as chat:
                request("POST", f"/variants/{variant_id}/check")
                brief = chat.call_args_list[0].args[0][-1]["content"]
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(brief_json(brief)["duration_sec"], 30)

    def test_a_cell_can_choose_another_template(self):
        templates = request("GET", "/templates").json()
        builtin = next(item for item in templates if item["builtin"])
        self.assertEqual(builtin["name"], "墨西哥带货")
        self.assertIn("TikTok Shop", builtin["body"])
        created = request("POST", "/templates", json={"name": "只展示产品", "body": "只写产品展示，不要人物故事。"}).json()
        self.assertEqual(created["builtin"], 0)
        scene = request(
            "POST",
            "/scenes",
            data={"name": "模板场景", "description": "只取阳台的日光"},
            files={"image": ("b.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("POST", f"/products/{product['id']}/images", data={"label": "灯"}, files={"image": ("p.png", png(), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant = next(node for node in board["nodes"] if node["type"] == "variant")
        self.assertEqual(variant["data"]["templateName"], "墨西哥带货")
        picked = request("PATCH", f"/variants/{variant['id']}/template", json={"template_id": created["id"]}).json()
        self.assertEqual(picked["variant"]["template_id"], created["id"])
        blocked = request("DELETE", f"/templates/{builtin['id']}")
        self.assertEqual(blocked.status_code, 400)
        request("DELETE", f"/templates/{created['id']}")
        self.assertEqual(request("GET", f"/variants/{variant['id']}").json()["variant"]["template_id"], "")

    def test_speech_extracts_then_a_person_marks_it_normal(self):
        variant_id = _returned_variant("VOZ: Este cepillo seca el cabello rapido.", 1)
        with patch("server.media.spoken_text", return_value=""), patch("server.bobtoken.chat") as chat:
            missed = request("POST", f"/variants/{variant_id}/speech")
        self.assertFalse(chat.called)
        self.assertEqual(missed.status_code, 200, missed.text)
        self.assertIsNone(missed.json()["variant"]["speech_ok"])
        self.assertIn("没有识别到口播", missed.json()["variant"]["speech_note"])
        blocked = request("POST", f"/variants/{variant_id}/speech/approve")
        self.assertEqual(blocked.status_code, 400)
        listed = request("GET", "/speeches").json()
        self.assertTrue(any(item["id"] == variant_id for item in listed))
        self.assertNotIn("scripts", listed[0])

        def fake(messages, timeout=120, temperature=0.2):
            self.assertIsNone(temperature)
            self.assertTrue(all(isinstance(item.get("content"), str) for item in messages))
            self.assertEqual(messages[-1]["content"], "Cepillo secador cinco en uno")
            self.assertNotIn("Este cepillo seca", messages[-1]["content"])
            self.assertIn("翻译成通顺的中文", messages[0]["content"])
            return "五合一吹风机，几分钟就能打理头发。"

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            with patch("server.media.spoken_text", return_value="Cepillo secador cinco en uno"), patch("server.bobtoken.chat", side_effect=fake) as chat:
                checked = request("POST", f"/variants/{variant_id}/speech")
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(chat.call_count, 1)
        self.assertIsNone(checked.json()["variant"]["speech_ok"])
        self.assertEqual(checked.json()["variant"]["speech_text"], "Cepillo secador cinco en uno")
        self.assertIn("五合一", checked.json()["variant"]["speech_note"])
        approved = request("POST", f"/variants/{variant_id}/speech/approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["variant"]["speech_ok"], 1)
        self.assertIn("五合一", approved.json()["variant"]["speech_note"])
        with db.connect() as conn:
            conn.execute("UPDATE variants SET status = 'passed' WHERE id = ?", (variant_id,))
        queued = request("POST", f"/variants/{variant_id}/generate").json()
        self.assertIsNone(queued["variant"]["speech_ok"])
        self.assertEqual(queued["variant"]["speech_text"], "")

    def test_speech_translation_ignores_the_product_prompt(self):
        variant_id = _returned_variant("不要写口播。", 0)
        with patch("server.media.spoken_text", return_value=""), patch("server.bobtoken.chat") as chat:
            quiet = request("POST", f"/variants/{variant_id}/speech")
        self.assertEqual(quiet.status_code, 200, quiet.text)
        self.assertIsNone(quiet.json()["variant"]["speech_ok"])
        self.assertEqual(quiet.json()["variant"]["speech_note"], "没有识别到口播")
        self.assertFalse(chat.called)

        def fake(messages, timeout=120, temperature=0.2):
            self.assertIsNone(temperature)
            self.assertEqual(messages[-1]["content"], "Hola mira este producto")
            self.assertNotIn("不要写口播", messages[-1]["content"])
            return "你好，看这个商品。"

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            with patch("server.media.spoken_text", return_value="Hola mira este producto"), patch("server.bobtoken.chat", side_effect=fake):
                spoken = request("POST", f"/variants/{variant_id}/speech")
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(spoken.status_code, 200, spoken.text)
        self.assertIsNone(spoken.json()["variant"]["speech_ok"])
        self.assertEqual(spoken.json()["variant"]["speech_note"], "你好，看这个商品。")

    def test_chat_retries_when_the_gateway_rejects_the_same_request(self):
        import json
        import urllib.error
        from server.bobtoken import chat

        class Body:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}'

        def rejected():
            return urllib.error.HTTPError(
                "http://test",
                400,
                "bad",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"code":"invalid_request","message":"\\u8bf7\\u6c42\\u53c2\\u6570\\u65e0\\u6548"}}'),
            )

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            with patch("server.bobtoken.time.sleep"), patch(
                "server.bobtoken.urllib.request.urlopen",
                side_effect=[rejected(), Body()],
            ) as opened:
                text = chat([{"role": "user", "content": "hola"}], temperature=None)
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(text, "你好")
        self.assertEqual(opened.call_count, 2)
        sent = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("temperature", sent)

    def test_uploaded_film_stays_off_the_canvas(self):
        product = request("POST", "/products", json={"url": ""}).json()
        before = request("GET", f"/boards/{product['id']}").json()
        uploaded = request(
            "POST",
            "/speeches",
            data={"product_id": product["id"], "name": "手传成片"},
            files={"file": ("clip.mp4", b"x" * 64, "video/mp4")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(uploaded.json()["label"], "手传成片")
        self.assertTrue(uploaded.json()["video"].endswith("/video"))
        listed = request("GET", "/speeches").json()
        self.assertTrue(any(item["id"] == uploaded.json()["id"] and item["product_id"] == product["id"] for item in listed))
        after = request("GET", f"/boards/{product['id']}").json()
        before_ids = [node["id"] for node in before["nodes"] if node["type"] == "variant"]
        after_ids = [node["id"] for node in after["nodes"] if node["type"] == "variant"]
        self.assertEqual(after_ids, before_ids)
        rejected = request("POST", "/speeches", data={"product_id": product["id"]}, files={"file": ("note.txt", b"x" * 64, "text/plain")})
        self.assertEqual(rejected.status_code, 400)

    def test_speech_review_can_reject_tag_and_delete_a_film(self):
        product = request("POST", "/products", json={"url": ""}).json()
        uploaded = request(
            "POST",
            "/speeches",
            data={"product_id": product["id"], "name": "待审成片"},
            files={"file": ("clip.mp4", b"x" * 64, "video/mp4")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        variant_id = uploaded.json()["id"]
        rejected = request("POST", f"/variants/{variant_id}/speech/reject")
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["variant"]["speech_ok"], 0)
        tagged = request("PATCH", f"/variants/{variant_id}/speech/tags", json={"tags": ["tk2", " tk2 ", ""]})
        self.assertEqual(tagged.status_code, 200, tagged.text)
        self.assertEqual(json.loads(tagged.json()["variant"]["speech_tags"]), ["tk2"])
        listed = request("GET", "/speeches").json()
        item = next(row for row in listed if row["id"] == variant_id)
        self.assertEqual(item["speech_ok"], 0)
        self.assertEqual(item["tags"], ["tk2"])
        too_long = request("PATCH", f"/variants/{variant_id}/speech/tags", json={"tags": ["x" * 41]})
        self.assertEqual(too_long.status_code, 400)
        output = db.FILES / "outputs" / f"{variant_id}.mp4"
        self.assertTrue(output.is_file())
        deleted = request("DELETE", f"/speeches/{variant_id}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(output.exists())
        self.assertFalse(any(row["id"] == variant_id for row in request("GET", "/speeches").json()))
        self.assertEqual(request("GET", f"/variants/{variant_id}").status_code, 404)

        canvas_id = _returned_variant("VOZ: hola", 1)
        before = request("GET", f"/boards/{request('GET', f'/variants/{canvas_id}').json()['product']['id']}").json()
        self.assertTrue(any(node["id"] == canvas_id for node in before["nodes"]))
        product_id = request("GET", f"/variants/{canvas_id}").json()["product"]["id"]
        removed = request("DELETE", f"/speeches/{canvas_id}")
        self.assertEqual(removed.status_code, 200, removed.text)
        kept = request("GET", f"/variants/{canvas_id}")
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertEqual(kept.json()["variant"]["output_path"], "")
        self.assertFalse(any(row["id"] == canvas_id for row in request("GET", "/speeches").json()))
        after = request("GET", f"/boards/{product_id}").json()
        self.assertTrue(any(node["id"] == canvas_id for node in after["nodes"]))

    def test_reference_video_becomes_an_asset_without_blocking_the_cell(self):
        product = request("POST", "/products", json={"url": ""}).json()
        created = request(
            "POST",
            f"/products/{product['id']}/reference",
            data={"url": ""},
            files={"video": ("clip.mp4", b"not-a-video", "video/mp4")},
        )
        self.assertEqual(created.status_code, 200)
        from server.worker import _prepare_references

        _prepare_references()
        saved = request("GET", f"/products/{product['id']}").json()
        reference = next(item for item in saved["assets"] if item["kind"] == "reference")
        self.assertFalse(reference["meta"]["pending"])
        self.assertTrue(reference["meta"]["detail"])

    def test_product_card_deletes_and_link_refresh_replaces_its_facts(self):
        old = request("POST", "/products", json={"url": "https://example.com/old"}).json()
        product_id = old["id"]
        image = db.FILES / "products" / product_id / "photo-1.img"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"old-image-bytes")
        with db.connect() as conn:
            conn.execute("UPDATE products SET title = ?, points = ?, status = 'ready' WHERE id = ?", ("旧标题", "旧卖点", product_id))
            conn.execute(
                "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, 'product', '图 1', ?, '{}', ?)",
                ("asset_old_photo", product_id, str(image), "2026-01-01T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO product_specs (product_id, selling_points, updated_at) VALUES (?, ?, ?)",
                (product_id, "旧卖点", "2026-01-01T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO prompts (id, name, body, product_id, created_at) VALUES (?, ?, ?, ?, ?)",
                ("prompt_old", "旧提示词", "旧正文", product_id, "2026-01-01T00:00:00+00:00"),
            )
        missing = request("POST", f"/products/{product_id}/refresh", json={"url": ""})
        self.assertEqual(missing.status_code, 400, missing.text)
        refreshed = request("POST", f"/products/{product_id}/refresh", json={"url": "https://detail.1688.com/offer/1.html"})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["status"], "collecting")
        cleared = request("GET", f"/products/{product_id}").json()
        self.assertEqual(cleared["title"], "")
        self.assertEqual(cleared["specs"], [])
        self.assertFalse(any(item["kind"] == "product" for item in cleared["assets"]))
        self.assertFalse(image.exists())

        import server.media as media

        original = media._read_page
        media._read_page = lambda url: ("新款收纳盒 - 1688.com", "材质：塑料\n售价 ¥29.00", [], "<html></html>")
        try:
            media.fetch_product(product_id)
        finally:
            media._read_page = original
        saved = request("GET", f"/products/{product_id}").json()
        self.assertEqual(saved["source_url"], "https://detail.1688.com/offer/1.html")
        self.assertEqual(saved["title"], "新款收纳盒")
        self.assertEqual(saved["price"], "¥29.00")

        removed = request("DELETE", f"/products/{product_id}")
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(request("GET", f"/products/{product_id}").status_code, 404)
        self.assertFalse(any(item["id"] == "prompt_old" for item in request("GET", "/prompts").json()))
        self.assertEqual(request("DELETE", "/products/prod_missing").status_code, 404)


    def test_amazon_bullets_fill_selling_points_and_title(self):
        from server.catalog import extract_profile
        from server.media import _clean_title, _price

        text = "\n".join([
            "Skip to",
            "About this item",
            "Keyboard shortcuts",
            "✭ 【Quick Stripping】The socket releases fast.",
            "✭ 【Chrome-plated Surface】The steel surface resists rust.",
            "Similar items that may deliver to you quickly",
            "材质：塑料",
            "尺寸：30cm",
            "Quantity：1",
            "售价 $10.99",
            "Pay $0.00 later",
        ])
        profile = extract_profile("<html></html>", text)
        self.assertIn("Quick Stripping", profile["selling_points"])
        self.assertNotIn("Similar items", profile["selling_points"])
        self.assertIn("材质：塑料", profile["specs"])
        self.assertNotIn("Quantity", profile["specs"])
        self.assertEqual(_clean_title("Amazon.com: Kumprohu Socket Set : Tools & Home Improvement"), "Kumprohu Socket Set")
        self.assertEqual(_clean_title("新款收纳盒 - 1688.com"), "新款收纳盒")
        self.assertEqual(_price(text), "$10.99")
        measured = extract_profile(
            "<html></html>",
            "\n".join([
                "Está bonita, el cable del pedal viene si a caso 40 cm de largo",
                "Peso (): 0.65 kg (Ultra ligera)",
                "Dimensiones (××): 20 cm × 10 cm × 19 cm (Diseño mini ergonómico)",
                "Package Dimensions：12 x 8 x 6 cm",
                "Product Dimensions",
                "8.27 x 2.5 x 1.2 inches",
            ]),
        )
        self.assertIn("20 cm × 10 cm × 19 cm", measured["size"])
        self.assertIn("8.27 x 2.5 x 1.2 inches", measured["size"])
        self.assertNotIn("40 cm", measured["size"])
        self.assertIn("0.65 kg", measured["weight"])
        self.assertIn("12 x 8 x 6 cm", measured["package_size"])
        socket = extract_profile(
            "<html></html>",
            "\n".join([
                "Egofine 117 Pieces 1/4 & 3/8 inch Drive Socket Ratchet Wrench Set, Metric/SAE Size, with Bit Socket Set",
                "Specification:",
                "Ratchet wrench: 1/4 (6.3mm interface)",
                "Post: 4 inches - 100mm long",
                "Socket: 4-5-6-7-8-9-10-11-12-13-14mm",
                "Product List:",
                "1*Ratchet wrench",
            ]),
        )
        self.assertIn("4 inches - 100mm long", socket["size"])
        self.assertIn("14mm", socket["size"])
        self.assertNotIn("Egofine", socket["size"])

    def test_prompt_writing_sends_white_images_and_stops_without_them(self):
        import base64

        scene = request(
            "POST",
            "/scenes",
            data={"name": "白底提示词", "description": "只取桌面的光"},
            files={"image": ("desk.png", png(), "image/png")},
        ).json()
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "白底杯", "price": "8", "points": "轻"})
        product_bytes = b"\x89PNG\r\n\x1a\n" + b"P" * 80
        request("POST", f"/products/{product['id']}/images", files={"image": ("live.png", io.BytesIO(product_bytes), "image/png")})
        board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        blocked = request("POST", f"/variants/{variant_id}/check")
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertIn("还没有白底图", blocked.text)
        self.assertFalse((request("GET", f"/variants/{variant_id}").json()["variant"].get("prompt_text") or "").strip())
        calls = []

        def fake(messages, timeout=180):
            calls.append(messages)
            if "译成中文" in str(messages[0]["content"]):
                return "中文"
            return "ENVIRONMENT\nwhite lock\nDONE"

        os.environ["BOBTOKEN_API_KEY"] = "test-key"
        try:
            with patch("server.bobtoken.chat", side_effect=fake):
                still = request("POST", f"/variants/{variant_id}/check")
                self.assertEqual(still.status_code, 400, still.text)
                self.assertEqual(calls, [])
                white_bytes = b"\x89PNG\r\n\x1a\n" + b"W" * 80
                uploaded = request(
                    "POST",
                    f"/products/{product['id']}/white-images",
                    files=[("images", ("front.png", io.BytesIO(white_bytes), "image/png"))],
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                checked = request("POST", f"/variants/{variant_id}/check").json()
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
        self.assertEqual(checked["variant"]["status"], "passed", checked["variant"].get("check"))
        self.assertIn("DONE", checked["variant"]["prompt_text"])
        image_calls = [item for item in calls if isinstance(item[-1]["content"], list)]
        self.assertGreaterEqual(len(image_calls), 2)
        white_b64 = base64.b64encode(white_bytes).decode()
        product_b64 = base64.b64encode(product_bytes).decode()
        for messages in image_calls:
            content = messages[-1]["content"]
            labels = [part.get("text") for part in content if part.get("type") == "text"]
            self.assertIn("白底图", labels)
            self.assertNotIn("产品图片", labels)
            blobs = "".join(part.get("image_url", {}).get("url", "") for part in content if part.get("type") == "image_url")
            self.assertIn(white_b64, blobs)
            self.assertNotIn(product_b64, blobs)

    def test_white_background_photos_stay_on_the_product_card(self):
        product = request("POST", "/products", json={"url": "https://example.com/white"}).json()
        product_id = product["id"]
        request("POST", f"/products/{product_id}/images", files={"image": ("live.png", png(), "image/png")})
        empty = request("POST", f"/products/{product_id}/white-images", files={"images": ("empty.png", io.BytesIO(b"x"), "image/png")})
        self.assertEqual(empty.status_code, 400, empty.text)
        blocked = request("POST", f"/boards/{product_id}/nodes", json={"type": "white", "source": f"product:{product_id}"})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        uploaded = request(
            "POST",
            f"/products/{product_id}/white-images",
            files=[
                ("images", ("front.png", png(), "image/png")),
                ("images", ("side.png", png(), "image/png")),
            ],
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        board = uploaded.json()
        self.assertFalse(any(item["type"] == "white" for item in board["nodes"]))
        card = next(item for item in board["nodes"] if item["type"] == "product")
        self.assertEqual(card["data"]["count"], 1)
        self.assertEqual(len(card["data"]["whiteIds"]), 2)
        self.assertEqual(len(card["data"]["whites"]), 2)
        detail = request("GET", f"/products/{product_id}").json()
        white_files = [item for item in detail["assets"] if item["kind"] == "white"]
        self.assertEqual(len(white_files), 2)
        self.assertTrue(all(Path(item["path"]).exists() for item in white_files))
        kept = card["data"]["whiteIds"][0]
        dropped = card["data"]["whiteIds"][1]
        missing = request("DELETE", f"/products/{product_id}/white-images/missing")
        self.assertEqual(missing.status_code, 404, missing.text)
        trimmed = request("DELETE", f"/products/{product_id}/white-images/{dropped}").json()
        left = next(item for item in trimmed["nodes"] if item["type"] == "product")
        self.assertEqual(left["data"]["whiteIds"], [kept])
        self.assertFalse(any(item["type"] == "white" for item in trimmed["nodes"]))
        refreshed = request("POST", f"/products/{product_id}/refresh", json={"url": "https://detail.1688.com/offer/9.html"})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        after = request("GET", f"/boards/{product_id}").json()
        still = next(item for item in after["nodes"] if item["type"] == "product")
        self.assertEqual(still["data"]["whiteIds"], [kept])
        self.assertEqual(still["data"]["count"], 0)
        self.assertTrue(Path(next(item["path"] for item in request("GET", f"/products/{product_id}").json()["assets"] if item["id"] == kept)).exists())
        request("DELETE", f"/products/{product_id}")
        self.assertFalse((db.FILES / "whites" / product_id).exists())

    def test_long_duration_is_rejected_before_seedance(self):
        variant_id = _passed_variant("超时")
        with db.connect() as conn:
            conn.execute("UPDATE variants SET duration_sec = 30 WHERE id = ?", (variant_id,))
        rejected = request("POST", f"/variants/{variant_id}/generate")
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("15", rejected.json()["detail"])

    def test_a_failed_video_can_be_generated_again(self):
        variant_id = _passed_variant("重试")
        with db.connect() as conn:
            conn.execute("UPDATE variants SET status = 'hold', qa_note = 'Seedance 超时' WHERE id = ?", (variant_id,))
        again = request("POST", f"/variants/{variant_id}/generate")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["variant"]["status"], "queued")
        self.assertEqual(again.json()["variant"]["qa_note"], "正在提交 Seedance 2.0 Fast")

    def test_a_finished_video_can_be_generated_again(self):
        variant_id = _passed_variant("再来")
        with db.connect() as conn:
            conn.execute("UPDATE variants SET status = 'returned', output_path = 'clip.mp4' WHERE id = ?", (variant_id,))
        again = request("POST", f"/variants/{variant_id}/generate")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["variant"]["status"], "queued")

    def test_seedance_fast_submits_the_prompt_and_white_image(self):
        variant_id = _passed_variant("快模型")
        prompt = request("GET", f"/variants/{variant_id}").json()["variant"]["prompt_text"]
        output = db.FILES / "outputs" / f"{variant_id}-direct.mp4"
        calls = []

        class Body:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                if isinstance(self.payload, bytes):
                    return self.payload
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=120):
            calls.append(req)
            if req.method == "POST":
                return Body({"id": "task-9", "status": "queued"})
            if req.full_url.endswith("/video/generations/task-9"):
                return Body({"id": "task-9", "status": "succeeded", "video_url": "https://cdn.example/clip.mp4"})
            if req.full_url == "https://cdn.example/clip.mp4":
                return Body(b"x" * 64)
            raise AssertionError(req.full_url)

        os.environ["BOBTOKEN_API_KEY"] = "chat-key"
        os.environ["BOBTOKEN_VIDEO_API_KEY"] = "video-key"
        try:
            with patch("server.seedance.urllib.request.urlopen", fake_urlopen):
                from server.seedance import render_variant

                ok, detail = render_variant(variant_id, output)
        finally:
            os.environ.pop("BOBTOKEN_API_KEY", None)
            os.environ.pop("BOBTOKEN_VIDEO_API_KEY", None)
        self.assertTrue(ok, detail)
        self.assertEqual(output.read_bytes(), b"x" * 64)
        sent = json.loads(calls[0].data.decode())
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer video-key")
        self.assertEqual(calls[0].method, "POST")
        self.assertTrue(calls[0].full_url.endswith("/video/generations"))
        self.assertEqual(sent["model"], "doubao-seedance-2-0-fast-260128")
        self.assertEqual(sent["prompt"], prompt)
        self.assertEqual(sent["duration"], 15)
        self.assertEqual(sent["metadata"]["ratio"], "9:16")
        self.assertEqual(sent["metadata"]["resolution"], "480p")
        self.assertIs(sent["metadata"]["generate_audio"], True)
        self.assertIs(sent["metadata"]["watermark"], False)
        self.assertTrue(sent["images"][0].startswith("data:image/png;base64,"))

    def test_omni_preview_submits_ten_seconds_at_720p(self):
        variant_id = _passed_variant("全能试看")
        prompt = request("GET", f"/variants/{variant_id}").json()["variant"]["prompt_text"]
        output = db.FILES / "outputs" / f"{variant_id}-omni.mp4"
        calls = []

        class Body:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                if isinstance(self.payload, bytes):
                    return self.payload
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=120):
            calls.append(req)
            if req.method == "POST" and req.full_url.endswith("/videos"):
                return Body({"id": "vid-1", "status": "queued"})
            if req.full_url.endswith("/videos/vid-1"):
                return Body({"id": "vid-1", "status": "succeeded", "video_url": "https://cdn.example/omni.mp4"})
            if req.full_url == "https://cdn.example/omni.mp4":
                return Body(b"x" * 64)
            raise AssertionError(req.full_url)

        _upload_white(variant_id, "plain-white.png", _plain_white_png())
        _upload_white(variant_id, "extra-white.png", _closeup_png())
        _upload_white(variant_id, "angle-detail.png", _detail_sheet_png())
        os.environ["BOBTOKEN_VIDEO_MODEL"] = "gemini-omni-1.1-flash-preview"
        os.environ["TOAPIS_API_KEY"] = "omni-key"
        os.environ["TOAPIS_BASE_URL"] = "https://toapis.example/v1"
        uploaded = []

        def fake_upload(path):
            uploaded.append(Path(path).name)
            return f"https://files.example/{len(uploaded)}.png"

        try:
            with patch("server.seedance._upload_public_image", side_effect=fake_upload):
                with patch("server.seedance.urllib.request.urlopen", fake_urlopen):
                    from server.seedance import render_variant

                    ok, detail = render_variant(variant_id, output)
        finally:
            os.environ.pop("BOBTOKEN_VIDEO_MODEL", None)
            os.environ.pop("TOAPIS_API_KEY", None)
            os.environ.pop("TOAPIS_BASE_URL", None)
        self.assertTrue(ok, detail)
        sent = json.loads(calls[0].data.decode())
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer omni-key")
        self.assertTrue(calls[0].full_url.endswith("/v1/videos"))
        self.assertEqual(sent["model"], "gemini-omni-1.1-flash-preview")
        self.assertEqual(sent["duration"], 10)
        self.assertEqual(sent["resolution"], "720p")
        self.assertEqual(sent["aspect_ratio"], "9:16")
        self.assertEqual(sent["image_urls"], ["https://files.example/1.png", "https://files.example/2.png"])
        with db.connect() as conn:
            labels = [
                conn.execute("SELECT label FROM assets WHERE path LIKE ?", (f"%{name}",)).fetchone()["label"]
                for name in uploaded
            ]
        self.assertEqual(labels, ["plain-white", "angle-detail"])
        self.assertIn("1. white-background product photo", sent["prompt"])
        self.assertIn("2. multi-angle and product-detail sheet on white", sent["prompt"])
        self.assertNotIn("scene photo", sent["prompt"])
        self.assertNotIn("generate_audio", sent)
        self.assertIn("compress it into 10 seconds", sent["prompt"])
        self.assertIn(prompt, sent["prompt"])
        self.assertEqual(output.read_bytes(), b"x" * 64)

    def test_omni_refuses_when_a_required_white_is_missing(self):
        variant_id = _passed_variant("缺细节图")
        _upload_white(variant_id, "plain-white.png", _plain_white_png())
        output = db.FILES / "outputs" / f"{variant_id}-missing.mp4"
        os.environ["BOBTOKEN_VIDEO_MODEL"] = "gemini-omni-1.1-flash-preview"
        os.environ["TOAPIS_API_KEY"] = "omni-key"
        os.environ["TOAPIS_BASE_URL"] = "https://toapis.example/v1"
        try:
            from server.seedance import render_variant

            ok, detail = render_variant(variant_id, output)
        finally:
            os.environ.pop("BOBTOKEN_VIDEO_MODEL", None)
            os.environ.pop("TOAPIS_API_KEY", None)
            os.environ.pop("TOAPIS_BASE_URL", None)
        self.assertFalse(ok)
        self.assertIn("多角度和产品细节", detail)

    def test_generate_click_starts_seedance_without_the_night_window(self):
        variant_id = _passed_variant("立即")
        with db.connect() as conn:
            conn.execute("UPDATE scheduler SET open = 0, probed = 0")
        os.environ["SELLING_VIDEO_WORKER"] = "1"
        try:
            def fake(variant_id, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"mp4" * 20)
                return True, "ok"

            with patch("server.media.run_generation", side_effect=fake):
                started = request("POST", f"/variants/{variant_id}/generate")
                self.assertEqual(started.status_code, 200, started.text)
                self.assertEqual(_wait_status(variant_id, "returned"), "returned")
            self.assertEqual(request("GET", "/scheduler").json()["open"], 0)
        finally:
            os.environ["SELLING_VIDEO_WORKER"] = "0"
        self.assertTrue(request("GET", f"/variants/{variant_id}").json()["variant"]["video"])

    def test_export_materials_downloads_zip(self):
        product = request("POST", "/products", json={"url": ""}).json()
        request("PATCH", f"/products/{product['id']}", json={"title": "素材套筒/测试", "price": "USD 12", "points": "通孔"})
        request("POST", f"/products/{product['id']}/images", data={"label": "正面"}, files={"image": ("a.png", png(), "image/png")})
        attach_white(product["id"])
        exported = request("POST", "/products/export-materials", json={"ids": [product["id"]]})
        empty = request("POST", "/products/export-materials", json={"ids": []})
        self.assertEqual(exported.status_code, 200, exported.text[:300])
        self.assertIn("application/zip", exported.headers["content-type"])
        self.assertIn("attachment", exported.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            names = archive.namelist()
            text = archive.read("TikTok商品素材/素材套筒 测试/产品信息.MD").decode("utf-8")
        self.assertIn(product["id"], text)
        self.assertIn("通孔", text)
        self.assertIn("USD 12", text)
        self.assertTrue(any(name.startswith("TikTok商品素材/素材套筒 测试/商品图片/") and not name.endswith("/") for name in names))
        self.assertTrue(any(name.startswith("TikTok商品素材/素材套筒 测试/白底图/") and not name.endswith("/") for name in names))
        self.assertEqual(empty.status_code, 400)

    def test_export_trims_folder_name_that_would_end_with_a_space(self):
        product = request("POST", "/products", json={"url": ""}).json()
        title = ("A" * 79) + " 尾部说明"
        request("PATCH", f"/products/{product['id']}", json={"title": title, "price": "1", "points": "热风"})
        exported = request("POST", "/products/export-materials", json={"ids": [product["id"]]})
        self.assertEqual(exported.status_code, 200, exported.text[:300])
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            names = archive.namelist()
        folder = next(name.split("/")[1] for name in names if name.startswith("TikTok商品素材/") and name.count("/") >= 2)
        self.assertFalse(folder.endswith(" ") or folder.endswith("."))
        self.assertTrue(any(name.endswith("/商品图片/") for name in names))
        self.assertTrue(any(name.endswith("/产品信息.MD") for name in names))

    def test_usage_sheet_uses_product_facts_and_stays_on_the_card(self):
        from server.usage import parse_usage_brief, render_usage_prompt

        brief = {
            "product_info": {
                "product_name": "Plush cushion",
                "material_and_color": "soft pink plush",
                "style": "cute character cushion",
                "key_features": "about 50 cm tall",
                "included_accessories": "1 cushion",
            },
            "scenarios": {
                "category": "home decor",
                "scenario_1": "on a sofa",
                "scenario_2": "on a bed",
                "scenario_3": "as a gift",
                "scenario_4": "in a nursery",
            },
            "detail_shots": ["face", "ear", "shirt", "feet", "side seam"],
        }
        prompt = render_usage_prompt(brief)
        self.assertIn("Plush cushion", prompt)
        self.assertIn("on a sofa", prompt)
        self.assertIn("side seam", prompt)
        self.assertNotIn("Travel Tumbler", prompt)
        self.assertEqual(parse_usage_brief(json.dumps(brief))["detail_shots"][-1], "side seam")
        with self.assertRaises(ValueError):
            parse_usage_brief("{")

        product = request("POST", "/products", json={"url": ""}).json()
        product_id = product["id"]
        blocked = request("POST", f"/products/{product_id}/usage-images", json={"prompt": "scene", "asset_ids": []})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertIn("还没有产品信息", blocked.text)
        with db.connect() as conn:
            conn.execute("UPDATE products SET profile_text = ? WHERE id = ?", ("法兰绒靠垫", product_id))
        request("POST", f"/products/{product_id}/images", data={"label": "正面"}, files={"image": ("a.png", png(), "image/png")})
        photo_id = next(item["id"] for item in request("GET", f"/products/{product_id}").json()["assets"] if item["kind"] == "product")
        wrong = request("POST", f"/products/{product_id}/usage-images", json={"prompt": prompt, "asset_ids": [photo_id]})
        self.assertEqual(wrong.status_code, 400, wrong.text)
        self.assertIn("只能参考细节图", wrong.text)
        sheet = b"\x89PNG\r\n\x1a\n" + b"1" * 1200
        with patch("server.usage.fetch_relay_image", return_value=sheet):
            missing_white = request("POST", f"/products/{product_id}/detail-images/generate", json={"prompt": "细节", "asset_ids": ["asset_missing"]})
            self.assertEqual(missing_white.status_code, 400, missing_white.text)
            self.assertIn("只能参考商品图或白底图", missing_white.text)
            from_photo = request("POST", f"/products/{product_id}/detail-images/generate", json={"prompt": "细节", "asset_ids": [photo_id]})
            self.assertEqual(from_photo.status_code, 200, from_photo.text[:300])
            request("POST", f"/products/{product_id}/white-images", files=[("images", ("white.png", png(), "image/png"))])
            white_id = next(item["id"] for item in request("GET", f"/products/{product_id}").json()["assets"] if item["kind"] == "white")
            detail = request("POST", f"/products/{product_id}/detail-images/generate", json={"prompt": "细节图", "asset_ids": [white_id]})
            self.assertEqual(detail.status_code, 200, detail.text[:300])
            first = next(item for item in detail.json()["nodes"] if item["type"] == "product")
            self.assertEqual(len(first["data"]["detailIds"]), 1)
            replaced = request("POST", f"/products/{product_id}/detail-images/generate", json={"prompt": "细节图", "asset_ids": [white_id]})
            second = next(item for item in replaced.json()["nodes"] if item["type"] == "product")
            self.assertEqual(len(second["data"]["detailIds"]), 1)
            self.assertNotEqual(second["data"]["detailIds"][0], first["data"]["detailIds"][0])
            created = request("POST", f"/products/{product_id}/usage-images", json={"prompt": prompt, "asset_ids": second["data"]["detailIds"]})
        self.assertEqual(created.status_code, 200, created.text[:300])
        card = next(item for item in created.json()["nodes"] if item["type"] == "product")
        self.assertEqual(len(card["data"]["usageIds"]), 1)
        exported = request("POST", "/products/export-materials", json={"ids": [product_id]})
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            names = archive.namelist()
        self.assertTrue(any("/使用场景图/" in name and not name.endswith("/") for name in names))
        self.assertTrue(any("/细节图/" in name and not name.endswith("/") for name in names))
        asset_id = card["data"]["usageIds"][0]
        removed = request("DELETE", f"/products/{product_id}/usage-images/{asset_id}").json()
        left = next(item for item in removed["nodes"] if item["type"] == "product")
        self.assertEqual(left["data"]["usageIds"], [])
        request("DELETE", f"/products/{product_id}")
        self.assertFalse((db.FILES / "usages" / product_id).exists())
        self.assertFalse((db.FILES / "details" / product_id).exists())

    def test_white_generation_needs_product_info_and_keeps_the_new_photo(self):
        product = request("POST", "/products", json={"url": ""}).json()
        product_id = product["id"]
        blocked = request("POST", f"/products/{product_id}/white-images/generate", json={"prompt": "根据产品信息及上传的图片生成一张白底图", "asset_ids": []})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertIn("还没有产品信息", blocked.text)
        with db.connect() as conn:
            conn.execute("UPDATE products SET profile_text = ? WHERE id = ?", ("法兰绒靠垫，高约50厘米", product_id))
        request("POST", f"/products/{product_id}/images", data={"label": "正面"}, files={"image": ("a.png", png(), "image/png")})
        photo_id = next(item["id"] for item in request("GET", f"/products/{product_id}").json()["assets"] if item["kind"] == "product")
        empty = request("POST", f"/products/{product_id}/white-images/generate", json={"prompt": "生成白底图", "asset_ids": []})
        self.assertEqual(empty.status_code, 400, empty.text)
        self.assertIn("至少留一张参考图", empty.text)
        sheet = b"\x89PNG\r\n\x1a\n" + b"2" * 1200
        with patch("server.usage.fetch_relay_image", return_value=sheet) as fetch:
            missing = request(
                "POST",
                f"/products/{product_id}/white-images/generate",
                json={"prompt": "根据产品信息及上传的图片生成一张白底图\n法兰绒靠垫，高约50厘米", "asset_ids": [photo_id, "missing"]},
            )
            self.assertEqual(missing.status_code, 400, missing.text)
            self.assertIn("参考图不存在", missing.text)
            self.assertFalse(fetch.called)
            extra_ids = []
            for name in ("b.png", "c.png", "d.png"):
                request("POST", f"/products/{product_id}/images", data={"label": name}, files={"image": (name, png(), "image/png")})
            extra_ids = [item["id"] for item in request("GET", f"/products/{product_id}").json()["assets"] if item["kind"] == "product"]
            crowded = request(
                "POST",
                f"/products/{product_id}/white-images/generate",
                json={"prompt": "根据产品信息及上传的图片生成一张白底图\n法兰绒靠垫，高约50厘米", "asset_ids": extra_ids},
            )
            self.assertEqual(crowded.status_code, 400, crowded.text)
            self.assertIn("最多留 3 张", crowded.text)
            created = request(
                "POST",
                f"/products/{product_id}/white-images/generate",
                json={"prompt": "根据产品信息及上传的图片生成一张白底图\n法兰绒靠垫，高约50厘米", "asset_ids": [photo_id]},
            )
        self.assertEqual(created.status_code, 200, created.text[:300])
        card = next(item for item in created.json()["nodes"] if item["type"] == "product")
        self.assertEqual(len(card["data"]["whiteIds"]), 1)
        self.assertEqual(card["data"]["priorWhiteId"], card["data"]["whiteIds"][0])
        self.assertIn("法兰绒靠垫", card["data"]["profile"])
        self.assertIn("根据产品信息及上传的图片生成一张白底图", "根据产品信息及上传的图片生成一张白底图\n" + card["data"]["profile"])

    def test_storyboard_ports_draft_and_generate(self):
        product = request("POST", "/products", json={"url": ""}).json()
        product_id = product["id"]
        request("PATCH", f"/products/{product_id}", json={"title": "靠垫", "price": "10", "points": "约50cm"})
        made = request("POST", f"/boards/{product_id}/nodes", json={"type": "storyboard", "source": f"product:{product_id}"}).json()
        story = next(node for node in made["nodes"] if node["type"] == "storyboard")
        edge = next(item for item in made["edges"] if item["target"] == story["id"])
        self.assertEqual(edge["targetHandle"], "product")
        viral = request("POST", f"/boards/{product_id}/nodes", json={"type": "viral", "source": f"product:{product_id}"}).json()
        viral_id = next(node["id"] for node in viral["nodes"] if node["type"] == "viral")
        extra = [
            *viral["edges"],
            {"id": "dup", "source": f"product:{product_id}", "target": story["id"], "targetHandle": "product", "type": "smoothstep"},
            {"id": "bad", "source": f"product:{product_id}", "target": story["id"], "targetHandle": "person", "type": "smoothstep"},
            {"id": "viral-link", "source": viral_id, "target": story["id"], "targetHandle": "viral", "type": "smoothstep"},
        ]
        saved = request("PUT", f"/boards/{product_id}", json={"nodes": viral["nodes"], "edges": extra}).json()
        handles = [item.get("targetHandle") for item in saved["edges"] if item["target"] == story["id"]]
        self.assertEqual(handles, ["product", "viral"])
        alone = request("POST", f"/boards/{product_id}/nodes", json={"type": "storyboard", "source": ""}).json()
        empty_id = next(node["id"] for node in alone["nodes"] if node["type"] == "storyboard" and node["id"] != story["id"])
        blocked = request("POST", f"/boards/{product_id}/nodes/{empty_id}/storyboard/draft", json={"duration": 10, "shot_count": 2})
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("先接上素材", blocked.text)
        payload = json.dumps({"shots": [{"seconds": 4, "prompt": "第一镜，单镜头。"}, {"seconds": 6, "prompt": "第二镜，单镜头。"}]}, ensure_ascii=False)
        with patch("server.storyboard.chat", return_value=payload):
            drafted = request("POST", f"/boards/{product_id}/nodes/{story['id']}/storyboard/draft", json={"duration": 10, "shot_count": 2, "selected_ids": []}).json()
        drafted_node = next(node for node in drafted["nodes"] if node["id"] == story["id"])
        shots = drafted_node["data"]["shots"]
        self.assertEqual(len(shots), 2)
        self.assertEqual(sum(item["seconds"] for item in shots), 10)
        self.assertIn("第一镜", shots[0]["prompt"])
        self.assertEqual(drafted_node["data"]["videoPrompt"], "")
        attach_white(product_id)
        fresh = request("GET", f"/boards/{product_id}").json()
        white_ids = next(node["data"]["whiteIds"] for node in fresh["nodes"] if node["type"] == "product")
        self.assertTrue(white_ids)

        calls = []

        def fake_image(prompt, paths, **kwargs):
            calls.append(prompt)
            self.assertEqual(len(paths), 1)
            self.assertIn("只输出一张", prompt)
            self.assertIn("第一镜", prompt)
            self.assertIn("第二镜", prompt)
            return b"x" * 1500

        with patch("server.storyboard.fetch_relay_image", side_effect=fake_image), patch("server.storyboard.chat", return_value="整片按镜序切换，商品保持一致。"):
            generated = request(
                "POST",
                f"/boards/{product_id}/nodes/{story['id']}/storyboard/generate",
                json={"duration": 10, "shot_count": 2, "selected_ids": white_ids, "shots": shots},
            )
        self.assertEqual(generated.status_code, 200, generated.text)
        self.assertEqual(len(calls), 1)
        result = next(node for node in generated.json()["nodes"] if node["id"] == story["id"])
        self.assertTrue(result["data"]["image"])
        self.assertEqual(result["data"]["error"], "")
        self.assertIn("镜序", result["data"]["videoPrompt"])
        with patch("server.storyboard.fetch_relay_image", side_effect=RuntimeError("图片生成超时")):
            failed = request(
                "POST",
                f"/boards/{product_id}/nodes/{story['id']}/storyboard/generate",
                json={"duration": 10, "shot_count": 2, "selected_ids": white_ids, "shots": shots},
            ).json()
        failed_node = next(node for node in failed["nodes"] if node["id"] == story["id"])
        self.assertEqual(failed_node["data"]["image"], result["data"]["image"])
        self.assertIn("超时", failed_node["data"]["error"])
        kept = request(
            "POST",
            f"/boards/{product_id}/nodes/{story['id']}/storyboard/generate",
            json={"duration": 10, "shot_count": 2, "selected_ids": [], "shots": shots},
        ).json()
        kept_node = next(node for node in kept["nodes"] if node["id"] == story["id"])
        self.assertEqual(kept_node["data"]["videoPrompt"], "")
        self.assertEqual(kept_node["data"]["shots"][0]["prompt"], shots[0]["prompt"])

    def test_storyboard_video_sends_checked_images_prompt_and_sheet_to_fast(self):
        product = request("POST", "/products", json={"url": ""}).json()
        product_id = product["id"]
        request("PATCH", f"/products/{product_id}", json={"title": "雕像", "price": "10", "points": "树脂"})
        made = request("POST", f"/boards/{product_id}/nodes", json={"type": "storyboard", "source": f"product:{product_id}"}).json()
        story_id = next(node["id"] for node in made["nodes"] if node["type"] == "storyboard")
        attach_white(product_id)
        white_ids = next(node["data"]["whiteIds"] for node in request("GET", f"/boards/{product_id}").json()["nodes"] if node["type"] == "product")
        shots = [{"ordinal": 1, "seconds": 15, "prompt": "车内俯拍，手把雕像悬在两腿之间。"}]
        payload = json.dumps({"shots": [{"seconds": 15, "prompt": shots[0]["prompt"]}]}, ensure_ascii=False)

        def fake_image(prompt, paths, **kwargs):
            return b"x" * 1500

        with patch("server.storyboard.chat", return_value=payload):
            request("POST", f"/boards/{product_id}/nodes/{story_id}/storyboard/draft", json={"duration": 15, "shot_count": 1, "selected_ids": white_ids})
        with patch("server.storyboard.fetch_relay_image", side_effect=fake_image), patch("server.storyboard.chat", return_value="整片按镜序切换。"):
            generated = request(
                "POST",
                f"/boards/{product_id}/nodes/{story_id}/storyboard/generate",
                json={"duration": 15, "selected_ids": white_ids, "shots": shots},
            )
        self.assertEqual(generated.status_code, 200, generated.text)
        seen = {}

        def fake_clip(prompt, images, seconds, output):
            seen["prompt"] = prompt
            seen["count"] = len(images)
            seen["last"] = Path(images[-1]).name
            seen["seconds"] = seconds
            output.write_bytes(b"0" * 80)

        class Immediate:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        with patch("server.seedance.render_fast_clip", side_effect=fake_clip), patch("server.storyboard.threading.Thread", Immediate):
            filmed = request(
                "POST",
                f"/boards/{product_id}/nodes/{story_id}/storyboard/video",
                json={"duration": 15, "selected_ids": white_ids, "shots": shots},
            )
        self.assertEqual(filmed.status_code, 200, filmed.text)
        self.assertEqual(next(item for item in filmed.json()["nodes"] if item["id"] == story_id)["data"]["clipStatus"], "done")
        node = next(item for item in request("GET", f"/boards/{product_id}").json()["nodes"] if item["id"] == story_id)
        self.assertEqual(seen["seconds"], 15)
        self.assertEqual(seen["count"], 2)
        self.assertTrue(seen["last"].endswith(".png"))
        self.assertIn("车内俯拍", seen["prompt"])
        self.assertIn("分镜图", seen["prompt"])
        self.assertIn("整片按镜序切换", seen["prompt"])
        self.assertTrue(node["data"]["clip"])
        self.assertEqual(node["data"]["clipStatus"], "done")
        self.assertIn("480p", node["data"]["clipModel"])
        too_long = request(
            "POST",
            f"/boards/{product_id}/nodes/{story_id}/storyboard/video",
            json={"duration": 30, "selected_ids": white_ids, "shots": shots},
        )
        self.assertEqual(too_long.status_code, 400)
        self.assertIn("15", too_long.text)

    def test_fast_clip_stays_on_seedance_480p_when_omni_is_the_film_model(self):
        image = TMP / "fast-ref.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        output = TMP / "fast-out.mp4"
        calls = []

        class Body:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                if isinstance(self.payload, bytes):
                    return self.payload
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=120):
            calls.append(req)
            if req.method == "POST" and req.full_url.endswith("/video/generations"):
                return Body({"id": "fast-1", "status": "queued"})
            if req.full_url.endswith("/video/generations/fast-1"):
                return Body({"id": "fast-1", "status": "succeeded", "video_url": "https://cdn.example/fast.mp4"})
            if req.full_url == "https://cdn.example/fast.mp4":
                return Body(b"x" * 64)
            raise AssertionError(req.full_url)

        os.environ["BOBTOKEN_VIDEO_MODEL"] = "gemini-omni-1.1-flash-preview"
        os.environ["BOBTOKEN_VIDEO_API_KEY"] = "fast-key"
        os.environ["BOBTOKEN_BASE_URL"] = "https://bob.example/v1"
        try:
            with patch("server.seedance.urllib.request.urlopen", fake_urlopen):
                from server.seedance import render_fast_clip

                render_fast_clip("车内俯拍", [image], 15, output)
        finally:
            os.environ.pop("BOBTOKEN_VIDEO_MODEL", None)
            os.environ.pop("BOBTOKEN_VIDEO_API_KEY", None)
            os.environ.pop("BOBTOKEN_BASE_URL", None)
        sent = json.loads(calls[0].data.decode())
        self.assertEqual(sent["model"], "doubao-seedance-2-0-fast-260128")
        self.assertEqual(sent["metadata"]["resolution"], "480p")
        self.assertEqual(sent["metadata"]["ratio"], "9:16")
        self.assertFalse(sent["metadata"]["generate_audio"])
        self.assertEqual(sent["duration"], 15)
        self.assertIn("车内俯拍", sent["prompt"])
        self.assertEqual(len(sent["images"]), 1)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer fast-key")
        self.assertTrue(calls[0].full_url.startswith("https://bob.example/v1/video/generations"))
        self.assertGreaterEqual(output.stat().st_size, 32)

    def test_jobs_queue_two_reject_duplicates_and_clear_on_restart(self):
        from server.jobs import dismiss_job, enqueue_job, fail_interrupted, list_jobs, pump

        first = request("POST", "/products", json={"url": ""}).json()
        second = request("POST", "/products", json={"url": ""}).json()
        enqueue_job(first["id"], "white", "白底图", {"prompt": "白", "asset_ids": []})
        with self.assertRaises(ValueError) as duplicate:
            enqueue_job(first["id"], "white", "白底图", {"prompt": "白", "asset_ids": []})
        self.assertIn("已经提交", str(duplicate.exception))
        blocked = request("POST", f"/products/{first['id']}/white-images/generate", json={"prompt": "白", "asset_ids": []})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertIn("已经提交", blocked.text)
        enqueue_job(first["id"], "detail", "细节图", {"prompt": "细", "asset_ids": []})
        enqueue_job(second["id"], "usage", "使用场景图", {"prompt": "场", "asset_ids": []})
        made = request("POST", f"/boards/{first['id']}/nodes", json={"type": "storyboard", "source": ""}).json()
        story_ids = [node["id"] for node in made["nodes"] if node["type"] == "storyboard"]
        self.assertGreaterEqual(len(story_ids), 1)
        extra = request("POST", f"/boards/{first['id']}/nodes", json={"type": "storyboard", "source": ""}).json()
        story_ids = [node["id"] for node in extra["nodes"] if node["type"] == "storyboard"]
        self.assertGreaterEqual(len(story_ids), 2)
        enqueue_job(first["id"], "storyboard", "分镜图", {"shots": []}, story_ids[0])
        enqueue_job(first["id"], "storyboard", "分镜图", {"shots": []}, story_ids[1])
        with self.assertRaises(ValueError):
            enqueue_job(first["id"], "storyboard", "分镜图", {"shots": []}, story_ids[0])

        class Hold:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self.target = target

            def start(self):
                return None

        with patch("server.jobs.threading.Thread", Hold):
            pump()
        ours = {first["id"], second["id"]}
        listed = [item for item in request("GET", "/jobs").json() if item["product_id"] in ours]
        self.assertEqual(sum(item["status"] == "running" for item in listed), 2)
        self.assertEqual(sum(item["status"] == "queued" for item in listed), 3)
        running_id = next(item["id"] for item in listed if item["status"] == "running")
        early = request("POST", f"/jobs/{running_id}/dismiss")
        self.assertEqual(early.status_code, 400, early.text)
        with db.connect() as conn:
            raw = conn.execute("SELECT document_json FROM boards WHERE product_id = ?", (first["id"],)).fetchone()
            document = json.loads(raw["document_json"])
            for node in document["nodes"]:
                if node.get("type") == "storyboard":
                    node.setdefault("data", {})["clipStatus"] = "running"
            conn.execute("UPDATE boards SET document_json = ? WHERE product_id = ?", (json.dumps(document, ensure_ascii=False), first["id"]))
        fail_interrupted()
        cleared = request("GET", f"/boards/{first['id']}").json()
        for node in cleared["nodes"]:
            if node["type"] == "storyboard":
                self.assertEqual(node["data"].get("clipStatus") or "", "")
        failed = [item for item in request("GET", "/jobs").json() if item["product_id"] in ours]
        self.assertEqual(len(failed), 5)
        self.assertTrue(all(item["status"] == "failed" for item in failed))
        self.assertTrue(all("接口重启了" in item["error"] for item in failed))
        gone = failed[0]["id"]
        self.assertEqual(request("POST", f"/jobs/{gone}/dismiss").status_code, 200)
        self.assertNotIn(gone, [item["id"] for item in request("GET", "/jobs").json()])
        self.assertEqual(list_jobs(first["id"])[0]["status"], "failed")
        dismiss_job(list_jobs(first["id"])[0]["id"])


def _returned_variant(prompt: str, voiceover: int) -> str:
    scene = request(
        "POST",
        "/scenes",
        data={"name": f"口播场景{voiceover}{len(prompt)}", "description": "只取台面的光"},
        files={"image": ("s.png", png(), "image/png")},
    ).json()
    product = request("POST", "/products", json={"url": ""}).json()
    request("PATCH", f"/products/{product['id']}", json={"title": "吹风机", "price": "10", "points": "五合一"})
    request("POST", f"/products/{product['id']}/images", data={"label": "机身"}, files={"image": ("p.png", png(), "image/png")})
    board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
    variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
    output = db.FILES / "outputs" / f"{variant_id}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"mp4")
    with db.connect() as conn:
        conn.execute(
            "UPDATE variants SET status = 'returned', output_path = ?, prompt_text = ?, voiceover = ? WHERE id = ?",
            (str(output), prompt, voiceover, variant_id),
        )
    return variant_id


def _wait_status(variant_id: str, status: str) -> str:
    last = ""
    deadline = time.time() + 3
    while time.time() < deadline:
        last = request("GET", f"/variants/{variant_id}").json()["variant"]["status"]
        if last == status:
            return last
        time.sleep(0.05)
    return last


def _passed_variant(name: str = "出片") -> str:
    scene = request(
        "POST",
        "/scenes",
        data={"name": name, "description": f"{name}只取桌面灯光，不取图里的其他商品"},
        files={"image": ("n.png", png(), "image/png")},
    ).json()
    product = request("POST", "/products", json={"url": ""}).json()
    request("PATCH", f"/products/{product['id']}", json={"title": name, "price": "10", "points": "暖光"})
    request("POST", f"/products/{product['id']}/images", data={"label": name}, files={"image": ("p.png", png(), "image/png")})
    board = request("POST", f"/boards/{product['id']}/scenes", json={"scene_id": scene["id"]}).json()
    variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
    attach_white(product["id"])
    checked = request("POST", f"/variants/{variant_id}/check")
    if checked.status_code != 200 or checked.json()["variant"]["status"] != "passed":
        raise AssertionError(checked.text)
    return variant_id


def _queue_variant(name: str) -> str:
    variant_id = _passed_variant(name)
    request("POST", f"/variants/{variant_id}/generate")
    return variant_id


if __name__ == "__main__":
    unittest.main()
