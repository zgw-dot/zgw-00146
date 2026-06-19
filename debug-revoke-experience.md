# Debug Session: revoke-experience
Status: [OPEN]
Created: 2026-06-20

## Session ID
`revoke-experience`

## Hypotheses

### H1: Revocation results are not fully persisted per-order
- **Observation**: The `RestoreBatchItem` has `is_revoked`, `revoked_at`, `revoke_failed_reason` but lacks structured storage for:
  - Changed fields during revocation
  - Success reason message
  - Attribution for which fields caused failure (manual modification check)
- **Impact**: After refresh/restart, admins only see "成功 X 条，失败 Y 条" without per-order details

### H2: Frontend toast/popup messages are inconsistent with API responses
- **Observation**: Frontend toast at line 1551 only shows aggregate counts: `成功 ${result.revoked} 条，失败 ${result.failed} 条`
- **Impact**: Admins can't immediately see which orders failed and why

### H3: Revocation failure reasons don't clearly attribute changed fields
- **Observation**: `_check_order_modified_since` returns a string like "工单已被人工修改，变更字段: team_id, vehicle_id" but doesn't store structured field list
- **Impact**: Batch detail shows a long string but can't format or filter by specific changed fields

### H4: Edge cases lack stable feedback
- **Observation**: Empty before recovery, manual changes after recovery, duplicate revocation, refresh - all have varying display quality
- **Impact**: Inconsistent UX where some cases show detailed info, others show empty or generic messages

### H5: RevokeBatchOut lacks per-order changed_fields and attribution
- **Observation**: `RevokeBatchItemOut` has `order_no`, `action`, `success`, `reason` but no `changed_fields` or attribution details
- **Impact**: API consumers (including frontend) can't distinguish between batch stats, single reasons, and failure attribution

## Investigation Plan
1. Add database fields for structured revocation result storage
2. Update revoke API to persist per-order results with changed fields
3. Ensure consistent message formatting across all display layers
4. Update frontend to show detailed, consistent revocation information
5. Add regression tests for all scenarios

## Log Collection Points
- `revoke_restore_batch` function: per-order processing
- `_check_order_modified_since` function: field change detection
- Frontend `confirmRevokeBatch` function: toast and modal updates
- Database reads for batch details after restart
