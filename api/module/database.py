"""MongoDB 데이터 계층.

라우트 파일에서는 MongoClient를 직접 만들지 않고 이 모듈만 사용합니다.
기본 ID는 기존 URL/세션과의 호환을 위해 숫자 시퀀스로 유지하고,
MongoDB의 실제 문서 식별자인 ``_id``는 내부적으로만 사용합니다.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument


MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DATABASE") or "dico_page"

_client: MongoClient | None = None
_database = None
_database_lock = threading.Lock()
_indexes_ready = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_database_sync():
    global _client, _database
    if _database is not None:
        return _database
    with _database_lock:
        if _database is None:
            _client = MongoClient(
                MONGODB_URI,
                tz_aware=True,
                serverSelectionTimeoutMS=int(os.getenv("MONGODB_TIMEOUT_MS", "5000")),
            )
            _database = _client[MONGODB_DATABASE]
    return _database


def collection(name: str):
    return get_database_sync()[name]


def ensure_database_sync():
    """컬렉션과 인덱스를 한 번만 준비합니다."""
    global _indexes_ready
    db = get_database_sync()
    if _indexes_ready:
        return db
    with _database_lock:
        if not _indexes_ready:
            db.users.create_index("id", unique=True)
            db.users.create_index("user_uid", unique=True)
            db.users.create_index("email", unique=True, collation={"locale": "en", "strength": 2})
            db.users.create_index("nickname", unique=True, collation={"locale": "en", "strength": 2})
            db.user_profiles.create_index("user_id", unique=True)
            db.posts.create_index([("category", ASCENDING), ("id", DESCENDING)])
            db.posts.create_index([("author_id", ASCENDING), ("id", DESCENDING)])
            db.posts.create_index([("created_at", DESCENDING)])
            db.comments.create_index([("post_id", ASCENDING), ("id", ASCENDING)])
            db.comments.create_index([("author_id", ASCENDING), ("id", DESCENDING)])
            db.post_likes.create_index([("post_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
            db.notifications.create_index([("recipient_user_id", ASCENDING), ("created_at", DESCENDING)])
            db.notifications.create_index([("recipient_user_id", ASCENDING), ("is_read", ASCENDING)])
            db.notification_settings.create_index("user_id", unique=True)
            db.notices.create_index([("is_pinned", DESCENDING), ("id", DESCENDING)])
            db.media_uploads.create_index("expire_at", expireAfterSeconds=0)
            _indexes_ready = True
    return db


async def ensure_database():
    import asyncio

    return await asyncio.to_thread(ensure_database_sync)


def next_sequence_sync(name: str) -> int:
    document = collection("counters").find_one_and_update(
        {"_id": name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(document["value"])


def next_id_sync(name: str) -> int:
    return next_sequence_sync(name)


def _plain(document: dict[str, Any] | None):
    if not document:
        return None
    result = dict(document)
    result.pop("_id", None)
    return result


def migrate_sqlite_to_mongodb_sync(sqlite_path: str | Path, *, replace: bool = False) -> dict[str, int]:
    """기존 SQLite 데이터를 MongoDB 컬렉션으로 1회 이전합니다."""
    path = Path(sqlite_path)
    if not path.exists():
        raise FileNotFoundError(path)

    ensure_database_sync()
    mappings = {
        "users": "users",
        "user_profiles": "user_profiles",
        "posts": "posts",
        "comments": "comments",
        "post_likes": "post_likes",
        "notification_settings": "notification_settings",
        "notifications": "notifications",
        "notices": "notices",
    }
    counts: dict[str, int] = {}
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        for table, target in mappings.items():
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
            target_collection = collection(target)
            if replace and rows:
                target_collection.delete_many({})
            inserted = 0
            for row in rows:
                key = {"id": row["id"]} if "id" in row else None
                if key is None:
                    if table in {"user_profiles", "notification_settings"}:
                        key = {"user_id": row["user_id"]}
                    elif table == "post_likes":
                        key = {"post_id": row["post_id"], "user_id": row["user_id"]}
                if key:
                    target_collection.replace_one(key, row, upsert=True)
                else:
                    target_collection.insert_one(row)
                inserted += 1
            if inserted and table in {"users", "posts", "comments", "notices"}:
                highest = target_collection.find_one(sort=[("id", DESCENDING)])
                if highest and "id" in highest:
                    collection("counters").update_one(
                        {"_id": table},
                        {"$max": {"value": int(highest["id"])}},
                        upsert=True,
                    )
            counts[table] = inserted
    return counts


async def migrate_sqlite_to_mongodb(sqlite_path: str | Path, *, replace: bool = False) -> dict[str, int]:
    import asyncio

    return await asyncio.to_thread(
        migrate_sqlite_to_mongodb_sync,
        sqlite_path,
        replace=replace,
    )


def close_database() -> None:
    global _client, _database, _indexes_ready
    if _client is not None:
        _client.close()
    _client = None
    _database = None
    _indexes_ready = False
