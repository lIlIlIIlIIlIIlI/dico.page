import asyncio
import base64
import binascii
import html
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

try:
    import bleach
    import markdown as markdown_module
except ImportError:
    bleach = None
    markdown_module = None

from quart import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session, url_for

from .module.auth import NICKNAME_PATTERN, ensure_database, login_required, validate_csrf_token
from .module.notifications import create_notification, get_notices_sync
from ..module.database import collection, next_id_sync, utc_now



home_bp = Blueprint("home", __name__)
POST_CATEGORIES = {
    "general": "자유",
    "question": "질문",
    "info": "정보",
}
MAX_AVATAR_BYTES = 512 * 1024


def _validate_avatar_data(value):
    if len(value) > 700_000:
        raise ValueError("프로필 이미지는 512KB 이하로 저장할 수 있습니다.")
    match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})", value)
    if not match:
        raise ValueError("PNG, JPG, WebP, GIF 이미지만 사용할 수 있습니다.")
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("이미지 데이터를 읽을 수 없습니다.") from None
    if not data or len(data) > MAX_AVATAR_BYTES:
        raise ValueError("프로필 이미지는 512KB 이하로 저장할 수 있습니다.")
    image_type = match.group(1)
    valid = {
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": data.startswith(b"\xff\xd8\xff"),
        "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "gif": data.startswith((b"GIF87a", b"GIF89a")),
    }
    if not valid[image_type]:
        raise ValueError("이미지 형식과 내용이 일치하지 않습니다.")
    return value


def _get_profile_settings_sync(user_id):
    user = collection("users").find_one(
        {"id": int(user_id)}, {"_id": 0, "id": 1, "user_uid": 1, "nickname": 1}
    )
    if not user:
        return None
    saved = collection("user_profiles").find_one({"user_id": int(user_id)}, {"_id": 0}) or {}
    return {
        **user,
        "headline": saved.get("headline", "") or "",
        "bio": saved.get("bio", "") or "",
        "avatar_url": saved.get("avatar_url", "") or "",
    }


def _save_profile_settings_sync(user_id, nickname, headline, bio, avatar_action, avatar_data):
    user_id = int(user_id)
    old_user = collection("users").find_one({"id": user_id}, {"_id": 0, "nickname": 1})
    if not old_user:
        return False
    if nickname != old_user["nickname"]:
        collection("users").update_one({"id": user_id}, {"$set": {"nickname": nickname}})
        collection("posts").update_many({"author_id": user_id}, {"$set": {"author_nickname": nickname}})
        collection("comments").update_many({"author_id": user_id}, {"$set": {"author_nickname": nickname}})
    changes = {"headline": headline, "bio": bio, "updated_at": utc_now()}
    if avatar_action == "replace":
        changes["avatar_url"] = avatar_data
    elif avatar_action == "remove":
        changes["avatar_url"] = ""
    collection("user_profiles").update_one(
        {"user_id": user_id}, {"$set": changes}, upsert=True
    )
    return True


def _profile_activity_grid(events, today=None):
    """최근 1년의 글·댓글을 GitHub식 주간 활동 셀로 변환합니다."""
    today = today or datetime.now(timezone.utc).date()
    first_day = today - timedelta(days=364)
    grid_start = first_day - timedelta(days=(first_day.weekday() + 1) % 7)
    counts = {}
    for event in events:
        day = str(event.get("day") or "")[:10]
        if first_day.isoformat() <= day <= today.isoformat():
            counts[day] = counts.get(day, 0) + int(event.get("count") or 0)

    def level(count):
        if count <= 0:
            return 0
        if count == 1:
            return 1
        if count <= 3:
            return 2
        if count <= 6:
            return 3
        return 4

    weeks = []
    for week_index in range(53):
        week = []
        for day_index in range(7):
            current = grid_start + timedelta(days=week_index * 7 + day_index)
            key = current.isoformat()
            outside = current < first_day or current > today
            count = 0 if outside else counts.get(key, 0)
            week.append({"date": key, "count": count, "level": level(count), "outside": outside})
        weeks.append(week)
    return weeks, sum(counts.values())


