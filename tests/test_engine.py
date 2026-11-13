import json
import tempfile
import unittest
from pathlib import Path

from metric_scope_registry.compiler import (Compiler, canonical_digest,
                                            normalize_definitions)
from metric_scope_registry.engine import run
from metric_scope_registry.schema import ScopeError, predicate_covers
from metric_scope_registry.states import merge_states, nearest_rank

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def compile_definitions(definitions, pins=None):
    registry = normalize_definitions(definitions)
    compiler = Compiler(registry, pinned=pins or {})
    return compiler.compile_all()


class StateTests(unittest.TestCase):
    def test_hash_set_union_does_not_double_count(self):
        left = {'family': 'hash_set', 'members': ['u0', 'u1']}
        right = {'family': 'hash_set', 'members': ['u1', 'u2']}
        merged = merge_states(left, right)
        self.assertEqual(merged['members'], ['u0', 'u1', 'u2'])

    def test_scalar_is_additive_but_sample_keeps_values(self):
        merged = merge_states({'family': 'scalar', 'value': 3},
                              {'family': 'scalar', 'value': 4})
        self.assertEqual(merged['value'], 7)
        merged = merge_states({'family': 'sample', 'values': [1, 2]},
                              {'family': 'sample', 'values': [3]})
        self.assertEqual(merged['values'], [1, 2, 3])
        self.assertEqual(nearest_rank(merged['values'], 0.95), 3)

    def test_incompatible_families_are_rejected(self):
        with self.assertRaises(ScopeError) as caught:
            merge_states({'family': 'scalar', 'value': 1},
                         {'family': 'hash_set', 'members': ['u0']})
        self.assertEqual(caught.exception.code, 'state_incompatible')

    def test_ratio_merges_components_not_quotients(self):
        left = {'family': 'ratio',
                'numerator': {'family': 'scalar', 'value': 1},
                'denominator': {'family': 'scalar', 'value': 2}}
        right = {'family': 'ratio',
                 'numerator': {'family': 'scalar', 'value': 3},
                 'denominator': {'family': 'scalar', 'value': 4}}
        merged = merge_states(left, right)
        self.assertEqual(merged['numerator']['value'], 4)
        self.assertEqual(merged['denominator']['value'], 6)

    def test_predicate_coverage(self):
        outer = [{'field': 'firmware', 'op': 'in', 'value': ['v1', 'v2'],
                  '_table': 'requests'}]
        inner = [{'field': 'firmware', 'op': 'eq', 'value': 'v1',
                  '_table': 'requests'}]
        self.assertTrue(predicate_covers(outer, inner))
        other = [{'field': 'firmware', 'op': 'eq', 'value': 'v3',
                  '_table': 'requests'}]
        self.assertFalse(predicate_covers(outer, other))
        self.assertTrue(predicate_covers([], inner))


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.definitions = load('definitions.json')['metric_definitions']

    def test_lineage_carries_grain_anchor_population(self):
        compiled = compile_definitions(self.definitions)
        completion = compiled[('request_completion', 1)]
        self.assertEqual(completion['grain'], 'request')
        self.assertEqual(completion['time_anchor_table'], 'requests')
        self.assertEqual(completion['family'], 'ratio')
        success = compiled[('request_success', 1)]
        self.assertEqual(success['population'][0]['field'], 'success')

    def test_circular_reference_rejected(self):
        bad = load('bad_cyclic.json')['metric_definitions']
        with self.assertRaises(ScopeError) as caught:
            compile_definitions(bad)
        self.assertEqual(caught.exception.code, 'circular_reference')

    def test_filter_incompatible_rejected(self):
        bad = load('bad_incompatible.json')['metric_definitions']
        with self.assertRaises(ScopeError) as caught:
            compile_definitions(bad)
        self.assertEqual(caught.exception.code, 'filter_incompatible')

    def test_undeclared_many_to_many_rejected(self):
        bad = load('bad_undeclared.json')['metric_definitions']
        with self.assertRaises(ScopeError) as caught:
            compile_definitions(bad)
        self.assertEqual(caught.exception.code, 'undeclared_join')

    def test_grain_mismatch_ratio_rejected(self):
        bad = load('bad_grain_mix.json')['metric_definitions']
        with self.assertRaises(ScopeError) as caught:
            compile_definitions(bad)
        self.assertEqual(caught.exception.code, 'ratio_grain_mismatch')

    def test_version_mixing_rejected(self):
        registry = normalize_definitions(self.definitions)
        compiler = Compiler(registry)
        compiler.pinned['latency_p95'] = 1
        with self.assertRaises(ScopeError) as caught:
            compiler.compile_ref('latency_p95', 2)
        self.assertEqual(caught.exception.code, 'version_mixing')

    def test_duplicate_version_with_different_expr_rejected(self):
        duplicated = self.definitions + [
            {'name': 'request_total', 'version': 1, 'unit': 'request',
             'expr': {'op': 'count', 'table': 'calls'}}]
        with self.assertRaises(ScopeError) as caught:
            normalize_definitions(duplicated)
        self.assertEqual(caught.exception.code, 'duplicate_definition')

    def test_multicast_bridge_produces_parent_key_set(self):
        compiled = compile_definitions(self.definitions)
        node = compiled[('requests_with_office_call', 1)]
        self.assertEqual(node['bridge']['rule'], 'multicast')
        self.assertEqual(node['family'], 'hash_set')


class EngineEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'state'
        self.out = Path(self.temp.name) / 'out.json'

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, fixture):
        run(str(FIXTURES / fixture), str(self.state), str(self.out))
        return json.loads(self.out.read_text(encoding='utf-8'))

    def test_daily_then_crossday_users_are_unioned(self):
        daily = self._run('query_daily.json')
        crossday = self._run('query_crossday.json')
        daily_users = [
            group for value in daily['values']
            if value['metric'] == 'active_users' for group in value['groups']]
        summed = sum(group['value'] for group in daily_users)
        cross = next(value for value in crossday['values']
                     if value['metric'] == 'active_users')['groups'][0]
        self.assertEqual(summed, 24)
        self.assertEqual(cross['value'], 12)
        self.assertEqual(len(cross['state']['members']), 12)

    def test_selective_invalidation_on_single_call_correction(self):
        self._run('query_daily.json')
        corrected = self._run('query_daily_corrected.json')
        effects = {item['metric']: item['status']
                   for item in corrected['cache_effects']}
        self.assertEqual(effects['request_completion'], 'hit')
        self.assertEqual(effects['active_users'], 'hit')
        self.assertIn(effects['call_completion'], ('stored', 'recomputed'))

    def test_replay_reproduces_historical_values(self):
        first = self._run('query_daily.json')
        self._run('query_daily_corrected.json')
        replay_input = FIXTURES / 'replay_daily.json'
        replay_output = Path(self.temp.name) / 'replay.json'
        run(str(replay_input), str(self.state), str(replay_output))
        replayed = json.loads(replay_output.read_text(encoding='utf-8'))
        self.assertEqual(replayed['partition_versions'], first['partition_versions'])
        self.assertEqual(replayed['values'], first['values'])
        self.assertTrue(all(effect['status'] == 'hit'
                            for effect in replayed['cache_effects']))

    def test_incomparable_dimensions_are_null_not_zero(self):
        result = self._run('query_mixed.json')
        codes = {item['code'] for item in result['rejected']}
        self.assertIn('dimension_unavailable', codes)
        statuses = {value['metric']: value['status'] for value in result['values']}
        self.assertEqual(statuses['active_users'], 'incomparable')
        call_metric = next(value for value in result['values']
                           if value['metric'] == 'call_completion')
        self.assertEqual(call_metric['status'], 'ok')
        self.assertEqual(len(call_metric['groups']), 6)

    def test_request_and_call_success_are_distinct_metrics(self):
        result = self._run('query_daily.json')
        values = {value['metric']: value['groups'] for value in result['values']}
        request_rate = values['request_completion'][0]['value']
        call_rate = values['call_completion'][0]['value']
        self.assertNotEqual(request_rate, call_rate)
        self.assertAlmostEqual(request_rate, 19 / 24)
        self.assertAlmostEqual(call_rate, 41 / 48)

    def test_rejected_run_still_emits_full_sections(self):
        result = self._run('bad_mixing.json')
        for section in ('values', 'plans', 'lineage', 'cache_effects', 'rejected'):
            self.assertIn(section, result)
        self.assertTrue(any(item['code'] == 'version_mixing'
                            for item in result['rejected']))

    def test_cache_key_changes_with_definition_version(self):
        pinned_v1 = load('query_daily.json')
        pinned_v1['run_id'] = 'run-pinned-p95'
        pinned_v1['query']['metrics'] = [{'metric': 'latency_p95', 'version': 1}]
        v1_path = Path(self.temp.name) / 'v1.json'
        v1_path.write_text(json.dumps(pinned_v1, ensure_ascii=False), encoding='utf-8')
        run(str(v1_path), str(self.state), str(self.out))
        first = json.loads(self.out.read_text(encoding='utf-8'))
        first_key = next(item['cache_key'] for item in first['cache_effects'])

        pinned_v2 = load('query_daily.json')
        pinned_v2['run_id'] = 'run-pinned-p90'
        pinned_v2['query']['metrics'] = [{'metric': 'latency_p95', 'version': 2}]
        v2_path = Path(self.temp.name) / 'v2.json'
        v2_path.write_text(json.dumps(pinned_v2, ensure_ascii=False), encoding='utf-8')
        run(str(v2_path), str(self.state),
            str(Path(self.temp.name) / 'v2-out.json'))
        second = json.loads((Path(self.temp.name) / 'v2-out.json')
                            .read_text(encoding='utf-8'))
        second_key = next(item['cache_key'] for item in second['cache_effects'])
        self.assertNotEqual(first_key, second_key)
        self.assertTrue(canonical_digest({'a': 1}) == canonical_digest({'a': 1}))

    def test_missing_partition_is_rejected_not_zeroed(self):
        from metric_scope_registry.executor import Executor
        from metric_scope_registry.store import StateStore, ingest_document
        from metric_scope_registry.compiler import Compiler, normalize_definitions
        document = load('query_daily.json')
        store = StateStore(str(Path(self.temp.name) / 'missing-state'))
        ingest_document(store, document)
        registry = normalize_definitions(document['metric_definitions'])
        node = Compiler(registry).compile_ref('active_users', 1)
        digest_map = dict(store.index['partitions'])
        expected = set(digest_map)
        # 人为摘掉一天的分区指针，模拟明细或中间状态缺失。
        digest_map.pop('user_days/2026-11-11')
        executor = Executor(store, digest_map,
                            {name: document['tables'][name]['revision']
                             for name in document['tables']},
                            expected_partition_keys=expected)
        with self.assertRaises(ScopeError) as caught:
            executor.run_plan(node, ['day'], None)
        self.assertEqual(caught.exception.code, 'partition_missing')

    def test_pick_bridge_is_deterministic_and_does_not_duplicate(self):
        from metric_scope_registry.executor import Executor
        from metric_scope_registry.store import StateStore, ingest_document
        from metric_scope_registry.compiler import Compiler, normalize_definitions
        document = load('query_daily.json')
        definitions = document['metric_definitions'] + [{
            'name': 'picked_office_latency', 'version': 1, 'unit': 'ms',
            'expr': {
                'op': 'ref', 'metric': 'call_total', 'version': 1,
                'source_table': 'requests',
                'bridge': {'from': 'requests', 'to': 'calls', 'rule': 'pick',
                           'order_by': 'latency_ms', 'direction': 'desc'},
                'filters': [{'field': 'app', 'op': 'eq', 'value': 'office'}]}}]
        registry = normalize_definitions(definitions)
        node = Compiler(registry).compile_ref('picked_office_latency', 1)
        store = StateStore(str(Path(self.temp.name) / 'pick-state'))
        digest_map, revisions = ingest_document(store, document)
        executor = Executor(store, digest_map, revisions)
        groups, _ = executor.run_plan(node, [], None)
        state = next(iter(groups.values()))
        # 每个请求至多保留一条 office 调用，计数不超过请求总数。
        self.assertEqual(state['family'], 'scalar')
        self.assertLessEqual(state['value'], len(document['tables']['requests']['rows']))


if __name__ == '__main__':
    unittest.main()
