"""Local smoke tests; run: python test_app.py (uses temporary database)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

APP=Path(__file__).parent/'app.py'
PORT=18765
URL=f'http://127.0.0.1:{PORT}'

class WarehouseSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        env={**os.environ,'AURVA_DB_PATH':str(Path(cls.temp.name)/'test.db'),'AURVA_PORT':str(PORT)}
        cls.proc=subprocess.Popen([sys.executable,str(APP)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.get('/api/state')
                break
            except Exception:
                time.sleep(.07)
        else:
            raise RuntimeError('Server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.temp.cleanup()

    @classmethod
    def get(cls,path):
        return json.loads(urlopen(URL+path,timeout=3).read())

    @classmethod
    def post(cls,path,body,expected_status=200):
        request=Request(URL+path,json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
        try:
            with urlopen(request,timeout=3) as res:
                assert res.status==expected_status,(res.status,expected_status)
                return json.loads(res.read())
        except HTTPError as e:
            if e.code!=expected_status:
                raise
            return json.loads(e.read())

    def test_cuttable_paint_strips_and_catalogue(self):
        state = self.get('/api/state')
        active = {k['sku']: k for k in state['kits']}
        expected = {
            'PNFA3-91-0001': 15, 'PNFA3-91-0002': 15,
            'PNFA3-91-0003': 18, 'PN0A4-91-0002': 15,
            'PN0A4-91-0004': 12, 'PN0A4-91-0007': 12,
            'PN0A4-91-0005': 10, 'PN0A4-91-0006': 8,
            'PN0A4-91-0003': 15
        }
        for sku, pots in expected.items():
            self.assertEqual(active[sku]['paint_colours'], pots)
            self.assertFalse(active[sku]['stock_counted'])
            self.assertEqual(active[sku]['packed_qty'], 0)
        components = {c['code']: c for c in state['components']}
        for code in ['EMPTY-6POT']+[f'EMPTY-CUT-{i}' for i in range(1,6)]:
            self.assertEqual(components[code]['qty'], 0)
        # Five purchased six-pot strips can make two kits of fifteen pots.
        # The first pack creates a reusable three-pot remnant for the second.
        full = components['EMPTY-6POT']
        self.post(f'/api/components/{full["id"]}/count', {'qty': 5})
        kid = self.post('/api/kits', {'sku': 'PBN-CUT-15', 'name': 'Cutting test',
                                   'size': 'A4', 'paint_colours': 15})['id']
        canvas = self.post('/api/components', {'code':'CAN-CUT-15',
                           'name':'Paint cutting test canvas', 'unit':'pcs',
                           'qty':3, 'min_qty':0,'purchase_multiplier':1,
                           'purchase_unit':'canvas'})['id']
        self.post(f'/api/kits/{kid}/recipe', {
            'items':[{'component_id':canvas, 'qty_per_kit':1}]})
        self.post(f'/api/kits/{kid}/confirm-recipe',{})
        current = next(k for k in self.get('/api/state')['kits'] if k['id'] == kid)
        self.assertEqual(current['can_make'], 2)
        result = self.post(f'/api/kits/{kid}/pack', {'quantity':2})
        self.assertEqual(result['packed'],2)
        self.assertEqual(result['full_paint_strips_used'],5)
        after = self.get('/api/state')
        c = {x['code']: x for x in after['components']}
        self.assertEqual(c['EMPTY-6POT']['qty'],0)
        self.assertTrue(all(c[f'EMPTY-CUT-{i}']['qty']==0 for i in range(1,6)))
        self.assertEqual(next(k for k in after['kits'] if k['id']==kid)['packed_qty'],2)
        self.assertEqual(next(x for x in after['components'] if x['id']==canvas)['qty'],1)
        blocked = self.post(f'/api/kits/{kid}/pack', {'quantity':1}, 409)
        self.assertIn('connected',blocked['error'].lower())
        # One fifteen-pot kit leaves a connected three-pot remnant.
        self.post(f'/api/components/{full["id"]}/count', {'qty':3})
        result = self.post(f'/api/kits/{kid}/pack', {'quantity':1})
        self.assertEqual(result['packed'],1)
        c = {x['code']: x for x in self.get('/api/state')['components']}
        self.assertEqual(c['EMPTY-6POT']['qty'],0)
        self.assertEqual(c['EMPTY-CUT-3']['qty'],1)
        # The leftover can be reused for a future design without buying strips.
        kid3 = self.post('/api/kits', {'sku':'PBN-CUT-3','name':'Three-pot test',
                          'size':'A4','paint_colours':3})['id']
        canvas3 = self.post('/api/components', {'code':'CAN-CUT-3',
                            'name':'Three-pot cutting test canvas','unit':'pcs',
                            'qty':1,'min_qty':0,'purchase_multiplier':1,
                            'purchase_unit':'canvas'})['id']
        self.post(f'/api/kits/{kid3}/recipe', {
            'items':[{'component_id':canvas3,'qty_per_kit':1}]})
        self.post(f'/api/kits/{kid3}/confirm-recipe',{})
        result = self.post(f'/api/kits/{kid3}/pack', {'quantity':1})
        self.assertEqual(result['packed'],1)
        self.assertEqual(result['full_paint_strips_used'],0)
        c = {x['code']: x for x in self.get('/api/state')['components']}
        self.assertEqual(c['EMPTY-CUT-3']['qty'],0)

    def test_warehouse_flow(self):
        s=self.get('/api/state')
        self.assertGreaterEqual(len(s['components']),13)
        comps={c['code']:c for c in s['components']}
        ins=comps['HANG-INSTRUCT']
        r=self.post(f'/api/components/{ins["id"]}/receive',{'amount':2})
        self.assertEqual(r['received'],12)
        self.assertEqual(next(c for c in self.get('/api/state')['components'] if c['code']=='HANG-INSTRUCT')['qty'],12)

        # Create SKU and two artwork-specific components; no fake initial stock.
        kid=self.post('/api/kits',{'sku':'PBN-TEST-A4','name':'Sample A4 design','size':'A4','packed_qty':3})['id']
        cid=self.post('/api/components',{'code':'CAN-TEST-A4','name':'Test printed canvas A4','unit':'pcs','qty':8,'min_qty':2,'purchase_unit':'canvas','purchase_multiplier':1})['id']
        # Can't pack until the recipe is present and explicitly confirmed.
        blocked=self.post(f'/api/kits/{kid}/pack',{'quantity':2},409)
        self.assertIn('recipe',blocked['error'].lower())
        self.post(f'/api/kits/{kid}/recipe',{'items':[{'component_id':cid,'qty_per_kit':1},{'component_id':ins['id'],'qty_per_kit':1}]})
        self.post(f'/api/kits/{kid}/confirm-recipe',{})
        # Max possible pack = 8 canvases; attempts to pack 12 only pack 8.
        pack=self.post(f'/api/kits/{kid}/pack',{'quantity':12})
        self.assertEqual(pack['packed'],8)
        self.assertTrue(pack['limited'])
        state=self.get('/api/state')
        kit=next(k for k in state['kits'] if k['id']==kid)
        self.assertEqual(kit['packed_qty'],11)
        self.assertFalse(kit['low_stock'])
        self.assertEqual(next(c for c in state['components'] if c['id']==cid)['qty'],0)
        self.assertEqual(next(c for c in state['components'] if c['id']==ins['id'])['qty'],4)
        # Handover reduces packed kits but leaves components unchanged.
        handover=self.post(f'/api/kits/{kid}/handover',{'quantity':2})
        self.assertEqual(handover['balance'],9)
        state=self.get('/api/state')
        self.assertTrue(next(k for k in state['kits'] if k['id']==kid)['low_stock'])
        self.assertEqual(next(c for c in state['components'] if c['id']==ins['id'])['qty'],4)
        blocked=self.post(f'/api/kits/{kid}/handover',{'quantity':100},409)
        self.assertIn('Pack another batch',blocked['error'])
        # supplier price history
        sid=self.post('/api/suppliers',{'component_id':cid,'name':'Test print vendor','price':22,'url':'https://example.com/product'})['id']
        self.post(f'/api/suppliers/{sid}/update',{'price':25})
        s=self.get('/api/state')
        self.assertEqual([p['price'] for p in s['price_history'] if p['supplier_id']==sid],[25,22])
        # converting one brush set into separate brushes
        comps={c['code']:c for c in s['components']}
        src=comps['BRUSH-SET']
        self.post(f'/api/components/{src["id"]}/receive',{'amount':4})
        self.post('/api/convert',{'source_component_id':src['id'],'amount':2,'outputs':[
            {'component_id':comps['BRUSH-FINE']['id'],'amount':2},
            {'component_id':comps['BRUSH-MED']['id'],'amount':2},
            {'component_id':comps['BRUSH-BROAD']['id'],'amount':2}]})
        final={c['code']:c for c in self.get('/api/state')['components']}
        self.assertEqual(final['BRUSH-SET']['qty'],2)
        self.assertEqual(final['BRUSH-FINE']['qty'],2)
        self.assertGreater(len(self.get('/api/state')['activity']),5)

if __name__=='__main__':
    unittest.main(verbosity=2)
