"""引擎编排：定义登记 -> 编译 -> 规划 -> 缓存判定 -> 执行 -> 持久化。

入口：python3 -m metric_scope_registry.engine --input <JSON> --state <目录> --output <JSON>
输出固定包含 values、plans、lineage、cache_effects、rejected 五个部分。
"""

import argparse
import sys
import time

from .compiler import (Compiler, canonical_digest, lineage_view,
                       normalize_definitions)
from .executor import Executor, render_value
from .io import read_json, write_json
from .schema import (DIMENSION_OWNERS, ScopeError, validate_predicate)
from .store import StateStore, ingest_document

PARTITION_FAMILIES = {'requests', 'calls', 'user_days'}


def run(input_path, state_dir, output_path):
    document = read_json(input_path)
    _validate_input(document)
    store = StateStore(state_dir)
    run_id = document.get('run_id') or _generate_run_id(document)

    replay = document.get('replay')
    if replay:
        result = _run_replay(store, document, run_id)
    else:
        result = _run_fresh(store, document, run_id)

    write_json(output_path, result)
    return result


def _run_fresh(store, document, run_id):
    definitions = document['metric_definitions']
    definitions_digest = store.register_definitions(definitions)
    registry = normalize_definitions(definitions)

    partition_digests, table_revisions = ingest_document(store, document)
    query = document['query']
    group_by, window = query['group_by'], _window(query)
    requested = list(query['metrics'])

    compiler = Compiler(registry, pinned=_pins(query))
    rejected = []
    plans, lineage, nodes = _plan(compiler, requested, group_by, rejected)

    executor = Executor(store, partition_digests, table_revisions,
                        expected_partition_keys=set(partition_digests))
    values, cache_effects = _execute(
        store, executor, nodes, plans, group_by, window, query,
        definitions_digest, partition_digests, table_revisions, rejected)

    result = _assemble(run_id, values, plans, lineage, cache_effects, rejected,
                       query, definitions_digest, partition_digests, table_revisions,
                       replay_of=None)
    store.record_run(run_id, _run_record(document, result))
    return result


def _run_replay(store, document, run_id):
    replay = document['replay']
    source = _resolve_replay_run(store, replay)
    definitions_bundle = store.load_definition_bundle(source['definitions_digest'])
    registry = normalize_definitions(definitions_bundle['definitions'])
    query = document.get('query') or source['query']
    group_by, window = query['group_by'], _window(query)

    pinned_digests = {}
    for key, digest in source['partition_digests'].items():
        if not store.snapshots.joinpath(digest.split(':', 1)[1] + '.json').exists():
            raise ScopeError('replay_unavailable',
                             '回放所需分区快照已不在状态目录：' + key)
        pinned_digests[key] = digest
    table_revisions = source['table_revisions']

    compiler = Compiler(registry, pinned=_pins(query))
    rejected = []
    plans, lineage, nodes = _plan(compiler, list(query['metrics']), group_by, rejected)
    executor = Executor(store, pinned_digests, table_revisions,
                        expected_partition_keys=set(source['partition_digests']))
    values, cache_effects = _execute(
        store, executor, nodes, plans, group_by, window, query,
        source['definitions_digest'], pinned_digests, table_revisions, rejected,
        replay=True)

    result = _assemble(run_id, values, plans, lineage, cache_effects, rejected,
                       query, source['definitions_digest'], pinned_digests,
                       table_revisions, replay_of=source['run_id'])
    store.record_run(run_id, _run_record(document, result))
    return result


# ---- 规划：编译期决定每个指标在请求维度上可定义还是不可比较 ----
def _plan(compiler, requested, group_by, rejected):
    plans, lineage, nodes = [], {}, {}
    for reference in requested:
        name, wanted = _split_reference(reference)
        try:
            if wanted is not None:
                # 同一查询闭包内，同名指标先锁定的版本与显式版本必须一致。
                locked = compiler.pinned.get(name)
                if locked is not None and locked != wanted:
                    raise ScopeError(
                        'version_mixing',
                        '指标 %s 在同一查询中混用版本 %s 与 %s'
                        % (name, locked, wanted))
                compiler.pinned[name] = wanted
            node = compiler.compile_ref(name, wanted)
        except ScopeError as error:
            rejected.append(_rejection(reference, error.code, error.message))
            continue
        label = _reference_label(reference)
        nodes[label] = node
        lineage[node['metric']] = lineage_view(node)
        dimensions = []
        incompatible = []
        for field in group_by:
            try:
                resolution = _check_dimension(node, field)
                dimensions.append(resolution)
            except ScopeError as error:
                incompatible.append({'dimension': field, 'code': error.code,
                                     'message': error.message})
        if incompatible:
            for item in incompatible:
                rejected.append(_rejection(
                    label, item['code'],
                    '指标 %s 在维度 %s 上不可比较，输出 null 而不是零：%s'
                    % (name, item['dimension'], item['message'])))
            plans.append({'metric': label, 'status': 'incomparable',
                          'grain': node['grain'], 'dimensions': dimensions,
                          'blocked_by': [item['dimension'] for item in incompatible]})
        else:
            plans.append({'metric': label, 'status': 'planned',
                          'grain': node['grain'], 'state_family': node['family'],
                          'time_anchor': node['time_anchor_table'] + '.day',
                          'dimensions': dimensions,
                          'dependencies': node['dependencies'],
                          'bridges': [node['bridge']] if node.get('bridge') else []})
    return plans, lineage, nodes


