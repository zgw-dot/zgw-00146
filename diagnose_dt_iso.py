import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from datetime import datetime, timezone

# 从 main.py 复制 _serialize_order_snapshot 的 _dt_iso 逻辑
def _dt_iso(val):
    if not val:
        return ''
    if hasattr(val, 'tzinfo') and val.tzinfo is not None:
        val = val.replace(tzinfo=None)
    return val.isoformat()

# 测试：带时区的时间
z_time = datetime(2026, 6, 25, 1, 0, 0, 0, tzinfo=timezone.utc)
print(f'带时区时间: {z_time}')
print(f'  tzinfo: {z_time.tzinfo}')
print(f'  isoformat(): {z_time.isoformat()}')
print(f'  _dt_iso(): {_dt_iso(z_time)}')
print(f'  _dt_iso 是否去掉了时区? {_dt_iso(z_time) == "2026-06-25T01:00:00"}')

# 测试：不带时区的时间
naive_time = datetime(2026, 6, 25, 1, 0, 0, 0)
print(f'\n不带时区时间: {naive_time}')
print(f'  tzinfo: {naive_time.tzinfo}')
print(f'  isoformat(): {naive_time.isoformat()}')
print(f'  _dt_iso(): {_dt_iso(naive_time)}')

# 比较
print(f'\n两者 _dt_iso 结果相等? {_dt_iso(z_time) == _dt_iso(naive_time)}')
