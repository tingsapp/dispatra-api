"""Order agent worker: `python -m app.intake.worker` alongside the API.

Each cycle reads new mail from every enabled company mailbox, then processes RECEIVED intakes. Logs carry
error class names only, never credentials, addresses or message content.
"""
import argparse
import time
from . import service
from app.agents.telemetry import monitor_run


def cycle():
    stored = processed = 0
    with monitor_run('ORDER') as progress:
        progress.step('Finding enabled company mailboxes.', 5)
        companies = service.mailboxes()
        for index, organization_id in enumerate(companies):
            progress.step('Polling the company order mailbox.', 10 + int(30 * index / len(companies)), organization_id)
            try: stored += service.poll_mailbox(organization_id)
            except Exception as exc:
                progress.step('Company mailbox poll failed.', organization_id=organization_id, level='ERROR')
                print(f'Order agent mailbox error: {type(exc).__name__}', flush=True)
        progress.step('Finding emails queued for processing.', 45)
        companies = service.pending()
        for index, organization_id in enumerate(companies):
            progress.step('Processing the company email queue.', 50 + int(40 * index / len(companies)), organization_id)
            try: processed += service.process_pending(organization_id)
            except Exception as exc:
                progress.step('Company email processing failed.', organization_id=organization_id, level='ERROR')
                print(f'Order agent processing error: {type(exc).__name__}', flush=True)
        progress.step(f'Stored {stored} email(s); processed {processed} queued task(s).', 95)
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
