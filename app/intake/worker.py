"""Order agent worker: `python -m app.intake.worker` alongside the API.

Each cycle reads new mail from every enabled company mailbox, then processes RECEIVED intakes. Logs carry
error class names only, never credentials, addresses or message content.
"""
import argparse
import time
from . import service


def cycle():
    stored = processed = 0
    for organization_id in service.mailboxes():
        try: stored += service.poll_mailbox(organization_id)
        except Exception as exc: print(f'Order agent mailbox error: {type(exc).__name__}', flush=True)
    for organization_id in service.pending():
        try: processed += service.process_pending(organization_id)
        except Exception as exc: print(f'Order agent processing error: {type(exc).__name__}', flush=True)
    return stored, processed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=60)
    args = parser.parse_args()
    while True:
        stored, processed = cycle()
        if args.once:
            print(f'Stored {stored} email(s); processed {processed}.')
            return
        time.sleep(max(15, args.interval))


if __name__ == '__main__': main()
