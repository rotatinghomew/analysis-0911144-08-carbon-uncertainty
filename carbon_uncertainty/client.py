import json
from urllib.request import Request, urlopen


class Client:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip('/')

    def request(self, path, body=None):
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        request = Request(self.base_url + path, data=encoded, headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def import_data(self, data, request_key):
        return self.request('/imports', {'data': data, 'request_key': request_key})

    def summary(self, revision=None):
        return self.request('/summary' + ('' if revision is None else f'?revision={revision}'))

    def uncertainty(self, revision=None):
        return self.request('/uncertainty' + ('' if revision is None else f'?revision={revision}'))

    def comparison(self, base_revision=None, target_revision=None):
        query = []
        if base_revision is not None:
            query.append(f'from={base_revision}')
        if target_revision is not None:
            query.append(f'to={target_revision}')
        suffix = ('?' + '&'.join(query)) if query else ''
        return self.request('/comparison' + suffix)
