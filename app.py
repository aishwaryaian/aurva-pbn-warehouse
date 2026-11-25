"""Aurva World PBN Warehouse — local or private Railway-hosted web app.

For Railway: configure AURVA_USER, AURVA_PASSWORD and a persistent
volume mounted at /data (with AURVA_DB_PATH=/data/aurva.db).
For local use, run python app.py and open http://127.0.0.1:8765.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import secrets
import threading
import io
import csv
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from http.cookies import SimpleCookie

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
SESSION_COOKIE = 'aurva_session'
SESSION_DAYS = 14
# Signing key: an explicit AURVA_SECRET if set, otherwise derived from the login
# credentials so that changing the password logs everyone out.
SESSION_KEY = hashlib.sha256(('aurva-session|' + (os.environ.get('AURVA_SECRET') or
                              AUTH_USER + '|' + AUTH_PASSWORD)).encode()).digest()
LOGIN_FAILURES = {}
LOGIN_LIMIT = 8
LOGIN_WINDOW = 15 * 60
THRESHOLD = 10

# Verified active PBN catalogue, without A5 variants pending phase-out count.
# Barcode strings deliberately retain their leading zeros.
CATALOGUE_VERSION = 'aurva_pbn_active_catalogue_2026_11_06_v1'
# One-time stock-safe import of confirmed number of individual paint pots per kit.
# A5 phase-out variants are deliberately excluded.
PAINT_COUNTS_VERSION = 'aurva_pbn_paint_counts_and_cuttable_strips_2026_11_06_v1'
PAINT_COUNTS = {
    'PNFA3-91-0001': 15,
    'PNFA3-91-0002': 15,
    'PNFA3-91-0003': 18,
    'PN0A4-91-0002': 15,
    'PN0A4-91-0004': 12,
    'PN0A4-91-0007': 12,
    'PN0A4-91-0005': 10,
    'PN0A4-91-0006': 8,
    'PN0A4-91-0003': 15,
}

# Confirmed A3/A4 bill of materials, without any A5 or physical stock counts.
MASTER_RECIPES_VERSION = 'aurva_pbn_a3_a4_master_recipes_2026_11_06_v1'
ADHESIVE_PENDING_REASON = 'Adhesive consumption has not yet been measured'
PAINT_FILL_ML = 3.0

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
    """Reject obsolete whole-strip recipe rows; cuts are planned at packing time."""
    strip = conn.execute('SELECT id FROM components WHERE code=?', ('EMPTY-6POT',)).fetchone()
    if strip and colours is not None:
        conn.execute('DELETE FROM recipes WHERE kit_id=? AND component_id=?', (kit_id, strip['id']))
    conn.execute('UPDATE kits SET recipe_ready=0 WHERE id=?', (kit_id,))


def paint_stock(components):
    """Return one full-strip component and cut leftovers keyed by section length."""
    by_code = {c['code']: c for c in components}
    full = by_code.get('EMPTY-6POT')
    cuts = {n: by_code.get(f'EMPTY-CUT-{n}') for n in range(1, 6)}
    if full is None or any(c is None for c in cuts.values()):
        raise Problem('Paint strip or cut-section stock components are missing.')
    full_qty = int(float(full['qty']) // 1)
    cut_qty = {n: int(float(cuts[n]['qty']) // 1) for n in range(1, 6)}
    return full, cuts, full_qty, cut_qty


def plan_paint_strips(full_qty, cut_qty, pots_per_kit, requested):
    """Pack complete connected sections, reusing the smallest suitable cut piece.

    One kit always receives floor(pots/6) intact six-pot strips and (if needed)
    exactly one connected partial section. Existing sufficiently long sections
    are cut first. A freshly cut full strip creates one reusable remainder.
    Does not mutate the database.
    """
    full = int(full_qty)
    cuts = {n: int(cut_qty.get(n, 0)) for n in range(1, 6)}
    whole, partial = divmod(int(pots_per_kit), 6)
    total_positions = 6 * full + sum(n * q for n, q in cuts.items())
    limit = min(int(requested), total_positions // int(pots_per_kit))
    packed = 0
    for _ in range(limit):
        if full < whole:
            break
        fit = None
        if partial:
            fit = next((size for size in range(partial, 6) if cuts[size] > 0), None)
            if fit is None and full < whole + 1:
                break
        full -= whole
        if partial:
            if fit is not None:
                cuts[fit] -= 1
                if fit > partial:
                    cuts[fit - partial] += 1
            else:
                full -= 1
                cuts[6 - partial] += 1
        packed += 1
    return {'packed': packed, 'full_used': int(full_qty) - full, 'cuts': cuts}


def apply_paint_plan(conn, full_component, cut_components, before_cuts, plan):
    """Atomically adjust only full strips and usable leftover connected sections."""
    if plan['full_used']:
        conn.execute('UPDATE components SET qty=qty-? WHERE id=?',
                     (plan['full_used'], full_component['id']))
    for length, row in cut_components.items():
        change = plan['cuts'][length] - before_cuts[length]
        if change:
            conn.execute('UPDATE components SET qty=qty+? WHERE id=?', (change, row['id']))


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
          recipe_pending_reason TEXT NOT NULL DEFAULT '',
          paint_fill_ml REAL,
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
        ('recipe_pending_reason', "TEXT NOT NULL DEFAULT ''"),
        ('paint_fill_ml', 'REAL'),
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
    # Connected leftover sections are distinct from purchased whole strips.
    # Initial quantities are zero; no stock is invented or silently converted.
    for length in range(1, 6):
        conn.execute('''INSERT INTO components
            (code,name,unit,qty,min_qty,purchase_multiplier,purchase_unit)
            VALUES (?,?,?,0,0,1,'section')
            ON CONFLICT(code) DO NOTHING''',
            (f'EMPTY-CUT-{length}', f'Unfilled reusable {length}-pot connected cut section', 'pcs'))
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
    if not conn.execute('SELECT 1 FROM app_migrations WHERE migration=?', (PAINT_COUNTS_VERSION,)).fetchone():
        full_row = conn.execute("SELECT id FROM components WHERE code='EMPTY-6POT'").fetchone()
        for sku, colours in PAINT_COUNTS.items():
            kit = conn.execute('SELECT id FROM kits WHERE sku=?', (sku,)).fetchone()
            if kit is None:
                raise RuntimeError(f'Expected active PBN SKU missing: {sku}')
            conn.execute('UPDATE kits SET paint_colours=?, recipe_ready=0 WHERE id=?', (colours,kit[0]))
            if full_row:
                # Remove old ceil(colours/6) recipe rows to prevent duplicate deductions.
                conn.execute('DELETE FROM recipes WHERE kit_id=? AND component_id=?', (kit[0],full_row[0]))
        conn.execute('INSERT INTO app_migrations (migration,applied_at) VALUES (?,?)',
                     (PAINT_COUNTS_VERSION,now()))
        audit(conn,'Paint counts imported',
              'Nine confirmed paint-pot counts stored; cuttable-strip planning enabled; existing physical stock unchanged')
    if not conn.execute('SELECT 1 FROM app_migrations WHERE migration=?',
                        (MASTER_RECIPES_VERSION,)).fetchone():
        # New item records are empty until the real stocktake. Do not touch any
        # existing quantities, component prices, suppliers, or A5 recipes.
        common = [
            ('BRUSH-2SET', 'Ready-made set of 2 paint brushes', 'set'),
            ('TUBE-A4', 'A4 packaging tube including end caps', 'pcs'),
        ]
        for sku, name, subtitle, size, barcode, mrp, price in PRODUCT_CATALOGUE:
            common.extend([
                (f'CANVAS-{sku}', f'{name} — printed {size} canvas', 'pcs'),
                (f'BROCHURE-{sku}', f'{name} — culture brochure', 'pcs'),
                (f'LABEL-{sku}', f'{name} — packaging tube label', 'pcs'),
            ])
        for code, name, unit in common:
            conn.execute('''INSERT INTO components
                (code,name,unit,qty,min_qty,purchase_multiplier,purchase_unit)
                VALUES (?,?,?,0,0,1,?) ON CONFLICT(code) DO NOTHING''',
                (code,name,unit,unit))
        components_by_code = {row[0]: row[1] for row in conn.execute(
            'SELECT code,id FROM components')}
        for sku, name, subtitle, size, barcode, mrp, price in PRODUCT_CATALOGUE:
            kit = conn.execute('SELECT id FROM kits WHERE sku=?',(sku,)).fetchone()
            if kit is None:
                raise RuntimeError(f'Cannot apply recipe, expected SKU missing: {sku}')
            requirements = {
                f'CANVAS-{sku}':1,
                'WOOD-CM':60 if size=='A3' else 42,
                'BRUSH-2SET':1,
                f'BROCHURE-{sku}':1,
                'ZIPLOCK':1,
                'HOOK':2,
                'HANG-INSTRUCT':1,
                'CORD-CM':50,
                'TUBE-A3' if size=='A3' else 'TUBE-A4':1,
                f'LABEL-{sku}':1,
            }
            # Only the nine named active-kit recipes are intentionally replaced.
            # Existing arbitrary/A5 recipes and ALL physical stock remain intact.
            conn.execute('DELETE FROM recipes WHERE kit_id=?',(kit[0],))
            for code, quantity in requirements.items():
                cid = components_by_code.get(code)
                if cid is None:
                    raise RuntimeError(f'Missing recipe component: {code}')
                conn.execute('INSERT INTO recipes (kit_id,component_id,qty_per_kit) VALUES (?,?,?)',
                             (kit[0],cid,quantity))
            conn.execute('''UPDATE kits SET recipe_ready=0, recipe_pending_reason=?,
                            paint_fill_ml=? WHERE id=?''',
                         (ADHESIVE_PENDING_REASON, PAINT_FILL_ML,kit[0]))
        conn.execute('INSERT INTO app_migrations (migration,applied_at) VALUES (?,?)',
                     (MASTER_RECIPES_VERSION, now()))
        audit(conn,'Master recipes imported',
              'A3/A4 BOMs saved for 9 active kits; adhesive unmeasured, recipes blocked; all stock unchanged')
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
        k['total_paint_fill_ml'] = (k['paint_colours'] * k['paint_fill_ml']
                                     if k['paint_colours'] is not None and k['paint_fill_ml'] is not None
                                     else None)
        k['paint_strips_per_kit'] = ((k['paint_colours']+5)//6) if k['paint_colours'] is not None else None
        k['full_strips_per_kit'] = k['paint_colours']//6 if k['paint_colours'] is not None else None
        k['cut_pots_per_kit'] = k['paint_colours']%6 if k['paint_colours'] is not None else None
        k['can_make'] = min((int((compmap[r['component_id']]['qty'] + 1e-8)//r['qty_per_kit']) for r in b), default=None) if k['recipe_ready'] and b else None
        if k['can_make'] is not None and k['paint_colours'] is not None:
            _, _, full_available, leftover_sections = paint_stock(components)
            k['can_make'] = plan_paint_strips(full_available, leftover_sections,
                                              k['paint_colours'], k['can_make'])['packed']
        k['low_stock'] = k['packed_qty'] <= THRESHOLD
        k['missing'] = [compmap[r['component_id']]['name'] for r in b
                        if compmap[r['component_id']]['qty'] + 1e-8 < r['qty_per_kit']]
    # Component alert level: your own minimum if set, otherwise "enough for
    # THRESHOLD more kits" of the design that uses the most of it.
    per_kit = {}
    for r in recipes:
        per_kit[r['component_id']] = max(per_kit.get(r['component_id'], 0), r['qty_per_kit'])
    strips = max(((k['paint_colours'] + 5) // 6 for k in kits if k['paint_colours']), default=0)
    for c in components:
        if c['min_qty'] > 0:
            level, rule = c['min_qty'], 'custom'
        elif c['code'].upper().startswith('EMPTY-CUT-'):
            level, rule = 0, 'none'
        elif c['code'].upper() == 'EMPTY-6POT' and strips:
            level, rule = THRESHOLD * strips, 'kits'
        elif c['id'] in per_kit:
            level, rule = round(THRESHOLD * per_kit[c['id']], 4), 'kits'
        else:
            # Not used by any design and no minimum set: nothing to warn about.
            level, rule = 0, 'none'
        c['alert_level'], c['alert_rule'] = level, rule
        c['low_stock'] = rule != 'none' and c['qty'] <= level
        c['used_in'] = sum(1 for r in recipes if r['component_id'] == c['id'])
    return {'kits':kits,'components':components,'suppliers':suppliers, 'price_history':history,
            'activity':activity,'threshold':THRESHOLD,'timestamp':now(),
            'database':{'engine':'SQLite','path':str(DB),
                        'hosted':RAILWAY,'persistent':bool(MOUNT or os.environ.get('AURVA_DB_PATH'))}}


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
    colours = paint_colour_count(body.get('paint_colours'))
    with transaction() as conn:
        cur = conn.execute('''INSERT INTO kits
            (sku,name,size,packed_qty,stock_counted,subtitle,barcode,mrp,selling_price,paint_colours,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (sku,name,size,initial,int(counted),subtitle,barcode,mrp,price,colours,now()))
        if colours is not None:
            sync_paint_strip_recipe(conn,cur.lastrowid,colours)
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
        colours = paint_colour_count(body.get('paint_colours', kit['paint_colours']))
        conn.execute('''UPDATE kits SET name=?, subtitle=?, barcode=?, mrp=?, selling_price=?, paint_colours=?
                        WHERE id=?''',(name,subtitle,barcode,mrp,price,colours,kid))
        if colours != kit['paint_colours']:
            sync_paint_strip_recipe(conn,kid,colours)
        audit(conn, 'Catalogue updated', f'{kit["sku"]}: product details updated'+
              (f'; {colours} paint pots, {(colours+5)//6} strips per kit' if colours is not None and colours != kit['paint_colours'] else ''))
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
        if kit['paint_colours'] is not None:
            paint_components = {c['id'] for c in conn.execute(
                "SELECT id FROM components WHERE code='EMPTY-6POT' OR code LIKE 'EMPTY-CUT-%'")}
            if paint_components.intersection(parsed):
                raise Problem('Paint strips and cut sections are tracked automatically from the colour count. Remove them from the manual recipe.')
        if not parsed:
            raise Problem('Add artwork-specific and other non-paint components to the recipe.')
        conn.execute('DELETE FROM recipes WHERE kit_id=?',(kid,))
        for cid,qty in parsed.items():
            conn.execute('INSERT INTO recipes (kit_id,component_id,qty_per_kit) VALUES (?,?,?)',(kid,cid,qty))
        conn.execute('UPDATE kits SET recipe_ready=0 WHERE id=?',(kid,))
        audit(conn,'Recipe updated',f'{kit["sku"]}: {len(parsed)} component(s); needs confirmation')
        return {'ok':True}


def confirm_recipe(kid, body=None):
    skip = bool((body or {}).get('skip_pending'))
    with transaction() as conn:
        kit = fetchrow(conn,'kits',kid)
        count=conn.execute('SELECT COUNT(*) FROM recipes WHERE kit_id=?',(kid,)).fetchone()[0]
        if not count:
            raise Problem('Add components to the recipe before confirming it.')
        if kit['recipe_pending_reason'] and skip:
            audit(conn,'Packing unlocked',f'{kit["sku"]}: confirmed without deducting adhesive ('
                  +kit['recipe_pending_reason']+')')
            conn.execute("UPDATE kits SET recipe_pending_reason='' WHERE id=?",(kid,))
        elif kit['recipe_pending_reason']:
            raise Problem('Cannot confirm this recipe yet: '+kit['recipe_pending_reason']+
                          '. No adhesive quantity will be guessed.',409)
        if kit['paint_colours'] is not None:
            stale = conn.execute("""SELECT 1 FROM recipes r JOIN components c ON c.id=r.component_id
                WHERE r.kit_id=? AND (c.code='EMPTY-6POT' OR c.code LIKE 'EMPTY-CUT-%')
                LIMIT 1""", (kid,)).fetchone()
            if stale:
                raise Problem('Paint strip sections are automatic. Remove the old manual strip recipe row.', 409)
        conn.execute('UPDATE kits SET recipe_ready=1 WHERE id=?',(kid,))
        audit(conn,'Recipe confirmed',f'{kit["sku"]}: recipe confirmed complete')
        return {'ok':True}


def pack_kits(kid, body):
    requested = positive_int(body.get('quantity'), 'Packed quantity')
    with transaction() as conn:
        return pack_in(conn, kid, requested)


def pack_batch(body):
    """Record packing for several designs at once. Each design packs as many
    as its components allow; problems for one design don't block the others."""
    items = body.get('items')
    if not isinstance(items, list) or not items:
        raise Problem('Enter how many kits you packed for at least one design.')
    wanted = []
    for item in items:
        try:
            kid = int(item.get('kit_id'))
        except (TypeError, ValueError, AttributeError):
            raise Problem('Each row needs a valid design.')
        qty = item.get('quantity')
        if qty in (None, '', 0, '0'):
            continue
        wanted.append((kid, positive_int(qty, 'Packed quantity')))
    if not wanted:
        raise Problem('Enter how many kits you packed for at least one design.')
    results = []
    for kid, qty in wanted:
        try:
            with transaction() as conn:
                r = pack_in(conn, kid, qty)
                r['kit_id'] = kid
                results.append(r)
        except Problem as e:
            results.append({'kit_id': kid, 'packed': 0, 'requested': qty, 'limited': True, 'error': e.msg})
    return {'results': results, 'packed': sum(r['packed'] for r in results)}


