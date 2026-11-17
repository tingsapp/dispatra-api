"""Company work hours and holidays applied to an Order's scheduled pickup, in the company time zone."""
from datetime import datetime
from zoneinfo import ZoneInfo

DAY_NAMES = {'MON': 'Monday', 'TUE': 'Tuesday', 'WED': 'Wednesday', 'THU': 'Thursday', 'FRI': 'Friday', 'SAT': 'Saturday', 'SUN': 'Sunday'}


def closed_reason(config, at: datetime) -> str | None:
    """Why a pickup at `at` falls outside company hours, or None when the company is open. Times are minute-precise."""
    local = at.astimezone(ZoneInfo(config.time_zone))
    holiday = next((h for h in config.holidays if h.date == local.date()), None)
    if holiday: return f'Closed on {holiday.name} ({local:%b} {local.day}).'
    day = config.work_hours[local.weekday()]
    name = DAY_NAMES[day.day]
    if not day.open: return f'Closed on {name}s.'
    # 23:59 closes at midnight, so an all-day 00:00–23:59 schedule has no gap.
    end = '24:00' if day.end == '23:59' else day.end
    if not day.start <= f'{local:%H:%M}' < end: return f'{name} pickups are {day.start}–{end}.'
    return None