def _get_profile_sync(user_uid):
    """사용자 정보와 최근 글·댓글·활동량을 한 번에 집계합니다."""
    today = datetime.now(timezone.utc).date()
    first_day = today - timedelta(days=364)

    user = collection("users").find_one(
        {"user_uid": {"$regex": f"^{re.escape(user_uid)}$", "$options": "i"}},
        {"_id": 0, "id": 1, "user_uid": 1, "nickname": 1, "role": 1, "created_at": 1},
    )
    if not user:
        return None

    user_id = int(user["id"])
    profile_document = collection("user_profiles").find_one({"user_id": user_id}, {"_id": 0}) or {}
    posts = list(
        collection("posts").find(
            {"author_id": user_id},
            {"_id": 0, "id": 1, "title": 1, "category": 1, "created_at": 1, "views": 1},
        ).sort("id", -1).limit(10)
    )
    for post in posts:
        post["comment_count"] = collection("comments").count_documents({"post_id": post["id"]})
    comments = list(
        collection("comments").find(
            {"author_id": user_id},
            {"_id": 0, "id": 1, "content": 1, "created_at": 1, "post_id": 1},
        ).sort("id", -1).limit(10)
    )
    for comment in comments:
        post = collection("posts").find_one({"id": comment["post_id"]}, {"_id": 0, "title": 1})
        comment["post_title"] = post.get("title", "삭제된 게시글") if post else "삭제된 게시글"

    posts_count = collection("posts").count_documents({"author_id": user_id})
    comments_count = collection("comments").count_documents({"author_id": user_id})
    user_post_ids = [row["id"] for row in collection("posts").find({"author_id": user_id}, {"id": 1, "_id": 0})]
    likes_received = collection("post_likes").count_documents({"post_id": {"$in": user_post_ids}}) if user_post_ids else 0

    events = {}
    for item in list(collection("posts").find({"author_id": user_id}, {"created_at": 1, "_id": 0})) + list(collection("comments").find({"author_id": user_id}, {"created_at": 1, "_id": 0})):
        day = str(item.get("created_at", ""))[:10]
        if first_day.isoformat() <= day <= today.isoformat():
            events[day] = events.get(day, 0) + 1

    activity_weeks, activity_total = _profile_activity_grid(
        [{"day": day, "count": count} for day, count in events.items()],
        today,
    )
    activity_months = []
    last_month = None
    for week_index, week in enumerate(activity_weeks):
        for day_index, cell in enumerate(week):
            if cell["outside"]:
                continue
            month = cell["date"][:7]
            if month != last_month:
                activity_months.append({
                    "label": f"{month[:4]}년 {int(month[5:])}월",
                    "weeks": [],
                    "total": 0,
                })
                last_month = month
            month_data = activity_months[-1]
            if not month_data["weeks"] or month_data["weeks"][-1]["index"] != week_index:
                month_data["weeks"].append({"index": week_index, "days": [None] * 7})
            month_data["weeks"][-1]["days"][day_index] = cell
            month_data["total"] += cell["count"]
    profile = dict(user)
    profile.update({
        "avatar_url": profile_document.get("avatar_url", "") or "",
        "headline": profile_document.get("headline", "") or "",
        "bio": profile_document.get("bio", "") or "",
        "posts": posts,
        "comments": comments,
        "posts_count": int(posts_count),
        "comments_count": int(comments_count),
        "likes_received": int(likes_received),
        "activity_weeks": activity_weeks,
        "activity_months": activity_months,
        "activity_year": today.year,
        "activity_start": first_day.isoformat(),
        "activity_end": today.isoformat(),
        "activity_total": int(activity_total),
    })
    return profile


def _get_posts_sync(search, category, page, per_page):
    filters = {}
    if search:
        expression = {"$regex": re.escape(search), "$options": "i"}
        filters["$or"] = [{"title": expression}, {"content": expression}, {"author_nickname": expression}]
    if category in POST_CATEGORIES:
        filters["category"] = category

    posts_collection = collection("posts")
    total = posts_collection.count_documents(filters)
    total_pages = max(1, math.ceil(total / per_page))
    page = min(max(page, 1), total_pages)
    rows = list(
        posts_collection.find(filters, {"_id": 0}).sort("id", -1)
        .skip((page - 1) * per_page).limit(per_page)
    )
    for row in rows:
        row["comment_count"] = collection("comments").count_documents({"post_id": row["id"]})
        row["like_count"] = collection("post_likes").count_documents({"post_id": row["id"]})
    return rows, total, page, total_pages


