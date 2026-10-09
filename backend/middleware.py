from starlette.responses import JSONResponse

class BodyLimitMiddleware:
    """Bound the entire request before multipart parsing creates temporary files."""
    def __init__(self, app, limit=11 * 1024 * 1024):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH'):
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > self.limit:
                return await JSONResponse({'code':'REQUEST_TOO_LARGE','detail': 'Request exceeds the 11 MB transport limit.'}, status_code=413)(scope, receive, send)
            chunks.append(message.get('body', b''))
            if not message.get('more_body', False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()

        await self.app(scope, bounded_receive, send)
