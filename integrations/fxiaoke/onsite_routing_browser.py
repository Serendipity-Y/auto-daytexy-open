"""Read-only CasesObj lookup for the narrowly scoped onsite routing policy."""

import re
import time
from urllib.parse import urlsplit

import fxiaoke_remote_workorder as remote
import fxiaoke_service_record as fx

from .onsite_routing import (
    AFTER_SALES_TYPE,
    RoutingError,
    routing_values,
    select_after_sales_workorder,
)
from .read_only import WebDetailObserver, _origin


DETAIL_TIMEOUT_SECONDS = 8
MAX_CANDIDATES = 100
AFTER_SALES_LABEL = "《售后服务工单》"
_TYPE_LABELS = {
    "全部业务类型", AFTER_SALES_LABEL, "《项目实施工单》", "《远程服务工单》", "《PoC/演示工单》",
}
_DATA_ID = re.compile(r"[a-fA-F0-9]{24}")


def _select_business_type(page, label):
    fields = page.locator("input.j-select-input.crm-ui-title:visible")
    matches = [fields.nth(index) for index in range(fields.count())
               if fields.nth(index).get_attribute("data-title") in _TYPE_LABELS]
    if len(matches) != 1:
        raise RoutingError("工单业务类型筛选不唯一或尚未加载，已停止自动选单。")
    fx.choose_technical_service_dropdown_value(page, matches[0], label, "业务类型")
    if matches[0].get_attribute("data-title") != label:
        raise RoutingError("工单业务类型筛选未生效，已停止自动选单。")


def _assert_search_state(page, query, label):
    fields = page.locator("input.j-select-input.crm-ui-title:visible")
    current = [fields.nth(index).get_attribute("data-title") for index in range(fields.count())
               if fields.nth(index).get_attribute("data-title") in _TYPE_LABELS]
    inputs = page.locator(fx.TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR + ":visible")
    if current != [label] or inputs.count() != 1 or inputs.first.input_value() != query:
        raise RoutingError("工单查询条件在读取中发生变化，已停止自动选单。")


def _prepare_search(page, query, label):
    remote.reset_remote_workorder_dialogs(page)
    fx.open_technical_service_list(page)
    _select_business_type(page, label)
    if remote.clear_remote_workorder_list_filters(page) is False:
        raise RoutingError("工单列表仍有固定筛选，查询范围不完整。")
    fx.select_technical_service_search_field(page, "全字段")
    inputs = page.locator(fx.TECHNICAL_SERVICE_SEARCH_INPUT_SELECTOR + ":visible")
    if inputs.count() != 1:
        raise RoutingError("工单搜索框不唯一，已停止自动选单。")
    inputs.first.fill(query)
    inputs.first.press("Enter")
    page.wait_for_timeout(1_500)
    _assert_search_state(page, query, label)


def _row_with_url(page, row):
    number = str(row.get("workorder_id", ""))
    if not fx.JSFW_ID_PATTERN.fullmatch(number):
        raise RoutingError("工单列表存在无效编号。")
    table, _pager, _hidden = remote._locate_remote_workorder_table(page)
    if table is None:
        raise RoutingError("工单列表归属不明确。")
    cells = table.locator(f'div.j-td[data-fieldname="name"][title="{number}"]:visible')
    if cells.count() != 1:
        raise RoutingError("同编号可见工单对象不唯一，已停止自动选单。")
    data_id = cells.first.get_attribute("data-id") or ""
    if not _DATA_ID.fullmatch(data_id):
        raise RoutingError("工单列表缺少可靠对象ID。")
    return {**row, "detail_url": fx.FXIAOKE_HOME_URL.rsplit("#", 1)[0] + f"#objdetail/CasesObj/{data_id}"}


def _collect_complete_rows(page, query, label):
    state, rows = remote._wait_remote_workorder_page(page, 1, query=query)
    total, pages = state["total"], state["pages"]
    _assert_search_state(page, query, label)
    if total > MAX_CANDIDATES:
        raise RoutingError("匹配工单超过100条，无法安全自动选单，请缩小业务范围。")
    if total == 0:
        return []
    collected = {}
    seen_objects = set()
    snapshots = []
    previous_ids = None
    for number in range(1, pages + 1):
        if number > 1:
            page.locator("div.crm-w-pagination:visible .el-icon-arrow-right").click(timeout=5_000)
            state, rows = remote._wait_remote_workorder_page(page, number, previous_ids=previous_ids)
        _assert_search_state(page, query, label)
        if (state["total"], state["pages"]) != (total, pages):
            raise RoutingError("工单分页数量发生变化，已停止自动选单。")
        current = [_row_with_url(page, row) for row in rows]
        snapshot = {row["workorder_id"]: row for row in current}
        if len(snapshot) != len(current):
            raise RoutingError("工单列表存在重复编号。")
        snapshots.append(snapshot)
        previous_ids = frozenset(snapshot)
        for row in current:
            object_id = row["detail_url"].rsplit("/", 1)[-1]
            if row["workorder_id"] in collected or object_id in seen_objects:
                raise RoutingError("工单跨页编号或对象ID重复，查询不完整。")
            seen_objects.add(object_id)
            collected[row["workorder_id"]] = row
        if len(collected) > total:
            raise RoutingError("工单数量超过分页总数。")
    if len(collected) != total:
        raise RoutingError("工单分页未完整收集，已停止自动选单。")
    for number in range(pages, 0, -1):
        previous_ids = frozenset(row["workorder_id"] for row in rows)
        if number < pages:
            page.locator("div.crm-w-pagination:visible .el-icon-arrow-left").click(timeout=5_000)
        state, rows = remote._wait_remote_workorder_page(
            page, number, previous_ids=previous_ids if number < pages else None,
        )
        _assert_search_state(page, query, label)
        current = [_row_with_url(page, row) for row in rows]
        if ((state["total"], state["pages"]) != (total, pages)
                or {row["workorder_id"]: row for row in current} != snapshots[number - 1]):
            raise RoutingError("工单列表在逆向复核时变化，已停止自动选单。")
    return list(collected.values())


