"""Remove OAuth callback query material before the server's response/access logging."""
import re
CALLBACK=re.compile(r'^/api/v1/companies/[a-z0-9-]{1,63}/integrations/connectors/[a-z0-9-]+/callback$')

class CallbackPrivacyMiddleware:
    def __init__(self,app):self.app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or not CALLBACK.fullmatch(scope.get('path','')):
            return await self.app(scope,receive,send)
        async def private_send(message):
            if message['type']=='http.response.start':scope['query_string']=b''
            await send(message)
        try:await self.app(scope,receive,private_send)
        finally:scope['query_string']=b''
