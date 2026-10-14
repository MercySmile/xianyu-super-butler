import asyncio
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from app.card_inventory import GROUPED_CARD_SEPARATOR as SEP, split_card_inventory, join_card_inventory
from app.db_manager import DBManager


BUNDLE = '--- UPDEVNEWS MAIL ---\nUsername: account1\nEmail:    account1@example.com\nPassword: xxx\n\n--- PROTON ---\nEmail: mail1@example.com\nPassword: xxx\n\n--- EPIC ---\nAccount: mail1@example.com\nPassword: xxx'
SECOND = BUNDLE.replace('account1', 'account2').replace('mail1', 'mail2')


class GroupedCardParsingTests(unittest.TestCase):
    def test_multiline_bundle_without_separator_is_one_unit(self):
        self.assertEqual([BUNDLE], split_card_inventory('grouped_data', BUNDLE))

    def test_empty_groups_and_windows_line_endings(self):
        content = f'{SEP}\n\n{BUNDLE}\n\n  {SEP}  \n{SEP}\n{SECOND}\n{SEP}\n'
        self.assertEqual([BUNDLE, SECOND], split_card_inventory('grouped_data', content.replace('\n', '\r\n')))

    def test_indentation_and_field_spacing_survive(self):
        content = '  Username: a  \n\n    Password: xxx  '
        self.assertEqual([content], split_card_inventory('grouped_data', '\n' + content + '\n'))

    def test_separator_inside_field_is_plain_text(self):
        content = f'Note: {SEP}\nPassword: xxx'
        self.assertEqual([content], split_card_inventory('grouped_data', content))

    def test_only_separators_is_empty(self):
        self.assertEqual([], split_card_inventory('grouped_data', f'\n{SEP}\n{SEP}\n'))

    def test_ordinary_batch_keeps_line_semantics(self):
        self.assertEqual(['a', 'b', SEP], split_card_inventory('data', f' a \n\nb\n{SEP}'))


class GroupedCardInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp.name, 'cards.db'))
        self.manager.conn.execute("INSERT OR IGNORE INTO users (id, username, email, password_hash) VALUES (1, 'owner', 'owner@example.com', 'x')")
        self.manager.conn.commit()
        self.card_id = self.manager.create_card('bundles', 'grouped_data', data_content=join_card_inventory('grouped_data', [BUNDLE, SECOND]), user_id=1)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_consume_one_bundle_records_one_complete_shipment(self):
        self.assertEqual(BUNDLE, self.manager.consume_batch_data(self.card_id, order_id='order-1', item_id='item-1', buyer_id='buyer-1', cookie_id='seller-1'))
        self.assertEqual(SECOND, self.manager.get_card_by_id(self.card_id)['data_content'])
        row = self.manager.conn.execute('SELECT content, order_id, item_id, buyer_id, cookie_id FROM card_shipments').fetchone()
        self.assertEqual((BUNDLE, 'order-1', 'item-1', 'buyer-1', 'seller-1'), row)

    def test_shortage_changes_neither_stock_nor_shipments(self):
        before = self.manager.get_card_by_id(self.card_id)['data_content']
        self.assertIsNone(self.manager.consume_batch_data_batch(self.card_id, 3))
        self.assertEqual(before, self.manager.get_card_by_id(self.card_id)['data_content'])
        self.assertEqual(0, self.manager.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0])

    def test_consume_all_and_exhaustion(self):
        self.assertEqual([BUNDLE, SECOND], self.manager.consume_batch_data_batch(self.card_id, 2))
        self.assertEqual('', self.manager.get_card_by_id(self.card_id)['data_content'])
        self.assertIsNone(self.manager.consume_batch_data(self.card_id))
        self.assertEqual(2, self.manager.conn.execute('SELECT COUNT(*) FROM card_shipments').fetchone()[0])

    def test_concurrent_consumers_do_not_duplicate_bundles(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.manager.consume_batch_data(self.card_id), range(4)))
        self.assertCountEqual([BUNDLE, SECOND], [result for result in results if result])

    def test_record_failure_rolls_back_stock(self):
        before = self.manager.get_card_by_id(self.card_id)['data_content']
        self.manager.conn.execute("CREATE TRIGGER reject_shipment BEFORE INSERT ON card_shipments BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        self.manager.conn.commit()
        self.assertIsNone(self.manager.consume_batch_data(self.card_id))
        self.assertEqual(before, self.manager.get_card_by_id(self.card_id)['data_content'])

    def test_stock_counts_and_edit(self):
        self.assertEqual(2, self.manager._card_stock_count('grouped_data', join_card_inventory('grouped_data', [BUNDLE, SECOND])))
        self.assertIsNone(self.manager._card_stock_count('text', BUNDLE))
        self.assertTrue(self.manager.update_card(self.card_id, data_content=SECOND, user_id=1))
        self.assertEqual(SECOND, self.manager.consume_batch_data(self.card_id))

    def _prepare_delivery(self, count=1):
        self.manager.conn.execute("INSERT INTO cookies (id, value, user_id) VALUES ('seller-1', 'cookie', 1)")
        self.manager.conn.execute("INSERT INTO item_info (cookie_id, item_id, item_title, item_detail) VALUES ('seller-1', 'item-1', 'Subscription', 'detail')")
        self.manager.conn.commit()
        self.manager.save_item_delivery_config(user_id=1, cookie_id='seller-1', item_id='item-1', enabled=True, is_multi_spec=False, variants=[{'card_id': self.card_id, 'delivery_count': count}])
        from XianyuAutoAsync import XianyuLive
        live = object.__new__(XianyuLive)
        live.cookie_id = 'seller-1'
        live.save_item_info_to_db = AsyncMock()
        return live

    def test_delivery_queue_sends_complete_bundles(self):
        live = self._prepare_delivery(count=2)
        context = {}
        with patch('app.db_manager.db_manager', self.manager):
            first = asyncio.run(live._auto_delivery('item-1', 'Subscription', 'order-1', 'buyer-1', delivery_context=context))
            second = asyncio.run(live._auto_delivery('item-1', 'Subscription', 'order-1', 'buyer-1', delivery_context=context))
        self.assertEqual([BUNDLE, SECOND], [first, second])
        self.assertEqual([], context['batch_data_queue'])
        self.assertEqual(2, context['batch_data_required_count'])
        self.assertEqual(2, self.manager.conn.execute("SELECT COUNT(*) FROM card_shipments WHERE order_id = 'order-1'").fetchone()[0])

    def test_delivery_shortage_consumes_nothing(self):
        live = self._prepare_delivery(count=2)
        with patch('app.db_manager.db_manager', self.manager):
            result = asyncio.run(live._auto_delivery('item-1', 'Subscription', 'order-1', 'buyer-1', delivery_context={}, requested_item_quantity=2))
        self.assertIsNone(result)
        self.assertEqual(2, self.manager._card_stock_count('grouped_data', self.manager.get_card_by_id(self.card_id)['data_content']))

    def test_purchase_two_items_consumes_two_bundles(self):
        live = self._prepare_delivery()
        config = self.manager.get_item_delivery_config(cookie_id='seller-1', item_id='item-1', user_id=1)
        self.assertEqual(2, config['variants'][0]['stock_count'])
        context = {}
        with patch('app.db_manager.db_manager', self.manager):
            first = asyncio.run(live._auto_delivery('item-1', 'Subscription', 'order-1', 'buyer-1', delivery_context=context, requested_item_quantity=2))
            second = asyncio.run(live._auto_delivery('item-1', 'Subscription', 'order-1', 'buyer-1', delivery_context=context))
        self.assertEqual([BUNDLE, SECOND], [first, second])
        self.assertEqual(2, context['batch_data_required_count'])

    def test_delivery_without_context_sends_one_bundle(self):
        live = self._prepare_delivery()
        with patch('app.db_manager.db_manager', self.manager):
            result = asyncio.run(live._auto_delivery('item-1', 'Subscription', 'order-1', 'buyer-1'))
        self.assertEqual(BUNDLE, result)

    def test_card_api_round_trip_preserves_grouped_content(self):
        from fastapi.testclient import TestClient
        from app.reply_server import app, get_current_user

        previous = app.dependency_overrides.copy()
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(previous)))
        app.dependency_overrides[get_current_user] = lambda: {'user_id': 1, 'username': 'owner'}
        client = TestClient(app)
        with patch('app.db_manager.db_manager', self.manager):
            response = client.post('/cards', json={'name': 'API bundles', 'type': 'grouped_data', 'data_content': BUNDLE})
            self.assertEqual(200, response.status_code, response.text)
            card_id = response.json()['id']
            response = client.get(f'/cards/{card_id}')
            self.assertEqual('grouped_data', response.json()['type'])
            self.assertEqual(BUNDLE, response.json()['data_content'])
            content = join_card_inventory('grouped_data', [BUNDLE, SECOND])
            response = client.put(f'/cards/{card_id}', json={'data_content': content})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(content, client.get(f'/cards/{card_id}').json()['data_content'])
            self.assertEqual([BUNDLE, SECOND], self.manager.consume_batch_data_batch(card_id, 2))


