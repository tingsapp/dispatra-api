"""Run alongside API: python -m app.company_billing.worker [--once]."""
import argparse
import time
from . import recurring, provider


def cycle():
    if not provider.available(): return 0
    count = 0
    for identity in recurring.due():
        try: count += recurring.synchronize(identity)
        except Exception as exc:
            print('Company billing retry: ' + type(exc).__name__, flush=True)
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        try: count = cycle()
        except Exception as exc:
            print('Company billing unavailable: ' + type(exc).__name__, flush=True)
        if args.once: return
        time.sleep(5)


if __name__ == '__main__': main()