def _create_post_sync(title, content, category, media_url, author_id, author_nickname):
    post_id = next_id_sync("posts")
    collection("posts").insert_one({
        "id": post_id,
        "title": title,
        "content": content,
        "category": category,
        "media_url": media_url or None,
        "author_id": int(author_id),
        "author_nickname": author_nickname,
        "views": 0,
        "created_at": utc_now(),
    })
    return post_id


def _get_post_sync(post_id, current_user_id=None):
    posts_collection = collection("posts")
    row = posts_collection.find_one_and_update(
        {"id": int(post_id)}, {"$inc": {"views": 1}}, return_document=ReturnDocument.AFTER
    )
    if not row:
        return None, [], False
    row.pop("_id", None)
    row["comment_count"] = collection("comments").count_documents({"post_id": int(post_id)})
    row["like_count"] = collection("post_likes").count_documents({"post_id": int(post_id)})
    comments = list(collection("comments").find(
        {"post_id": int(post_id)}, {"_id": 0, "id": 1, "author_id": 1, "author_nickname": 1, "content": 1, "created_at": 1}
    ).sort("id", 1))
    liked = bool(current_user_id is not None and collection("post_likes").find_one({"post_id": int(post_id), "user_id": int(current_user_id)}))
    return row, comments, liked


def _create_comment_sync(post_id, user_id, nickname, content):
    if not collection("posts").find_one({"id": int(post_id)}, {"_id": 1}):
        return None
    comment = {
        "id": next_id_sync("comments"),
        "post_id": int(post_id),
        "author_id": user_id,
        "author_nickname": nickname,
        "content": content,
        "created_at": utc_now(),
    }
    # PyMongo는 insert_one() 호출 시 원본 딕셔너리에 _id(ObjectId)를 추가합니다.
    # 이 값을 그대로 Quart jsonify()에 넘기면 ObjectId가 JSON으로 직렬화되지
    # 않으므로, 저장용 딕셔너리와 API 응답용 딕셔너리를 분리합니다.
    collection("comments").insert_one(dict(comment))
    return comment


def _get_post_notification_target_sync(post_id):
    """알림 대상 게시글의 작성자 정보를 가져옵니다."""
    return collection("posts").find_one({"id": int(post_id)}, {"_id": 0, "author_id": 1, "title": 1})


def _toggle_like_sync(post_id, user_id):
    post_id = int(post_id)
    user_id = int(user_id)
    if not collection("posts").find_one({"id": post_id}, {"_id": 1}):
        return None
    likes = collection("post_likes")
    existing = likes.find_one({"post_id": post_id, "user_id": user_id})
    if existing:
        likes.delete_one({"post_id": post_id, "user_id": user_id})
        liked = False
    else:
        likes.insert_one({"post_id": post_id, "user_id": user_id, "created_at": utc_now()})
        liked = True
    return liked, likes.count_documents({"post_id": post_id})


def _get_media_info(media_url):
    if not media_url:
        return None, None

    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, None

    host = parsed.netloc.lower().removeprefix("www.")
    video_id = None
    playlist_id = None

    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
    }:
        query = parse_qs(parsed.query)
        playlist_id = query.get("list", [None])[0]
        if parsed.path == "/watch":
            video_id = query.get("v", [None])[0]
        elif (
            parsed.path.startswith("/shorts/")
            or parsed.path.startswith("/embed/")
            or parsed.path.startswith("/live/")
        ):
            video_id = parsed.path.split("/")[2]

    if host in {"instagram.com", "instagr.am"}:
        instagram_type = parsed.path.strip("/").split("/")
        if len(instagram_type) >= 2 and instagram_type[0].lower() in {"reel", "reels", "p", "tv"}:
            shortcode = instagram_type[1]
            if re.fullmatch(r"[A-Za-z0-9_-]{2,100}", shortcode):
                content_type = "reel" if instagram_type[0].lower() in {"reel", "reels"} else instagram_type[0].lower()
                return "instagram", f"https://www.instagram.com/{content_type}/{shortcode}/embed"

    if (
        playlist_id
        and re.fullmatch(r"[A-Za-z0-9_-]{10,100}", playlist_id)
        and parsed.path in {"/playlist", "/watch"}
    ):
        return "youtube", f"https://www.youtube-nocookie.com/embed/videoseries?list={playlist_id}"

    if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        return "youtube", f"https://www.youtube-nocookie.com/embed/{video_id}"

    path = parsed.path.lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return "image", media_url
    if path.endswith((".mp4", ".webm", ".ogg")):
        return "video", media_url
    return "link", media_url


