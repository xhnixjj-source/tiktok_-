from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from server.catalog import page_facts, save_profile, spec_blocks
from server.checks import _wants_subtitle, all_passed, evaluate, rules_path
from server.db import FILES, connect, loads, row_dict

_STRUCTURAL_NUMBERS = {"0", "1", "2", "3", "9", "10", "15", "16", "720"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv"}
_REJECTED = "审查未通过，未写入提示词库"


def configured() -> bool:
    return bool(os.environ.get("BOBTOKEN_API_KEY", "").strip())


def model() -> str:
    return os.environ.get("BOBTOKEN_MODEL", "claude-opus-5").strip() or "claude-opus-5"


def endpoint() -> str:
    base = os.environ.get("BOBTOKEN_BASE_URL", "https://www.bobtoken.cloud/v1").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _retryable_request(detail: str) -> bool:
    text = detail.lower()
    return "invalid_request" in text or "请求参数无效" in detail


def chat(messages: list[dict], timeout: int = 360, temperature: float | None = 0.2, max_tokens: int | None = None) -> str:
    key = os.environ.get("BOBTOKEN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有配置 BOBTOKEN_API_KEY")
    payload_body = {"model": model(), "messages": messages}
    if temperature is not None:
        payload_body["temperature"] = temperature
    if max_tokens:
        payload_body["max_tokens"] = max_tokens
    body = json.dumps(payload_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint(),
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    payload = None
    system = messages[0].get("content") if messages and isinstance(messages[0].get("content"), str) else ""
    stage = "revise" if "quality-control" in system else "draft"
    image_count = 0
    text_chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    image_count += 1
                else:
                    text_chars += len(str(part.get("text") or ""))
    # #region agent log
    def _agent_log(message, data, hypothesis_id):
        try:
            line = json.dumps({"sessionId": "02d4e0", "hypothesisId": hypothesis_id, "location": "bobtoken.py:chat", "message": message, "data": data, "timestamp": int(time.time() * 1000)}, ensure_ascii=False)
            with open(r"d:\AI\debug-02d4e0.log", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
    _agent_log("chat request", {"stage": stage, "model": model(), "temperature": temperature, "bytes": len(body), "images": image_count, "textChars": text_chars, "messages": len(messages)}, "C")
    # #endregion
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # #region agent log
            _agent_log("chat http error", {"stage": stage, "attempt": attempt, "code": exc.code, "detail": detail[:240]}, "A" if stage == "draft" else "B")
            # #endregion
            if attempt < 2 and exc.code == 400 and _retryable_request(detail):
                time.sleep(0.6)
                continue
            raise RuntimeError(f"BobToken 返回 {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"BobToken 连不上：{exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("BobToken 超时") from exc
    if payload is None:
        raise RuntimeError("BobToken 没有返回提示词")
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("BobToken 没有返回提示词") from exc
    if isinstance(content, list):
        content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    if not text:
        raise RuntimeError("BobToken 没有返回提示词")
    return text


_ZH_SYSTEM = "把下面的视频生成提示词译成中文，供人阅读。保留原来的段落和顺序。VOICEOVER 或 VOZ 后面的台词保持原文，不要翻译。不要加解释，不要加标题，只输出中文译文。"


def translate_prompt_zh(prompt: str) -> str:
    return chat(
        [
            {"role": "system", "content": _ZH_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        timeout=180,
    ).strip()


def review_selling_prompt(bundle: dict) -> tuple[str, str, list[dict], str, int]:
    base_round = int(bundle["variant"]["round"] or 0)
    draft = chat(_messages(bundle, []))
    revised = chat(_revise_messages(bundle, draft))
    return revised, translate_prompt_zh(revised), [], "passed", base_round + 1


def _revise_messages(bundle: dict, draft: str) -> list[dict]:
    from server.templates import REVISE_BODY

    text = "材料：\n" + json.dumps(canvas_elements(bundle), ensure_ascii=False, indent=2) + "\n\n第一稿：\n" + draft.strip()
    content: str | list = text
    images = _element_images(bundle)
    if images:
        content = [{"type": "text", "text": text}, *images]
    return [{"role": "system", "content": REVISE_BODY}, {"role": "user", "content": content}]


def build_brief(bundle: dict) -> str:
    product = bundle["product"]
    scene = bundle["scene"]
    product_id = product.get("id") or ""
    selection = bundle.get("selection")
    use_facts = True if selection is None else bool(selection.get("facts"))
    use_scene = True if selection is None else bool(selection.get("scene"))
    manual = _manual_prompt(product_id)
    description = (scene.get("description") or "").strip()
    voice_on = voice_enabled(bundle)
    steer = ((bundle.get("variant") or {}).get("steer") or "").strip()
    from server.checks import allowed_duration

    seconds = allowed_duration((bundle.get("variant") or {}).get("duration_sec"))
    lines = [
        f"为 TikTok 墨西哥带货视频写一条生成提示词。竖屏 9:16，总时长 {seconds} 秒。"
        + ("必须写出可朗读的墨西哥西班牙语口播，放在 VOZ: 后面，不能写无口播。" if voice_on else "不要写口播，不要出现 VOZ:。"),
        "只使用下面接上的材料。没接上的产品信息、场景、爆款分镜和模特都不要写进提示词。",
        rules_path().read_text(encoding="utf-8").strip(),
    ]
    if use_facts:
        facts = page_facts(product_id)
        missing = [
            block["label"]
            for block in spec_blocks(product_id)
            if block["value"] == "页面未提供" or block["value"].startswith("页面未提供")
        ]
        profile = (product.get("profile_text") or "").strip()
        lines.extend(
            [
                f"商品标题：{product.get('title') or '未填'}",
                f"价格：{product.get('price') or '未填'}",
                f"卖点：{product.get('points') or '未填'}",
                "页面资料（只许使用这些，不要另编尺寸、重量、功率或功效）：",
                facts or "没有更多页面资料。",
                "这些字段页面未提供，不要写成确定值：" + ("、".join(missing) if missing else "没有单独列出。"),
            ]
        )
        if profile:
            lines.append("标准产品信息（措辞用这一份，事实不能超出上面的页面资料）：\n" + profile)
    else:
        lines.append("没有接产品信息。不要写商品卖点、价格、尺寸、重量、功率或功效。")
    if use_scene:
        lines.append(f"场景名称：{scene.get('name') or '未命名'}")
        lines.append("场景描述词必须原样出现在提示词里，不要改写：" + description)
    else:
        lines.append("没有接场景。不要指定拍摄地点。")
    lines.append("分镜（时长要保持，动作可以按已接上的材料收紧）：")
    for shot in bundle["shots"]:
        spoken = f"，口播 {shot.get('voiceover') or '无'}" if voice_on else ""
        lines.append(
            f"第 {shot['ordinal']} 镜 {shot['t_start']}-{shot['t_end']} 秒，运镜 {shot.get('camera') or ''}，动作 {shot.get('action') or ''}{spoken}。"
        )
    lines.append("模特：" + _person_lines(bundle["assets"]))
    if selection is None:
        lines.append("爆款镜头：" + viral_brief(product_id))
    else:
        viral = []
        for item in selection.get("viral") or []:
            if isinstance(item, str) and item.strip():
                viral.append(item.strip())
            elif isinstance(item, dict) and (item.get("shot_prompt") or "").strip():
                viral.append(item["shot_prompt"].strip())
        lines.append("爆款复刻（只采用下面的景别、运镜和动作，不要人脸、声音、音乐和卖点）：\n" + ("\n".join(viral) if viral else "没有爆款视频。"))
    if manual:
        lines.append("手写补充，只能作为额外要求，不能当成新的商品事实：" + manual)
    if steer:
        lines.append("优化要求，必须遵守。不能因此编造页面没有的尺寸、重量、功率、功效或价格：" + steer)
    closing = "不要要求画面出现字幕。不要复制爆款的人脸、声音和音乐。没有模特时不要指定出镜人。"
    if voice_on:
        closing += "口播用资料里已有的事实，写成墨西哥西班牙语，放在 VOZ: 后面。"
    else:
        closing += "不要写口播稿，不要写 VOZ:。"
    lines.append(closing)
    return "\n".join(lines)


def viral_brief(product_id: str) -> str:
    if not product_id:
        return "没有爆款视频。"
    with connect() as db:
        hits = [row_dict(row) for row in db.execute("SELECT * FROM hits WHERE product_id = ?", (product_id,))]
        refs = [
            row_dict(row)
            for row in db.execute("SELECT * FROM assets WHERE product_id = ? AND kind = 'reference'", (product_id,))
        ]
    parts: list[str] = []
    for hit in hits:
        note = " ".join(part for part in ((hit.get("name") or "").strip(), (hit.get("note") or "").strip()) if part)
        if note:
            parts.append("备注只供运镜参考，不要采用里面的卖点和人物：" + note)
        path = Path(hit.get("path") or "")
        if hit.get("kind") == "video" and path.suffix.lower() in _VIDEO_SUFFIXES:
            parts.append(_shot_text(path, hit.get("id") or path.stem))
    for ref in refs:
        meta = loads(ref.get("meta_json") or "{}", {})
        transcript = (meta.get("transcript") or "").strip()
        if transcript:
            parts.append("转写只取运镜和动作，不要声音、人脸、音乐和卖点：" + transcript[:1500])
        path = Path(ref.get("path") or "")
        if path.suffix.lower() in _VIDEO_SUFFIXES:
            parts.append(_shot_text(path, ref.get("id") or path.stem))
    return "\n".join(part for part in parts if part) or "没有爆款视频。"


def _shot_text(video: Path, key: str) -> str:
    if not video.exists():
        return "爆款视频文件不在，没有镜头分析。"
    frames = _frames(video, key)
    if not frames:
        return "没有抽出镜头帧。"
    content: list[dict] = [
        {
            "type": "text",
            "text": "下面是同一条视频按时间抽出的画面。用中文短句写景别、运镜和动作。要写清这一镜有没有人、露脸还是只露手、身体和产品怎么接触。不要写这个人是谁，不要写音乐、口播、字幕和商品卖点。",
        }
    ]
    for frame in frames:
        encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    try:
        text = chat(
            [
                {"role": "system", "content": "你写景别、运镜、动作，以及有没有人出镜、露脸还是只露手。不要写这个人是谁，不要写音乐和卖点。"},
                {"role": "user", "content": content},
            ]
        )
    except RuntimeError as exc:
        return "镜头分析没有完成：" + str(exc)
    return "镜头分析：\n" + text


def _frames(video: Path, key: str) -> list[Path]:
    dest = FILES / "shot-cache" / re.sub(r"[^a-zA-Z0-9_-]", "", key)[:80]
    dest.mkdir(parents=True, exist_ok=True)
    ready = sorted(dest.glob("frame-*.jpg"))
    if ready:
        return ready[:4]
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-vf", "fps=1,scale=480:-1", "-frames:v", "4", str(dest / "frame-%02d.jpg")],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted(dest.glob("frame-*.jpg"))[:4]


def _messages(bundle: dict, feedback: list[str]) -> list[dict]:
    system = _template_body(bundle["variant"].get("template_id") or "").strip()
    text = json.dumps(canvas_elements(bundle), ensure_ascii=False, indent=2)
    if feedback:
        text += "\n\n上一稿没有通过审查。按下面逐条改完，再输出完整提示词：\n" + "\n".join(f"- {item}" for item in feedback)
    content: str | list = text
    images = _element_images(bundle)
    if images:
        content = [{"type": "text", "text": text}, *images]
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def _template_body(template_id: str) -> str:
    from server.templates import DEFAULT_BODY

    with connect() as db:
        row = None
        if template_id:
            row = row_dict(db.execute("SELECT body FROM prompt_templates WHERE id = ?", (template_id,)).fetchone())
        if not row:
            row = row_dict(db.execute("SELECT body FROM prompt_templates WHERE builtin = 1 ORDER BY created_at LIMIT 1").fetchone())
        if not row:
            row = row_dict(db.execute("SELECT body FROM prompt_templates ORDER BY created_at LIMIT 1").fetchone())
    return ((row or {}).get("body") or DEFAULT_BODY).strip()


def canvas_elements(bundle: dict) -> dict:
    selection = bundle.get("selection") or {}
    product = bundle["product"]
    from server.checks import allowed_duration

    seconds = allowed_duration((bundle.get("variant") or {}).get("duration_sec"))
    payload: dict = {"duration_sec": seconds, "aspect": "9:16", "voiceover_enabled": voice_enabled(bundle)}
    if selection.get("facts"):
        product_id = product.get("id") or ""
        missing = [
            block["label"]
            for block in spec_blocks(product_id)
            if block["value"] == "页面未提供" or str(block["value"]).startswith("页面未提供")
        ]
        item = {
            "title": product.get("title") or "",
            "price": product.get("price") or "",
            "points": product.get("points") or "",
        }
        facts = page_facts(product_id)
        profile = (product.get("profile_text") or "").strip()
        if facts:
            item["page_facts"] = facts
        if profile:
            item["profile"] = profile
        if missing:
            item["missing_fields"] = missing
        payload["product"] = item
    if selection.get("scene"):
        scene = bundle.get("scene") or {}
        payload["scene"] = {"name": scene.get("name") or "", "description": (scene.get("description") or "").strip()}
    people = _people(bundle.get("assets") or [])
    if people:
        payload["people"] = people
    viral = [item for item in selection.get("viral") or [] if isinstance(item, dict) and (item.get("shot_prompt") or "").strip()]
    if viral:
        payload["viral"] = viral
    if payload["voiceover_enabled"]:
        voice = (bundle.get("voice_text") or "").strip()
        if voice:
            payload["voice"] = voice
    notes = [text.strip() for text in bundle.get("notes") or [] if text and text.strip()]
    if notes:
        payload["notes"] = notes
    steer = ((bundle.get("variant") or {}).get("steer") or "").strip()
    if steer:
        payload["steer"] = steer
    manual = _manual_prompt(product.get("id") or "")
    if manual:
        payload["library"] = manual
    return payload


def _people(assets: list[dict]) -> list[dict]:
    people = []
    with connect() as db:
        for asset in assets:
            if asset.get("kind") != "person":
                continue
            meta = loads(asset.get("meta_json") or "{}", {})
            note = ""
            person_id = meta.get("person_id") or ""
            if person_id:
                row = row_dict(db.execute("SELECT note FROM people WHERE id = ?", (person_id,)).fetchone())
                note = ((row or {}).get("note") or "").strip()
            item = {"name": (asset.get("label") or "").strip() or "未命名模特"}
            if note:
                item["note"] = note
            people.append(item)
    return people


def _element_images(bundle: dict) -> list[dict]:
    selection = bundle.get("selection") or {}
    parts: list[dict] = []
    for asset in bundle.get("whites") or []:
        image = _image_part(Path(asset.get("path") or ""))
        if not image:
            continue
        parts.append({"type": "text", "text": "白底图"})
        parts.append(image)
    if selection.get("scene"):
        image = _image_part(Path((bundle.get("scene") or {}).get("image_path") or ""))
        if image:
            parts.append({"type": "text", "text": "场景图片"})
            parts.append(image)
    for asset in bundle.get("assets") or []:
        if asset.get("kind") != "person":
            continue
        image = _image_part(Path(asset.get("path") or ""))
        if not image:
            continue
        parts.append({"type": "text", "text": "人物图片：" + ((asset.get("label") or "").strip() or "模特")})
        parts.append(image)
    return parts


def _has_section(prompt: str, name: str) -> bool:
    return re.search(rf"(?m)^#+\s*{name}\s*$|^{name}\s*$", prompt) is not None


def _failed(checks: list[dict]) -> list[str]:
    lines = []
    for item in checks:
        if item["ok"]:
            continue
        detail = (item.get("detail") or "").strip()
        lines.append(item["name"] + (f"：{detail}" if detail else ""))
    return lines


def _requests_subtitle(prompt: str) -> bool:
    cleaned = prompt
    for phrase in (
        "不要加字幕",
        "不要显示字幕",
        "不要字幕",
        "不要在画面上写字幕",
        "do not add subtitle",
        "don't add subtitle",
        "do not render subtitles",
        "no subtitle",
        "no on-screen text",
        "without subtitle",
    ):
        cleaned = re.sub(re.escape(phrase), "", cleaned, flags=re.IGNORECASE)
    return _wants_subtitle(cleaned)


def _extra_failures(prompt: str, bundle: dict) -> list[str]:
    failures = []
    for name in ("FORMAT", "SCENE", "PRODUCT", "CAST", "SHOTS"):
        if not _has_section(prompt, name):
            failures.append(f"补上单独一行的段落标题 {name}")
    if voice_enabled(bundle):
        if not _has_section(prompt, "VOICE"):
            failures.append("补上单独一行的段落标题 VOICE")
    elif _has_section(prompt, "VOICE"):
        failures.append("已要求不要口播，删掉 VOICE 段")
    if _requests_subtitle(prompt):
        failures.append("删掉画面字幕和屏幕文字要求，字幕后期再加")
    product = bundle["product"]
    scene = (bundle["scene"].get("description") or "").strip()
    allowed = _numbers(
        " ".join(
            [
                product.get("title") or "",
                product.get("price") or "",
                product.get("points") or "",
                page_facts(product.get("id") or ""),
                _manual_prompt(product.get("id") or ""),
                scene,
            ]
        )
    ) | _STRUCTURAL_NUMBERS
    invented = sorted(_numbers(prompt) - allowed, key=float)
    if invented:
        failures.append("这些数字资料里没有，删掉：" + "、".join(invented))
    if not any(asset.get("kind") == "person" for asset in bundle["assets"]):
        if re.search(r"(模特名叫|出镜人[:：]|presenter named)", prompt, re.IGNORECASE):
            failures.append("没有绑定模特，不要指定具体出镜人")
    if voice_enabled(bundle):
        if re.search(r"无口播|不使用口播|不要口播|不配口播|no voiceover|without voiceover", prompt, re.IGNORECASE):
            failures.append("必须写出墨西哥西班牙语口播，不能取消口播")
        voice = re.search(r"VOZ:\s*(.+)", prompt, re.IGNORECASE | re.DOTALL)
        words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}", voice.group(1)) if voice else []
        if voice is None or len(words) < 4:
            failures.append("加上以 VOZ: 开头的墨西哥西班牙语口播，至少两句")
    elif re.search(r"VOZ\s*:", prompt, re.IGNORECASE):
        failures.append("已要求不要口播，删掉 VOZ 和口播稿")
    return failures


def translate_spoken(transcript: str) -> str:
    if not configured():
        raise RuntimeError("没有配置 BOBTOKEN_API_KEY")
    text = chat(
        [
            {"role": "system", "content": "把口播原文翻译成通顺的中文。只输出译文，不要评价，不要对照商品或提示词。"},
            {"role": "user", "content": transcript.strip()},
        ],
        timeout=120,
        temperature=None,
    )
    return text.strip()[:2000]


def review_spoken(transcript: str, script: str, facts: str) -> tuple[bool, str]:
    if not configured():
        raise RuntimeError("没有配置 BOBTOKEN_API_KEY")
    raw = chat(
        [
            {
                "role": "system",
                "content": "你只检查口播文字。不要评价画面、字幕、运镜或人物。只输出 JSON：{\"ok\": true或false, \"reason\": \"一句中文\"}。",
            },
            {
                "role": "user",
                "content": (
                    "识别出的口播：\n"
                    + transcript.strip()
                    + "\n\n应说的口播稿：\n"
                    + (script.strip() or "没有口播稿")
                    + "\n\n页面上已有的资料：\n"
                    + (facts.strip() or "没有更多资料")
                    + "\n\n判断识别文字是否在说这段口播稿，并且没有编出资料里没有的尺寸、重量、功率、功效或价格。意思接近即可，不要求逐字相同。"
                ),
            },
        ],
        timeout=120,
    )
    data = _json_object(raw)
    if "ok" not in data:
        return False, "口播检查没有返回结果"
    ok = data.get("ok") is True
    reason = str(data.get("reason") or "").strip()
    if not reason:
        reason = "口播和稿子一致" if ok else "口播和稿子不一致"
    return ok, reason[:500]


def voice_enabled(bundle: dict) -> bool:
    value = (bundle.get("variant") or {}).get("voiceover")
    if value is None or value == "":
        return True
    return bool(int(value))


def _numbers(text: str) -> set[str]:
    found = set()
    for item in re.findall(r"\d+(?:\.\d+)?", text or ""):
        value = float(item)
        if value.is_integer():
            found.add(str(int(value)))
        else:
            found.add(format(value, "f").rstrip("0").rstrip("."))
    return found


def _manual_prompt(product_id: str) -> str:
    if not product_id:
        return ""
    with connect() as db:
        row = row_dict(
            db.execute(
                "SELECT body FROM prompts WHERE product_id = ? AND COALESCE(origin, 'manual') = 'manual'",
                (product_id,),
            ).fetchone()
        )
    return ((row or {}).get("body") or "").strip()


def _person_lines(assets: list[dict]) -> str:
    people = [asset for asset in assets if asset.get("kind") == "person"]
    if not people:
        return "无模特。不要指定具体出镜人。"
    lines = []
    with connect() as db:
        for asset in people:
            meta = loads(asset.get("meta_json") or "{}", {})
            note = ""
            person_id = meta.get("person_id") or ""
            if person_id:
                row = row_dict(db.execute("SELECT note FROM people WHERE id = ?", (person_id,)).fetchone())
                note = ((row or {}).get("note") or "").strip()
            label = (asset.get("label") or "").strip() or "未命名模特"
            lines.append(label + (f"。{note}" if note else ""))
    return "\n".join(lines)


def _json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _grounded(text: str, source: str) -> str:
    text = (text or "").strip()
    if not text or text == "页面未提供" or text.startswith("页面未提供"):
        return ""
    allowed = _numbers(source) | _STRUCTURAL_NUMBERS
    kept = []
    for piece in re.split(r"\n+|。", text):
        line = piece.strip()
        if not line:
            continue
        if _numbers(line) - allowed:
            continue
        kept.append(line)
    return "\n".join(kept)


def _measurements_covered(profile: str, *fields: str) -> bool:
    if not (profile or "").strip():
        return False
    needed = set()
    for field in fields:
        if not field or field.startswith("页面未提供"):
            continue
        needed |= _numbers(field)
    needed -= _STRUCTURAL_NUMBERS
    if not needed:
        return True
    return needed <= (_numbers(profile) | _STRUCTURAL_NUMBERS)


def _image_part(path: Path) -> dict | None:
    if not path.exists():
        return None
    size = path.stat().st_size
    if size < 32 or size > 1_500_000:
        return None
    raw = path.read_bytes()
    if raw.startswith(b"\x89PNG"):
        mime = "image/png"
    elif raw.startswith(b"RIFF"):
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def standardize_product(title: str, price: str, page_text: str, points: str, facts: str, image_paths: list[str]) -> tuple[str, str, str, str, str]:
    content: list[dict] = [
        {
            "type": "text",
            "text": "\n".join(
                [
                    "把下面的商品页面和商品图整理成一份标准产品信息。",
                    "只能使用页面文字里已经出现的卖点、尺寸、重量、功率和功效。",
                    "图片只用来确认外观，可以写颜色和形状，不能从图片推断尺寸、重量或功率。",
                    "页面没有的内容不要写。不要写拍摄建议。",
                    "输出 JSON，不要解释：{\"card\":\"中文标准产品信息，写明产品尺寸、包装尺寸和重量，没有就写页面未提供\",\"selling_points\":\"页面上的卖点，没有就留空\",\"size\":\"产品尺寸，没有就留空\",\"package_size\":\"包装尺寸，没有就留空\",\"weight\":\"重量，没有就留空\"}",
                    f"标题：{title or '未填'}",
                    f"价格：{price or '未填'}",
                    f"已保存卖点：{points or '未填'}",
                    "页面资料：",
                    facts or "没有整理出的参数。",
                    "页面原文：",
                    (page_text or "")[:6000] or "没有页面原文。",
                ]
            ),
        }
    ]
    for path in image_paths[:4]:
        part = _image_part(Path(path))
        if part:
            content.append(part)
    text = chat(
        [
            {"role": "system", "content": "你只整理已经给出的商品事实。不要编造尺寸、重量、功率、功效或价格。"},
            {"role": "user", "content": content},
        ],
        max_tokens=700,
    )
    data = _json_object(text)
    card = str(data.get("card") or "").strip()
    selling = str(data.get("selling_points") or "").strip()
    if not card and not data:
        stripped = text.strip()
        if not stripped.startswith("{") and not stripped.startswith("```"):
            card = stripped
    return card, selling, str(data.get("size") or "").strip(), str(data.get("package_size") or "").strip(), str(data.get("weight") or "").strip()


def apply_standard_profile(product_id: str) -> None:
    if not product_id or not configured():
        return
    from server.media import is_banner

    with connect() as db:
        product = row_dict(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
        images = [
            row["path"]
            for row in db.execute(
                "SELECT path FROM assets WHERE product_id = ? AND kind = 'product' ORDER BY created_at",
                (product_id,),
            )
            if row["path"] and not is_banner(row["path"])
        ]
    if not product:
        return
    stored = {block["label"]: block["value"] for block in spec_blocks(product_id)}
    if _measurements_covered(product.get("profile_text") or "", stored.get("产品尺寸") or "", stored.get("包装尺寸") or "", stored.get("重量") or ""):
        return
    facts = page_facts(product_id)
    source = "\n".join(
        [
            product.get("title") or "",
            product.get("price") or "",
            product.get("points") or "",
            product.get("page_text") or "",
            facts,
        ]
    )
    if not ((product.get("page_text") or "").strip() or facts or (product.get("points") or "").strip() or images):
        return
    try:
        card, selling, size, package, weight = standardize_product(
            product.get("title") or "",
            product.get("price") or "",
            product.get("page_text") or "",
            product.get("points") or "",
            facts,
            images,
        )
    except RuntimeError as exc:
        if not images or ("预扣额度失败" not in str(exc) and "额度不足" not in str(exc)):
            raise
        card, selling, size, package, weight = standardize_product(
            product.get("title") or "",
            product.get("price") or "",
            product.get("page_text") or "",
            product.get("points") or "",
            facts,
            [],
        )
    card = _grounded(card, source)
    selling = _grounded(selling, source)
    fields = {
        "size": _grounded(size, source),
        "package_size": _grounded(package, source),
        "weight": _grounded(weight, source),
    }
    if not card and selling:
        card = selling
    if not card and not selling and not any(fields.values()):
        return
    missing = {}
    if fields["size"] and not (stored.get("产品尺寸") or "").strip():
        missing["size"] = fields["size"]
    if fields["package_size"] and not (stored.get("包装尺寸") or "").strip():
        missing["package_size"] = fields["package_size"]
    if fields["weight"] and not (stored.get("重量") or "").strip():
        missing["weight"] = fields["weight"]
    if missing:
        save_profile(product_id, missing)
    with connect() as db:
        if card:
            db.execute("UPDATE products SET profile_text = ? WHERE id = ?", (card, product_id))
        if selling:
            db.execute(
                "UPDATE products SET points = ? WHERE id = ? AND TRIM(COALESCE(points, '')) = ''",
                (selling, product_id),
            )


def _safe_key(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", key)[:80] or "shot"


def shot_frames(video: Path, key: str) -> list[Path]:
    dest = FILES / "shot-board" / _safe_key(key)
    dest.mkdir(parents=True, exist_ok=True)
    ready = sorted(dest.glob("shot-*.jpg"))
    if ready:
        return ready[:8]
    if not video.exists():
        return []
    commands = [
        ["ffmpeg", "-y", "-i", str(video), "-vf", "select='gt(scene,0.3)',scale=480:-1", "-vsync", "vfr", "-frames:v", "8", str(dest / "shot-%02d.jpg")],
        ["ffmpeg", "-y", "-i", str(video), "-vf", "fps=1,scale=480:-1", "-frames:v", "6", str(dest / "shot-%02d.jpg")],
    ]
    for command in commands:
        try:
            subprocess.run(command, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        ready = sorted(path for path in dest.glob("shot-*.jpg") if path.stat().st_size > 32)
        if len(ready) >= 2:
            return ready[:8]
    return sorted(path for path in dest.glob("shot-*.jpg") if path.stat().st_size > 32)[:8]


def save_hit_shots(hit_id: str, refresh: bool = False) -> str:
    if not hit_id or not configured():
        return ""
    with connect() as db:
        hit = row_dict(db.execute("SELECT * FROM hits WHERE id = ?", (hit_id,)).fetchone())
    if not hit or hit.get("kind") != "video":
        return ""
    video = Path(hit.get("path") or "")
    if refresh:
        shutil.rmtree(FILES / "shot-board" / _safe_key(hit_id), ignore_errors=True)
    frames = shot_frames(video, hit_id)
    if not frames:
        return ""
    content: list[dict] = [
        {
            "type": "text",
            "text": "这些帧按时间顺序来自同一条带货视频。写分镜：每一镜的大致顺序、景别、运镜和动作。要写清这一镜有没有人、露脸还是只露手、身体和产品怎么接触。不要写这个人是谁，不要写口播、音乐、字幕、价格和商品卖点。输出一段可直接当作运镜参考的提示词，不要解释。",
        }
    ]
    for frame in frames:
        part = _image_part(frame)
        if part:
            content.append(part)
    if len(content) == 1:
        return ""
    text = chat(
        [
            {"role": "system", "content": "你写景别、运镜、动作，以及有没有人出镜、露脸还是只露手。不要写这个人是谁，不要写音乐、口播和卖点。"},
            {"role": "user", "content": content},
        ]
    ).strip()
    if not text:
        return ""
    with connect() as db:
        db.execute("UPDATE hits SET shot_prompt = ? WHERE id = ?", (text, hit_id))
    return text
