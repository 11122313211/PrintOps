"""Provenance survival across the single-product to multi-product split (TD-02).

One message with N distinct products must produce exactly N order items — a
restated mention ("…，名片90×54mm") attaches parameters to the product's first
item instead of splitting a duplicate — and the provenance accumulated while
the order was still single-product must move to ``items.{itemId}.*`` keys so
the item badge and confidence views keep their data.
"""
from pathlib import Path
import tempfile
import unittest

from agent import Agent, Memory


class MultiProductProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = Memory(Path(self.directory.name) / 'orders.sqlite3')
        self.agent = Agent(self.memory, 'multi-provenance')

    def item_meta_keys(self, state):
        return sorted(key for key in state.get('fieldMeta', {}) if key.startswith('items.'))

    def test_repeated_product_mentions_yield_one_item_per_product(self):
        result = self.agent.chat('帮我做名片和海报，名片90×54mm 300g铜版纸，海报A2 157g哑粉纸')
        items = result['order']['items']
        self.assertEqual([item['itemId'] for item in items], ['item-1', 'item-2'])
        self.assertEqual([item['productType'] for item in items], ['名片', '海报'])
        self.assertEqual(items[0]['paper'], '300 g/m² 铜版纸')
        self.assertEqual(items[0]['size'], '90×54 mm')
        self.assertEqual(items[0]['productSpecs'].get('cardStock'), '300 g/m² 铜版纸')
        self.assertEqual(items[1]['paper'], '157 g/m² 哑粉纸')
        self.assertEqual(items[1]['size'], '420×594 mm')
        # Every item carries its own item-scoped provenance keys.
        for index, fields in ((0, ('productType', 'paper', 'size', 'dimensions.finishedSize')),
                              (1, ('productType', 'paper', 'size', 'dimensions.finishedSize'))):
            for field in fields:
                key = f"items.item-{index + 1}.{field}"
                self.assertIn(key, result['fieldMeta'], key)
                self.assertTrue(result['fieldMeta'][key].get('evidence'), key)
        # No duplicate stub items and no stale mention-scoped keys survive.
        self.assertNotIn('items.item-3.productType', result['fieldMeta'])
        self.assertNotIn('items.item-4.productType', result['fieldMeta'])
        for candidate in result['fieldCandidates']:
            self.assertNotIn('items.item-3.', candidate['field'])
            self.assertNotIn('items.item-4.', candidate['field'])

    def test_first_multi_product_message_migrates_single_product_provenance(self):
        self.agent.chat('做500张名片，300g铜版纸')
        top_level = self.agent.state['fieldMeta']
        self.assertNotIn('items', top_level)  # still a single-product order
        paper_before = top_level['paper']
        quantity_before = top_level['quantity']
        result = self.agent.chat('帮我做名片和海报，名片90×54mm，海报A2')
        items = result['order']['items']
        self.assertEqual([item['productType'] for item in items], ['名片', '海报'])
        meta = result['fieldMeta']
        # paper/quantity provenance followed the 名片 item verbatim.
        for field, before in (('paper', paper_before), ('quantity', quantity_before)):
            migrated = meta.get(f'items.item-1.{field}')
            self.assertIsNotNone(migrated, field)
            self.assertEqual(migrated['value'], before['value'], field)
            self.assertEqual(migrated['status'], before['status'], field)
            self.assertEqual(migrated['confidence'], before['confidence'], field)
            self.assertEqual(migrated.get('evidence'), before.get('evidence'), field)
        self.assertEqual(meta['items.item-1.quantityValue']['value'], 500)
        self.assertEqual(meta['items.item-1.quantityUnit']['value'], '张')
        # The split turn's own clause data lands on the first item as well.
        self.assertIn('items.item-1.size', meta)
        self.assertIn('items.item-1.productType', meta)
        self.assertIn('items.item-2.productType', meta)
        # No migrated product-scoped keys remain at the top level; order-scope
        # keys stay where they were.
        for key in ('paper', 'quantity', 'quantityValue', 'quantityUnit',
                    'productSpecs.cardStock', 'size'):
            self.assertNotIn(key, meta, key)
        for key in ('productType', 'productTypes', 'items'):
            self.assertIn(key, meta, key)
        # Order values are untouched by the provenance move.
        self.assertEqual(result['order']['paper'], '300 g/m² 铜版纸')
        self.assertEqual(result['order']['quantity'], '500 张')
        # The migration survives persistence.
        restored = Agent(self.memory, self.agent.id).state['fieldMeta']
        self.assertEqual(restored['items.item-1.paper']['value'], paper_before['value'])
        self.assertEqual(restored['items.item-1.paper'].get('evidence'), paper_before.get('evidence'))

    def test_single_product_message_keeps_top_level_provenance(self):
        result = self.agent.chat('做500张名片，300g铜版纸')
        self.assertEqual(result['order']['productType'], '名片')
        self.assertEqual(result['order'].get('items') or [], [])
        for key in ('paper', 'quantity', 'productSpecs.cardStock'):
            self.assertIn(key, result['fieldMeta'], key)
        self.assertEqual(self.item_meta_keys(result), [])

    def test_unattributable_provenance_stays_top_level(self):
        self.agent.chat('做500张海报，157g哑粉纸')
        paper_before = self.agent.state['fieldMeta']['paper']
        result = self.agent.chat('做500张名片和1000张折页')
        self.assertEqual([item['productType'] for item in result['order']['items']], ['名片', '折页'])
        # The pre-split order was 海报; no item carries that product, so the
        # paper provenance cannot be attributed and stays at the top level.
        self.assertEqual(result['fieldMeta']['paper'], paper_before)
        self.assertNotIn('items.item-1.paper', result['fieldMeta'])
        self.assertNotIn('items.item-2.paper', result['fieldMeta'])

    def test_split_projection_provenance_follows_the_first_item(self):
        # 海报 was the single product, but the splitting message lists 名片
        # first: provenance the split patch itself wrote (the item-1
        # compatibility projection) follows item 1, while the 海报-era entries
        # follow the matching item.
        self.agent.chat('做500张海报，157g哑粉纸')
        paper_before = self.agent.state['fieldMeta']['paper']
        result = self.agent.chat('做1000张名片和500张海报，海报A2')
        self.assertEqual([item['productType'] for item in result['order']['items']], ['名片', '海报'])
        meta = result['fieldMeta']
        # The 海报-era paper provenance moved to the 海报 item (second mention).
        migrated = meta.get('items.item-2.paper')
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated['value'], paper_before['value'])
        self.assertEqual(migrated.get('evidence'), paper_before.get('evidence'))
        self.assertNotIn('paper', meta)
        # The A2 size belongs to the 海报 clause; the 名片 clause stated none,
        # so item 1 carries no size provenance at all.
        self.assertEqual(meta['items.item-2.size']['value'], '420×594 mm')
        self.assertNotIn('items.item-1.size', meta)
        # No migrated product-scoped keys remain at the top level.
        for key in ('paper', 'quantity', 'quantityValue', 'quantityUnit', 'size'):
            self.assertNotIn(key, meta, key)


if __name__ == '__main__':
    unittest.main()
