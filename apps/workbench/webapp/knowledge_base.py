from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


KNOWLEDGE_BASE_SYNC_ACTION = "knowledge_base_sync"
DEFAULT_CONFIG_PATH = Path("~/.config/auto-daytexy/knowledge_base.json").expanduser()
DEFAULT_KEYCHAIN_SERVICE = "auto-daytexy-knowledge-base"
DEFAULT_TIMEOUT_SECONDS = 15.0
PAGE_BATCH_SIZE = 100
PAGE_METADATA_CQL = "type = page ORDER BY created DESC"
PAGE_METADATA_EXPAND = "content.space,content.version,content.history"
SYNC_MODES = frozenset({"incremental", "full"})
MAX_SYNC_DURATION_SECONDS = 240.0
BUSINESS_TIMEZONE = timezone(timedelta(hours=8))


class KnowledgeBaseSyncError(RuntimeError):
    """Raised when the read-only knowledge-base index cannot be refreshed."""


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _url_origin(req.full_url) != _url_origin(newurl):
            raise KnowledgeBaseSyncError("知识库返回了跨站跳转，已停止请求以保护本机凭据。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _url_origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def parse_metadata_timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUSINESS_TIMEZONE)
    return parsed.astimezone(BUSINESS_TIMEZONE)


class ConfluenceMetadataClient:
    def __init__(self, *, base_url: str, username: str, password: str) -> None:
        normalized_base_url = str(base_url or "").strip().rstrip("/") + "/"
        parsed_url = urllib.parse.urlsplit(normalized_base_url)
        if parsed_url.scheme != "https":
            raise KnowledgeBaseSyncError("知识库地址必须使用 HTTPS。")
        if (
            not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise KnowledgeBaseSyncError("知识库地址配置无效。")
        if not str(username or "").strip() or password is None:
            raise KnowledgeBaseSyncError("知识库账号配置不完整。")
        self.base_url = normalized_base_url
        self.timeout = DEFAULT_TIMEOUT_SECONDS
        token = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
            "User-Agent": "auto-daytexy-knowledge-index/1.0",
        }
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _SameOriginRedirectHandler()
        )

    @classmethod
    def from_local_config(
        cls,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
    ) -> "ConfluenceMetadataClient":
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KnowledgeBaseSyncError(
                "未找到知识库本机配置，请先配置知识库地址、账号和 macOS 钥匙串。"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeBaseSyncError("知识库本机配置无法读取。") from exc
        if not isinstance(payload, dict):
            raise KnowledgeBaseSyncError("知识库本机配置格式无效。")

        username = str(payload.get("username") or "").strip()
        service = str(payload.get("keychain_service") or DEFAULT_KEYCHAIN_SERVICE)
        if not username:
            raise KnowledgeBaseSyncError("知识库本机配置缺少账号。")
        password = _read_keychain_password(service, username)
        return cls(
            base_url=str(payload.get("base_url") or "").strip(),
            username=username,
            password=password,
        )

    def whoami(self) -> dict:
        payload = self._get_json("rest/api/user/current")
        if payload.get("type") == "anonymous":
            raise KnowledgeBaseSyncError("知识库认证已失效，请重新完成本机认证。")
        user_key = str(payload.get("userKey") or "").strip()
        if not user_key:
            raise KnowledgeBaseSyncError("知识库没有返回当前账号身份。")
        return {
            "user_key": user_key,
            "display_name": str(payload.get("displayName") or "").strip(),
        }

    def search_page_metadata(self, *, start: int, limit: int) -> dict:
        if start < 0 or limit <= 0 or limit > PAGE_BATCH_SIZE:
            raise ValueError("知识库分页参数无效。")
        return self._get_json(
            "rest/api/search",
            {
                "cql": PAGE_METADATA_CQL,
                "limit": limit,
                "start": start,
                "expand": PAGE_METADATA_EXPAND,
            },
        )

    def absolute_page_url(self, *, page_id: str, webui_path: object) -> str:
        normalized_page_id = str(page_id or "").strip()
        if not normalized_page_id.isdigit():
            raise KnowledgeBaseSyncError("知识库页面编号无效。")
        candidate = str(webui_path or "").strip()
        if (
            candidate.startswith("/") and not candidate.startswith("//")
            and "\\" not in candidate
            and not any(ord(char) < 32 or ord(char) == 127 for char in candidate)
        ):
            resolved = urllib.parse.urljoin(self.base_url, candidate)
            if _url_origin(resolved) == _url_origin(self.base_url):
                return resolved
        return urllib.parse.urljoin(
            self.base_url,
            f"pages/viewpage.action?pageId={urllib.parse.quote(normalized_page_id)}",
        )

    def _get_json(self, path: str, params: dict[str, object] | None = None) -> dict:
        url = urllib.parse.urljoin(self.base_url, path.lstrip("/"))
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=self.headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise KnowledgeBaseSyncError(
                    "知识库拒绝了只读请求，请检查本机认证或访问权限。"
                ) from exc
            raise KnowledgeBaseSyncError(
                f"知识库只读请求失败（HTTP {exc.code}）。"
            ) from exc
        except urllib.error.URLError as exc:
            raise KnowledgeBaseSyncError("无法连接知识库服务。") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KnowledgeBaseSyncError("知识库返回了无法解析的数据。") from exc
        if not isinstance(payload, dict):
            raise KnowledgeBaseSyncError("知识库返回的数据格式无效。")
        return payload


def build_knowledge_base_sync_descriptor(*, mode: str = "incremental") -> dict:
    normalized_mode = _normalize_sync_mode(mode)
    payload = {
        "action": KNOWLEDGE_BASE_SYNC_ACTION,
        "scope": "all_visible_spaces_created_by_current_user",
        "metadata_only": True,
        "mode": normalized_mode,
    }
    return {
        **payload,
        "unique_key": _digest_json(payload),
        "authorization_digest": _digest_json({"version": 1, **payload}),
    }


def sync_knowledge_base(
    store,
    *,
    mode: str = "incremental",
    progress_callback: Callable[[str], None] | None = None,
    client: ConfluenceMetadataClient | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict:
    normalized_mode = _normalize_sync_mode(mode)
    now = now or datetime.now
    monotonic = monotonic or time.monotonic
    scanned_count = 0
    matched_count = 0
    full_import = True
    pruned_count = 0
    backup = None
    try:
        client = client or ConfluenceMetadataClient.from_local_config()
        _progress(progress_callback, "[认证预检] 正在验证知识库本机凭据与当前账号。")
        identity = client.whoami()
        previous_state = store.get_knowledge_base_sync_state()
        previous_identity = str(previous_state.get("identity_user_key") or "").strip()
        if (
            previous_identity
            and previous_identity != identity["user_key"]
            and store.count_knowledge_base_pages() > 0
        ):
            raise KnowledgeBaseSyncError(
                "当前知识库账号与本机索引账号不一致，已停止同步以防混入记录。"
            )
        watermark = parse_metadata_timestamp(store.get_latest_knowledge_base_created_at())
        known_page_ids = {
            str(page["page_id"]) for page in store.list_knowledge_base_pages()
        }
        full_import = (
            normalized_mode == "full"
            or previous_state.get("status") != "success"
            or not watermark
        )
        if full_import:
            _progress(progress_callback, "[备份] 正在创建全量复核前的本机索引快照。")
            backup = store.create_knowledge_base_backup(created_at=_now_iso(now))
            _progress(
                progress_callback,
                f"[备份] 快照已创建：{backup['size_bytes']} 字节。",
            )
        store.start_knowledge_base_sync(
            identity_user_key=identity["user_key"],
            identity_display_name=identity["display_name"],
            started_at=_now_iso(now),
        )
        _progress(
            progress_callback,
            "知识库首次全量元数据导入已开始。"
            if full_import
            else "正在读取新建知识库页面元数据。",
        )

        start = 0
        total_size = None
        sync_started_at = monotonic()
        visible_page_ids: set[str] = set()
        pending_records: list[dict] = []
        previous_created_at = None
        while True:
            _ensure_sync_time_remaining(monotonic, sync_started_at)
            payload = client.search_page_metadata(start=start, limit=PAGE_BATCH_SIZE)
            _ensure_sync_time_remaining(monotonic, sync_started_at)
            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                raise KnowledgeBaseSyncError("知识库搜索结果格式无效。")
            total_value = payload.get("totalSize")
            if total_value is not None:
                if type(total_value) is not int or total_value < 0:
                    raise KnowledgeBaseSyncError("知识库分页总数无效。")
                if total_size is not None and total_size != total_value:
                    raise KnowledgeBaseSyncError("知识库分页期间记录总数发生变化，请重新同步。")
                total_size = total_value
            if not raw_results and total_size is not None and start < total_size:
                raise KnowledgeBaseSyncError("知识库分页未完整返回，已保留原索引。")

            records = []
            reached_watermark = False
            for raw_result in raw_results:
                scanned_count += 1
                content = raw_result.get("content") if isinstance(raw_result, dict) else None
                page_id = str((content or {}).get("id") or "").strip() if isinstance(content, dict) else ""
                created_at = parse_metadata_timestamp(_created_at(raw_result))
                if not page_id.isdigit() or not isinstance(content, dict) or content.get("type") != "page" or created_at is None:
                    raise KnowledgeBaseSyncError("知识库页面元数据不完整，已保留原索引。")
                # Ownership must be known even for pages older than the watermark.
                _page_creator(content)
                if previous_created_at is not None and created_at > previous_created_at:
                    raise KnowledgeBaseSyncError("知识库分页未按创建时间排序，请重新同步。")
                previous_created_at = created_at
                if page_id in visible_page_ids:
                    raise KnowledgeBaseSyncError("知识库分页包含重复页面，请重新同步。")
                visible_page_ids.add(page_id)
                if not full_import and watermark and created_at < watermark:
                    reached_watermark = True
                    continue
                page = _build_page_record(
                    raw_result,
                    client=client,
                    identity=identity,
                    indexed_at=_now_iso(now),
                )
                if page is not None and (full_import or page_id not in known_page_ids):
                    records.append(page)

            if records:
                pending_records.extend(records)
                matched_count += len(records)
            store.update_knowledge_base_sync_progress(
                scanned_count=scanned_count,
                matched_count=matched_count,
            )
            _progress(
                progress_callback,
                _progress_message(
                    scanned_count=scanned_count,
                    matched_count=matched_count,
                    total_size=total_size,
                    full_import=full_import,
                ),
            )

            if reached_watermark or not raw_results:
                break
            start += len(raw_results)
            if total_size is not None and start > total_size:
                raise KnowledgeBaseSyncError("知识库分页数量与总数不一致。")
            if total_size is not None and start >= total_size:
                break

        pruned_count = store.apply_knowledge_base_snapshot(
            pending_records,
            creator_user_key=identity["user_key"] if full_import else None,
            visible_page_ids=visible_page_ids if full_import else None,
        )

        total_indexed = store.count_knowledge_base_pages()
        completed_at = _now_iso(now)
        store.finish_knowledge_base_sync(
            status="success",
            completed_at=completed_at,
            scanned_count=scanned_count,
            matched_count=matched_count,
        )
        result = {
            "status": "completed",
            "mode": "full" if full_import else "incremental",
            "scanned_count": scanned_count,
            "matched_count": matched_count,
            "pruned_count": pruned_count,
            "total_indexed_count": total_indexed,
            "identity_display_name": identity["display_name"],
            "completed_at": completed_at,
        }
        if backup is not None:
            result["backup_created_at"] = backup["created_at"]
            result["backup_size_bytes"] = backup["size_bytes"]
        _progress(
            progress_callback,
            "知识库同步完成："
            f"本次匹配 {matched_count} 条，清理 {pruned_count} 条过期索引，"
            f"当前已索引 {total_indexed} 条。",
        )
        return result
    except Exception as exc:
        message = _safe_error_message(exc)
        store.fail_knowledge_base_sync(
            completed_at=_now_iso(now),
            scanned_count=scanned_count,
            matched_count=matched_count,
            error_summary=message,
        )
        raise


def _page_creator(content: dict) -> dict | None:
    history = content.get("history")
    created_by = history.get("createdBy") if isinstance(history, dict) else None
    if not isinstance(created_by, dict):
        raise KnowledgeBaseSyncError("知识库页面创建者信息不完整，已保留原索引。")
    user_key = created_by.get("userKey")
    if created_by.get("type") == "anonymous":
        if user_key is None or (isinstance(user_key, str) and not user_key.strip()):
            return None
        raise KnowledgeBaseSyncError("知识库页面创建者身份信息矛盾，已保留原索引。")
    if not isinstance(user_key, str) or not user_key.strip():
        raise KnowledgeBaseSyncError("无法确定知识库页面创建者，已保留原索引。")
    return created_by


def _build_page_record(raw_result, *, client, identity: dict, indexed_at: str) -> dict | None:
    if not isinstance(raw_result, dict):
        return None
    content = raw_result.get("content")
    if not isinstance(content, dict) or content.get("type") != "page":
        return None
    history = content.get("history")
    history = history if isinstance(history, dict) else {}
    created_by = _page_creator(content)
    if created_by is None or created_by["userKey"].strip() != identity["user_key"]:
        return None
    page_id = str(content.get("id") or "").strip()
    title = str(content.get("title") or "").strip()
    created_at = parse_metadata_timestamp(history.get("createdDate"))
    if not page_id or not title or created_at is None:
        raise KnowledgeBaseSyncError("本人知识库页面元数据不完整，已保留原索引。")
    space = content.get("space")
    space = space if isinstance(space, dict) else {}
    version = content.get("version")
    version = version if isinstance(version, dict) else {}
    links = content.get("_links")
    links = links if isinstance(links, dict) else {}
    return {
        "page_id": page_id,
        "title": title,
        "space_key": str(space.get("key") or "").strip(),
        "space_name": str(space.get("name") or "").strip(),
        "creator_user_key": identity["user_key"],
        "creator_display_name": str(created_by.get("displayName") or "").strip(),
        "created_at": created_at.isoformat(),
        "modified_at": str(
            raw_result.get("lastModified") or version.get("when") or ""
        ).strip(),
        "page_url": client.absolute_page_url(
            page_id=page_id,
            webui_path=links.get("webui"),
        ),
        "indexed_at": indexed_at,
    }


def _created_at(raw_result) -> str:
    if not isinstance(raw_result, dict):
        return ""
    content = raw_result.get("content")
    if not isinstance(content, dict):
        return ""
    history = content.get("history")
    if not isinstance(history, dict):
        return ""
    return str(history.get("createdDate") or "").strip()


def _read_keychain_password(service: str, username: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", username, "-w"],
            check=False, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise KnowledgeBaseSyncError("读取知识库凭据需要 macOS 钥匙串。") from exc
    except subprocess.TimeoutExpired as exc:
        raise KnowledgeBaseSyncError("读取 macOS Keychain 凭据超时，请检查本机解锁状态。") from exc
    password = result.stdout.removesuffix("\n")
    if result.returncode != 0 or not password:
        raise KnowledgeBaseSyncError("无法从 macOS Keychain 读取知识库凭据。")
    return password


def _digest_json(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now_iso(now: Callable[[], datetime]) -> str:
    return now().astimezone().isoformat(timespec="seconds")


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _progress_message(*, scanned_count: int, matched_count: int, total_size, full_import: bool) -> str:
    scan_label = (
        f"{scanned_count}/{total_size}" if total_size is not None else str(scanned_count)
    )
    prefix = "全量复核" if full_import else "增量同步"
    return f"知识库{prefix}：已读取 {scan_label} 条元数据，匹配 {matched_count} 条本人页面。"


def _normalize_sync_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized not in SYNC_MODES:
        raise ValueError("知识库同步模式无效。")
    return normalized


def _ensure_sync_time_remaining(
    monotonic: Callable[[], float],
    started_at: float,
) -> None:
    if monotonic() - started_at > MAX_SYNC_DURATION_SECONDS:
        raise KnowledgeBaseSyncError(
            "知识库同步超过 4 分钟已安全停止，原索引与备份均已保留。"
        )


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:500]
