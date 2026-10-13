from typing import Annotated,Literal
from uuid import UUID
from fastapi import APIRouter,Depends,Query
from app import auth
from app.models import User
from app.routes import DB,OperationKey
from app.agents.schemas import AgentRunView
from . import commands,queries
from .schemas import IncidentPage,IncidentView,QuestionView,Reply,Acknowledge

owner_router=APIRouter(prefix='/api/v1/platform/agents/operational',tags=['Operational agent'])
company_router=APIRouter(prefix='/api/v1/companies/{slug}',tags=['Operational order monitoring'])
Owner=Annotated[User,Depends(auth.platform)]


@owner_router.get('/issues',response_model=IncidentPage)
def owner_issues(db:DB,owner:Owner,status:Literal['OPEN','ALL']='OPEN',before:UUID|None=None,limit:int=Query(20,ge=1,le=100)):
    return queries.incidents(db,owner,status,before,limit)


@owner_router.post('/issues/{identity}/acknowledge',response_model=IncidentView)
def owner_acknowledge(identity:UUID,data:Acknowledge,db:DB,key:OperationKey,owner:Owner):
    return commands.acknowledge(db,owner,identity,data,key)


@owner_router.post('/check',response_model=AgentRunView,status_code=202)
def request_review(db:DB,key:OperationKey,owner:Owner):
    return commands.request_review(db,owner,key)


@company_router.get('/operational/issues',response_model=IncidentPage)
def company_issues(slug:str,db:DB,user:Annotated[User,Depends(auth.dispatcher)],status:Literal['OPEN','ALL']='OPEN',before:UUID|None=None,limit:int=Query(20,ge=1,le=100)):
    return queries.incidents(db,user,status,before,limit)


@company_router.post('/operational/issues/{identity}/acknowledge',response_model=IncidentView)
def company_acknowledge(slug:str,identity:UUID,data:Acknowledge,db:DB,key:OperationKey,user:Annotated[User,Depends(auth.dispatcher)]):
    return commands.acknowledge(db,user,identity,data,key)


@company_router.get('/driver/operational/questions',response_model=list[QuestionView])
def driver_questions(slug:str,db:DB,user:Annotated[User,Depends(auth.driver)]):
    return queries.questions(db,user)


@company_router.post('/driver/operational/questions/{identity}/reply',response_model=QuestionView)
def driver_reply(slug:str,identity:UUID,data:Reply,db:DB,key:OperationKey,user:Annotated[User,Depends(auth.driver)]):
    return commands.reply(db,user,identity,data,key)
