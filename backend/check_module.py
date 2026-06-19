"""
直接调用服务进程的函数，检查 _serialize_order_snapshot 是否正确
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

# 让 Python 重新加载 main 模块（防止 import 缓存）
if 'main' in sys.modules:
    del sys.modules['main']

from datetime import datetime, timezone
from main import _serialize_order_snapshot, _parse_iso
from database import SessionLocal, WorkOrder, init_db

init_db()
db = SessionLocal()

# 找一个工单，手动设置带时区的时间
order = db.query(WorkOrder).filter(WorkOrder.id == 61).first()
if order:
    # 设置带时区的时间（模拟从带 Z 快照解析后写入内存对象的情况）
    z_time = _parse_iso('2099-06-25T01:00:00.000Z')
    print(f'_parse_iso 返回: {z_time}, tzinfo={z_time.tzinfo}')
    
    order.road_close_start = z_time
    
    # 直接序列化
    snap = _serialize_order_snapshot(order)
    print(f'序列化 road_close_start: {snap["road_close_start"]!r}')
    
    # 检查是否用了 _dt_iso
    import inspect
    from main import _serialize_order_snapshot as s
    print(f'\n_serialize_order_snapshot 源码:')
    print(inspect.getsource(s))

db.close()
