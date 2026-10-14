"""Pure ASGI checks; run directly without a database fixture."""
import asyncio
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.connectors.privacy import CallbackPrivacyMiddleware

class CallbackPrivacyTests(unittest.TestCase):
    def test_callback_parameters_reach_handler_but_not_response_logging(self):
        scope={'type':'http','path':'/api/v1/companies/acme/integrations/connectors/rose-rocket/callback','query_string':b'code=private-code&state=private-state'}
        seen=[]
        async def handler(scope,receive,send):
            self.assertEqual(scope['query_string'],b'code=private-code&state=private-state')
            await send({'type':'http.response.start','status':303,'headers':[]})
        async def send(message):seen.append(scope['query_string'])
        asyncio.run(CallbackPrivacyMiddleware(handler)(scope,None,send))
        self.assertEqual(seen,[b'']);self.assertEqual(scope['query_string'],b'')

    def test_failure_also_removes_callback_query(self):
        scope={'type':'http','path':'/api/v1/companies/acme/integrations/connectors/cartoncloud/callback','query_string':b'code=private'}
        async def handler(*args):raise ValueError('controlled failure')
        with self.assertRaises(ValueError):asyncio.run(CallbackPrivacyMiddleware(handler)(scope,None,None))
        self.assertEqual(scope['query_string'],b'')

    def test_existing_integration_requests_are_untouched(self):
        scope={'type':'http','path':'/api/v1/companies/acme/integrations/tms/inbox','query_string':b'status=open'}
        async def handler(scope,*args):self.assertEqual(scope['query_string'],b'status=open')
        asyncio.run(CallbackPrivacyMiddleware(handler)(scope,None,None));self.assertEqual(scope['query_string'],b'status=open')

if __name__=='__main__':unittest.main()
