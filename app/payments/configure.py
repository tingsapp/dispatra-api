"""Administrator links an already-connected Stripe account; never accepts public IDs from shippers."""
import argparse
import os
import re
from uuid import uuid4
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database import context
from app.models import Organization, User
from app.services import audit
from .models import StripeConnection
from . import provider


def main():
    parser = argparse.ArgumentParser(description='Link a company to an existing account connected to your Stripe platform.')
    parser.add_argument('--company', required=True)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'acct_[A-Za-z0-9]+', args.account): parser.error('Use a Stripe connected account ID.')
    remote = provider.request('GET', 'accounts/' + args.account)
    if remote.get('id') != args.account or not remote.get('details_submitted') or not remote.get('charges_enabled'):
        raise SystemExit('Complete Stripe onboarding and enable charges for this connected account first.')
    with Session(create_engine(os.environ['MIGRATION_DATABASE_URL'])) as db, db.begin():
        context(db, platform=True)
        company = db.scalar(select(Organization).where(Organization.slug == args.company, Organization.active.is_(True)).with_for_update())
        if company is None: raise SystemExit('Company not found.')
        owner = db.scalar(select(User).where(User.role == 'ADMIN', User.active.is_(True)))
        if owner is None: raise SystemExit('An active platform administrator is required.')
        row = db.scalar(select(StripeConnection).where(StripeConnection.organization_id == company.id,
            StripeConnection.livemode == provider.livemode()))
        if row and row.account_id != args.account:
            raise SystemExit('This company already has a different Stripe account in this mode. Account migration requires review; existing cards were not changed.')
        if not row:
            db.add(StripeConnection(id=uuid4(), organization_id=company.id, account_id=args.account, livemode=provider.livemode()))
            audit(db, owner, 'company.stripe_connected', company.id, company.id)
    print('Company Stripe connection configured (' + ('live' if provider.livemode() else 'test') + ' mode).')


if __name__ == '__main__': main()
