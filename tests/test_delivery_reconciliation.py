import asyncio
import base64
import json
import os
import tempfile
import unittest
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.db_manager import DBManager
from app.delivery_attempt import DeliveryAttempt, delivery_block_reason, mark_external_acquisition
from app.delivery_reconciliation import (
    _enqueue, _find_chat, reconcile_pending_deliveries, request_delivery_reconciliation,
)


class DatabaseFixture:
    def setup_database(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'recovery.db')
        self.db = DBManager(self.path)
        self.db.conn.execute("INSERT INTO cookies(id,value,user_id) VALUES ('seller','cookie',1)")
        self.db.conn.execute("INSERT INTO item_info(cookie_id,item_id,item_title,item_detail) VALUES ('seller','item','Bundle','details')")
        self.db.conn.commit()
        self.stock = self.db.create_card('accounts', 'data', user_id=1, data_content='A1\nA2\nA3\nA4')
        self.text = self.db.create_card('guide', 'text', user_id=1, text_content='Download guide')
        self.db.save_item_delivery_config(
            1, 'seller', 'item', True, True, [
                {'display_name': name, 'spec_text': f'类型={name}', 'card_id': card,
                 'delivery_count': 1, 'fixed_text_card_ids': extras}
                for name, card, extras in [('账号', self.stock, []), ('下载器', self.text, []),
                                           ('账号和下载器', self.stock, [self.text])]
            ],
        )
        self.order('100')

    def order(self, order_id, **kwargs):
        data = dict(order_id=order_id, cookie_id='seller', item_id='item', buyer_id='buyer',
                    order_status='pending_ship', chat_id='chat')
        data.update(kwargs)
        self.db.insert_or_update_order(**data)

    def cleanup_database(self):
        self.db.close()
        self.temp.cleanup()

    def shipment_count(self):
        return self.db.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0]

    def state(self, order_id='100'):
        return self.db.conn.execute(
            'SELECT state, phase FROM order_delivery_attempts WHERE order_id=?', (order_id,)
        ).fetchone()


class DeliveryAttemptTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_database()

    def tearDown(self):
        self.cleanup_database()

    def test_unconsumed_failure_is_retryable(self):
        with DeliveryAttempt(self.db, '100', 'seller') as attempt:
            self.assertTrue(attempt.acquired)
        self.assertEqual(('retryable', 'ready'), self.state())
        with DeliveryAttempt(self.db, '100', 'seller') as retry:
            self.assertTrue(retry.acquired)

    def test_claim_serializes_separate_database_connections(self):
        other = DBManager(self.path)
        try:
            with DeliveryAttempt(self.db, '100', 'seller') as first:
                with ThreadPoolExecutor(1) as pool:
                    def race():
                        with DeliveryAttempt(other, '100', 'seller') as second:
                            return second.acquired
                    self.assertFalse(pool.submit(race).result())
                first.sent()
            self.assertEqual(('sent', 'prepared'), self.state())
        finally:
            other.close()

    def test_consumption_is_durable_even_if_shipment_history_is_cleared(self):
        with DeliveryAttempt(self.db, '100', 'seller'):
            self.assertEqual(['A1'], self.db.consume_batch_data_batch(self.stock, 1, order_id='100'))
            self.assertEqual(('active', 'prepared'), self.state())
            self.db.conn.execute('DELETE FROM card_shipments')
            self.db.conn.commit()
        self.db.close()
        self.db = DBManager(self.path)
        with DeliveryAttempt(self.db, '100', 'seller') as again:
            self.assertFalse(again.acquired)

    def test_external_api_timeout_blocks_retry(self):
        with self.assertRaises(TimeoutError):
            with DeliveryAttempt(self.db, '100', 'seller'):
                mark_external_acquisition(self.db, '100')
                raise TimeoutError()
        self.assertEqual(('blocked', 'prepared'), self.state())

    def test_interrupted_active_attempt_is_not_automatically_taken_over(self):
        first = DeliveryAttempt(self.db, '100', 'seller').__enter__()
        self.assertTrue(first.acquired)
        other = DBManager(self.path)
        try:
            with DeliveryAttempt(other, '100', 'seller') as again:
                self.assertFalse(again.acquired)
        finally:
            other.close()

    def test_legacy_manual_shipment_and_system_shipped_block_new_acquisition(self):
        self.db.consume_batch_data_batch(self.stock, 1, order_id='100')
        self.assertTrue(delivery_block_reason(self.db, '100', 'seller'))
        self.order('101', system_shipped=True)
        self.assertTrue(self.db.get_order_by_id('101')['system_shipped'])
        with DeliveryAttempt(self.db, '101', 'seller') as again:
            self.assertFalse(again.acquired)

    def test_status_only_can_finish_sent_order_but_cannot_race_active_delivery(self):
        with DeliveryAttempt(self.db, '100', 'seller') as active:
            with DeliveryAttempt(self.db, '100', 'seller', status_only=True) as status:
                self.assertFalse(status.acquired)
            active.sent()
        with DeliveryAttempt(self.db, '100', 'seller', status_only=True) as status:
            self.assertTrue(status.acquired)
        self.assertEqual(('sent', 'prepared'), self.state())

    def test_shortage_does_not_block_recovery_after_restock(self):
        with DeliveryAttempt(self.db, '100', 'seller'):
            self.assertIsNone(self.db.consume_batch_data_batch(self.stock, 99, order_id='100'))
        self.assertEqual(('retryable', 'ready'), self.state())
        self.assertEqual(0, self.shipment_count())


class DeliveryRecoveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from XianyuAutoAsync import XianyuLive
        self.setup_database()
        self.live = object.__new__(XianyuLive)
        self.live.cookie_id = 'seller'
        self.live.cookies_str = 'cookie'
        self.live.myid = 'seller-id'
        self.live.ws = SimpleNamespace(closed=False, send=AsyncMock())
        self.live._listener_loop = asyncio.get_running_loop()
        self.live._order_locks = defaultdict(asyncio.Lock)
        self.live._lock_hold_info = {}
        self.live._lock_usage_times = {}
        self.live.delivery_sent_orders = set()
        self.live.delivery_blocked_orders = set()
        self.live.last_delivery_time = {}
        self.live.delivery_cooldown = 600
        self.live.order_status_handler = None
        self.live.confirmed_orders = {}
        self.live.background_tasks = set()
        self.live.save_item_info_to_db = AsyncMock()
        self.live.apply_delivery_block_rules = AsyncMock(return_value={'action': 'allow'})
        self.live.is_auto_confirm_enabled = Mock(return_value=False)
        self.live.send_delivery_failure_notification = AsyncMock()
        self.live.get_im_conversations = AsyncMock(return_value={})
        self.details = {'100': self.detail('100')}
        async def fetch(cookie, cookies, oid):
            return self.details.get(oid)
        self.fetch = AsyncMock(side_effect=fetch)
        self.patches = [
            patch('app.db_manager.db_manager', self.db),
            patch('app.cookie_manager.manager', None),
            patch('utils.seller_order_sync.fetch_order_detail_direct', self.fetch),
            patch.dict(XianyuLive._instances, {'seller': self.live}, clear=True),
        ]
        for context in self.patches:
            context.start()

    async def asyncTearDown(self):
        tasks = list(self.live.background_tasks)
        tasks += [info['task'] for info in self.live._lock_hold_info.values() if info.get('task')]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for context in reversed(self.patches):
            context.stop()
        self.cleanup_database()

    def detail(self, oid, spec='账号', status='pending_ship', **kwargs):
        data = dict(order_id=oid, item_id='item', buyer_id='buyer', order_status=status,
                    status_text='等待卖家发货', quantity='1', buy_num=1,
                    spec_name='类型', spec_value=spec)
        data.update(kwargs)
        return data

    def sent_texts(self):
        result = []
        for call in self.live.ws.send.await_args_list:
            data = json.loads(call.args[0])
            if isinstance(data.get('body'), list):
                content = data['body'][0]['content']['custom']['data']
                result.append(json.loads(base64.b64decode(content))['text']['text'])
        return result

    async def test_three_orders_same_buyer_item_use_their_own_specifications(self):
        for oid, spec in [('100', '账号'), ('101', '下载器'), ('102', '账号和下载器')]:
            self.order(oid)
            self.details[oid] = self.detail(oid, spec)
        await reconcile_pending_deliveries(self.live, ['102', '100', '101'])
        self.assertEqual(['A1', 'Download guide', 'A2\n\nDownload guide'], self.sent_texts())
        self.assertEqual(2, self.shipment_count())
        for oid in self.details:
            self.assertTrue(self.db.get_order_by_id(oid)['system_shipped'])
        await reconcile_pending_deliveries(self.live, list(self.details))
        self.assertEqual(3, len(self.sent_texts()))

    async def test_stale_pending_order_must_be_remotely_confirmed(self):
        for status in ('processing', 'refunding', 'cancelled', 'shipped', 'completed', 'unknown'):
            self.details['100'] = self.detail('100', status=status)
            await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual([], self.sent_texts())
        self.assertEqual(0, self.shipment_count())

    async def test_remote_failure_and_identity_mismatch_never_consume(self):
        for detail in (None, self.detail('wrong-id'), self.detail('100', buyer_id='other'),
                       self.detail('100', item_id='other'), self.detail('100', buyer_id='')):
            self.details['100'] = detail
            await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(0, self.shipment_count())

    async def test_missing_or_unknown_specification_does_not_fall_back_to_other_inventory(self):
        for spec in ('', 'nonexistent'):
            self.details['100'] = self.detail('100', spec)
            await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(0, self.shipment_count())

    async def test_partial_send_failure_remains_blocked_after_restart(self):
        self.live.ws.send.side_effect = ConnectionError('disconnected')
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(('blocked', 'prepared'), self.state())
        self.assertEqual(1, self.shipment_count())
        self.live.delivery_blocked_orders.clear()
        self.live.background_tasks.update(info['task'] for info in self.live._lock_hold_info.values())
        self.live._lock_hold_info.clear()
        self.live.last_delivery_time.clear()
        self.live.ws.send.reset_mock(side_effect=True)
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(1, self.shipment_count())
        self.live.ws.send.assert_not_awaited()

    async def test_manual_success_and_later_sync_does_not_consume_twice(self):
        from app.reply_server import manual_ship_orders
        with patch.object(self.db, 'get_item_multi_quantity_delivery_status', return_value=False):
            self.db.save_item_delivery_config(1, 'seller', 'item', True, False, [
                {'display_name': 'default', 'card_id': self.stock, 'delivery_count': 1}
            ])
            result = await manual_ship_orders(['100'], 'full_delivery', None, {'user_id': 1, 'username': 'tester'})
        self.assertEqual(1, result['success_count'], result)
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(1, self.shipment_count())
        self.assertEqual(['A1'], self.sent_texts())

    async def test_manual_and_message_race_only_acquires_once(self):
        from app.reply_server import manual_ship_orders
        entered, release = asyncio.Event(), asyncio.Event()
        async def protection(**kwargs):
            entered.set()
            await release.wait()
            return {'action': 'allow'}
        self.live.apply_delivery_block_rules.side_effect = protection
        recovery = asyncio.create_task(reconcile_pending_deliveries(self.live, ['100']))
        await asyncio.wait_for(entered.wait(), 2)
        result = await manual_ship_orders(['100'], 'full_delivery', None, {'user_id': 1, 'username': 'tester'})
        self.assertEqual(1, result['failed_count'])
        release.set()
        await recovery
        self.assertEqual(1, self.shipment_count())

    async def test_manual_winning_race_blocks_compensation(self):
        from app.reply_server import manual_ship_orders
        self.db.save_item_delivery_config(1, 'seller', 'item', True, False, [
            {'display_name': 'default', 'card_id': self.stock, 'delivery_count': 1}
        ])
        entered, release = asyncio.Event(), asyncio.Event()
        async def protection(**kwargs):
            entered.set()
            await release.wait()
            return {'action': 'allow'}
        self.live.apply_delivery_block_rules.side_effect = protection
        manual = asyncio.create_task(manual_ship_orders(
            ['100'], 'full_delivery', None, {'user_id': 1, 'username': 'tester'}
        ))
        await asyncio.wait_for(entered.wait(), 2)
        await reconcile_pending_deliveries(self.live, ['100'])
        self.fetch.assert_not_awaited()
        release.set()
        result = await manual
        self.assertEqual(1, result['success_count'], result)
        self.assertEqual(1, self.shipment_count())

    async def test_status_change_while_waiting_for_order_lock_prevents_delivery(self):
        lock = self.live._order_locks['100']
        await lock.acquire()
        trigger = asyncio.create_task(self.live._handle_auto_delivery(
            self.live.ws, {'3': {'orderId': '100'}}, 'buyer', 'buyer', 'item', 'chat',
            'now', verified_order_detail=self.details['100'],
        ))
        await asyncio.sleep(0)
        self.order('100', order_status='refunding')
        lock.release()
        await trigger
        self.assertEqual(0, self.shipment_count())

    async def test_fixed_text_send_failure_is_durable_without_stock_ledger(self):
        self.details['100'] = self.detail('100', '下载器')
        self.live.ws.send.side_effect = ConnectionError('disconnected')
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(('blocked', 'prepared'), self.state())
        self.assertEqual(0, self.shipment_count())
        with DeliveryAttempt(self.db, '100', 'seller') as again:
            self.assertFalse(again.acquired)

    async def test_platform_confirmation_failure_does_not_repeat_content(self):
        self.live.is_auto_confirm_enabled.return_value = True
        self.live.auto_confirm = AsyncMock(return_value={'success': False, 'error': 'timeout'})
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertTrue(self.db.get_order_by_id('100')['system_shipped'])
        self.assertEqual('pending_ship', self.db.get_order_by_id('100')['order_status'])
        self.live.delivery_sent_orders.clear()
        self.live.background_tasks.update(info['task'] for info in self.live._lock_hold_info.values())
        self.live._lock_hold_info.clear()
        self.live.last_delivery_time.clear()
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(['A1'], self.sent_texts())
        self.assertEqual(1, self.shipment_count())

    async def test_message_and_compensation_race_share_gate(self):
        async def trigger():
            await self.live._handle_auto_delivery(
                self.live.ws, {'3': {'orderId': '100'}}, 'buyer', 'buyer', 'item', 'chat',
                'now', verified_order_detail=self.details['100'],
            )
        await asyncio.gather(trigger(), reconcile_pending_deliveries(self.live, ['100']), trigger())
        self.assertEqual(1, self.shipment_count())
        self.assertEqual(['A1'], self.sent_texts())

    async def test_disconnected_or_disabled_account_never_queries_or_consumes(self):
        self.live.ws.closed = True
        await reconcile_pending_deliveries(self.live, ['100'])
        self.live.ws.closed = False
        with patch('app.cookie_manager.manager', SimpleNamespace(get_cookie_status=lambda _: False)):
            await reconcile_pending_deliveries(self.live, ['100'])
        self.fetch.assert_not_awaited()
        self.assertEqual(0, self.shipment_count())

    async def test_connection_change_during_preparation_prevents_stock_consumption(self):
        async def close_during_preparation(*args):
            self.live.ws.closed = True
        self.live.save_item_info_to_db.side_effect = close_during_preparation
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(0, self.shipment_count())
        self.assertEqual(('retryable', 'ready'), self.state())

    async def test_cancelled_worker_can_be_scheduled_again(self):
        _enqueue(self.live, {'100'}, 'order_sync')
        worker = self.live._delivery_reconciliation_task
        await asyncio.sleep(0)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertIsNone(self.live._delivery_reconciliation_task)
        with patch('app.delivery_reconciliation.asyncio.sleep', new_callable=AsyncMock):
            _enqueue(self.live, {'100'}, 'order_sync')
            await self.live._delivery_reconciliation_task
        self.assertEqual(['A1'], self.sent_texts())

    async def test_missing_chat_does_not_acquire(self):
        self.order('100', chat_id='')
        await reconcile_pending_deliveries(self.live, ['100'])
        self.assertEqual(0, self.shipment_count())
        self.assertIsNone(self.state())

    async def test_offline_order_chat_resolves_only_exact_participant_pair(self):
        def raw(seller, buyer, chat):
            return {'singleChatUserConversation': {'singleChatConversation': {
                'cid': chat, 'pairFirst': seller, 'pairSecond': buyer,
            }}}
        self.live.get_im_conversations.return_value = {'userConvs': [
            raw('stranger', 'buyer', 'wrong'), raw('seller-id', 'buyer', 'right@goofish')
        ]}
        order = self.db.get_order_by_id('100')
        order['chat_id'] = ''
        self.assertEqual('right', await _find_chat(self.live, self.db, order))

    async def test_nonchat_seller_reminder_schedules_query_without_mistaking_chat_id(self):
        reminder = {'1': '68206784517@goofish', '3': {'redReminder': '等待卖家发货'}}
        package = {'headers': {}, 'body': {'syncPushPackage': {'data': [{
            'data': base64.b64encode(json.dumps(reminder).encode()).decode()
        }]}}}
        self.live._extract_order_id = Mock(side_effect=AssertionError('must not infer an order from cid'))
        with patch('app.delivery_reconciliation.request_delivery_reconciliation') as schedule:
            await self.live.handle_message(package, self.live.ws)
        schedule.assert_called_once_with('seller', source='seller_reminder')
        self.live._extract_order_id.assert_not_called()

    async def test_worker_coalesces_bursts_and_rechecks_reminder(self):
        with patch('app.delivery_reconciliation.asyncio.sleep', new_callable=AsyncMock), \
             patch('app.delivery_reconciliation.reconcile_pending_deliveries', new_callable=AsyncMock) as check:
            _enqueue(self.live, {'100'}, 'order_sync')
            _enqueue(self.live, None, 'seller_reminder')
            _enqueue(self.live, {'101'}, 'order_sync')
            await self.live._delivery_reconciliation_task
        self.assertEqual(2, check.await_count)
        self.assertIsNone(check.await_args_list[0].args[1])

    async def test_cross_thread_refresh_is_dispatched_to_listener_loop(self):
        with patch('app.delivery_reconciliation._enqueue') as enqueue:
            queued = await asyncio.to_thread(request_delivery_reconciliation, 'seller', ['100'])
            await asyncio.sleep(0)
        self.assertTrue(queued)
        enqueue.assert_called_once_with(self.live, {'100'}, 'order_sync')

    async def test_sync_schedules_only_saved_pending_orders(self):
        from utils.seller_order_sync import sync_account_orders
        api = SimpleNamespace(
            get_expected_order_total=AsyncMock(return_value=2),
            iter_sold_orders=AsyncMock(return_value=[self.detail('100'), self.detail('101', status='shipped', status_text='已发货')]),
            cookies_str='cookie', close=AsyncMock(),
        )
        with patch('utils.seller_order_sync.XianyuSellerAPI', return_value=api), \
             patch('utils.seller_order_sync.parse_sold_order', side_effect=lambda value: value), \
             patch('app.delivery_reconciliation.request_delivery_reconciliation') as schedule:
            result = await sync_account_orders('seller', 'cookie', include_refund_history=False)
        self.assertEqual(['100'], result['pending_order_ids'])
        schedule.assert_called_once_with('seller', ['100'], source='order_sync')

    async def test_reminder_queries_actual_seller_orders_including_multiple_specs(self):
        api = SimpleNamespace(iter_sold_orders=AsyncMock(return_value=[self.detail('100')]), close=AsyncMock())
        with patch('utils.xianyu_seller_api.XianyuSellerAPI', return_value=api), \
             patch('utils.xianyu_seller_api.parse_sold_order', side_effect=lambda value: value):
            await reconcile_pending_deliveries(self.live, source='reminder')
        api.iter_sold_orders.assert_awaited_once()
        self.assertEqual(['A1'], self.sent_texts())


if __name__ == '__main__':
    unittest.main()
