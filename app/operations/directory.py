"""Company settings, catalogues, and transactional profile/account administration."""
import secrets
from uuid import uuid4
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, delete, func
from app.models import User, LoginSession, Organization, now
from app.security import hash_password
from app.services import audit
from .models import Catalog, RateCard, Shipper, Driver, Vehicle, Route, Settings, DutySession
from .identifiers import issue_number, vehicle_number
from .schemas import SettingsView, CatalogView, RateView
from .common import record, version, company_lock, command, changed, dump, settings, operational_shipper


def save_settings(db, actor, data, key):
    def run():
        company_lock(db, actor)
        row, _ = settings(db, actor)
        version(row, data.version)
        if data.data.default_service_id:
            service = db.get(Catalog, data.data.default_service_id)
            if service is None or service.organization_id != actor.organization_id or service.kind != 'SERVICE' or not service.active:
                raise HTTPException(422, 'Choose an active service level as the default.')
        values = data.data.model_dump(mode='json')
        if 'tms_workspace' not in data.data.model_fields_set:
            values['tms_workspace'] = row.data.get('tms_workspace', False)
        if values['dispatch_mode'] == 'AUTO' and row.data.get('dispatch_mode', 'MANUAL') != 'AUTO' and values['tms_workspace']:
            from .dispatch_mode import require_connected_tms
            require_connected_tms(db, actor)
        if 'tms_workspace' in data.data.model_fields_set:
            from .tms_pricing import apply_workspace_rate
            apply_workspace_rate(db, actor, row, values['tms_workspace'])
        if 'dispatch_zones' not in data.data.model_fields_set:
            values['dispatch_zones'] = row.data.get('dispatch_zones', [])
        row.data = values
        db.get(Organization, actor.organization_id).name = data.data.company_name
        changed(db, actor, row, 'settings.updated')
        return SettingsView.model_validate(row).model_dump(mode='json')
    payload = data.model_dump(mode='json')
    if 'tms_workspace' not in data.data.model_fields_set: payload['data'].pop('tms_workspace')
    if 'dispatch_zones' not in data.data.model_fields_set: payload['data'].pop('dispatch_zones')
    return command(db, actor, key, 'settings', payload, run)


def save_catalog(db, actor, data, key, identity=None):
    def run():
        company_lock(db, actor)
        row = record(db, Catalog, actor, identity, True) if identity else None
        if row:
            version(row, data.version)
            if not row.active: raise HTTPException(409, 'Deleted catalogue entry cannot be edited.')
        kind = row.kind if row else data.kind
        values = data.data.model_dump(mode='json')
        if 'road_profile' not in data.data.model_fields_set:
            values.pop('road_profile', None)
            if row and 'road_profile' in row.data: values['road_profile'] = row.data['road_profile']
        if kind == 'VEHICLE_TYPE' and not values.get('road_profile'):
            raise HTTPException(422, 'Outside length, width, height and loaded gross weight are required for a Vehicle Type.')
        if values.get('road_profile') and kind != 'VEHICLE_TYPE':
            raise HTTPException(422, 'Truck routing measurements belong to a Vehicle Type.')
        if values.get('road_profile'):
            gross = values['road_profile']['gross_weight_kg']
            payloads = db.scalars(select(Vehicle.data['payload_kg'].astext).where(
                Vehicle.organization_id == actor.organization_id, Vehicle.type_id == identity,
                Vehicle.archived_at.is_(None))).all() if identity else []
            from decimal import Decimal
            if any(Decimal(payload) > gross for payload in payloads) or (values.get('payload_kg') and Decimal(values['payload_kg']) > gross):
                raise HTTPException(422, 'Loaded gross weight must cover this type and its registered vehicles’ payload capacities.')
        if row:
            profile_changed = values.get('road_profile') != row.data.get('road_profile')
            legacy_disabled = 'road_profile' in values and 'road_profile' not in row.data
            if profile_changed or legacy_disabled:
                active = select(Route.id).join(Vehicle, (Vehicle.id == Route.vehicle_id) &
                    (Vehicle.organization_id == Route.organization_id)).where(Route.organization_id == actor.organization_id,
                    Vehicle.organization_id == actor.organization_id, Vehicle.type_id == row.id,
                    Route.status.in_(['PLANNED', 'IN_PROGRESS']))
                if not profile_changed:
                    active = active.where(Vehicle.data['road_profile'].astext.is_not(None))
                if db.scalar(active.limit(1)):
                    from .travel import dispatch_provider
                    # Initial setup does not replace an existing truck profile or rebuild a Google-planned trip.
                    first_setup = (not row.data.get('road_profile') and bool(values.get('road_profile'))
                        and dispatch_provider() != 'tomtom'
                        and not db.scalar(active.where(Vehicle.data['road_profile'].astext.is_not(None)).limit(1)))
                    if not first_setup:
                        raise HTTPException(409, 'Vehicles of this type have active Routes; finish them before changing existing truck routing measurements.')
        if row:
            row.data = values
            changed(db, actor, row, 'catalog.updated')
        else:
            row = Catalog(organization_id=actor.organization_id, kind=kind, code=data.code, data=values)
            db.add(row); db.flush()
            audit(db, actor, 'catalog.created', row.id, actor.organization_id)
        return CatalogView.model_validate(row).model_dump(mode='json')
    payload = data.model_dump(mode='json')
    if 'road_profile' not in data.data.model_fields_set: payload['data'].pop('road_profile', None)
    return command(db, actor, key, 'catalog:' + str(identity or 'new'), payload, run)