def _read_detail_snapshot(page, url):
    parts = urlsplit(url)
    match = re.fullmatch(r"objdetail/CasesObj/([a-fA-F0-9]{24})", parts.fragment)
    if (_origin(url) != _origin(fx.FXIAOKE_HOME_URL) or not match
            or parts.path != urlsplit(fx.FXIAOKE_HOME_URL).path or parts.query):
        raise RoutingError("工单详情地址不符合已验证的只读契约。")
    detail = page.context.new_page()
    try:
        with WebDetailObserver(detail, url, "CasesObj", match[1]) as observed:
            if not observed.enabled:
                raise RoutingError("工单详情只读观察未能建立。")
            fx.fast_goto(detail, url)
            deadline = time.monotonic() + DETAIL_TIMEOUT_SECONDS
            while True:
                values = routing_values(observed.value)
                if values is not None and values["data_id"] == match[1]:
                    return values
                if time.monotonic() >= deadline:
                    raise RoutingError("工单详情字段契约不完整，不能判定该工单是否可选。")
                detail.wait_for_timeout(200)
    finally:
        detail.close()


def _matched_detail(page, row):
    observed = _read_detail_snapshot(page, row["detail_url"])
    if (observed["workorder_id"] != row["workorder_id"]
            or remote.strict_customer_identity(observed["customer_name"]) != remote.strict_customer_identity(row["customer_name"])
            or observed["data_id"] != row["detail_url"].rsplit("/", 1)[-1]):
        raise RoutingError("工单详情与列表编号、客户或对象ID不一致。")
    return observed


def read_workorder_snapshot(page, workorder_id):
    if not isinstance(workorder_id, str) or not fx.JSFW_ID_PATTERN.fullmatch(workorder_id):
        raise RoutingError("工单编号无效。")
    search = page.context.new_page()
    try:
        _prepare_search(search, workorder_id, "全部业务类型")
        rows = _collect_complete_rows(search, workorder_id, "全部业务类型")
        matching = [row for row in rows if row["workorder_id"] == workorder_id]
        if len(matching) != 1:
            raise RoutingError("指定工单没有精确唯一命中，已停止自动选单。")
        return _matched_detail(search, matching[0])
    finally:
        search.close()


def find_latest_after_sales_workorder(page, source, report_date):
    search = page.context.new_page()
    try:
        query = source["customer_name"]
        _prepare_search(search, query, AFTER_SALES_LABEL)
        rows = _collect_complete_rows(search, query, AFTER_SALES_LABEL)
        candidates = []
        for row in rows:
            candidate = _matched_detail(search, row)
            if candidate["record_type"] != AFTER_SALES_TYPE:
                raise RoutingError("售后筛选返回了其他业务类型，查询未可靠生效。")
            candidates.append(candidate)
        if _collect_complete_rows(search, query, AFTER_SALES_LABEL) != rows:
            raise RoutingError("工单列表在详情读取后发生变化，已停止自动选单。")
        _assert_search_state(search, query, AFTER_SALES_LABEL)
        return select_after_sales_workorder(source, candidates, report_date)
    finally:
        search.close()


def open_target_workorder(page, snapshot):
    if snapshot.get("record_type") != AFTER_SALES_TYPE:
        raise RoutingError("仅允许打开已冻结的售后目标工单。")
    fx.dismiss_active_workorder_dialog(page)
    fx.open_technical_service_list(page)
    _select_business_type(page, AFTER_SALES_LABEL)
    customer = fx.open_known_workorder_for_service_record_verification(page, snapshot["workorder_id"])
    if remote.strict_customer_identity(customer) != remote.strict_customer_identity(snapshot["customer_name"]):
        raise RoutingError("目标工单客户或部门已变化，未提交。")
    current = _row_with_url(page, {"workorder_id": snapshot["workorder_id"]})
    if current["detail_url"].rsplit("/", 1)[-1] != snapshot["data_id"]:
        raise RoutingError("目标工单对象ID已变化，未提交。")
    return customer