def pack_in(conn, kid, requested):
    if True:
        kit = fetchrow(conn,'kits',kid)
        if kit['recipe_pending_reason']:
            raise Problem('Packing is blocked: '+kit['recipe_pending_reason']+'.',409)
        if not kit['recipe_ready']:
            raise Problem('Confirm the kit recipe before packing. This prevents missing component deductions.',409)
        items=conn.execute('''SELECT r.*,c.qty AS available,c.name,c.unit,c.code
            FROM recipes r JOIN components c ON r.component_id=c.id
            WHERE r.kit_id=?''',(kid,)).fetchall()
        if not items:
            raise Problem('This kit has no components in its recipe.',409)
        if kit['paint_colours'] is not None and any(
            r['code']=='EMPTY-6POT' or r['code'].startswith('EMPTY-CUT-') for r in items
        ):
            raise Problem('Remove obsolete strip rows from this recipe before packing.',409)
        can_make = max(0,min(int((r['available']+1e-8)//r['qty_per_kit']) for r in items))
        qty = min(requested,can_make)
        strip_plan = None
        full_component = None
        cut_components = None
        cut_before = None
        if kit['paint_colours'] is not None:
            stock_rows = conn.execute("""SELECT id,code,qty FROM components
                WHERE code='EMPTY-6POT' OR code LIKE 'EMPTY-CUT-%'""").fetchall()
            full_component, cut_components, full_qty, cut_before = paint_stock(stock_rows)
            strip_plan = plan_paint_strips(full_qty,cut_before,kit['paint_colours'],qty)
            qty = strip_plan['packed']
        if qty <= 0:
            missing=[f'{r["name"]} (need {r["qty_per_kit"]:g} {r["unit"]}, have {r["available"]:g})'
                     for r in items if r['available']+1e-8<r['qty_per_kit']]
            if strip_plan is not None and strip_plan['packed']<=0 and can_make>0:
                missing.append('Not enough connected full strips and reusable cut sections')
            raise Problem('Insufficient components to pack even one kit: '+ '; '.join(missing),409)
        for r in items:
            conn.execute('UPDATE components SET qty=ROUND(qty-?,4) WHERE id=?',
                         (round(qty*r['qty_per_kit'],4),r['component_id']))
        if strip_plan is not None:
            apply_paint_plan(conn,full_component,cut_components,cut_before,strip_plan)
        conn.execute('UPDATE kits SET packed_qty=packed_qty+?, stock_counted=1 WHERE id=?',(qty,kid))
        strip_note = (f'; {strip_plan["full_used"]} full paint strips consumed, cut remnants adjusted'
                      if strip_plan is not None else '')
        audit(conn,'Packed batch',f'{kit["sku"]}: packed +{qty} kit(s); {len(items)} recipe components deducted'
              +strip_note+(f' (requested {requested})' if requested!=qty else ''))
        return {'packed':qty,'requested':requested,'limited':qty<requested,
                'full_paint_strips_used':strip_plan['full_used'] if strip_plan else None}


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


# ---------------------------------------------------------------------------
# Excel bulk upload / download (stdlib only: .xlsx is a zip of XML files)
# ---------------------------------------------------------------------------
IMPORT_MAX_BYTES = 5 * 1024 * 1024
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'

PART_COLUMNS = [
    ('code', 'Part code'), ('name', 'Part name'), ('unit', 'Unit'),
    ('qty', 'Stock quantity'), ('min_qty', 'Alert minimum'),
    ('purchase_multiplier', 'Units per purchase'), ('purchase_unit', 'Purchase unit'),
    ('supplier', 'Supplier'), ('price', 'Price per unit (Rs)'), ('url', 'Reorder link'),
]
DESIGN_COLUMNS = [
    ('sku', 'SKU'), ('name', 'Design name'), ('size', 'Size'),
    ('packed_qty', 'Packed stock'), ('paint_colours', 'Paint colours'),
    ('subtitle', 'Subtitle'), ('barcode', 'Barcode'), ('mrp', 'MRP'), ('selling_price', 'Selling price'),
]
HEADER_ALIASES = {
    'code': 'code', 'partcode': 'code', 'componentcode': 'code', 'itemcode': 'code',
    'partname': 'name', 'componentname': 'name', 'item': 'name', 'itemname': 'name',
    'designname': 'name', 'productname': 'name', 'name': 'name', 'artwork': 'name',
    'unit': 'unit', 'units': 'unit',
    'stockquantity': 'qty', 'quantity': 'qty', 'qty': 'qty', 'stock': 'qty', 'instock': 'qty',
    'count': 'qty', 'received': 'qty',
    'alertminimum': 'min_qty', 'minimum': 'min_qty', 'minqty': 'min_qty', 'alertlevel': 'min_qty', 'reorderlevel': 'min_qty',
    'unitsperpurchase': 'purchase_multiplier', 'purchaseunit': 'purchase_unit',
    'supplier': 'supplier', 'suppliername': 'supplier', 'vendor': 'supplier',
    'priceperunitrs': 'price', 'price': 'price', 'priceperunit': 'price', 'unitprice': 'price',
    'reorderlink': 'url', 'link': 'url', 'url': 'url', 'productlink': 'url',
    'sku': 'sku', 'size': 'size', 'packedstock': 'packed_qty', 'packed': 'packed_qty', 'packedkits': 'packed_qty',
    'paintcolours': 'paint_colours', 'paintcolors': 'paint_colours', 'colours': 'paint_colours',
    'subtitle': 'subtitle', 'barcode': 'barcode', 'mrp': 'mrp', 'sellingprice': 'selling_price',
}
UNITS = ('pcs', 'set', 'strip', 'tube', 'cm', 'ml', 'g')


def header_key(text):
    return HEADER_ALIASES.get(re.sub(r'[^a-z]', '', str(text or '').lower()))


def col_index(ref):
    letters = re.match(r'[A-Z]+', ref or '')
    n = 0
    for ch in (letters.group(0) if letters else 'A'):
        n = n * 26 + ord(ch) - 64
    return n - 1


def cell_text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_xlsx(data):
    """Return {sheet name: [rows]} with each row a list of cell values."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Problem('That file is not a valid Excel (.xlsx) file. Save it as "Excel Workbook" and try again.')
    if sum(i.file_size for i in zf.infolist()) > 60 * 1024 * 1024:
        raise Problem('The spreadsheet is too large.')

    def xml(name):
        raw = zf.read(name)
        if b'<!DOCTYPE' in raw[:4000] or b'<!ENTITY' in raw:
            raise Problem('The spreadsheet contains unsupported content.')
        return ET.fromstring(raw)

    names = set(zf.namelist())
    if 'xl/workbook.xml' not in names:
        raise Problem('That file is not a valid Excel (.xlsx) file.')
    shared = []
    if 'xl/sharedStrings.xml' in names:
        for si in xml('xl/sharedStrings.xml').findall('m:si', NS):
            shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % NS['m'])))
    rels = {}
    if 'xl/_rels/workbook.xml.rels' in names:
        for r in xml('xl/_rels/workbook.xml.rels'):
            target = r.get('Target', '')
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            rels[r.get('Id')] = target
    sheets = {}
    for sh in xml('xl/workbook.xml').iter('{%s}sheet' % NS['m']):
        path = rels.get(sh.get(REL_NS))
        if not path or path not in names:
            continue
        rows = []
        for row in xml(path).iter('{%s}row' % NS['m']):
            values = {}
            for c in row.findall('m:c', NS):
                kind = c.get('t')
                v = c.find('m:v', NS)
                if kind == 'inlineStr':
                    val = ''.join(t.text or '' for t in c.iter('{%s}t' % NS['m']))
                elif v is None or v.text is None:
                    continue
                elif kind == 's':
                    idx = int(v.text)
                    val = shared[idx] if idx < len(shared) else ''
                elif kind in ('str', 'e'):
                    val = v.text
                elif kind == 'b':
                    val = v.text == '1'
                else:
                    try:
                        val = float(v.text)
                    except ValueError:
                        val = v.text
                values[col_index(c.get('r'))] = val
            try:
                number_ = int(row.get('r') or 0)
            except ValueError:
                number_ = 0
            if number_ > len(rows) + 1:          # keep Excel row numbers: blank rows stay blank
                if number_ > 20000:
                    raise Problem('Please upload 5000 rows or fewer at a time.')
                rows.extend([] for _ in range(number_ - len(rows) - 1))
            if values:
                rows.append([values.get(i) for i in range(max(values) + 1)])
            elif number_:
                rows.append([])
            if len(rows) > 5000:
                raise Problem('Please upload 5000 rows or fewer at a time.')
        sheets[sh.get('name', 'Sheet')] = rows
    return sheets


def read_csv(data):
    text = data.decode('utf-8-sig', errors='replace')
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) > 5000:
        raise Problem('Please upload 5000 rows or fewer at a time.')
    return {'CSV': rows}


def fmt_num(n):
    return f'{n:g}'


def import_part(conn, rec, mode):
    code = clean(rec.get('code'), 'Part code', 80)
    row = conn.execute('SELECT * FROM components WHERE code=?', (code,)).fetchone()
    changes = []
    if row is None:
        name = clean(rec.get('name'), 'Part name (needed for a new part)')
        unit = (rec.get('unit') or 'pcs').strip().lower()
        if unit not in UNITS:
            raise Problem('Unit must be one of ' + ', '.join(UNITS) + '.')
        qty = number(rec.get('qty', 0), 'Stock quantity', True)
        minq = number(rec.get('min_qty', 0), 'Alert minimum', True)
        mult = number(rec.get('purchase_multiplier', 1), 'Units per purchase')
        punit = (rec.get('purchase_unit') or unit)[:80]
        cid = conn.execute('INSERT INTO components (code,name,unit,qty,min_qty,purchase_multiplier,purchase_unit) '
                           'VALUES (?,?,?,?,?,?,?)', (code, name, unit, qty, minq, mult, punit)).lastrowid
        changes.append(f'new part, stock {fmt_num(qty)} {unit}')
        audit(conn, 'Bulk upload', f'{code}: new part, opening stock {fmt_num(qty)} {unit}')
        action, shown_name = 'new', name
    else:
        cid, action, shown_name = row['id'], 'update', row['name']
        if 'unit' in rec and rec['unit'].strip().lower() != row['unit']:
            raise Problem(f"Unit can't be changed by upload (this part is counted in {row['unit']}).")
        if 'qty' in rec:
            amount = number(rec['qty'], 'Stock quantity', True)
            new = round(row['qty'] + amount, 4) if mode == 'add' else amount
            if new != row['qty']:
                changes.append(f'stock {fmt_num(row["qty"])} → {fmt_num(new)} {row["unit"]}')
                conn.execute('UPDATE components SET qty=? WHERE id=?', (new, cid))
                audit(conn, 'Bulk upload', f'{code}: stock {fmt_num(row["qty"])} → {fmt_num(new)} {row["unit"]}'
                      + (' (received)' if mode == 'add' else ' (count)'))
        if rec.get('name') and rec['name'] != row['name']:
            conn.execute('UPDATE components SET name=? WHERE id=?', (clean(rec['name'], 'Part name'), cid))
            changes.append('renamed')
        for key, label in (('min_qty', 'alert minimum'), ('purchase_multiplier', 'units per purchase')):
            if key in rec:
                val = number(rec[key], label.capitalize(), key == 'min_qty')
                if val != row[key]:
                    conn.execute(f'UPDATE components SET {key}=? WHERE id=?', (val, cid))
                    changes.append(f'{label} {fmt_num(val)}')
        if rec.get('purchase_unit') and rec['purchase_unit'] != row['purchase_unit']:
            conn.execute('UPDATE components SET purchase_unit=? WHERE id=?', (rec['purchase_unit'][:80], cid))
            changes.append('purchase unit')
    if rec.get('supplier'):
        sname = clean(rec['supplier'], 'Supplier', 160)
        existing = conn.execute('SELECT * FROM suppliers WHERE component_id=? AND name=? COLLATE NOCASE',
                                (cid, sname)).fetchone()
        if existing is None:
            price = number(rec.get('price'), 'Price per unit (needed for a new supplier)', True)
            url = normal_url(rec.get('url'))
            sid = conn.execute('INSERT INTO suppliers (component_id,name,url,price,updated_at) VALUES (?,?,?,?,?)',
                               (cid, sname, url, price, now())).lastrowid
            conn.execute('INSERT INTO supplier_price_history (supplier_id,price,changed_at) VALUES (?,?,?)',
                         (sid, price, now()))
            changes.append(f'reorder link added ({sname})')
        else:
            price = number(rec['price'], 'Price per unit', True) if 'price' in rec else existing['price']
            url = normal_url(rec['url']) if 'url' in rec else existing['url']
            if price != existing['price'] or url != existing['url']:
                conn.execute('UPDATE suppliers SET url=?,price=?,updated_at=? WHERE id=?',
                             (url, price, now(), existing['id']))
                if price != existing['price']:
                    conn.execute('INSERT INTO supplier_price_history (supplier_id,price,changed_at) VALUES (?,?,?)',
                                 (existing['id'], price, now()))
                    changes.append(f'{sname} price ₹{fmt_num(existing["price"])} → ₹{fmt_num(price)}')
                else:
                    changes.append(f'{sname} link updated')
    elif rec.get('url') or rec.get('price'):
        raise Problem('Add a Supplier name to save a price or reorder link.')
    if action == 'update' and not changes:
        action = 'same'
    return code, shown_name, action, changes


def import_design(conn, rec, mode):
    sku = clean(rec.get('sku'), 'SKU', 80)
    row = conn.execute('SELECT * FROM kits WHERE sku=?', (sku,)).fetchone()
    changes = []
    if row is None:
        name = clean(rec.get('name'), 'Design name (needed for a new design)')
        size = (rec.get('size') or '').strip().upper()
        if size not in ('A5', 'A4', 'A3'):
            raise Problem('Size must be A5, A4 or A3 for a new design.')
        counted = 'packed_qty' in rec
        packed = nonneg_int(rec['packed_qty'], 'Packed stock') if counted else 0
        colours = paint_colour_count(rec.get('paint_colours'))
        conn.execute('INSERT INTO kits (sku,name,size,packed_qty,stock_counted,subtitle,barcode,mrp,'
                     'selling_price,paint_colours,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (sku, name, size, packed, int(counted), (rec.get('subtitle') or '')[:160],
                      check_barcode(rec.get('barcode')), optional_price(rec.get('mrp'), 'MRP'),
                      optional_price(rec.get('selling_price'), 'Selling price'), colours, now()))
        audit(conn, 'Bulk upload', f'{sku}: new design {name} ({size}), packed stock '
              + (str(packed) if counted else 'not counted'))
        return sku, name, 'new', [f'new design, {packed if counted else "not counted"} packed']
    if rec.get('size') and rec['size'].strip().upper() != row['size']:
        raise Problem(f"Size can't be changed by upload (this design is {row['size']}).")
    if 'packed_qty' in rec:
        amount = nonneg_int(rec['packed_qty'], 'Packed stock')
        new = row['packed_qty'] + amount if mode == 'add' else amount
        if new != row['packed_qty'] or not row['stock_counted']:
            conn.execute('UPDATE kits SET packed_qty=?, stock_counted=1 WHERE id=?', (new, row['id']))
            before = row['packed_qty'] if row['stock_counted'] else 'not counted'
            changes.append(f'packed {before} → {new}')
            audit(conn, 'Bulk upload', f'{sku}: packed stock {before} → {new} (no parts deducted)')
    if rec.get('name') and rec['name'] != row['name']:
        conn.execute('UPDATE kits SET name=? WHERE id=?', (clean(rec['name'], 'Design name'), row['id']))
        changes.append('renamed')
    if 'paint_colours' in rec:
        colours = paint_colour_count(rec['paint_colours'])
        if colours != row['paint_colours']:
            conn.execute('UPDATE kits SET paint_colours=? WHERE id=?', (colours, row['id']))
            sync_paint_strip_recipe(conn, row['id'], colours)
            changes.append(f'{colours} colours (recipe needs confirming again)')
    for key, label in (('mrp', 'MRP'), ('selling_price', 'Selling price')):
        if key in rec:
            val = optional_price(rec[key], label)
            if val != row[key]:
                conn.execute(f'UPDATE kits SET {key}=? WHERE id=?', (val, row['id']))
                changes.append(f'{label} ₹{fmt_num(val)}')
    if rec.get('barcode') and rec['barcode'] != row['barcode']:
        conn.execute('UPDATE kits SET barcode=? WHERE id=?', (check_barcode(rec['barcode']), row['id']))
        changes.append('barcode')
    if rec.get('subtitle') and rec['subtitle'] != row['subtitle']:
        conn.execute('UPDATE kits SET subtitle=? WHERE id=?', (rec['subtitle'][:160], row['id']))
        changes.append('subtitle')
    return sku, row['name'], 'update' if changes else 'same', changes


# ---------------------------------------------------------------------------
# Reading any Excel layout: guess which column is what, let the person
# confirm, optionally ask a (free) AI model for help.
# ---------------------------------------------------------------------------
import difflib
import hashlib as _hashlib
import urllib.request
import urllib.error

AI_API_KEY = os.environ.get('AI_API_KEY') or os.environ.get('GEMINI_API_KEY', '')
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai/').rstrip('/') + '/'
AI_MODELS = [m.strip() for m in os.environ.get('AI_MODEL', 'gemini-flash-latest,gemini-3.6-flash,gemini-3.5-flash').split(',') if m.strip()]
AI_STATE = {'model': None}
AI_NAME_CACHE = {}

FIELD_LABELS = {
    'parts': dict(PART_COLUMNS),
    'designs': dict(DESIGN_COLUMNS),
}
# Keyword rules for headings that aren't an exact match (checked in order).
KEYWORD_RULES = [
    ('designs', 'sku', ('sku',)),
    ('parts', 'code', ('code', 'itemno', 'partno', 'id')),
    ('designs', 'packed_qty', ('packed',)),
    ('parts', 'min_qty', ('minimum', 'min', 'reorderlevel', 'reorderpoint', 'alert', 'safety')),
    ('parts', 'qty', ('qty', 'quantity', 'stock', 'onhand', 'inhand', 'balance', 'available', 'closing', 'count', 'nos', 'pieces')),
    ('parts', 'url', ('link', 'url', 'website', 'http')),
    ('parts', 'supplier', ('supplier', 'vendor', 'seller', 'shop', 'store', 'from')),
    ('designs', 'selling_price', ('selling', 'saleprice', 'sp')),
    ('designs', 'mrp', ('mrp',)),
    ('parts', 'price', ('price', 'rate', 'cost', 'amount', 'rs', 'inr')),
    ('parts', 'unit', ('unit', 'uom', 'measure')),
    ('designs', 'size', ('size',)),
    ('designs', 'paint_colours', ('colour', 'color', 'pots')),
    ('designs', 'barcode', ('barcode', 'ean')),
    ('parts', 'name', ('name', 'item', 'description', 'product', 'particulars', 'material', 'component', 'part', 'design', 'artwork', 'title')),
]


def norm(text):
    return re.sub(r'[^a-z0-9]', '', str(text or '').lower())


def guess_field(heading):
    key = header_key(heading)
    if key:
        return key
    n = norm(heading)
    if not n:
        return None
    for _kind, field, words in KEYWORD_RULES:
        if any(w in n for w in words):
            return field
    return None


def find_header_row(rows):
    """Pick the row (within the first 15) that looks most like column headings."""
    best, best_score = None, 0
    for i, row in enumerate(rows[:15]):
        texts = [cell_text(c) for c in row]
        filled = [t for t in texts if t]
        if len(filled) < 2:
            continue
        wordy = sum(1 for t in filled if not re.fullmatch(r'[-\d.,₹ ]+', t))
        known = sum(1 for t in filled if guess_field(t))
        score = known * 3 + wordy
        if score > best_score:
            best, best_score = i, score
    return best


def sheet_kind(columns):
    fields = set(columns.values())
    design_only = {'sku', 'packed_qty', 'size', 'paint_colours', 'barcode', 'mrp', 'selling_price'}
    return 'designs' if fields & design_only and not fields & {'code', 'supplier', 'url', 'unit'} else 'parts'


def clean_mapping(kind, columns):
    """Keep only valid fields for this kind, each used once (first column wins)."""
    allowed = FIELD_LABELS.get(kind, {})
    out, used = {}, set()
    for idx, field in sorted(((int(i), f) for i, f in columns.items()), key=lambda x: x[0]):
        if field in allowed and field not in used:
            out[idx] = field
            used.add(field)
    return out


def inspect_upload(sheets, header_rows=None):
    result = []
    for name, rows in sheets.items():
        h = find_header_row(rows)
        try:
            forced = int((header_rows or {}).get(name) or 0)
        except (TypeError, ValueError):
            forced = 0
        if 1 <= forced <= len(rows):
            h = forced - 1
        if h is None:
            result.append({'sheet': name, 'header_row': None, 'headers': [], 'samples': [], 'kind': 'skip',
                           'columns': {}, 'rows': len(rows)})
            continue
        headers = [cell_text(c) for c in rows[h]]
        guess = {}
        for i, head in enumerate(headers):
            f = guess_field(head)
            if f:
                guess[i] = f
        kind = sheet_kind(guess)
        if kind == 'designs' and 'qty' in guess.values():
            guess = {i: ('packed_qty' if f == 'qty' else f) for i, f in guess.items()}
        if kind == 'parts' and 'sku' in guess.values():
            guess = {i: ('code' if f == 'sku' else f) for i, f in guess.items()}
        guess = clean_mapping(kind, guess)
        has_key = bool(set(guess.values()) & {'code', 'sku', 'name'})
        samples = [[cell_text(c) for c in r][:len(headers)] for r in rows[h + 1:h + 6]]
        result.append({'sheet': name, 'header_row': h + 1, 'headers': headers, 'samples': samples,
                       'kind': kind if has_key else 'skip', 'columns': {str(k): v for k, v in guess.items()},
                       'rows': max(0, len(rows) - h - 1)})
    return result


def tables_from_mapping(sheets, mapping):
    tables = []
    for m in mapping:
        kind = m.get('kind')
        if kind not in ('parts', 'designs'):
            continue
        rows = sheets.get(m.get('sheet'))
        if rows is None:
            raise Problem(f'Sheet "{m.get("sheet")}" was not found in the file.')
        try:
            h = int(m.get('header_row')) - 1
        except (TypeError, ValueError):
            raise Problem('Choose which row has the column headings.')
        cols = clean_mapping(kind, m.get('columns') or {})
        if not set(cols.values()) & ({'sku', 'name'} if kind == 'designs' else {'code', 'name'}):
            raise Problem(f'Sheet "{m.get("sheet")}": choose which column has the '
                          + ('SKU or design name.' if kind == 'designs' else 'part code or part name.'))
        records = []
        for n, r in enumerate(rows[h + 1:], start=h + 2):
            rec = {}
            for idx, field in cols.items():
                if idx < len(r) and cell_text(r[idx]) != '':
                    rec[field] = cell_text(r[idx])
            if set(rec) & {'code', 'sku', 'name'}:
                records.append((n, rec))
        tables.append({'sheet': m['sheet'], 'kind': kind, 'records': records})
    if not tables:
        raise Problem('Pick at least one sheet to import, and say which column has the part code or name.')
    return tables


def auto_tables(sheets):
    mapping = [m for m in inspect_upload(sheets) if m['kind'] != 'skip']
    if not mapping:
        raise Problem('No stock table found. The first row needs column names, including "Part code" (for parts) '
                      'or "SKU" (for designs). Download the Excel template to see the layout.')
    return tables_from_mapping(sheets, mapping)


# ---------- matching rows by name ----------
def name_index(conn, kind):
    if kind == 'designs':
        rows = conn.execute('SELECT sku AS key, name FROM kits').fetchall()
    else:
        rows = conn.execute('SELECT code AS key, name FROM components').fetchall()
    return [(r['key'], r['name']) for r in rows]


def local_match(name, index):
    """Exact (ignoring case/punctuation) or a close spelling match. Returns (key, how)."""
    n = norm(name)
    for key, label in index:
        if norm(label) == n or norm(key) == n:
            return key, 'exact'
    labels = {norm(label): key for key, label in index}
    close = difflib.get_close_matches(n, list(labels), n=1, cutoff=0.82)
    if close:
        return labels[close[0]], 'similar'
    return None, None


# ---------- AI ----------
def ai_available():
    return bool(AI_API_KEY)


def ai_json(task, payload):
    """Ask the AI model for a JSON answer. Raises Problem with a friendly message."""
    if not AI_API_KEY:
        raise Problem('AI is not set up. Add a free AI_API_KEY in Railway Variables.', 400)
    models = [AI_STATE['model']] if AI_STATE['model'] else AI_MODELS
    last = 'no model available'
    for model in models:
        body = json.dumps({
            'model': model,
            'temperature': 0,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': 'You help a small paint-by-number kit warehouse read stock spreadsheets. '
                 'Reply with JSON only. Never invent data that is not in the input.'},
                {'role': 'user', 'content': task + '\n\nINPUT:\n' + json.dumps(payload, ensure_ascii=False)},
            ],
        }).encode()
        req = urllib.request.Request(AI_BASE_URL + 'chat/completions', body, method='POST', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + AI_API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=45) as res:
                data = json.loads(res.read())
        except urllib.error.HTTPError as e:
            last = f'{e.code}'
            if e.code in (400, 404) and len(models) > 1:
                continue
            if e.code == 429:
                raise Problem('The free AI limit was reached. Try again in a minute, or match the columns yourself.', 429)
            if e.code in (401, 403):
                raise Problem('The AI key was refused. Check AI_API_KEY in Railway Variables.', 502)
            raise Problem(f'The AI service returned an error ({e.code}). You can still match the columns yourself.', 502)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Problem('Could not reach the AI service. You can still match the columns yourself.', 502)
        try:
            text = data['choices'][0]['message']['content'] or ''
            text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M).strip()
            AI_STATE['model'] = model
            return json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError):
            raise Problem('The AI gave an answer that could not be read. Try again or match the columns yourself.', 502)
    raise Problem(f'No AI model worked (last error {last}). Set AI_MODEL in Railway Variables.', 502)


def ai_map_columns(sheets):
    base = inspect_upload(sheets)
    payload = {
        'fields_for_parts': {k: v for k, v in PART_COLUMNS},
        'fields_for_designs': {k: v for k, v in DESIGN_COLUMNS},
        'sheets': [{'sheet': name, 'first_rows': [[cell_text(c) for c in r][:30] for r in rows[:12]]}
                   for name, rows in sheets.items()],
    }
    task = ('For each sheet decide: kind = "parts" (raw materials / packing items with stock quantities), '
            '"designs" (finished paint-by-number kits by SKU with packed stock) or "skip"; header_row = the 1-based '
            'row number that holds the column headings; columns = object mapping 0-based column index (as a string) '
            'to one field key from the matching field list. Leave out columns that fit no field. '
            'Return {"sheets":[{"sheet":..., "kind":..., "header_row":..., "columns":{...}}]}.')
    answer = ai_json(task, payload)
    by_name = {s.get('sheet'): s for s in (answer.get('sheets') or []) if isinstance(s, dict)}
    for b in base:
        a = by_name.get(b['sheet'])
        rows = sheets[b['sheet']]
        if a and a.get('kind') in ('parts', 'designs', 'skip'):
            kind = a['kind']
            try:
                h = int(a.get('header_row'))
                h = h if 1 <= h <= min(len(rows), 30) else b['header_row']
            except (TypeError, ValueError):
                h = b['header_row']
            cols = clean_mapping(kind, a.get('columns') or {}) if kind != 'skip' else {}
            if h:
                headers = [cell_text(c) for c in rows[h - 1]]
                b.update({'kind': kind, 'header_row': h, 'headers': headers,
                          'samples': [[cell_text(c) for c in r][:len(headers)] for r in rows[h:h + 5]],
                          'columns': {str(k): v for k, v in cols.items() if k < len(headers)},
                          'rows': max(0, len(rows) - h)})
        b['ai'] = bool(a)
    return base


def ai_match_names(kind, names, index):
    """Ask the AI to match free-text item names to known codes. Cached so preview and save agree."""
    names = sorted(set(names))[:150]
    if not names:
        return {}
    key = _hashlib.sha256(json.dumps([kind, names, index]).encode()).hexdigest()
    if key in AI_NAME_CACHE:
        return AI_NAME_CACHE[key]
    task = ('Match each name in "names" to the one catalogue entry it clearly refers to, using the catalogue '
            '"key". If you are not confident, use null. Return {"matches": {"<name>": "<key or null>"}}.')
    answer = ai_json(task, {'names': names, 'catalogue': [{'key': k, 'name': n} for k, n in index]})
    valid = {k for k, _ in index}
    matches = {n: v for n, v in (answer.get('matches') or {}).items() if n in names and v in valid}
    if len(AI_NAME_CACHE) > 50:
        AI_NAME_CACHE.clear()
    AI_NAME_CACHE[key] = matches
    return matches


def resolve_names(conn, tables, use_ai):
    """Fill in code/sku for rows that only have a name. Adds a note saying how it was matched."""
    notes = {}
    for t in tables:
        key_field = 'sku' if t['kind'] == 'designs' else 'code'
        index = name_index(conn, t['kind'])
        unmatched = []
        for line, rec in t['records']:
            if rec.get(key_field) or not rec.get('name'):
                continue
            key, how = local_match(rec['name'], index)
            if key:
                rec[key_field] = key
                rec['_given_name'] = rec.pop('name')   # matched by name: never rename the item
                if how != 'exact':
                    notes[(t['sheet'], line)] = f'matched “{rec["_given_name"]}” by similar name → {key}'
            else:
                unmatched.append(rec['name'])
        if unmatched and use_ai and ai_available():
            matches = ai_match_names(t['kind'], unmatched, index)
            for line, rec in t['records']:
                if not rec.get(key_field) and rec.get('name') in matches:
                    rec[key_field] = matches[rec['name']]
                    notes[(t['sheet'], line)] = f'AI matched “{rec["name"]}” → {rec[key_field]}'
                    rec['_given_name'] = rec.pop('name')
        for line, rec in t['records']:
            if not rec.get(key_field):
                rec['_unmatched'] = True
    return notes


def decode_upload(body):
    filename = str(body.get('filename') or '').strip()
    lower = filename.lower()
    try:
        data = base64.b64decode(str(body.get('data') or ''), validate=True)
    except (binascii.Error, ValueError):
        raise Problem('The file could not be read. Please choose it again.')
    if not data:
        raise Problem('Choose an Excel file to upload.')
    if len(data) > IMPORT_MAX_BYTES:
        raise Problem('That file is over 5 MB. Split it into smaller files.')
    if lower.endswith('.xls'):
        raise Problem("Old .xls files aren't supported. In Excel choose File → Save As → Excel Workbook (.xlsx).")
    return filename, (read_csv(data) if lower.endswith('.csv') else read_xlsx(data))


def inspect_import(body):
    _, sheets = decode_upload(body)
    hr = body.get('header_rows') if isinstance(body.get('header_rows'), dict) else None
    return {'sheets': inspect_upload(sheets, hr), 'ai': ai_available(),
            'fields': {k: [{'key': f, 'label': l} for f, l in cols] for k, cols in
                       (('parts', PART_COLUMNS), ('designs', DESIGN_COLUMNS))}}


def ai_map_import(body):
    _, sheets = decode_upload(body)
    return {'sheets': ai_map_columns(sheets)}


def bulk_import(body, apply):
    """Preview (apply=False) or save (apply=True) an uploaded stock sheet.
    The preview runs the real changes inside a transaction and rolls back,
    so what you see is exactly what Save will do."""
    mode = body.get('mode')
    if mode not in ('set', 'add'):
        raise Problem('Choose whether the numbers are a stock count or stock received.')
    filename, sheets = decode_upload(body)
    mapping = body.get('mapping')
    if mapping is not None and not isinstance(mapping, list):
        raise Problem('Invalid column matching.')
    tables = tables_from_mapping(sheets, mapping) if mapping else auto_tables(sheets)
    # Match name-only rows to codes before locking the database (AI calls can take a few seconds).
    rconn = sqlite3.connect(str(DB)); rconn.row_factory = sqlite3.Row
    try:
        notes = resolve_names(rconn, tables, bool(body.get('ai_names')))
    finally:
        rconn.close()
    results, counts = [], {'new': 0, 'update': 0, 'same': 0, 'error': 0}
    with LOCK:
        conn = sqlite3.connect(str(DB), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            conn.execute('BEGIN IMMEDIATE')
            for t in tables:
                handler = import_design if t['kind'] == 'designs' else import_part
                for line, rec in t['records']:
                    conn.execute('SAVEPOINT row')
                    try:
                        if rec.get('_unmatched'):
                            what = 'design' if t['kind'] == 'designs' else 'part'
                            raise Problem(f'No {what} called “{rec.get("name")}”. Add a '
                                          + ('SKU' if what == 'design' else 'Part code')
                                          + f' column to create it as a new {what}, or fix the name.')
                        key, name, action, changes = handler(conn, rec, mode)
                        if (t['sheet'], line) in notes and action != 'same':
                            changes = [notes[(t['sheet'], line)]] + changes
                        conn.execute('RELEASE row')
                        results.append({'sheet': t['sheet'], 'kind': t['kind'], 'line': line, 'key': key,
                                        'name': name, 'action': action, 'changes': changes})
                        counts[action] += 1
                    except (Problem, sqlite3.IntegrityError) as e:
                        conn.execute('ROLLBACK TO row')
                        conn.execute('RELEASE row')
                        msg = e.msg if isinstance(e, Problem) else 'Value not allowed (' + str(e) + ')'
                        results.append({'sheet': t['sheet'], 'kind': t['kind'], 'line': line,
                                        'key': rec.get('code') or rec.get('sku') or '',
                                        'name': rec.get('name', ''), 'action': 'error', 'error': msg})
                        counts['error'] += 1
            if apply and (counts['new'] or counts['update']):
                audit(conn, 'Bulk upload', f'{filename or "file"}: {counts["new"]} new, {counts["update"]} updated, '
                      f'{counts["error"]} skipped ({"stock received" if mode == "add" else "stock count"})')
                conn.execute('COMMIT')
            else:
                conn.execute('ROLLBACK')
        except Exception:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
    return {'applied': bool(apply), 'counts': counts, 'results': results,
            'tables': [{'sheet': t['sheet'], 'kind': t['kind'], 'rows': len(t['records'])} for t in tables]}


def xlsx_bytes(sheets):
    """Build a small .xlsx file. sheets = [(name, header list, rows, column widths)]."""
    def col(i):
        out, i = '', i + 1
        while i:
            i, r = divmod(i - 1, 26)
            out = chr(65 + r) + out
        return out

    def cell(ref, value, style=0):
        s = f' s="{style}"' if style else ''
        if isinstance(value, bool):
            value = 'yes' if value else 'no'
        if isinstance(value, int):
            return f'<c r="{ref}"{s}><v>{value}</v></c>'
        if isinstance(value, float):
            return f'<c r="{ref}"{s}><v>{value:.10g}</v></c>'
        if value is None or value == '':
            return ''
        return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{xml_escape(str(value))}</t></is></c>'

    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    ct = 'application/vnd.openxmlformats-officedocument.spreadsheetml'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', head
                   + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   + '<Default Extension="xml" ContentType="application/xml"/>'
                   + f'<Override PartName="/xl/workbook.xml" ContentType="{ct}.sheet.main+xml"/>'
                   + f'<Override PartName="/xl/styles.xml" ContentType="{ct}.styles+xml"/>'
                   + ''.join(f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="{ct}.worksheet+xml"/>'
                             for i in range(len(sheets)))
                   + '</Types>')
        z.writestr('_rels/.rels', head + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', head + f'<workbook xmlns="{NS["m"]}" xmlns:r="{rel}"><sheets>'
                   + ''.join(f'<sheet name="{xml_escape(s[0])}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i, s in enumerate(sheets))
                   + '</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', head
                   + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + ''.join(f'<Relationship Id="rId{i+1}" Type="{rel}/worksheet" Target="worksheets/sheet{i+1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets)+1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', head + f'<styleSheet xmlns="{NS["m"]}">'
                   + '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                   + '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
                   + '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                   + '<fill><patternFill patternType="solid"><fgColor rgb="FF111111"/></patternFill></fill></fills>'
                   + '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
                   + '<cellXfs count="2"><xf/><xf fontId="1" fillId="2" applyFont="1" applyFill="1"/></cellXfs></styleSheet>')
        for i, (name, header, rows, widths) in enumerate(sheets):
            cols = ''.join(f'<col min="{j+1}" max="{j+1}" width="{w}" customWidth="1"/>' for j, w in enumerate(widths))
            data = ['<row r="1">' + ''.join(cell(f'{col(j)}1', h, 1) for j, h in enumerate(header)) + '</row>']
            for r, values in enumerate(rows, start=2):
                data.append(f'<row r="{r}">' + ''.join(cell(f'{col(j)}{r}', v) for j, v in enumerate(values)) + '</row>')
            z.writestr(f'xl/worksheets/sheet{i+1}.xml', head + f'<worksheet xmlns="{NS["m"]}">'
                       + '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                       + f'<cols>{cols}</cols><sheetData>' + ''.join(data) + '</sheetData></worksheet>')
    return buf.getvalue()


def stock_workbook():
    st = state()
    first_supplier = {}
    for s in st['suppliers']:
        first_supplier.setdefault(s['component_id'], s)
    blank = lambda v: '' if v is None else v
    parts = []
    for c in st['components']:
        s = first_supplier.get(c['id'])
        parts.append([c['code'], c['name'], c['unit'], c['qty'], c['min_qty'] or '',
                      c['purchase_multiplier'], c['purchase_unit'],
                      s['name'] if s else '', s['price'] if s else '', s['url'] if s else ''])
    designs = [[k['sku'], k['name'], k['size'], k['packed_qty'] if k['stock_counted'] else '',
                blank(k['paint_colours']), k['subtitle'], k['barcode'], blank(k['mrp']), blank(k['selling_price'])]
               for k in st['kits']]
    guide = [[line] for line in (
        '1. Change the numbers in "Stock quantity" (Parts sheet) or "Packed stock" (Designs sheet). Leave a cell empty to keep it as it is.',
        '2. Add new rows at the bottom for new parts or designs. A new part needs Part code, Part name and Unit (pcs, set, strip, tube, cm, ml or g).',
        '3. In the app open Bulk upload, drop this file in, and choose "Stock count" (replace the numbers) or "Stock received" (add the numbers).',
        '4. Check the preview, then press Save. Nothing changes until you press Save.',
        '5. Fill in Supplier, Price per unit and Reorder link to save a reorder link for a part.',
        'Note: uploading packed stock does not deduct parts. Use Pack orders for kits you pack.',
    )]
    return xlsx_bytes([
        ('Parts', [h for _, h in PART_COLUMNS], parts, [24, 44, 8, 15, 14, 18, 16, 20, 18, 44]),
        ('Designs', [h for _, h in DESIGN_COLUMNS], designs, [18, 30, 7, 14, 14, 36, 26, 10, 14]),
        ('How to use', ['How to use this file'], guide, [130]),
    ])


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
    if path=='/api/pack-batch': return pack_batch(body)
    if path=='/api/import/inspect': return inspect_import(body)
    if path=='/api/import/ai-map': return ai_map_import(body)
    if path=='/api/import/preview': return bulk_import(body, False)
    if path=='/api/import/apply': return bulk_import(body, True)
    m=re.fullmatch(r'/api/kits/(\d+)/(recipe|confirm-recipe|pack|handover|count)',path)
    if m:
        kid=int(m.group(1)); action=m.group(2)
        return {'recipe':save_recipe,'confirm-recipe':confirm_recipe,
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
    def client_ip(self):
        forwarded = self.headers.get('X-Forwarded-For', '')
        return forwarded.split(',')[0].strip() if forwarded else self.client_address[0]

    def session_valid(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return False
        try:
            expiry, signature = morsel.value.split('.', 1)
            if int(expiry) < time.time():
                return False
        except ValueError:
            return False
        good = hmac.new(SESSION_KEY, expiry.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, good)

    def session_cookie(self, clear=False):
        if clear:
            value, age = '', 0
        else:
            expiry = str(int(time.time()) + SESSION_DAYS * 86400)
            value = expiry + '.' + hmac.new(SESSION_KEY, expiry.encode(), hashlib.sha256).hexdigest()
            age = SESSION_DAYS * 86400
        flags = '; Secure' if RAILWAY else ''
        return f'{SESSION_COOKIE}={value}; Max-Age={age}; Path=/; HttpOnly; SameSite=Strict{flags}'

    def authorized(self):
        if not SECURE_MODE:
            return True
        if self.session_valid():
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

    def check_authorized(self, path='/'):
        if self.authorized():
            return True
        if path.startswith('/api/'):
            self.json_result(401, {'error': 'Your session has ended. Please sign in again.', 'login': True})
        else:
            self.send_response(303)
            self.send_header('Location', '/login')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
        return False

    def login(self, body):
        ip = self.client_ip()
        now_t = time.time()
        recent = [t for t in LOGIN_FAILURES.get(ip, []) if now_t - t < LOGIN_WINDOW]
        LOGIN_FAILURES[ip] = recent
        if len(recent) >= LOGIN_LIMIT:
            return self.json_result(429, {'error': 'Too many attempts. Please wait 15 minutes and try again.'})
        user = str(body.get('username') or '')
        password = str(body.get('password') or '')
        ok = (not SECURE_MODE) or (hmac.compare_digest(user.encode(), AUTH_USER.encode()) and
                                   hmac.compare_digest(password.encode(), AUTH_PASSWORD.encode()))
        if not ok:
            recent.append(now_t)
            return self.json_result(401, {'error': 'That username or password is not right.'})
        LOGIN_FAILURES.pop(ip, None)
        return self.json_result(200, {'ok': True}, cookie=self.session_cookie())

    def send_bytes(self,status,content,kind,cookie=None):
        self.send_response(status)
        if cookie:
            self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Type',kind)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if RAILWAY:
            self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
        self.send_header('Content-Length',str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    PUBLIC = {
        '/login': ('login.html', 'text/html; charset=utf-8'),
        '/login.js': ('login.js', 'text/javascript; charset=utf-8'),
        '/style.css': ('style.css', 'text/css; charset=utf-8'),
        '/logo.svg': ('logo.svg', 'image/svg+xml'),
        '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
    }
    PRIVATE = {
        '/': ('index.html', 'text/html; charset=utf-8'),
        '/index.html': ('index.html', 'text/html; charset=utf-8'),
        '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    }

    def do_HEAD(self):
        self.send_response(200 if urlparse(self.path).path in self.PUBLIC else 303)
        self.end_headers()

    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/favicon.ico':
            path='/favicon.svg'
        if path=='/login' and (not SECURE_MODE or self.session_valid()):
            self.send_response(303); self.send_header('Location','/'); self.send_header('Content-Length','0'); self.end_headers()
            return
        if path in self.PUBLIC:
            name, kind = self.PUBLIC[path]
            return self.send_bytes(200,(ROOT/'static'/name).read_bytes(),kind)
        if not self.check_authorized(path):
            return
        if path=='/api/export.xlsx':
            content=stock_workbook()
            self.send_response(200)
            self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            self.send_header('Content-Disposition',f'attachment; filename="aurva-stock-{datetime.now().strftime("%Y-%m-%d")}.xlsx"')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if path.startswith('/api/'):
            return self.handle_api('GET',path,{})
        if path not in self.PRIVATE:
            return self.send_bytes(404,b'Not found','text/plain')
        name, kind = self.PRIVATE[path]
        self.send_bytes(200,(ROOT/'static'/name).read_bytes(),kind)

    def do_POST(self):
        path=urlparse(self.path).path
        if path=='/api/logout':
            return self.json_result(200,{'ok':True},cookie=self.session_cookie(clear=True))
        if path!='/api/login' and not self.check_authorized(path):
            return
        if not path.startswith('/api/'):
            return self.send_bytes(404,b'Not found','text/plain')
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
            return self.json_result(415,{'error':'Use Content-Type: application/json.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length>(8*1024*1024 if path.startswith('/api/import/') else 65536):
                raise Problem('Request too large.',413)
            body=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body,dict):
                raise Problem('Request should be a JSON object.')
        except (ValueError,UnicodeError,json.JSONDecodeError):
            return self.json_result(400,{'error':'Invalid JSON request.'})
        except Problem as e:
            return self.json_result(e.status,{'error':e.msg})
        if path=='/api/login':
            return self.login(body)
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

    def json_result(self,status,payload,cookie=None):
        return self.send_bytes(status,json.dumps(payload).encode('utf-8'),'application/json; charset=utf-8',cookie)

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