def save_rate(db, actor, data, key, identity=None):
    def run():
        company_lock(db, actor)
        if data.is_default and not getattr(data, 'active', True): raise HTTPException(422, 'Default rate card must be active.')
        if data.is_default:
            for old in db.scalars(select(RateCard).where(RateCard.organization_id == actor.organization_id, RateCard.is_default.is_(True))):
                if old.id != identity:
                    old.is_default = False
                    changed(db, actor, old, 'rate.default_removed')
        if identity:
            row = record(db, RateCard, actor, identity, True)
            version(row, data.version)
            if not row.active: raise HTTPException(409, 'Archived Rate Card cannot be edited.')
            row.data = data.data.model_dump(mode='json')
            row.is_default, row.active = data.is_default, data.active
            changed(db, actor, row, 'rate.updated')
        else:
            row = RateCard(organization_id=actor.organization_id, code=data.code, is_default=data.is_default, data=data.data.model_dump(mode='json'))
            db.add(row); db.flush()
            audit(db, actor, 'rate.created', row.id, actor.organization_id)
        return RateView.model_validate(row).model_dump(mode='json')
    return command(db, actor, key, 'rate:' + str(identity or 'new'), data.model_dump(mode='json'), run)


def remove_catalog(db, actor, identity, data, key):
    def run():
        row = record(db, Catalog, actor, identity, True)
        version(row, data.version)
        if str(row.id) == str(settings(db, actor)[1].default_service_id):
            raise HTTPException(409, 'Set another service level as Default before deleting this one.')
        row.active = False
        changed(db, actor, row, 'catalog.deleted')
        return {'id': str(row.id), 'version': row.version}
    return command(db, actor, key, 'catalog-delete:' + str(identity), data.model_dump(), run)


def default_rate(db, actor):
    return db.scalar(select(RateCard).where(RateCard.organization_id == actor.organization_id, RateCard.is_default.is_(True), RateCard.active.is_(True)))


def shipper_view(db, actor, row):
    """`rate_card_name` is the card that prices this shipper's orders: its own, else the company Default."""
    card = record(db, RateCard, actor, row.rate_card_id) if row.rate_card_id else default_rate(db, actor)
    from .orders import company_scope
    driver_name = None
    if row.assigned_driver_id:
        with company_scope(db, actor):
            driver_name = db.scalar(select(Driver.name).where(Driver.organization_id == actor.organization_id,
                Driver.id == row.assigned_driver_id))
    return jsonable_encoder({**{k: v for k, v in dump(row).items() if k not in {'contact_name', 'address'}},
        'rate_card_name': card.data['name'] if card else None, 'assigned_driver_name': driver_name})


