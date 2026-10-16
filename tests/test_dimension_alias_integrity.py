"""One physical size across form inputs, compatibility fields and migrations."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from agent import Agent, Memory
from order_model import normalize_state, STATE_SCHEMA_VERSION
from tools import prepare_handoff


class DimensionAliasIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = Memory(Path(self.directory.name) / 'orders.sqlite3')
        self.agent = Agent(self.memory, 'dimension-aliases')

    def test_canonical_dimension_wins_over_size_in_either_patch_key_order(self):
        self.agent.chat('做500张名片，300g白卡纸，双面四色，下周内')
        for patch in ({'size': 'A4', 'dimensions': {'finishedSize': 'A5'}},
                      {'dimensions': {'finishedSize': 'A5'}, 'size': 'A4'}):
            with self.subTest(patch=patch):
                result = self.agent.chat('按表单确认', patch)
                self.assertEqual(result['order']['size'], '148×210 mm')
                self.assertEqual(result['order']['dimensions']['finishedSize'], '148×210 mm')
                self.assertEqual(result['canonicalOrder']['measurements']['finishedSize']['value']['width'], 148)
                mapped = prepare_handoff(result['order'])['mappedOrder']
                self.assertEqual(mapped['size'], mapped['dimensions']['finishedSize'])
                self.assertEqual(mapped['measurements']['finishedSize']['width'], 148)

    def test_box_compatibility_alias_updates_canonical_measurement_and_persists(self):
        self.agent.chat('天地盖包装盒，内尺寸60×40×20cm，外尺寸62×42×22cm')
        result = self.agent.chat('调整内尺寸', {'productSpecs': {'boxSizeInner': '50×30×10cm'}})
        self.assertEqual(result['order']['dimensions']['packageInnerSize'], '500×300×100 mm')
        self.assertEqual(result['canonicalOrder']['measurements']['packageInnerSize']['value']['width'], 500)
        self.assertEqual(result['order']['dimensions']['packageOuterSize'], '620×420×220 mm')
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual(restored['order']['productSpecs']['boxSizeInner'], '500×300×100 mm')
        self.assertEqual(restored['order']['measurements']['packageInnerSize']['depth'], 100)

    def test_explicit_canonical_box_dimension_wins_over_spec_alias(self):
        self.agent.chat('天地盖包装盒，内尺寸60×40×20cm')
        result = self.agent.chat('确认尺寸', {
            'dimensions': {'packageInnerSize': '50×30×10cm'},
            'productSpecs': {'boxSizeInner': '55×35×15cm'},
        })
        self.assertEqual(result['order']['productSpecs']['boxSizeInner'], '500×300×100 mm')
        self.assertEqual(result['order']['dimensions']['packageInnerSize'], '500×300×100 mm')

    def test_same_value_dimension_confirmation_only_confirms_its_own_aliases(self):
        self.agent.chat('折页成品210×297mm，展开420×297mm')
        for field in ('size', 'dimensions.finishedSize', 'dimensions.expandedSize'):
            self.agent.state['fieldMeta'][field]['confidence'] = .4
            self.agent.state['fieldMeta'][field]['status'] = 'pending_confirmation'
        old_evidence = deepcopy(self.agent.state['fieldMeta']['dimensions.expandedSize']['evidence'])
        result = self.agent.chat('确认成品尺寸', {'dimensions': {'finishedSize': '210×297 mm'}})
        self.assertEqual(result['fieldMeta']['dimensions.finishedSize']['confidence'], 1)
        self.assertEqual(result['fieldMeta']['size']['confidence'], 1)
        self.assertEqual(result['fieldMeta']['dimensions.expandedSize']['confidence'], .4)
        self.assertEqual(result['fieldMeta']['dimensions.expandedSize']['evidence'], old_evidence)
        self.assertEqual(result['canonicalOrder']['measurements']['expandedSize']['status'], 'pending_confirmation')

    def test_same_value_size_confirmation_resolves_canonical_alias(self):
        self.agent.chat('名片90×54mm')
        self.agent.state['fieldMeta']['dimensions.finishedSize']['confidence'] = .4
        self.agent.state['fieldMeta']['dimensions.finishedSize']['status'] = 'pending_confirmation'
        result = self.agent.chat('确认尺寸', {'size': '90×54 mm'})
        self.assertEqual(result['canonicalOrder']['measurements']['finishedSize']['status'], 'confirmed')

    def test_uncertain_replacement_does_not_mark_conflict_resolved(self):
        self.agent.chat('名片用300g铜版纸')
        self.agent._update_order({'paper': '白卡纸'}, source='model', confidence=.4)
        self.assertFalse(self.agent.state['conflicts'][-1]['resolved'])
        self.agent.chat('确认用白卡纸', {'paper': '白卡纸'})
        self.assertTrue(self.agent.state['conflicts'][-1]['resolved'])

    def test_legacy_jis_b_sizes_keep_physical_dimensions_and_original_evidence(self):
        old = {'schemaVersion': 2, 'order': {'productType': '名片', 'size': 'B5',
               'items': [{'itemId': 'b4', 'productType': '海报', 'size': 'B4'},
                         {'itemId': 'explicit', 'productType': '海报', 'size': '176×250 mm'}]},
               'fieldMeta': {'size': {'value': 'B5', 'source': 'user', 'confidence': 1,
                                     'evidence': {'quote': 'B5', 'source': 'user_utterance'}}}}
        migrated = normalize_state(deepcopy(old))
        self.assertEqual(migrated['schemaVersion'], STATE_SCHEMA_VERSION)
        self.assertEqual(migrated['order']['size'], '182×257 mm')
        self.assertEqual(migrated['order']['items'][0]['size'], '257×364 mm')
        self.assertEqual(migrated['order']['items'][1]['size'], '176×250 mm')
        self.assertEqual(migrated['fieldMeta']['size']['evidence']['quote'], 'B5')
        self.assertEqual(migrated['fieldMeta']['size']['value'], '182×257 mm')
        self.assertEqual(migrated['measurementMigrations'][0]['raw'], 'B5')
        self.assertEqual(normalize_state(deepcopy(migrated)), migrated)
        current = normalize_state({'schemaVersion': 3, 'order': {'productType': '名片', 'size': 'B5'}})
        self.assertEqual(current['order']['size'], '176×250 mm')


if __name__ == '__main__':
    unittest.main()
