import asyncio
import base64
import binascii
import hashlib
import math
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import quote

import httpx
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from quart import Blueprint, Response, abort, jsonify, request, session, url_for

from ...module.database import collection, next_id_sync
from .auth import ensure_database, get_current_user, validate_csrf_token


media_bp = Blueprint("media", __name__)
REPOSITORY = "dico-page/postimage"
CHUNK_BYTES = 768 * 1024
WIRE_CHUNK_BYTES = 4 * 1024 * 1024
IMAGE_CHUNK_BYTES = 5 * 1024 * 1024
VIDEO_CHUNK_BYTES = 50 * 1024 * 1024
POSTER_BYTES = 128 * 1024
PLAYBACK_READ_CONCURRENCY = 4
MAX_PLAYBACK_RANGE_BYTES = 200 * 1024 * 1024
OPEN_PLAYBACK_RANGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 10 * 1024 ** 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENTS = 5
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
VIDEO_TYPES = {"video/mp4": "mp4", "video/webm": "webm", "video/ogg": "ogv"}
FILE_TYPES = {".pdf", ".zip", ".txt", ".csv", ".hwp", ".hwpx", ".docx", ".xlsx", ".pptx"}
MEDIA_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class MediaStorageError(Exception):
    pass


class MediaRateLimitError(MediaStorageError):
    def __init__(self, retry_after):
        super().__init__("GitHub 요청 제한으로 잠시 대기하고 있습니다.")
        self.retry_after = float(retry_after)


@lru_cache(maxsize=1)
def _github_client():
    return httpx.Client(timeout=60, follow_redirects=True,
                        limits=httpx.Limits(max_connections=16, max_keepalive_connections=12))


def _github(method, endpoint, payload=None, raw=False):
    token = os.getenv("postimage", "").strip()
    if not token:
        raise MediaStorageError("서버에 postimage GitHub 토큰이 설정되지 않았습니다.")
    url = "https://api.github.com/repos/" + REPOSITORY + endpoint
    headers = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dico-page-media",
        "Accept-Encoding": "identity",
    }
    try:
        response = _github_client().request(method, url, headers=headers, json=payload)
        response.raise_for_status()
        return response.content if raw else response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 429):
            try:
                message = exc.response.json().get("message", "").lower()
            except (ValueError, AttributeError):
                message = ""
            if (exc.response.status_code == 429 or exc.response.headers.get("x-ratelimit-remaining") == "0"
                    or "rate limit" in message or "abuse detection" in message):
                retry = exc.response.headers.get("retry-after", "")
                reset = exc.response.headers.get("x-ratelimit-reset", "")
                try:
                    if retry:
                        try:
                            delay = float(retry)
                        except ValueError:
                            retry_at = parsedate_to_datetime(retry)
                            if retry_at.tzinfo is None:
                                retry_at = retry_at.replace(tzinfo=timezone.utc)
                            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                    elif reset and exc.response.headers.get("x-ratelimit-remaining") == "0":
                        delay = max(60, int(reset) - int(datetime.now(timezone.utc).timestamp()))
                    else:
                        delay = 60
                except (ValueError, TypeError, OverflowError):
                    delay = 60
                raise MediaRateLimitError(min(3600, max(1, delay))) from exc
        if exc.response.status_code in (401, 403):
            raise MediaStorageError("postimage 토큰의 저장소 접근 권한을 확인해 주세요.") from exc
        if exc.response.status_code in (409, 422):
            raise MediaStorageError("GitHub 저장소가 동시에 변경되었습니다. 다시 시도해 주세요.") from exc
        if exc.response.status_code == 404:
            raise MediaStorageError("GitHub 저장소 또는 파일을 찾을 수 없습니다.") from exc
        raise MediaStorageError("GitHub 미디어 저장 요청이 실패했습니다.") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise MediaStorageError("GitHub 미디어 저장소에 연결하지 못했습니다.") from exc


def _blob(data):
    result = _github("POST", "/git/blobs", {
        "content": base64.b64encode(data).decode("ascii"), "encoding": "base64",
    })
    return result["sha"]


