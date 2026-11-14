import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError

from carbon_uncertainty.client import Client
from carbon_uncertainty.domain import records, summarize, validate
from carbon_uncertainty.repository import Conflict, Repository
from carbon_uncertainty.service import make_server


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / 'data.sqlite3'
        self.repository = Repository(self.database)
        self.data = json.loads(Path('fixtures/demo.json').read_text())

    def tearDown(self):
        self.directory.cleanup()

    def test_fixture_and_summary(self):
        validate(self.data)
        self.assertIsInstance(summarize(self.data), dict)
        self.assertGreater(len(records(self.data)), 0)

    def test_replay_and_conflict(self):
        receipt = self.repository.import_data(self.data, 'first')
        replay = self.repository.import_data(self.data, 'first')
        self.assertEqual(receipt['revision'], replay['revision'])
        self.assertTrue(replay['replayed'])
        changed = copy.deepcopy(self.data)
        changed['note'] = '已复核'
        with self.assertRaises(Conflict):
            self.repository.import_data(changed, 'first')
        self.assertEqual(len(self.repository.history()), 1)

    def test_old_revision_and_restart(self):
        first = self.repository.import_data(self.data, 'a')
        changed = copy.deepcopy(self.data)
        changed['note'] = '第二版资料'
        second = self.repository.import_data(changed, 'b')
        restarted = Repository(self.database)
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(restarted.get(first['revision'])['data'], self.data)
        self.assertEqual(restarted.get()['data'], changed)

    def test_digest_ignores_object_key_order(self):
        first = self.repository.import_data(self.data, 'a')
        reordered = dict(reversed(list(self.data.items())))
        second = self.repository.import_data(reordered, 'b')
        self.assertEqual(first['digest'], second['digest'])
        self.assertEqual(first['revision'], second['revision'])

    def test_invalid_import_is_atomic(self):
        with self.assertRaises(ValueError):
            self.repository.import_data({}, 'broken')
        self.assertEqual(self.repository.history(), [])
        self.assertEqual(self.repository.import_data(self.data, 'broken')['revision'], 1)

    def test_no_revision(self):
        with self.assertRaises(LookupError):
            self.repository.get(999)

    def test_input_must_not_be_list(self):
        with self.assertRaises(ValueError):
            self.repository.import_data([], 'invalid')

    def test_empty_request_key(self):
        with self.assertRaises(ValueError):
            self.repository.import_data(self.data, '')

    def test_http_roundtrip_and_failures(self):
        server = make_server(self.repository)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = Client(f'http://127.0.0.1:{server.server_port}')
        try:
            self.assertEqual(client.request('/health')['status'], 'ok')
            with self.assertRaises(HTTPError) as caught:
                client.summary()
            self.assertEqual(caught.exception.code, 404)
            receipt = client.import_data(self.data, 'roundtrip')
            result = client.summary(receipt['revision'])
            self.assertEqual(result['digest'], receipt['digest'])
            self.assertEqual(result['result'], summarize(self.data))
            self.assertEqual(client.request('/records')['result'], records(self.data))
            self.assertEqual(len(client.request('/revisions')['revisions']), 1)
            with self.assertRaises(HTTPError) as caught:
                client.request('/summary?revision=wrong')
            self.assertEqual(caught.exception.code, 400)
            with self.assertRaises(HTTPError) as caught:
                client.import_data({}, 'invalid')
            self.assertEqual(caught.exception.code, 400)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_units_and_allocation(self):
        from carbon_uncertainty.domain import convert
        self.assertEqual(convert(1, 'MWh', 'kWh'), 1000)
        self.assertEqual(summarize(self.data)['known_kg'], 100)
        self.assertEqual(summarize(self.data)['products_kg']['p1'], 55)

    def test_missing_not_zero_total(self):
        self.data['activities'][0]['factor'] = 'unavailable'
        result = summarize(self.data)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing_factor_activities'], ['a1'])

    def test_allocation_conservation(self):
        self.data['activities'][0]['allocation']['p1'] = 1
        with self.assertRaises(ValueError):
            validate(self.data)

    def test_dimension_mismatch(self):
        self.data['activities'][0]['unit'] = 'L'
        with self.assertRaises(ValueError):
            validate(self.data)
