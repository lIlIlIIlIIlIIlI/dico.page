from quart import Blueprint, jsonify, render_template, request


error_bp = Blueprint("errors", __name__)


def _wants_json_response():
    """API 경로나 JSON을 명시적으로 더 선호한 요청만 JSON으로 처리합니다."""
    if request.path.startswith("/api/"):
        return True

    accepts = request.accept_mimetypes
    return accepts["application/json"] > accepts["text/html"]


@error_bp.app_errorhandler(404)
async def page_not_found(error):
    """브라우저에는 공통 404 화면을, API에는 JSON 오류를 반환합니다."""
    if _wants_json_response():
        return jsonify({
            "success": False,
            "error": "not_found",
            "message": "요청한 페이지를 찾을 수 없습니다.",
        }), 404

    return await render_template(
        "errors/404.html",
        requested_path=request.path,
    ), 404


@error_bp.app_errorhandler(403)
async def access_forbidden(error):
    """권한이 없는 브라우저에는 403 화면을, API에는 JSON 오류를 반환합니다."""
    if _wants_json_response():
        return jsonify({
            "success": False,
            "error": "forbidden",
            "message": "이 페이지에 접근할 권한이 없습니다.",
        }), 403

    return await render_template(
        "errors/403.html",
        requested_path=request.path,
    ), 403
