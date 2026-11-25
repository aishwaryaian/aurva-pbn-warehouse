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

    def test_a3_a4_confirmed_recipes_are_synced_but_locked(self):
        state=self.get('/api/state')
        kits={k['sku']: k for k in state['kits']}
        comps={c['id']:c for c in state['components']}
        bycode={c['code']:c for c in state['components']}
        expected={
            'PNFA3-91-0001':('A3',15),
            'PNFA3-91-0002':('A3',15),
            'PNFA3-91-0003':('A3',18),
            'PN0A4-91-0002':('A4',15),
            'PN0A4-91-0004':('A4',12),
            'PN0A4-91-0007':('A4',12),
            'PN0A4-91-0005':('A4',10),
            'PN0A4-91-0006':('A4',8),
            'PN0A4-91-0003':('A4',15)
        }
        self.assertFalse(any(k['size']=='A5' for k in kits.values()))
        for sku,(size,pots) in expected.items():
            k=kits[sku]
            self.assertEqual(k['size'],size)
            self.assertEqual(k['paint_fill_ml'],3)
            self.assertEqual(k['total_paint_fill_ml'],pots*3)
            self.assertFalse(k['recipe_ready'])
            self.assertIn('Adhesive',k['recipe_pending_reason'])
            self.assertFalse(k['stock_counted'])
            self.assertEqual(k['packed_qty'],0)
            recipe={comps[x['component_id']]['code']:x['qty_per_kit']
                    for x in k['recipe']}
            self.assertEqual(recipe,{
                'CANVAS-'+sku:1,
                'WOOD-CM':60 if size=='A3' else 42,
                'BRUSH-2SET':1,
                'BROCHURE-'+sku:1,
                'LABEL-'+sku:1,
                'ZIPLOCK':1,'HOOK':2,'HANG-INSTRUCT':1,'CORD-CM':50,
                'TUBE-A3' if size=='A3' else 'TUBE-A4':1})
            self.assertEqual(bycode['CANVAS-'+sku]['qty'],0)
            self.assertEqual(bycode['BROCHURE-'+sku]['qty'],0)
            self.assertEqual(bycode['LABEL-'+sku]['qty'],0)
            res=self.post(f'/api/kits/{k["id"]}/confirm-recipe',{},409)
            self.assertIn('adhesive',res['error'].lower())
            blocked=self.post(f'/api/kits/{k["id"]}/pack',{'quantity':1},409)
            self.assertIn('adhesive',blocked['error'].lower())
        self.assertEqual(bycode['BRUSH-2SET']['qty'],0)
        self.assertEqual(bycode['TUBE-A4']['qty'],0)
        # The deferred A5 stock is neither seeded nor marked as a confirmed recipe.
        self.assertFalse(any('A5' in x['code'] and x['code'].startswith('CANVAS-')
                             for x in bycode.values()))

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

    def test_unlock_batch_pack_and_alerts(self):
        s=self.get('/api/state')
        self.assertEqual(s['database']['engine'],'SQLite')
        kits={k['sku']:k for k in s['kits']}
        comps={c['code']:c for c in s['components']}
        # Uncounted 0-stock designs now show as low stock; parts get automatic alert levels.
        self.assertTrue(kits['PN0A4-91-0004']['low_stock'])
        self.assertEqual(comps['HOOK']['alert_level'],20)
        self.assertEqual(comps['CORD-CM']['alert_level'],500)
        self.assertEqual(comps['EMPTY-CUT-3']['alert_rule'],'none')
        goldi=kits['PN0A4-91-0004']
        # Unlocking skips the unmeasured adhesive only when explicitly asked.
        self.post(f'/api/kits/{goldi["id"]}/confirm-recipe',{'skip_pending':True})
        # Stock every recipe part for 3 kits, plus paint strips (12 pots = 2 strips/kit).
        for r in goldi['recipe']:
            self.post(f'/api/components/{r["component_id"]}/count',{'qty':r['qty_per_kit']*3})
        self.post(f'/api/components/{comps["EMPTY-6POT"]["id"]}/count',{'qty':6})
        res=self.post('/api/pack-batch',{'items':[
            {'kit_id':goldi['id'],'quantity':5},
            {'kit_id':kits['PN0A4-91-0005']['id'],'quantity':2},
            {'kit_id':kits['PN0A4-91-0006']['id'],'quantity':''}]})
        byid={r['kit_id']:r for r in res['results']}
        self.assertEqual(byid[goldi['id']]['packed'],3)
        self.assertTrue(byid[goldi['id']]['limited'])
        self.assertIn('adhesive',byid[kits['PN0A4-91-0005']['id']]['error'].lower())
        self.assertEqual(len(res['results']),2)
        after=self.get('/api/state')
        g=next(k for k in after['kits'] if k['id']==goldi['id'])
        self.assertEqual(g['packed_qty'],3)
        self.assertTrue(g['low_stock'])
        c={x['code']:x for x in after['components']}
        self.assertEqual(c['HOOK']['qty'],0)
        self.assertEqual(c['EMPTY-6POT']['qty'],0)
        self.assertTrue(c['HOOK']['low_stock'])
        self.post('/api/pack-batch',{'items':[]},400)

    def test_excel_bulk_upload(self):
        import base64, importlib.util, zipfile, io
        spec=importlib.util.spec_from_file_location('aurva_app',APP)
        appmod=importlib.util.module_from_spec(spec); spec.loader.exec_module(appmod)
        raw=urlopen(URL+'/api/export.xlsx',timeout=5).read()
        names=zipfile.ZipFile(io.BytesIO(raw)).namelist()
        self.assertIn('xl/worksheets/sheet1.xml',names)
        enc=lambda b: base64.b64encode(b).decode()
        # Re-uploading the downloaded file unchanged changes nothing.
        same=self.post('/api/import/preview',{'filename':'aurva.xlsx','data':enc(raw),'mode':'set'})
        self.assertEqual(same['counts']['new']+same['counts']['update']+same['counts']['error'],0)
        self.assertGreater(same['counts']['same'],20)
        comps={c['code']:c for c in self.get('/api/state')['components']}
        hook_before=comps['HOOK']['qty']
        book=appmod.xlsx_bytes([('Parts',['Part code','Part name','Unit','Stock quantity','Supplier','Price per unit (Rs)','Reorder link'],
            [['HOOK','','',40,'Hook shop',2.5,'https://example.com/hooks'],
             ['NEW-GLUE','Glue stick','pcs',12,'','',''],
             ['BAD-ROW','','',5,'','',''],
             ['ZIPLOCK','','kg',3,'','','']],[10]*7),
            ('Designs',['SKU','Packed stock'],[['PN0A4-91-0003',14]],[10,10])])
        prev=self.post('/api/import/preview',{'filename':'stock.xlsx','data':enc(book),'mode':'set'})
        self.assertEqual(prev['counts'],{'new':1,'update':2,'same':0,'error':2})
        # Preview must not save anything.
        self.assertEqual({c['code']:c for c in self.get('/api/state')['components']}['HOOK']['qty'],hook_before)
        done=self.post('/api/import/apply',{'filename':'stock.xlsx','data':enc(book),'mode':'set'})
        self.assertTrue(done['applied'])
        st=self.get('/api/state')
        c={x['code']:x for x in st['components']}
        self.assertEqual(c['HOOK']['qty'],40)
        self.assertEqual(c['NEW-GLUE']['qty'],12)
        self.assertNotIn('BAD-ROW',c)
        self.assertTrue(any(s['name']=='Hook shop' and s['url']=='https://example.com/hooks' for s in st['suppliers']))
        self.assertEqual(next(k for k in st['kits'] if k['sku']=='PN0A4-91-0003')['packed_qty'],14)
        # "Stock received" adds, and CSV works too.
        csv_data='Part code,Stock quantity\nHOOK,5\nNEW-GLUE,3\n'.encode()
        self.post('/api/import/apply',{'filename':'in.csv','data':enc(csv_data),'mode':'add'})
        c={x['code']:x for x in self.get('/api/state')['components']}
        self.assertEqual(c['HOOK']['qty'],45)
        self.assertEqual(c['NEW-GLUE']['qty'],15)
        bad=self.post('/api/import/preview',{'filename':'x.xlsx','data':enc(b'not a zip'),'mode':'set'},400)
        self.assertIn('Excel',bad['error'])
        nohead=self.post('/api/import/preview',{'filename':'x.csv','data':enc(b'a,b\n1,2\n'),'mode':'set'},400)
        self.assertIn('Part code',nohead['error'])


