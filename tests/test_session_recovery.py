"""Use real recovery methods with fake platform responses; never log into a real account."""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from multidict import CIMultiDict

from XianyuAutoAsync import XianyuLive, ConnectionState
from utils import browser_limit, risk_control
from utils.auth_errors import is_session_expired, is_token_expired
from utils.xianyu_seller_api import XianyuSellerAPI, SellerApiError


COOKIES = 'unb=123; cookie2=session; _m_h5_tk=old_9999999999999'


class BrowserCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_does_not_steal_later_slot(self):
        sem = threading.Semaphore(0)
        with patch.object(browser_limit, '_get_semaphore', return_value=sem):
            task = asyncio.create_task(browser_limit.launch_browser(None))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            sem.release()
            await asyncio.sleep(0.12)
        self.assertTrue(sem.acquire(blocking=False))
        self.assertFalse(sem.acquire(blocking=False), 'a cancelled waiter must not over-release')

    async def test_cancelled_launch_returns_slot_exactly_once(self):
        sem = threading.Semaphore(1)
        entered = asyncio.Event()

        async def launch(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        with patch.object(browser_limit, '_get_semaphore', return_value=sem):
            task = asyncio.create_task(browser_limit.launch_browser(playwright))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(sem.acquire(blocking=False))
        self.assertFalse(sem.acquire(blocking=False))

    async def test_queue_timeout_does_not_touch_occupied_slot(self):
        sem = threading.Semaphore(0)
        with patch.object(browser_limit, '_get_semaphore', return_value=sem), \
                patch.object(browser_limit, '_ACQUIRE_TIMEOUT', 0.01):
            with self.assertRaises(TimeoutError):
                await browser_limit.launch_browser(None)
        self.assertFalse(sem.acquire(blocking=False))

    async def test_close_failure_releases_slot_only_once(self):
        sem = threading.Semaphore(0)
        browser = browser_limit.LimitedBrowser(SimpleNamespace(close=AsyncMock(side_effect=RuntimeError)), sem, 'test')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await browser.close()
        self.assertTrue(sem.acquire(blocking=False))
        self.assertFalse(sem.acquire(blocking=False))


class CrossLoopRateLimitTests(unittest.TestCase):
    def test_one_account_can_wait_on_two_event_loops(self):
        guard = risk_control.AccountGuard('test', rate_per_minute=6000)
        guard._tokens = 0
        barrier = threading.Barrier(2)

        def run():
            barrier.wait(timeout=2)
            async def acquire_twice():
                await guard.acquire()
                await guard.acquire()
            asyncio.run(acquire_twice())

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            for future in futures:
                future.result(timeout=3)


class Response:
    def __init__(self, result, cookies=None):
        self.result = result
        self.status = 200
        self.headers = CIMultiDict({'content-type': 'application/json'})
        for cookie in cookies or []:
            self.headers.add('set-cookie', cookie)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.result


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def close(self):
        self.closed = True

    def post(self, *args, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def success():
    return Response({'ret': ['SUCCESS::调用成功'], 'data': {
        'accessToken': 'valid-token', 'accessTokenExpiredTime': (time.time() + 600) * 1000,
    }})


class LiveSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [patch.object(XianyuLive, '_init_order_status_handler'),
                        patch('app.db_manager.db_manager'),
                        patch.object(risk_control, 'registry', risk_control.RiskControlRegistry(6000)),
                        patch('app.cookie_manager.manager', None)]
        for context in self.patches:
            context.start()
        from app.db_manager import db_manager
        self.db = db_manager
        self.db.get_cookie_details.return_value = {'value': COOKIES, 'username': '', 'password': ''}
        def persist(_cookie_id, expected_value, new_value):
            self.db.get_cookie_details.return_value['value'] = new_value
            return True
        self.db.refresh_cookie_value.side_effect = persist
        self.live = XianyuLive(COOKIES, '123', 1)
        self.live.ws = SimpleNamespace(closed=False, close=AsyncMock(), send=AsyncMock())
        self.live.connection_state = ConnectionState.CONNECTED
        self.live.send_token_refresh_notification = AsyncMock()
        self.live._listener_loop = asyncio.get_running_loop()
        XianyuLive._last_password_login_time.pop('123', None)

    async def asyncTearDown(self):
        for task in self.live.background_tasks.copy():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.live.background_tasks, return_exceptions=True)
        for context in reversed(self.patches):
            context.stop()

    async def test_empty_signature_token_from_qr_retries_without_relogin(self):
        session = Session([Response({'ret': ['FAIL_SYS_TOKEN_EMPTY::令牌为空'], 'data': {}},
                                    ['_m_h5_tk=fresh_9999999999999; Path=/']), success()])
        with patch('XianyuAutoAsync.aiohttp.ClientSession', return_value=session):
            self.assertEqual('valid-token', await self.live.refresh_token())
        self.assertEqual(2, len(session.requests))
        self.assertIn('_m_h5_tk=fresh_', session.requests[-1]['headers']['cookie'])
        self.assertFalse(self.live.needs_relogin)

    async def test_database_cookie_value_is_reloaded_and_messages_do_not_starve_renewal(self):
        self.db.get_cookie_details.return_value = {'value': COOKIES.replace('session', 'updated')}
        self.live.last_message_received_time = time.time()
        session = Session([success()])
        with patch('XianyuAutoAsync.aiohttp.ClientSession', return_value=session):
            self.assertEqual('valid-token', await self.live.refresh_token())
        self.assertIn('cookie2=updated', session.requests[0]['headers']['cookie'])
        self.assertLess(self.live.token_expires_at, time.time() + 601)

    async def test_session_expiry_without_password_is_terminal_and_notifies_once(self):
        session = Session([Response({'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期'], 'data': {}})])
        with patch('XianyuAutoAsync.aiohttp.ClientSession', return_value=session):
            self.assertIsNone(await self.live.refresh_token())
            self.assertIsNone(await self.live.refresh_token())
        self.assertTrue(self.live.needs_relogin)
        self.assertEqual(ConnectionState.FAILED, self.live.connection_state)
        self.assertEqual(1, len(session.requests))
        self.live.send_token_refresh_notification.assert_awaited_once()
        self.assertEqual('relogin_required', self.live.send_token_refresh_notification.await_args.args[1])

    async def test_session_expiry_password_refresh_resumes_on_same_listener(self):
        session = Session([Response({'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期'], 'data': {}}), success()])
        self.live._try_password_login_refresh = AsyncMock(return_value=True)
        with patch('XianyuAutoAsync.aiohttp.ClientSession', return_value=session):
            self.assertEqual('valid-token', await self.live.refresh_token())
        self.assertFalse(self.live.authentication_pending)
        self.assertFalse(self.live.needs_relogin)
        self.assertEqual(0, self.live.password_login_failures)

    async def test_actual_password_login_keeps_instance_and_settings(self):
        self.db.get_cookie_details.return_value = {'value': COOKIES, 'username': 'user', 'password': 'pass'}
        self.live._restart_instance = AsyncMock()
        slider = Mock()
        slider.login_with_password_playwright.return_value = {'unb': '123', 'cookie2': 'renewed'}
        with patch('utils.xianyu_slider_stealth.XianyuSliderStealth', return_value=slider):
            self.assertTrue(await self.live._try_password_login_refresh())
        self.assertEqual('renewed', self.live.cookies['cookie2'])
        self.live._restart_instance.assert_not_awaited()
        self.assertEqual(('123', COOKIES, self.live.cookies_str), self.db.refresh_cookie_value.call_args.args)

    async def test_password_login_failure_limit_survives_retry_attempts(self):
        self.db.get_cookie_details.return_value = {'value': COOKIES, 'username': 'user', 'password': 'bad'}
        slider = Mock()
        slider.login_with_password_playwright.return_value = None
        with patch('utils.xianyu_slider_stealth.XianyuSliderStealth', return_value=slider):
            for _ in range(3):
                XianyuLive._last_password_login_time.pop('123', None)
                self.assertFalse(await self.live._try_password_login_refresh())
        self.assertTrue(self.live.needs_relogin)
        self.assertEqual(3, slider.login_with_password_playwright.call_count)

    async def test_terminal_wait_accepts_updated_cookie_and_restores_main_loop(self):
        self.live._mark_relogin_required('expired')
        self.live._cancel_background_tasks = AsyncMock()
        self.db.get_cookie_details.return_value = {'value': COOKIES.replace('session', 'new-session')}
        await asyncio.wait_for(self.live._hold_relogin_terminal(), timeout=2)
        self.assertFalse(self.live.needs_relogin)
        self.assertIn('new-session', self.live.cookies_str)
        self.assertEqual(ConnectionState.DISCONNECTED, self.live.connection_state)
        self.assertIsNone(self.live.ws)

    async def test_cookie_refresh_timeout_keeps_listener_heartbeat_and_records_failure(self):
        heartbeat = asyncio.create_task(asyncio.Event().wait())
        self.live.heartbeat_task = heartbeat
        self.live._refresh_cookies_via_browser = AsyncMock(side_effect=asyncio.TimeoutError)
        try:
            await self.live._execute_cookie_refresh(time.time())
            self.assertFalse(heartbeat.cancelled())
            self.assertFalse(heartbeat.done())
            self.assertEqual('timeout', self.live.last_cookie_refresh_status)
            self.assertEqual(0, self.live.last_cookie_refresh_time)
            self.assertGreater(self.live.last_cookie_refresh_attempt, 0)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def test_cookie_snapshot_does_not_overwrite_newer_signing_token(self):
        self.live.cookies['_m_h5_tk'] = 'newer-token'
        self.live._merge_cookie_snapshot(COOKIES.replace('session', 'renewed'), COOKIES)
        self.assertEqual('newer-token', self.live.cookies['_m_h5_tk'])
        self.assertEqual('renewed', self.live.cookies['cookie2'])

    async def test_concurrent_persistence_cannot_roll_back_newer_cookie(self):
        entered, release = threading.Event(), threading.Event()
        writes = []

        def persist(_cookie_id, expected_value, new_value):
            if not writes:
                entered.set()
                release.wait(timeout=2)
            writes.append(new_value)
            return True

        self.db.refresh_cookie_value.side_effect = persist
        first = asyncio.create_task(self.live.update_config_cookies())
        await asyncio.to_thread(entered.wait, 1)
        self.live.cookies_str = COOKIES.replace('session', 'latest')
        second = asyncio.create_task(self.live.update_config_cookies())
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(2, len(writes))
        self.assertIn('cookie2=latest', writes[-1])

    async def test_simulated_72_hours_of_token_rotation_with_ongoing_messages(self):
        now = [1_800_000_000.0]
        responses = [Response({'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': f'token-{hour}'}},
                              [f'_m_h5_tk=signature-{hour}_9999999999999; Path=/']) for hour in range(73)]
        session = Session(responses)
        with patch('XianyuAutoAsync.time.time', side_effect=lambda: now[0]), \
                patch('XianyuAutoAsync.aiohttp.ClientSession', return_value=session):
            for hour in range(73):
                self.live.last_message_received_time = now[0]
                self.assertTrue(self.live._token_refresh_due())
                self.assertEqual(f'token-{hour}', await self.live.refresh_token())
                self.assertFalse(self.live.needs_relogin)
                now[0] += 3601
        self.assertEqual(73, len(session.requests))
        self.assertEqual('signature-72_9999999999999', self.live.cookies['_m_h5_tk'])

    async def test_cookie_validation_reads_seller_count_without_uploading(self):
        api = SimpleNamespace(get_order_count=AsyncMock(return_value={'ALL': 0}), cookies_str=COOKIES)
        context = AsyncMock()
        context.__aenter__.return_value = api
        with patch('utils.xianyu_seller_api.XianyuSellerAPI', return_value=context):
            result = await self.live._verify_cookie_validity()
        self.assertTrue(result['valid'])
        api.get_order_count.assert_awaited_once()

    async def test_seller_failures_share_one_recovery_and_reconnect(self):
        self.live.refresh_token = AsyncMock(return_value='renewed')
        self.live._schedule_session_recovery()
        self.live._schedule_session_recovery()
        task = self.live._session_recovery_task
        await task
        self.live.refresh_token.assert_awaited_once()
        self.live.ws.close.assert_awaited_once()

    async def test_heartbeat_timeout_closes_half_open_connection(self):
        self.live.last_heartbeat_response = time.time() - 120
        await self.live.heartbeat_loop(self.live.ws)
        self.live.ws.close.assert_awaited_once()
        self.live.ws.send.assert_not_awaited()

    async def test_regular_business_response_is_not_a_heartbeat(self):
        self.assertFalse(await self.live.handle_heartbeat_response({'code': 200, 'body': {'orders': []}}))

    async def test_repeated_network_failures_do_not_force_manual_login(self):
        self.live.create_session = AsyncMock()
        self.live.close_session = AsyncMock()
        self.live._try_password_login_refresh = AsyncMock()
        self.live._restart_instance = AsyncMock()
        self.live._calculate_retry_delay = Mock(return_value=0)
        self.live._create_websocket_connection = AsyncMock(side_effect=[
            *[ConnectionError('network timeout') for _ in range(6)], asyncio.CancelledError(),
        ])
        with self.assertRaises(asyncio.CancelledError):
            await self.live.main()
        self.assertFalse(self.live.needs_relogin)
        self.live._try_password_login_refresh.assert_not_awaited()
        self.live._restart_instance.assert_not_awaited()
        self.assertEqual(7, self.live._create_websocket_connection.await_count)

    async def test_main_waits_for_rescan_then_reconnects_and_queues_missed_orders(self):
        ready = asyncio.Event()
        sockets = []

        class Socket:
            def __init__(self):
                self.closed = False
                self.send = AsyncMock()
                self.stopped = asyncio.Event()

            async def close(self):
                self.closed = True
                self.stopped.set()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                await self.close()

            def __aiter__(self):
                async def messages():
                    ready.set()
                    await self.stopped.wait()
                    if False:
                        yield ''
                return messages()

        async def connect(headers):
            sock = Socket()
            sockets.append(sock)
            return sock

        async def hold(*args):
            await asyncio.Event().wait()

        async def fast_sleep(duration):
            await asyncio.sleep(0.005)

        self.live.create_session = AsyncMock()
        self.live.close_session = AsyncMock()
        self.live._create_websocket_connection = AsyncMock(side_effect=connect)
        self.live._interruptible_sleep = fast_sleep
        self.live._profile_synced = True
        for method in ('heartbeat_loop', 'token_refresh_loop', 'pause_cleanup_loop', 'cookie_refresh_loop',
                       'item_sync_loop', 'order_sync_loop', 'item_polish_loop',
                       'delivery_timeout_loop', 'buyer_interaction_loop'):
            setattr(self.live, method, AsyncMock(side_effect=hold))
        session = Session([Response({'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期'], 'data': {}}), success()])
        with patch('XianyuAutoAsync.aiohttp.ClientSession', return_value=session), \
                patch('app.delivery_reconciliation.request_delivery_reconciliation') as reconcile:
            task = asyncio.create_task(self.live.main())
            try:
                async def wait_terminal():
                    while not self.live.needs_relogin:
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(wait_terminal(), timeout=2)
                await asyncio.sleep(0.03)
                self.assertEqual(1, len(sockets), 'terminal state must not reconnect repeatedly')
                self.db.get_cookie_details.return_value['value'] = COOKIES.replace('session', 'rescanned')
                await asyncio.wait_for(ready.wait(), timeout=3)
                self.assertEqual(2, len(sockets))
                self.assertFalse(self.live.needs_relogin)
                self.assertEqual(ConnectionState.CONNECTED, self.live.connection_state)
                reconcile.assert_called_once_with('123', source='connected')
                self.assertIsNotNone(self.live.heartbeat_task)
                self.assertIsNotNone(self.live.token_refresh_task)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_session_failure_is_not_silenced_as_normal_token_expiry(self):
        self.assertFalse(self.live._is_normal_token_expiry('FAIL_SYS_SESSION_EXPIRED::Session过期'))
        self.assertFalse(self.live._is_normal_token_expiry('Token定时刷新失败，将自动重试'))
        self.assertTrue(self.live._is_normal_token_expiry('FAIL_SYS_TOKEN_EXOIRED::令牌过期'))


class SellerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_failure_stops_pagination_on_first_request(self):
        api = XianyuSellerAPI('test', COOKIES)
        api.get_sold_orders = AsyncMock(side_effect=SellerApiError('sold', ['FAIL_SYS_SESSION_EXPIRED']))
        with self.assertRaises(SellerApiError):
            await api.iter_sold_orders(max_pages=100)
        api.get_sold_orders.assert_awaited_once()

    async def test_session_failure_stops_count_and_refund_requests(self):
        from utils.seller_order_sync import sync_account_orders
        api = XianyuSellerAPI('test', COOKIES)
        api.get_expected_order_total = AsyncMock(side_effect=SellerApiError('count', ['FAIL_SYS_SESSION_EXPIRED']))
        api.iter_sold_orders = AsyncMock()
        api.iter_refund_orders = AsyncMock()
        with patch('utils.seller_order_sync.XianyuSellerAPI', return_value=api):
            result = await sync_account_orders('test', COOKIES)
        self.assertFalse(result['success'])
        self.assertFalse(result['complete'])
        self.assertTrue(result['session_expired'])
        self.assertEqual(1, result['failed'])
        api.iter_sold_orders.assert_not_awaited()
        api.iter_refund_orders.assert_not_awaited()

    async def test_cross_loop_or_transport_error_is_a_failed_sync(self):
        from utils.seller_order_sync import sync_account_orders
        api = XianyuSellerAPI('test', COOKIES)
        api.get_expected_order_total = AsyncMock(side_effect=RuntimeError('bound to a different event loop'))
        with patch('utils.seller_order_sync.XianyuSellerAPI', return_value=api):
            result = await sync_account_orders('test', COOKIES)
        self.assertFalse(result['success'])
        self.assertFalse(result['complete'])
        self.assertEqual(1, result['failed'])
        self.assertIn('different event loop', result['error'])

    async def test_authentication_error_is_cached_and_recovery_requested_once(self):
        session = Session([Response({'ret': ['FAIL_SYS_SESSION_EXPIRED'], 'data': {}})])
        api = XianyuSellerAPI('test', COOKIES, session=session)
        with patch.object(api, '_request_session_recovery') as recover, \
                patch.object(risk_control, 'registry', risk_control.RiskControlRegistry(6000)):
            for _ in range(2):
                with self.assertRaises(SellerApiError):
                    await api.get_order_count()
        recover.assert_called_once()
        self.assertEqual(1, len(session.requests))


class CookiePersistenceTests(unittest.TestCase):
    def setUp(self):
        from app.db_manager import DBManager
        self.temp = tempfile.TemporaryDirectory()
        self.db = DBManager(os.path.join(self.temp.name, 'session.db'))
        self.db.save_cookie('123', COOKIES, user_id=1)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_refresh_preserves_credentials_and_refuses_to_overwrite_new_scan(self):
        self.db.update_cookie_account_info('123', username='seller', password='pass')
        self.assertTrue(self.db.refresh_cookie_value('123', COOKIES, 'refreshed'))
        self.db.update_cookie_account_info('123', cookie_value='new-scan')
        self.assertFalse(self.db.refresh_cookie_value('123', 'refreshed', 'stale-background-result'))
        info = self.db.get_cookie_details('123')
        self.assertEqual('new-scan', info['value'])
        self.assertEqual('seller', info['username'])
        self.assertEqual('pass', info['password'])

    def test_deleted_account_cannot_be_recreated_by_late_refresh(self):
        self.db.delete_cookie('123')
        self.assertFalse(self.db.refresh_cookie_value('123', COOKIES, 'late-refresh'))
        self.assertIsNone(self.db.get_cookie_details('123'))


if __name__ == '__main__':
    unittest.main()