class CardTypeMigrationTests(unittest.TestCase):
    def test_old_schema_preserves_fields_ids_bindings_and_sequence(self):
        manager = object.__new__(DBManager)
        manager.conn = sqlite3.connect(':memory:')
        self.addCleanup(manager.conn.close)
        cursor = manager.conn.cursor()
        cursor.executescript("""
            CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, type TEXT CHECK (type IN ('api', 'text', 'data', 'image')), data_content TEXT, delivery_template TEXT, custom_field TEXT);
            CREATE INDEX cards_name_idx ON cards(name);
            CREATE TRIGGER cards_mark_insert AFTER INSERT ON cards BEGIN UPDATE cards SET custom_field = 'trigger kept' WHERE id = NEW.id; END;
            CREATE TABLE bindings (card_id INTEGER REFERENCES cards(id));
            INSERT INTO cards VALUES (7, 'old', 'data', 'CODE-1', 'template', 'keep');
            INSERT INTO bindings VALUES (7);
            INSERT INTO cards (id, name, type) VALUES (50, 'deleted', 'text');
            DELETE FROM cards WHERE id = 50;
            UPDATE cards SET custom_field = 'keep' WHERE id = 7;
        """)
        manager._update_cards_table_constraints(cursor)
        manager._update_cards_table_constraints(cursor)  # Idempotent.
        self.assertEqual((7, 'old', 'data', 'CODE-1', 'template', 'keep'), cursor.execute('SELECT * FROM cards').fetchone())
        self.assertEqual((7,), cursor.execute('SELECT * FROM bindings').fetchone())
        self.assertEqual([], cursor.execute('PRAGMA foreign_key_check').fetchall())
        self.assertIsNotNone(cursor.execute("SELECT name FROM sqlite_master WHERE name = 'cards_name_idx'").fetchone())
        cursor.execute("INSERT INTO cards (name, type) VALUES ('new', 'grouped_data')")
        self.assertEqual(51, cursor.lastrowid)
        self.assertEqual(('trigger kept',), cursor.execute('SELECT custom_field FROM cards WHERE id = 51').fetchone())

    def test_failed_migration_restores_original_table(self):
        manager = object.__new__(DBManager)
        manager.conn = sqlite3.connect(':memory:')
        self.addCleanup(manager.conn.close)
        cursor = manager.conn.cursor()
        cursor.executescript("""
            CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT CHECK (type IN ('data')), data_content TEXT);
            INSERT INTO cards (type, data_content) VALUES ('data', 'keep');
        """)
        manager.conn.set_authorizer(lambda action, arg1, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE and arg1 == 'cards' else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            manager._update_cards_table_constraints(cursor)
        manager.conn.set_authorizer(None)
        self.assertEqual((1, 'data', 'keep'), cursor.execute('SELECT * FROM cards').fetchone())
        self.assertIsNone(cursor.execute("SELECT name FROM sqlite_master WHERE name = 'cards_inventory_migration'").fetchone())


if __name__ == '__main__':
    unittest.main()
