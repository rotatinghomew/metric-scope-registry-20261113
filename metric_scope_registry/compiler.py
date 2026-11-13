"""指标定义编译器：把业务写的一次定义编译成带完整口径的执行计划。

编译期保证：
* 循环引用、未知表/字段、版本混用直接拒绝；
* 每个节点都能追溯到实体粒度、时间锚点和有效人群（谓词集合）；
* 比例指标的分子分母同粒度、同时间锚点，且分母人群包含分子人群；
* 反向扇出连接必须携带显式 bridge（multicast/pick），否则拒绝。
"""

import hashlib
import json

from .schema import (EDGES, TABLES, ScopeError, field_owners,
                     find_many_to_one_path, predicate_covers, validate_predicate)

SCALAR_LEAVES = {'count', 'sum'}
LEAF_OPS = SCALAR_LEAVES | {'distinct_count', 'quantile'}


def canonical_digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return 'sha256:' + hashlib.sha256(payload).hexdigest()


def normalize_definitions(raw_definitions):
    """登记定义：同名同版本内容必须一致，返回 name -> {version: definition}。"""
    if not isinstance(raw_definitions, list):
        raise ScopeError('bad_definition', 'metric_definitions 必须是数组')
    registry = {}
    digests = {}
    for definition in raw_definitions:
        name = definition.get('name')
        version = definition.get('version')
        if not isinstance(name, str) or not name:
            raise ScopeError('bad_definition', '指标缺少 name')
        if not isinstance(version, int) or version < 1:
            raise ScopeError('bad_definition', '指标 %s 缺少正整数 version' % name)
        digest = canonical_digest(definition.get('expr'))
        previous = digests.get((name, version))
        if previous is not None and previous != digest:
            raise ScopeError('duplicate_definition',
                             '指标 %s 版本 %s 存在两份不一致的定义' % (name, version))
        digests[(name, version)] = digest
        registry.setdefault(name, {})[version] = definition
    return registry


def latest_versions(registry):
    return {name: max(versions) for name, versions in registry.items()}


