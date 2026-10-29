from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from server.db import FILES, connect, now

_started = False


def start_worker() -> None:
    global _started
    if _started or os.environ.get("SELLING_VIDEO_WORKER") == "0":
        return
    _started = True
    threading.Thread(target=_loop, name="selling-video-worker", daemon=True).start()
    threading.Thread(target=_fill_ai_assets, name="selling-video-ai", daemon=True).start()


def _loop() -> None:
    while True:
        try:
            _collect_products()
            _prepare_references()
            _night_generate()
            from server.jobs import pump

            pump()
        except Exception:
            pass
        time.sleep(3)


def _fill_ai_assets() -> None:
    from server.bobtoken import apply_standard_profile, configured, save_hit_shots

    if not configured():
        return
    with connect() as db:
        products = [
            row["id"]
            for row in db.execute("SELECT id FROM products WHERE status = 'ready' AND TRIM(COALESCE(profile_text, '')) = ''")
        ]
        hits = [
            row["id"]
            for row in db.execute("SELECT id FROM hits WHERE kind = 'video' AND TRIM(COALESCE(shot_prompt, '')) = ''")
        ]
    for product_id in products:
        try:
            apply_standard_profile(product_id)
        except Exception:
            continue
    for hit_id in hits:
        try:
            save_hit_shots(hit_id)
        except Exception:
            continue


def _collect_products() -> None:
    from server.media import fetch_product

    with connect() as db:
        rows = db.execute("SELECT id FROM products WHERE status = 'collecting'").fetchall()
    for row in rows:
        with connect() as db:
            claimed = db.execute("UPDATE products SET status = 'fetching' WHERE id = ? AND status = 'collecting'", (row["id"],)).rowcount
        if claimed:
            fetch_product(row["id"])


def _prepare_references() -> None:
    from server.media import prepare_reference

    with connect() as db:
        rows = db.execute("SELECT id, meta_json FROM assets WHERE kind = 'reference'").fetchall()
    for row in rows:
        if '"pending": true' not in (row["meta_json"] or ""):
            continue
        with connect() as db:
            current = db.execute("SELECT meta_json FROM assets WHERE id = ?", (row["id"],)).fetchone()
            if not current or '"pending": true' not in current["meta_json"]:
                continue
            meta = current["meta_json"].replace('"pending": true', '"pending": false')
            db.execute("UPDATE assets SET meta_json = ? WHERE id = ?", (meta, row["id"]))
        prepare_reference(row["id"])


def _night_generate() -> None:
    with connect() as db:
        scheduler = db.execute("SELECT open, probed FROM scheduler WHERE id = 1").fetchone()
        if not scheduler or not scheduler["open"] or not scheduler["probed"]:
            return
        running = db.execute("SELECT COUNT(*) AS n FROM variants WHERE status = 'running'").fetchone()["n"]
        slots = 2 - running
        if slots <= 0:
            return
        queued = db.execute("SELECT id FROM variants WHERE status = 'queued' ORDER BY created_at, rowid LIMIT ?", (slots,)).fetchall()
        claimed = []
        for row in queued:
            if db.execute("UPDATE variants SET status = 'running' WHERE id = ? AND status = 'queued'", (row["id"],)).rowcount:
                claimed.append(row["id"])
    for variant_id in claimed:
        threading.Thread(target=_finish_generation, args=(variant_id,), name=f"night-{variant_id}", daemon=True).start()


def kick_generation(variant_id: str) -> None:
    if os.environ.get("SELLING_VIDEO_WORKER") == "0":
        return
    with connect() as db:
        claimed = db.execute(
            "UPDATE variants SET status = 'running', qa_note = ? WHERE id = ? AND status = 'queued'",
            ("正在连接 Seedance 2.0 Fast", variant_id),
        ).rowcount
    if claimed:
        threading.Thread(target=_finish_generation, args=(variant_id,), name=f"seedance-{variant_id}", daemon=True).start()


def _finish_generation(variant_id: str) -> None:
    from server.media import run_generation

    output = FILES / "outputs" / f"{variant_id}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        ok, detail = run_generation(variant_id, output)
    except Exception as exc:
        ok, detail = False, str(exc)
    with connect() as db:
        if ok and output.exists():
            db.execute(
                "UPDATE variants SET status = 'returned', output_path = ?, qa_note = '', speech_text = '', speech_note = '', speech_ok = NULL WHERE id = ?",
                (str(output), variant_id),
            )
        else:
            db.execute("UPDATE variants SET status = 'failed', qa_note = ? WHERE id = ?", (detail[:1000], variant_id))


def release_held() -> None:
    with connect() as db:
        db.execute("UPDATE variants SET status = 'queued' WHERE status = 'hold'")
        db.execute("UPDATE scheduler SET open = 1")
