import copy
import http.client
import importlib.util
import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
import canvas_api
import fetch
import ics
import netutil
import notify
import store
import timeutil
import widget

FEED = '''BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:assignment-12
DTSTART:20261005T130000Z
SUMMARY:Homework [COMP10001]
URL:https://canvas.example.edu/courses/42/assignments/12
END:VEVENT
END:VCALENDAR
'''


class TemporaryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        paths = {
            'ROOT': root, 'STATE_DIR': root / 'state', 'OUT_DIR': root / 'out',
            'CONFIG_FILE': root / 'config.json', 'MARKED_FILE': root / 'state/marked.json',
            'NOTIFIED_FILE': root / 'state/notified.json', 'TOKEN_STATE_FILE': root / 'state/token.json',
            'DEADLINES_FILE': root / 'out/deadlines.json', 'HTML_FILE': root / 'out/deadlines.html',
            'TEXT_FILE': root / 'out/deadlines.txt', 'LOG_FILE': root / 'out/fetch_log.txt',
            'FEED_DEBUG_FILE': root / 'state/feed_debug.txt', 'WIDGET_STATE_FILE': root / 'state/widget.json',
        }
        self.patcher = patch.multiple(store, **paths)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.cfg = copy.deepcopy(store.DEFAULT_CONFIG)
        self.cfg['output'].update(write_html=False, write_text=False)
        self.cfg['canvas']['calendar_feed_url'] = 'https://canvas.example.edu/feeds/calendars/user_TEST_ONLY.ics'
        self.cfg['display']['notify_hours'] = 0


