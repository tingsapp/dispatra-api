"""Save the company's Overview form as one versioned, audited transaction."""
from app.models import now
from app.services import audit, once
from . import queries, service


def update_overview(db, actor, organization_id, data, key):
    def run():
        service.serialize_lock(db, organization_id)
        org = service.company(db, organization_id, True)
        service.expect(org, data.version, 'Company')
        account = None
        if data.dispatcher:
            account = service.dispatcher(db, org.id, data.dispatcher.id, True)
            service.expect(account, data.dispatcher.version, 'Dispatcher account')
            details_changed = (account.login_id, account.display_name) != (data.dispatcher.login_id, data.dispatcher.display_name)
            password_changed = data.dispatcher.new_password is not None
            if details_changed:
                service.apply_dispatcher_details(db, account, data.dispatcher)
            if password_changed:
                service.apply_dispatcher_password(db, actor, account, data.dispatcher.new_password)
            if details_changed or password_changed:
                service.touched(db, actor, account, 'dispatcher.updated' if details_changed else 'dispatcher.password_changed')
                if details_changed and password_changed:
                    audit(db, actor, 'dispatcher.password_changed', account.id, org.id)
        if service.apply_company_details(db, actor, org, data):
            audit(db, actor, 'organization.updated', org.id, org.id)
        if org._active != data.active:
            service.apply_company_access(db, actor, org, data.active)
        org.version += 1
        org.updated_at = now()
        db.flush()
        return {'company': queries.company_detail(db, org),
                'dispatcher': queries.dispatcher_view(db, account) if account else None}
    result = once(db, actor, key, f'platform-company-overview:{organization_id}', service.company_payload(data), run)
    return {**result, 'company': queries.include_company_details(db, organization_id, result['company'])}
