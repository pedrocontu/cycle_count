import logging

from flask import Flask
from flask_wtf.csrf import CSRFProtect

from . import bom, config, db, email_utils, usage

csrf = CSRFProtect()
log = logging.getLogger(__name__)


def create_app(overrides=None):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app = Flask(__name__)
    app.config.update(config.from_env())
    if overrides:
        app.config.update(overrides)

    if not app.config.get('SECRET_KEY'):
        raise RuntimeError(
            'CC_SECRET_KEY is not set. Generate one with '
            '`python -c "import secrets; print(secrets.token_hex(32))"` '
            'and put it in the environment / .env file.')

    db.configure(app.config.get('CC_SQL_CONN'))
    bom.configure(app.config.get('CC_AZURE_CONN'))
    usage.configure(app.config['CC_MYSQL'], app.config['CC_REF_CODES_DIR'])
    email_utils.configure(
        host=app.config['CC_SMTP_HOST'], port=app.config['CC_SMTP_PORT'],
        use_tls=app.config['CC_SMTP_TLS'], from_email=app.config['CC_MAIL_FROM'],
        domain=app.config['CC_MAIL_DOMAIN'],
    )

    csrf.init_app(app)

    from . import filters, views, cli
    filters.register(app)
    app.register_blueprint(views.bp)
    views.register_error_handlers(app)
    cli.register(app)
    return app
