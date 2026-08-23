#builds the flask application: settings, database, blueprints, the login
#manager and csrf protection.
#
#this is a function rather than an app created when the file is imported, so
#that the tests can build one of their own against a temporary database.

from __future__ import annotations

import logging

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from .config import Config

csrf = CSRFProtect()


def make_app(config: type[Config] = Config) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(config.TEMPLATES_DIR),
        static_folder=str(config.STATIC_DIR),
    )
    app.config.from_object(config)

    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for warning in config.warnings():
        app.logger.warning(warning)

    #make sure the tables exist and are up to date before anything tries to
    #read from them.
    from .models import Database

    config.INSTANCE_DIR.mkdir(exist_ok=True)
    db = Database.get_db(config.DATABASE_PATH)
    from .migrations import run_migrations

    run_migrations(db, app.logger)

    #cross site request forgery protection for every form and every api call
    #that changes something.
    csrf.init_app(app)

    #market data. the background refresher keeps recently viewed prices warm,
    #so the browser can ask for a price far more often than the source is
    #actually contacted.
    from .market import get_market

    market = get_market(db)
    #flask runs this file twice in debug mode, so only the child process
    #should own the background threads.
    import os

    if not config.DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        market.refresher.start()

        #start the bot again for anyone who had it switched on before the last
        #shutdown. the settings are in the database, so they survive a restart.
        from .bot import BotManager

        BotManager.instance(db=db, app=app).restore()

    #the blueprints holding the routes
    from .api import api
    from .auth import auth
    from .views import views

    app.register_blueprint(views, url_prefix="/")
    app.register_blueprint(auth, url_prefix="/")
    app.register_blueprint(api)

    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to continue."
    login_manager.login_message_category = "error"
    login_manager.init_app(app)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.get_by_id(db, user_id)

    return app
