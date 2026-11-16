"""Shared asynchronous HTTP fakes; never perform network requests."""

class Response:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = {"total": 0} if data is None else data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

class Session:
    def __init__(self, responses):
        self.responses = responses if callable(responses) else iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        response = self.responses(kwargs) if callable(self.responses) else next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response
