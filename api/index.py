from quart import (
    Quart,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
    g,
)
from datetime import timedelta
import logging
import asyncio
import sys
import os
from routes import (
    all_blueprints
)

app = Quart(
    __name__,
    template_folder="./templates",
    static_folder="./static"
)

app.secret_key = os.getenv(
    "SECRET_KEY",
    "dev-secret-key-change-me"
)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


for bp in all_blueprints:
    app.register_blueprint(bp)
    
if __name__ == "__main__":
    from uvicorn import run

    run(app, host="0.0.0.0", port=3000)
