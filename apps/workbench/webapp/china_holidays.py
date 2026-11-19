"""Official China work/rest-day calendars published year by year."""

from __future__ import annotations

from datetime import date, timedelta


# Additional weekday rest days created by holiday arrangements.
ADJUSTED_REST_DAYS = {
    2024: frozenset(
        {
            "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",
            "2024-04-05", "2024-05-02", "2024-05-03", "2024-09-16", "2024-10-04",
            "2024-10-07",
        }
    ),
    2025: frozenset(
        {
            "2025-02-03", "2025-02-04", "2025-05-05", "2025-06-02",
            "2025-10-07", "2025-10-08",
        }
    ),
    2026: frozenset(
        {
            "2026-01-02", "2026-02-20", "2026-02-23", "2026-04-06",
            "2026-05-04", "2026-05-05", "2026-10-05", "2026-10-06",
            "2026-10-07",
        }
    ),
}

STATUTORY_HOLIDAYS = {
    2024: frozenset(
        {
            "2024-01-01", "2024-02-10", "2024-02-11", "2024-02-12",
            "2024-04-04", "2024-05-01", "2024-06-10",
            "2024-09-17", "2024-10-01", "2024-10-02", "2024-10-03",
        }
    ),
    2025: frozenset(
        {
            "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30",
            "2025-01-31", "2025-04-04", "2025-05-01", "2025-05-02",
            "2025-05-31", "2025-10-01", "2025-10-02", "2025-10-03",
            "2025-10-06",
        }
    ),
    2026: frozenset(
        {
            "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18",
            "2026-02-19", "2026-04-05", "2026-05-01", "2026-05-02",
            "2026-06-19", "2026-09-25", "2026-10-01", "2026-10-02",
            "2026-10-03",
        }
    ),
}

# Weekend workdays explicitly ordered by the State Council.
ADJUSTED_WORKDAYS = {
    2024: frozenset(
        {
            "2024-02-04", "2024-02-18", "2024-04-07", "2024-04-28",
            "2024-05-11", "2024-09-14", "2024-09-29", "2024-10-12",
        }
    ),
    2025: frozenset(
        {
            "2025-01-26", "2025-02-08", "2025-04-27", "2025-09-28",
            "2025-10-11",
        }
    ),
    2026: frozenset(
        {
            "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
            "2026-09-20", "2026-10-10",
        }
    ),
}

# Sources: State Council General Office holiday arrangements for 2024–2026.
# https://www.gov.cn/zhengce/content/202310/content_6911527.htm
# https://www.gov.cn/zhengce/zhengceku/202411/content_6986383.htm
# https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm
AUTO_STATUS = {
    "adjusted_rest": {"status": "adjusted_rest", "label": "调休"},
    "statutory_holiday": {"status": "statutory_holiday", "label": "法定节假日"},
    "weekend_rest": {"status": "weekend_rest", "label": "双休"},
    "adjusted_workday": {"status": "adjusted_workday", "label": "调休上班"},
}
REST_STATUSES = frozenset({"adjusted_rest", "statutory_holiday", "weekend_rest"})


def automatic_day_statuses(start_date: str, end_date: str) -> dict[str, dict]:
    """Return official rest/work labels for an inclusive ISO date range."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError("日期范围无效")

    result = {}
    current = start
    while current <= end:
        day = current.isoformat()
        year = current.year
        if year in ADJUSTED_REST_DAYS and day in ADJUSTED_WORKDAYS[year]:
            status = AUTO_STATUS["adjusted_workday"]
        elif year in ADJUSTED_REST_DAYS and day in ADJUSTED_REST_DAYS[year]:
            status = AUTO_STATUS["adjusted_rest"]
        elif year in ADJUSTED_REST_DAYS and day in STATUTORY_HOLIDAYS[year]:
            status = AUTO_STATUS["statutory_holiday"]
        elif current.weekday() >= 5:
            status = AUTO_STATUS["weekend_rest"]
        else:
            status = None
        if status:
            result[day] = {"report_date": day, **status, "is_automatic": True}
        if current == end:
            break
        current += timedelta(days=1)
    return result


def automatic_day_status(value: str) -> dict | None:
    return automatic_day_statuses(value, value).get(value)


def is_china_workday(value: str) -> bool:
    status = automatic_day_status(value)
    if status:
        return status["status"] == "adjusted_workday"
    return date.fromisoformat(value).weekday() < 5
