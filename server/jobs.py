from __future__ import annotations

import os
import threading
from pathlib import Path

from server.db import connect, dumps, loads, new_id, now, row_dict

MAX_RUNNING = 2
_OPEN = ("queued", "running")
_NODE_KINDS = {"storyboard", "storyboard_video"}
_RESTART = "接口重启了，请再提交一次"


def enqueue_job(product_id: str, kind: str, label: str, payload: dict, node_id: str = "") -> str:
    locked = node_id if kind in _NODE_KINDS else ""
    with connect() as db:
        active = db.execute(
            "SELECT id FROM jobs WHERE product_id = ? AND kind = ? AND node_id = ? AND status IN ('queued', 'running') AND dismissed = 0",
            (product_id, kind, locked),
        ).fetchone()
        if active:
            raise ValueError(f"{label}已经提交，等它完成后再交")
        db.execute(
            "UPDATE jobs SET dismissed = 1 WHERE product_id = ? AND kind = ? AND node_id = ? AND status IN ('done', 'failed')",
            (product_id, kind, locked),
        )
        job_id = new_id("job")
        db.execute(
            """
            INSERT INTO jobs (id, product_id, kind, node_id, label, status, error, payload_json, dismissed, created_at, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, 'queued', '', ?, 0, ?, '', '')
            """,
            (job_id, product_id, kind, locked, label, dumps(payload), now()),
        )
    return job_id


def submit_job(product_id: str, kind: str, label: str, payload: dict, node_id: str = "") -> str:
    job_id = enqueue_job(product_id, kind, label, payload, node_id)
    if os.environ.get("SELLING_VIDEO_WORKER") == "0":
        run_job(job_id)
    else:
        pump()
    return job_id


def list_jobs(product_id: str = "") -> list[dict]:
    with connect() as db:
        if product_id:
            rows = db.execute(
                "SELECT * FROM jobs WHERE dismissed = 0 AND product_id = ? ORDER BY created_at DESC, rowid DESC",
                (product_id,),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM jobs WHERE dismissed = 0 ORDER BY created_at DESC, rowid DESC").fetchall()
    return [_public(row_dict(row)) for row in rows]


def dismiss_job(job_id: str) -> None:
    with connect() as db:
        row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise LookupError("任务不存在")
        if row["status"] in _OPEN:
            raise ValueError("这一项还在做，做完再关掉")
        db.execute("UPDATE jobs SET dismissed = 1 WHERE id = ?", (job_id,))


def fail_interrupted() -> None:
    from server.board import read_graph
    from server.db import dumps as dump_doc

    with connect() as db:
        db.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE status IN ('queued', 'running')",
            (_RESTART, now()),
        )
        rows = db.execute("SELECT product_id FROM boards").fetchall()
    for row in rows:
        product_id = row["product_id"]
        try:
            doc = read_graph(product_id)
        except Exception:
            continue
        changed = False
        for node in doc.get("nodes") or []:
            if node.get("type") != "storyboard":
                continue
            data = dict(node.get("data") or {})
            if data.get("clipStatus") != "running":
                continue
            data["clipStatus"] = ""
            data["clipError"] = ""
            node["data"] = data
            changed = True
        if not changed:
            continue
        with connect() as db:
            db.execute(
                "UPDATE boards SET document_json = ? WHERE product_id = ?",
                (dump_doc(doc), product_id),
            )


def pump() -> None:
    claimed: list[str] = []
    with connect() as db:
        running = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'running'").fetchone()["n"]
        slots = MAX_RUNNING - int(running or 0)
        if slots <= 0:
            return
        queued = db.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT ?",
            (slots,),
        ).fetchall()
        for row in queued:
            if db.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (now(), row["id"]),
            ).rowcount:
                claimed.append(row["id"])
    for job_id in claimed:
        threading.Thread(target=_run_later, args=(job_id,), name=f"job-{job_id}", daemon=True).start()


def run_job(job_id: str) -> None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or row["status"] not in _OPEN:
            return
        db.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND started_at = ''",
            (now(), job_id),
        )
        kind = row["kind"]
        product_id = row["product_id"]
        node_id = row["node_id"] or ""
        payload = loads(row["payload_json"] or "{}", {})
    if not isinstance(payload, dict):
        payload = {}
    try:
        if kind == "white":
            from server.usage import generate_white_image

            generate_white_image(product_id, str(payload.get("prompt") or ""), list(payload.get("asset_ids") or []))
        elif kind == "detail":
            from server.usage import generate_detail_image

            generate_detail_image(product_id, str(payload.get("prompt") or ""), list(payload.get("asset_ids") or []))
        elif kind == "usage":
            from server.usage import generate_usage_image

            generate_usage_image(product_id, str(payload.get("prompt") or ""), list(payload.get("asset_ids") or []))
        elif kind == "storyboard":
            from server.storyboard import generate_storyboard

            board = generate_storyboard(
                product_id,
                node_id,
                payload.get("duration"),
                payload.get("shot_count"),
                payload.get("selected_ids"),
                payload.get("shots"),
            )
            node = next((item for item in board.get("nodes") or [] if item.get("id") == node_id), {})
            error = str((node.get("data") or {}).get("error") or "")
            if error:
                _mark(job_id, "failed", error)
                return
        elif kind == "storyboard_video":
            from server.storyboard import _FAST_MODEL, _finish_clip, _merge_story

            _merge_story(product_id, node_id, {"clipStatus": "running", "clipError": "", "clipModel": _FAST_MODEL})
            images = [Path(item) for item in payload.get("images") or []]
            _finish_clip(product_id, node_id, str(payload.get("prompt") or ""), images, int(payload.get("seconds") or 0))
            from server.board import read_graph

            doc = read_graph(product_id)
            node = next((item for item in doc.get("nodes") or [] if item.get("id") == node_id), {})
            data = node.get("data") or {}
            if data.get("clipStatus") == "failed":
                _mark(job_id, "failed", str(data.get("clipError") or "视频没有生成"))
                return
        else:
            raise RuntimeError("不认识这个任务")
    except Exception as exc:
        message = str(exc).strip() or "没有生成"
        _mark(job_id, "failed", message)
        if os.environ.get("SELLING_VIDEO_WORKER") == "0":
            raise
        return
    _mark(job_id, "done", "")


def _run_later(job_id: str) -> None:
    try:
        run_job(job_id)
    except Exception:
        pass
    finally:
        if os.environ.get("SELLING_VIDEO_WORKER") != "0":
            pump()


def _mark(job_id: str, status: str, error: str) -> None:
    with connect() as db:
        db.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, (error or "")[:500], now() if status in {"done", "failed"} else "", job_id),
        )


def _public(row: dict) -> dict:
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "kind": row["kind"],
        "node_id": row.get("node_id") or "",
        "label": row["label"],
        "status": row["status"],
        "error": row.get("error") or "",
    }
