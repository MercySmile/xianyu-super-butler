"""Recover missed payment events using authoritative seller orders."""
import asyncio
import time

from loguru import logger

from app.delivery_attempt import delivery_block_reason


def request_delivery_reconciliation(cookie_id, order_ids=None, *, source="order_sync"):
    """Queue on the listener loop; HTTP refreshes must never use its socket on another loop."""
    from XianyuAutoAsync import XianyuLive
    live = XianyuLive.get_instance(cookie_id)
    loop = getattr(live, "_listener_loop", None)
    if not live or not loop or not loop.is_running() or loop.is_closed():
        return False
    ids = None if order_ids is None else {str(value) for value in order_ids if value}
    if ids == set():
        return False
    try:
        loop.call_soon_threadsafe(_enqueue, live, ids, source)
        return True
    except RuntimeError:
        return False


def _enqueue(live, ids, source):
    if not getattr(live, "_delivery_reconciliation_pending", None):
        live._delivery_reconciliation_pending = {"all": False, "ids": set(), "reminder": False}
    pending = live._delivery_reconciliation_pending
    pending["all"] |= ids is None
    pending["ids"].update(ids or ())
    pending["reminder"] |= source == "seller_reminder"
    task = getattr(live, "_delivery_reconciliation_task", None)
    if not task or task.done():
        live._delivery_reconciliation_task = live._create_tracked_task(_worker(live))


async def _worker(live):
    try:
        # Collapse bursts without blocking message reception; allow seller status to settle.
        await asyncio.sleep(2)
        while live._delivery_reconciliation_pending:
            pending = live._delivery_reconciliation_pending
            live._delivery_reconciliation_pending = None
            ids = None if pending["all"] else pending["ids"]
            await reconcile_pending_deliveries(live, ids, source="reminder" if pending["reminder"] else "order_sync")
            if pending["reminder"]:
                await asyncio.sleep(5)
                await reconcile_pending_deliveries(live, ids, source="reminder_recheck")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(f"【{live.cookie_id}】发货补偿检查失败: {type(exc).__name__}")
    finally:
        live._delivery_reconciliation_task = None


def _online(live):
    from XianyuAutoAsync import XianyuLive
    from app.cookie_manager import manager
    from utils import risk_control
    return (XianyuLive.get_instance(live.cookie_id) is live
            and not getattr(live, 'needs_relogin', False)
            and not getattr(live, 'authentication_pending', False)
            and bool(live.ws) and not live.ws.closed
            and (not manager or manager.get_cookie_status(live.cookie_id))
            and not risk_control.registry.get(live.cookie_id).is_blocked)


async def _find_chat(live, db, order):
    chat_id = order.get("chat_id") or db.find_chat_id_by_buyer(live.cookie_id, order["buyer_id"])
    if chat_id:
        return str(chat_id).split("@")[0]
    # For orders received while offline, resolve the actual participant pair, never
    # interpret the numeric conversation id from a status reminder as an order id.
    from app.xianyu_im import parse_conversation
    cursor = None
    for _ in range(5):
        body = await live.get_im_conversations(cursor, 100)
        for item in body.get("userConvs", []):
            raw = item.get("singleChatUserConversation", item)
            conv = raw.get("singleChatConversation") or {}
            pair = {str(conv.get(key) or "").split("@")[0] for key in ("pairFirst", "pairSecond")}
            if pair != {str(live.myid), str(order["buyer_id"])}:
                continue
            parsed = parse_conversation(raw, str(live.myid))
            if parsed and (not parsed["itemId"] or parsed["itemId"] == str(order["item_id"])):
                return parsed["cid"]
        next_cursor = body.get("nextCursor")
        if not body.get("hasMore") or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    return ""


async def reconcile_pending_deliveries(live, order_ids=None, *, source="order_sync"):
    from app.db_manager import db_manager as db
    from utils.seller_order_sync import _save_order, fetch_order_detail_direct
    from utils.order_status_rules import normalize_order_status
    from utils.xianyu_seller_api import XianyuSellerAPI, parse_sold_order

    if not _online(live):
        return
    if order_ids is None:
        api = XianyuSellerAPI(live.cookie_id, live.cookies_str)
        try:
            items = await api.iter_sold_orders(rows_per_page=50)
            order_ids = set()
            for item in items:
                parsed = parse_sold_order(item)
                if normalize_order_status("", parsed.get("status_text")) != "pending_ship":
                    continue
                order_id = parsed.get("order_id")
                existing = db.get_order_by_id(order_id) if order_id else None
                if existing and str(existing.get("cookie_id")) != str(live.cookie_id):
                    continue
                if order_id and _save_order(db, live.cookie_id, parsed):
                    order_ids.add(order_id)
        finally:
            await api.close()

    for order_id in sorted({str(value) for value in order_ids}):
        if not _online(live):
            return
        try:
            reason = delivery_block_reason(db, order_id, live.cookie_id)
            if reason:
                logger.info(f"【{live.cookie_id}】发货补偿[{source}] {order_id} 跳过: {reason}")
                continue
            old = db.get_order_by_id(order_id)
            # Always query fresh; cached pending_ship is insufficient for compensation.
            detail = await fetch_order_detail_direct(live.cookie_id, live.cookies_str, order_id)
            if (not detail or detail.get("order_status") != "pending_ship"
                    or str(detail.get("order_id")) != order_id
                    or not detail.get("buyer_id") or not detail.get("item_id")):
                logger.info(f"【{live.cookie_id}】发货补偿[{source}] {order_id} 未确认真实待发货订单")
                continue
            if any(old.get(key) and str(old[key]) != str(detail[key]) for key in ("buyer_id", "item_id")):
                logger.warning(f"【{live.cookie_id}】发货补偿 {order_id} 商品或买家信息冲突，停止")
                continue
            if not _save_order(db, live.cookie_id, detail):
                continue
            order = {**old, **detail}
            chat_id = await _find_chat(live, db, order)
            if not chat_id:
                logger.warning(f"【{live.cookie_id}】发货补偿 {order_id} 未找到买家会话，未扣库存")
                continue
            if not _online(live):
                return
            logger.info(f"【{live.cookie_id}】发货补偿[{source}] {order_id} 已确认付款，进入原发货校验")
            await live._handle_auto_delivery(
                live.ws, {"3": {"orderId": order_id}}, "买家", str(detail["buyer_id"]),
                str(detail["item_id"]), chat_id, time.strftime("%Y-%m-%d %H:%M:%S"),
                verified_order_detail=detail,
            )
        except Exception as exc:
            logger.error(f"【{live.cookie_id}】发货补偿 {order_id} 失败: {type(exc).__name__}")
