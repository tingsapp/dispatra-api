"""Versioned email draft edits and authenticated shipper detail requests."""
import os
from urllib.parse import urlsplit
from uuid import uuid4
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from app.email_templates import button, layout, paragraph
from app.models import Organization, User, now
from app.operations.common import command, record, version
from app.operations.orders import bookable_drivers
from app.operations.models import Catalog, EmailDelivery, RateCard, Shipper
from app.operations.schemas import Booking, EmailDeliveryView
from app.services import audit
from .draft_schemas import DraftBooking, DraftShipment, ShipperDraftView
from .models import EmailIntake

DRAFTS = {'NEEDS_REVIEW', 'UNKNOWN_SENDER'}


def editable(db, actor, identity, expected=None):
    row = record(db, EmailIntake, actor, identity, lock=expected is not None)
    if expected is not None: version(row, expected)
    if row.status not in DRAFTS: raise HTTPException(409, 'This draft is no longer available.')
    return row


def validate_references(db, actor, draft):
    for key, model in [('shipper_id', Shipper), ('billing_shipper_id', Shipper), ('rate_card_id', RateCard)]:
        if draft.get(key): record(db, model, actor, draft[key])
    if draft.get('preferred_driver_id') and not bookable_drivers(db, actor, draft['preferred_driver_id']):
        raise HTTPException(422, 'Choose an available driver or no preference.')
    for identity, kind in [(draft.get('service_id'), 'SERVICE'), (draft.get('vehicle_type_id'), 'VEHICLE_TYPE'),
                           *((item['id'], 'ACCESSORIAL') for item in draft.get('accessorials', [])),
                           *((item.get('package_type_id'), 'PACKAGE_TYPE') for item in draft.get('items', []))]:
        if identity:
            entry = record(db, Catalog, actor, identity)
            if entry.kind != kind or not entry.active: raise HTTPException(422, 'Choose an active catalogue option.')


def missing_facts(draft):
    labels = {'service_id': 'Service', 'scheduled_at': 'Pickup date and time', 'address': 'Address', 'text': 'Street address',
              'city': 'City', 'province': 'Province', 'postal_code': 'Postal code', 'quantity': 'Quantity',
              'weight_kg': 'Weight', 'length_cm': 'Length', 'width_cm': 'Width', 'height_cm': 'Height',
              'pickup_id': 'Pickup', 'delivery_id': 'Delivery'}
    missing = [] if draft.get('shipper_id') else ['Shipper']
    try: Booking.model_validate(draft)
    except ValidationError as error:
        for issue in error.errors():
            loc = issue['loc']
            prefix = f"{'Stop' if loc[0] == 'stops' else 'Package'} {loc[1] + 1}: " if len(loc) > 1 and isinstance(loc[1], int) else ''
            label = labels.get(str(loc[-1]), str(loc[-1]).replace('_', ' ').capitalize()) if loc else str(issue['msg']).removeprefix('Value error, ')
            missing.append(prefix + label)
    return list(dict.fromkeys(missing))


def save(db, actor, identity, data, key, view):
    def run():
        row = editable(db, actor, identity, data.version)
        draft = data.draft.model_dump(mode='json')
        validate_references(db, actor, draft)
        if str(row.shipper_id or '') != str(draft['shipper_id'] or ''):
            row.completion_requested_at = row.completion_submitted_at = None
            row.sender_verified = False
        row.shipper_id = data.draft.shipper_id
        row.draft, row.missing = draft, missing_facts(draft)
        # Once a dispatcher has selected a company Shipper, the known account is
        # sufficient for draft review; mailbox authentication remains diagnostic.
        if not row.sender_verified and not row.shipper_id:
            row.missing.insert(0, 'Sender verification: confirm this email really came from the Shipper')
        row.status = 'NEEDS_REVIEW' if row.shipper_id else 'UNKNOWN_SENDER'
        row.version += 1
        db.flush()
        audit(db, actor, 'intake.draft_saved', row.id, actor.organization_id)
        return view(row)
    return command(db, actor, key, 'intake-draft-save', {'id': str(identity), **data.model_dump(mode='json')}, run)


