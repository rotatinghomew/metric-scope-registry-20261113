"""状态目录的持久化层：分区快照、指标定义版本库、值级缓存与运行记录。

所有持久对象都按内容摘要寻址，因此不可变：同一份明细永远命中同一份分区，
修订一条调用只会产生一个新的分区摘要，依赖旧分区的结果天然失效，
而无关分区和无关缓存条目原样保留。
"""

import os
from pathlib import Path

from .compiler import canonical_digest
from .io import read_json, write_json
from .schema import ScopeError

PARTITION_TABLES = ('requests', 'calls', 'user_days')
# calls 按 (day, app) 切分，修订某一个应用的调用只失效该分区；
# 其余表按 day 切分。


def partition_spec(table, row):
    if table == 'calls':
        return table, row['day'], row['app']
    return table, row['day']


def partition_key(spec):
    return '/'.join(str(part) for part in spec)


class StateStore:
    def __init__(self, directory):
        self.root = Path(directory)
        self.snapshots = self.root / 'partitions'
        self.definition_dir = self.root / 'definitions'
        self.cache_dir = self.root / 'cache'
        self.runs_dir = self.root / 'runs'
        for path in (self.snapshots, self.definition_dir, self.cache_dir, self.runs_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.index = self._load_json(self.root / 'index.json', default={
            'partitions': {}, 'runs': [], 'definitions_digest': None})

    # ---- 指标定义：同名同版本不可变，版本长期可回放 ----
    def register_definitions(self, definitions):
        digest = canonical_digest(definitions)
        path = self.definition_dir / (digest.split(':', 1)[1] + '.json')
        write_json(path, {'digest': digest, 'definitions': definitions})
        previous = self.index['definitions_digest']
        if previous is not None and previous != digest:
            # 定义整体演进是允许的，但同一次运行只能锁定一个定义摘要，
            # 版本混用由编译器按指标名再做一层检查。
            self.index['definitions_digest'] = digest
        else:
            self.index['definitions_digest'] = digest
        self._flush_index()
        return digest

    def load_definition_bundle(self, digest):
        path = self.definition_dir / (digest.split(':', 1)[1] + '.json')
        if not path.exists():
            raise ScopeError('replay_unavailable',
                             '状态目录中找不到定义版本：' + digest)
        return read_json(path)

    # ---- 事实分区：内容寻址，不可变快照 ----
    def put_partition(self, spec, table_revision, rows, corrections):
        body = {'spec': list(spec), 'table_revision': table_revision,
                'rows': rows,
                'corrections': sorted(corrections, key=lambda item: item['key'])}
        digest = canonical_digest(body)
        relative = digest.split(':', 1)[1] + '.json'
        path = self.snapshots / relative
        if not path.exists():
            write_json(path, body)
        self.index['partitions'][partition_key(spec)] = digest
        self._flush_index()
        return digest

    def get_partition(self, digest):
        path = self.snapshots / (digest.split(':', 1)[1] + '.json')
        if not path.exists():
            raise ScopeError('replay_unavailable',
                             '状态目录中找不到分区快照：' + digest)
        return read_json(path)

    def current_partition_digest(self, spec):
        return self.index['partitions'].get(partition_key(spec))

    def all_partition_keys(self):
        return sorted(self.index['partitions'])

    # ---- 值级缓存：键同时绑定指标版本、查询形状与分区摘要 ----
    def cache_lookup(self, cache_key):
        path = self._cache_path(cache_key)
        if not path.exists():
            return None
        return read_json(path)

    def cache_put(self, cache_key, entry):
        write_json(self._cache_path(cache_key), entry)

    def cache_exists(self, cache_key):
        return self._cache_path(cache_key).exists()

    def _cache_path(self, cache_key):
        return self.cache_dir / (cache_key.split(':', 1)[1] + '.json')

    # ---- 运行记录：支持按 run_id 或最近一次兼容运行回放 ----
    def record_run(self, run_id, record):
        write_json(self.runs_dir / (run_id + '.json'), record)
        self.index['runs'].append(run_id)
        self._flush_index()

    def load_run(self, run_id):
        path = self.runs_dir / (run_id + '.json')
        if not path.exists():
            raise ScopeError('replay_unavailable', '找不到运行记录：' + run_id)
        return read_json(path)

    def latest_run(self):
        if not self.index['runs']:
            return None
        return self.load_run(self.index['runs'][-1])

    def _flush_index(self):
        write_json(self.root / 'index.json', self.index)

    @staticmethod
    def _load_json(path, default):
        if not path.exists():
            return default
        return read_json(path)


def ingest_document(store, document):
    """把一份 catalog 事实写入内容寻址分区，返回分区摘要映射与表修订。

    corrections 在入分区前按主键应用到对应行；被修订行所在分区的内容
    摘要随之改变，实现按分区（calls 精确到 day+app）的选择性失效。
    """
    tables = document['tables']
    corrections_by_table = {}
    for correction in document.get('corrections', ()):
        corrections_by_table.setdefault(correction['table'], []).append(correction)

    grouped = {}
    touched = {}
    for table in PARTITION_TABLES:
        if table not in tables:
            raise ScopeError('unknown_table', 'catalog 缺少事实表：' + table)
        key_fields = tables[table].get('primary_key')
        rows = [dict(row) for row in tables[table]['rows']]
        if key_fields is None:
            from .schema import TABLES as _TABLES
            key_fields = _TABLES[table]['primary_key']
        index = {tuple(row[field] for field in key_fields): row for row in rows}
        touched[table] = {}
        for correction in corrections_by_table.get(table, ()):
            row = index.get((correction['key'],)) \
                if len(key_fields) == 1 else index.get(_composite_key(correction, key_fields))
            if row is None:
                raise ScopeError('bad_correction',
                                 '修订指向不存在的事实：%s/%s'
                                 % (table, correction['key']))
            changes = correction.get('set')
            if changes is None:
                # 兼容 catalog 现有写法：除保留字段外的顶层键即待覆盖列。
                changes = {field: value for field, value in correction.items()
                           if field not in ('table', 'key', 'revision', 'set')}
            for field, value in changes.items():
                row[field] = value
            touched[table][tuple(row[field] for field in key_fields)] = correction
        grouped[table] = {}
        for row in rows:
            spec = partition_spec(table, row)
            grouped[table].setdefault(spec, []).append(row)

    digest_map = {}
    for table, buckets in grouped.items():
        key_fields = tables[table].get('primary_key')
        if key_fields is None:
            from .schema import TABLES as _TABLES
            key_fields = _TABLES[table]['primary_key']
        for spec, rows in sorted(buckets.items()):
            relevant = [touched[table][tuple(row[field] for field in key_fields)]
                        for row in rows
                        if tuple(row[field] for field in key_fields) in touched[table]]
            digest_map[partition_key(spec)] = store.put_partition(
                spec, tables[table]['revision'], rows, relevant)
    table_revisions = {name: tables[name]['revision'] for name in PARTITION_TABLES}
    return digest_map, table_revisions


def _composite_key(correction, key_fields):
    key = correction['key']
    if not isinstance(key, list) or len(key) != len(key_fields):
        raise ScopeError('bad_correction',
                         '复合主键修订必须按字段给数组：' + str(key))
    return tuple(key)