def _safe_http_url(value):
    candidate = html.unescape(value).strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return html.escape(candidate, quote=True)


def _render_inline_markdown(value):
    tokens = []

    def keep(fragment):
        token = f"\x00DICO{len(tokens)}\x00"
        tokens.append(fragment)
        return token

    def render_code(match):
        return keep(f"<code>{html.escape(match.group(1))}</code>")

    def render_image(match):
        source = _safe_http_url(match.group(2))
        if not source:
            return match.group(0)
        alt = html.escape(match.group(1), quote=True)
        return keep(f'<img src="{source}" alt="{alt}" loading="lazy">')

    def render_link(match):
        target = _safe_http_url(match.group(2))
        if not target:
            return match.group(0)
        label = html.escape(match.group(1))
        return keep(
            f'<a href="{target}" target="_blank" rel="noopener noreferrer">{label}</a>'
        )

    def render_url(match):
        candidate = match.group(0)
        trailing = ""
        while candidate and candidate[-1] in ".,!?:;":
            trailing = candidate[-1] + trailing
            candidate = candidate[:-1]

        target = _safe_http_url(candidate)
        if not target:
            return match.group(0)
        return keep(
            f'<a href="{target}" target="_blank" rel="noopener noreferrer">'
            f"{html.escape(candidate)}</a>"
        ) + trailing

    rendered = str(value)
    rendered = re.sub(r"`([^`\n]+)`", render_code, rendered)
    rendered = re.sub(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)", render_image, rendered)
    rendered = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", render_link, rendered)
    rendered = re.sub(r"https?://[^\s<>()]+", render_url, rendered)
    rendered = html.escape(rendered)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"__(.+?)__", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"~~(.+?)~~", r"<del>\1</del>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)

    for index, fragment in enumerate(tokens):
        rendered = rendered.replace(f"\x00DICO{index}\x00", fragment)

    return rendered


def _standalone_media(line):
    candidate = line.strip()
    if candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1].strip()

    kind, source = _get_media_info(candidate)
    if kind not in {"youtube", "instagram", "video"}:
        return None
    return kind, source


def _render_media_block(kind, source):
    safe_source = html.escape(source, quote=True)
    if kind == "instagram":
        return (
            '<div class="dico-media dico-instagram-media">'
            f'<iframe class="dico-media-frame dico-media-instagram-frame" src="{safe_source}" '
            'title="Instagram 게시물" loading="lazy" '
            'referrerpolicy="strict-origin-when-cross-origin" '
            'allow="autoplay; clipboard-write; encrypted-media; picture-in-picture; web-share" '
            'scrolling="no" '
            'allowfullscreen></iframe>'
            "</div>"
        )
    if kind == "youtube":
        return (
            '<div class="dico-media">'
            f'<iframe class="dico-media-frame" src="{safe_source}" '
            'title="YouTube 동영상" loading="lazy" '
            'referrerpolicy="strict-origin-when-cross-origin" '
            'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
            'gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>'
            "</div>"
        )

    return (
        '<div class="dico-media">'
        f'<video class="dico-media-frame" src="{safe_source}" '
        'controls preload="metadata" playsinline></video>'
        "</div>"
    )


def _single_line_code_fence(line):
    """Discord처럼 ```내용``` 한 줄 표기도 코드 블록으로 인식합니다."""
    match = re.fullmatch(r"\s*(`{3,}|~{3,})(.*?)\1\s*", line)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _is_markdown_block_start(line):
    stripped = line.strip()
    if not stripped:
        return True
    if re.match(r"^```", stripped):
        return True
    if re.match(r"^#{1,6}\s+", stripped):
        return True
    if re.match(r"^(?:[-*_]\s*){3,}$", stripped):
        return True
    if re.match(r"^>\s?", stripped):
        return True
    if re.match(r"^\s*[-+*]\s+", line) or re.match(r"^\s*\d+[.)]\s+", line):
        return True
    return _standalone_media(line) is not None