def _check_dimension(node, field):
    if field not in DIMENSION_OWNERS:
        raise ScopeError('unknown_dimension', '未登记维度：' + field)
    from .schema import find_many_to_one_path
    owners = DIMENSION_OWNERS[field]
    source = node['table']
    if source in owners:
        return {'dimension': field, 'via': source, 'join_path': [source]}
    for owner in owners:
        path = find_many_to_one_path(source, owner)
        if path is not None:
            return {'dimension': field, 'via': owner, 'join_path': path,
                    'join_kind': 'declared_many_to_one'}
    bridge = node.get('bridge')
    if bridge is not None and bridge['to'] in owners:
        return {'dimension': field, 'via': bridge['to'],
                'join_path': [source, bridge['to']],
                'join_kind': 'declared_bridge:' + bridge['rule']}
    raise ScopeError(
        'dimension_unavailable',
        '指标粒度 %s 到维度 %s 没有声明过的连接路径' % (source, field))


# ---- 执行与缓存 ----
def _execute(store, executor, nodes, plans, group_by, window, query,
             definitions_digest, partition_digests, table_revisions,
             rejected, replay=False):
    values = []
    cache_effects = []
    planned = {item['metric']: item for item in plans if item['status'] == 'planned'}
    for reference, node in nodes.items():
        if reference not in planned:
            values.append({'metric': reference, 'status': 'incomparable',
                           'groups': []})
            continue
        needed = sorted(executor._required_partitions(node, window))
        cache_key = _cache_key(node, group_by, window, query,
                               definitions_digest, needed, partition_digests)
        cached = store.cache_lookup(cache_key)
        effect_base = {
            'metric': reference,
            'cache_key': cache_key,
            'partition_versions': {key: partition_digests[key] for key in needed},
        }
        if cached is not None and cached['quantile_q'] == query.get('quantile_q', 0.95):
            cache_effects.append(dict(effect_base, status='hit',
                                      detail='定义、查询形状与分区摘要均未变化'))
            values.append(cached['result'])
            continue
        try:
            groups, _ = executor.run_plan(node, group_by, window)
        except ScopeError as error:
            rejected.append(_rejection(reference, error.code, error.message))
            values.append({'metric': reference, 'status': 'rejected', 'groups': []})
            cache_effects.append(dict(effect_base, status='bypassed',
                                      detail='执行被拒绝，不写缓存：' + error.code))
            continue
        rendered = _render_groups(reference, node, groups, group_by, query)
        entry = {'cache_key': cache_key, 'quantile_q': query.get('quantile_q', 0.95),
                 'definitions_digest': definitions_digest,
                 'partition_digests': {key: partition_digests[key] for key in needed},
                 'result': rendered}
        store.cache_put(cache_key, entry)
        cache_effects.append(dict(
            effect_base, status='stored' if cached is None else 'recomputed',
            detail=('新计算并写入缓存' if cached is None
                    else '旧缓存键不再匹配（定义或分区已修订），重算覆盖')))
        values.append(rendered)
    return values, cache_effects


def _render_groups(reference, node, groups, group_by, query):
    rendered_groups = []
    q = query.get('quantile_q', 0.95)
    for key, state in sorted(groups.items(), key=lambda item: item[0]):
        rendered_groups.append({
            'dimensions': dict(zip(group_by, key)),
            'state_family': state['family'],
            'value': render_value(node, state, q),
            'state': _state_brief(state),
        })
    return {'metric': reference, 'status': 'ok', 'groups': rendered_groups}


