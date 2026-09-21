"""Offline regressions for search failures and application editing."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

fd, db_path = tempfile.mkstemp(prefix='.test-app-', suffix='.db', dir=Path(__file__).parent)
os.close(fd)
os.environ['APPLICATIONS_DB'] = db_path
os.environ.pop('DATABASE_URL', None)
import app as appmod


class AppRegressions(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        self.env = patch.dict(os.environ, {'APIFY_TOKEN': ''})
        self.env.start()
        with appmod._db_conn() as conn:
            conn.execute('UPDATE users SET passcode_hash = NULL')
            conn.execute('DELETE FROM applications')

    def tearDown(self):
        self.env.stop()

    def search(self, **changes):
        payload = {'skills': ['Support'], 'token': 'test-secret'}
        payload.update(changes)
        return self.client.post('/api/search', json=payload)

    def test_missing_token_is_actionable_without_fetching(self):
        with patch.object(appmod, 'fetch_apify_jobs') as fetch:
            response = self.search(token='')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Setup', response.json['error'])
        fetch.assert_not_called()

    def test_bad_input_does_not_crash_or_call_provider(self):
        with patch.object(appmod, 'fetch_apify_jobs') as fetch:
            for payload in [None, [], {'skills': [42]}, {'skills': 'Support'},
                            {'skills': [' ']}, {'skills': ['Support'], 'token': 4},
                            {'skills': ['Support'], 'time_range': {}}]:
                with self.subTest(payload=payload):
                    response = self.client.post('/api/search', json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertIsNotNone(response.json)
        fetch.assert_not_called()

    def test_provider_failure_is_not_a_successful_empty_search(self):
        output = io.StringIO()
        with patch.object(appmod, 'fetch_apify_jobs', side_effect=RuntimeError(
                'Unavailable https://example.test/?token=test-secret')), contextlib.redirect_stdout(output):
            response = self.search()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('jobs', response.json)
        self.assertIn('previous results', response.json['error'])
        self.assertNotIn('test-secret', response.get_data(as_text=True))
        self.assertNotIn('test-secret', output.getvalue())

    def test_server_token_and_normalized_skills(self):
        with patch.dict(os.environ, {'APIFY_TOKEN': 'server-secret'}), \
                patch.object(appmod, 'fetch_apify_jobs', return_value=[]) as fetch:
            response = self.search(token='', skills=[' Support ', '', 'Support'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['jobs'], [])
        self.assertEqual(fetch.call_args.args[:2], (['Support'], 'server-secret'))

    def test_locked_search_does_not_spend_provider_credits(self):
        self.assertEqual(self.client.post('/api/auth/init', json={'passcode': 'testpass123'}).status_code, 200)
        self.client.post('/api/auth/logout')
        with patch.object(appmod, 'fetch_apify_jobs', return_value=[]) as fetch:
            self.assertEqual(self.search().status_code, 401)
            fetch.assert_not_called()
            self.assertEqual(self.client.post('/api/auth/login', json={'passcode': 'testpass123'}).status_code, 200)
            self.assertEqual(self.search().status_code, 200)
            fetch.assert_called_once()

    def test_application_edits_reject_bad_data_and_keep_saved_values(self):
        response = self.client.post('/api/applications', json={'company': 'Example', 'title': 'Support'})
        self.assertEqual(response.status_code, 200)
        app_id = response.json['id']
        for payload in [{'title': ' '}, {'company': None}, {'notes': 3}, [], {'status': 'Invalid'}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(f'/api/applications/{app_id}', json=payload).status_code, 400)
        saved = self.client.get('/api/applications').json['applications'][0]
        self.assertEqual((saved['company'], saved['title']), ('Example', 'Support'))
        self.assertEqual(self.client.patch(f'/api/applications/{app_id}', json={
            'title': ' IT Support ', 'notes': None, 'status': 'Interview'}).status_code, 200)
        saved = self.client.get('/api/applications').json['applications'][0]
        self.assertEqual((saved['title'], saved['status']), ('IT Support', 'Interview'))
        self.assertEqual(self.client.delete(f'/api/applications/{app_id}').status_code, 200)
        self.assertEqual(self.client.get('/api/applications').json['count'], 0)

    def test_main_screens_and_health(self):
        for url in ['/', '/applications', '/api/health', '/api/profile', '/api/daily-results',
                    '/api/autopilot/queue', '/api/cvs']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        os.unlink(db_path)
