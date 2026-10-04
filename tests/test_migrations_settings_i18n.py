from __future__ import annotations

import sqlite3

import pytest

from admin import db
from app.database import MIGRATIONS, get_connection, migrate, set_database_path
from app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, TRANSLATIONS, normalize_locale, t
from app.runtime_config import RuntimeConfig
from tests.conftest import make_bootstrap


def test_empty_database_migrates_idempotently(configured_db):
    with get_connection() as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert versions == [version for version, _, _ in MIGRATIONS]
    assert journal.lower() == "wal"
    assert timeout == 30_000
    assert migrate() == []


def test_old_v0_schema_migrates_without_data_loss(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE video_cache (
          id INTEGER PRIMARY KEY AUTOINCREMENT, content_id TEXT NOT NULL,
          source TEXT, original_url TEXT, quality INTEGER, filesize INTEGER,
          cache_type TEXT NOT NULL, telegram_chat_id INTEGER,
          telegram_message_id INTEGER, telegram_file_id TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(content_id, quality));
        CREATE TABLE users (
          telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          language_code TEXT, first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          downloads_total INTEGER NOT NULL DEFAULT 0,
          downloads_success INTEGER NOT NULL DEFAULT 0,
          downloads_failed INTEGER NOT NULL DEFAULT 0,
          cache_hits INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE groups (
          chat_id INTEGER PRIMARY KEY,type TEXT NOT NULL,title TEXT,username TEXT,
          first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          downloads_total INTEGER NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE admins (
          id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL UNIQUE,
          role TEXT NOT NULL, added_by INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE admin_actions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
          action TEXT NOT NULL, target TEXT, metadata TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE inline_buttons (
          id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, url TEXT NOT NULL,
          scope TEXT NOT NULL DEFAULT 'downloads', position INTEGER NOT NULL DEFAULT 0,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE broadcasts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL,
          target_filter TEXT NOT NULL, message_payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft', total INTEGER NOT NULL DEFAULT 0,
          sent INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          started_at TEXT, finished_at TEXT);
        CREATE TABLE broadcast_targets (
          id INTEGER PRIMARY KEY AUTOINCREMENT, broadcast_id INTEGER NOT NULL,
          target_chat_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          error TEXT, sent_at TEXT);
        INSERT INTO users(telegram_id,username,language_code) VALUES(42,'old_user','kk-KZ');
        INSERT INTO groups(chat_id,type,title) VALUES(-1001,'supergroup','Old group');
        INSERT INTO admins(telegram_id,role,added_by) VALUES(1,'owner',1);
        INSERT INTO inline_buttons(text,url,position) VALUES('Legacy','https://example.com',0);
        INSERT INTO broadcasts(creator_id,target_filter,message_payload,status,total)
          VALUES(1,'{"audience":"all"}','{"type":"text","text":"Legacy"}','completed',1);
        INSERT INTO broadcast_targets(broadcast_id,target_chat_id,status)
          VALUES(1,42,'sent');
        INSERT INTO video_cache(content_id,source,original_url,quality,filesize,cache_type,
          telegram_chat_id,telegram_message_id,telegram_file_id)
          VALUES('youtube:abc','Youtube','https://youtu.be/abc',1080,123,'small',-100,7,'FILE_OLD');
        """
    )
    conn.commit()
    conn.close()
    set_database_path(path)
    migrate()
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE telegram_id=42").fetchone()
        group = conn.execute("SELECT * FROM groups WHERE chat_id=-1001").fetchone()
        cache = conn.execute("SELECT * FROM cache_entries WHERE content_id='youtube:abc'").fetchone()
        admin = conn.execute("SELECT * FROM admins WHERE telegram_id=1").fetchone()
        button = conn.execute("SELECT * FROM inline_buttons WHERE text='Legacy'").fetchone()
        broadcast = conn.execute("SELECT * FROM broadcasts WHERE id=1").fetchone()
    assert user["username"] == "old_user"
    assert user["locale"] == "kk"
    assert group["title"] == "Old group"
    assert cache["quality"] == 1080 and cache["telegram_file_id"] == "FILE_OLD"
    assert admin["role"] == "owner"
    assert button["url"] == "https://example.com" and "system_key" in button.keys()
    assert broadcast["status"] == "completed" and broadcast["total"] == 1


def test_runtime_settings_crud_validation_and_persistence(configured_db):
    bootstrap, runtime = configured_db
    assert runtime.get("max_concurrent_downloads") == 2
    runtime.set("max_batch_size", 8, actor_id=1)
    reopened = RuntimeConfig(bootstrap)
    reopened.seed_defaults()
    assert reopened.get("max_batch_size") == 8
    with pytest.raises(ValueError):
        runtime.set("max_batch_size", 0)
    with pytest.raises(ValueError):
        runtime.set("max_concurrent_downloads", -10)
    with pytest.raises(ValueError):
        runtime.set("small_tier_max_mb", 1500)
    with pytest.raises(ValueError):
        runtime.set("private_default_quality", "9000")


def test_local_transport_limit_is_realistic(tmp_path, monkeypatch):
    monkeypatch.delenv("MAX_DELIVERY_MB", raising=False)
    bootstrap = make_bootstrap(tmp_path, local=True)
    set_database_path(bootstrap.database_path)
    migrate()
    runtime = RuntimeConfig(bootstrap)
    runtime.seed_defaults()
    assert runtime.get("max_delivery_mb") == 1900
    with pytest.raises(ValueError):
        runtime.set("max_delivery_mb", 2001)


@pytest.mark.parametrize(
    ("language_code", "expected"),
    [("ru-RU", "ru"), ("kk-KZ", "kk"), ("uz-UZ", "uz_latn"), ("en-US", "en"), ("de-DE", "ru"), (None, "ru")],
)
def test_locale_detection(language_code, expected):
    assert normalize_locale(language_code) == expected


def test_locale_resources_are_complete_and_fallback_is_safe():
    required = set(TRANSLATIONS[DEFAULT_LOCALE])
    for locale in SUPPORTED_LOCALES:
        assert required <= set(TRANSLATIONS[locale]), locale
    assert t("en", "missing.key") == "missing.key"
    assert t("unknown", "start") == TRANSLATIONS["ru"]["start"]


def test_manual_locale_and_quality_are_independent_and_persistent(configured_db):
    db.upsert_user(10, "tester", "Test", "uz-UZ")
    assert db.get_user_locale(10) == "uz_latn"
    assert db.set_user_locale(10, "uz_cyrl")
    assert db.set_user_quality(10, "1080")
    db.upsert_user(10, "tester2", "Test", "en-US")
    row = db.get_user(10)
    assert row["locale"] == "uz_cyrl" and row["locale_manual"] == 1
    assert db.get_user_quality(10) == "1080"
    db.set_user_locale(10, "en")
    assert db.get_user_quality(10) == "1080"
    db.set_user_quality(10, "720")
    assert db.get_user_locale(10) == "en"


def test_group_locale_does_not_follow_later_members(configured_db):
    db.upsert_group(-100, "supergroup", "Group", None, "kk-KZ")
    db.upsert_group(-100, "supergroup", "Group", None, "en-US")
    assert db.get_group_locale(-100) == "kk"
    db.set_group_locale(-100, "uz_cyrl")
    db.upsert_group(-100, "supergroup", "Renamed", None, "ru-RU")
    assert db.get_group_locale(-100) == "uz_cyrl"
    db.set_group_quality(-100, "1440")
    db.set_group_locale(-100, "en")
    assert db.get_group_quality(-100) == "1440"
