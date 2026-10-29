import base64
import json
import time
from pathlib import Path

import urllib.request
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PNG = ROOT / "data" / "ui-scene.png"
PNG.parent.mkdir(parents=True, exist_ok=True)
PNG.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
SHOT = ROOT / "data" / "ui-failure.png"


def api(method: str, path: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:8790" + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def main() -> None:
    stamp = str(int(time.time()))
    scene_name = f"阳台{stamp}"
    disabled_name = f"对比{stamp}"
    product_name = f"音乐盒{stamp}"
    description = f"只取{scene_name}木桌和午后的光，不取图里的杯子"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(8000)
        page.goto("http://localhost:5174/", wait_until="domcontentloaded")
        page.get_by_role("heading", name="新增场景").wait_for()
        form = page.locator("form.panel").first
        form.get_by_label("名称").fill(scene_name)
        form.get_by_label("描述词").fill(f"只取{scene_name}木桌和白天的光，不取图里的杯子")
        form.locator("input[type=file]").set_input_files(PNG)
        form.get_by_role("button", name="保存").click()
        card = page.locator("article.panel").filter(has_text=scene_name)
        card.wait_for()
        width = card.locator("img").evaluate(
            """img => new Promise(resolve => {
              const done = () => resolve(img.naturalWidth);
              if (img.complete) done();
              else { img.onload = done; img.onerror = () => resolve(0); }
            })"""
        )
        if width < 1:
            raise SystemExit("场景图片没有显示")
        if f"只取{scene_name}木桌和白天的光" not in card.inner_text():
            raise SystemExit("场景描述词没有和图片一起显示")
        card.get_by_label("描述词").fill(description)
        card.get_by_role("button", name="保存修改").click()
        card.get_by_text(description).wait_for()

        form.get_by_label("名称").fill(disabled_name)
        form.get_by_label("描述词").fill("左右并列，只取桌面，不取原来的包装")
        form.locator("input[type=file]").set_input_files(PNG)
        form.get_by_role("button", name="保存").click()
        disabled = page.locator("article.panel").filter(has_text=disabled_name)
        disable = disabled.get_by_role("button", name="停用")
        disable.scroll_into_view_if_needed()
        disable.click()
        disabled.get_by_role("heading", name=f"{disabled_name} · 已停用").wait_for()

        page.get_by_role("button", name="商品").click()
        page.get_by_role("button", name="创建并打开画布").click()
        page.get_by_label("标题").fill(product_name)
        page.get_by_label("价格").fill("USD 19")
        page.get_by_label("卖点").fill("胡桃木盒身")
        page.get_by_role("button", name="保存资料").click()
        page.locator("label").filter(has_text="产品图").locator("input").set_input_files(PNG)
        page.locator(".react-flow__node-asset").wait_for()
        options = page.locator("select").inner_text()
        if disabled_name in options:
            raise SystemExit("停用场景仍出现在新商品里")
        if scene_name not in options:
            raise SystemExit("启用场景没有出现在画布")
        page.locator("select").select_option(label=scene_name)
        page.get_by_role("button", name="加一版").click()
        page.locator(".react-flow__node-scene").wait_for()
        page.locator(".react-flow__node-variant").wait_for()
        scene_text = page.locator(".react-flow__node-scene").inner_text()
        if description not in scene_text:
            raise SystemExit("场景节点没有同时带上描述词")

        product_id = next(item["id"] for item in api("GET", "/products") if item["title"] == product_name)
        board = api("GET", f"/boards/{product_id}")
        asset_id = next(node["id"] for node in board["nodes"] if node["type"] == "asset")
        variant_id = next(node["id"] for node in board["nodes"] if node["type"] == "variant")
        variant = next(node for node in board["nodes"] if node["id"] == variant_id)
        variant["position"] = {"x": variant["position"]["x"] + 48, "y": variant["position"]["y"] + 24}
        api("PUT", f"/boards/{product_id}", {"nodes": board["nodes"], "edges": board["edges"] + [{"source": asset_id, "target": variant_id}]})

        other_browser = browser.new_context(viewport={"width": 1440, "height": 900})
        other = other_browser.new_page()
        other.set_default_timeout(8000)
        other.goto("http://localhost:5174/", wait_until="domcontentloaded")
        other.get_by_role("button", name="商品").click()
        other.locator("article.panel").filter(has_text=product_name).get_by_role("button", name="打开画布").click()
        other.locator(".react-flow__node-scene").wait_for()
        other.locator(".react-flow__edge").nth(1).wait_for()
        moved = api("GET", f"/boards/{product_id}")
        moved_variant = next(node for node in moved["nodes"] if node["id"] == variant_id)
        if moved_variant["position"]["x"] != variant["position"]["x"]:
            raise SystemExit("换浏览器后节点位置丢了")
        if not any(edge["source"] == asset_id and edge["target"] == variant_id for edge in moved["edges"]):
            raise SystemExit("换浏览器后产品和场景没有一起留在这一格")

        other.locator(".react-flow__node-variant").click()
        generate = other.get_by_role("button", name="生成", exact=True)
        if generate.is_enabled():
            raise SystemExit("检查通过前可以点生成")
        other.get_by_role("button", name="检查").click()
        other.get_by_role("heading", name="passed · 第 1 轮").wait_for()
        generate.click()
        other.get_by_role("heading", name="queued · 第 1 轮").wait_for()
        other.get_by_role("button", name="采用这一格").click()
        other.locator(".react-flow__node-variant").get_by_text("已采用").wait_for()
        other_browser.close()
        browser.close()
        print("ui-ok", product_id)


if __name__ == "__main__":
    main()
