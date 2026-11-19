from __future__ import annotations

import sys

from integrations.fxiaoke import crm_enabled

from .webapp.config import load_web_config
from .webapp.jobs import JobCoordinator
from .webapp.migration import execute_login_state_migration, plan_login_state_migration
from .webapp.security import SingleInstanceLock


def create_app(*, config):
    from .webapp.app import create_app as create_app_factory

    return create_app_factory(config=config)


def main(argv=None) -> int:
    _ = [] if argv is None else list(argv)
    config = load_web_config()
    lock_path = config.database_path.parent / "web_manager.lock"
    service_url = f"http://{config.host}:{config.port}"
    with SingleInstanceLock(lock_path, service_url):
        if crm_enabled():
            migration = execute_login_state_migration(
                plan_login_state_migration(config.project_root, config.database_path.parent)
            )
            if migration.copied_paths:
                print("已迁移纷享销客登录态：" + "、".join(str(path) for path in migration.copied_paths))
        app = create_app(config=config)
        coordinator = None
        if not config.testing:
            coordinator = JobCoordinator(
                app.extensions["web_store"],
                [sys.executable, str(config.project_root / "web_worker.py")],
            )
            coordinator.start()
        print(service_url)
        try:
            app.run(
                host=config.host,
                port=config.port,
                debug=False,
                use_reloader=False,
            )
        finally:
            if coordinator is not None:
                coordinator.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
