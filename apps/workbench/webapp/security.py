from __future__ import annotations

import fcntl
import json
import os
import secrets
from pathlib import Path
from typing import TextIO

from flask import abort, request


LOCAL_REQUEST_TOKEN_FIELD = "local_request_token"
LOCAL_REQUEST_TOKEN_HEADER = "X-Local-Request-Token"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def generate_local_token() -> str:
    return secrets.token_urlsafe(32)


def install_request_guard(app, token: str) -> None:
    web_config = app.config["WEB_CONFIG"]
    allowed_authority = f"{web_config.host}:{web_config.port}"
    allowed_origin = f"http://{allowed_authority}"
    app.config["LOCAL_REQUEST_TOKEN"] = token
    app.config["LOCAL_REQUEST_TOKEN_FIELD"] = LOCAL_REQUEST_TOKEN_FIELD
    app.config["LOCAL_REQUEST_TOKEN_HEADER"] = LOCAL_REQUEST_TOKEN_HEADER
    app.config["LOCAL_REQUEST_ALLOWED_AUTHORITY"] = allowed_authority
    app.config["LOCAL_REQUEST_ALLOWED_ORIGIN"] = allowed_origin

    @app.before_request
    def _enforce_local_request_boundary():
        if request.host != allowed_authority:
            abort(403)

        origin = request.headers.get("Origin", "")
        if origin and origin != allowed_origin:
            abort(403)

        if request.method not in STATE_CHANGING_METHODS:
            return None

        header_token = request.headers.get(LOCAL_REQUEST_TOKEN_HEADER, "")
        form_token = request.form.get(LOCAL_REQUEST_TOKEN_FIELD, "")
        expected = token.encode("utf-8")
        if secrets.compare_digest(header_token.encode("utf-8"), expected) or secrets.compare_digest(form_token.encode("utf-8"), expected):
            return None

        abort(403)

    @app.context_processor
    def _local_request_security_context():
        return {
            "local_request_token": token,
            "local_request_token_field": LOCAL_REQUEST_TOKEN_FIELD,
            "local_request_token_header": LOCAL_REQUEST_TOKEN_HEADER,
        }


class SingleInstanceLock:
    def __init__(self, path, service_url: str):
        self.path = Path(path)
        self.service_url = service_url
        self._owned_payload: dict[str, object] | None = None
        self._lock_file: TextIO | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def release(self) -> None:
        if self._lock_file is None:
            return

        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_file.close()
            self._lock_file = None
        self._owned_payload = None

    def _acquire(self) -> None:
        payload = {"pid": os.getpid(), "service_url": self.service_url}
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(f"Unable to open lock file: {self.path}") from exc

        lock_file = os.fdopen(fd, "r+", encoding="utf-8")

        try:
            if os.name == "posix":
                os.fchmod(lock_file.fileno(), 0o600)

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running_payload = self._read_payload_for_running_instance(lock_file)
                raise RuntimeError(
                    f"auto-daytexy web is already running at {running_payload['service_url']}"
                ) from None

            self._write_payload(lock_file, payload)
        except Exception:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_file.close()
            raise

        self._lock_file = lock_file
        self._owned_payload = payload

    def _read_payload_for_running_instance(self, lock_file) -> dict[str, object]:
        text = self._read_lock_text(lock_file)
        if not text.strip():
            return {"pid": None, "service_url": self.service_url}

        try:
            return self._read_payload(text)
        except RuntimeError:
            return {"pid": None, "service_url": self.service_url}

    def _read_lock_text(self, lock_file) -> str:
        lock_file.seek(0)
        return lock_file.read()

    def _write_payload(self, lock_file, payload: dict[str, object]) -> None:
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(payload, lock_file, ensure_ascii=False)
        lock_file.flush()
        os.fsync(lock_file.fileno())

    def _read_payload(self, text: str) -> dict[str, object]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Malformed lock file: {self.path}") from exc

        pid = payload.get("pid") if isinstance(payload, dict) else None
        if not isinstance(pid, (int, float, str)):
            raise RuntimeError(f"Malformed lock file: {self.path}")
        try:
            normalized_pid = int(pid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Malformed lock file: {self.path}") from exc

        if normalized_pid <= 0:
            raise RuntimeError(f"Malformed lock file: {self.path}")

        service_url = payload.get("service_url") if isinstance(payload, dict) else None
        if not isinstance(service_url, str) or not service_url:
            raise RuntimeError(f"Malformed lock file: {self.path}")

        return {"pid": normalized_pid, "service_url": service_url}
