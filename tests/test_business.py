import copy
import json
import tempfile
import unittest
from pathlib import Path


def load(name):
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / name).read_text(encoding='utf-8'))


from metric_scope_registry.facts import base_counts, validate_facts, inspect


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.document = load('catalog.json')

    def test_separate_grains(self):
        result = inspect(self.document)
        self.assertGreater(sum(row['rows'] for row in result['calls']), sum(row['rows'] for row in result['requests']))

    def test_daily_users_not_additive(self):
        tables = self.document['tables']
        daily = base_counts(tables, 'requests', ['day'])
        whole = base_counts(tables, 'requests', [])
        self.assertGreater(sum(row['users'] for row in daily), whole[0]['users'])

    def test_missing_foreign_key(self):
        tables = copy.deepcopy(self.document['tables'])
        tables['calls']['rows'][0]['request_id'] = 'missing'
        with self.assertRaises(ValueError):
            validate_facts(tables)

    def test_primary_key(self):
        tables = copy.deepcopy(self.document['tables'])
        tables['requests']['rows'].append(tables['requests']['rows'][0])
        with self.assertRaises(ValueError):
            validate_facts(tables)

    def test_duplicate_dimensions(self):
        with self.assertRaises(ValueError):
            base_counts(self.document['tables'], 'requests', ['day', 'day'])
