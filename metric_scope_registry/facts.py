from collections import defaultdict


KEYS = {'requests': ('request_id',), 'calls': ('call_id',), 'user_days': ('user_id', 'day')}


def validate_facts(tables):
    for table, fields in KEYS.items():
        keys = [tuple(row[field] for field in fields) for row in tables[table]['rows']]
        if len(set(keys)) != len(keys):
            raise ValueError('事实主键重复：' + table)
    requests = {row['request_id'] for row in tables['requests']['rows']}
    if any(row['request_id'] not in requests for row in tables['calls']['rows']):
        raise ValueError('调用找不到所属请求')
    return tables


def base_counts(tables, table, dimensions):
    validate_facts(tables)
    if len(set(dimensions)) != len(dimensions):
        raise ValueError('分组维度重复')
    groups = defaultdict(lambda: {'rows': 0, 'users': set(), 'success': 0})
    for row in tables[table]['rows']:
        key = tuple(row[field] for field in dimensions)
        group = groups[key]
        group['rows'] += 1
        if 'user_id' in row:
            group['users'].add(row['user_id'])
        group['success'] += int(row.get('success') is True)
    return [{'dimensions': dict(zip(dimensions, key)), 'rows': value['rows'],
             'users': len(value['users']), 'success': value['success']}
            for key, value in sorted(groups.items())]


def inspect(document):
    tables = validate_facts(document['tables'])
    return {name: base_counts(tables, name, ['day']) for name in KEYS}
