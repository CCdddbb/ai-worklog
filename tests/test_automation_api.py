"""Authenticated automation/ledger endpoints; synthetic local runner only."""
import json
import threading
import unittest

import test_automation as fixtures
import test_product_iteration as product

from worklog.web import make_server


class AutomationApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AutomationTests()
        self.fixture.setUp()
        self.server = make_server(self.fixture.db, fixtures.DAY, 'UTC', 0, self.fixture.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.fixture.tearDown()

    def call(self, path, body=None, **kwargs):
        code, text = product.ProductIterationTests.request(self, self.server, path, body, **kwargs)
        return code, json.loads(text)

    def test_authorization_origin_and_explicit_consent_cannot_be_bypassed(self):
        body = {'enabled': True, 'scope': 'now', 'consent': True}
        self.assertEqual(self.call('/api/automation', body, auth=False)[0], 401)
        self.assertEqual(self.call('/api/automation', body, origin=False)[0], 403)
        self.assertEqual(self.call('/api/automation', dict(body, consent=False))[0], 400)
        self.assertEqual(self.call('/api/automation', dict(body, path='/private'))[0], 400)
        self.assertEqual(self.call('/api/automation', dict(body, enabled='true'))[0], 400)
        self.assertEqual(self.call('/api/automation', dict(body, scope='all-history'))[0], 400)
        code, settings = self.call('/api/automation', body)
        self.assertEqual(code, 200)
        self.assertTrue(settings['enabled'])
        self.assertEqual(self.fixture.runner.calls, [])  # A now-only grant does not send earlier logs.
        self.assertEqual(self.call('/api/automation', {'enabled': False})[0], 200)
        self.assertEqual(self.call('/api/automation-retry', {'confirm': False})[0], 400)

    def test_ledger_history_filters_and_optional_corrections_do_not_call_a_model(self):
        self.fixture.enable()
        code, value = self.call('/api/work-records?date=' + fixtures.DAY)
        self.assertEqual(code, 200)
        self.assertEqual(value['stats']['total'], 2)
        record = value['records'][0]
        self.assertEqual(self.call('/api/conversations', auth=False)[0], 401)
        code, tree = self.call('/api/conversations?date=' + fixtures.DAY)
        self.assertEqual(code, 200)
        self.assertEqual(tree['conversation_count'], 2)
        session = tree['projects'][0]['conversations'][0]['session']
        self.assertEqual(self.call('/api/conversation-progress?session=' + session)[0], 200)
        self.assertEqual(self.call('/api/conversation-progress?session=invalid')[0], 400)
        self.assertEqual(self.call('/api/work-records?date=' + fixtures.DAY, auth=False)[0], 401)
        self.assertEqual(self.call('/api/work-record-history?id=' + record['id'])[0], 200)
        self.assertEqual(self.call('/api/work-record-history?id=invalid')[0], 400)
        self.assertEqual(self.call('/api/work-record', {'id': record['id'], 'category': 'docs'}, origin=False)[0], 403)
        self.assertEqual(self.call('/api/work-record', {'id': record['id'], 'category': None})[0], 400)
        self.assertEqual(self.call('/api/work-record', {'id': record['id'], 'archived': 'false'})[0], 400)
        self.assertEqual(self.call('/api/work-record', {'id': record['id'], 'title': 'untrusted'})[0], 400)
        self.assertEqual(self.call('/api/work-record', {'id': record['id'], 'category': 'docs', 'archived': False})[0], 200)
        code, filtered = self.call('/api/work-records?date=' + fixtures.DAY + '&category=docs&state=active')
        self.assertEqual(code, 200)
        self.assertEqual(filtered['stats']['total'], 1)
        self.assertEqual(self.call('/api/work-records?date=' + fixtures.DAY + '&category=invalid')[0], 400)
        self.assertEqual(len(self.fixture.runner.calls), 2)


if __name__ == '__main__':
    unittest.main()
