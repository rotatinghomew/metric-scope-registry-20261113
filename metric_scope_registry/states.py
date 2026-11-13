"""可合并的中间状态（mergeable state）。

汇总查询不是把明细查询的数字再相加，而是把各分组留下的中间状态按其
数学语义合并。每种状态都声明自己的 family，family 不兼容时拒绝合并，
避免把“用户数之和”当成月活、把分位数当可加量。
"""

from .schema import ScopeError


def make_state(family, **payload):
    state = {'family': family}
    state.update(payload)
    return state


def empty_state(family):
    if family == 'scalar':
        return make_state('scalar', value=0)
    if family == 'hash_set':
        return make_state('hash_set', members=[])
    if family == 'sample':
        return make_state('sample', values=[])
    if family == 'ratio':
        return make_state('ratio', numerator=empty_state('scalar'),
                          denominator=empty_state('scalar'))
    raise ScopeError('bad_state', '未知状态族：' + str(family))


def merge_states(left, right):
    """按状态族合并两个中间状态；不兼容时拒绝而不是猜测口径。"""
    if left is None:
        return right
    if right is None:
        return left
    if left['family'] != right['family']:
        raise ScopeError(
            'state_incompatible',
            '中间状态不可合并：%s 与 %s' % (left['family'], right['family']))
    family = left['family']
    if family == 'scalar':
        return make_state('scalar', value=left['value'] + right['value'])
    if family == 'hash_set':
        # 集合取并：跨日用户数因此不会被重复相加。
        merged = sorted(set(left['members']) | set(right['members']))
        return make_state('hash_set', members=merged)
    if family == 'sample':
        # 保留原始样本，分位数只能在合并后的完整样本上计算。
        merged = sorted(left['values'] + right['values'])
        return make_state('sample', values=merged)
    if family == 'ratio':
        return make_state(
            'ratio',
            numerator=merge_states(left['numerator'], right['numerator']),
            denominator=merge_states(left['denominator'], right['denominator']))
    raise ScopeError('bad_state', '未知状态族：' + family)


def finalize(state):
    """把中间状态落成对外输出的标量；null 表示不可计算而非零。"""
    if state is None:
        return None
    family = state['family']
    if family == 'scalar':
        return state['value']
    if family == 'hash_set':
        return len(state['members'])
    if family == 'sample':
        values = state['values']
        if not values:
            return None
        return values  # 分位数在查询层按请求的 q 计算，样本本身不压成数字
    if family == 'ratio':
        denominator = state['denominator']['value']
        if denominator == 0:
            return None
        return state['numerator']['value'] / denominator
    raise ScopeError('bad_state', '未知状态族：' + family)


def nearest_rank(values, quantile):
    """最近秩法分位数；空样本返回 None。"""
    if not values:
        return None
    if not 0 < quantile < 1:
        raise ScopeError('bad_quantile', '分位数 q 必须落在 (0, 1)：' + str(quantile))
    ordered = sorted(values)
    rank = max(1, min(len(ordered), _ceil(quantile * len(ordered))))
    return ordered[rank - 1]


def _ceil(number):
    truncated = int(number)
    return truncated if number == truncated else truncated + 1
