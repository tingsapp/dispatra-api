"""Company work hours and holidays applied to an Order's scheduled pickup and planned deliveries, in the company time zone."""
from datetime import datetime, time, timedelta
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


def day_bounds(config, at: datetime) -> tuple[datetime, datetime]:
    """Company-local start of `at`'s day and of the next day, as aware datetimes."""
    zone = ZoneInfo(config.time_zone)
    day = at.astimezone(zone).date()
    return datetime.combine(day, time.min, zone), datetime.combine(day + timedelta(days=1), time.min, zone)


def opening(config, at: datetime) -> datetime:
    """When the company opens on `at`'s day (its work-hours start), even when that day is closed."""
    start, _ = day_bounds(config, at)
    hour, minute = map(int, config.work_hours[start.weekday()].start.split(':'))
    return start.replace(hour=hour, minute=minute)


def off_day(config, at: datetime) -> bool:
    """Whether `at` falls on a Saturday, Sunday or company holiday, whatever the company's work hours."""
    local = at.astimezone(ZoneInfo(config.time_zone))
    return local.weekday() >= 5 or any(h.date == local.date() for h in config.holidays)


def delivered_off_day(config, booking) -> bool:
    """Whether any drop-off is planned on an off day: its window start, else its deadline, else the scheduled pickup day."""
    return any(off_day(config, stop.window_start or stop.window_end or booking.scheduled_at)
        for stop in booking.stops if stop.kind == 'DROPOFF')