def save_shipper(db, actor, data, key, identity=None):
    password = None
    def run():
        nonlocal password
        company_lock(db, actor)
        payload = data.data if identity else data
        if payload.rate_card_id:
            rate = record(db, RateCard, actor, payload.rate_card_id)
            if not rate.active: raise HTTPException(422, 'Choose an active Rate Card.')
        if identity:
            row = customer = operational_shipper(db, actor, identity, True); version(row, data.version)
            if row.archived_at: raise HTTPException(409, 'Archived Shipper cannot be edited.')
            user = db.scalar(select(User).where(User.shipper_id == identity, User.organization_id == actor.organization_id).with_for_update())
        else:
            identity_new = uuid4()
            row = customer = Shipper(id=identity_new, organization_id=actor.organization_id,
                number=issue_number(db, actor, Shipper, 'S'), name=payload.name)
            db.add(row); db.flush()  # the account row references shippers
            password = secrets.token_urlsafe(18)
            user = User(organization_id=actor.organization_id, shipper_id=identity_new, scope=str(actor.organization_id),
                role='SHIPPER', password_hash=hash_password(password), login_id=payload.email)
            db.add(user)
        if identity and data.status is not None:
            customer.status = data.status
            user.active = data.status != 'INACTIVE'
            if not user.active: db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        # Older clients that omit the new field preserve the saved assignment.
        selected = payload.assigned_driver_id if 'assigned_driver_id' in payload.model_fields_set or not identity else row.assigned_driver_id
        if selected and (not identity or selected != row.assigned_driver_id):
            driver = db.scalar(select(Driver).where(Driver.organization_id == actor.organization_id, Driver.id == selected,
                Driver.active.is_(True), Driver.archived_at.is_(None)))
            if driver is None: raise HTTPException(422, 'Choose an active company driver or no assigned driver.')
        row.assigned_driver_id = selected
        customer.name, customer.email, customer.phone = payload.name, payload.email, payload.phone
        customer.address = payload.warehouse.text
        row.kind, row.company_name = payload.kind, payload.company_name
        row.warehouse, row.discount = payload.warehouse.model_dump(mode='json'), payload.discount.model_dump(mode='json')
        # Every shipper carries a Rate Card; without a choice it is the company Default at save time.
        default = None if payload.rate_card_id else default_rate(db, actor)
        row.rate_card_id = payload.rate_card_id or (default.id if default else None)
        row.instructions, row.email_updates = payload.instructions, payload.email_updates
        if user.login_id != payload.email:
            user.login_id = payload.email
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        if identity: changed(db, actor, row, 'shipper.updated')
        else:
            db.flush(); audit(db, actor, 'shipper.created', row.id, actor.organization_id)
        return shipper_view(db, actor, row)
    values = data.model_dump(mode='json')
    payload = data.data if identity else data
    if 'assigned_driver_id' not in payload.model_fields_set:
        (values['data'] if identity else values).pop('assigned_driver_id', None)
    result = command(db, actor, key, 'shipper:' + str(identity or 'new'), values, run)
    return {**result, 'initial_password': password}


def in_use(db, actor, field, identity):
    return db.scalar(select(Route.id).where(Route.organization_id == actor.organization_id,
        getattr(Route, field) == identity, Route.status.in_(['PLANNED', 'IN_PROGRESS']))) is not None