def _render_markdown_fallback(value):
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        single_line_fence = _single_line_code_fence(line)
        if single_line_fence:
            _, code = single_line_fence
            output.append(f"<pre><code>{html.escape(code)}</code></pre>")
            index += 1
            continue

        fence = re.match(r"^```([A-Za-z0-9_+-]{0,32})\s*$", stripped)
        if fence:
            language = fence.group(1)
            code_lines = []
            index += 1
            while index < len(lines) and not re.match(r"^```\s*$", lines[index].strip()):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            language_class = f' class="language-{language}"' if language else ""
            output.append(
                f"<pre><code{language_class}>{html.escape(chr(10).join(code_lines))}</code></pre>"
            )
            continue

        media = _standalone_media(line)
        if media:
            output.append(_render_media_block(*media))
            index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = len(heading.group(1))
            output.append(f"<h{level}>{_render_inline_markdown(heading.group(2))}</h{level}>")
            index += 1
            continue

        if re.match(r"^(?:[-*_]\s*){3,}$", stripped):
            output.append("<hr>")
            index += 1
            continue

        if re.match(r"^>\s?", stripped):
            quoted = []
            while index < len(lines):
                quote_match = re.match(r"^\s*>\s?(.*)$", lines[index])
                if not quote_match:
                    break
                quoted.append(quote_match.group(1))
                index += 1
            output.append(f"<blockquote>{_render_markdown_fallback(chr(10).join(quoted))}</blockquote>")
            continue

        unordered = re.match(r"^\s*[-+*]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if unordered or ordered:
            tag = "ul" if unordered else "ol"
            pattern = r"^\s*[-+*]\s+(.+)$" if unordered else r"^\s*\d+[.)]\s+(.+)$"
            items = []
            while index < len(lines):
                item = re.match(pattern, lines[index])
                if not item:
                    break
                items.append(f"<li>{_render_inline_markdown(item.group(1))}</li>")
                index += 1
            output.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        paragraph = [line]
        index += 1
        while index < len(lines) and not _is_markdown_block_start(lines[index]):
            paragraph.append(lines[index])
            index += 1
        output.append(
            "<p>" + "<br>".join(_render_inline_markdown(item) for item in paragraph) + "</p>"
        )

    return "\n".join(output)


def _prepare_markdown_media(value):
    output = []
    fence_marker = None

    for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        single_line_fence = None if fence_marker else _single_line_code_fence(line)
        if single_line_fence:
            marker, code = single_line_fence
            output.extend((marker, code, marker))
            continue

        fence = re.match(r"^\s*(```|~~~)", line)
        if fence:
            marker = fence.group(1)
            fence_marker = None if fence_marker == marker else marker
            output.append(line)
            continue

        media = None if fence_marker else _standalone_media(line)
        if media:
            output.extend(("", _render_media_block(*media), ""))
        else:
            output.append(line)

    return "\n".join(output)


def _external_link_attributes(attributes, new=False):
    attributes[(None, "target")] = "_blank"
    attributes[(None, "rel")] = "noopener noreferrer"
    return attributes


def _normalize_code_entities(value):
    """Sanitizer가 코드 안의 &quot;를 다시 문자 그대로 보이게 만드는 문제를 정리합니다."""
    code_pattern = re.compile(r"(<code\b[^>]*>)(.*?)(</code>)", re.IGNORECASE | re.DOTALL)

    def normalize(match):
        code_text = html.unescape(html.unescape(match.group(2)))
        return f"{match.group(1)}{html.escape(code_text, quote=False)}{match.group(3)}"

    return code_pattern.sub(normalize, value)


def _render_markdown(value):
    if markdown_module is None or bleach is None:
        return _render_markdown_fallback(value)

    rendered = markdown_module.markdown(
        _prepare_markdown_media(value),
        extensions=("extra", "sane_lists", "nl2br"),
        output_format="html5",
    )

    allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS).union({
        "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
        "pre", "code", "blockquote", "ul", "ol", "li", "del",
        "img", "table", "thead", "tbody", "tr", "th", "td",
        "div", "iframe", "video",
    })
    allowed_attributes = {
        "a": ("href", "title", "target", "rel"),
        "img": ("src", "alt", "title", "loading"),
        "code": ("class",),
        "div": ("class",),
        "iframe": (
            "class", "src", "title", "loading", "scrolling", "referrerpolicy",
            "allow", "allowfullscreen",
        ),
        "video": ("class", "src", "controls", "preload", "playsinline"),
        "th": ("align",),
        "td": ("align",),
    }

    cleaned = bleach.clean(
        rendered,
        tags=allowed_tags,
        attributes=allowed_attributes,
        protocols=("http", "https"),
        strip=True,
    )
    cleaned = _normalize_code_entities(cleaned)
    return bleach.linkify(
        cleaned,
        callbacks=(_external_link_attributes,),
        skip_tags=("pre", "code", "iframe", "video"),
    )


