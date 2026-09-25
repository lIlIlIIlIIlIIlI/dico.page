import asyncio
import re
import secrets
from functools import wraps
from urllib.parse import urlsplit
from uuid import uuid4

from pymongo.errors import DuplicateKeyError
from quart import Blueprint, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ...module.database  import (
    collection,
    ensure_database,
    next_id_sync,
    utc_now,
)


auth_bp = Blueprint("auth", __name__)

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
NICKNAME_PATTERN = re.compile(r"^[가-힣A-Za-z0-9_]{2,20}$")


def _fetch_user_by_email_sync(email):
    return collection("users").find_one({"email": email}, {"_id": 0})


def _fetch_user_by_id_sync(user_id):
    user = collection("users").find_one(
        {"id": int(user_id)},
        {"_id": 0, "id": 1, "user_uid": 1, "email": 1, "nickname": 1, "role": 1},
    )
    if user:
        profile = collection("user_profiles").find_one(
            {"user_id": int(user_id)}, {"_id": 0, "avatar_url": 1}
        ) or {}
        user["avatar_url"] = profile.get("avatar_url", "") or ""
    return user


async def get_user_by_id(user_id):
    await ensure_database()
    return await asyncio.to_thread(_fetch_user_by_id_sync, user_id)


async def get_current_user():
    if hasattr(g, "current_user"):
        return g.current_user

    user = None
    user_id = session.get("user_id")

    if user_id is not None:
        user = await get_user_by_id(user_id)

    g.current_user = user
    return user


def _fetch_duplicate_sync(email, nickname):
    return collection("users").find_one(
        {"$or": [
            {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
            {"nickname": {"$regex": f"^{re.escape(nickname)}$", "$options": "i"}},
        ]},
        {"_id": 0, "email": 1, "nickname": 1},
    )


def _create_user_sync(email, password_hash, nickname, registration_ip):
    user_uid = uuid4().hex
    created_at = utc_now()
    user = {
        "id": next_id_sync("users"),
        "user_uid": user_uid,
        "email": email,
        "password_hash": password_hash,
        "nickname": nickname,
        "role": "user",
        "registration_ip": registration_ip,
        "created_at": created_at,
        "last_login_at": None,
    }
    collection("users").insert_one(user)
    user.pop("_id", None)
    return user


def _update_last_login_sync(user_id):
    collection("users").update_one(
        {"id": int(user_id)},
        {"$set": {"last_login_at": utc_now()}},
    )


def get_csrf_token():
    token = session.get("csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def validate_csrf_token(form):
    submitted = form.get("csrf_token", "")
    stored = session.get("csrf_token", "")
    return bool(submitted and stored and secrets.compare_digest(submitted, stored))


def safe_next_url(target):
    if not target:
        return url_for("home.index")

    parsed = urlsplit(target)

    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return url_for("home.index")

    return target


def save_login_session(user, remember=False):
    session.clear()
    session.permanent = remember
    session["user_id"] = user["id"]
    session["user_uid"] = user["user_uid"]
    session["nickname"] = user["nickname"]
    session["role"] = user["role"]


def login_required(handler):
    @wraps(handler)
    async def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

        return await handler(*args, **kwargs)

    return wrapped


@auth_bp.app_context_processor
async def inject_auth_context():
    current_user = await get_current_user()

    if "user_id" in session:
        if current_user:
            session["user_uid"] = current_user["user_uid"]
            session["nickname"] = current_user["nickname"]
            session["role"] = current_user["role"]
        else:
            session.clear()

    return {
        "current_user": current_user,
        "csrf_token": get_csrf_token(),
    }


@auth_bp.route("/login", methods=["GET", "POST"])
async def login():
    if "user_id" in session:
        return redirect(url_for("home.index"))

    await ensure_database()
    error = None
    email = ""
    next_url = request.args.get("next", "")

    if request.method == "POST":
        form = await request.form

        if not validate_csrf_token(form):
            abort(400)

        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        next_url = form.get("next", "")
        user = await asyncio.to_thread(_fetch_user_by_email_sync, email)

        if not user or not check_password_hash(user["password_hash"], password):
            error = "이메일 또는 비밀번호가 올바르지 않습니다."
        else:
            await asyncio.to_thread(_update_last_login_sync, user["id"])
            save_login_session(user, remember=form.get("remember") == "on")
            return redirect(safe_next_url(next_url))

    return await render_template(
        "auth/login.html",
        error=error,
        email=email,
        next_url=next_url,
    )


@auth_bp.route("/register", methods=["GET", "POST"])
async def register():
    if "user_id" in session:
        return redirect(url_for("home.index"))

    await ensure_database()
    error = None
    email = ""
    nickname = ""

    if request.method == "POST":
        form = await request.form

        if not validate_csrf_token(form):
            abort(400)

        email = form.get("email", "").strip().lower()
        nickname = form.get("nickname", "").strip()
        password = form.get("password", "")
        password_confirm = form.get("password_confirm", "")

        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 254:
            error = "올바른 이메일 주소를 입력해 주세요."
        elif not NICKNAME_PATTERN.fullmatch(nickname):
            error = "닉네임은 한글, 영문, 숫자, 밑줄로 2~20자까지 사용할 수 있습니다."
        elif len(password) < 8 or len(password) > 128:
            error = "비밀번호는 8자 이상 128자 이하로 입력해 주세요."
        elif password != password_confirm:
            error = "비밀번호 확인이 일치하지 않습니다."
        else:
            duplicate = await asyncio.to_thread(_fetch_duplicate_sync, email, nickname)

            if duplicate and duplicate["email"].lower() == email:
                error = "이미 가입된 이메일입니다."
            elif duplicate and duplicate["nickname"].lower() == nickname.lower():
                error = "이미 사용 중인 닉네임입니다."
            else:
                try:
                    user = await asyncio.to_thread(
                        _create_user_sync,
                        email,
                        generate_password_hash(password),
                        nickname,
                        request.headers.get("CF-Connecting-IP") or request.remote_addr,
                    )
                except DuplicateKeyError:
                    error = "이미 사용 중인 이메일 또는 닉네임입니다."
                else:
                    from .admin import publish_admin_event

                    await publish_admin_event(
                        "user-created",
                        {"user": user},
                    )
                    save_login_session(user)
                    return redirect(url_for("home.index"))

    return await render_template(
        "auth/register.html",
        error=error,
        email=email,
        nickname=nickname,
    )


@auth_bp.post("/logout")
async def logout():
    form = await request.form

    if not validate_csrf_token(form):
        abort(400)

    session.clear()
    redirect_url = url_for("home.index")

    if "application/json" in request.headers.get("Accept", ""):
        return jsonify({
            "success": True,
            "redirect": redirect_url,
        })

    return redirect(redirect_url)
