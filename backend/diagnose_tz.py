import sys
sys.path.insert(0, '.')
from database import SessionLocal, WorkOrder, init_db
from main import _parse_iso, _serialize_order_snapshot
from datetime import datetime, timezone

init_db()
db = SessionLocal()

order = db.query(WorkOrder).first()
if order:
    print(f'工单ID {order.id}:')
    tz1 = getattr(order.reported_at, 'tzinfo', None)
    tz2 = getattr(order.assigned_at, 'tzinfo', None)
    print(f'  reported_at={order.reported_at}, type={type(order.reported_at)}, tzinfo={tz1}')
    print(f'  assigned_at={order.assigned_at}, type={type(order.assigned_at)}, tzinfo={tz2}')
    
    snap = _serialize_order_snapshot(order)
    print(f'  serialized reported_at: {snap["reported_at"]}')
    print(f'  serialized assigned_at: {snap["assigned_at"]}')
    
    z_time = _parse_iso('2026-06-19T20:14:07.377800Z')
    print(f'\n设置 submitted_at 为带 Z 时间: {z_time}, tzinfo={z_time.tzinfo}')
    order.submitted_at = z_time
    db.commit()
    db.refresh(order)
    tz3 = getattr(order.submitted_at, 'tzinfo', None)
    print(f'从 DB 重新读出: submitted_at={order.submitted_at}, tzinfo={tz3}')
    snap2 = _serialize_order_snapshot(order)
    print(f'序列化后 submitted_at: {snap2["submitted_at"]}')

db.close()
