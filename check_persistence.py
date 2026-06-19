import requests, os

BASE = 'http://127.0.0.1:8000/api'
H = {'X-User-Id': '1', 'Content-Type': 'application/json'}

r = requests.get(f'{BASE}/restore-batches', headers=H)
batches = r.json()
print(f'批次数量: {len(batches)}')
for b in batches:
    print(f'  [{b["status"]:10s}] {b["batch_no"]} | 成功{b["imported_count"]}条 | 撤销{b.get("revoked_count", 0)}条 | {b["operator_name"]}')

print()
db_path = 'backend/data/app.db'
if os.path.exists(db_path):
    size = os.path.getsize(db_path)
    print(f'数据库文件: {db_path}')
    print(f'文件大小: {size} bytes ({size/1024:.1f} KB)')
else:
    print(f'数据库文件不存在: {db_path}')
