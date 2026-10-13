from fastapi import HTTPException
from sqlalchemy import select,tuple_
from app.models import Organization
from app.operations.models import Order,Route
from .models import Incident,Question
from .commands import OPEN,question_view
from .schemas import CHOICES


def incident_view(db,row):
    order=db.get(Order,row.order_id)
    q=db.scalar(select(Question).where(Question.organization_id==row.organization_id,Question.incident_id==row.id))
    fields=('id','version','organization_id','order_id','stop_id','kind','title','description','severity','certainty','status',
        'evidence','suggestions','response_due_at','acknowledged_at','resolved_at','resolution','created_at','updated_at')
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder({**{field:getattr(row,field) for field in fields},
        'company_name':db.scalar(select(Organization.name).where(Organization.id==row.organization_id)),
        'order_number':order.number if order else '',
        'driver_reply':q.details or next((c['label'] for c in CHOICES if c['value']==q.reason),'') if q and q.status=='ANSWERED' else None})


def incidents(db,actor,status,before,limit):
    query=select(Incident)
    if actor.role!='ADMIN':query=query.where(Incident.organization_id==actor.organization_id)
    if status=='OPEN':query=query.where(Incident.status.in_(OPEN))
    if before:
        anchor=db.scalar(query.where(Incident.id==before))
        if anchor is None:raise HTTPException(404,'Operational issue not found.')
        query=query.where(tuple_(Incident.created_at,Incident.id)<(anchor.created_at,anchor.id))
    rows=db.scalars(query.order_by(Incident.created_at.desc(),Incident.id.desc()).limit(limit+1)).all()
    return dict(items=[incident_view(db,row) for row in rows[:limit]],next_before=rows[limit-1].id if len(rows)>limit else None)


def questions(db,actor):
    query=(select(Question).join(Order,(Order.id==Question.order_id)&(Order.organization_id==Question.organization_id))
        .join(Route,(Route.id==Question.route_id)&(Route.organization_id==Question.organization_id))
        .where(Question.organization_id==actor.organization_id,Question.driver_id==actor.driver_id,Question.status=='OPEN',
            Route.driver_id==actor.driver_id,Route.generation==Question.generation,Order.route_id==Route.id,
            Order.status.in_(('ASSIGNED','IN_PROGRESS')),Route.status.in_(('PLANNED','IN_PROGRESS')))
        .order_by(Question.created_at).limit(100))
    return [question_view(db,q) for q in db.scalars(query)]
