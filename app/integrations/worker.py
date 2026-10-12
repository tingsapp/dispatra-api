"""Connected TMS worker: `python -m app.integrations.worker` alongside the API.

Each cycle syncs drivers and vehicles and polls orders for every enabled connection whose interval has passed, then
processes received TMS orders and sends order events to the TMS and the company webhook. Logs carry counts and error class names only, never credentials, URLs or TMS data.
"""
import argparse
import time
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from . import fleet, order_intake, outbound
from .models import TmsConnection


def due():
    """(fleet syncs due, order polls due) as company IDs."""
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        rows = db.execute(select(TmsConnection).where(TmsConnection.enabled.is_(True))).scalars().all()
        def passed(at, minutes): return at is None or now() - at >= timedelta(minutes=minutes)
        return ([row.organization_id for row in rows if passed(row.last_fleet_sync_at, row.fleet_sync_minutes)],
            [row.organization_id for row in rows if row.sync_method == 'polling' and passed(row.last_order_poll_at, row.poll_interval_minutes)])


def cycle():
    synced = polled = processed = 0
    fleets, polls = due()
    for organization_id in fleets:
        try: synced += fleet.sync(organization_id) is not None
        except Exception as exc: print(f'TMS fleet sync error: {type(exc).__name__}', flush=True)
    for organization_id in polls:
        try: polled += order_intake.poll(organization_id) or 0
        except Exception as exc: print(f'TMS order poll error: {type(exc).__name__}', flush=True)
    for organization_id in order_intake.pending_companies():
        try: processed += order_intake.process_pending(organization_id)
        except Exception as exc: print(f'TMS order processing error: {type(exc).__name__}', flush=True)
    sent = 0
    for target in (outbound.TMS_STATUS, outbound.API_WEBHOOKS):
        for organization_id in outbound.companies(target):
            try: sent += outbound.deliver(organization_id, target) or 0
            except Exception as exc: print(f'Outbound {target.consumer} error: {type(exc).__name__}', flush=True)
    return synced, polled, processed, sent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=60)
    args = parser.parse_args()
    while True:
        synced, polled, processed, sent = cycle()
        if args.once:
            print(f'Synced {synced} TMS fleet(s); stored {polled} polled order(s); processed {processed} order(s); sent {sent} event(s).')
            return
        time.sleep(max(15, args.interval))


if __name__ == '__main__': main()