def _queued_blob(data):
    """Write blobs through a Mongo-backed, cross-instance serial queue."""
    locks = collection("media_github_upload_queue")
    now = datetime.now(timezone.utc)
    epoch = datetime.fromtimestamp(0, timezone.utc)
    try:
        locks.update_one({"_id": "github-blob"}, {"$setOnInsert": {
            "lease_until": epoch, "next_allowed_at": epoch,
        }}, upsert=True)
    except DuplicateKeyError:
        pass
    lease_id = uuid.uuid4().hex
    lock = locks.find_one_and_update({
        "_id": "github-blob", "lease_until": {"$lte": now}, "next_allowed_at": {"$lte": now},
    }, {"$set": {"lease_id": lease_id, "lease_until": now + timedelta(seconds=120)}})
    if not lock:
        state = locks.find_one({"_id": "github-blob"}) or {}
        due = max(state.get("lease_until", now), state.get("next_allowed_at", now))
        wait = max(1.0, (due - now).total_seconds())
        raise MediaRateLimitError(min(3600, wait))
    delay = random.uniform(1.0, 1.5)
    try:
        return _blob(data)
    except MediaRateLimitError as exc:
        delay = max(delay, exc.retry_after)
        raise
    finally:
        finished = datetime.now(timezone.utc)
        locks.update_one({"_id": "github-blob", "lease_id": lease_id}, {"$set": {
            "lease_until": finished,
            "next_allowed_at": finished + timedelta(seconds=delay),
        }, "$unset": {"lease_id": ""}})


