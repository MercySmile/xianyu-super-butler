import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.card_inventory import join_card_inventory
from app.db_manager import DBManager
from app.delivery_template import send_payload, split_delivery_content


class BundledVariantDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'bundle.db')
        self.manager = DBManager(self.path)
        self.manager.conn.execute("INSERT OR IGNORE INTO users (id, username, email, password_hash) VALUES (1, 'owner', 'owner@example.com', 'x')")
        self.manager.conn.execute("INSERT OR IGNORE INTO users (id, username, email, password_hash) VALUES (2, 'other', 'other@example.com', 'x')")
        self.manager.conn.execute("INSERT INTO cookies (id, value, user_id) VALUES ('seller', 'cookie', 1)")
        self.manager.conn.execute("INSERT INTO item_info (cookie_id, item_id, item_title, item_detail) VALUES ('seller', 'item', 'Bundle', 'detail')")
        self.manager.conn.commit()
        self.units = [f'Username: account{i}\n\nPassword: xxx{i}' for i in range(1, 6)]
        self.stock = self.manager.create_card('accounts', 'grouped_data', user_id=1, data_content=join_card_inventory('grouped_data', self.units))
        self.download = self.manager.create_card('download', 'text', user_id=1, text_content='Download: https://example.com/download')
        self.guide = self.manager.create_card('guide', 'text', user_id=1, text_content='Read the guide\nKeep your password private')

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def save(self, *, text_ids=None, card_id=None, count=1, multi=False, spec='类型=账号和下载器'):
        return self.manager.save_item_delivery_config(
            user_id=1, cookie_id='seller', item_id='item', enabled=True,
            is_multi_spec=multi, variants=[{
                'display_name': 'Account and downloader', 'spec_text': spec,
                'card_id': card_id or self.stock, 'delivery_count': count,
                'fixed_text_card_ids': [self.download, self.guide] if text_ids is None else text_ids,
            }],
        )

    def resolve(self, **kwargs):
        return self.manager.resolve_item_delivery_binding('seller', 'item', **kwargs)

    def live(self):
        from XianyuAutoAsync import XianyuLive
        live = object.__new__(XianyuLive)
        live.cookie_id = 'seller'
        live.order_status_handler = None
        live.save_item_info_to_db = AsyncMock()
        return live

    def acquire(self, *, quantity=1, calls=1, context=None, detail=None):
        live = self.live()

        async def run():
            results = []
            for index in range(calls):
                results.append(await live._auto_delivery(
                    'item', 'Bundle', 'order', 'buyer', order_detail=detail,
                    delivery_context=context,
                    requested_item_quantity=quantity if index == 0 else 1,
                ))
            return results

        with patch('app.db_manager.db_manager', self.manager):
            return asyncio.run(run())

    def remaining(self):
        return self.manager.get_card_by_id(self.stock)['data_content']

    def test_round_trip_preserves_text_order_and_hides_contents_in_config(self):
        config = self.save(text_ids=[self.guide, self.download])
        variant = config['variants'][0]
        self.assertEqual([self.guide, self.download], variant['fixed_text_card_ids'])
        self.assertEqual(['guide', 'download'], [card['name'] for card in variant['fixed_text_cards']])
        self.assertTrue(config['complete'])
        self.assertEqual(5, variant['stock_count'])
        self.assertNotIn('text_content', variant['fixed_text_cards'][0])
        self.assertEqual([self.guide, self.download], [card['id'] for card in self.resolve()['fixed_text_cards']])

    def test_grouped_account_and_texts_are_one_complete_delivery(self):
        self.save()
        result = self.acquire()[0]
        self.assertEqual(self.units[0] + '\n\nDownload: https://example.com/download\n\nRead the guide\nKeep your password private', result)
        self.assertEqual(join_card_inventory('grouped_data', self.units[1:]), self.remaining())
        self.assertEqual(1, self.manager.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0])
        self.assertEqual('Download: https://example.com/download', self.manager.get_card_by_id(self.download)['text_content'])

    def test_multiple_items_and_per_item_count_deliver_complete_sets(self):
        self.save(count=2)
        context = {}
        contents = self.acquire(quantity=2, calls=4, context=context)
        for index, content in enumerate(contents):
            self.assertTrue(content.startswith(self.units[index] + '\n\n'))
            self.assertIn('Download:', content)
            self.assertIn('Read the guide', content)
        self.assertEqual(self.units[4], self.remaining())
        self.assertEqual(4, context['batch_data_required_count'])
        self.assertEqual(4, self.manager.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0])

    def test_regular_line_inventory_can_bundle_text(self):
        stock = self.manager.create_card('codes', 'data', user_id=1, data_content='CODE-1\nCODE-2')
        self.save(card_id=stock, text_ids=[self.download])
        self.assertEqual('CODE-1\n\nDownload: https://example.com/download', self.acquire()[0])
        self.assertEqual('CODE-2', self.manager.get_card_by_id(stock)['data_content'])

    def test_shortage_does_not_consume_inventory_or_produce_partial_bundle(self):
        self.save(count=3)
        before = self.remaining()
        self.assertEqual([None], self.acquire(quantity=2, context={}))
        self.assertEqual(before, self.remaining())
        self.assertEqual(0, self.manager.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0])

    def test_old_single_source_config_still_delivers_without_extra_text(self):
        self.save(text_ids=[])
        self.assertEqual(self.units[0], self.acquire()[0])

    def test_existing_binding_survives_old_database_upgrade(self):
        config = self.save(text_ids=[], count=2)
        binding_id = config['variants'][0]['binding_id']
        self.manager.conn.execute('ALTER TABLE variant_delivery_bindings DROP COLUMN fixed_text_card_ids')
        self.manager.conn.commit()
        self.manager.close()
        self.manager = DBManager(self.path)
        config = self.manager.get_item_delivery_config('seller', 'item', 1)
        self.assertEqual(binding_id, config['variants'][0]['binding_id'])
        self.assertEqual(2, config['variants'][0]['delivery_count'])
        self.assertEqual([], config['variants'][0]['fixed_text_card_ids'])
        self.assertEqual(self.units[0], self.acquire()[0])

    def test_invalid_bundle_choices_do_not_replace_saved_configuration(self):
        foreign = self.manager.create_card('other text', 'text', user_id=2, text_content='private')
        empty = self.manager.create_card('empty', 'text', user_id=1, text_content=' \n')
        image = self.manager.create_card('image', 'image', user_id=1, image_url='https://example.com/image.jpg')
        self.save()
        invalid_choices = ([foreign], [empty], [self.stock], [self.download, self.download], [0], ['1'], 'not-json')
        for ids in invalid_choices:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.save(text_ids=ids)
        with self.assertRaises(ValueError):
            self.save(card_id=image)
        with self.assertRaises(ValueError):
            self.save(card_id=self.download, text_ids=[self.download])
        self.assertEqual([self.download, self.guide], self.manager.get_item_delivery_config('seller', 'item', 1)['variants'][0]['fixed_text_card_ids'])

    def test_unavailable_text_blocks_before_any_stock_is_consumed(self):
        self.save()
        cases = [
            ('enabled', 0, 1), ('type', 'data', 'text'),
            ('text_content', '', 'Download: https://example.com/download'),
            ('user_id', 2, 1),
        ]
        for column, invalid, valid in cases:
            with self.subTest(column=column):
                self.manager.conn.execute(f'UPDATE cards SET {column} = ? WHERE id = ?', (invalid, self.download))
                self.manager.conn.commit()
                self.assertEqual('fixed_text_unavailable', self.resolve()['reason'])
                self.assertEqual([None], self.acquire(context={}))
                self.assertEqual(join_card_inventory('grouped_data', self.units), self.remaining())
                config = self.manager.get_item_delivery_config('seller', 'item', 1)
                self.assertFalse(config['complete'])
                self.assertFalse(self.manager.get_item_delivery_config_summaries(1)[0]['complete'])
                self.manager.conn.execute(f'UPDATE cards SET {column} = ? WHERE id = ?', (valid, self.download))
                self.manager.conn.commit()
        self.manager.delete_card(self.download, 1)  # Simulate an out-of-band deletion.
        self.assertEqual('fixed_text_unavailable', self.resolve()['reason'])
        self.assertEqual([None], self.acquire(context={}))
        self.assertEqual(0, self.manager.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0])

    def test_corrupt_bundle_fails_closed(self):
        self.save()
        self.manager.conn.execute("UPDATE variant_delivery_bindings SET fixed_text_card_ids = 'broken'")
        self.manager.conn.commit()
        self.assertEqual('fixed_text_unavailable', self.resolve()['reason'])
        self.assertEqual([None], self.acquire())
        self.assertFalse(self.manager.get_item_delivery_config_summaries(1)[0]['complete'])

    def test_text_edits_apply_to_future_orders_and_descriptions_are_preserved(self):
        self.save(text_ids=[self.guide])
        self.manager.update_card(self.guide, text_content='New guide', description='Instructions:\n{DELIVERY_CONTENT}', user_id=1)
        self.assertEqual(self.units[0] + '\n\nInstructions:\nNew guide', self.acquire()[0])

    def test_different_specifications_can_share_stock_and_attach_different_texts(self):
        self.manager.save_item_delivery_config(
            user_id=1, cookie_id='seller', item_id='item', enabled=True, is_multi_spec=True,
            variants=[
                {'spec_text': '类型=单账号', 'card_id': self.stock, 'fixed_text_card_ids': [self.guide]},
                {'spec_text': '类型=账号和下载器', 'card_id': self.stock, 'fixed_text_card_ids': [self.download, self.guide]},
            ],
        )
        detail = {'spec_payload': {'类型': '单账号'}}
        first = self.acquire(context={}, detail=detail)[0]
        self.assertIn('Read the guide', first)
        self.assertNotIn('Download:', first)
        detail = {'spec_payload': {'类型': '账号和下载器'}}
        second = self.acquire(context={}, detail=detail)[0]
        self.assertIn('Download:', second)
        self.assertTrue(second.startswith(self.units[1]))

    def test_fixed_text_primary_can_attach_more_text_without_inventory(self):
        self.save(card_id=self.download, text_ids=[self.guide])
        self.assertEqual('Download: https://example.com/download\n\nRead the guide\nKeep your password private', self.acquire()[0])
        self.assertEqual(0, self.manager.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0])

    def test_api_round_trip_toggle_and_reference_protection(self):
        from fastapi.testclient import TestClient
        from app.reply_server import app, get_current_user
        old = app.dependency_overrides.copy()
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(old)))
        app.dependency_overrides[get_current_user] = lambda: {'user_id': 1, 'username': 'owner'}
        client = TestClient(app)
        payload = {'enabled': True, 'is_multi_spec': False, 'variants': [{'card_id': self.stock, 'fixed_text_card_ids': [self.download]}]}
        with patch('app.reply_server.db_manager', self.manager), patch('app.db_manager.db_manager', self.manager):
            response = client.put('/items/seller/item/delivery-config', json=payload)
            self.assertEqual(200, response.status_code, response.text)
            config = client.get('/items/seller/item/delivery-config').json()
            self.assertEqual([self.download], config['variants'][0]['fixed_text_card_ids'])
            config['enabled'] = False
            self.assertEqual(200, client.put('/items/seller/item/delivery-config', json=config).status_code)
            self.assertEqual([self.download], client.get('/items/seller/item/delivery-config').json()['variants'][0]['fixed_text_card_ids'])
            self.assertEqual(409, client.delete(f'/cards/{self.download}').status_code)
            self.save(text_ids=[])
            self.assertEqual(200, client.delete(f'/cards/{self.download}').status_code)


class BundledDeliverySegmentTests(unittest.TestCase):
    def test_long_bundle_preserves_account_spacing_and_all_text_segments(self):
        content = '  Username: account  \n\nPassword: xxx  \n\n' + ('Guide line\n' * 150)
        segments = split_delivery_content(content)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(len(segment) <= 500 for segment in segments))
        self.assertEqual(content, ''.join(segments))
        live = type('Live', (), {'send_msg': AsyncMock()})()
        with patch('app.delivery_template.asyncio.sleep', new_callable=AsyncMock):
            count = asyncio.run(send_payload(live, 'ws', 'chat', 'buyer', content))
        self.assertEqual(len(segments), count)
        self.assertEqual(content, ''.join(call.args[-1] for call in live.send_msg.await_args_list))

    def test_segment_send_failure_is_propagated(self):
        live = type('Live', (), {'send_msg': AsyncMock(side_effect=RuntimeError('send failed'))})()
        with self.assertRaisesRegex(RuntimeError, 'send failed'):
            asyncio.run(send_payload(live, 'ws', 'chat', 'buyer', 'Account\n\nGuide'))


if __name__ == '__main__':
    unittest.main()