def save_vehicle(db, actor, data, key, identity=None):
    def run():
        company_lock(db, actor)
        payload = data.data if identity else data
        vehicle_type = record(db, Catalog, actor, payload.type_id)
        if vehicle_type.kind != 'VEHICLE_TYPE' or not vehicle_type.active: raise HTTPException(422, 'Choose an active vehicle type.')
        if identity:
            row = record(db, Vehicle, actor, identity, True); version(row, data.version)
            if row.archived_at: raise HTTPException(409, 'Archived Vehicle cannot be edited.')
            if in_use(db, actor, 'vehicle_id', identity): raise HTTPException(409, 'Vehicle has an active Route; finish or cancel it before editing.')
        else:
            row = Vehicle(organization_id=actor.organization_id)
        duplicate = select(Vehicle.id).where(Vehicle.organization_id == actor.organization_id,
            func.upper(func.btrim(Vehicle.data['unit_number'].astext)) == payload.unit_number)
        if identity: duplicate = duplicate.where(Vehicle.id != identity)
        if db.scalar(duplicate) is not None: raise HTTPException(409, 'Unit number already exists in this company, including archived vehicles.')
        company = db.get(Organization, actor.organization_id)
        row.number = vehicle_number(payload.unit_number, company.name, company.slug, row.number if identity else None)
        if not identity: db.add(row)
        type_changed = identity and row.type_id != payload.type_id
        row.type_id, row.plate, row.province = payload.type_id, payload.plate.upper(), payload.province.upper()
        values = payload.model_dump(mode='json')
        if identity and 'road_profile' not in payload.model_fields_set:
            values['road_profile'] = None if type_changed else row.data.get('road_profile')
            if values['road_profile'] and values['road_profile']['gross_weight_kg'] < payload.payload_kg:
                raise HTTPException(422, 'Update loaded gross weight before increasing payload capacity.')
        if vehicle_type.data.get('road_profile') and vehicle_type.data['road_profile']['gross_weight_kg'] < payload.payload_kg:
            raise HTTPException(422, 'Update loaded gross weight in Vehicle Types before increasing this vehicle’s payload capacity.')
        row.active, row.data = payload.active, values
        if identity: changed(db, actor, row, 'vehicle.updated')
        else:
            db.flush(); audit(db, actor, 'vehicle.created', row.id, actor.organization_id)
        from app.company_billing.models import CompanyBilling
        from app.company_billing.recurring import request_sync
        billing = db.get(CompanyBilling, actor.organization_id)
        if billing: request_sync(billing)
        return jsonable_encoder(dump(row))
    values = data.model_dump(mode='json')
    payload = data.data if identity else data
    if 'road_profile' not in payload.model_fields_set:
        (values['data'] if identity else values).pop('road_profile', None)
    return command(db, actor, key, 'vehicle:' + str(identity or 'new'), values, run)


def driver_view(db, actor, row):
    on_duty = db.scalar(select(DutySession.id).where(
        DutySession.organization_id == actor.organization_id,
        DutySession.driver_id == row.id, DutySession.ended_at.is_(None))) is not None
    return jsonable_encoder({**dump(row), 'on_duty': on_duty})


def save_driver(db, actor, data, key, identity=None):
    password = None
    def run():
        nonlocal password
        company_lock(db, actor)
        payload = data.data if identity else data
        if payload.vehicle_id:
            vehicle = record(db, Vehicle, actor, payload.vehicle_id, True)
            if not vehicle.active: raise HTTPException(422, 'Choose an active vehicle.')
        if identity:
            row = record(db, Driver, actor, identity, True); version(row, data.version)
            if row.archived_at: raise HTTPException(409, 'Archived Driver cannot be edited.')
            if in_use(db, actor, 'driver_id', identity): raise HTTPException(409, 'Driver has an active Route; finish or cancel it before editing.')
            user = db.scalar(select(User).where(User.driver_id == identity, User.organization_id == actor.organization_id).with_for_update())
            if data.duty_status is not None:
                current_duty = db.scalar(select(DutySession.id).where(
                    DutySession.organization_id == actor.organization_id,
                    DutySession.driver_id == row.id, DutySession.ended_at.is_(None))) is not None
                if current_duty != data.expected_on_duty:
                    raise HTTPException(409, 'Driver duty changed since this form was opened. Reload and try again.')
        else:
            row = Driver(id=uuid4(), organization_id=actor.organization_id, number=issue_number(db, actor, Driver, 'D')); db.add(row)
            password = secrets.token_urlsafe(18)
            user = User(organization_id=actor.organization_id, driver_id=row.id, scope=str(actor.organization_id),
                role='DRIVER', password_hash=hash_password(password), login_id=payload.email)
        row.name, row.email, row.phone = payload.name, payload.email, payload.phone
        row.address = payload.address.model_dump(mode='json')
        row.service_city = ' '.join(payload.address.city.casefold().split())
        row.vehicle_id, row.active = payload.vehicle_id, payload.active
        row.data = payload.model_dump(mode='json', exclude={'name', 'email', 'phone', 'address', 'vehicle_id', 'active'})
        db.flush()
        if not identity: db.add(user)
        if user.login_id != payload.email or user.active != payload.active:
            user.login_id, user.active = payload.email, payload.active
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
            if not payload.active:
                from .execution import close_duty_on_logout
                close_duty_on_logout(db,user,actor)
        if identity and data.duty_status is not None:
            if data.duty_status == 'ON_DUTY' and not payload.active:
                raise HTTPException(409, 'Activate the driver account before setting On Duty.')
            duty = db.scalar(select(DutySession).where(
                DutySession.organization_id == actor.organization_id,
                DutySession.driver_id == row.id, DutySession.ended_at.is_(None)).with_for_update())
            if data.duty_status == 'ON_DUTY' and duty is None:
                duty = DutySession(organization_id=actor.organization_id, driver_id=row.id, started_at=now())
                db.add(duty); db.flush()
                audit(db, actor, 'duty.started_by_dispatcher', duty.id, actor.organization_id)
            elif data.duty_status == 'OFF_DUTY' and duty is not None:
                duty.ended_at = now()
                changed(db, actor, duty, 'duty.ended_by_dispatcher')
        if identity: changed(db, actor, row, 'driver.updated')
        else:
            db.flush(); audit(db, actor, 'driver.created', row.id, actor.organization_id)
        return driver_view(db, actor, row)
    result = command(db, actor, key, 'driver:' + str(identity or 'new'), data.model_dump(mode='json'), run)
    return {**result, 'initial_password': password}


