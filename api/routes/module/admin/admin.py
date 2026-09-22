import asyncio
import json
from functools import wraps

from quart import Blueprint, abort, jsonify, make_response, redirect, render_template, request, session, url_for

from ..auth import (
    ensure_database,
    get_current_user,
    validate_csrf_token,
)
from ....module.database import collection, utc_now
from ..notifications import (
    create_notification,
    create_notice_sync,
    delete_notice_sync,
    get_notices_sync,
)


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
admin_subscribers = {}


async def publish_admin_event(event_name, payload):
    event = {
        "event": event_name,
        "payload": payload,
    }

    for queue in tuple(admin_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def revoke_admin_streams(user_id):
    event = {
        "event": "admin-access-revoked",
        "payload": {"redirect": url_for("home.index")},
    }

    for queue, subscriber_user_id in tuple(admin_subscribers.items()):
        if subscriber_user_id != str(user_id):
            continue

        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _get_dashboard_data_sync():
    today = utc_now()[:10]
    users = collection("users")
    stats = {
        "total_users": users.count_documents({}),
        "admin_users": users.count_documents({"role": "admin"}),
        "today_users": users.count_documents({"created_at": {"$regex": f"^{today}"}}),
    }
    recent_users = list(users.find({}, {"_id": 0, "id": 1, "user_uid": 1, "email": 1, "nickname": 1, "role": 1, "created_at": 1}).sort("id", -1).limit(8))
    return stats, recent_users


def _get_users_sync(search):
    filters = {}
    if search:
        expression = {"$regex": search, "$options": "i"}
        filters["$or"] = [{"email": expression}, {"nickname": expression}, {"user_uid": expression}]
    return list(collection("users").find(filters, {"_id": 0}).sort("id", -1).limit(100))


def _get_notification_recipient_ids_sync():
    return [row["id"] for row in collection("users").find({}, {"_id": 0, "id": 1}).sort("id", 1)]


def _update_user_role_sync(user_id, role, acting_user_id):
    target = collection("users").find_one({"id": int(user_id)}, {"_id": 0})
    if not target:
        return {"status": "not_found"}
    if target["id"] == int(acting_user_id) and target["role"] == "admin" and role != "admin":
        return {"status": "self_demotion"}
    old_role = target["role"]
    collection("users").update_one({"id": int(user_id)}, {"$set": {"role": role}})
    target["role"] = role
    return {"status": "updated", "old_role": old_role, "user": target}


def admin_required(handler):
    @wraps(handler)
    async def wrapped(*args, **kwargs):
        user_id = session.get("user_id")

        if user_id is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

        user = await get_current_user()

        if not user:
            session.clear()
            return redirect(url_for("auth.login"))

        if user["role"] != "admin":
            abort(403)

        session["role"] = user["role"]
        session["nickname"] = user["nickname"]
        return await handler(*args, **kwargs)

    return wrapped


@admin_bp.get("")
@admin_bp.get("/")
@admin_required
async def dashboard():
    await ensure_database()
    stats, recent_users = await asyncio.to_thread(_get_dashboard_data_sync)
    return await render_template(
        "admin/dashboard.html",
        stats=stats,
        recent_users=recent_users,
    )


@admin_bp.get("/users")
@admin_required
async def users():
    search = request.args.get("q", "").strip()[:100]
    return await render_template(
        "admin/users.html",
        user_items=None,
        search=search,
        updated=request.args.get("updated") == "1",
        error=request.args.get("error"),
    )


@admin_bp.get("/api/dashboard")
@admin_required
async def dashboard_data():
    await ensure_database()
    stats, recent_users = await asyncio.to_thread(_get_dashboard_data_sync)
    return jsonify({
        "success": True,
        "stats": stats,
        "recent_users": recent_users,
    })


@admin_bp.get("/api/users")
@admin_required
async def users_data():
    await ensure_database()
    search = request.args.get("q", "").strip()[:100]
    user_items = await asyncio.to_thread(_get_users_sync, search)
    return jsonify({
        "success": True,
        "users": user_items,
    })


@admin_bp.get("/notices")
@admin_required
async def notices():
    await ensure_database()
    notice_items = await asyncio.to_thread(get_notices_sync, 100)
    return await render_template(
        "admin/notices.html",
        notice_items=notice_items,
    )


@admin_bp.post("/notices")
@admin_required
async def create_notice():
    form = await request.form

    if not validate_csrf_token(form):
        abort(400)

    title = form.get("title", "").strip()
    content = form.get("content", "").strip()
    is_pinned = form.get("is_pinned") == "on"
    send_notification = form.get("send_notification") == "on"

    if not title or len(title) > 100:
        return jsonify({
            "success": False,
            "message": "제목은 1자 이상 100자 이하로 입력해 주세요.",
        }), 400

    if not content or len(content) > 5000:
        return jsonify({
            "success": False,
            "message": "내용은 1자 이상 5,000자 이하로 입력해 주세요.",
        }), 400

    notice = await asyncio.to_thread(
        create_notice_sync,
        title,
        content,
        is_pinned,
        session["user_id"],
        session["nickname"],
    )

    delivered_count = 0

    if send_notification:
        recipient_ids = await asyncio.to_thread(_get_notification_recipient_ids_sync)
        notice_url = url_for("notifications.notice_detail", notice_id=notice["id"])

        for index in range(0, len(recipient_ids), 100):
            batch = recipient_ids[index:index + 100]
            results = await asyncio.gather(*(
                create_notification(
                    recipient_user_id=user_id,
                    message=f"새 공지: {title}",
                    sender="Dico.page",
                    category="notice",
                    url=notice_url,
                )
                for user_id in batch
            ))
            delivered_count += sum(result is not None for result in results)

    await publish_admin_event("notice-created", {"notice": notice})
    return jsonify({
        "success": True,
        "message": "공지를 작성했습니다.",
        "notice": notice,
        "delivered_count": delivered_count,
    })


@admin_bp.delete("/notices/<int:notice_id>")
@admin_required
async def delete_notice(notice_id):
    csrf_token = request.headers.get("X-CSRF-Token", "")

    if not validate_csrf_token({"csrf_token": csrf_token}):
        abort(400)

    deleted = await asyncio.to_thread(delete_notice_sync, notice_id)

    if not deleted:
        return jsonify({
            "success": False,
            "message": "공지를 찾을 수 없습니다.",
        }), 404

    await publish_admin_event("notice-deleted", {"notice_id": notice_id})
    return jsonify({
        "success": True,
        "message": "공지를 삭제했습니다.",
    })


@admin_bp.get("/stream")
@admin_required
async def admin_stream():
    queue = asyncio.Queue(maxsize=100)
    admin_subscribers[queue] = str(session["user_id"])

    async def send_events():
        try:
            yield b"retry: 3000\n\n"

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=20)
                    # 예기치 않은 MongoDB ObjectId가 포함되어도 관리자 SSE가
                    # 스트림 전체를 종료하지 않도록 문자열로 안전하게 변환합니다.
                    data = json.dumps(item["payload"], ensure_ascii=False, default=str)
                    yield f"event: {item['event']}\ndata: {data}\n\n".encode("utf-8")

                    if item["event"] == "admin-access-revoked":
                        break
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
        finally:
            admin_subscribers.pop(queue, None)

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