@home_bp.get("/")
async def index():
    await ensure_database()
    search = request.args.get("q", "").strip()[:100]
    category = request.args.get("category", "").strip()

    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    (posts, total, page, total_pages), notices = await asyncio.gather(
        asyncio.to_thread(_get_posts_sync, search, category, page, 20),
        asyncio.to_thread(get_notices_sync, 4),
    )

    return await render_template(
        "home/index.html",
        posts=posts,
        notices=notices if page == 1 and not search and not category else [],
        search=search,
        category=category,
        categories=POST_CATEGORIES,
        total=total,
        page=page,
        total_pages=total_pages,
    )


@home_bp.get("/users/<string:user_uid>")
@home_bp.get("/profile/<string:user_uid>")
async def user_profile(user_uid):
    await ensure_database()
    profile = await asyncio.to_thread(_get_profile_sync, user_uid.strip())
    if not profile:
        abort(404)

    return await render_template(
        "profile/profile.html",
        profile=profile,
        is_own_profile=(str(session.get("user_id")) == str(profile["id"])),
    )


@home_bp.get("/profile")
@login_required
async def my_profile():
    user_uid = session.get("user_uid")
    if not user_uid:
        abort(404)
    return redirect(url_for("home.user_profile", user_uid=user_uid))


@home_bp.route("/settings/profile", methods=["GET", "POST"])
@login_required
async def profile_settings():
    await ensure_database()
    user_id = session["user_id"]
    settings = await asyncio.to_thread(_get_profile_settings_sync, user_id)
    if not settings:
        abort(404)
    if request.method == "GET":
        return await render_template(
            "settings/profile.html", profile_settings=settings, saved=request.args.get("saved") == "1"
        )

    form = await request.form
    if not validate_csrf_token(form):
        abort(400)
    nickname = form.get("nickname", "").strip()
    headline = form.get("headline", "").strip()
    bio = form.get("bio", "").strip()
    avatar_action = form.get("avatar_action", "keep")
    avatar_data = form.get("avatar_data", "")
    error = None
    if not NICKNAME_PATTERN.fullmatch(nickname):
        error = "닉네임은 한글, 영문, 숫자, 밑줄로 2~20자까지 사용할 수 있습니다."
    elif len(headline) > 80 or len(bio) > 500:
        error = "한 줄 소개는 80자, 자기소개는 500자 이하로 입력해 주세요."
    elif avatar_action not in {"keep", "replace", "remove"}:
        error = "이미지 변경 요청이 올바르지 않습니다."
    elif avatar_action == "replace":
        try:
            avatar_data = _validate_avatar_data(avatar_data)
        except ValueError as exc:
            error = str(exc)

    if not error:
        try:
            saved = await asyncio.to_thread(
                _save_profile_settings_sync, user_id, nickname, headline, bio, avatar_action, avatar_data
            )
        except DuplicateKeyError:
            error = "이미 사용 중인 닉네임입니다."
        else:
            if not saved:
                abort(404)

    is_async = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if error:
        if is_async:
            return jsonify({"ok": False, "error": error}), 400
        settings.update({"nickname": nickname, "headline": headline, "bio": bio})
        return await render_template("settings/profile.html", profile_settings=settings, error=error), 400

    session["nickname"] = nickname
    if is_async:
        return jsonify({"ok": True, "nickname": nickname, "avatar_changed": avatar_action != "keep"})
    return redirect(url_for("home.profile_settings", saved="1"))