def reset_driver_password(db, actor, identity, key):
    password = None
    def run():
        nonlocal password
        driver = record(db, Driver, actor, identity, True)
        account = db.scalar(select(User).where(User.organization_id == actor.organization_id, User.driver_id == driver.id).with_for_update())
        if not account: raise HTTPException(404, 'Driver account not found.')
        password = secrets.token_urlsafe(18)
        account.password_hash = hash_password(password)
        db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
        from .execution import close_duty_on_logout
        close_duty_on_logout(db,account,actor)
        audit(db, actor, 'driver.password_reset', driver.id, actor.organization_id)
        return {'driver_id':str(driver.id),'login_id':account.login_id}
    result = command(db,actor,key,'driver-reset:' + str(identity),{},run)
    return {**result,'initial_password':password}


def change_directory_password(db, actor, identity, data, key, kind):
    """Dispatcher-supplied credentials; replay results contain only public record revisions."""
    if actor.role != 'DISPATCHER' or actor.id is None:
        raise HTTPException(403, 'Dispatcher account access required.')
    def run():
        company_lock(db, actor)
        row = operational_shipper(db, actor, identity, True) if kind == 'shipper' else record(db, Driver, actor, identity, True)
        version(row, data.version)
        if row.archived_at: raise HTTPException(409, 'Archived account cannot be changed.')
        link = User.shipper_id if kind == 'shipper' else User.driver_id
        account = db.scalar(select(User).where(User.organization_id == actor.organization_id,
            link == row.id, User.role == kind.upper()).with_for_update())
        if account is None: raise HTTPException(404, 'Account not found.')
        account.password_hash = hash_password(data.new_password)
        account.version += 1
        account.updated_at = now()
        db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
        if kind == 'driver':
            from .execution import close_duty_on_logout
            close_duty_on_logout(db, account, actor)
        changed(db, actor, row, f'{kind}.password_reset')
        return {'id': str(row.id), 'version': row.version}
    return command(db, actor, key, f'{kind}-password:{identity}', data.model_dump(mode='json'), run)


def update_shipper_profile(db, actor, data, key):
    def run():
        row = customer = operational_shipper(db,actor,actor.shipper_id,True); version(row,data.version)
        row.warehouse = data.warehouse.model_dump(mode='json')
        customer.phone, customer.address = data.phone, data.warehouse.text
        if data.contact_name is not None:
            customer.name = data.contact_name.strip()
            customer.contact_name = customer.name
        changed(db,actor,row,'shipper.profile_updated')
        return shipper_view(db,actor,row)
    return command(db,actor,key,'shipper-profile',data.model_dump(mode='json'),run)