@lru_cache(maxsize=2)
def _read_blob(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
        raise MediaStorageError("미디어 정보가 올바르지 않습니다.")
    return _github("GET", "/git/blobs/" + sha, raw=True)


def _commit_blobs(files, message):
    for attempt in range(3):
        ref = _github("GET", "/git/ref/heads/main")
        parent = ref["object"]["sha"]
        tree_sha = _github("GET", "/git/commits/" + parent)["tree"]["sha"]
        tree = _github("POST", "/git/trees", {
            "base_tree": tree_sha,
            "tree": [
                {"path": path, "mode": "100644", "type": "blob", "sha": sha}
                for path, sha in files
            ],
        })
        commit = _github("POST", "/git/commits", {
            "message": message, "tree": tree["sha"], "parents": [parent],
        })
        try:
            _github("PATCH", "/git/refs/heads/main", {"sha": commit["sha"], "force": False})
            return
        except MediaRateLimitError:
            raise
        except MediaStorageError:
            if attempt == 2:
                raise


def _document(kind, item_id):
    published = collection("posts" if kind == "posts" else "notices").find_one(
        {"id": item_id}, {"_id": 0, "id": 1, "author_id": 1, "images": 1, "files": 1}
    )
    if published:
        return published
    return collection("media_drafts").find_one(
        {"_id": _draft_key(kind, item_id), "draft_state": "active", "expire_at": {"$gt": datetime.now(timezone.utc)}},
        {"id": 1, "author_id": 1, "images": 1, "files": 1, "draft_state": 1},
    )


def _draft_key(kind, item_id):
    return kind + ":" + str(item_id)


def claim_media_draft_sync(kind, item_id, user_id):
    return collection("media_drafts").find_one_and_update({
        "_id": _draft_key(kind, item_id), "author_id": int(user_id), "draft_state": "active",
        "expire_at": {"$gt": datetime.now(timezone.utc)},
    }, {"$set": {"draft_state": "publishing"}}, return_document=ReturnDocument.BEFORE)


def release_media_draft_sync(kind, item_id):
    collection("media_drafts").update_one(
        {"_id": _draft_key(kind, item_id), "draft_state": "publishing"},
        {"$set": {"draft_state": "active"}},
    )


def finish_media_draft_sync(kind, item_id):
    collection("media_drafts").delete_one({"_id": _draft_key(kind, item_id)})
    _remove_upload_state(kind, item_id)


async def _write_target(kind, item_id):
    await ensure_database()
    if kind not in {"posts", "notices"}:
        abort(404)
    if not session.get("user_id"):
        return None, (jsonify({"success": False, "message": "로그인이 필요합니다."}), 401)
    user = await get_current_user()
    if not user:
        return None, (jsonify({"success": False, "message": "다시 로그인해 주세요."}), 401)
    if kind == "notices" and user.get("role") != "admin":
        abort(403)
    doc = await asyncio.to_thread(_document, kind, item_id)
    if not doc:
        abort(404)
    if kind == "posts" and int(doc["author_id"]) != int(user["id"]):
        abort(403)
    if not validate_csrf_token({"csrf_token": request.headers.get("X-CSRF-Token", "")}):
        abort(400)
    return doc, None


def _name(value):
    name = str(value or "").strip()
    if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
        raise ValueError("파일명은 1~120자로 입력해 주세요.")
    return name


def _signature(mime, data):
    return {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "video/mp4": len(data) > 8 and data[4:8] == b"ftyp",
        "video/webm": data.startswith(b"\x1a\x45\xdf\xa3"),
        "video/ogg": data.startswith(b"OggS"),
    }.get(mime, False)


def _poster_data(value):
    if not value:
        return None
    if not isinstance(value, str) or not value.startswith("data:image/webp;base64,") or len(value) > 180000:
        raise ValueError("영상 미리보기 형식이 올바르지 않습니다.")
    try:
        data = base64.b64decode(value.split(",", 1)[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("영상 미리보기 형식이 올바르지 않습니다.") from exc
    if not 0 < len(data) <= POSTER_BYTES or not _signature("image/webp", data):
        raise ValueError("영상 미리보기 형식이 올바르지 않습니다.")
    return data


def _folder(kind):
    return "게시글" if kind == "posts" else "공지"


def _save_attachment(kind, item_id, media, field="images"):
    drafts = collection("media_drafts")
    result = drafts.update_one(
        {"_id": _draft_key(kind, item_id), "draft_state": "active", field + ".id": {"$ne": media["id"]}, field + ".4": {"$exists": False}},
        {"$push": {field: media}},
    )
    if result.modified_count:
        return True
    return collection("posts" if kind == "posts" else "notices").update_one(
        {"id": item_id, field + ".id": {"$ne": media["id"]}, field + ".4": {"$exists": False}},
        {"$push": {field: media}},
    ).modified_count == 1


def _upload_key(kind, item_id, media_id):
    return kind + ":" + str(item_id) + ":" + media_id


def _media_paths(kind, item_id, media):
    prefix = _folder(kind) + "/" + str(item_id) + "/"
    if media["kind"] == "image":
        if media.get("chunks"):
            return [prefix + "이미지/" + media["id"] + "/" + str(index).zfill(4) + ".part"
                    for index in range(len(media["chunks"]))]
        return [prefix + "이미지/" + media["id"] + "." + IMAGE_TYPES[media["mime"]]]
    directory = "영상/" if media["kind"] == "video" else "첨부파일/"
    paths = [prefix + directory + media["id"] + "/" + str(index).zfill(4) + ".part"
             for index in range(len(media["chunks"]))]
    if media["kind"] == "video" and media.get("poster"):
        paths.append(prefix + directory + media["id"] + "/poster.webp")
    return paths


def _discard_untracked_media(kind, item_id, media):
    doc = _document(kind, item_id)
    if doc and any(item.get("id") == media["id"] for field in ("images", "files") for item in doc.get(field, [])):
        return
    _commit_blobs([(path, None) for path in _media_paths(kind, item_id, media)], "Remove incomplete media " + media["id"])


def _remove_upload_state(kind, item_id, media_id=None):
    uploads = collection("media_uploads")
    staged = collection("media_upload_parts")
    if media_id:
        key = _upload_key(kind, item_id, media_id)
        keys = [key, "file:" + key]
        uploads.delete_many({"_id": {"$in": keys}})
        staged.delete_many({"upload_id": {"$in": keys}})
    else:
        pattern = r"^(?:file:)?" + re.escape(kind + ":" + str(item_id) + ":")
        uploads.delete_many({"_id": {"$regex": pattern}})
        staged.delete_many({"upload_id": {"$regex": pattern}})


def _remove_draft_sync(kind, item_id, author_id=None):
    query = {"_id": _draft_key(kind, item_id),
             "draft_state": "active" if author_id is not None else {"$in": ["active", "publishing"]}}
    if author_id is not None:
        query["author_id"] = int(author_id)
    drafts = collection("media_drafts")
    draft = drafts.find_one(query)
    if not draft:
        return False
    if not collection(kind).find_one({"id": item_id}, {"_id": 1}):
        paths = [_media_paths(kind, item_id, media) for media in draft.get("images", []) + draft.get("files", [])]
        flat_paths = [path for group in paths for path in group]
        if flat_paths:
            _commit_blobs([(path, None) for path in flat_paths], "Remove draft " + _draft_key(kind, item_id))
    drafts.delete_one(query)
    _remove_upload_state(kind, item_id)
    return True


def _remove_draft_media_sync(kind, item_id, media_id, author_id):
    drafts = collection("media_drafts")
    query = {"_id": _draft_key(kind, item_id), "author_id": int(author_id), "draft_state": "active"}
    draft = drafts.find_one(query)
    if not draft:
        return False
    media = next((item for field in ("images", "files") for item in draft.get(field, []) if item["id"] == media_id), None)
    if media:
        _commit_blobs([(path, None) for path in _media_paths(kind, item_id, media)], "Remove media " + media_id)
        field = "images" if media["kind"] in ("image", "video") else "files"
        drafts.update_one(query, {"$pull": {field: {"id": media_id}}})
    _remove_upload_state(kind, item_id, media_id)
    return True


def _clean_expired_drafts_sync():
    stale = collection("media_drafts").find({
        "draft_state": {"$in": ["active", "publishing"]},
        "expire_at": {"$lte": datetime.now(timezone.utc)},
    }).limit(3)
    for draft in stale:
        try:
            _remove_draft_sync(draft["kind"], draft["id"])
        except MediaStorageError:
            continue


async def _draft_user(kind):
    if kind not in {"posts", "notices"}:
        abort(404)
    await ensure_database()
    user = await get_current_user() if session.get("user_id") else None
    if not user:
        abort(401)
    if kind == "notices" and user.get("role") != "admin":
        abort(403)
    token = request.headers.get("X-CSRF-Token")
    if not token:
        form = await request.form
        token = form.get("csrf_token", "")
    if not validate_csrf_token({"csrf_token": token}):
        abort(400)
    return user


@media_bp.post("/api/media/drafts/<kind>")
async def create_draft(kind):
    user = await _draft_user(kind)
    await asyncio.to_thread(_clean_expired_drafts_sync)
    item_id = await asyncio.to_thread(next_id_sync, kind)
    await asyncio.to_thread(collection("media_drafts").insert_one, {
        "_id": _draft_key(kind, item_id), "kind": kind, "id": item_id,
        "author_id": int(user["id"]), "images": [], "files": [],
        "draft_state": "active", "expire_at": datetime.now(timezone.utc) + timedelta(hours=24),
    })
    return jsonify({"success": True, "id": item_id})


@media_bp.post("/api/media/drafts/<kind>/<int:item_id>/resume")
async def resume_draft(kind, item_id):
    user = await _draft_user(kind)
    result = await asyncio.to_thread(collection("media_drafts").update_one, {
        "_id": _draft_key(kind, item_id), "author_id": int(user["id"]),
        "draft_state": "active", "expire_at": {"$gt": datetime.now(timezone.utc)},
    }, {"$set": {"expire_at": datetime.now(timezone.utc) + timedelta(hours=24)}})
    if not result.matched_count:
        return jsonify({"success": False, "message": "이어올릴 임시 글이 만료되었습니다."}), 404
    return jsonify({"success": True, "id": item_id})


@media_bp.post("/api/media/drafts/<kind>/<int:item_id>/cancel")
async def cancel_draft(kind, item_id):
    user = await _draft_user(kind)
    try:
        removed = await asyncio.to_thread(_remove_draft_sync, kind, item_id, user["id"])
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    return jsonify({"success": removed}) if removed else (jsonify({"success": False, "message": "임시 글을 찾을 수 없습니다."}), 404)


@media_bp.post("/api/media/drafts/<kind>/<int:item_id>/remove/<media_id>")
async def remove_draft_media(kind, item_id, media_id):
    user = await _draft_user(kind)
    if not MEDIA_ID.fullmatch(media_id):
        abort(400)
    try:
        removed = await asyncio.to_thread(_remove_draft_media_sync, kind, item_id, media_id, user["id"])
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    return jsonify({"success": removed}) if removed else (jsonify({"success": False, "message": "임시 글을 찾을 수 없습니다."}), 404)


@media_bp.get("/api/media/status")
async def media_status():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "로그인이 필요합니다."}), 401
    try:
        await asyncio.to_thread(_github, "GET", "/git/ref/heads/main")
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 503
    return jsonify({"success": True, "configured": True})


@media_bp.get("/api/media/<kind>/<int:item_id>/<media_kind>/<media_id>/status")
async def media_upload_status(kind, item_id, media_kind, media_id):
    doc, problem = await _write_target(kind, item_id)
    if problem:
        return problem
    fields = {"image": "images", "images": "images", "video": "images", "videos": "images",
              "file": "files", "files": "files"}
    if media_kind not in fields or not MEDIA_ID.fullmatch(media_id):
        abort(404)
    field = fields[media_kind]
    media = next((item for item in doc.get(field, []) if item.get("id") == media_id), None)
    if media:
        return jsonify({"success": True, "complete": True, "uploaded_chunks": [],
                        "sha256": media.get("sha256")})
    key = ("file:" if field == "files" else "") + _upload_key(kind, item_id, media_id)
    state = await asyncio.to_thread(collection("media_uploads").find_one, {"_id": key})
    if state and int(state.get("user_id", -1)) != int(session["user_id"]):
        abort(404)
    return jsonify({
        "success": True, "complete": False,
        "uploaded_chunks": sorted((state or {}).get("uploaded_chunks", [])),
        "chunk_count": (state or {}).get("chunk_count", 0),
        "chunk_size": (state or {}).get("chunk_size", 0),
        "mime_type": (state or {}).get("mime_type"),
        "total_size": (state or {}).get("total_size", 0),
        "sha256": (state or {}).get("sha256"),
    })


@media_bp.post("/api/media/<kind>/<int:item_id>/images/<media_id>")
async def upload_image(kind, item_id, media_id):
    doc, problem = await _write_target(kind, item_id)
    if problem:
        return problem
    if not MEDIA_ID.fullmatch(media_id):
        abort(400)
    mime = request.headers.get("Content-Type", "").split(";")[0].lower()
    if mime not in IMAGE_TYPES:
        return jsonify({"success": False, "message": "지원하지 않는 이미지 형식입니다."}), 400
    if request.content_length and request.content_length > CHUNK_BYTES:
        return jsonify({"success": False, "message": "이미지는 768KB 이하로 올려 주세요."}), 413
    data = await request.get_data()
    if not data or len(data) > CHUNK_BYTES or not _signature(mime, data):
        return jsonify({"success": False, "message": "이미지 형식 또는 용량이 올바르지 않습니다."}), 400
    try:
        name = _name(request.args.get("name"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    for media in doc.get("images", []):
        if media.get("id") == media_id:
            return jsonify({"success": True, "url": url_for("media.serve_media", kind=kind, item_id=item_id, media_id=media_id)})
    if len(doc.get("images", [])) >= MAX_ATTACHMENTS:
        return jsonify({"success": False, "message": "첨부는 최대 5개까지 가능합니다."}), 400
    path = _folder(kind) + "/" + str(item_id) + "/이미지/" + media_id + "." + IMAGE_TYPES[mime]
    try:
        sha = await asyncio.to_thread(_queued_blob, data)
        await asyncio.to_thread(_commit_blobs, [(path, sha)], "Upload " + path)
        media = {"id": media_id, "name": name, "kind": "image", "mime": mime, "size": len(data), "sha": sha}
        saved = await asyncio.to_thread(_save_attachment, kind, item_id, media)
    except MediaRateLimitError as exc:
        return jsonify({"success": False, "message": str(exc), "retry_after": exc.retry_after}), 429, {"Retry-After": str(math.ceil(exc.retry_after))}
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    if not saved:
        try:
            await asyncio.to_thread(_discard_untracked_media, kind, item_id, media)
        except MediaStorageError:
            pass
        return jsonify({"success": False, "message": "첨부 정보 저장에 실패했습니다."}), 409
    return jsonify({"success": True, "url": url_for("media.serve_media", kind=kind, item_id=item_id, media_id=media_id)})


@media_bp.post("/api/media/<kind>/<int:item_id>/images/<media_id>/chunks/<int:index>")
async def upload_image_chunk(kind, item_id, media_id, index):
    return await _upload_chunk(kind, item_id, media_id, index, "image")


@media_bp.post("/api/media/<kind>/<int:item_id>/images/<media_id>/complete")
async def complete_image(kind, item_id, media_id):
    return await _complete_chunks(kind, item_id, media_id, "image")


@media_bp.post("/api/media/<kind>/<int:item_id>/videos/<media_id>/chunks/<int:index>")
async def upload_video_chunk(kind, item_id, media_id, index):
    return await _upload_chunk(kind, item_id, media_id, index, "video")


@media_bp.post("/api/media/<kind>/<int:item_id>/files/<media_id>/chunks/<int:index>")
async def upload_file_chunk(kind, item_id, media_id, index):
    return await _upload_chunk(kind, item_id, media_id, index, "file")


async def _upload_chunk(kind, item_id, media_id, index, media_kind):
    doc, problem = await _write_target(kind, item_id)
    if problem:
        return problem
    field = "files" if media_kind == "file" else "images"
    if len(doc.get(field, [])) >= MAX_ATTACHMENTS:
        return jsonify({"success": False, "message": "첨부는 최대 5개까지 가능합니다."}), 400
    if not MEDIA_ID.fullmatch(media_id):
        abort(400)
    if any(field not in request.args for field in ("chunk_size", "chunk_index", "chunk_offset")):
        return jsonify({
            "success": False, "reload_required": True,
            "message": "업로드 방식이 변경되었습니다. 페이지를 새로고침한 뒤 파일을 다시 선택해 주세요.",
        }), 409
    try:
        name = _name(request.args.get("name"))
        mime = request.args["mime"]
        size = int(request.args["size"])
        count = int(request.args["count"])
        chunk_size = int(request.args["chunk_size"])
        logical_index = int(request.args["chunk_index"])
        chunk_offset = int(request.args["chunk_offset"])
    except (KeyError, ValueError) as exc:
        return jsonify({"success": False, "message": "파일 정보가 올바르지 않습니다."}), 400
    if media_kind == "video":
        valid_type = mime in VIDEO_TYPES
        expected_logical_size = VIDEO_CHUNK_BYTES
        limit = MAX_VIDEO_BYTES
    elif media_kind == "image":
        valid_type = mime in IMAGE_TYPES
        expected_logical_size = IMAGE_CHUNK_BYTES
        limit = MAX_IMAGE_BYTES
    else:
        valid_type = mime == "application/octet-stream" and os.path.splitext(name)[1].lower() in FILE_TYPES
        expected_logical_size = VIDEO_CHUNK_BYTES
        limit = MAX_FILE_BYTES
    chunks_per_logical = math.ceil(expected_logical_size / WIRE_CHUNK_BYTES)
    if (not valid_type or not 0 < size <= limit or chunk_size != expected_logical_size
            or count != math.ceil(size / chunk_size) or not 0 <= logical_index < count
            or chunk_offset < 0 or chunk_offset % WIRE_CHUNK_BYTES
            or index != logical_index * chunks_per_logical + chunk_offset // WIRE_CHUNK_BYTES):
        return jsonify({"success": False, "message": "파일 형식 또는 크기가 올바르지 않습니다."}), 400
    group_size = min(chunk_size, size - logical_index * chunk_size)
    expected = min(WIRE_CHUNK_BYTES, group_size - chunk_offset)
    if expected <= 0:
        return jsonify({"success": False, "message": "파일 조각 위치가 올바르지 않습니다."}), 400
    if request.content_length and request.content_length > WIRE_CHUNK_BYTES:
        return jsonify({"success": False, "message": "전송 조각이 서버 요청 한도를 초과했습니다."}), 413
    data = await request.get_data()
    if media_kind == "file":
        extension = os.path.splitext(name)[1].lower()
        if extension in {".txt", ".csv"}:
            valid_signature = True
        elif extension == ".pdf":
            valid_signature = data.startswith(b"%PDF-")
        elif extension == ".hwp":
            valid_signature = data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        else:
            valid_signature = data.startswith(b"PK\x03\x04")
    else:
        valid_signature = _signature(mime, data)
    if len(data) != expected or (logical_index == 0 and chunk_offset == 0 and not valid_signature):
        return jsonify({"success": False, "message": "파일 조각이 올바르지 않습니다."}), 400
    key = ("file:" if media_kind == "file" else "") + _upload_key(kind, item_id, media_id)
    uploads = collection("media_uploads")
    state = await asyncio.to_thread(uploads.find_one, {"_id": key})
    if state and (int(state.get("user_id", -1)) != int(session["user_id"])
                  or state.get("total_size") != size or state.get("mime_type") != mime
                  or state.get("name") != name or state.get("chunk_count") != count
                  or state.get("chunk_size") != chunk_size):
        return jsonify({"success": False, "message": "업로드 정보가 서로 다릅니다. 새로고침 후 다시 선택해 주세요."}), 409
    if state and logical_index in state.get("uploaded_chunks", []):
        return jsonify({"success": True, "index": index, "logical_index": logical_index,
                        "uploaded_chunks": state["uploaded_chunks"]})
    try:
        parts = collection("media_upload_parts")
        part_key = key + ":" + str(index)
        await asyncio.to_thread(parts.replace_one, {"_id": part_key}, {
            "_id": part_key, "upload_id": key, "index": index,
            "logical_index": logical_index, "offset": chunk_offset,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "data": data, "expire_at": datetime.now(timezone.utc) + timedelta(hours=24),
        }, upsert=True)
        await asyncio.to_thread(uploads.update_one, {"_id": key}, {
            "$setOnInsert": {
                "user_id": int(session["user_id"]), "uuid": media_id, "name": name,
                "mime_type": mime, "total_size": size, "chunk_count": count,
                "chunk_size": chunk_size, "uploaded_chunks": [], "chunks": {}, "sha256": None,
            },
            "$set": {"expire_at": datetime.now(timezone.utc) + timedelta(hours=24)},
        }, upsert=True)
        group_parts = await asyncio.to_thread(lambda: list(parts.find({
            "upload_id": key, "logical_index": logical_index,
        }).sort("offset", 1)))
        expected_part_count = math.ceil(group_size / WIRE_CHUNK_BYTES)
        if len(group_parts) == expected_part_count:
            if any(part["offset"] != part_index * WIRE_CHUNK_BYTES
                   or part["size"] != min(WIRE_CHUNK_BYTES, group_size - part["offset"])
                   for part_index, part in enumerate(group_parts)):
                return jsonify({"success": False, "message": "업로드 조각 정보가 올바르지 않습니다."}), 409
            logical_data = b"".join(part["data"] for part in group_parts)
            logical_sha256 = hashlib.sha256(logical_data).hexdigest()
            github_sha = await asyncio.to_thread(_queued_blob, logical_data)
            await asyncio.to_thread(uploads.update_one, {"_id": key}, {
                "$set": {"chunks." + str(logical_index): {
                    "index": logical_index, "sha": github_sha, "size": len(logical_data),
                    "sha256": logical_sha256,
                }},
                "$addToSet": {"uploaded_chunks": logical_index},
            })
            await asyncio.to_thread(parts.delete_many, {"upload_id": key, "logical_index": logical_index})
    except MediaRateLimitError as exc:
        return jsonify({"success": False, "message": str(exc), "retry_after": exc.retry_after}), 429, {"Retry-After": str(math.ceil(exc.retry_after))}
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    latest = await asyncio.to_thread(uploads.find_one, {"_id": key}, {"uploaded_chunks": 1})
    return jsonify({"success": True, "index": index, "logical_index": logical_index,
                    "uploaded_chunks": sorted(latest.get("uploaded_chunks", [])) if latest else []})


@media_bp.post("/api/media/<kind>/<int:item_id>/videos/<media_id>/complete")
async def complete_video(kind, item_id, media_id):
    return await _complete_chunks(kind, item_id, media_id, "video")


@media_bp.post("/api/media/<kind>/<int:item_id>/files/<media_id>/complete")
async def complete_file(kind, item_id, media_id):
    return await _complete_chunks(kind, item_id, media_id, "file")


async def _complete_chunks(kind, item_id, media_id, media_kind):
    doc, problem = await _write_target(kind, item_id)
    if problem:
        return problem
    if not MEDIA_ID.fullmatch(media_id):
        abort(400)
    field = "files" if media_kind == "file" else "images"
    if any(item.get("id") == media_id for item in doc.get(field, [])):
        await asyncio.to_thread(_remove_upload_state, kind, item_id, media_id)
        return jsonify({"success": True, "url": url_for("media.serve_media", kind=kind, item_id=item_id, media_id=media_id)})
    key = ("file:" if media_kind == "file" else "") + _upload_key(kind, item_id, media_id)
    uploads = collection("media_uploads")
    state = await asyncio.to_thread(uploads.find_one, {"_id": key})
    if not state or int(state.get("user_id", -1)) != int(session["user_id"]):
        return jsonify({"success": False, "message": "업로드 상태를 찾을 수 없습니다."}), 404
    if len(doc.get(field, [])) >= MAX_ATTACHMENTS:
        return jsonify({"success": False, "message": "첨부는 최대 5개까지 가능합니다."}), 400
    chunks = state.get("chunks", {})
    count = state["chunk_count"]
    size = state["total_size"]
    parts = [chunks.get(str(index)) for index in range(count)]
    if any(part is None for part in parts):
        return jsonify({"success": False, "message": "누락된 업로드 조각이 있습니다."}), 409
    if sorted(state.get("uploaded_chunks", [])) != list(range(count)) or sum(part["size"] for part in parts) != size:
        return jsonify({"success": False, "message": "업로드 전송이 아직 끝나지 않았습니다."}), 409
    try:
        payload = await request.get_json(silent=True)
        payload = payload if isinstance(payload, dict) else {}
        file_sha256 = payload.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", str(file_sha256)):
            raise ValueError("파일 SHA-256 확인값이 올바르지 않습니다.")
        poster_data = _poster_data(payload.get("poster")) if media_kind == "video" else None
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    directory = {"image": "이미지/", "video": "영상/", "file": "첨부파일/"}[media_kind]
    prefix = _folder(kind) + "/" + str(item_id) + "/" + directory + media_id + "/"
    files = [(prefix + str(index).zfill(4) + ".part", part["sha"]) for index, part in enumerate(parts)]
    try:
        poster = None
        if poster_data:
            poster = {"sha": await asyncio.to_thread(_queued_blob, poster_data), "size": len(poster_data)}
            files.append((prefix + "poster.webp", poster["sha"]))
        await asyncio.to_thread(_commit_blobs, files, "Upload media " + prefix)
        media = {
            "id": media_id, "uuid": media_id, "name": state["name"],
            "kind": media_kind, "mime": state["mime_type"], "mime_type": state["mime_type"],
            "size": size, "total_size": size, "chunk_count": count,
            "chunk_size": state["chunk_size"], "uploaded_chunks": list(range(count)),
            "sha256": file_sha256, "chunks": parts,
        }
        if poster:
            media["poster"] = poster
        saved = await asyncio.to_thread(_save_attachment, kind, item_id, media, field)
    except MediaRateLimitError as exc:
        return jsonify({"success": False, "message": str(exc), "retry_after": exc.retry_after}), 429, {"Retry-After": str(math.ceil(exc.retry_after))}
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    if not saved:
        try:
            await asyncio.to_thread(_discard_untracked_media, kind, item_id, media)
        except MediaStorageError:
            pass
        return jsonify({"success": False, "message": "영상 정보 저장에 실패했습니다."}), 409
    await asyncio.to_thread(uploads.delete_one, {"_id": key})
    await asyncio.to_thread(collection("media_upload_parts").delete_many, {"upload_id": key})
    return jsonify({"success": True, "url": url_for("media.serve_media", kind=kind, item_id=item_id, media_id=media_id)})


@media_bp.route("/media/<kind>/<int:item_id>/<media_id>", methods=["GET", "HEAD"])
async def serve_media(kind, item_id, media_id):
    if kind not in {"posts", "notices"} or not MEDIA_ID.fullmatch(media_id):
        abort(404)
    await ensure_database()
    doc = await asyncio.to_thread(_document, kind, item_id)
    if doc and doc.get("draft_state") and int(doc["author_id"]) != int(session.get("user_id") or 0):
        abort(404)
    media = next((item for field in ("images", "files") for item in (doc or {}).get(field, []) if item.get("id") == media_id), None)
    if not media:
        abort(404)
    is_poster = request.args.get("poster") == "1"
    if is_poster and (media["kind"] != "video" or not media.get("poster")):
        abort(404)
    size = media["poster"]["size"] if is_poster else media["size"]
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store" if doc.get("draft_state") else "public, max-age=3600", "X-Content-Type-Options": "nosniff"}
    download = not is_poster and (media["kind"] == "file" or request.args.get("download") == "1")
    if download:
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(media.get("name") or "attachment", safe="")
    content_type = "image/webp" if is_poster else "application/octet-stream" if media["kind"] == "file" else media["mime"]
    if request.method == "HEAD":
        response = Response(b"", status=200, headers=headers, content_type=content_type)
        response.content_length = size
        return response
    if is_poster:
        try:
            data = await asyncio.to_thread(_read_blob, media["poster"]["sha"])
        except MediaStorageError:
            abort(502)
        if len(data) != size:
            abort(502)
        return Response(data, status=200, headers=headers, content_type=content_type)

    if media["kind"] == "image":
        try:
            if media.get("chunks"):
                image_parts = []
                for part in media["chunks"]:
                    chunk = await asyncio.to_thread(_read_blob, part["sha"])
                    if len(chunk) != part["size"]:
                        abort(502)
                    image_parts.append(chunk)
                data = b"".join(image_parts)
            else:
                data = await asyncio.to_thread(_read_blob, media["sha"])
        except MediaStorageError:
            abort(502)
        if len(data) != size:
            abort(502)
        return Response(data, status=200, headers=headers, content_type=content_type)

    if not request.headers.get("Range") and download:
        async def stream():
            for part in media["chunks"]:
                chunk = await asyncio.to_thread(_read_blob, part["sha"])
                if len(chunk) != part["size"]:
                    raise MediaStorageError("저장된 파일 크기가 올바르지 않습니다.")
                yield chunk

        headers["Content-Length"] = str(size)
        response = Response(stream(), status=200, headers=headers, content_type=content_type)
        response.timeout = None
        return response

    range_value = request.headers.get("Range", "")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_value)
    if range_value and (not match or not any(match.groups())):
        return Response(b"", status=416, headers={"Content-Range": "bytes */" + str(size)})
    if match and not match.group(1):
        start = max(0, size - int(match.group(2)))
    else:
        start = int(match.group(1)) if match else 0
    if start >= size:
        return Response(b"", status=416, headers={"Content-Range": "bytes */" + str(size)})
    chunk_size = media.get("chunk_size", CHUNK_BYTES)
    index = start // chunk_size
    limit = OPEN_PLAYBACK_RANGE_BYTES if media["kind"] == "video" and match and not match.group(2) else MAX_PLAYBACK_RANGE_BYTES
    end = min(size - 1, start + limit - 1)
    if match and match.group(2):
        end = min(end, int(match.group(2)))
    if end < start:
        return Response(b"", status=416, headers={"Content-Range": "bytes */" + str(size)})
    parts = media["chunks"][index:end // chunk_size + 1]
    try:
        first = await asyncio.to_thread(_read_blob, parts[0]["sha"])
    except MediaStorageError:
        abort(502)
    if len(first) != parts[0]["size"]:
        abort(502)

    def selected_bytes(chunk, part_index):
        absolute = part_index * chunk_size
        return chunk[max(0, start - absolute):min(len(chunk), end + 1 - absolute)]

    async def stream_range():
        yield selected_bytes(first, index)
        for group_start in range(1, len(parts), PLAYBACK_READ_CONCURRENCY):
            group = parts[group_start:group_start + PLAYBACK_READ_CONCURRENCY]
            chunks = await asyncio.gather(*(asyncio.to_thread(_read_blob, part["sha"]) for part in group))
            for offset, (chunk, part) in enumerate(zip(chunks, group)):
                if len(chunk) != part["size"]:
                    raise MediaStorageError("저장된 파일 크기가 올바르지 않습니다.")
                yield selected_bytes(chunk, index + group_start + offset)

    headers["Content-Range"] = "bytes " + str(start) + "-" + str(end) + "/" + str(size)
    response = Response(stream_range(), status=206, headers=headers, content_type=content_type)
    response.content_length = end - start + 1
    response.timeout = None
    return response
