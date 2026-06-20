"""快速验证追溯记录是否正常写入"""
import requests
import sys

BASE = "http://127.0.0.1:8005/api"
s = requests.Session()
s.headers["X-User-Id"] = "1"  # admin

# 找最新的几张工单
orders = s.get(f"{BASE}/orders").json()
orders.sort(key=lambda x: x["id"], reverse=True)

print(f"\n=== 最近 5 张工单的追溯记录统计 ===\n")
for o in orders[:5]:
    r = s.get(f"{BASE}/orders/{o['id']}/trace")
    if r.status_code != 200:
        print(f"工单 #{o['id']} {o['order_no']}: HTTP {r.status_code} {r.text[:80]}")
        continue
    t = r.json()
    print(f"工单 #{o['id']} {o['order_no']} [{o['status']}]")
    print(f"  摘要: {t['summary']}")
    print(f"  can_see_batch_detail={t['can_see_batch_detail']}")
    for e in t["events"]:
        flag = "OK" if e["success"] else "FAIL"
        batch = f" [批次{e['batch_no']}]" if e.get("batch_no") else ""
        fields = f" 变更={','.join(e['changed_fields'][:5])}" if e.get("changed_fields") else ""
        fail = f" 失败={e['fail_reason'][:40]}" if not e["success"] and e.get("fail_reason") else ""
        print(f"    [{flag}] [{e['event_label']}] {e['from_status'] or '创'}->{e['to_status'] or '删'}{batch}{fields}{fail}")
    print()

# 测下导出
if orders:
    oid = orders[0]["id"]
    rj = s.get(f"{BASE}/orders/{oid}/trace/export/json").json()
    print(f"JSON 导出 OK, events={len(rj.get('events', []))}")
    rc = s.get(f"{BASE}/orders/{oid}/trace/export/csv")
    lines = rc.content.decode("utf-8-sig").strip().split("\n")
    print(f"CSV 导出 OK, lines={len(lines)}")
    print()
    print("CSV 前 5 行:")
    for l in lines[:5]:
        print("  " + l[:120])

# 权限测试：巡查员(3)看巡查员老张(2)上报的工单
print("\n=== 权限测试: 巡查员小李(id=3)看老张上报的工单 ===")
s3 = requests.Session()
s3.headers["X-User-Id"] = "3"
lao_zhang_order = None
for o in orders:
    if o.get("reporter_id") == 2:
        lao_zhang_order = o
        break
if lao_zhang_order:
    r = s3.get(f"{BASE}/orders/{lao_zhang_order['id']}/trace")
    print(f"  小李看老张工单 #{lao_zhang_order['id']} -> HTTP {r.status_code}（期望 403）")
print()
print("DONE - 快速验证完成")
