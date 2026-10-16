"""Conversation corrections must target the same product and normalized field."""
from pathlib import Path
import tempfile
import unittest

from agent import Agent, Memory


class ChatItemContextTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = Memory(Path(self.directory.name) / 'orders.sqlite3')
        self.agent = Agent(self.memory, 'item-context')

    def test_negative_material_removes_canonical_grammage_value(self):
        for negative in ('不要300g铜版纸', '不要300克铜版纸'):
            with self.subTest(negative=negative):
                self.agent.chat('做500张名片，300g铜版纸')
                result = self.agent.chat(negative)
                self.assertEqual(result['order']['paper'], '')
                self.assertNotIn('paper', result['fieldMeta'])
                rejected = [c for c in result['fieldCandidates'] if c['negated'] and c['field'] == 'paper']
                self.assertTrue(rejected)
                self.assertEqual(rejected[-1]['rawText'], negative)

    def test_denial_does_not_remove_different_grammage_or_new_replacement(self):
        self.agent.chat('做500张名片，250g铜版纸')
        self.assertIn('250', self.agent.chat('不要300g铜版纸')['order']['paper'])
        result = self.agent.chat('不要250g铜版纸，改300g白卡纸')
        self.assertIn('300', result['order']['paper'])
        self.assertIn('白卡纸', result['order']['paper'])

    def test_new_explicit_item_focus_drives_category_specific_perception(self):
        self.agent.chat('500张名片和1000张标签')
        result = self.agent.chat('第2项改成可移胶')
        self.assertEqual(result['order']['items'][1]['productSpecs'].get('adhesive'), '可移胶')
        self.assertNotIn('adhesive', result['order']['items'][0]['productSpecs'])
        self.assertIn('items.item-2.productSpecs.adhesive', result['fieldMeta'])

    def test_api_item_focus_and_persisted_focus_drive_followups(self):
        self.agent.chat('500张名片和1000张标签')
        self.agent.chat('改成可移胶', item_index=1)
        restored = Agent(self.memory, self.agent.id)
        result = restored.chat('改成强粘胶')
        self.assertEqual(result['order']['items'][1]['productSpecs'].get('adhesive'), '强粘胶')
        self.assertNotIn('adhesive', result['order']['items'][0]['productSpecs'])

    def test_invalid_item_focus_cannot_change_any_product(self):
        self.agent.chat('500张名片和1000张标签')
        result = self.agent.chat('第9项改成可移胶')
        self.assertEqual(result['toolResult']['reason'], 'item_not_found')
        for item in result['order']['items']:
            self.assertNotIn('adhesive', item['productSpecs'])


if __name__ == '__main__':
    unittest.main()
