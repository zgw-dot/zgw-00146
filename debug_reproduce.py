"""
后端直连复现脚本 - 直接操作数据库和调用内部函数
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from database import SessionLocal, WorkOrder, RestoreBatch, RestoreBatchItem
from main import _serialize_order_snapshot, _check_order_modified_since
import json

db = SessionLocal()

# 找一个 overwrite 且成功但未撤销的批次子项
item = db.query(RestoreBatchItem).filter(
    RestoreBatchItem.action == 'overwrite',
    RestoreBatchItem.success == True,
    RestoreBatchItem.is_revoked == False,
    RestoreBatchItem.after_snapshot_json.isnot(None),
).order_by(RestoreBatchItem.id.desc()).first()

if not item:
    print("找不到符合条件的 overwrite 子项，需要先做一次覆盖恢复")
    exit(1)

print(f"找到批次子项: id={item.id}, order_no={item.order_no}, action={item.action}")
print(f"批次号: {item.batch.batch_no}")
print()

# 拿到工单
order = db.query(WorkOrder).filter(WorkOrder.id == item.order_id).first()
if not order:
    print(f"工单 {item.order_id} 不存在")
    exit(1)

print(f"工单当前状态: {order.status.value}")
print(f"工单 team_id: {order.team_id}")
print(f"工单 vehicle_id: {order.vehicle_id}")
print()

# 打印 after_snapshot_json
print("=== after_snapshot_json (保存的) ===")
after_snap = json.loads(item.after_snapshot_json)
for k, v in sorted(after_snap.items()):
    print(f"  {k}: {v!r}")

print()
print("=== _serialize_order_snapshot(order) (当前的) ===")
cur_snap = _serialize_order_snapshot(order)
for k, v in sorted(cur_snap.items()):
    print(f"  {k}: {v!r}")

print()
print("=== 差异字段 ===")
ignore = {"id", "order_no"}
changed = []
for key in cur_snap:
    if key in ignore:
        continue
    cv = cur_snap.get(key)
    sv = after_snap.get(key)
    if cv != sv:
        changed.append(key)
        print(f"  ❌ {key}: saved={sv!r} vs current={cv!r}")

if changed:
    print(f"\n❌ 发现 {len(changed)} 个差异字段: {changed}")
else:
    print(f"\n✅ 没有差异字段")

print()
print("=== 调用 _check_order_modified_since ===")
modified, reason = _check_order_modified_since(db, order, item.after_snapshot_json)
print(f"是否被修改: {modified}")
print(f"原因: {reason}")

db.close()
