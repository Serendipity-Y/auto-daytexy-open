"""Local customer-name mappings, with explicit approval and stale-form protection."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from integrations.fxiaoke import service_record as crm
from packages.report_domain.history import _pending_queue_lock


class AliasConflict(ValueError):
    pass


def _read_aliases(path: Path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("客户映射文件链接失效，已保留原文件。") from None
        raw = b""
    revision = hashlib.sha256(raw).hexdigest()
    if not raw:
        if path.exists():
            raise ValueError("客户映射文件为空，已停止修改并保留原文件。")
        return {}, revision
    try:
        payload = json.loads(raw, object_pairs_hook=crm.unique_customer_alias_object)
        if not isinstance(payload, dict):
            raise ValueError
        if "customer_aliases" in payload:
            if set(payload) != {"customer_aliases"}:
                raise ValueError
            payload = payload["customer_aliases"]
        if not isinstance(payload, dict):
            raise ValueError
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, (str, dict)):
                raise ValueError
            if isinstance(value, dict):
                if set(value) - {"alias", "primary_customer", "explicitly_confirmed"}:
                    raise ValueError
                if not isinstance(value.get("alias", key), str):
                    raise ValueError
                if not isinstance(value.get("primary_customer"), str):
                    raise ValueError
                if "explicitly_confirmed" in value and not isinstance(value["explicitly_confirmed"], bool):
                    raise ValueError
        mappings = crm.load_customer_alias_memory(alias_path=path, strict=True)
        if len(mappings) != len(payload):
            raise ValueError
    except (ValueError, UnicodeError, RuntimeError) as exc:
        raise ValueError("客户映射文件存在异常或重复项，已停止修改并保留原文件。") from exc
    return mappings, revision


def customer_alias_snapshot(path):
    mappings, revision = _read_aliases(Path(path))
    rows = []
    for key, item in sorted(mappings.items()):
        rows.append({
            **item,
            "key": key,
            "source": "人工明确确认" if item.get("explicitly_confirmed") else "历史本地映射",
            "known_candidates": known_customer_candidates(item["alias"], mappings),
        })
    return {"rows": rows, "revision": revision}


def known_customer_candidates(alias, mappings):
    """Suggest only names already stored locally; never claim a CRM lookup."""
    core = crm.normalize_customer_alias_key(alias)
    names = {
        item["primary_customer"]
        for item in mappings.values()
        if core and (
            core in crm.normalize_customer_alias_key(item["primary_customer"])
            or crm.normalize_customer_alias_key(item["alias"]) == core
        )
    }
    return sorted(names)


def _customer_name(value):
    name = str(value or "").strip()
    if not name or len(name) > 200 or re.search(r"[\x00-\x1f\x7f]", name):
        raise ValueError("客户名称须为 1–200 个字符，且不能包含换行或控制字符。")
    if not crm.normalize_customer_alias_key(name):
        raise ValueError("客户名称无效。")
    return name


def update_customer_alias(path, form):
    path = Path(path)
    action = form.get("action", "")
    if action not in {"add", "edit", "delete"}:
        raise ValueError("未知的客户映射操作。")
    with _pending_queue_lock(path):
        mappings, revision = _read_aliases(path)
        if form.get("revision", "") != revision:
            raise AliasConflict("客户映射已发生变化，请刷新页面后重新核对。")
        old_key = form.get("key", "")
        if action != "add" and old_key not in mappings:
            raise AliasConflict("这条客户映射已不存在，请刷新页面。")
        if action == "delete":
            if form.get("confirm_delete") != "yes":
                raise ValueError("请明确确认撤销此映射。")
            del mappings[old_key]
        else:
            alias = _customer_name(form.get("alias"))
            primary = _customer_name(form.get("primary_customer"))
            if alias == primary:
                raise ValueError("简称与正式客户名称相同，无需保存映射。")
            if form.get("confirm_target") != "yes":
                raise ValueError("请先核对正式客户名称并明确确认。")
            university_department = bool(re.search(r"大学\s*.+", primary))
            if (university_department or crm.university_subcollege_conflicts(alias, primary)) and form.get("confirm_department") != "yes":
                raise ValueError("此客户包含大学下属单位，请明确确认具体院系或部门。")
            new_key = crm.normalize_customer_alias_key(alias)
            if new_key in mappings and (action == "add" or new_key != old_key):
                raise AliasConflict("该简称已有映射，请在原记录中纠正，避免覆盖。")
            if action == "edit":
                del mappings[old_key]
            mappings[new_key] = {"alias": alias, "primary_customer": primary, "explicitly_confirmed": True}
        # A non-cooperating writer must not be overwritten after form validation.
        if _read_aliases(path)[1] != revision:
            raise AliasConflict("客户映射刚被其他操作更新，请刷新页面。")
        crm.save_customer_alias_memory(mappings, alias_path=path)