@admin_bp.post("/users/<int:user_id>/role")
@admin_required
async def update_user_role(user_id):
    form = await request.form

    if not validate_csrf_token(form):
        abort(400)

    role = form.get("role", "")

    if role not in {"user", "admin"}:
        abort(400)

    result = await asyncio.to_thread(
        _update_user_role_sync,
        user_id,
        role,
        session["user_id"],
    )

    wants_json = "application/json" in request.headers.get("Accept", "")

    if result["status"] == "not_found":
        if wants_json:
            return jsonify({"success": False, "message": "회원을 찾을 수 없습니다."}), 404
        abort(404)

    if result["status"] == "self_demotion":
        if wants_json:
            return jsonify({
                "success": False,
                "message": "현재 로그인한 관리자의 권한은 직접 해제할 수 없습니다.",
            }), 400
        return redirect(url_for("admin.users", error="self_demotion"))

    if result["old_role"] == "admin" and result["user"]["role"] != "admin":
        await revoke_admin_streams(result["user"]["id"])

    await publish_admin_event(
        "user-role-updated",
        {
            "user": result["user"],
            "old_role": result["old_role"],
        },
    )

    if wants_json:
        return jsonify({
            "success": True,
            "message": "회원 권한을 변경했습니다.",
            "user": result["user"],
        })

    return redirect(url_for("admin.users", updated="1"))
