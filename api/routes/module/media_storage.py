import asyncio
import base64
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pymongo import ReturnDocument
from quart import Blueprint, Response, abort, jsonify, request, session, url_for

from ...module.database import collection, next_id_sync
from .auth import ensure_database, get_current_user, validate_csrf_token


media_bp = Blueprint("media", __name__)
REPOSITORY = "dico-page/postimage"
CHUNK_BYTES = 768 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENTS = 5
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
VIDEO_TYPES = {"video/mp4": "mp4", "video/webm": "webm", "video/ogg": "ogv"}
FILE_TYPES = {".pdf", ".zip", ".txt", ".csv", ".hwp", ".hwpx", ".docx", ".xlsx", ".pptx"}
MEDIA_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class MediaStorageError(Exception):
    pass


def _github(method, endpoint, payload=None):
    token = os.getenv("postimage", "").strip()
    if not token:
        raise MediaStorageError("서버에 postimage GitHub 토큰이 설정되지 않았습니다.")
    url = "https://api.github.com/repos/" + REPOSITORY + endpoint
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dico-page-media",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=body, headers=headers, method=method), timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise MediaStorageError("postimage 토큰의 저장소 접근 권한을 확인해 주세요.") from exc
        if exc.code in (409, 422):
            raise MediaStorageError("GitHub 저장소가 동시에 변경되었습니다. 다시 시도해 주세요.") from exc
        if exc.code == 404:
            raise MediaStorageError("GitHub 저장소 또는 파일을 찾을 수 없습니다.") from exc
        raise MediaStorageError("GitHub 미디어 저장 요청이 실패했습니다.") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise MediaStorageError("GitHub 미디어 저장소에 연결하지 못했습니다.") from exc


def _blob(data):
    result = _github("POST", "/git/blobs", {
        "content": base64.b64encode(data).decode("ascii"), "encoding": "base64",
    })
    return result["sha"]


