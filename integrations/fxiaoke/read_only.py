"""Observe the CRM's own detail request; unknown contracts leave DOM reads intact."""

import re
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError

from .tenant import tenant_value


_RECORD_ID = re.compile(r"[a-fA-F0-9]{24}")
_HTML = re.compile(r"</?[A-Za-z][^>]*>|<!|&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);")


def _origin(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (
            parts.scheme == "https" and parts.username is None and parts.password is None
            and parts.port in (None, 443)
            and (host == "fxiaoke.com" or host.endswith(".fxiaoke.com"))
        ):
            return parts.scheme, host, 443
    except (AttributeError, TypeError, ValueError):
        pass
    return None


class WebDetailObserver:
    """A per-navigation listener; it never sends requests or stores data on disk."""

    def __init__(self, page, detail_url, object_api_name, record_data_id=""):
        self.page = page
        self.value = None
        self.object_api_name = object_api_name
        self.origin = _origin(detail_url)
        self.record_data_id = ""
        self.enabled = False
        if self.origin and object_api_name and (
            object_api_name == "CasesObj" or object_api_name == tenant_value("service_record", "object", token=True)
        ):
            fragment = urlsplit(detail_url).fragment
            match = re.fullmatch(rf"objdetail/{re.escape(object_api_name)}/([a-fA-F0-9]{{24}})", fragment)
            if match and (not record_data_id or record_data_id == match[1]):
                self.record_data_id = match[1]
                self.enabled = True

    def __enter__(self):
        if self.enabled:
            try:
                self.page.on("response", self._capture)
            except (AttributeError, PlaywrightError):
                self.enabled = False
        return self

    def __exit__(self, *_):
        if self.enabled:
            try:
                self.page.remove_listener("response", self._capture)
            except (AttributeError, PlaywrightError):
                pass

    def _capture(self, response):
        try:
            parts = urlsplit(response.url)
            expected_path = f"/FHH/EM1HNCRM/API/v1/object/{self.object_api_name}/controller/WebDetail"
            if (
                _origin(response.url) != self.origin or parts.path != expected_path
                or parts.fragment or response.status != 200 or response.request.method != "POST"
            ):
                return
            request = response.request.post_data_json
            if (
                not isinstance(request, dict)
                or request.get("objectDescribeApiName") != self.object_api_name
                or request.get("objectDataId") != self.record_data_id
                or request.get("fromRecycleBin") is not False
            ):
                return
            payload = response.json()
            if not isinstance(payload, dict):
                return
            result = payload.get("Result")
            if not isinstance(result, dict) or any(
                type(result.get(key)) is not int or result[key] != 0
                for key in ("StatusCode", "FailureCode")
            ):
                return
            value = payload.get("Value")
            if not isinstance(value, dict):
                return
            data, describe = value.get("data"), value.get("objectDescribeExt")
            if (
                isinstance(data, dict) and data.get("_id") == self.record_data_id
                and _RECORD_ID.fullmatch(data["_id"])
                and data.get("object_describe_api_name") == self.object_api_name
                and data.get("is_deleted") is False
                and isinstance(describe, dict) and describe.get("api_name") == self.object_api_name
            ):
                self.value = value
        except (AttributeError, TypeError, ValueError, PlaywrightError):
            # A missing/expired/changed response is unknown, never an absent record.
            return


def _text(value):
    if isinstance(value, str) and value.strip() and value.strip() != "--" and not _HTML.search(value):
        return value.strip()
    return None


def _parts(value):
    if not isinstance(value, dict):
        return {}, {}
    data, describe = value.get("data"), value.get("objectDescribeExt")
    fields = describe.get("fields") if isinstance(describe, dict) else None
    return (data, fields) if isinstance(data, dict) and isinstance(fields, dict) else ({}, {})


def _field(fields, api_name, label, kind):
    field = fields.get(api_name)
    if (
        isinstance(field, dict) and field.get("api_name") == api_name
        and field.get("label") == label and field.get("type") == kind
        and sum(isinstance(item, dict) and item.get("label") == label for item in fields.values()) == 1
    ):
        return field
    return None


def _option(data, field):
    if not field or not isinstance(field.get("options"), list):
        return None
    api_name = field["api_name"]
    if not _text(data.get(api_name)):
        return None
    options = [item for item in field["options"] if isinstance(item, dict) and item.get("value") == data.get(api_name)]
    if len(options) != 1 or data.get(api_name + "__v"):
        return None
    label = _text(options[0].get("label"))
    if label and sum(isinstance(item, dict) and item.get("label") == label for item in field["options"]) == 1:
        return label
    return None


def _date(value, field):
    formats = {"yyyy-MM-dd": "%Y-%m-%d", "yyyy-MM-dd HH:mm": "%Y-%m-%d %H:%M", "yyyy-MM-dd HH:mm:ss": "%Y-%m-%d %H:%M:%S"}
    if not field or not isinstance(field.get("date_format"), str) or field["date_format"] not in formats:
        return None
    fmt = formats[field["date_format"]]
    if (field["type"] == "date") != (fmt == "%Y-%m-%d"):
        return None
    raw = str(value) if type(value) in (str, int) else ""
    try:
        if re.fullmatch(r"\d{10}|\d{13}", raw):
            stamp = int(raw) / (1000 if len(raw) == 13 else 1)
            parsed = datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai"))
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) and field["type"] == "date":
            return date.fromisoformat(raw).isoformat()
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", raw):
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        else:
            return None
        return parsed.strftime(fmt)
    except (ValueError, OverflowError, OSError):
        return None


