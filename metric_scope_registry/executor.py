"""执行器：同一套编译计划同时服务明细查询与汇总查询。

执行顺序固定为：分区装载 -> 分组累积可合并状态 -> 跨分区/跨组按状态族合并。
非可加量（hash_set/sample/ratio）从来不会被当成标量相加；缺分区或缺中间
状态时直接拒绝汇总，输出 null 而不是零。
"""

from collections import defaultdict

from .schema import EDGES, TABLES, ScopeError, find_many_to_one_path, row_matches
from .states import empty_state, finalize, make_state, merge_states, nearest_rank
from .store import partition_key, partition_spec


class Executor:
    def __init__(self, store, partition_digests, table_revisions,
                 expected_partition_keys=None):
        self.store = store
        # 当前目录下每个逻辑分区对应的内容摘要（可能指向历史快照）。
        self.partition_digests = partition_digests
        self.table_revisions = table_revisions
        # 本次数据装载时应存在的分区全集；回放/摘指针后若实际摘要缺失，
        # 就用它发现“明细或中间状态缺失”，而不是静默在残缺数据上汇总。
        self.expected_partition_keys = set(expected_partition_keys or
                                           partition_digests.keys())
        self._partition_cache = {}
        self._materialized = {}

    # ---- 分区装载与物化 ----
    def load_partition(self, spec):
        digest = self.partition_digests[partition_key(spec)]
        if digest not in self._partition_cache:
            self._partition_cache[digest] = self.store.get_partition(digest)
        return self._partition_cache[digest]

    def materialize_table(self, table):
        if table in self._materialized:
            return self._materialized[table]
        rows = []
        for key, digest in self.partition_digests.items():
            if not key.startswith(table + '/'):
                continue
            body = self.load_partition(key.split('/'))
            rows.extend(body['rows'])
        self._materialized[table] = rows
        return rows

    # ---- 单指标计划 ----
    def run_plan(self, node, group_by, window, quantile_q=None):
        needed = self._required_partitions(node, window)
        missing = self._missing_partitions(node, window)
        if missing:
            raise ScopeError(
                'partition_missing',
                '缺少事实分区，拒绝在不完整数据上汇总：' + ', '.join(sorted(missing)))
        groups = defaultdict(lambda: None)
        for key in sorted(needed):
            body = self.load_partition(key.split('/'))
            for row, joined in self._iter_rows(node, body):
                if not self._in_window(node, row, joined, window):
                    continue
                group_key = tuple(self._dimension_value(node, field, row, joined)
                                  for field in group_by)
                state = self.evaluate(node, row, joined)
                groups[group_key] = merge_states(groups[group_key], state)
        return dict(groups), needed

    def _iter_rows(self, node, partition_body):
        """产出 (驱动行, 连接上下文)；m:1 连接按外键补父表，扇出走 bridge。"""
        source = node['table']
        if partition_body['spec'][0] != source:
            return
        requests = {row['request_id']: row
                    for row in self.materialize_table('requests')}
        for row in partition_body['rows']:
            joined = {}
            if source == 'calls':
                parent = requests.get(row['request_id'])
                if parent is None:
                    raise ScopeError('orphan_fact',
                                     '调用 %s 找不到所属请求' % row.get('call_id'))
                joined['requests'] = parent
            yield row, joined

    def _in_window(self, node, row, joined, window):
        if not window:
            return True
        start, end = window
        anchor_table = node['time_anchor_table']
        if anchor_table == node['table']:
            anchor_row = row
        elif anchor_table in joined:
            anchor_row = joined[anchor_table]
        elif node.get('bridge') and anchor_table == node['table']:
            anchor_row = row
        else:
            anchor_row = row
        day = anchor_row['day']
        return (start is None or day >= start) and (end is None or day <= end)

    # ---- 节点求值：每个节点产出一个可合并状态 ----
    def evaluate(self, node, row, joined):
        op = node['op']
        if op in ('count', 'sum', 'distinct_count', 'quantile'):
            return self._leaf_state(node, row)
        if op == 'ratio':
            numerator = self.evaluate(node['children']['numerator'], row, joined)
            denominator = self.evaluate(node['children']['denominator'], row, joined)
            return make_state('ratio', numerator=numerator, denominator=denominator)
        if op == 'ref':
            return self._ref_state(node, row, joined)
        raise ScopeError('bad_expression', '无法执行的节点：' + op)

    def _leaf_state(self, node, row):
        for predicate in node['predicates']:
            if not row_matches(row, predicate):
                return self._empty_for(node)
        op = node['op']
        if op == 'count':
            return make_state('scalar', value=1)
        if op == 'sum':
            return make_state('scalar', value=row[node['field']])
        if op == 'distinct_count':
            return make_state('hash_set', members=[row[TABLES[node['table']]['entity_field']]])
        return make_state('sample', values=[row[node['field']]])

    def _ref_state(self, node, row, joined):
        bridge = node.get('bridge')
        child = node['child']
        if bridge is None:
            state = self.evaluate(child, row, joined)
        else:
            state = self._bridge_state(node, child, bridge, row)
        if state is None:
            return self._empty_for(node)
        if node['predicates']:
            if bridge is not None and bridge['rule'] == 'pick':
                # pick 只保留一条子行，谓词作用于被选中的那一条。
                predicate_rows = [self._picked_row(bridge, row)]
            else:
                predicate_rows = self._predicate_rows(node, row, joined, bridge)
            for predicate in node['predicates']:
                if not any(row_matches(candidate, predicate)
                           for candidate in predicate_rows):
                    return self._empty_for(node)
        return state

    def _picked_row(self, bridge, parent_row):
        join_field = bridge_join_field(bridge)
        parent_field = bridge_parent_field(bridge)
        children = [candidate for candidate in self.materialize_table(bridge['to'])
                    if candidate[join_field] == parent_row[parent_field]]
        return sorted(
            children,
            key=lambda item: (item[bridge['order_by']],
                              item[TABLES[bridge['to']]['primary_key'][0]]),
            reverse=bridge['direction'] == 'desc')[0]

    def _predicate_rows(self, node, parent_row, joined, bridge):
        """包装谓词的作用行：无桥接用当前行；桥接时用被扇出的子行集合。"""
        if bridge is None:
            return [parent_row]
        join_field = bridge_join_field(bridge)
        parent_field = bridge_parent_field(bridge)
        return [candidate for candidate in self.materialize_table(bridge['to'])
                if candidate[join_field] == parent_row[parent_field]]

    def _bridge_state(self, node, child, bridge, parent_row):
        """从父表行出发，按声明的扇出规则映射到子表状态。

        multicast：每个父实体至多计入一次，关联多少条子行都不会膨胀；
        pick：按 order_by 确定性地只选一条，禁止“随便选一条”。
        """
        join_field = bridge_join_field(bridge)
        parent_field = bridge_parent_field(bridge)
        children = [candidate for candidate in self.materialize_table(bridge['to'])
                    if candidate[join_field] == parent_row[parent_field]]
        if not children:
            return self._empty_for(node)
        joined = {'requests': parent_row} if bridge['to'] == 'calls' else {}
        if bridge['rule'] == 'pick':
            chosen = self._picked_row(bridge, parent_row)
            return self.evaluate(child, chosen, joined)
        if child['family'] == 'scalar':
            accepted = set()
            for candidate in children:
                state = self.evaluate(child, candidate, joined)
                if state['value']:
                    accepted.add(candidate[join_field])
            return make_state('hash_set', members=sorted(accepted))
        if child['family'] == 'ratio':
            numerator, denominator = set(), set()
            for candidate in children:
                state = self.evaluate(child, candidate, joined)
                if state['denominator']['value']:
                    denominator.add(candidate[join_field])
                if state['numerator']['value']:
                    numerator.add(candidate[join_field])
            return make_state(
                'ratio',
                numerator=make_state('hash_set', members=sorted(numerator)),
                denominator=make_state('hash_set', members=sorted(denominator)))
        raise ScopeError('bridge_unsupported',
                         'multicast 不支持状态族：' + child['family'])

    # ---- 分组维度 ----
    def _resolve_dimension(self, node, field):
        owners = _owners(field)
        source = node['table']
        if source in owners:
            return {'field': field, 'via': source, 'join_path': [source]}
        for owner in owners:
            path = find_many_to_one_path(source, owner)
            if path is not None:
                return {'field': field, 'via': owner, 'join_path': path}
        bridge = node.get('bridge')
        if bridge is not None and bridge['to'] in owners:
            return {'field': field, 'via': bridge['to'],
                    'join_path': [source, bridge['to']], 'bridge': bridge['rule']}
        raise ScopeError('dimension_unavailable',
                         '维度 %s 在指标 %s 的粒度上不可比较' % (field, node['metric']))

    def _dimension_value(self, node, field, row, joined):
        resolution = self._resolve_dimension(node, field)
        via = resolution['via']
        if via == node['table']:
            return row[field]
        if via in joined:
            return joined[via][field]
        # 沿已声明的多对一外键逐跳补父表行。
        current = row
        for left, right in zip(resolution['join_path'], resolution['join_path'][1:]):
            edge = EDGES[(left, right)]
            parent_key = current[edge['join'][0]]
            parent = self._row_index(right).get(parent_key)
            if parent is None:
                raise ScopeError('orphan_fact',
                                 '事实连接不到 %s：%s' % (right, parent_key))
            current = parent
        return current[field]

    def _row_index(self, table):
        key = TABLES[table]['primary_key']
        if len(key) != 1:
            raise ScopeError('bad_grain', '表 %s 无法按单列建索引' % table)
        return {row[key[0]]: row for row in self.materialize_table(table)}

    # ---- 分区需求 ----
    def _required_partitions(self, node, window):
        specs = set()
        self._collect_partitions(node, window, specs)
        return specs

    def _missing_partitions(self, node, window):
        required = self._required_partitions(node, window)
        return sorted(required - set(self.partition_digests))

    def _collect_partitions(self, node, window, specs):
        table = node['table']
        for key in self.expected_partition_keys:
            if not key.startswith(table + '/'):
                continue
            parts = key.split('/')
            if window and not _day_in_window(parts[1], window):
                continue
            specs.add(key)
        if node['op'] == 'ratio':
            self._collect_partitions(node['children']['numerator'], window, specs)
            self._collect_partitions(node['children']['denominator'], window, specs)
        if node['op'] == 'ref':
            self._collect_partitions(node['child'], window, specs)

    @staticmethod
    def _empty_for(node):
        family = node['family']
        if family == 'ratio':
            return make_state('ratio',
                              numerator=make_state('scalar', value=0),
                              denominator=make_state('scalar', value=0))
        return empty_state(family)


def bridge_join_field(bridge):
    return EDGES[(bridge['to'], bridge['from'])]['join'][0]


def bridge_parent_field(bridge):
    return EDGES[(bridge['to'], bridge['from'])]['join'][1]


def _owners(field):
    from .schema import DIMENSION_OWNERS
    return DIMENSION_OWNERS[field]


def _day_in_window(day, window):
    start, end = window
    return (start is None or day >= start) and (end is None or day <= end)


def render_value(node, state, quantile_q):
    value = finalize(state)
    if node['family'] == 'sample':
        if value is None:
            return None
        return nearest_rank(value, quantile_q or 0.95)
    return value
