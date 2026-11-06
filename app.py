"""Aurva World PBN Warehouse — local or private Railway-hosted web app.

For Railway: configure AURVA_USER, AURVA_PASSWORD and a persistent
volume mounted at /data (with AURVA_DB_PATH=/data/aurva.db).
For local use, run python app.py and open http://127.0.0.1:8765.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
RAILWAY = bool(os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('RAILWAY_PROJECT_ID'))
MOUNT = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '')
DB = Path(os.environ.get('AURVA_DB_PATH', str(Path(MOUNT) / 'aurva.db') if MOUNT else str(ROOT / 'data' / 'aurva.db')))
HOST = os.environ.get('AURVA_HOST', '0.0.0.0' if RAILWAY else '127.0.0.1')
PORT = int(os.environ.get('PORT', os.environ.get('AURVA_PORT', '8765')))
AUTH_USER = os.environ.get('AURVA_USER', '')
AUTH_PASSWORD = os.environ.get('AURVA_PASSWORD', '')
SECURE_MODE = RAILWAY or HOST not in ('127.0.0.1', 'localhost', '::1')
LOCK = threading.RLock()
THRESHOLD = 10

# Verified active PBN catalogue, without A5 variants pending phase-out count.
# Barcode strings deliberately retain their leading zeros.
CATALOGUE_VERSION = 'aurva_pbn_active_catalogue_2026_11_06_v1'
PRODUCT_CATALOGUE = [
    ('PNFA3-91-0001', 'The Painted Gateway', 'Where Walls Become Art', 'A3', 'EAN-13-0608028960384', 1299, 1299),
    ('PNFA3-91-0002', 'The Folk Trance', 'Echoes of the Desert Sands', 'A3', 'EAN-13-0608028960360', 1299, 1299),
    ('PNFA3-91-0003', 'Varanasi Ghats', 'Where the Sacred River Meets the Soul', 'A3', 'EAN-13-0608028960483', 1199, 1199),
    ('PN0A4-91-0002', 'The Reading Nook', 'Stories, Silence, and Sunlight', 'A4', 'EAN-13-0608028960407', 899, 899),
    ('PN0A4-91-0004', 'Goldi, the Fish', 'Grace in Motion', 'A4', 'EAN-13-0608028960445', 899, 899),
    ('PN0A4-91-0007', 'Olive Ridley Sea Turtle', 'The Ocean Wanderer', 'A4', 'EAN-13-0608028960469', 899, 899),
    ('PN0A4-91-0005', 'The Sarus Crane', 'The Eternal Pair', 'A4', 'EAN-13-0608028960476', 899, 899),
    ('PN0A4-91-0006', 'The Indian Peacock', 'The Monsoon Dancer', 'A4', 'EAN-13-0608028960452', 899, 899),
    ('PN0A4-91-0003', 'The Good Old Days', 'Whiskers at the Café', 'A4', 'EAN-13-0608028960377', 899, 899),
]


def check_barcode(barcode):
    """Allow blank or user-provided format; verify explicitly labelled EAN-13."""
    barcode = str(barcode or '').strip()
    if len(barcode) > 80:
        raise Problem('Barcode is too long.')
    if barcode.startswith('EAN-13-'):
        digits = barcode[7:]
        if len(digits) != 13 or not digits.isascii() or not digits.isdigit():
            raise Problem('An EAN-13 barcode must have exactly 13 digits (including leading zeros).')
        total = sum(int(n) * (1 if i % 2 == 0 else 3) for i,n in enumerate(digits[:12]))
        expected = (10-total%10)%10
        if int(digits[-1]) != expected:
            raise Problem('EAN-13 checksum failed; check the barcode printed on the product.')
    return barcode


def paint_colour_count(value):
    """Count of individual paint pots in one kit; blank means unknown."""
    if value is None or str(value).strip() == '':
        return None
    n = positive_int(value, 'Paint colours per kit')
    if n > 240:
        raise Problem('Paint colours per kit must be 240 or fewer.')
    return n


def sync_paint_strip_recipe(conn, kit_id, colours):
    """Update recipe requirements without changing existing physical stock."""
    if colours is None:
        # Existing recipe remains for human review when count is cleared.
        conn.execute('UPDATE kits SET recipe_ready=0 WHERE id=?', (kit_id,))
        return
    strip = conn.execute('SELECT id FROM components WHERE code=?', ('EMPTY-6POT',)).fetchone()
    if not strip:
        raise Problem('Missing EMPTY-6POT component.')
    strips = (colours + 5) // 6
    conn.execute("""INSERT INTO recipes (kit_id,component_id,qty_per_kit)
                    VALUES (?,?,?) ON CONFLICT(kit_id,component_id)
                    DO UPDATE SET qty_per_kit=excluded.qty_per_kit""",
                 (kit_id,strip['id'],strips))
    conn.execute('UPDATE kits SET recipe_ready=0 WHERE id=?', (kit_id,))


def optional_price(value, label):
    if value is None or value == '':
        return None
    return number(value, label)


class Problem(Exception):
    def __init__(self, msg, status=400, extra=None):
        self.msg, self.status, self.extra = msg, status, extra or {}
        super().__init__(msg)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value, field, limit=160):
    if not isinstance(value, str) or not value.strip():
        raise Problem(f'{field} is required.')
    value = value.strip()
    if len(value) > limit:
        raise Problem(f'{field} must be {limit} characters or fewer.')
    return value


def number(value, label, allow_zero=False):
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise Problem(f'{label} must be a number.')
    if not math.isfinite(n) or (n < 0 if allow_zero else n <= 0):
        raise Problem(f'{label} must be {"zero or greater" if allow_zero else "greater than zero"}.')
    return round(n, 4)


def positive_int(value, label):
    n = number(value, label)
    if not n.is_integer():
        raise Problem(f'{label} must be a whole number.')
    return int(n)


def nonneg_int(value, label):
    n = number(value, label, True)
    if not n.is_integer():
        raise Problem(f'{label} must be a whole number.')
    return int(n)


def normal_url(value):
    value = str(value or '').strip()
    if value and (urlparse(value).scheme not in ('http','https') or not urlparse(value).netloc):
        raise Problem('Reorder URL must start with https:// or http://')
    if len(value) > 1000:
        raise Problem('Reorder URL is too long.')
    return value

@contextmanager
def transaction():
    with LOCK:
        conn = sqlite3.connect(str(DB), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=10000')
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()


def fetchrow(conn, table, item_id):
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (item_id,)).fetchone()
    if row is None:
        raise Problem(f'{table[:-1].capitalize()} not found.', 404)
    return row


def audit(conn, category, description):
    conn.execute('INSERT INTO activity (category, description, occurred_at) VALUES (?,?,?)',
                 (category, description, now()))


def seed_components(conn):
    """Known reusable components only; no invented stock, pricing, or product SKUs."""
    items = [
        ('EMPTY-6POT', 'Empty connected 6-pot strip with lids', 'strip', 1, 'strip'),
        ('BRUSH-SET', 'Brush set (fine + medium + broad)', 'set', 1, 'set'),
        ('BRUSH-FINE', 'Fine brush', 'pcs', 1, 'brush'),
        ('BRUSH-MED', 'Medium brush', 'pcs', 1, 'brush'),
        ('BRUSH-BROAD', 'Broad brush', 'pcs', 1, 'brush'),
        ('WOOD-CM', 'Wooden rod stock', 'cm', 1, 'cm'),
        ('CORD-CM', 'Hanging cord', 'cm', 1, 'cm'),
        ('HOOK', 'Hanging hook', 'pcs', 1, 'hook'),
        ('ZIPLOCK', 'Hanging accessory ziplock bag', 'pcs', 1, 'bag'),
        ('ADHESIVE-20G', 'Liquid adhesive 20 g tube', 'tube', 1, 'tube'),
        ('TUBE-A5-A4', 'Cardboard packaging tube (A5/A4)', 'pcs', 1, 'tube'),
        ('TUBE-A3', 'Cardboard packaging tube (A3)', 'pcs', 1, 'tube'),
        ('HANG-INSTRUCT', 'Canvas hanging instruction (14 x 6.9 cm)', 'pcs', 6, 'A4 printed sheet'),
    ]
    for sku, name, unit, multi, buy_unit in items:
        conn.execute('INSERT INTO components (code,name,unit,qty,min_qty,purchase_multiplier,purchase_unit) VALUES (?,?,?,0,0,?,?)',
                     (sku,name,unit,multi,buy_unit))


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS kits (
          id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE COLLATE NOCASE,
          name TEXT NOT NULL, size TEXT NOT NULL CHECK(size IN ('A5','A4','A3')),
          packed_qty INTEGER NOT NULL DEFAULT 0 CHECK(packed_qty>=0),
          recipe_ready INTEGER NOT NULL DEFAULT 0 CHECK(recipe_ready IN (0,1)),
          subtitle TEXT NOT NULL DEFAULT '',
          barcode TEXT NOT NULL DEFAULT '',
          mrp REAL, selling_price REAL,
          stock_counted INTEGER NOT NULL DEFAULT 0 CHECK(stock_counted IN (0,1)),
          paint_colours INTEGER CHECK(paint_colours IS NULL OR (paint_colours>0 AND paint_colours<=240)),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS components (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE,
          name TEXT NOT NULL, unit TEXT NOT NULL,
          qty REAL NOT NULL DEFAULT 0 CHECK(qty>=0),
          min_qty REAL NOT NULL DEFAULT 0 CHECK(min_qty>=0),
          purchase_multiplier REAL NOT NULL DEFAULT 1 CHECK(purchase_multiplier>0),
          purchase_unit TEXT NOT NULL DEFAULT 'unit'
        );
        CREATE TABLE IF NOT EXISTS recipes (
          kit_id INTEGER NOT NULL REFERENCES kits(id) ON DELETE CASCADE,
          component_id INTEGER NOT NULL REFERENCES components(id),
          qty_per_kit REAL NOT NULL CHECK(qty_per_kit>0),
          PRIMARY KEY(kit_id,component_id)
        );
        CREATE TABLE IF NOT EXISTS suppliers (
          id INTEGER PRIMARY KEY,
          component_id INTEGER NOT NULL REFERENCES components(id),
          name TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
          price REAL NOT NULL CHECK(price>=0),
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS supplier_price_history (
          id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
          price REAL NOT NULL, changed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_migrations (
          migration TEXT PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity (
          id INTEGER PRIMARY KEY, category TEXT NOT NULL,
          description TEXT NOT NULL, occurred_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recipes_component ON recipes(component_id);
        CREATE INDEX IF NOT EXISTS idx_activity_time ON activity(occurred_at);
    ''')
    # Non-destructive migration: preserve all existing inventory, recipes and audit data.
    columns = {row[1] for row in conn.execute('PRAGMA table_info(kits)')}
    for column, spec in (
        ('subtitle', "TEXT NOT NULL DEFAULT ''"),
        ('barcode', "TEXT NOT NULL DEFAULT ''"),
        ('mrp', 'REAL'),
        ('selling_price', 'REAL'),
        ('stock_counted', 'INTEGER NOT NULL DEFAULT 0'),
        ('paint_colours', 'INTEGER'),
    ):
        if column not in columns:
            conn.execute(f'ALTER TABLE kits ADD COLUMN {column} {spec}')
    if 'stock_counted' not in columns:
        conn.execute('''UPDATE kits SET stock_counted=1
            WHERE packed_qty>0 OR EXISTS (
              SELECT 1 FROM activity WHERE activity.category='Packed stock count'
              AND activity.description LIKE kits.sku || ':%'
            )''')
    if conn.execute('SELECT COUNT(*) FROM components').fetchone()[0] == 0:
        seed_components(conn)
    if not conn.execute('SELECT 1 FROM app_migrations WHERE migration=?', (CATALOGUE_VERSION,)).fetchone():
        for sku, name, subtitle, size, barcode, mrp, price in PRODUCT_CATALOGUE:
            check_barcode(barcode)
            conn.execute('''INSERT INTO kits
                (sku, name, subtitle, size, barcode, mrp, selling_price, packed_qty, stock_counted, created_at)
                VALUES (?,?,?,?,?,?,?,0,0,?)
                ON CONFLICT(sku) DO UPDATE SET
                    name=excluded.name, subtitle=excluded.subtitle,
                    size=excluded.size, barcode=excluded.barcode,
                    mrp=excluded.mrp, selling_price=excluded.selling_price''',
                (sku,name,subtitle,size,barcode,mrp,price,now()))
        conn.execute('INSERT INTO app_migrations (migration,applied_at) VALUES (?,?)',
                     (CATALOGUE_VERSION,now()))
        audit(conn, 'Catalogue imported', '9 A3/A4 products imported; packed stock not counted and existing quantities preserved')
    conn.commit()
    conn.close()


def state():
    with LOCK:
        conn = sqlite3.connect(str(DB))
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        kits = [dict(r) for r in conn.execute('SELECT * FROM kits ORDER BY name, size')]
        components = [dict(r) for r in conn.execute('SELECT * FROM components ORDER BY name')]
        recipes = [dict(r) for r in conn.execute('SELECT * FROM recipes ORDER BY kit_id,component_id')]
        suppliers = [dict(r) for r in conn.execute('SELECT * FROM suppliers ORDER BY component_id, price, name')]
        history = [dict(r) for r in conn.execute('SELECT * FROM supplier_price_history ORDER BY id DESC LIMIT 200')]
        activity = [dict(r) for r in conn.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 80')]
        conn.close()
    compmap = {c['id']: c for c in components}
    for k in kits:
        b = [r for r in recipes if r['kit_id'] == k['id']]
        k['recipe'] = b
        k['paint_strips_per_kit'] = ((k['paint_colours']+5)//6) if k['paint_colours'] is not None else None
        k['can_make'] = min((int((compmap[r['component_id']]['qty'] + 1e-8)//r['qty_per_kit']) for r in b), default=None) if k['recipe_ready'] and b else None
        k['low_stock'] = bool(k['stock_counted']) and k['packed_qty'] <= THRESHOLD
    return {'kits':kits,'components':components,'suppliers':suppliers, 'price_history':history,
            'activity':activity,'threshold':THRESHOLD,'timestamp':now()}


def create_kit(body):
    sku = clean(body.get('sku'), 'Kit SKU', 80)
    name = clean(body.get('name'), 'Kit name')
    size = clean(body.get('size'), 'Size')
    if size not in ('A5','A4','A3'):
        raise Problem('Size must be A5, A4 or A3.')
    initial_raw = body.get('packed_qty')
    counted = initial_raw is not None and str(initial_raw).strip() != ''
    initial = nonneg_int(initial_raw, 'Opening packed stock') if counted else 0
    subtitle = str(body.get('subtitle') or '').strip()
    if len(subtitle) > 160:
        raise Problem('Subtitle must be 160 characters or fewer.')
    barcode = check_barcode(body.get('barcode'))
    mrp = optional_price(body.get('mrp'), 'MRP')
    price = optional_price(body.get('selling_price'), 'Selling price')
    with transaction() as conn:
        cur = conn.execute('''INSERT INTO kits
            (sku,name,size,packed_qty,stock_counted,subtitle,barcode,mrp,selling_price,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (sku,name,size,initial,int(counted),subtitle,barcode,mrp,price,now()))
        audit(conn,'Kit created',f'{sku} — {name} ({size}), opening packed stock '+(str(initial) if counted else 'not counted'))
        return {'id':cur.lastrowid}


def update_kit(kid, body):
    # Edit catalogue fields without changing inventory quantities.
    with transaction() as conn:
        kit = fetchrow(conn, 'kits', kid)
        name = clean(body.get('name', kit['name']), 'Product name')
        subtitle = str(body.get('subtitle', kit['subtitle']) or '').strip()
        if len(subtitle) > 160:
            raise Problem('Subtitle must be 160 characters or fewer.')
        barcode = check_barcode(body.get('barcode', kit['barcode']))
        mrp = optional_price(body.get('mrp', kit['mrp']), 'MRP')
        price = optional_price(body.get('selling_price', kit['selling_price']), 'Selling price')
        conn.execute('''UPDATE kits SET name=?, subtitle=?, barcode=?, mrp=?, selling_price=?
                        WHERE id=?''',(name,subtitle,barcode,mrp,price,kid))
        audit(conn, 'Catalogue updated', f'{kit["sku"]}: product details updated')
        return {'ok':True}


def create_component(body):
    code = clean(body.get('code'), 'Component code', 80)
    name = clean(body.get('name'), 'Component name')
    unit = clean(body.get('unit'), 'Unit', 20)
    if unit not in ('pcs','set','strip','tube','cm','ml','g'):
        raise Problem('Choose a valid unit.')
    opening = number(body.get('qty',0), 'Opening quantity', True)
    minqty = number(body.get('min_qty',0), 'Minimum quantity', True)
    multiple = number(body.get('purchase_multiplier',1), 'Receiving multiplier')
    purchase_unit = clean(body.get('purchase_unit','unit'), 'Receiving unit', 80)
    with transaction() as conn:
        cur = conn.execute('INSERT INTO components (code,name,unit,qty,min_qty,purchase_multiplier,purchase_unit) VALUES (?,?,?,?,?,?,?)',
                           (code,name,unit,opening,minqty,multiple,purchase_unit))
        audit(conn,'Component created',f'{code} — {name}, opening quantity {opening} {unit}')
        return {'id':cur.lastrowid}


def update_component(cid, body):
    with transaction() as conn:
        old = fetchrow(conn,'components',cid)
        name = clean(body.get('name',old['name']),'Component name')
        minqty = number(body.get('min_qty',old['min_qty']),'Minimum quantity',True)
        multi = number(body.get('purchase_multiplier',old['purchase_multiplier']),'Receiving multiplier')
        purchase_unit = clean(body.get('purchase_unit',old['purchase_unit']),'Receiving unit',80)
        conn.execute('UPDATE components SET name=?,min_qty=?,purchase_multiplier=?,purchase_unit=? WHERE id=?',
                     (name,minqty,multi,purchase_unit,cid))
        audit(conn,'Component updated',f'{old["code"]}: settings updated')
        return {'ok':True}


def receive_component(cid, body):
    amount = number(body.get('amount'), 'Amount received')
    with transaction() as conn:
        row = fetchrow(conn,'components',cid)
        qty = round(amount*row['purchase_multiplier'],4)
        conn.execute('UPDATE components SET qty=ROUND(qty+?,4) WHERE id=?',(qty,cid))
        audit(conn,'Component received',f'{row["code"]}: +{qty:g} {row["unit"]} from {amount:g} {row["purchase_unit"]}(s)')
        return {'received':qty,'unit':row['unit']}


def count_component(cid, body):
    counted = number(body.get('qty'), 'Counted quantity', True)
    with transaction() as conn:
        row = fetchrow(conn,'components',cid)
        conn.execute('UPDATE components SET qty=? WHERE id=?',(counted,cid))
        audit(conn,'Component stock count',f'{row["code"]}: {row["qty"]:g} → {counted:g} {row["unit"]}')
        return {'ok':True}


def convert_component(body):
    src = int(body.get('source_component_id') or 0)
    amount = number(body.get('amount'),'Source quantity')
    outputs = body.get('outputs')
    if not isinstance(outputs,list) or not outputs:
        raise Problem('Select at least one output component.')
    combined = {}
    for o in outputs:
        cid = int(o.get('component_id') or 0)
        qty = number(o.get('amount'),'Output quantity')
        combined[cid] = round(combined.get(cid,0)+qty,4)
    if src in combined:
        raise Problem('Source component cannot also be an output.')
    with transaction() as conn:
        source = fetchrow(conn,'components',src)
        if source['qty']+1e-8<amount:
            raise Problem(f'Not enough {source["name"]}. Available {source["qty"]:g} {source["unit"]}.',409)
        for cid in combined:
            fetchrow(conn,'components',cid)
        conn.execute('UPDATE components SET qty=ROUND(qty-?,4) WHERE id=?',(amount,src))
        descriptions=[]
        for cid,qty in combined.items():
            conn.execute('UPDATE components SET qty=ROUND(qty+?,4) WHERE id=?',(qty,cid))
            row=fetchrow(conn,'components',cid)
            descriptions.append(f'+{qty:g} {row["code"]}')
        audit(conn,'Stock conversion',f'-{amount:g} {source["code"]} → '+', '.join(descriptions))
        return {'ok':True}


def save_recipe(kid, body):
    items = body.get('items')
    if not isinstance(items,list) or not items:
        raise Problem('Add at least one component to the recipe.')
    parsed = {}
    for item in items:
        try:
            cid = int(item['component_id'])
        except (ValueError,TypeError,KeyError):
            raise Problem('Each recipe row needs a valid component.')
        if cid in parsed:
            raise Problem('A component appears twice in this recipe. Combine its quantities.')
        parsed[cid] = number(item.get('qty_per_kit'), 'Quantity per kit')
    with transaction() as conn:
        kit = fetchrow(conn,'kits',kid)
        for cid in parsed:
            fetchrow(conn,'components',cid)
        conn.execute('DELETE FROM recipes WHERE kit_id=?',(kid,))
        for cid,qty in parsed.items():
            conn.execute('INSERT INTO recipes (kit_id,component_id,qty_per_kit) VALUES (?,?,?)',(kid,cid,qty))
        conn.execute('UPDATE kits SET recipe_ready=0 WHERE id=?',(kid,))
        audit(conn,'Recipe updated',f'{kit["sku"]}: {len(parsed)} component(s); needs confirmation')
        return {'ok':True}


def confirm_recipe(kid):
    with transaction() as conn:
        kit = fetchrow(conn,'kits',kid)
        count=conn.execute('SELECT COUNT(*) FROM recipes WHERE kit_id=?',(kid,)).fetchone()[0]
        if not count:
            raise Problem('Add components to the recipe before confirming it.')
        conn.execute('UPDATE kits SET recipe_ready=1 WHERE id=?',(kid,))
        audit(conn,'Recipe confirmed',f'{kit["sku"]}: recipe confirmed complete')
        return {'ok':True}


def pack_kits(kid, body):
    requested = positive_int(body.get('quantity'), 'Packed quantity')
    with transaction() as conn:
        kit = fetchrow(conn,'kits',kid)
        if not kit['recipe_ready']:
            raise Problem('Confirm the kit recipe before packing. This prevents missing component deductions.',409)
        items=conn.execute('SELECT r.*,c.qty AS available,c.name,c.unit,c.code FROM recipes r JOIN components c ON r.component_id=c.id WHERE r.kit_id=?',(kid,)).fetchall()
        if not items:
            raise Problem('This kit has no components in its recipe.',409)
        can_make = max(0,min(int((r['available']+1e-8)//r['qty_per_kit']) for r in items))
        qty = min(requested,can_make)
        if qty<=0:
            missing=[f'{r["name"]} (need {r["qty_per_kit"]:g} {r["unit"]}, have {r["available"]:g})' for r in items if r['available']+1e-8<r['qty_per_kit']]
            raise Problem('Insufficient components to pack even one kit: '+ '; '.join(missing),409)
        for r in items:
            conn.execute('UPDATE components SET qty=ROUND(qty-?,4) WHERE id=?',(round(qty*r['qty_per_kit'],4),r['component_id']))
        conn.execute('UPDATE kits SET packed_qty=packed_qty+?, stock_counted=1 WHERE id=?',(qty,kid))
        audit(conn,'Packed batch',f'{kit["sku"]}: packed +{qty} kit(s); {len(items)} component types deducted'+(f' (requested {requested})' if requested!=qty else ''))
        return {'packed':qty,'requested':requested,'limited':qty<requested}


def handover(kid,body):
    qty = positive_int(body.get('quantity'),'Handover quantity')
    with transaction() as conn:
        kit=fetchrow(conn,'kits',kid)
        if qty>kit['packed_qty']:
            raise Problem(f'Only {kit["packed_qty"]} packed kits available. Pack another batch first.',409)
        conn.execute('UPDATE kits SET packed_qty=packed_qty-? WHERE id=?',(qty,kid))
        audit(conn,'Packed stock handed over',f'{kit["sku"]}: -{qty} packed kits, balance {kit["packed_qty"]-qty}')
        return {'balance':kit['packed_qty']-qty}


def count_kit(kid,body):
    qty=nonneg_int(body.get('quantity'),'Physical packed stock count')
    with transaction() as conn:
        kit=fetchrow(conn,'kits',kid)
        conn.execute('UPDATE kits SET packed_qty=?, stock_counted=1 WHERE id=?',(qty,kid))
        audit(conn,'Packed stock count',f'{kit["sku"]}: corrected from {kit["packed_qty"]} to {qty}')
        return {'ok':True}


def create_supplier(body):
    cid=int(body.get('component_id') or 0)
    name=clean(body.get('name'),'Supplier name')
    url=normal_url(body.get('url'))
    price=number(body.get('price'),'Unit purchase price in ₹',True)
    with transaction() as conn:
        component=fetchrow(conn,'components',cid)
        cur=conn.execute('INSERT INTO suppliers (component_id,name,url,price,updated_at) VALUES (?,?,?,?,?)',
                         (cid,name,url,price,now()))
        conn.execute('INSERT INTO supplier_price_history (supplier_id,price,changed_at) VALUES (?,?,?)',
                     (cur.lastrowid,price,now()))
        audit(conn,'Supplier added',f'{name}: {component["code"]} @ ₹{price:g}/{component["unit"]}')
        return {'id':cur.lastrowid}


def update_supplier(sid,body):
    with transaction() as conn:
        row=fetchrow(conn,'suppliers',sid)
        name=clean(body.get('name',row['name']),'Supplier name')
        url=normal_url(body.get('url',row['url']))
        price=number(body.get('price',row['price']),'Unit purchase price in ₹',True)
        conn.execute('UPDATE suppliers SET name=?,url=?,price=?,updated_at=? WHERE id=?',
                     (name,url,price,now(),sid))
        if price != row['price']:
            conn.execute('INSERT INTO supplier_price_history (supplier_id,price,changed_at) VALUES (?,?,?)',(sid,price,now()))
            audit(conn,'Supplier price changed',f'{name}: ₹{row["price"]:g} → ₹{price:g}')
        else:
            audit(conn,'Supplier updated',f'{name}: supplier details updated')
        return {'ok':True}


def route(method,path,body):
    if method=='GET' and path=='/api/state':
        return state()
    if method!='POST':
        raise Problem('Route not found.',404)
    if path=='/api/kits': return create_kit(body)
    m=re.fullmatch(r'/api/kits/(\d+)/update',path)
    if m: return update_kit(int(m.group(1)),body)
    if path=='/api/components': return create_component(body)
    if path=='/api/convert': return convert_component(body)
    if path=='/api/suppliers': return create_supplier(body)
    m=re.fullmatch(r'/api/kits/(\d+)/(recipe|confirm-recipe|pack|handover|count)',path)
    if m:
        kid=int(m.group(1)); action=m.group(2)
        return {'recipe':save_recipe,'confirm-recipe':lambda id,b:confirm_recipe(id),
                'pack':pack_kits,'handover':handover,'count':count_kit}[action](kid,body)
    m=re.fullmatch(r'/api/components/(\d+)/(update|receive|count)',path)
    if m:
        cid=int(m.group(1)); action=m.group(2)
        return {'update':update_component,'receive':receive_component,'count':count_component}[action](cid,body)
    m=re.fullmatch(r'/api/suppliers/(\d+)/update',path)
    if m: return update_supplier(int(m.group(1)),body)
    raise Problem('Route not found.',404)

class AppHandler(BaseHTTPRequestHandler):
    server_version='AurvaWarehouse/1.0'
    def authorized(self):
        if not SECURE_MODE:
            return True
        header = self.headers.get('Authorization', '')
        if not header.startswith('Basic '):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode('utf-8')
            username, password = decoded.split(':', 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return (hmac.compare_digest(username, AUTH_USER) and
                hmac.compare_digest(password, AUTH_PASSWORD))

    def check_authorized(self):
        if self.authorized():
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Aurva PBN Warehouse", charset="UTF-8"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def send_bytes(self,status,content,kind):
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if RAILWAY:
            self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
        self.send_header('Content-Length',str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if not self.check_authorized():
            return
        path=urlparse(self.path).path
        if path.startswith('/api/'):
            return self.handle_api('GET',path,{})
        if path in ('/','/index.html'):
            file=ROOT/'static'/'index.html'; kind='text/html; charset=utf-8'
        elif path=='/app.js':
            file=ROOT/'static'/'app.js'; kind='text/javascript; charset=utf-8'
        elif path=='/style.css':
            file=ROOT/'static'/'style.css'; kind='text/css; charset=utf-8'
        else:
            return self.send_bytes(404,b'Not found','text/plain')
        self.send_bytes(200,file.read_bytes(),kind)

    def do_POST(self):
        if not self.check_authorized():
            return
        path=urlparse(self.path).path
        if not path.startswith('/api/'):
            return self.send_bytes(404,b'Not found','text/plain')
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
            return self.json_result(415,{'error':'Use Content-Type: application/json.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length>65536:
                raise Problem('Request too large.',413)
            body=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body,dict):
                raise Problem('Request should be a JSON object.')
        except (ValueError,UnicodeError,json.JSONDecodeError):
            return self.json_result(400,{'error':'Invalid JSON request.'})
        except Problem as e:
            return self.json_result(e.status,{'error':e.msg})
        self.handle_api('POST',path,body)

    def handle_api(self,method,path,body):
        try:
            result=route(method,path,body)
            return self.json_result(200,result)
        except Problem as e:
            return self.json_result(e.status,{'error':e.msg,**e.extra})
        except sqlite3.IntegrityError as e:
            if 'UNIQUE' in str(e):
                return self.json_result(409,{'error':'That code/SKU already exists. Use a unique code.'})
            return self.json_result(400,{'error':'Database constraint failed: '+str(e)})
        except Exception as e:
            print('Internal error:',repr(e))
            return self.json_result(500,{'error':'Unexpected server error. Check terminal.'})

    def json_result(self,status,payload):
        return self.send_bytes(status,json.dumps(payload).encode('utf-8'),'application/json; charset=utf-8')

    def log_message(self,fmt,*args):
        if not self.path.startswith('/api/state'):
            super().log_message(fmt,*args)

if __name__=='__main__':
    if SECURE_MODE:
        if not AUTH_USER.strip() or len(AUTH_PASSWORD) < 12:
            raise SystemExit('Security setup required: set AURVA_USER and a strong AURVA_PASSWORD of 12+ characters in Railway Variables.')
        if RAILWAY and not MOUNT and not os.environ.get('AURVA_DB_PATH'):
            raise SystemExit('Database setup required: attach a Railway volume at /data (or set AURVA_DB_PATH to a mounted volume path) before using the hosted warehouse.')
    init_db()
    print(f'\nAurva PBN Warehouse\nOpen http://{HOST}:{PORT}\nDatabase: {DB}\nStop: Ctrl+C\n')
    ThreadingHTTPServer((HOST,PORT),AppHandler).serve_forever()