def request_details(db, actor, identity, data, key):
    def run():
        row = editable(db, actor, identity, data.version)
        if not row.shipper_id: raise HTTPException(422, 'Choose a shipper and save the draft first.')
        shipper = record(db, Shipper, actor, row.shipper_id)
        account = db.scalar(select(User.id).where(User.organization_id == actor.organization_id, User.shipper_id == shipper.id, User.active.is_(True), User.role == 'SHIPPER'))
        if shipper.status != 'ACTIVE' or not shipper.email or not account:
            raise HTTPException(422, 'The shipper needs an active account and email address.')
        base = os.environ.get('PUBLIC_WEB_URL', '').strip().rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.query or parsed.fragment:
            raise HTTPException(503, 'The public website address must be configured before sending a completion link.')
        duplicate = db.scalar(select(EmailDelivery.id).where(EmailDelivery.organization_id == actor.organization_id,
            EmailDelivery.intake_id == row.id, EmailDelivery.status.in_(['PENDING', 'SENDING', 'UNKNOWN'])))
        if duplicate: raise HTTPException(409, 'A completion email is pending or its delivery is uncertain. Check its delivery before requesting another.')
        company = db.get(Organization, actor.organization_id)
        link = f'{base}/{company.slug}/shipper?draft={row.id}'
        body = f'{company.name} needs a few details to finish your order draft. Sign in to your shipper account to complete it. Your changes will be returned to dispatch for review.'
        text = f'{body}\n\n{link}\n'
        html = layout(category='Order details', title='Complete your order details', preheader=body,
            content=paragraph(body) + button('Complete draft', link), company=company.name,
            footer='Sign in with the shipper account that received this request.')
        delivery_id = uuid4()
        delivery = EmailDelivery(id=delivery_id, organization_id=actor.organization_id, intake_id=row.id,
            requested_by=actor.id, recipient=shipper.email.strip().lower(), subject=f'{company.name}: complete your order details'[:300],
            body_text=text, body_html=html, message_id=f'<dispatra-{delivery_id.hex}@dispatra.com>')
        db.add(delivery)
        row.completion_requested_at, row.completion_submitted_at = now(), None
        row.version += 1
        db.flush()
        audit(db, actor, 'intake.details_requested', row.id, actor.organization_id)
        audit(db, actor, 'email.requested', delivery.id, actor.organization_id)
        return EmailDeliveryView.model_validate(delivery).model_dump(mode='json')
    return command(db, actor, key, 'intake-request-details', {'id': str(identity), **data.model_dump(mode='json')}, run)


def shipper_record(db, actor, identity, expected=None):
    row = editable(db, actor, identity, expected)
    if row.shipper_id != actor.shipper_id or not row.completion_requested_at:
        raise HTTPException(404, 'Draft not found.')
    return row


def shipper_view(row):
    safe = {key: value for key, value in (row.draft or {}).items() if key in DraftShipment.model_fields}
    return ShipperDraftView(id=row.id, version=row.version, draft=DraftShipment.model_validate(safe), submitted_at=row.completion_submitted_at).model_dump(mode='json')


def submit(db, actor, identity, data, key):
    def run():
        row = shipper_record(db, actor, identity, data.version)
        if row.completion_submitted_at: raise HTTPException(409, 'These details have already been submitted.')
        draft = {**(row.draft or {}), **data.draft.model_dump(mode='json'), 'shipper_id': str(actor.shipper_id)}
        # Shippers can fill shipment facts only. The dispatcher owns the stored commercial fields.
        validate_references(db, actor, data.draft.model_dump(mode='json'))
        missing = missing_facts(draft)
        if missing: raise HTTPException(422, 'Complete the missing details: ' + '; '.join(missing))
        row.draft, row.missing, row.completion_submitted_at = draft, [], now()
        row.version += 1
        db.flush()
        audit(db, actor, 'intake.details_submitted', row.id, actor.organization_id)
        return shipper_view(row)
    return command(db, actor, key, 'intake-shipper-submit', {'id': str(identity), **data.model_dump(mode='json')}, run)
