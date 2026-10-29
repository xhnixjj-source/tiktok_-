from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

from server.db import FILES, connect, dumps, loads, now


def fetch_product(product_id: str) -> None:
    with connect() as db:
        product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not product:
            return
        url = product["source_url"]
    try:
        title, text, images, html = _read_page(url)
        if _verification_page(title, text):
            # #region agent log
            try:
                import json
                import time
                line = json.dumps({"sessionId": "02d4e0", "hypothesisId": "A", "location": "media.py:fetch_product", "message": "product page is a verification wall", "data": {"productId": product_id, "title": title[:80], "hasPuzzle": "puzzle" in text.lower(), "textLen": len(text)}, "timestamp": int(time.time() * 1000)}, ensure_ascii=False)
                with open(r"d:\AI\debug-02d4e0.log", "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass
            # #endregion
            with connect() as db:
                current = db.execute("SELECT title FROM products WHERE id = ?", (product_id,)).fetchone()
                stored = (current["title"] if current else "") or ""
                if stored in ("", "Security Check"):
                    db.execute(
                        "UPDATE products SET title = '', price = '', points = '', page_text = '', profile_text = '', status = 'needs_input', detail = ? WHERE id = ?",
                        ("TikTok 弹出了验证，商品页面没有读到。请稍后再试。", product_id),
                    )
                else:
                    db.execute(
                        "UPDATE products SET status = 'ready', detail = ? WHERE id = ?",
                        ("这次打开页面时 TikTok 弹出了验证，已保留原来的商品资料。", product_id),
                    )
            return
        dest = FILES / "products" / product_id
        dest.mkdir(parents=True, exist_ok=True)
        saved = []
        for image in images[:12]:
            target = dest / f"photo-{len(saved) + 1}.img"
            if _save_image(image, target) and not is_banner(target):
                saved.append(target)
            else:
                target.unlink(missing_ok=True)
        from server.catalog import extract_profile, save_profile

        profile = extract_profile(html, text)
        page_title = _clean_title(title)
        if page_title:
            profile["original_title"] = page_title
        save_profile(product["id"], profile)
        points = (profile.get("selling_points") or "").strip() or product["points"] or ""
        price = _price(text)
        stored_title = (product["title"] or "").strip()
        raw_title = stored_title.lower().startswith("amazon.") or stored_title in {"Security Check", title, page_title, ""}
        kept_title = (page_title or title or stored_title) if raw_title else stored_title
        status = "ready" if kept_title or saved else "needs_input"
        detail = "" if status == "ready" else "页面上没有读到标题或图片，请手填"
        with connect() as db:
            db.execute(
                "UPDATE products SET title = ?, price = ?, points = ?, page_text = ?, status = ?, detail = ? WHERE id = ?",
                (kept_title, price, points, text[:8000], status, detail, product_id),
            )
            for index, path in enumerate(saved, start=1):
                db.execute(
                    "INSERT INTO assets (id, product_id, kind, label, path, meta_json, created_at) VALUES (?, ?, 'product', ?, ?, '{}', ?)",
                    (_asset_id(), product_id, f"图 {index}", str(path), now()),
                )
        try:
            from server.bobtoken import apply_standard_profile

            apply_standard_profile(product_id)
        except Exception:
            pass
    except Exception as exc:
        with connect() as db:
            db.execute(
                "UPDATE products SET status = 'needs_input', detail = ? WHERE id = ?",
                (f"没有打开页面：{exc}", product_id),
            )


def prepare_reference(asset_id: str) -> None:
    with connect() as db:
        asset = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            return
        meta = {}
        try:
            import json
            meta = json.loads(asset["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        source = meta.get("source_url") or ""
        dest = FILES / "references" / asset_id
        dest.mkdir(parents=True, exist_ok=True)
    try:
        video = dest / "source.mp4"
        if source:
            _run(["yt-dlp", "-o", str(video), source])
        elif not video.exists() and asset["path"]:
            video = Path(asset["path"])
        frames = dest / "frames"
        frames.mkdir(exist_ok=True)
        _run(["ffmpeg", "-y", "-i", str(video), "-vf", "fps=1", str(frames / "frame-%03d.jpg")])
        transcript = _transcribe(video)
        first = next(frames.glob("frame-*.jpg"), None)
        meta.update({"detail": "已抽帧", "transcript": transcript, "frame_dir": str(frames)})
        with connect() as db:
            db.execute(
                "UPDATE assets SET path = ?, meta_json = ? WHERE id = ?",
                (str(first or video), dumps(meta), asset_id),
            )
    except Exception as exc:
        meta["detail"] = str(exc)
        with connect() as db:
            db.execute("UPDATE assets SET meta_json = ? WHERE id = ?", (dumps(meta), asset_id))


def run_generation(variant_id: str, output: Path) -> tuple[bool, str]:
    from server.seedance import render_variant

    return render_variant(variant_id, output)


def _verification_page(title: str, text: str) -> bool:
    blob = f"{title}\n{text}".lower()
    return any(marker in blob for marker in ("security check", "verify to continue", "drag the puzzle", "拖动拼图"))


def _read_page(url: str) -> tuple[str, str, list[str], str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        page.evaluate(
            """() => document.querySelector("#prodDetails, #productDetails_feature_div, #productAttributes, .od-pc-attribute, .detail-attribute")?.scrollIntoView()"""
        )
        page.wait_for_timeout(1200)
        title = page.title() or ""
        focused = page.evaluate(
            """() => {
              const clean = (value) => (value || "").replace(/[\\u200e\\u200f\\u00a0]/g, " ").replace(/[ \\t]+/g, " ").trim();
              const selectors = [
                "#productTitle", "#feature-bullets", "#productFactsDesktopExpander",
                "#productDescription", "#detailBullets_feature_div", "#productDetails_techSpec_section_1",
                "#productOverview_feature_div", "#prodDetails", "#productDetails_feature_div",
                ".title-text", ".od-pc-offer-title",
                "#productAttributes", ".od-pc-attribute", ".detail-attribute", ".offer-attr-list"
              ];
              const parts = [];
              const push = (value) => {
                const text = clean(value);
                if (text && !parts.includes(text)) parts.push(text);
              };
              for (const selector of selectors) {
                const root = document.querySelector(selector);
                if (!root) continue;
                root.querySelectorAll("tr").forEach((row) => {
                  const cells = [...row.querySelectorAll("th,td")].map((cell) => clean(cell.innerText)).filter(Boolean);
                  if (cells.length >= 2) push(cells[0] + "：" + cells.slice(1).join(" "));
                });
                push(root.innerText);
              }
              const price = clean(document.querySelector(".a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-offscreen, .price-text")?.innerText || "");
              return { text: parts.join("\\n"), price };
            }"""
        )
        body = page.inner_text("body")
        chunks = []
        price_text = (focused or {}).get("price") or ""
        if price_text:
            chunks.append(f"售价 {price_text}")
        if (focused or {}).get("text"):
            chunks.append(focused["text"])
        chunks.append(body)
        text = "\n\n".join(chunk for chunk in chunks if chunk)
        images = page.eval_on_selector_all(
            "img",
            """els => els.map(e => {
              const src = e.currentSrc || e.src || "";
              const w = e.naturalWidth || 0;
              const h = e.naturalHeight || 0;
              const short = Math.min(w, h);
              const long = Math.max(w, h);
              if (!src || src.startsWith("data:")) return "";
              if (/logo|icon|sprite|favicon|avatar/i.test(src)) return "";
              if (w && h && (short < 160 || long / short >= 2.4)) return "";
              return src;
            }).filter(Boolean)""",
        )
        html = page.content()
        browser.close()
    return title.strip(), text.strip(), images, html


def _save_image(source: str, target: Path) -> bool:
    try:
        if source.startswith("data:"):
            return False
        with urlopen(source, timeout=20) as response:
            data = response.read(2_000_000)
        if len(data) < 1000:
            return False
        target.write_bytes(data)
        return True
    except Exception:
        return False


def is_banner(path: str | Path) -> bool:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    size = _image_size(data)
    if not size:
        return False
    width, height = size
    short = min(width, height)
    long = max(width, height)
    if short <= 0:
        return True
    return short < 160 or long / short >= 2.4


def _image_size(data: bytes) -> tuple[int, int] | None:
    width = height = 0
    if data.startswith(b"\x89PNG") and len(data) >= 24:
        import struct
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8 " and len(data) >= 30:
            import struct
            width, height = struct.unpack("<HH", data[26:30])
            width, height = width & 0x3FFF, height & 0x3FFF
        elif data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
    if width > 0 and height > 0:
        return width, height
    return None


def _clean_title(title: str) -> str:
    text = re.sub(r"\s+", " ", (title or "")).strip()
    amazon = re.match(r"^Amazon\.[^:]+:\s*(.+?)\s+:\s+[^:]+$", text, flags=re.IGNORECASE)
    if amazon:
        text = amazon.group(1).strip()
    text = re.sub(r"^Amazon\.[^:]+:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s*[-|:：]\s*(?:TikTok Shop|Amazon\.[A-Za-z.]+|1688(?:\.com)?|阿里巴巴).*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _price(text: str) -> str:
    matches = re.findall(r"(?:USD|US\$|\$|￥|¥)\s*\d+(?:\.\d{1,2})?", text)
    cents = [item for item in matches if re.search(r"\.(?!00(?:\D|$))\d", item)]
    chosen = cents[0] if cents else (matches[0] if matches else "")
    return re.sub(r"\s+", "", chosen)


_whisper = None


def spoken_text(video: Path) -> str:
    dest = FILES / "speech" / f"{video.stem}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(dest)])
    if _silent(dest):
        return ""
    return _transcribe_spanish(dest)


def _silent(wav: Path) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    completed = subprocess.run(
        ["ffmpeg", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"max_volume:\s*(-inf|-?\d+(?:\.\d+)?)\s*dB", (completed.stderr or "") + (completed.stdout or ""))
    if not match:
        return False
    if match.group(1) == "-inf":
        return True
    return float(match.group(1)) <= -45


def _transcribe_spanish(audio: Path) -> str:
    global _whisper
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError("本机没有语音识别") from exc
    if _whisper is None:
        _whisper = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, _info = _whisper.transcribe(_wav_samples(audio), language="es")
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip())[:4000]


def _wav_samples(path: Path):
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise RuntimeError("语音文件格式不对")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples)


def _transcribe(video: Path) -> str:
    try:
        return _transcribe_spanish(video)
    except Exception:
        return ""


def _video_proxy() -> str:
    if "VIDEO_PROXY" in os.environ:
        return os.environ["VIDEO_PROXY"].strip()
    return _windows_proxy() or "http://127.0.0.1:7890"


def _windows_proxy() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    server = str(server or "").strip()
    if "=" in server:
        parts = {}
        for item in server.split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            parts[key.strip().lower()] = value.strip()
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return ""
    if not server.startswith(("http://", "https://", "socks5://", "socks4://")):
        server = "http://" + server
    return server


def _tiktok_host(url: str) -> bool:
    host = url.split("/")[2].split("@")[-1].split(":")[0].lower() if "://" in url else ""
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _tiktok_video_id(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else ""


def _tag_attr(html: str, element_id: str, attr: str) -> str:
    match = re.search(rf'<[^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
    if not match:
        return ""
    found = re.search(rf'\b{re.escape(attr)}="([^"]*)"', match.group(0))
    return found.group(1) if found else ""


def _solve_waf(html: str) -> tuple[str, str] | None:
    import base64
    import hashlib
    import json

    encoded = _tag_attr(html, "cs", "class")
    name = _tag_attr(html, "wci", "class")
    if not encoded or not name or "Please wait" not in html:
        return None
    data = json.loads(base64.b64decode(encoded + "==="))
    expected = base64.b64decode(data["v"]["c"])
    digest = hashlib.sha256(base64.b64decode(data["v"]["a"]))
    for index in range(1_000_001):
        number = str(index).encode()
        test = digest.copy()
        test.update(number)
        if test.digest() == expected:
            data["d"] = base64.b64encode(number).decode()
            break
    else:
        raise RuntimeError("TikTok 验证没通过，视频没有下载下来")
    value = base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    return name, value


def _tiktok_page(session, url: str) -> tuple[str, str]:
    response = session.get(url, timeout=40)
    page = response.text or ""
    final = str(response.url)
    solved = _solve_waf(page)
    if solved:
        session.cookies.set(solved[0], solved[1], domain=".tiktok.com")
        response = session.get(final, timeout=40)
        page = response.text or ""
        final = str(response.url)
    if "/login" in final:
        raise RuntimeError("这条视频被 TikTok 标成需要登录才能看，公开链接下不下来")
    if "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in page:
        raise RuntimeError("TikTok 验证没通过，视频没有下载下来")
    return page, final


def _http_urls(value) -> list[str]:
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str) and node.startswith("http") and node not in found:
            found.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key in ("UrlList", "url_list", "PlayAddr", "playAddr"):
                if key in node:
                    walk(node[key])

    walk(value)
    return found


def _tiktok_media_urls(page: str, video_id: str) -> list[str]:
    import json

    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
        page,
    )
    if not match:
        raise RuntimeError("视频没有下载下来")
    chosen: dict | None = None

    def walk(node) -> bool:
        nonlocal chosen
        if isinstance(node, dict):
            play = node.get("playAddr")
            if isinstance(play, str) and play.startswith("http"):
                if not video_id or str(node.get("id") or "") == video_id:
                    chosen = node
                    return bool(video_id)
            for value in node.values():
                if walk(value):
                    return True
        elif isinstance(node, list):
            for item in node:
                if walk(item):
                    return True
        return False

    walk(json.loads(match.group(1)))
    if not chosen:
        raise RuntimeError("这条视频已经不可用")
    ordered: list[str] = []
    ordered.extend(_http_urls(chosen.get("playAddr")))
    bitrates = chosen.get("bitrateInfo") if isinstance(chosen.get("bitrateInfo"), list) else []
    def bitrate(item: dict) -> int:
        try:
            return int(item.get("Bitrate") or 0)
        except (TypeError, ValueError):
            return 0

    ranked = sorted((item for item in bitrates if isinstance(item, dict)), key=bitrate, reverse=True)
    for item in ranked:
        ordered.extend(url for url in _http_urls(item.get("PlayAddr")) if url not in ordered)
    ordered.extend(url for url in _http_urls(chosen.get("downloadAddr")) if url not in ordered)
    if not ordered:
        raise RuntimeError("这条视频已经不可用")
    return ordered


def _tiktok_media_url(page: str, video_id: str) -> str:
    return _tiktok_media_urls(page, video_id)[0]


def _save_remote(session, file_url: str, dest: Path) -> None:
    with session.stream("GET", file_url, timeout=120, headers={"Referer": "https://www.tiktok.com/"}) as response:
        if response.status_code != 200:
            raise RuntimeError("视频没有下载下来")
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if chunk:
                    handle.write(chunk)
    if not dest.exists() or dest.stat().st_size <= 50_000:
        raise RuntimeError("视频没有下载下来")
    if b"ftyp" not in dest.read_bytes()[:12]:
        raise RuntimeError("视频没有下载下来")


def _download_tiktok(url: str, dest: Path, proxy: str) -> None:
    from curl_cffi import requests

    session = requests.Session(impersonate="chrome")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        page, final = _tiktok_page(session, url)
        last = RuntimeError("视频没有下载下来")
        for media in _tiktok_media_urls(page, _tiktok_video_id(final) or _tiktok_video_id(url)):
            try:
                _save_remote(session, media, dest)
                return
            except RuntimeError as exc:
                dest.unlink(missing_ok=True)
                last = exc
        raise last
    except RuntimeError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        text = str(exc).lower()
        if proxy and ("connect" in text or "proxy" in text or "timed out" in text):
            raise RuntimeError("本地代理没有连上，TikTok 视频下不下来") from exc
        raise RuntimeError("视频没有下载下来") from exc


def download_video(url: str, dest: Path) -> None:
    source = (url or "").strip()
    if not source.startswith(("http://", "https://")):
        raise RuntimeError("视频链接需要以 http 开头")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    proxy = _video_proxy()
    if _tiktok_host(source):
        _download_tiktok(source, dest, proxy)
        return
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "40",
        "-f",
        "mp4/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(dest),
        source,
    ]
    if proxy:
        command[3:3] = ["--proxy", proxy]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired as exc:
        dest.unlink(missing_ok=True)
        raise RuntimeError("视频下载超时") from exc
    if dest.exists() and dest.stat().st_size > 50_000:
        return
    dest.unlink(missing_ok=True)
    text = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()
    if "Log in for access" in text or "requiring login" in text:
        reason = "这条视频被 TikTok 标成需要登录才能看，公开链接下不下来"
    elif "Video not available" in text or "status code 10231" in text:
        reason = "这条视频已经不可用"
    elif "Failed to connect" in text and "proxy" in text.lower():
        reason = "本地代理没有连上，TikTok 视频下不下来"
    elif "HTTP Error 403" in text:
        reason = "TikTok 验证没通过，视频没有下载下来"
    else:
        reason = "视频没有下载下来"
    raise RuntimeError(reason)


def _run(command: list[str]) -> None:
    if shutil.which(command[0]) is None:
        raise RuntimeError(f"本机没有 {command[0]}")
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "命令失败")[-500:])


def _asset_id() -> str:
    from server.db import new_id
    return new_id("asset")
