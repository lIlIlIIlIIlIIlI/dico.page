import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from markupsafe import Markup
from quart import Blueprint, abort, jsonify, make_response, render_template, request, session

from routes.module.auth import ensure_database, login_required, validate_csrf_token
from module.database import collection, next_id_sync, utc_now


notification_bp = Blueprint("notifications", __name__)

# 개발용 메모리 저장소입니다. 실제 서비스에서는 DB로 교체하세요.
notification_store = defaultdict(list)
notification_subscribers = defaultdict(set)
SESSION_USER_KEY = "user_id"
NOTIFICATION_SETTING_KEYS = (
    "all_enabled",
    "notice_enabled",
    "activity_enabled",
    "benefit_enabled",
)


def _default_settings():
    return {key: True for key in NOTIFICATION_SETTING_KEYS}


def _get_notification_settings_sync(user_id):
    row = collection("notification_settings").find_one({"user_id": int(user_id)}, {"_id": 0})
    if not row:
        return _default_settings()

    return {key: bool(row[key]) for key in NOTIFICATION_SETTING_KEYS}


def _save_notification_settings_sync(user_id, settings):
    collection("notification_settings").replace_one(
        {"user_id": int(user_id)},
        {"user_id": int(user_id), **{key: bool(settings[key]) for key in NOTIFICATION_SETTING_KEYS}, "updated_at": utc_now()},
        upsert=True,
    )
    return settings


def get_notices_sync(limit=50):
    notices = list(collection("notices").find({}, {"_id": 0}).sort([("is_pinned", -1), ("id", -1)]).limit(limit))
    for notice in notices:
        notice["is_pinned"] = bool(notice["is_pinned"])
    return notices


def get_notice_sync(notice_id):
    row = collection("notices").find_one({"id": int(notice_id)}, {"_id": 0})
    if not row:
        return None

    notice = dict(row)
    notice["is_pinned"] = bool(notice["is_pinned"])
    return notice


def create_notice_sync(title, content, is_pinned, author_id, author_nickname):
    notice_id = next_id_sync("notices")
    notice = {
        "id": notice_id,
        "title": title,
        "content": content,
        "is_pinned": bool(is_pinned),
        "author_id": author_id,
        "author_nickname": author_nickname,
        "created_at": utc_now(),
    }
    # PyMongo는 전달한 딕셔너리에 _id(ObjectId)를 추가하므로
    # API/SSE 응답에 ObjectId가 섞이지 않도록 복사본을 저장합니다.
    collection("notices").insert_one(dict(notice))
    return notice


def delete_notice_sync(notice_id):
    return collection("notices").delete_one({"id": int(notice_id)}).deleted_count > 0


def get_current_user_id():
    user_id = get_optional_user_id()

    if user_id is None:
        abort(401)

    return user_id


def get_optional_user_id():
    user_id = session.get(SESSION_USER_KEY)
    return str(user_id) if user_id is not None else None


def get_notifications(user_id, limit=10):
    return list(
        collection("notifications")
        .find({"recipient_user_id": int(user_id)}, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
    )


@notification_bp.app_context_processor
async def inject_notification_context():
    user_id = get_optional_user_id()

    if user_id is None:
        return {
            "notifications": [],
            "unread_count": 0,
            "total_notification_count": 0,
            "visible_notification_count": 0,
        }

    await ensure_database()
    all_notifications = list(collection("notifications").find({"recipient_user_id": int(user_id)}, {"_id": 0}))
    visible_notifications = get_notifications(user_id, limit=10)
    unread_count = sum(
        notification.get("is_read") is not True
        for notification in all_notifications
    )

    return {
        "notifications": visible_notifications,
        "unread_count": unread_count,
        "total_notification_count": len(all_notifications),
        "visible_notification_count": len(visible_notifications),
    }


async def create_notification(
    recipient_user_id,
    message,
    sender="Dico",
    category="notice",
    url="#",
    image=None,
):
    if recipient_user_id is None:
        raise ValueError("recipient_user_id가 필요합니다.")

    recipient_user_id = int(recipient_user_id)
    await ensure_database()
    settings = await asyncio.to_thread(_get_notification_settings_sync, recipient_user_id)
    category_key = f"{category}_enabled"

    if not settings["all_enabled"] or not settings.get(category_key, True):
        return None

    notification = {
        "id": uuid4().hex,
        # 수신자별 DB 조회와 읽음 처리를 위해 수신자 ID를 문서에 저장합니다.
        "recipient_user_id": recipient_user_id,
        "message": message,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "url": url,
        "image": image,
        "sender": sender,
        "category": category,
        "is_read": False,
    }

    collection("notifications").insert_one(dict(notification))
    recipient_key = str(recipient_user_id)
    notification_store[recipient_key].insert(0, notification)

    # 세션 사용자 ID와 SSE 구독 키 타입을 문자열로 통일합니다.
    for queue in tuple(notification_subscribers[recipient_key]):
        try:
            queue.put_nowait(notification)
        except asyncio.QueueFull:
            pass

    return notification


@notification_bp.get("/settings/notifications")
@login_required
async def notification_settings():
    await ensure_database()
    settings = await asyncio.to_thread(
        _get_notification_settings_sync,
        session[SESSION_USER_KEY],
    )
    return await render_template(
        "settings/notifications.html",
        notification_settings=settings,
    )


@notification_bp.post("/api/settings/notifications")
@login_required
async def update_notification_settings():
    csrf_token = request.headers.get("X-CSRF-Token", "")

    if not validate_csrf_token({"csrf_token": csrf_token}):
        abort(400)

    data = await request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"success": False, "message": "올바른 설정값이 아닙니다."}), 400

    settings = {
        key: data.get(key) is True
        for key in NOTIFICATION_SETTING_KEYS
    }

    await ensure_database()
    saved_settings = await asyncio.to_thread(
        _save_notification_settings_sync,
        session[SESSION_USER_KEY],
        settings,
    )
    return jsonify({
        "success": True,
        "message": "알림 설정을 저장했습니다.",
        "settings": saved_settings,
    })


