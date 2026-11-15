import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError

from carbon_uncertainty.client import Client
from carbon_uncertainty.comparison import compare_revisions
from carbon_uncertainty.domain import validate
from carbon_uncertainty.repository import Repository
from carbon_uncertainty.service import make_server
from carbon_uncertainty.uncertainty import analyze_uncertainty


FIXTURE = json.loads(Path('fixtures/demo.json').read_text())
REVISED = json.loads(Path('fixtures/demo_revised.json').read_text())


class UncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.data = copy.deepcopy(FIXTURE)

    def test_shared_source_is_not_independent_evidence(self):
        result = analyze_uncertainty(self.data)
        grid = next(source for source in result['sources'] if source['source'] == 'grid')
        # grid 因子在 a1、a2 两行出现，但只有一个共享驱动、一组来源级硬界
        self.assertTrue(grid['shared_driver'])
        self.assertEqual(grid['activity_count'], 2)
        # 完全相关下界宽 = 各行界宽直接相加：(60-40)+(30-20)=30，而非独立化的 sqrt(20^2+10^2)
        self.assertEqual(grid['hard_low_kg'], 60.0)
        self.assertEqual(grid['hard_high_kg'], 90.0)
        self.assertEqual(grid['half_width_kg'], 15.0)

    def test_interval_only_supports_hard_bounds_not_coverage(self):
        data = copy.deepcopy(self.data)
        data['factors'] = [data['factors'][0]]
        data['activities'] = [data['activities'][0], data['activities'][1]]
        result = analyze_uncertainty(data)
        self.assertEqual(result['total']['hard_low_kg'], 60.0)
        self.assertEqual(result['total']['hard_high_kg'], 90.0)
        self.assertIsNone(result['total']['sd_independent_sources_kg'])
        self.assertIsNone(result['covariance'])
        self.assertTrue(any('覆盖率' in claim for claim in result['unsupported_claims']))

    def test_normal_independent_sd_and_frechet_bounds(self):
        data = {
            'factors': [
                {'id': 'e1', 'source': 'elec', 'unit': 'kWh', 'kg_per_unit': 0.5,
                 'uncertainty': {'kind': 'normal', 'sd': 0.1}},
                {'id': 'g1', 'source': 'gas', 'unit': 'L', 'kg_per_unit': 2.0,
                 'uncertainty': {'kind': 'normal', 'sd': 0.2}},
            ],
            'activities': [
                {'id': 'x', 'amount': 1000, 'unit': 'kWh', 'factor': 'e1', 'allocation': {'P': 1}},
                {'id': 'y', 'amount': 10, 'unit': 'L', 'factor': 'g1',
                 'allocation': {'P': 0.5, 'Q': 0.5}},
            ],
        }
        validate(data)
        result = analyze_uncertainty(data)
        # 独立来源假设：sqrt(100^2 + 2^2)
        self.assertAlmostEqual(result['total']['sd_independent_sources_kg'], 100.019998, places=5)
        # 相关性未知时的可行 sd 界：|100-2| 到 100+2
        self.assertEqual(result['total']['sd_range_unknown_correlation_kg'], [98.0, 102.0])
        self.assertIsNone(result['total']['hard_low_kg'])
        self.assertIsNotNone(result['covariance'])
        self.assertTrue(any('相关性未知' in claim for claim in result['unsupported_claims']))

    def test_unit_conversion_and_allocation_keep_dependency(self):
        data = {
            'factors': [
                {'id': 'e1', 'source': 'elec', 'unit': 'kWh', 'kg_per_unit': 0.5,
                 'uncertainty': {'kind': 'normal', 'sd': 0.1}},
            ],
            'activities': [
                {'id': 'x', 'amount': 1, 'unit': 'MWh', 'factor': 'e1',
                 'allocation': {'P': 0.6, 'Q': 0.4}},
            ],
        }
        validate(data)
        result = analyze_uncertainty(data)
        source = result['sources'][0]
        # 1 MWh = 1000 kWh，sd = 1000 * 0.1 = 100；分摊不独立化
        self.assertEqual(source['activities'][0]['converted_amount_kg_basis'], 1000.0)
        self.assertEqual(source['sd_kg'], 100.0)
        products = {row['product']: row for row in result['products']}
        # 同一驱动分到两个产品，独立来源假设下各产品 sd 与中心值同比例
        self.assertEqual(products['P']['sd_independent_sources_kg'], 60.0)
        self.assertEqual(products['Q']['sd_independent_sources_kg'], 40.0)

    def test_missing_factor_is_gap_never_zero(self):
        self.data['activities'][0]['factor'] = 'unavailable'
        result = analyze_uncertainty(self.data)
        gap_refs = [gap['ref'] for gap in result['information_gaps'] if gap['kind'] == 'missing_factor']
        self.assertEqual(gap_refs, ['a1'])
        # a1 中心值 50kg 被排除而非按零计入
        self.assertEqual(result['total']['center_kg'], 50.0)
        self.assertTrue(any('不能按零' in gap['reason'] for gap in result['information_gaps']))

    def test_priority_ranking_prefers_largest_interval_width(self):
        result = analyze_uncertainty(self.data)
        interval_gaps = [gap for gap in result['information_gaps'] if gap['kind'] == 'interval_only_source']
        self.assertEqual(interval_gaps[0]['ref'], 'grid')
        self.assertEqual(interval_gaps[0]['rank'], 1)
        self.assertEqual(interval_gaps[0]['top_activity_id'], 'a1')

    def test_mixed_interval_normal_has_no_hard_bounds(self):
        result = analyze_uncertainty(self.data)
        self.assertIsNone(result['total']['hard_low_kg'])
        self.assertIsNone(result['total']['sd_independent_sources_kg'])
        self.assertTrue(any('混合' in claim for claim in result['unsupported_claims']))

    def test_mixed_kind_inside_one_source_is_flagged(self):
        data = copy.deepcopy(self.data)
        data['factors'].append({
            'id': 'grid-alt', 'source': 'grid', 'unit': 'kWh', 'kg_per_unit': 0.5,
            'uncertainty': {'kind': 'normal', 'sd': 0.05},
        })
        data['activities'][1]['factor'] = 'grid-alt'
        validate(data)
        result = analyze_uncertainty(data)
        grid = next(source for source in result['sources'] if source['source'] == 'grid')
        self.assertIn('unsupported', grid)
        self.assertIsNone(grid['hard_low_kg'])
        self.assertIsNone(grid['sd_kg'])


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.old = copy.deepcopy(FIXTURE)
        self.new = copy.deepcopy(REVISED)

    def test_bridge_separates_revision_from_operations(self):
        result = compare_revisions(self.old, self.new)
        bridge = result['bridge_total_kg']
        # 手工核算：因子效应 -7.5，分摊效应 0，经营活动效应 +18.5
        self.assertEqual(bridge['factor_effect'], -7.5)
        self.assertEqual(bridge['allocation_effect'], 0.0)
        self.assertEqual(bridge['activity_effect'], 18.5)
        self.assertTrue(result['reconciliation_ok'])
        self.assertEqual(result['known_total_kg']['old'], 100.0)
        self.assertEqual(result['known_total_kg']['new'], 111.0)

    def test_product_level_bridge_reconciles(self):
        result = compare_revisions(self.old, self.new)
        p1 = next(row for row in result['product_bridge'] if row['product'] == 'p1')
        # 55 - 5.5 + 4.5 + 13.5 = 67.5
        self.assertEqual(p1['old_kg'], 55.0)
        self.assertEqual(p1['factor_effect_kg'], -5.5)
        self.assertEqual(p1['allocation_effect_kg'], 4.5)
        self.assertEqual(p1['activity_effect_kg'], 13.5)
        self.assertEqual(p1['new_kg'], 67.5)
        for row in result['product_bridge']:
            total = (row['old_kg'] + row['factor_effect_kg'] + row['allocation_effect_kg']
                     + row['activity_effect_kg'] + row['added_effect_kg']
                     + row['removed_effect_kg'] + row['relink_effect_kg'])
            self.assertAlmostEqual(total, row['new_kg'], places=6)

    def test_factor_revision_is_detected_by_field(self):
        result = compare_revisions(self.old, self.new)
        revisions = {row['factor_id']: row for row in result['factor_revisions']}
        self.assertEqual(revisions['grid-v1']['fields'], ['kg_per_unit', 'uncertainty'])
        self.assertEqual(revisions['fuel-v1']['fields'], ['uncertainty'])

    def test_added_missing_factor_activity_stays_out_of_center_bridge(self):
        result = compare_revisions(self.old, self.new)
        self.assertEqual(result['missing_factor_activities']['new'], ['a4'])
        log = next(row for row in result['activity_changelog'] if row['activity_id'] == 'a4')
        self.assertIn('added_activity', log['changes'])
        self.assertIn('missing_factor', log['changes'])
        # a4 不贡献任何中心值效应
        self.assertEqual(result['bridge_total_kg']['added_effect'], 0.0)

    def test_factor_relink_is_operational_not_revision(self):
        new = copy.deepcopy(self.old)
        new['activities'][1]['factor'] = 'fuel-v1'
        new['activities'][1]['unit'] = 'L'
        new['activities'][1]['amount'] = 5
        result = compare_revisions(self.old, new)
        self.assertTrue(result['reconciliation_ok'])
        self.assertEqual(result['bridge_total_kg']['factor_effect'], 0.0)
        self.assertEqual(result['bridge_total_kg']['relink_effect'], -12.5)
        log = next(row for row in result['activity_changelog'] if row['activity_id'] == 'a2')
        self.assertIn('factor_relink', log['changes'])
        self.assertIn('relink_effect', result['interpretation']['operational_effects'])
        self.assertNotIn('relink_effect', result['interpretation']['data_revision_effects'])

    def test_added_and_removed_activities(self):
        new = copy.deepcopy(self.old)
        new['activities'] = [row for row in new['activities'] if row['id'] != 'a3']
        new['activities'].append({
            'id': 'a5', 'amount': 20, 'unit': 'L', 'factor': 'fuel-v1',
            'allocation': {'p1': 1},
        })
        result = compare_revisions(self.old, new)
        self.assertTrue(result['reconciliation_ok'])
        # a3 移除：-25；a5 新增：+50
        self.assertEqual(result['bridge_total_kg']['removed_effect'], -25.0)
        self.assertEqual(result['bridge_total_kg']['added_effect'], 50.0)

    def test_historical_revision_not_overwritten(self):
        old_uncertainty = analyze_uncertainty(self.old)
        new_uncertainty = analyze_uncertainty(self.new)
        # 旧版本按旧因子（0.5）解释，新版本按新因子（0.45）解释
        self.assertEqual(old_uncertainty['total']['center_kg'], 100.0)
        self.assertEqual(new_uncertainty['total']['center_kg'], 111.0)
        result = compare_revisions(self.old, self.new)
        self.assertEqual(result['uncertainty_change']['old_total']['center_kg'], 100.0)
        self.assertEqual(result['uncertainty_change']['new_total']['center_kg'], 111.0)

    def test_identical_revision_has_zero_delta(self):
        result = compare_revisions(self.old, self.old)
        self.assertEqual(result['known_total_kg']['delta'], 0.0)
        self.assertTrue(all(value == 0.0 for value in result['bridge_total_kg'].values()))
        self.assertTrue(result['reconciliation_ok'])


class HttpAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.repository = Repository(Path(self.directory.name) / 'data.sqlite3')
        self.server = make_server(self.repository)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f'http://127.0.0.1:{self.server.server_port}')

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()

    def test_uncertainty_endpoint_is_version_pinned(self):
        first = self.client.import_data(copy.deepcopy(FIXTURE), 'first')
        second = self.client.import_data(copy.deepcopy(REVISED), 'second')
        latest = self.client.uncertainty()
        pinned = self.client.uncertainty(first['revision'])
        self.assertEqual(latest['revision'], second['revision'])
        self.assertEqual(pinned['revision'], first['revision'])
        self.assertEqual(pinned['result']['total']['center_kg'], 100.0)
        self.assertEqual(latest['result']['total']['center_kg'], 111.0)
        self.assertEqual(pinned['digest'], first['digest'])

    def test_comparison_endpoint_default_and_explicit(self):
        first = self.client.import_data(copy.deepcopy(FIXTURE), 'first')
        second = self.client.import_data(copy.deepcopy(REVISED), 'second')
        defaulted = self.client.comparison()
        explicit = self.client.comparison(first['revision'], second['revision'])
        self.assertEqual(defaulted['base_revision'], first['revision'])
        self.assertEqual(defaulted['target_revision'], second['revision'])
        self.assertEqual(defaulted['result']['bridge_total_kg'], explicit['result']['bridge_total_kg'])
        self.assertTrue(explicit['result']['reconciliation_ok'])

    def test_comparison_needs_two_revisions(self):
        self.client.import_data(copy.deepcopy(FIXTURE), 'only')
        with self.assertRaises(HTTPError) as caught:
            self.client.comparison()
        self.assertEqual(caught.exception.code, 400)

    def test_unknown_path_and_missing_revision(self):
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/uncertainty?revision=99')
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/nope')
        self.assertEqual(caught.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
