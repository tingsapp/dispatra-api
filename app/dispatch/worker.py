"""Dispatch agent worker: `python -m app.dispatch.worker` alongside the API.

Each cycle evaluates due Orders in every company whose dispatch mode is AUTO. Logs carry counts and error
class names only, never addresses, names or provider responses.
"""
import argparse
import time
from collections import Counter
from . import service


def cycle():
    totals = Counter()
    for organization_id in service.auto_companies():
        try: totals.update(service.dispatch_pending(organization_id))
        except Exception as exc: print(f'Dispatch agent error: {type(exc).__name__}', flush=True)
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