@home_bp.route("/write", methods=["GET", "POST"])
@login_required
async def write():
    error = None
    values = {
        "title": "",
        "content": "",
        "category": "general",
    }

    if request.method == "POST":
        form = await request.form
        wants_json = "application/json" in request.headers.get("Accept", "")

        if not validate_csrf_token(form):
            abort(400)

        values = {
            "title": form.get("title", "").strip(),
            "content": form.get("content", "").strip(),
            "category": form.get("category", "general"),
        }

        if not values["title"] or len(values["title"]) > 100:
            error = "제목은 1자 이상 100자 이하로 입력해 주세요."
        elif not values["content"] or len(values["content"]) > 10000:
            error = "내용은 1자 이상 10,000자 이하로 입력해 주세요."
        elif values["category"] not in POST_CATEGORIES:
            error = "올바른 게시판 분류를 선택해 주세요."
        else:
            await ensure_database()
            post_id = await asyncio.to_thread(
                _create_post_sync,
                values["title"],
                values["content"],
                values["category"],
                "",
                session["user_id"],
                session["nickname"],
            )
            redirect_url = url_for("home.post_detail", post_id=post_id)
            if wants_json:
                return jsonify({"success": True, "redirect": redirect_url})
            return redirect(redirect_url)

        if error and wants_json:
            return jsonify({"success": False, "message": error}), 400

    return await render_template(
        "home/write.html",
        error=error,
        values=values,
        categories=POST_CATEGORIES,
    )


@home_bp.post("/api/markdown/preview")
@login_required
async def preview_markdown():
    form = await request.form

    if not validate_csrf_token(form):
        abort(400)

    content = form.get("content", "")
    if len(content) > 10000:
        return jsonify({
            "success": False,
            "message": "내용은 10,000자 이하로 입력해 주세요.",
        }), 400

    return jsonify({
        "success": True,
        "html": _render_markdown(content),
    })


@home_bp.get("/posts/<int:post_id>")
async def post_detail(post_id):
    await ensure_database()
    post, comments, liked = await asyncio.to_thread(
        _get_post_sync,
        post_id,
        session.get("user_id"),
    )

    if not post:
        abort(404)

    post["media_kind"], post["media_src"] = _get_media_info(post.get("media_url"))
    post["content_html"] = _render_markdown(post["content"])
    return await render_template(
        "home/detail.html",
        post=post,
        comments=comments,
        liked=liked,
        category_name=POST_CATEGORIES.get(post["category"], "자유"),
    )


@home_bp.post("/posts/<int:post_id>/comments")
@login_required
async def create_comment(post_id):
    form = await request.form

    if not validate_csrf_token(form):
        abort(400)

    content = form.get("content", "").strip()
    if not content or len(content) > 1000:
        return jsonify({
            "success": False,
            "message": "댓글은 1자 이상 1,000자 이하로 입력해 주세요.",
        }), 400

    comment = await asyncio.to_thread(
        _create_comment_sync,
        post_id,
        session["user_id"],
        session["nickname"],
        content,
    )

    if not comment:
        abort(404)

    target = await asyncio.to_thread(_get_post_notification_target_sync, post_id)
    if target and str(target["author_id"]) != str(session["user_id"]):
        try:
            await create_notification(
                recipient_user_id=target["author_id"],
                sender=session["nickname"],
                message=f"{session['nickname']}님이 회원님의 게시글에 댓글을 남겼습니다.",
                category="activity",
                url=url_for("home.post_detail", post_id=post_id),
            )
        except Exception:
            current_app.logger.exception("댓글 알림 저장에 실패했습니다.")

    return jsonify({"success": True, "comment": comment})


@home_bp.post("/posts/<int:post_id>/like")
@login_required
async def toggle_like(post_id):
    csrf_token = request.headers.get("X-CSRF-Token", "")

    if not validate_csrf_token({"csrf_token": csrf_token}):
        abort(400)

    result = await asyncio.to_thread(_toggle_like_sync, post_id, session["user_id"])
    if not result:
        abort(404)

    liked, like_count = result
    if liked:
        target = await asyncio.to_thread(_get_post_notification_target_sync, post_id)
        if target and str(target["author_id"]) != str(session["user_id"]):
            try:
                await create_notification(
                    recipient_user_id=target["author_id"],
                    sender=session["nickname"],
                    message=f"{session['nickname']}님이 회원님의 게시글을 좋아합니다.",
                    category="activity",
                    url=url_for("home.post_detail", post_id=post_id),
                )
            except Exception:
                current_app.logger.exception("게시글 좋아요 알림 저장에 실패했습니다.")

    return jsonify({
        "success": True,
        "liked": liked,
        "like_count": like_count,
    })
