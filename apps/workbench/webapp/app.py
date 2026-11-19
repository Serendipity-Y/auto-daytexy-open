from __future__ import annotations

from flask import Flask, jsonify

from .config import load_web_config
from .indexer import HistoryIndexer
from .routes import create_web_blueprint
from .security import generate_local_token, install_request_guard
from .store import WebStore


def create_app(config=None) -> Flask:
    app = Flask(__name__)
    web_config = config or load_web_config()
    app.config["WEB_CONFIG"] = web_config
    app.config["TESTING"] = bool(web_config.testing)
    app.config["SERVER_NAME"] = f"{web_config.host}:{web_config.port}"
    app.config["LOCAL_REQUEST_TOKEN"] = generate_local_token()

    store = WebStore(web_config.database_path)
    store.initialize()
    app.extensions["web_store"] = store

    indexer = HistoryIndexer(store, web_config.report_path)
    app.extensions["history_indexer"] = indexer
    if _should_run_startup_index_sync(web_config):
        app.extensions["history_index_summary"] = indexer.sync()

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "auto-daytexy-web"})

    app.register_blueprint(
        create_web_blueprint(
            store,
            artifact_root=indexer.artifact_root,
            indexer=indexer if not web_config.testing else None,
        )
    )
    install_request_guard(app, app.config["LOCAL_REQUEST_TOKEN"])

    return app


def _should_run_startup_index_sync(web_config) -> bool:
    explicit_testing_sync = bool(getattr(web_config, "run_startup_index_sync", False))
    if bool(web_config.testing):
        return explicit_testing_sync
    return True
