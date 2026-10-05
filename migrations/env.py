import os
from alembic import context
from sqlalchemy import create_engine
from app.models import Base
from app.operations import models as operational_models
from app.intake import models as intake_models
engine = create_engine(os.environ['MIGRATION_DATABASE_URL'])
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction(): context.run_migrations()
