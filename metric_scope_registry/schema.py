"""事实表的静态口径登记：实体粒度、时间锚点、维度与已声明的表间连接。

业务指标定义只能引用这里登记过的表、字段和连接；任何未声明的多对多连接
在编译期就会被拒绝，而不是等到执行时悄悄选一条明细。
"""

# 三张事实表的粒度（grain）即“一行代表什么实体”。
TABLES = {
    'requests': {
        'grain': 'request',
        'primary_key': ('request_id',),
        'time_anchor': 'day',
        'entity_field': 'user_id',
        'dimensions': ('day', 'user_id', 'firmware'),
    },
    'calls': {
        'grain': 'call',
        'primary_key': ('call_id',),
        'time_anchor': 'day',
        'entity_field': None,
        'dimensions': ('day', 'app', 'request_id'),
    },
    'user_days': {
        'grain': 'user_day',
        'primary_key': ('user_id', 'day'),
        'time_anchor': 'day',
        'entity_field': 'user_id',
        'dimensions': ('day', 'user_id'),
    },
}

# 只登记有外键背书的多对一连接；o2m 方向的扇出必须由指标定义里的
# 显式 bridge 声明 multicast 或 pick 规则后才允许通行。
# 每条边：from -> to 为多对一（多行 from 对应一行 to）。
EDGES = {
    ('calls', 'requests'): {
        'kind': 'many_to_one',
        'join': ('request_id', 'request_id'),
    },
    ('user_days', 'requests'): None,  # 无外键路径，禁止隐式连接
}

# 可以被分组/谓词引用的列及其所在表（同名列在多表出现时语义一致）。
# 维度列可用于 group_by，度量列只能用于过滤或 sum/quantile 取值。
DIMENSION_OWNERS = {
    'day': ('requests', 'calls', 'user_days'),
    'firmware': ('requests',),
    'app': ('calls',),
    'user_id': ('requests', 'user_days'),
    'request_id': ('requests', 'calls'),
}

MEASURE_OWNERS = {
    'success': ('requests', 'calls'),
    'active': ('user_days',),
    'latency_ms': ('calls',),
}

GROUPABLE_DIMENSIONS = frozenset(DIMENSION_OWNERS)
KNOWN_FIELDS = frozenset(DIMENSION_OWNERS) | frozenset(MEASURE_OWNERS)

SUPPORTED_PREDICATES = ('eq', 'ne', 'in')


class ScopeError(ValueError):
    """口径错误：编译期发现的所有拒绝都带稳定 code，便于上层分类。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def field_owners(field):
    """返回字段可出现的表；维度列与度量列都允许做谓词。"""
    if field in DIMENSION_OWNERS:
        return DIMENSION_OWNERS[field]
    if field in MEASURE_OWNERS:
        return MEASURE_OWNERS[field]
    return None


def require_table(name):
    if name not in TABLES:
        raise ScopeError('unknown_table', '未登记的事实表：' + str(name))
    return TABLES[name]


def validate_predicate(predicate):
    if not isinstance(predicate, dict):
        raise ScopeError('bad_predicate', '过滤谓词必须是对象：' + repr(predicate))
    op = predicate.get('op')
    if op not in SUPPORTED_PREDICATES:
        raise ScopeError('bad_predicate', '不支持的谓词操作：' + str(op))
    field = predicate.get('field')
    if not isinstance(field, str) or not field:
        raise ScopeError('bad_predicate', '谓词缺少字段名')
    value = predicate.get('value')
    if op in ('eq', 'ne'):
        if isinstance(value, (dict, list)):
            raise ScopeError('bad_predicate', op + ' 谓词的值必须是标量')
    elif op == 'in':
        if not isinstance(value, list) or not value or any(
                isinstance(item, (dict, list)) for item in value):
            raise ScopeError('bad_predicate', 'in 谓词的值必须是非空标量数组')


def row_matches(row, predicate):
    validate_predicate(predicate)
    actual = row.get(predicate['field'])
    op = predicate['op']
    if op == 'eq':
        return actual == predicate['value']
    if op == 'ne':
        return actual != predicate['value']
    return actual in predicate['value']


def predicate_covers(outer, inner):
    """outer 谓词集合是否逻辑包含 inner（inner 的有效人群是 outer 的子集）。

    空的 outer 表示全集，任何 inner 都被包含。只处理同字段等值/枚举之间的
    包含关系；无法证明包含时返回 False，让分母过滤不兼容在编译期暴露，
    而不是默默放宽口径。
    """
    if not outer:
        return True
    if not inner:
        return True
    grouped = {}
    for predicate in outer:
        grouped.setdefault((predicate.get('_table'), predicate['field']), []).append(predicate)
    for predicate in inner:
        candidates = grouped.get((predicate.get('_table'), predicate['field']))
        if not candidates:
            return False
        allowed = set()
        negated = []
        for candidate in candidates:
            if candidate['op'] == 'eq':
                allowed.add(candidate['value'])
            elif candidate['op'] == 'in':
                allowed.update(candidate['value'])
            elif candidate['op'] == 'ne':
                negated.append(candidate['value'])
        if predicate['op'] == 'eq':
            wanted = {predicate['value']}
        elif predicate['op'] == 'in':
            wanted = set(predicate['value'])
        else:
            return False
        if allowed:
            if not wanted <= allowed:
                return False
        elif negated:
            if wanted & set(negated):
                return False
        else:
            return False
    return True


def find_many_to_one_path(source, target):
    """返回 source->target 的多对一连接路径（表名序列），找不到返回 None。"""
    if source == target:
        return [source]
    queue = [(source, [source])]
    seen = {source}
    while queue:
        current, path = queue.pop(0)
        for (left, right), edge in EDGES.items():
            if left != current or edge is None:
                continue
            if right in seen:
                continue
            next_path = path + [right]
            if right == target:
                return next_path
            seen.add(right)
            queue.append((right, next_path))
    return None