class Compiler:
    def __init__(self, registry, pinned=None):
        self.registry = registry
        self.latest = latest_versions(registry)
        # 本次编译闭包内每个指标名锁定的唯一版本。
        self.pinned = dict(pinned or {})
        self.compiled = {}

    def compile_all(self):
        for name in self.registry:
            self.compile_ref(name, self.pinned.get(name))
        return self.compiled

    def compile_ref(self, name, wanted=None, bridge=None, extra_predicates=None):
        if name not in self.registry:
            raise ScopeError('unknown_metric', '未登记的指标：' + name)
        wanted = wanted or self.pinned.get(name) or self.latest[name]
        if wanted not in self.registry[name]:
            raise ScopeError('unknown_metric',
                             '指标 %s 不存在版本 %s' % (name, wanted))
        locked = self.pinned.get(name)
        if locked is not None and locked != wanted:
            raise ScopeError(
                'version_mixing',
                '指标 %s 在同一查询闭包中混用版本 %s 与 %s' % (name, locked, wanted))
        self.pinned[name] = wanted
        key = (name, wanted)
        if key in self.compiled:
            if self.compiled[key] is None:
                raise ScopeError('circular_reference',
                                 '检测到循环引用，涉及指标：' + name)
            node = self.compiled[key]
            if bridge is not None or extra_predicates:
                return self._wrap_ref(node, bridge, extra_predicates or [])
            return node
        self.compiled[key] = None  # 占位，DFS 回边即循环引用
        definition = self.registry[name][wanted]
        tree = self._compile_expr(definition['expr'], stack=[key])
        tree.update({
            'metric': name,
            'version': wanted,
            'unit': definition.get('unit'),
            'expr_digest': canonical_digest(definition['expr']),
        })
        self.compiled[key] = tree
        if bridge is not None or extra_predicates:
            return self._wrap_ref(tree, bridge, extra_predicates or [])
        return tree

    def _compile_expr(self, expr, stack):
        if not isinstance(expr, dict) or 'op' not in expr:
            raise ScopeError('bad_expression', '表达式必须是带 op 的对象')
        op = expr['op']
        if op in LEAF_OPS:
            return self._compile_leaf(expr)
        if op == 'ratio':
            return self._compile_ratio(expr, stack)
        if op == 'ref':
            return self._compile_ref_expr(expr, stack)
        raise ScopeError('bad_expression', '不支持的表达式操作：' + str(op))

    def _compile_leaf(self, expr):
        op = expr['op']
        table = expr.get('table')
        if table not in TABLES:
            raise ScopeError('unknown_table', '表达式引用未登记的表：' + str(table))
        predicates = list(expr.get('filters', ()))
        for predicate in predicates:
            validate_predicate(predicate)
            self._require_field_reachable(table, predicate['field'])
        spec = TABLES[table]
        if op == 'quantile':
            q = expr.get('q')
            if not isinstance(q, (int, float)) or not 0 < q < 1:
                raise ScopeError('bad_quantile', 'quantile 的 q 必须落在 (0, 1)')
            family = 'sample'
        elif op == 'distinct_count':
            if not spec['entity_field']:
                raise ScopeError('bad_grain',
                                 '表 %s 没有实体字段，无法去重' % table)
            family = 'hash_set'
        else:
            family = 'scalar'
        if op == 'sum' and not isinstance(expr.get('field'), str):
            raise ScopeError('bad_expression', 'sum 必须声明数值字段 field')
        return {
            'op': op,
            'table': table,
            'field': expr.get('field'),
            'q': expr.get('q'),
            'predicates': predicates,
            'grain': spec['grain'],
            'time_anchor_table': table,
            'population': _population(table, predicates),
            'family': family,
            'bridge': None,
            'dependencies': [],
        }

    def _compile_ratio(self, expr, stack):
        children_expr = expr.get('terms')
        if not isinstance(children_expr, dict) or \
                set(children_expr) != {'numerator', 'denominator'}:
            raise ScopeError('bad_expression',
                             'ratio 必须包含 numerator 与 denominator 两个分支')
        numerator = self._compile_expr(children_expr['numerator'], stack)
        denominator = self._compile_expr(children_expr['denominator'], stack)
        if numerator['grain'] != denominator['grain']:
            raise ScopeError(
                'ratio_grain_mismatch',
                '比例分子粒度 %s 与分母粒度 %s 不一致，禁止混算'
                % (numerator['grain'], denominator['grain']))
        if numerator['time_anchor_table'] != denominator['time_anchor_table']:
            raise ScopeError(
                'ratio_anchor_mismatch',
                '比例分子与分母的时间锚点不同：%s vs %s'
                % (numerator['time_anchor_table'], denominator['time_anchor_table']))
        if not predicate_covers(denominator['population'], numerator['population']):
            raise ScopeError(
                'filter_incompatible',
                '分母过滤不包含分子有效人群，分母会被悄悄换掉，拒绝编译')
        for child in (numerator, denominator):
            if child['family'] not in ('scalar', 'hash_set'):
                raise ScopeError('ratio_grain_mismatch',
                                 '比例分支必须是计数型状态，实际为 ' + child['family'])
        return {
            'op': 'ratio',
            'table': numerator['table'],
            'predicates': [],
            'children': {'numerator': numerator, 'denominator': denominator},
            'grain': numerator['grain'],
            'time_anchor_table': numerator['time_anchor_table'],
            'population': denominator['population'],
            'family': 'ratio',
            'bridge': numerator.get('bridge'),
            'dependencies': _dependencies(numerator) + _dependencies(denominator),
        }

    def _compile_ref_expr(self, expr, stack):
        target = expr.get('metric')
        wanted = expr.get('version')
        if any(name == target and (wanted is None or version == wanted)
               for name, version in stack):
            raise ScopeError('circular_reference',
                             '检测到循环引用，涉及指标：' + target)
        bridge = expr.get('bridge')
        if bridge is not None:
            bridge = self._validate_bridge(bridge)
        predicates = list(expr.get('filters', ()))
        node = self.compile_ref(target, wanted)
        if bridge is None:
            # 无桥接引用只能引用同源表、同实体粒度；显式声明 source_table
            # 但与被引用指标的源表不一致时，属于未声明的多对多，直接拒绝。
            source_table = expr.get('source_table')
            if source_table is not None and \
                    source_table != node['time_anchor_table']:
                raise ScopeError(
                    'undeclared_join',
                    '指标 %s 想在 %s 粒度引用 %s 粒度的 %s，却没有声明 bridge；'
                    '一个请求关联多个应用时禁止隐式连接'
                    % (target, source_table, node['time_anchor_table'],
                       node['grain']))
            return self._wrap_same_grain_ref(node, predicates)
        # 反向扇出（如 requests -> calls）只有显式 bridge 才允许；
        # 正向多对一（如 calls -> requests）沿已登记外键放行。
        reverse_edge = (bridge['to'], bridge['from'])
        if EDGES.get(reverse_edge) is None and \
                find_many_to_one_path(bridge['from'], bridge['to']) is None:
            raise ScopeError(
                'undeclared_join',
                '连接 %s -> %s 没有外键背书，禁止隐式多对多'
                % (bridge['from'], bridge['to']))
        return self._wrap_ref(node, bridge, predicates)

    def _wrap_same_grain_ref(self, node, predicates):
        # 沿引用链找到最内层叶子，确认整条链没有已经声明的反向桥接。
        inner = node
        while inner['op'] == 'ref':
            if inner.get('bridge') is not None:
                raise ScopeError(
                    'undeclared_join',
                    '指标 %s 已经通过桥接改变粒度，外层引用必须重新显式声明连接'
                    % inner['metric'])
            inner = inner['child']
        # 无 bridge 的引用只能落在同一张事实表上；跨表即跨实体粒度，
        # 一个请求关联多个应用时会静默放大计数，必须先声明 multicast/pick。
        if inner['table'] != node['time_anchor_table']:
            raise ScopeError(
                'undeclared_join',
                '无 bridge 的 ref 从 %s 跨到了 %s（粒度 %s -> %s），'
                '禁止未声明的多对多连接'
                % (node['time_anchor_table'], inner['table'],
                   node['grain'], inner['grain']))
        return self._wrap_ref(node, None, predicates)

    def _wrap_ref(self, node, bridge, predicates):
        for predicate in predicates:
            validate_predicate(predicate)
        source_table = node['time_anchor_table']
        if bridge is None:
            # 无桥接引用必须与被引用指标同表同粒度；反向扇出未声明时直接拒绝，
            # 防止在 requests 口径里把一个请求关联的多条 calls 重复计数。
            if node['op'] == 'ref' and node.get('bridge'):
                raise ScopeError('undeclared_join',
                                 '嵌套桥接必须在每层显式声明，拒绝隐式连接')
        filter_table = bridge['to'] if bridge is not None else source_table
        for predicate in predicates:
            self._require_field_reachable(filter_table, predicate['field'], bridge)
        wrapped = {
            'op': 'ref',
            'table': source_table,
            'field': None,
            'q': None,
            'predicates': predicates,
            'predicate_table': filter_table,
            'ref': (node['metric'], node['version']),
            'child': node,
            'grain': node['grain'],
            'time_anchor_table': node['time_anchor_table'],
            'population': node['population'] + _population(filter_table, predicates),
            'family': node['family'],
            'bridge': bridge,
            'dependencies': _dependencies(node),
        }
        if bridge is not None:
            self._apply_bridge_lineage(wrapped, node, bridge)
        return wrapped

    def _validate_bridge(self, bridge):
        if not isinstance(bridge, dict):
            raise ScopeError('bad_bridge', 'bridge 必须是对象')
        source = bridge.get('from')
        target = bridge.get('to')
        rule = bridge.get('rule')
        if (source, target) not in {(t, f) for (f, t), edge in EDGES.items() if edge}:
            raise ScopeError(
                'undeclared_join',
                '连接 %s -> %s 没有已声明的外键背书，禁止隐式多对多' % (source, target))
        if rule not in ('multicast', 'pick'):
            raise ScopeError('bad_bridge',
                             'bridge 规则必须是 multicast 或 pick：' + str(rule))
        if rule == 'pick' and not isinstance(bridge.get('order_by'), str):
            raise ScopeError('bad_bridge', 'pick 桥接必须声明 order_by 字段')
        normalized = {'from': source, 'to': target, 'rule': rule,
                      'order_by': bridge.get('order_by'),
                      'direction': bridge.get('direction', 'asc')}
        if normalized['direction'] not in ('asc', 'desc'):
            raise ScopeError('bad_bridge', 'pick direction 只能是 asc/desc')
        return normalized

    def _apply_bridge_lineage(self, wrapped, node, bridge):
        """扇出后状态族的变化：multicast 用父键集合防重复，pick 保持原粒度。"""
        if bridge['rule'] == 'pick':
            return
        if node['family'] not in ('scalar', 'ratio'):
            raise ScopeError(
                'bridge_unsupported',
                'multicast 桥接只支持计数与比例指标，%s 不支持' % node['family'])
        wrapped['family'] = 'hash_set' if node['family'] == 'scalar' else 'ratio'

    def _require_field_reachable(self, table, field, bridge=None):
        owners = field_owners(field)
        if owners is None:
            raise ScopeError('unknown_dimension',
                             '字段 %s 未在任何事实表登记' % field)
        if table in owners:
            return
        if find_many_to_one_path(table, _first(owners)) is not None:
            return
        if bridge is not None and bridge['to'] == table and bridge['from'] in owners:
            return
        raise ScopeError(
            'dimension_unavailable',
            '维度 %s 在粒度 %s 上没有声明过的连接路径，不可比较' % (field, table))


def _population(table, predicates):
    return [dict(predicate, _table=table) for predicate in predicates]


def _dependencies(node):
    found = []
    if 'ref' in node:
        found.append(node['ref'])
    found.extend(node.get('dependencies', []))
    if node['op'] == 'ratio':
        found += _dependencies(node['children']['numerator'])
        found += _dependencies(node['children']['denominator'])
    return sorted(set(found))


def _first(values):
    return next(iter(values))


def lineage_view(node):
    """编译产物中可对外解释的口径追溯信息。"""
    return {
        'metric': node['metric'],
        'version': node['version'],
        'unit': node.get('unit'),
        'grain': node['grain'],
        'time_anchor': {'table': node['time_anchor_table'], 'field': 'day'},
        'source_table': node['table'],
        'population': [{k: v for k, v in predicate.items() if k != '_table'}
                       for predicate in node['population']],
        'state_family': node['family'],
        'bridges': [node['bridge']] if node.get('bridge') else [],
        'depends_on': [{'metric': name, 'version': version}
                       for name, version in _dependencies(node)],
        'expr_digest': node.get('expr_digest'),
    }
