"""Platform-owner read projections, always filtered by explicit organization IDs."""
from sqlalchemy import select, func, or_, tuple_, case
from app.models import Organization, User, LoginSession, AuditEvent, now
from .schemas import CompanySummary, CompanyDetail, DispatcherView, AuditEntry

AUDITED = ('organization.%', 'dispatcher.%')


def _role_count(role, active_only=False):
    query = select(func.count()).select_from(User).where(User.organization_id == Organization.id, User.role == role)
    if active_only: query = query.where(User.active.is_(True))
    return query.correlate(Organization).scalar_subquery()


def _escape(text):
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def companies(db, search=None, status=None, after=None, limit=50):
    query = select(Organization, _role_count('DISPATCHER').label('dispatchers'), _role_count('DISPATCHER', True).label('active_dispatchers'))
    if search:
        pattern = f'%{_escape(search.strip().lower())}%'
        query = query.where(or_(func.lower(Organization.name).like(pattern, escape='\\'), Organization.slug.like(pattern, escape='\\')))
    if status: query = query.where(Organization.active.is_(status == 'ACTIVE'))
    if after:
        anchor = db.execute(select(func.lower(Organization.name), Organization.id).where(Organization.id == after)).first()
        if anchor: query = query.where(tuple_(func.lower(Organization.name), Organization.id) > tuple(anchor))
    query = query.order_by(func.lower(Organization.name), Organization.id).limit(limit)
    return [CompanySummary.model_validate({**_org(org), 'dispatcher_count': total, 'active_dispatcher_count': active})
            for org, total, active in db.execute(query)]


def _org(org):
    return {key: getattr(org, key) for key in ('id', 'slug', 'name', 'active', 'version', 'created_at', 'updated_at')}


def company_detail(db, org):
    roles = dict(db.execute(select(User.role, func.count()).where(User.organization_id == org.id).group_by(User.role)).all())
    active = db.scalar(select(func.count()).select_from(User).where(User.organization_id == org.id, User.role == 'DISPATCHER', User.active.is_(True)))
    sessions = db.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.organization_id == org.id, LoginSession.expires_at > now()))
    return CompanyDetail.model_validate({**_org(org), 'dispatcher_count': roles.get('DISPATCHER', 0), 'active_dispatcher_count': active,
        'shipper_account_count': roles.get('SHIPPER', 0), 'driver_account_count': roles.get('DRIVER', 0),
        'active_session_count': sessions}).model_dump(mode='json')


def _sessions():
    return select(func.count()).select_from(LoginSession).where(LoginSession.user_id == User.id, LoginSession.expires_at > now()).correlate(User).scalar_subquery()


def dispatcher_view(db, account):
    sessions = db.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id == account.id, LoginSession.expires_at > now()))
    return DispatcherView.model_validate({**_account(account), 'active_session_count': sessions}).model_dump(mode='json')


def _account(account):
    return {key: getattr(account, key) for key in ('id', 'login_id', 'display_name', 'active', 'version', 'created_at', 'updated_at', 'last_login_at')}


def dispatchers(db, organization_id):
    query = (select(User, _sessions()).where(User.organization_id == organization_id, User.role == 'DISPATCHER')
             .order_by(case((User.active.is_(True), 0), else_=1), User.created_at, User.id))
    return [DispatcherView.model_validate({**_account(account), 'active_session_count': sessions}) for account, sessions in db.execute(query)]


def audit_history(db, organization_id, limit=50):
    query = (select(AuditEvent, User.login_id, User.role).join(User, User.id == AuditEvent.actor_id)
             .where(AuditEvent.organization_id == organization_id, or_(*[AuditEvent.action.like(prefix) for prefix in AUDITED]))
             .order_by(AuditEvent.created_at.desc(), AuditEvent.id).limit(limit))
    return [AuditEntry(id=event.id, action=event.action, entity_id=event.entity_id, actor_login_id=login, actor_role=role, created_at=event.created_at)
            for event, login, role in db.execute(query)]
