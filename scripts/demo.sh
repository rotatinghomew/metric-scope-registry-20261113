#!/usr/bin/env bash
# 端到端演练：同一套版本化指标定义跑通按天、跨天、修订与旧版本回放。
# 用法：bash scripts/demo.sh [状态目录] [输出目录]
set -euo pipefail

STATE="${1:-/tmp/msr-demo-state}"
OUT="${2:-/tmp/msr-demo-out}"
rm -rf "$STATE" "$OUT"
mkdir -p "$STATE" "$OUT"

run() {
  python3 -m metric_scope_registry.engine \
    --input "fixtures/$1" --state "$STATE" --output "$OUT/$2"
}

echo '== 1. 按天明细查询（同一定义同时出请求成功率、调用成功率、去重用户、p95） =='
run query_daily.json daily.json

echo '== 2. 跨天汇总查询（用户集合取并集，证明月活不是日活相加） =='
run query_crossday.json crossday.json

echo '== 3. 混合分组（app/firmware）：不可比较的指标标注 null 而不是零 =='
run query_mixed.json mixed.json

echo '== 4. 修订一条调用结果 c4-0：口径重编译，选择性缓存失效 =='
run query_daily_corrected.json daily_corrected.json

echo '== 5. 旧版本回放：不可变分区快照复现修订前结果 =='
run replay_daily.json replay.json

python3 - "$OUT" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
daily = json.loads((out / 'daily.json').read_text(encoding='utf-8'))
cross = json.loads((out / 'crossday.json').read_text(encoding='utf-8'))
mixed = json.loads((out / 'mixed.json').read_text(encoding='utf-8'))
corrected = json.loads((out / 'daily_corrected.json').read_text(encoding='utf-8'))
replay = json.loads((out / 'replay.json').read_text(encoding='utf-8'))


def value(doc, metric, dimensions):
    for entry in doc['values']:
        if entry['metric'] != metric:
            continue
        for group in entry['groups']:
            if group['dimensions'] == dimensions:
                return group['value']
    return None


print('\n--- 证据 A：跨天用户数是并集，不是相加 ---')
daily_sum = sum(group['value'] for entry in daily['values']
                if entry['metric'] == 'active_users' for group in entry['groups'])
cross_entry = next(group for entry in cross['values']
                   if entry['metric'] == 'active_users' for group in entry['groups'])
print('分日 active_users 相加 =', daily_sum,
      '；跨天 distinct =', cross_entry['value'],
      '；并集成员 =', cross_entry['state']['members'])
assert daily_sum == 24 and cross_entry['value'] == 12

print('\n--- 证据 B：请求成功与调用成功是两个合法但独立的口径 ---')
for day in ('2026-11-10', '2026-11-11'):
    print(day,
          'request_completion =', round(value(daily, 'request_completion', {'day': day}), 4),
          'call_completion =', round(value(daily, 'call_completion', {'day': day}), 4))

print('\n--- 证据 C：混合分组下不可比较而非零 ---')
print('rejected 代码：', sorted({item['code'] for item in mixed['rejected']}))
print('active_users 状态：',
      next(item['status'] for item in mixed['values'] if item['metric'] == 'active_users'))

print('\n--- 证据 D：修订 c4-0 后的选择性失效 ---')
for effect in corrected['cache_effects']:
    print(' ', effect['metric'], '->', effect['status'])
print('call_completion 11-10：',
      round(value(daily, 'call_completion', {'day': '2026-11-10'}), 4),
      '->',
      round(value(corrected, 'call_completion', {'day': '2026-11-10'}), 4))
print('request_completion 全程未变（请求事实未修订）：',
      value(daily, 'request_completion', {'day': '2026-11-10'})
      == value(corrected, 'request_completion', {'day': '2026-11-10'}))

print('\n--- 证据 E：旧版本回放 ---')
print('replay_of =', replay['replay_of'])
print('回放分区版本与首跑一致：',
      replay['partition_versions'] == daily['partition_versions'])
print('回放值与首跑一致：', replay['values'] == daily['values'])
print('回放全部命中历史缓存：',
      all(effect['status'] == 'hit' for effect in replay['cache_effects']))
print('\n全部断言通过。')
PY
