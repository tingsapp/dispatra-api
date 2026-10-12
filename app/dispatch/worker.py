"""Dispatch agent worker: `python -m app.dispatch.worker` alongside the API.

Each cycle evaluates due Orders in every company whose dispatch mode is AUTO. Logs carry counts and error
class names only, never addresses, names or provider responses.
"""
import argparse
import time
from collections import Counter
from . import service
from app.agents.telemetry import monitor_run


def cycle():
    totals = Counter()
    with monitor_run('DISPATCH') as progress:
        progress.step('Finding companies with Auto dispatch enabled.', 5)
        companies = service.auto_companies()
        for index, organization_id in enumerate(companies):
            progress.step('Checking eligible orders and driver feasibility.', 10 + int(80 * index / len(companies)), organization_id)
            try: totals.update(service.dispatch_pending(organization_id))
            except Exception as exc:
                progress.step('Company dispatch cycle failed.', organization_id=organization_id, level='ERROR')
                print(f'Dispatch agent error: {type(exc).__name__}', flush=True)
        progress.step(f'Assigned {totals["ASSIGNED"]} order(s); {totals["NO_CANDIDATE"]} need a driver.', 95)
    return totals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    while True:
        totals = cycle()
        if args.once:
            print(f"Assigned {totals['ASSIGNED']} order(s); {totals['NO_CANDIDATE']} without a driver.")
            return
        time.sleep(max(10, args.interval))


if __name__ == '__main__': main()
