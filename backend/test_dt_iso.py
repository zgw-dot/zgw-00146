import sys
sys.path.insert(0, '.')
from datetime import datetime, timezone

def _dt_iso(val):
    if not val:
        return ''
    if hasattr(val, 'tzinfo') and val.tzinfo is not None:
        val = val.replace(tzinfo=None)
    return val.isoformat()

# 测试带时区
z_time = datetime(2026, 6, 19, 20, 14, 7, 377800, tzinfo=timezone.utc)
print(f'带时区时间 isoformat: {z_time.isoformat()}')
print(f'_dt_iso 结果: {_dt_iso(z_time)}')

# 测试不带时区
naive_time = datetime(2026, 6, 19, 20, 14, 7, 377800)
print(f'不带时区 isoformat: {naive_time.isoformat()}')
print(f'_dt_iso 结果: {_dt_iso(naive_time)}')

# 测试两者相等
print(f'两者 _dt_iso 结果相等? {_dt_iso(z_time) == _dt_iso(naive_time)}')

# 测试空值
print(f'空值: {repr(_dt_iso(None))}')
