from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FILES = DATA / "files"
DB_PATH = DATA / "app.sqlite"

_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    FILES.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
              id TEXT PRIMARY KEY,
              source_url TEXT DEFAULT '',
              title TEXT DEFAULT '',
              price TEXT DEFAULT '',
              points TEXT DEFAULT '',
              page_text TEXT DEFAULT '',
              status TEXT DEFAULT 'needs_input',
              detail TEXT DEFAULT '',
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS scenes (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              image_path TEXT NOT NULL,
              description TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS people (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              image_path TEXT NOT NULL,
              note TEXT DEFAULT '',
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS person_photos (
              id TEXT PRIMARY KEY,
              person_id TEXT NOT NULL,
              path TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY,
              product_id TEXT,
              kind TEXT NOT NULL,
              label TEXT DEFAULT '',
              path TEXT DEFAULT '',
              meta_json TEXT DEFAULT '{}',
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS boards (
              product_id TEXT PRIMARY KEY,
              document_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS variants (
              id TEXT PRIMARY KEY,
              product_id TEXT NOT NULL,
              scene_id TEXT NOT NULL,
              label TEXT DEFAULT '',
              status TEXT DEFAULT 'draft',
              round INTEGER DEFAULT 0,
              check_json TEXT DEFAULT '[]',
              adopted INTEGER DEFAULT 0,
              output_path TEXT DEFAULT '',
              prompt_text TEXT DEFAULT '',
              qa_note TEXT DEFAULT '',
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS variant_assets (
              variant_id TEXT NOT NULL,
              asset_id TEXT NOT NULL,
              PRIMARY KEY (variant_id, asset_id)
            );
            CREATE TABLE IF NOT EXISTS shots (
              id TEXT PRIMARY KEY,
              variant_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              t_start REAL NOT NULL,
              t_end REAL NOT NULL,
              action TEXT DEFAULT '',
              voiceover TEXT DEFAULT '',
              camera TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS scheduler (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              open INTEGER NOT NULL DEFAULT 0,
              probed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS prompts (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              body TEXT NOT NULL,
              product_id TEXT DEFAULT '',
              scene_id TEXT DEFAULT '',
              scene_name TEXT DEFAULT '',
              origin TEXT DEFAULT 'manual',
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS product_specs (
              product_id TEXT PRIMARY KEY,
              name_zh TEXT DEFAULT '',
              original_title TEXT DEFAULT '',
              brand TEXT DEFAULT '',
              category TEXT DEFAULT '',
              selling_points TEXT DEFAULT '',
              specs TEXT DEFAULT '',
              size TEXT DEFAULT '',
              package_size TEXT DEFAULT '',
              weight TEXT DEFAULT '',
              options TEXT DEFAULT '',
              package_contents TEXT DEFAULT '',
              scenes TEXT DEFAULT '',
              seller TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              resolved_url TEXT DEFAULT '',
              fetch_status TEXT DEFAULT '',
              missing TEXT DEFAULT '',
              updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS hits (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              note TEXT DEFAULT '',
              path TEXT DEFAULT '',
              kind TEXT DEFAULT 'image',
              product_id TEXT DEFAULT '',
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              product_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              node_id TEXT DEFAULT '',
              label TEXT NOT NULL,
              status TEXT NOT NULL,
              error TEXT DEFAULT '',
              payload_json TEXT DEFAULT '{}',
              dismissed INTEGER NOT NULL DEFAULT 0,
              created_at TEXT,
              started_at TEXT DEFAULT '',
              finished_at TEXT DEFAULT ''
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(prompts)")}
        if "scene_id" not in columns:
            db.execute("ALTER TABLE prompts ADD COLUMN scene_id TEXT DEFAULT ''")
        if "scene_name" not in columns:
            db.execute("ALTER TABLE prompts ADD COLUMN scene_name TEXT DEFAULT ''")
        if "origin" not in columns:
            db.execute("ALTER TABLE prompts ADD COLUMN origin TEXT DEFAULT 'manual'")
        if "body_zh" not in columns:
            db.execute("ALTER TABLE prompts ADD COLUMN body_zh TEXT DEFAULT ''")
        if "tag" not in columns:
            db.execute("ALTER TABLE prompts ADD COLUMN tag TEXT DEFAULT ''")
        db.execute("DROP INDEX IF EXISTS prompts_one_product")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS prompts_one_manual ON prompts(product_id) WHERE product_id != '' AND COALESCE(origin, 'manual') = 'manual'")
        db.execute("DROP INDEX IF EXISTS prompts_one_generated_scene")
        db.execute("DROP INDEX IF EXISTS hits_one_product")
        product_columns = {row["name"] for row in db.execute("PRAGMA table_info(products)")}
        if "profile_text" not in product_columns:
            db.execute("ALTER TABLE products ADD COLUMN profile_text TEXT DEFAULT ''")
        hit_columns = {row["name"] for row in db.execute("PRAGMA table_info(hits)")}
        if "shot_prompt" not in hit_columns:
            db.execute("ALTER TABLE hits ADD COLUMN shot_prompt TEXT DEFAULT ''")
        variant_columns = {row["name"] for row in db.execute("PRAGMA table_info(variants)")}
        if "steer" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN steer TEXT DEFAULT ''")
        if "voiceover" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN voiceover INTEGER NOT NULL DEFAULT 1")
        if "speech_text" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN speech_text TEXT DEFAULT ''")
        if "speech_note" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN speech_note TEXT DEFAULT ''")
        if "speech_ok" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN speech_ok INTEGER")
        if "template_id" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN template_id TEXT DEFAULT ''")
        if "duration_sec" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN duration_sec INTEGER NOT NULL DEFAULT 15")
        if "prompt_zh" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN prompt_zh TEXT DEFAULT ''")
        if "speech_tags" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN speech_tags TEXT DEFAULT '[]'")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_templates (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              body TEXT NOT NULL,
              builtin INTEGER NOT NULL DEFAULT 0,
              created_at TEXT
            )
            """
        )
        from server.templates import DEFAULT_BODY, DEFAULT_NAME

        if db.execute("SELECT COUNT(*) AS n FROM prompt_templates").fetchone()["n"] == 0:
            db.execute(
                "INSERT INTO prompt_templates (id, name, body, builtin, created_at) VALUES (?, ?, ?, 1, ?)",
                (new_id("tpl"), DEFAULT_NAME, DEFAULT_BODY, now()),
            )
        else:
            current = db.execute("SELECT id, body FROM prompt_templates WHERE builtin = 1 ORDER BY created_at LIMIT 1").fetchone()
            if current and "成稿只用下面六段" in (current["body"] or ""):
                db.execute("UPDATE prompt_templates SET body = ? WHERE id = ?", (DEFAULT_BODY, current["id"]))
            elif current and "1. 15 秒，9:16 竖屏。" in (current["body"] or ""):
                db.execute(
                    "UPDATE prompt_templates SET body = ? WHERE id = ?",
                    (current["body"].replace("1. 15 秒，9:16 竖屏。", "1. 总时长用 JSON 的 duration_sec。成稿写明这个秒数，分镜加起来等于它。9:16 竖屏。", 1), current["id"]),
                )
        db.execute("INSERT OR IGNORE INTO scheduler (id, open, probed) VALUES (1, 0, 0)")


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        with _lock:
            yield conn
            conn.commit()
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def file_url(path: str) -> str:
    if not path:
        return ""
    rel = Path(path)
    try:
        rel = rel.resolve().relative_to(FILES.resolve())
    except ValueError:
        rel = Path(path).name
    return "/files/" + str(rel).replace("\\", "/")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str, fallback):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback
