import os
from uuid import uuid4
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from .routes import router
from .database import engine
from .schemas import ErrorEnvelope

app = FastAPI(title='Dispatra API', version='0.2.0', responses={
    status: {'model': ErrorEnvelope} for status in [400,401,403,404,409,422,429,503]
})
origins = {origin.strip() for origin in os.environ.get('WEB_ORIGINS', 'http://localhost:3000,http://127.0.0.1:3000').split(',') if origin.strip()}
origins.add('https://dispatra.vercel.app')

def error(request, status, message, fields=None):
    return JSONResponse(status_code=status, content={'error': {
        'code': {401:'UNAUTHENTICATED',403:'FORBIDDEN',404:'NOT_FOUND',409:'CONFLICT',422:'VALIDATION',429:'RATE_LIMITED',503:'UNAVAILABLE'}.get(status,'BAD_REQUEST'),
        'message': message, 'details': {}, 'field_errors': fields or [], 'retryable': status in [429,503],
        'request_id': getattr(request.state, 'request_id', str(uuid4()))}})

@app.middleware('http')
async def boundary(request: Request, call_next):
    request.state.request_id = str(uuid4())
    native_request = (not request.headers.get('origin') and not request.cookies.get('dispatra_session')
        and request.headers.get('x-requested-with') == 'Dispatra'
        and (request.url.path == '/api/v1/auth/driver/login'
             or request.headers.get('authorization', '').startswith('Bearer dm_')))
    if request.method not in {'GET','HEAD','OPTIONS'} and not native_request and (
        request.headers.get('origin') not in origins or request.headers.get('x-requested-with') != 'Dispatra'
    ):
        response = error(request, 403, 'Request origin could not be verified.')
    elif not request.headers.get('content-length', '0').isdigit() or int(request.headers.get('content-length', '0')) > (3_000_000 if request.url.path.endswith('/evidence') else 262144):
        response = error(request, 413, 'Request is too large.')
    else:
        response = await call_next(request)
    response.headers.update({'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
                             'X-Request-ID':request.state.request_id})
    return response

@app.exception_handler(HTTPException)
async def http_error(request, exc): return error(request, exc.status_code, str(exc.detail))

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Never echo submitted passwords or other input values in validation responses.
    fields = [{'field': '.'.join(map(str,e['loc'])), 'message': e['msg']} for e in exc.errors()]
    return error(request, 422, 'Check the entered information.', fields)

@app.exception_handler(IntegrityError)
async def conflict(request, exc): return error(request, 409, 'A record with those identifiers already exists, or a relationship is invalid.')

@app.exception_handler(SQLAlchemyError)
async def unavailable(request, exc): return error(request, 503, 'Database unavailable. Please try again.')

@app.get('/')
def root() -> dict[str,str]: return {'message':'Welcome to the Dispatra API'}

@app.get('/health')
def health() -> dict[str,str]: return {'status':'ok'}

@app.get('/ready')
def ready() -> dict[str,str]:
    with engine.connect() as connection:
        role = connection.execute(text('SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user')).one()
        if role.rolsuper or role.rolbypassrls: raise HTTPException(503, 'Runtime database role is not restricted.')
        connection.execute(text('SELECT id FROM organizations LIMIT 0'))
        connection.execute(text('SELECT id, pricing FROM orders LIMIT 0'))
        connection.execute(text('SELECT id, arrived_at FROM route_stops LIMIT 0'))
        connection.execute(text('SELECT id, snapshot FROM invoices LIMIT 0'))
        connection.execute(text('SELECT id, status FROM email_deliveries LIMIT 0'))
        connection.execute(text('SELECT seq, tx FROM events LIMIT 0'))
    return {'status':'ready'}

app.include_router(router)
from .operations.routes_directory import router as directory_router
from .operations.routes_orders import router as orders_router
from .operations.routes_driver import router as driver_router
from .operations.reporting import router as reporting_router
for operational_router in [directory_router, orders_router, driver_router, reporting_router]:
    app.include_router(operational_router)

from .platform.routes import router as platform_router
app.include_router(platform_router)
from .events.routes import router as events_router
app.include_router(events_router)
from .intake.routes import router as intake_router
app.include_router(intake_router)
from .dispatch.routes import router as dispatch_router
app.include_router(dispatch_router)