def _read_blob(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
        raise MediaStorageError("미디어 정보가 올바르지 않습니다.")
    result = _github("GET", "/git/blobs/" + sha)
    if result.get("encoding") != "base64":
        raise MediaStorageError("저장된 미디어를 읽을 수 없습니다.")
    return base64.b64decode(result["content"], validate=False)


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
        return [prefix + "이미지/" + media["id"] + "." + IMAGE_TYPES[media["mime"]]]
    directory = "영상/" if media["kind"] == "video" else "첨부파일/"
    return [prefix + directory + media["id"] + "/" + str(index).zfill(4) + ".part"
            for index in range(len(media["chunks"]))]


def _discard_untracked_media(kind, item_id, media):
    doc = _document(kind, item_id)
    if doc and any(item.get("id") == media["id"] for field in ("images", "files") for item in doc.get(field, [])):
        return
    _commit_blobs([(path, None) for path in _media_paths(kind, item_id, media)], "Remove incomplete media " + media["id"])


def _remove_upload_state(kind, item_id, media_id=None):
    uploads = collection("media_uploads")
    if media_id:
        key = _upload_key(kind, item_id, media_id)
        uploads.delete_many({"_id": {"$in": [key, "file:" + key]}})
    else:
        uploads.delete_many({"_id": {"$regex": r"^(?:file:)?" + re.escape(kind + ":" + str(item_id) + ":")}})


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
        sha = await asyncio.to_thread(_blob, data)
        await asyncio.to_thread(_commit_blobs, [(path, sha)], "Upload " + path)
        media = {"id": media_id, "name": name, "kind": "image", "mime": mime, "size": len(data), "sha": sha}
        saved = await asyncio.to_thread(_save_attachment, kind, item_id, media)
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    if not saved:
        try:
            await asyncio.to_thread(_discard_untracked_media, kind, item_id, media)
        except MediaStorageError:
            pass
        return jsonify({"success": False, "message": "첨부 정보 저장에 실패했습니다."}), 409
    return jsonify({"success": True, "url": url_for("media.serve_media", kind=kind, item_id=item_id, media_id=media_id)})


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
    field = "images" if media_kind == "video" else "files"
    if len(doc.get(field, [])) >= MAX_ATTACHMENTS:
        return jsonify({"success": False, "message": "첨부는 최대 5개까지 가능합니다."}), 400
    if not MEDIA_ID.fullmatch(media_id):
        abort(400)
    try:
        name = _name(request.args.get("name"))
        mime = request.args["mime"]
        size = int(request.args["size"])
        count = int(request.args["count"])
    except (KeyError, ValueError) as exc:
        return jsonify({"success": False, "message": "영상 정보가 올바르지 않습니다."}), 400
    if media_kind == "video":
        valid_type = mime in VIDEO_TYPES
    else:
        valid_type = mime == "application/octet-stream" and os.path.splitext(name)[1].lower() in FILE_TYPES
    if not valid_type or not 0 < size <= MAX_VIDEO_BYTES or count != math.ceil(size / CHUNK_BYTES) or not 0 <= index < count:
        return jsonify({"success": False, "message": "파일 형식 또는 크기가 올바르지 않습니다."}), 400
    expected = min(CHUNK_BYTES, size - index * CHUNK_BYTES)
    if request.content_length and request.content_length > expected:
        return jsonify({"success": False, "message": "영상 조각의 크기가 초과되었습니다."}), 413
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
    if len(data) != expected or (index == 0 and not valid_signature):
        return jsonify({"success": False, "message": "파일 조각이 올바르지 않습니다."}), 400
    key = ("file:" if media_kind == "file" else "") + _upload_key(kind, item_id, media_id)
    uploads = collection("media_uploads")
    state = await asyncio.to_thread(uploads.find_one, {"_id": key})
    if state and (state["user_id"] != session["user_id"] or state["size"] != size or state["mime"] != mime or state["name"] != name or state["count"] != count):
        return jsonify({"success": False, "message": "업로드 정보가 서로 다릅니다."}), 409
    try:
        sha = await asyncio.to_thread(_blob, data)
        await asyncio.to_thread(uploads.update_one, {"_id": key}, {
            "$setOnInsert": {
                "user_id": session["user_id"], "name": name, "mime": mime,
                "size": size, "count": count,
                "expire_at": datetime.now(timezone.utc) + timedelta(hours=24),
            },
            "$set": {"chunks." + str(index): {"sha": sha, "size": len(data)}},
        }, upsert=True)
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    return jsonify({"success": True, "index": index})


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
    field = "images" if media_kind == "video" else "files"
    if any(item.get("id") == media_id for item in doc.get(field, [])):
        return jsonify({"success": True, "url": url_for("media.serve_media", kind=kind, item_id=item_id, media_id=media_id)})
    key = ("file:" if media_kind == "file" else "") + _upload_key(kind, item_id, media_id)
    uploads = collection("media_uploads")
    state = await asyncio.to_thread(uploads.find_one, {"_id": key})
    if not state or state["user_id"] != session["user_id"]:
        return jsonify({"success": False, "message": "영상 업로드를 찾을 수 없습니다."}), 404
    if len(doc.get(field, [])) >= MAX_ATTACHMENTS:
        return jsonify({"success": False, "message": "첨부는 최대 5개까지 가능합니다."}), 400
    chunks = state.get("chunks", {})
    if len(chunks) != state["count"] or sum(part["size"] for part in chunks.values()) != state["size"]:
        return jsonify({"success": False, "message": "영상 전송이 아직 끝나지 않았습니다."}), 409
    parts = [chunks.get(str(index)) for index in range(state["count"])]
    if any(part is None for part in parts):
        return jsonify({"success": False, "message": "누락된 영상 조각이 있습니다."}), 409
    prefix = _folder(kind) + "/" + str(item_id) + ("/영상/" if media_kind == "video" else "/첨부파일/") + media_id + "/"
    files = [(prefix + str(index).zfill(4) + ".part", part["sha"]) for index, part in enumerate(parts)]
    try:
        await asyncio.to_thread(_commit_blobs, files, "Upload video " + prefix)
        media = {
            "id": media_id, "name": state["name"], "kind": media_kind, "mime": state["mime"],
            "size": state["size"], "chunks": parts,
        }
        saved = await asyncio.to_thread(_save_attachment, kind, item_id, media, field)
    except MediaStorageError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502
    if not saved:
        try:
            await asyncio.to_thread(_discard_untracked_media, kind, item_id, media)
        except MediaStorageError:
            pass
        return jsonify({"success": False, "message": "영상 정보 저장에 실패했습니다."}), 409
    await asyncio.to_thread(uploads.delete_one, {"_id": key})
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
    size = media["size"]
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store" if doc.get("draft_state") else "public, max-age=3600", "X-Content-Type-Options": "nosniff"}
    download = media["kind"] == "file" or request.args.get("download") == "1"
    if download:
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(media.get("name") or "attachment", safe="")
    content_type = "application/octet-stream" if media["kind"] == "file" else media["mime"]
    if request.method == "HEAD":
        response = Response(b"", status=200, headers=headers, content_type=content_type)
        response.content_length = size
        return response
    if media["kind"] == "image":
        try:
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
    index = start // CHUNK_BYTES
    offset = start % CHUNK_BYTES
    part = media["chunks"][index]
    end = min(size - 1, index * CHUNK_BYTES + part["size"] - 1)
    if match and match.group(2):
        end = min(end, int(match.group(2)))
    if end < start:
        return Response(b"", status=416, headers={"Content-Range": "bytes */" + str(size)})
    try:
        chunk = await asyncio.to_thread(_read_blob, part["sha"])
    except MediaStorageError:
        abort(502)
    if len(chunk) != part["size"]:
        abort(502)
    headers["Content-Range"] = "bytes " + str(start) + "-" + str(end) + "/" + str(size)
    return Response(chunk[offset:offset + end - start + 1], status=206, headers=headers, content_type=content_type)
