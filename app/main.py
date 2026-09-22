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

app = FastAPI(title='Dispatra API', version='0.1.0', responses={
    status: {'model': ErrorEnvelope} for status in [400,401,403,404,409,422,429,503]
})
origins = set(os.environ.get('WEB_ORIGINS', 'http://localhost:3000,http://127.0.0.1:3000').split(','))

def error(request, status, message, fields=None):
    return JSONResponse(status_code=status, content={'error': {
        'code': {401:'UNAUTHENTICATED',403:'FORBIDDEN',404:'NOT_FOUND',409:'CONFLICT',422:'VALIDATION',429:'RATE_LIMITED',503:'UNAVAILABLE'}.get(status,'BAD_REQUEST'),
        'message': message, 'details': {}, 'field_errors': fields or [], 'retryable': status in [429,503],
        'request_id': getattr(request.state, 'request_id', str(uuid4()))}})

@app.middleware('http')
async def boundary(request: Request, call_next):
    request.state.request_id = str(uuid4())
    if request.method not in {'GET','HEAD','OPTIONS'} and (
        request.headers.get('origin') not in origins or request.headers.get('x-requested-with') != 'Dispatra'
    ):
        response = error(request, 403, 'Request origin could not be verified.')
    elif not request.headers.get('content-length', '0').isdigit() or int(request.headers.get('content-length', '0')) > 32768:
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
    return {'status':'ready'}

app.include_router(router)