@notification_bp.get("/notices")
async def notice_list():
    await ensure_database()
    notices = await asyncio.to_thread(get_notices_sync, 50)
    return await render_template(
        "notices/index.html",
        notices=notices,
    )


@notification_bp.get("/notices/<int:notice_id>")
async def notice_detail(notice_id):
    await ensure_database()
    notice = await asyncio.to_thread(get_notice_sync, notice_id)

    if not notice:
        abort(404)

    from ..home import _render_markdown

    rendered_content = Markup(_render_markdown(notice["content"]))
    notice["content"] = rendered_content
    notice["content_html"] = rendered_content
    return await render_template(
        "notices/detail.html",
        notice=notice,
    )


@notification_bp.get("/api/notifications/stream")
async def notification_stream():
    if "text/event-stream" not in request.accept_mimetypes:
        abort(400)

    user_id = get_current_user_id()
    queue = asyncio.Queue(maxsize=100)
    notification_subscribers[user_id].add(queue)

    async def send_events():
        try:
            yield b"retry: 3000\n\n"

            while True:
                try:
                    notification = await asyncio.wait_for(queue.get(), timeout=20)
                    data = json.dumps(notification, ensure_ascii=False)
                    yield f"event: notification\ndata: {data}\n\n".encode("utf-8")
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
        finally:
            notification_subscribers[user_id].discard(queue)
            if not notification_subscribers[user_id]:
                notification_subscribers.pop(user_id, None)

    response = await make_response(
        send_events(),
        {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Transfer-Encoding": "chunked",
            "X-Accel-Buffering": "no",
        },
    )
    response.timeout = None
    return response


@notification_bp.post("/api/notifications/read-all")
async def read_all_notifications():
    user_id = get_current_user_id()

    collection("notifications").update_many(
        {"recipient_user_id": int(user_id), "is_read": {"$ne": True}},
        {"$set": {"is_read": True}},
    )
    for notification in notification_store[user_id]:
        notification["is_read"] = True

    return jsonify({"success": True})


@notification_bp.post("/api/notifications/<notification_id>/read")
async def read_notification(notification_id):
    user_id = get_current_user_id()
    result = collection("notifications").update_one(
        {"recipient_user_id": int(user_id), "id": notification_id},
        {"$set": {"is_read": True}},
    )
    if result.matched_count:
        user_notifications = notification_store[user_id]
        for notification in user_notifications:
            if notification.get("id") == notification_id:
                notification["is_read"] = True
                break
        unread_count = collection("notifications").count_documents(
            {"recipient_user_id": int(user_id), "is_read": {"$ne": True}}
        )
        return jsonify({"success": True, "notification_id": notification_id, "unread_count": unread_count})

    return jsonify({
        "success": False,
        "message": "알림을 찾을 수 없습니다.",
    }), 404


@notification_bp.get("/api/notifications/test")
async def create_test_notification():
    notification = await create_notification(
        recipient_user_id=get_current_user_id(),
        message="실시간 테스트 알림이 도착했습니다.",
        sender="Dico",
        category="notice",
        url="#",
    )
    return jsonify({
        "success": True,
        "suppressed": notification is None,
        "notification": notification,
    })