class StoreTests(TemporaryStore):
    def test_parallel_writes_do_not_collide(self):
        path = store.ROOT / 'concurrent.json'
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: store.write_json(path, {'n': n, 'text': '中' * 1000}), range(80)))
        self.assertIn(json.loads(path.read_text(encoding='utf-8'))['n'], range(80))
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_failed_replace_preserves_old_file_and_removes_temp(self):
        store.write_json(store.CONFIG_FILE, {'old': True})
        with patch('store.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                store.write_json(store.CONFIG_FILE, {'new': True})
        self.assertEqual(store.read_json(store.CONFIG_FILE), {'old': True})
        self.assertEqual(list(store.ROOT.glob('*.tmp')), [])

    def test_config_non_object_rejected(self):
        store.write_json(store.CONFIG_FILE, ['bad'])
        with self.assertRaisesRegex(ValueError, 'JSON'):
            store.load_config()

    def test_config_non_object_section_rejected(self):
        store.write_json(store.CONFIG_FILE, {'canvas': 'bad'})
        with self.assertRaisesRegex(ValueError, 'canvas'):
            store.load_config()

    def test_feed_and_query_credentials_redacted_from_log(self):
        store.append_log(['https://example.test/feeds/calendars/user_TEST_ONLY.ics',
                          'access_token=QUERY_TEST Bearer HEADER_TEST'])
        text = store.LOG_FILE.read_text(encoding='utf-8')
        for secret in ('user_TEST_ONLY', 'QUERY_TEST', 'HEADER_TEST'):
            self.assertNotIn(secret, text)

    def test_webcal_normalized(self):
        self.cfg['canvas']['calendar_feed_url'] = 'webcal://canvas.example.edu/test.ics'
        self.assertEqual(store.get_feed_url(self.cfg), 'https://canvas.example.edu/test.ics')


class NetworkTests(unittest.TestCase):
    def test_body_read_timeout_is_not_a_success(self):
        resp = Mock(headers={})
        resp.read.side_effect = socket.timeout('interrupted')
        with self.assertRaises(socket.timeout):
            netutil._read(resp)

    def test_query_goes_before_fragment(self):
        result = netutil.build_url('https://example.test/a?q=1#part', {'page': 2})
        self.assertEqual(result, 'https://example.test/a?q=1&page=2#part')

    def test_authenticated_redirect_to_other_host_rejected(self):
        req = urllib.request.Request('https://canvas.example.edu/api', headers={'Authorization': 'Bearer TEST'})
        with self.assertRaises(netutil.NetworkFailure):
            netutil.SafeRedirectHandler().redirect_request(req, None, 302, '', {}, 'https://other.test/a')

    def test_same_origin_redirect_allowed(self):
        req = urllib.request.Request('https://canvas.example.edu/a', headers={'Authorization': 'Bearer TEST'})
        result = netutil.SafeRedirectHandler().redirect_request(req, None, 302, '', {}, 'https://canvas.example.edu/b')
        self.assertEqual(result.get_header('Authorization'), 'Bearer TEST')

    def test_https_downgrade_rejected(self):
        req = urllib.request.Request('https://canvas.example.edu/calendar.ics')
        with self.assertRaises(netutil.TLSFailure):
            netutil.SafeRedirectHandler().redirect_request(req, None, 302, '', {}, 'http://canvas.example.edu/a')


class CalendarTests(TemporaryStore):
    def test_complete_calendar_download(self):
        with patch('netutil.get', return_value=netutil.Response(200, {}, FEED.encode())):
            self.assertEqual(fetch.fetch_feed('https://canvas.example.edu/test.ics', self.cfg), FEED)

    def test_truncated_calendar_download_rejected(self):
        truncated = FEED.replace('END:VCALENDAR', '')
        with patch('netutil.get', return_value=netutil.Response(200, {}, truncated.encode())):
            with self.assertRaises(canvas_api.CanvasError):
                fetch.fetch_feed('https://canvas.example.edu/test.ics', self.cfg)

    def test_valid_empty_calendar_allowed(self):
        text = 'BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n'
        with patch('netutil.get', return_value=netutil.Response(200, {}, text.encode())):
            self.assertEqual(fetch.fetch_feed('https://canvas.example.edu/test.ics', self.cfg), text)

    def test_cancelled_events_not_shown(self):
        text = FEED.replace('SUMMARY:', 'STATUS:CANCELLED\nSUMMARY:')
        self.assertEqual(ics.to_tasks(ics.parse_events(text), timeutil.get_zone('Australia/Melbourne')), [])

    def test_dst_transition_is_correct(self):
        zone = timeutil.get_zone('Australia/Melbourne')
        before = timeutil.parse_iso('2026-10-03T15:59:59Z').astimezone(zone)
        after = timeutil.parse_iso('2026-10-03T16:00:00Z').astimezone(zone)
        self.assertEqual(before.utcoffset(), timedelta(hours=10))
        self.assertEqual(after.utcoffset(), timedelta(hours=11))
        self.assertEqual((before.hour, after.hour), (1, 3))

    def test_missing_timezones_fail_instead_of_silent_utc(self):
        with patch('timeutil.ZoneInfo', side_effect=timeutil.ZoneInfoNotFoundError('missing')):
            with self.assertRaisesRegex(ValueError, 'tzdata'):
                timeutil.get_zone('Australia/Melbourne')

    def test_zero_past_days_respected(self):
        now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        self.cfg['display']['past_days'] = 0
        task = {'key': 'old', 'due_utc': (now - timedelta(days=1)).isoformat(), 'type': 'assignment', 'submitted': False}
        with patch('timeutil.now_utc', return_value=now):
            dated, _, _ = fetch._postprocess([task], self.cfg, timeutil.get_zone('Australia/Melbourne'), {})
        self.assertEqual(dated, [])

    def test_failure_preserves_cached_tasks(self):
        old = {'fetch': {'ok': True, 'source': 'calendar_feed'}, 'tasks': [{'key': 'keep-me'}], 'undated': []}
        store.write_json(store.DEADLINES_FILE, old)
        fetch._write_failure(self.cfg, 'offline', 'network', lambda message: None)
        new = store.read_json(store.DEADLINES_FILE)
        self.assertEqual(new['tasks'], old['tasks'])
        self.assertFalse(new['fetch']['ok'])

    def test_unexpected_fetch_crash_sets_error_and_keeps_cache(self):
        store.write_json(store.CONFIG_FILE, self.cfg)
        store.write_json(store.DEADLINES_FILE, {'tasks': [{'key': 'keep-me'}], 'fetch': {'ok': True}})
        with patch('fetch.collect_from_feed', side_effect=RuntimeError('unexpected')), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(app._cmd_fetch(['--quiet']), 3)
        new = store.read_json(store.DEADLINES_FILE)
        self.assertEqual(new['tasks'], [{'key': 'keep-me'}])
        self.assertEqual(new['fetch']['error_kind'], 'crash')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = canvas_api.CanvasClient('https://canvas.example.edu', 'TEST_TOKEN')

    def test_pagination_limit_is_error_not_partial_success(self):
        response = netutil.Response(200, {'Link': '<https://canvas.example.edu/api/v1/courses?page=2>; rel="next"'}, b'[{"id":1}]')
        with patch.object(self.client, '_request', return_value=response):
            with self.assertRaises(canvas_api.CanvasError):
                self.client.get_all('courses', max_pages=1)

    def test_exactly_full_final_page_succeeds(self):
        response = netutil.Response(200, {}, b'[{"id":1}]')
        with patch.object(self.client, '_request', return_value=response):
            self.assertEqual(self.client.get_all('courses', max_pages=1), [{'id': 1}])

    def test_external_pagination_never_receives_token(self):
        with patch('netutil.get') as get:
            with self.assertRaises(canvas_api.CanvasError):
                self.client._request('https://other.test/api/v1/courses')
            get.assert_not_called()

    def test_assignments_include_past_and_undated(self):
        with patch.object(self.client, 'get_all', return_value=[]) as get:
            self.client.course_assignments(42)
            self.assertNotIn('bucket', get.call_args.args[1])


class NotificationTests(TemporaryStore):
    def task(self, **changes):
        task = {'key': 'task', 'title': 'Homework', 'course_code': 'TEST10001',
                'due_utc': (timeutil.now_utc() + timedelta(hours=1)).isoformat(),
                'source': 'feed', 'submitted': None, 'needs_action': True}
        task.update(changes)
        return task

    def test_feed_reminder_states_unknown_submission(self):
        with patch('notify.notify', return_value=(True, 'mock')) as send:
            notify.notify_urgent([self.task()], 48)
            send.assert_called_once()
            self.assertIn('无法确认是否已交', send.call_args.args[1])

    def test_completed_feed_task_not_notified(self):
        with patch('notify.notify') as send:
            notify.notify_urgent([self.task(submitted=True, needs_action=False)], 48)
            send.assert_not_called()

    def test_unknown_api_task_not_notified(self):
        with patch('notify.notify') as send:
            notify.notify_urgent([self.task(source='planner')], 48)
            send.assert_not_called()

    def test_same_task_only_once_per_day(self):
        with patch('notify.notify', return_value=(True, 'mock')) as send:
            notify.notify_urgent([self.task()], 48)
            notify.notify_urgent([self.task()], 48)
            send.assert_called_once()


class WidgetTests(unittest.TestCase):
    def test_dead_fetch_process_releases_refresh_button(self):
        window = widget.Widget.__new__(widget.Widget)
        window.root = Mock()
        window.reload = Mock()
        window._render_status = Mock()
        window._fetch_process = Mock()
        window._fetch_process.poll.return_value = 3
        window.fetching = True
        window.mtime = 1
        window._fetch_initial_mtime = 1
        window._fetch_error = None
        window.payload = {'fetch': {'ok': True}}
        window._poll_file()
        self.assertFalse(window.fetching)
        self.assertIsNone(window._fetch_process)
        self.assertIsNotNone(window._fetch_error)


class PackagingTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / '打包Mac版.py').exists(), 'Mac packaging is not part of this release')
    def test_mac_package_never_collects_personal_config(self):
        spec = importlib.util.spec_from_file_location('package_mac', ROOT / '打包Mac版.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('config.json', 'config.json.bak', 'private.json', 'README.md', 'widget.py', 'config.example.json'):
                (root / name).write_text('{}')
            (root / 'state').mkdir()
            (root / 'state/marked.json').write_text('{}')
            with patch.object(module, 'ROOT', root):
                collected = {p.as_posix() for p in module.collect()}
        self.assertEqual(collected, {'README.md', 'widget.py', 'config.example.json'})


if __name__ == '__main__':
    unittest.main()
