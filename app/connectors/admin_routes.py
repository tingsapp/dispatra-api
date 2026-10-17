from typing import Annotated
from fastapi import APIRouter, Depends
from app import auth
from app.models import User
from app.routes import DB
from . import admin_queries
from .schemas import ProviderView

router=APIRouter(prefix='/api/v1/platform/integrations',tags=['Connector administration'])
Owner=Annotated[User,Depends(auth.platform)]

@router.get('/providers',response_model=list[ProviderView])
def providers(db:DB,user:Owner):return admin_queries.providers(db)
