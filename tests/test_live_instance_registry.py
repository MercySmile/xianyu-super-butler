"""Temporary HTTP helpers must never replace the account's message listener."""

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from XianyuAutoAsync import XianyuLive
from app import cookie_manager, reply_server


COOKIES = 'unb=12345; cookie2=test'
ACCOUNT = 'registry-test'
USER = {'user_id': 1, 'username': 'tester'}


class LiveInstanceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = patch.dict(XianyuLive._instances, {}, clear=True)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.order_handler = patch.object(XianyuLive, '_init_order_status_handler')
        self.order_handler.start()
        self.addCleanup(self.order_handler.stop)
        self.db_patch = patch.object(reply_server, 'db_manager')
        self.db = self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def helper(self, account=ACCOUNT):
        return XianyuLive(cookies_str=COOKIES, cookie_id=account, user_id=1)

    async def start_listener(self, account=ACCOUNT):
        live = self.helper(account)
        entered = asyncio.Event()

        async def hold_startup():
            # Run the real main lifecycle without opening a remote connection.
            live.ws = SimpleNamespace(closed=False, send=AsyncMock())
            entered.set()
            await asyncio.Event().wait()

        live.create_session = AsyncMock(side_effect=hold_startup)
        live.close_session = AsyncMock()
        task = asyncio.create_task(live.main())
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), timeout=2)
        return live, task

    async def test_construction_alone_does_not_register_a_listener(self):
        self.helper()
        self.assertIsNone(XianyuLive.get_instance(ACCOUNT))

    async def test_temporary_helpers_cannot_replace_online_listener(self):
        live, _ = await self.start_listener()
        for _ in range(3):
            helper = self.helper()
            self.assertIsNone(helper.ws)
            self.assertIs(XianyuLive.get_instance(ACCOUNT), live)
            helper._unregister_instance()
            self.assertIs(XianyuLive.get_instance(ACCOUNT), live)
        self.assertFalse(live.ws.closed)
        self.assertEqual(1, XianyuLive.get_instance_count())

    async def test_main_registers_before_startup_and_unregisters_on_cancel(self):
        live, task = await self.start_listener()
        self.assertIs(XianyuLive.get_instance(ACCOUNT), live)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(XianyuLive.get_instance(ACCOUNT))
        live.close_session.assert_awaited_once()

    async def test_cookie_manager_and_api_registry_share_running_instance(self):
        entered = asyncio.Event()

        async def hold_startup(live):
            entered.set()
            await asyncio.Event().wait()

        with (
            patch.object(cookie_manager.CookieManager, '_load_from_db'),
            patch.object(XianyuLive, 'create_session', new=hold_startup),
            patch.object(XianyuLive, 'close_session', new_callable=AsyncMock),
        ):
            manager = cookie_manager.CookieManager(asyncio.get_running_loop())
            task = asyncio.create_task(manager._run_xianyu(ACCOUNT, COOKIES, 1))
            self.tasks.append(task)
            await asyncio.wait_for(entered.wait(), timeout=2)
            live = manager.instances[ACCOUNT]
            self.assertIs(XianyuLive.get_instance(ACCOUNT), live)
            self.helper()
            self.assertIs(XianyuLive.get_instance(ACCOUNT), live)
            task.cancel()
            await task
            self.assertNotIn(ACCOUNT, manager.instances)
            self.assertIsNone(XianyuLive.get_instance(ACCOUNT))

    async def test_old_listener_cleanup_cannot_remove_replacement(self):
        old, old_task = await self.start_listener()
        current, _ = await self.start_listener()
        old_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await old_task
        self.assertIs(XianyuLive.get_instance(ACCOUNT), current)
        self.assertIsNot(old, current)

    async def test_separate_accounts_keep_separate_listeners(self):
        first, _ = await self.start_listener('first')
        second, _ = await self.start_listener('second')
        self.helper('first')
        self.assertIs(XianyuLive.get_instance('first'), first)
        self.assertIs(XianyuLive.get_instance('second'), second)

    async def test_startup_failure_does_not_leave_stale_registry_entry(self):
        live = self.helper()
        live.create_session = AsyncMock(side_effect=RuntimeError('startup failed'))
        live.close_session = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'startup failed'):
            await live.main()
        self.assertIsNone(XianyuLive.get_instance(ACCOUNT))

    async def test_close_failure_still_removes_listener_registration(self):
        live = self.helper()
        live.create_session = AsyncMock(side_effect=RuntimeError('startup failed'))
        live.close_session = AsyncMock(side_effect=RuntimeError('close failed'))
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            await live.main()
        self.assertIsNone(XianyuLive.get_instance(ACCOUNT))

    async def refresh_profile(self):
        with patch.object(XianyuLive, 'fetch_account_profile', new_callable=AsyncMock) as fetch:
            fetch.return_value = {'success': True, 'profile': {'nickname': 'Test'}}
            self.db.update_cookie_profile.return_value = True
            self.db.get_cookie_details.return_value = {'nickname': 'Test'}
            result = await reply_server._fetch_and_store_account_profile(ACCOUNT, COOKIES, 1)
            fetch.assert_awaited_once()
            self.assertTrue(result['success'])

    async def test_account_profile_refresh_keeps_message_listener(self):
        live, _ = await self.start_listener()
        await self.refresh_profile()
        self.assertIs(XianyuLive.get_instance(ACCOUNT), live)

    async def test_qr_cookie_enhancement_keeps_message_listener(self):
        live, _ = await self.start_listener()
        with (
            patch.object(XianyuLive, 'refresh_cookies_from_qr_login', new_callable=AsyncMock, return_value=False) as refresh,
            patch.object(reply_server, '_fetch_and_store_account_profile', new_callable=AsyncMock, return_value={'success': False}),
        ):
            await reply_server._enhance_qr_login_cookies(
                session_id='session', cookies=COOKIES,
                account_info={'account_id': ACCOUNT, 'cookie_field_count': 2}, current_user=USER,
            )
            refresh.assert_awaited_once()
        self.assertIs(XianyuLive.get_instance(ACCOUNT), live)

    async def test_explicit_qr_cookie_refresh_keeps_message_listener(self):
        live, _ = await self.start_listener()
        with patch.object(XianyuLive, 'refresh_cookies_from_qr_login', new_callable=AsyncMock, return_value=False) as refresh:
            await reply_server.refresh_cookies_from_qr_login(
                {'qr_cookies': COOKIES, 'cookie_id': ACCOUNT}, USER,
            )
            refresh.assert_awaited_once()
        self.assertIs(XianyuLive.get_instance(ACCOUNT), live)

    async def test_item_sync_keeps_message_listener(self):
        live, _ = await self.start_listener()
        self.db.get_all_cookies.return_value = {ACCOUNT: COOKIES}
        self.db.get_cookie_by_id.return_value = {'cookies_str': COOKIES}
        with patch.object(XianyuLive, 'get_all_items', new_callable=AsyncMock, return_value={'success': True, 'total_count': 0}) as sync:
            result = await reply_server.get_all_items_from_account({'cookie_id': ACCOUNT}, USER)
            self.assertTrue(result['success'])
            sync.assert_awaited_once()
        self.assertIs(XianyuLive.get_instance(ACCOUNT), live)

    async def test_full_delivery_after_profile_refresh_uses_live_socket(self):
        self.enable_delivery_gate()
        live, _ = await self.start_listener()
        await self.refresh_profile()
        live.apply_delivery_block_rules = AsyncMock(return_value={'action': 'allow'})
        live._auto_delivery = AsyncMock(return_value='Username: demo\n\nDownload instructions')
        live.is_auto_confirm_enabled = Mock(return_value=False)
        live.mark_delivery_sent = Mock()
        self.db.get_all_cookies.return_value = {ACCOUNT: COOKIES}
        self.db.get_order_by_id.return_value = {
            'order_id': 'order', 'cookie_id': ACCOUNT,
            'item_id': 'item', 'buyer_id': 'buyer', 'chat_id': 'chat',
        }
        self.db.get_item_multi_quantity_delivery_status.return_value = False
        with patch('app.db_manager.db_manager', self.db):
            result = await reply_server.manual_ship_orders(
                order_ids=['order'], ship_mode='full_delivery', custom_content=None, current_user=USER,
            )
        self.assertEqual(1, result['success_count'], result)
        self.assertEqual(0, result['failed_count'], result)
        live.ws.send.assert_awaited_once()
        message = json.loads(live.ws.send.await_args.args[0])
        encoded = message['body'][0]['content']['custom']['data']
        content = json.loads(base64.b64decode(encoded))
        self.assertEqual('Username: demo\n\nDownload instructions', content['text']['text'])
        live.mark_delivery_sent.assert_called_once_with('order', update_order_status=False)

    async def test_disconnected_real_listener_still_blocks_before_consumption(self):
        self.enable_delivery_gate()
        live, _ = await self.start_listener()
        live.ws.closed = True
        live._auto_delivery = AsyncMock()
        self.db.get_all_cookies.return_value = {ACCOUNT: COOKIES}
        self.db.get_order_by_id.return_value = {
            'order_id': 'order', 'cookie_id': ACCOUNT,
            'item_id': 'item', 'buyer_id': 'buyer', 'chat_id': 'chat',
        }
        with patch('app.db_manager.db_manager', self.db):
            result = await reply_server.manual_ship_orders(
                order_ids=['order'], ship_mode='full_delivery', custom_content=None, current_user=USER,
            )
        self.assertEqual(1, result['failed_count'])
        self.assertIn('WebSocket', result['results'][0]['message'])
        live._auto_delivery.assert_not_awaited()

    def enable_delivery_gate(self):
        # Keep the HTTP/listener mocks while exercising the real durable order gate.
        from app.db_manager import DBManager
        gate_db = DBManager(':memory:')
        self.addCleanup(gate_db.close)
        gate_db.conn.execute(
            "INSERT INTO orders(order_id,cookie_id,order_status) VALUES ('order',?,'pending_ship')",
            (ACCOUNT,),
        )
        gate_db.conn.commit()
        self.db.conn = gate_db.conn
        self.db.lock = gate_db.lock


if __name__ == '__main__':
    unittest.main()