def _state_brief(state):
    """输出可合并中间状态，证明汇总走的是状态合并而非数字相加。"""
    family = state['family']
    if family == 'scalar':
        return {'family': family, 'value': state['value']}
    if family == 'hash_set':
        return {'family': family, 'member_count': len(state['members']),
                'members': state['members']}
    if family == 'sample':
        return {'family': family, 'sample_size': len(state['values'])}
    return {'family': family,
            'numerator': _state_brief(state['numerator']),
            'denominator': _state_brief(state['denominator'])}


def _cache_key(node, group_by, window, query, definitions_digest, needed, digests):
    payload = {
        'metric': node['metric'],
        'version': node['version'],
        'expr_digest': node.get('expr_digest'),
        'group_by': group_by,
        'window': list(window or []),
        'quantile_q': query.get('quantile_q', 0.95),
        'definitions_digest': definitions_digest,
        'partitions': {key: digests[key] for key in needed},
    }
    key = canonical_digest(payload)
    return key


# ---- 输入校验与工具 ----
def _validate_input(document):
    if not isinstance(document, dict):
        raise ScopeError('bad_input', '输入必须是 JSON 对象')
    if document.get('replay'):
        if 'query' not in document and not isinstance(document.get('replay'), dict):
            raise ScopeError('bad_input', 'replay 必须指向已存在的运行')
        return
    for field in ('tables', 'metric_definitions', 'query'):
        if field not in document:
            raise ScopeError('bad_input', '输入缺少字段：' + field)
    query = document['query']
    if not isinstance(query.get('metrics'), list) or not query['metrics']:
        raise ScopeError('bad_input', 'query.metrics 必须是非空数组')
    if not isinstance(query.get('group_by', []), list):
        raise ScopeError('bad_input', 'query.group_by 必须是数组')
    for definition in document['metric_definitions']:
        if not isinstance(definition, dict) or 'expr' not in definition:
            raise ScopeError('bad_definition', '指标定义必须包含 expr')
        for predicate in definition['expr'].get('filters', ()):
            validate_predicate(predicate)
    for correction in document.get('corrections', ()):
        if correction.get('table') not in PARTITION_FAMILIES:
            raise ScopeError('bad_correction', '修订指向未登记的表')


def _window(query):
    window = query.get('window')
    if not window:
        return None
    start = window.get('start')
    end = window.get('end')
    if start and end and start > end:
        raise ScopeError('bad_input', '时间窗口起点晚于终点')
    return start, end


def _pins(query):
    # 版本锁定由 _plan 逐个引用处理并检测混用；这里不提前写入，避免后写覆盖先写。
    return {}


def _split_reference(reference):
    if isinstance(reference, str):
        return reference, None
    if isinstance(reference, dict) and 'metric' in reference:
        return reference['metric'], reference.get('version')
    raise ScopeError('bad_input', '无法识别的指标引用：' + str(reference))


def _reference_label(reference):
    if isinstance(reference, str):
        return reference
    name, wanted = _split_reference(reference)
    return name if wanted is None else '%s@v%s' % (name, wanted)


def _rejection(reference, code, message):
    metric = reference if isinstance(reference, str) else reference.get('metric', str(reference))
    return {'metric': metric, 'code': code, 'message': message}


def _resolve_replay_run(store, replay):
    if isinstance(replay, dict) and replay.get('run_id'):
        return store.load_run(replay['run_id'])
    latest = store.latest_run()
    if latest is None:
        raise ScopeError('replay_unavailable', '状态目录中没有可回放的运行')
    return latest


def _assemble(run_id, values, plans, lineage, cache_effects, rejected,
              query, definitions_digest, partition_digests, table_revisions,
              replay_of):
    return {
        'run_id': run_id,
        'replay_of': replay_of,
        'query': query,
        'definitions_digest': definitions_digest,
        'table_revisions': table_revisions,
        'partition_versions': partition_digests,
        'values': values,
        'plans': plans,
        'lineage': lineage,
        'cache_effects': cache_effects,
        'rejected': rejected,
    }


def _run_record(document, result):
    return {
        'run_id': result['run_id'],
        'query': result['query'],
        'definitions_digest': result['definitions_digest'],
        'partition_digests': result['partition_versions'],
        'table_revisions': result['table_revisions'],
    }


def _generate_run_id(document):
    return 'run-' + str(int(time.time() * 1000)) + '-' + \
        canonical_digest(document).split(':', 1)[1][:10]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='带口径登记、版本化定义与选择性缓存失效的指标引擎。')
    parser.add_argument('--input', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        run(args.input, args.state, args.output)
    except ScopeError as error:
        print('处理失败[%s]：%s' % (error.code, error.message), file=sys.stderr)
        return 2
    except (KeyError, TypeError, OSError) as error:
        print('处理失败：' + str(error), file=sys.stderr)
        return 2
    print('结果已保存：' + args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
