"""Durable order gate shared by message, reconciliation and manual delivery.

Never hold a SQLite transaction across an await. An interrupted active/prepared
attempt requires operator review: messages and external card APIs are not
transactional, so automatically taking over could consume a second card.
"""
import uuid


def delivery_block_reason(db, order_id, cookie_id):
    with db.lock:
        row = db.conn.execute(
            "SELECT cookie_id, system_shipped, order_status FROM orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if not row or str(row[0]) != str(cookie_id):
            return "订单不存在或账号不匹配"
        if row[1] or row[2] in ("shipped", "completed"):
            return "订单已发货，禁止再次发送卡券"
        attempt = db.conn.execute(
            "SELECT state FROM order_delivery_attempts WHERE order_id=?", (order_id,)
        ).fetchone()
        if attempt and attempt[0] != "retryable":
            return "订单正在发货、已发送或需要人工核对，禁止重复扣库存"
        if db.conn.execute(
            "SELECT 1 FROM card_shipments WHERE order_id=? LIMIT 1", (order_id,)
        ).fetchone():
            return "订单已有卡密出库记录，请核对原卡密，禁止再次扣库存"
    return ""


class DeliveryAttempt:
    def __init__(self, db, order_id, cookie_id, *, status_only=False, require_pending=False):
        self.db, self.order_id, self.cookie_id = db, order_id, cookie_id
        self.status_only = status_only
        self.require_pending = require_pending
        self.token = uuid.uuid4().hex
        self.acquired = False
        self.reason = ""
        self.previous = None

    def __enter__(self):
        with self.db.lock:
            try:
                self.db.conn.execute("BEGIN IMMEDIATE")
                self.previous = self.db.conn.execute(
                    "SELECT state, phase FROM order_delivery_attempts WHERE order_id=?",
                    (self.order_id,),
                ).fetchone()
                if self.status_only:
                    # Confirming platform status remains possible after cards were sent.
                    self.reason = ("订单正在发货，请稍后确认状态" if self.previous
                                   and self.previous[0] == "active" else "")
                else:
                    self.reason = delivery_block_reason(self.db, self.order_id, self.cookie_id)
                    if not self.reason and self.require_pending:
                        status = self.db.conn.execute(
                            "SELECT order_status FROM orders WHERE order_id=?", (self.order_id,)
                        ).fetchone()
                        if not status or status[0] != "pending_ship":
                            self.reason = "订单已不处于待发货状态"
                if not self.reason:
                    self.db.conn.execute("""
                        INSERT INTO order_delivery_attempts(order_id, cookie_id, token, state, phase)
                        VALUES (?, ?, ?, 'active', 'ready')
                        ON CONFLICT(order_id) DO UPDATE SET token=excluded.token,
                        state='active', phase='ready', updated_at=CURRENT_TIMESTAMP
                    """, (self.order_id, self.cookie_id, self.token))
                    self.acquired = True
                self.db.conn.commit()
            except BaseException:
                self.db.conn.rollback()
                raise
        return self

    def _update(self, state=None, phase=None):
        with self.db.lock:
            cursor = self.db.conn.execute("""
                UPDATE order_delivery_attempts SET state=COALESCE(?,state),
                phase=COALESCE(?,phase), updated_at=CURRENT_TIMESTAMP
                WHERE order_id=? AND token=?
            """, (state, phase, self.order_id, self.token))
            if state == "sent" and cursor.rowcount:
                self.db.conn.execute("""
                    UPDATE orders SET system_shipped=1, version=version+1,
                    updated_at=CURRENT_TIMESTAMP WHERE order_id=? AND cookie_id=?
                """, (self.order_id, self.cookie_id))
            self.db.conn.commit()

    def prepared(self):
        self._update(phase="prepared")

    def sent(self):
        self._update(state="sent", phase="prepared")

    def __exit__(self, *exc):
        if not self.acquired:
            return
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT state, phase FROM order_delivery_attempts WHERE order_id=? AND token=?",
                (self.order_id, self.token),
            ).fetchone()
            if not row or row[0] == "sent":
                return
            if self.status_only:
                state, phase = self.previous or ("retryable", "ready")
            else:
                consumed = self.db.conn.execute(
                    "SELECT 1 FROM card_shipments WHERE order_id=? LIMIT 1", (self.order_id,)
                ).fetchone()
                state, phase = ("blocked" if consumed or row[1] == "prepared" else "retryable"), row[1]
            self._update(state=state, phase=phase)


def mark_external_acquisition(db, order_id):
    """Call before an external card API may allocate inventory (even on timeout)."""
    with db.lock:
        db.conn.execute("""
            UPDATE order_delivery_attempts SET phase='prepared', updated_at=CURRENT_TIMESTAMP
            WHERE order_id=? AND state='active'
        """, (order_id,))
        db.conn.commit()
