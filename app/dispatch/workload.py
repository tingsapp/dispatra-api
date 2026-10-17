"""Observed work today plus committed remaining route time, in the company's calendar."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from app.models import now
from app.operations.models import Route, Order


def measure(db, actor, driver, config, route, plan, orders):
    current = now()
    midnight = datetime.combine(current.astimezone(ZoneInfo(config.time_zone)).date(), time.min, ZoneInfo(config.time_zone)).astimezone(timezone.utc)
    routes = db.scalars(select(Route).where(Route.organization_id == actor.organization_id,
        Route.driver_id == driver.id, Route.started_at.is_not(None),
        (Route.completed_at.is_(None) | (Route.completed_at >= midnight)), Route.started_at <= current)).all()
    worked = sum(max(0, ((r.completed_at or current)-max(r.started_at,midnight)).total_seconds()/60) for r in routes)
    completed = db.scalar(select(func.count()).select_from(Order).join(Route, Order.route_id == Route.id).where(
        Order.organization_id == actor.organization_id, Route.organization_id == actor.organization_id,
        Route.driver_id == driver.id, Order.status == 'COMPLETED', Order.completed_at >= midnight)) or 0
    start = current if route and route.status == 'IN_PROGRESS' else datetime.fromisoformat(plan['planned_at'])
    finish = max(datetime.fromisoformat(v['planned_at']) for v in plan['stops'])
    stop_map = {s['id']:s for o in orders for s in o.facts['stops']}
    last = plan['stops'][-1]['stop_id']
    remaining = max(0,(finish-start).total_seconds()/60) + stop_map[last].get('service_minutes',0)
    available = driver.data['maximum_work_minutes']
    if driver.data.get('shift_start') and driver.data.get('shift_end'):
        available = min(available, (datetime.fromisoformat(driver.data['shift_end'])-datetime.fromisoformat(driver.data['shift_start'])).total_seconds()/60)
    return round(worked,1), round(remaining,1), int(completed), round((worked+remaining)/max(available,1),3)
