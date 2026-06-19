"""
诊断：在导入流程中，after_snapshot_json 到底存了什么？
直接构造一个带时区的时间，调用 _serialize_order_snapshot 看看结果
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from datetime import datetime, timezone
from main import _serialize_order_snapshot, _parse_iso
from database import SessionLocal, WorkOrder, init_db

init_db()
db = SessionLocal()

# 找一个工单
order = db.query(WorkOrder).first()
if order:
    print(f"原始工单 {order.id}:")
    snap1 = _serialize_order_snapshot(order)
    print(f"  reported_at: {snap1['reported_at']!r}")
    print(f"  assigned_at: {snap1['assigned_at']!r}")

    # 手动设置带时区的时间（模拟从带 Z 的快照解析后设置）
    z_time = _parse_iso('2026-06-25T01:00:00.000Z')
    print(f"\n_parse_iso('2026-06-25T01:00:00.000Z') = {z_time}, tzinfo={z_time.tzinfo}")

    order.road_close_start = z_time
    order.road_close_end = _parse_iso('2026-06-25T03:00:00.000Z')
    order.started_at = _parse_iso('2026-06-20T02:00:00.123456Z')
    order.assigned_at = _parse_iso('2026-06-19T18:26:09.015713Z')
    order.reported_at = _parse_iso('2026-06-19T18:26:08.946538Z')

    # flush 之后立刻序列化（模拟当前代码行为）
    db.flush()
    snap_after = _serialize_order_snapshot(order)
    print(f"\nflush 后立刻序列化（内存对象，可能还带时区）:")
    print(f"  road_close_start: {snap_after['road_close_start']!r}")
    print(f"  road_close_end: {snap_after['road_close_end']!r}")
    print(f"  started_at: {snap_after['started_at']!r}")
    print(f"  assigned_at: {snap_after['assigned_at']!r}")
    print(f"  reported_at: {snap_after['reported_at']!r}")

    # 检查内存对象上的时间
    print(f"\n内存对象上的时间属性:")
    print(f"  road_close_start={order.road_close_start}, tzinfo={getattr(order.road_close_start, 'tzinfo', 'N/A')}")
    print(f"  started_at={order.started_at}, tzinfo={getattr(order.started_at, 'tzinfo', 'N/A')}")

    # commit + refresh 之后再序列化（模拟从 DB 读出）
    db.commit()
    db.refresh(order)
    snap_refresh = _serialize_order_snapshot(order)
    print(f"\ncommit+refresh 后序列化（从 DB 读出，时区已丢失）:")
    print(f"  road_close_start: {snap_refresh['road_close_start']!r}")
    print(f"  road_close_end: {snap_refresh['road_close_end']!r}")
    print(f"  started_at: {snap_refresh['started_at']!r}")
    print(f"  assigned_at: {snap_refresh['assigned_at']!r}")
    print(f"  reported_at: {snap_refresh['reported_at']!r}")

    print(f"\n内存对象上的时间属性（refresh后）:")
    print(f"  road_close_start={order.road_close_start}, tzinfo={getattr(order.road_close_start, 'tzinfo', 'N/A')}")
    print(f"  started_at={order.started_at}, tzinfo={getattr(order.started_at, 'tzinfo', 'N/A')}")

    # 关键比较：flush后序列化 vs refresh后序列化
    print(f"\n关键比较:")
    changed = []
    for k in ['road_close_start', 'road_close_end', 'started_at', 'assigned_at', 'reported_at']:
        if snap_after[k] != snap_refresh[k]:
            changed.append(k)
            print(f"  {k}: flush后={snap_after[k]!r} vs refresh后={snap_refresh[k]!r}")
    if changed:
        print(f"  ❌ 不一致字段: {', '.join(changed)} —— 这就是误判原因！")
    else:
        print(f"  ✅ 全部一致，_dt_iso 修复有效")

db.close()
