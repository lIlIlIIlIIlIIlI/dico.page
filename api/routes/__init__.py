# from .board.main import main_bp
# from .board.api.main import api_main_bp
# from .emall.v1 import apiv1_bp
from .module.admin.admin import admin_bp
from .module.auth import auth_bp
from .home import home_bp
from .module.notifications import notification_bp
from .module.media_storage import media_bp
from .error_handlers import error_bp

all_blueprints = [
    auth_bp,
    admin_bp,
    home_bp,
    notification_bp,
    media_bp,
    error_bp,
]