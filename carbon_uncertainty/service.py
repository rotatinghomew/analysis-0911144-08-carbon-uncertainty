import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .comparison import compare_revisions
from .domain import records, summarize
from .repository import Conflict
from .uncertainty import analyze_uncertainty

SNAPSHOT_PATHS = {
    '/records': records,
    '/summary': summarize,
    '/uncertainty': analyze_uncertainty,
}


def make_server(repository, host='127.0.0.1', port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return

        def send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.path == '/health':
                    return self.send_json(200, {'status': 'ok'})
                if parsed.path == '/revisions':
                    return self.send_json(200, {'revisions': repository.history()})
                query = parse_qs(parsed.query)
                if parsed.path == '/comparison':
                    # 两个不可变版本之间的比较；缺省基准为上一版本，目标为最新版本
                    target_snapshot = repository.get(
                        int(query['to'][0]) if 'to' in query else None
                    )
                    if 'from' in query:
                        base_snapshot = repository.get(int(query['from'][0]))
                    else:
                        history = repository.history()
                        if len(history) < 2:
                            raise ValueError('需要至少两个资料版本才能比较，或显式提供 from')
                        base_snapshot = repository.get(history[-2]['revision'])
                    result = compare_revisions(base_snapshot['data'], target_snapshot['data'])
                    return self.send_json(200, {
                        'base_revision': base_snapshot['revision'],
                        'base_digest': base_snapshot['digest'],
                        'target_revision': target_snapshot['revision'],
                        'target_digest': target_snapshot['digest'],
                        'result': result,
                    })
                if parsed.path not in SNAPSHOT_PATHS:
                    return self.send_json(404, {'error': '接口不存在'})
                revision = int(query['revision'][0]) if 'revision' in query else None
                snapshot = repository.get(revision)
                result = SNAPSHOT_PATHS[parsed.path](snapshot['data'])
                self.send_json(200, {'revision': snapshot['revision'], 'digest': snapshot['digest'], 'result': result})
            except LookupError as error:
                self.send_json(404, {'error': str(error)})
            except ValueError as error:
                self.send_json(400, {'error': str(error)})

        def do_POST(self):
            if urlsplit(self.path).path != '/imports':
                return self.send_json(404, {'error': '接口不存在'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4_000_000:
                    raise ValueError('请求正文大小不合法')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('请求必须是对象')
                result = repository.import_data(payload['data'], payload['request_key'])
                self.send_json(200 if result['replayed'] else 201, result)
            except Conflict as error:
                self.send_json(409, {'error': str(error)})
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                self.send_json(400, {'error': str(error)})

    return ThreadingHTTPServer((host, port), Handler)