class LoginTests(unittest.TestCase):
    """Hosted mode: pages need a signed-in session cookie."""
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        env={**os.environ,'AURVA_DB_PATH':str(Path(cls.temp.name)/'t.db'),'AURVA_PORT':str(PORT+1),
             'AURVA_HOST':'0.0.0.0',
             'AURVA_USER':'tester','AURVA_PASSWORD':'correct-horse-battery'}
        cls.url=f'http://127.0.0.1:{PORT+1}'
        cls.proc=subprocess.Popen([sys.executable,str(APP)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urlopen(cls.url+'/login',timeout=3); break
            except Exception:
                time.sleep(.07)
        else:
            raise RuntimeError('Server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=5); cls.temp.cleanup()

    def call(self,path,body=None,cookie=None):
        headers={'Content-Type':'application/json'}
        if cookie: headers['Cookie']=cookie
        req=Request(self.url+path,json.dumps(body).encode() if body is not None else None,headers=headers,
                    method='POST' if body is not None else 'GET')
        try:
            with urlopen(req,timeout=3) as r:
                return r.status,r.headers,r.read()
        except HTTPError as e:
            return e.code,e.headers,e.read()

    def test_login_flow(self):
        self.assertEqual(self.call('/login')[0],200)
        self.assertEqual(self.call('/logo.svg')[0],200)
        self.assertEqual(self.call('/api/state')[0],401)
        status,_,body=self.call('/api/login',{'username':'tester','password':'wrong'})
        self.assertEqual(status,401)
        status,headers,_=self.call('/api/login',{'username':'tester','password':'correct-horse-battery'})
        self.assertEqual(status,200)
        cookie=headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertEqual(self.call('/api/state',cookie=cookie)[0],200)
        self.assertEqual(self.call('/',cookie=cookie)[0],200)
        self.assertEqual(self.call('/api/state',cookie=cookie[:-3]+'abc')[0],401)

if __name__=='__main__':
    unittest.main(verbosity=2)