def remote_workorder_values(value):
    """Return all seven existing verification fields, or keep the DOM fallback."""
    data, fields = _parts(value)
    configured = {
        name: tenant_value("remote_workorder", f"{name}_field", token=True)
        for name in ("service_date", "hours", "summary", "completion_status")
    }
    if not all(configured.values()) or len(set(configured.values())) != len(configured):
        return None
    mapping = {
        "工单编码": ("name", "auto_number"), "客户单位": ("account_id", "object_reference"),
        "服务日期": (configured["service_date"], "date"), "服务工时": (configured["hours"], "number"),
        "服务需求详述": ("description", "long_text"), "完成小结": (configured["summary"], "long_text"),
        "服务完成情况": (configured["completion_status"], "select_one"),
    }
    values = {}
    for label, (api_name, kind) in mapping.items():
        field = _field(fields, api_name, label, kind)
        if not field:
            return None
        if kind == "date":
            observed = _date(data.get(api_name), field)
        elif kind == "select_one":
            observed = _option(data, field)
        elif kind == "object_reference":
            observed = _text(data.get(api_name + "__r")) if _text(data.get(api_name)) else None
        elif kind == "number":
            raw = data.get(api_name)
            observed = _text(str(raw)) if type(raw) in (str, int, float) else None
        else:
            observed = _text(data.get(api_name))
        if observed is None:
            return None
        values[label] = observed
    return values


def service_record_values(value, record_id):
    """Require a matching record and a linked employee before using all five fields."""
    data, fields = _parts(value)
    start, customer, method = (
        tenant_value("service_record", key, token=True) for key in ("start_time_field", "customer_field", "method_field")
    )
    if not all((start, customer, method)) or len({start, customer, method, "name", "owner"}) != 5:
        return None
    required = (
        ("name", "服务记录编号", "auto_number"), ("owner", "负责人", "employee"),
        (start, "服务开始时间", "date_time"),
        (customer, "客户单位", "object_reference"), (method, "服务方式", "select_one"),
    )
    if any(not _field(fields, *item) for item in required) or _text(data.get("name")) != record_id:
        return None
    owners, employee = data.get("owner"), data.get("owner__r")
    if not isinstance(owners, list) or len(owners) != 1 or not isinstance(employee, dict) or employee.get("id") != owners[0] or not _text(owners[0]):
        return None
    values = {
        "record_id": record_id, "owner_name": _text(employee.get("name")),
        "service_start_time": _date(data.get(start), fields[start]),
        "customer_name": _text(data.get(customer + "__r")) if _text(data.get(customer)) else None,
        "service_method": _option(data, fields[method]),
    }
    return values if all(values.values()) else None
