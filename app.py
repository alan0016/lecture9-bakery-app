#!/usr/bin/env python3
"""Home bakery order tracker (MGOC50 Lecture 9).

Start it:    python app.py            then open http://127.0.0.1:8000
Other port:  python app.py 8001
Stop it:     press Ctrl+C in the window where it runs.

Only Python's standard library. All data lives in one SQLite file, bakery.db, next to this file.
No AI inside the app: every automatic decision is one of the fixed rules below.
"""
import csv
import html
import http.server
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import date, timedelta
from urllib.parse import urlsplit

FOLDER = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(FOLDER, 'bakery.db')
PORT = 8000

# ----- The bakery's rules (the "About the bakery" box on the Instagram pages) -----
WEEK_START = date(2026, 11, 23)   # Monday 23 November 2026: the first day of messages
PRODUCTS = {
    'cupcakes': {'price': 40, 'hours': 0.5, 'one': 'dozen cupcakes', 'many': 'dozen cupcakes'},
    'cake': {'price': 120, 'hours': 2.0, 'one': 'cake', 'many': 'cakes'},
}
DAY_HOURS = 8        # the oven runs 8 hours a day
HELPER_HOURS = 4     # a helper adds 4 oven hours to one day
HELPER_COST = 100    # and costs $100
NOTICE_DAYS = 2      # orders, changes, cancellations and helper bookings need 2 days' notice
WAITING = ('requested', 'incompatible')   # new orders the baker has not said yes or no to
BOOKED = ('accepted', 'picked_up')        # orders that use oven time and bring revenue
STATUS_WORD = {'requested': 'waiting for your decision', 'incompatible': "waiting, but it can't be taken as it is",
               'accepted': 'accepted', 'rejected': 'rejected', 'cancelled': 'cancelled', 'picked_up': 'picked up'}

WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']


class RuleError(Exception):
    """An action that would break a rule. Its text is shown to the baker as it is."""


# ----- small helpers -----
def dstr(d): return d.isoformat()
def dparse(s): return date.fromisoformat(s)
def label(d): return f"{WEEKDAYS[d.weekday()][:3]} {d.day} {MONTHS[d.month - 1][:3]}"
def weekday(d): return WEEKDAYS[d.weekday()]
def hours_of(product, qty): return PRODUCTS[product]['hours'] * qty
def price_of(product, qty): return PRODUCTS[product]['price'] * qty
def item_text(product, qty): return f"{qty} {PRODUCTS[product]['one' if qty == 1 else 'many']}"
def h(x): return f"{x:g} h"
def money(x): return f"${x:,.0f}"
def plural(n, word): return f"{n} {word}{'' if n == 1 else 's'}"


# ----- reading dates, products and Instagram messages -----
_MON = r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?'
_WORDS = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
          'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12}
_NUM = r'(\d+|' + '|'.join(_WORDS) + r')'


def parse_day(text, year=WEEK_START.year):
    """'Wed Nov 25', 'Mon 23 Nov' or 'Monday 23 November 2026' -> a date. None if unclear or the weekday disagrees."""
    t = (text or '').lower()
    m = re.search(r'\b' + _MON + r'\s+(\d{1,2})\b', t)
    if m:
        mon, day = m.group(1), int(m.group(2))
    else:
        m = re.search(r'\b(\d{1,2})\s+' + _MON, t)
        if not m:
            return None
        day, mon = int(m.group(1)), m.group(2)
    try:
        d = date(year, [x[:3].lower() for x in MONTHS].index(mon) + 1, day)
    except ValueError:
        return None
    w = re.search(r'\b(mon|tue|wed|thu|fri|sat|sun)', t)
    if w and [x[:3].lower() for x in WEEKDAYS].index(w.group(1)) != d.weekday():
        return None
    return d


def parse_product(text):
    """'3 Dozen Cupcakes' -> ('cupcakes', 3); '1 Cake' -> ('cake', 1); anything else -> (None, None)."""
    t = (text or '').lower()
    num = lambda s: _WORDS.get(s) or int(s)
    m = re.search(_NUM + r'\s+dozen\b', t)
    if m and 'cupcake' in t:
        return 'cupcakes', num(m.group(1))
    m = re.search(_NUM + r'\s+cakes?\b', t)
    if m:
        return 'cake', num(m.group(1))
    return None, None


_MSG = re.compile(r'<div class="meta"><b>(.*?)</b>\s*&middot;\s*(.*?)</div>\s*<p class="message">(.*?)</p>', re.S)
_DAYLINE = re.compile(r'<div class="day">(.*?)</div>')


def instagram_file(d):
    return os.path.join(FOLDER, f'InstagramOrders_{weekday(d)}.html')


def read_instagram(d):
    """The messages on day d's Instagram page, or None if there is no page for that date."""
    path = instagram_file(d)
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        page = f.read()
    m = _DAYLINE.search(page)
    if not m or parse_day(html.unescape(m.group(1))) != d:
        return None   # the file is for another week
    out = []
    for handle, time, body in _MSG.findall(page):
        body = html.unescape(re.sub(r'<br\s*/?>', '\n', body)).strip()
        fields = {k.strip().lower(): v.strip() for k, v in re.findall(r'^\s*([A-Za-z ]+?)\s*:\s*(.*)$', body, re.M)}
        out.append({'handle': html.unescape(handle).strip(), 'time': html.unescape(time).strip(), 'text': body, 'fields': fields})
    return out


# ----- the database -----
SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
  date        TEXT PRIMARY KEY,          -- YYYY-MM-DD
  total_hours REAL NOT NULL DEFAULT 8,   -- the oven hours that day: 8, or 12 with a helper
  helper      INTEGER NOT NULL DEFAULT 0 -- 1 = a helper is booked ($100)
);
CREATE TABLE IF NOT EXISTS orders (
  id       INTEGER PRIMARY KEY,          -- the order number: 101 is shown as #101
  customer TEXT NOT NULL,
  product  TEXT NOT NULL CHECK (product IN ('cupcakes', 'cake')),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  pickup   TEXT NOT NULL REFERENCES days(date),   -- baked that day, picked up at 5 PM
  hours    REAL NOT NULL,                -- oven hours required: 0.5 per dozen cupcakes, 2 per cake
  status   TEXT NOT NULL,                -- requested, incompatible, accepted, rejected, cancelled, picked_up
  reason   TEXT NOT NULL DEFAULT '',     -- why it can't be taken, or what changed
  received TEXT                          -- the day its message came; empty for orders taken before Monday
);
-- One row per day, counted from the two tables above (so nothing is stored twice):
-- the number of orders, oven hours used and left, revenue and helper cost.
-- "How many oven hours are left on Friday?"  SELECT hours_left FROM day_totals WHERE date = '2026-11-27';
-- "What is the total revenue this week?"     SELECT SUM(revenue) FROM day_totals WHERE date BETWEEN '2026-11-23' AND '2026-11-29';
CREATE VIEW IF NOT EXISTS day_totals AS
  SELECT d.date, d.total_hours, d.helper, d.helper * 100 AS helper_cost,
         COUNT(o.id) AS orders,
         COALESCE(SUM(o.hours), 0) AS hours_used,
         d.total_hours - COALESCE(SUM(o.hours), 0) AS hours_left,
         COALESCE(SUM(o.quantity * CASE o.product WHEN 'cake' THEN 120 ELSE 40 END), 0) AS revenue
  FROM days d LEFT JOIN orders o ON o.pickup = d.date AND o.status IN ('accepted', 'picked_up')
  GROUP BY d.date;
CREATE TABLE IF NOT EXISTS messages (    -- every Instagram message the app has read, and what it did
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  day      TEXT NOT NULL,
  time     TEXT NOT NULL,
  customer TEXT NOT NULL,
  text     TEXT NOT NULL,
  kind     TEXT NOT NULL,                -- order, change, cancel, unclear
  outcome  TEXT NOT NULL,                -- waiting, done, refused, check
  result   TEXT NOT NULL,
  order_id INTEGER
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- today, and which days were fetched
"""
TABLES = ('days', 'orders', 'messages', 'state')


def connect(path=None):
    conn = sqlite3.connect(path or DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if get_state(conn, 'today') is None:
        with conn:
            start_week(conn)
    return conn


def get_state(conn, key):
    row = conn.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else None


def set_state(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', (key, value))


def get_today(conn): return dparse(get_state(conn, 'today'))
def ensure_day(conn, d): conn.execute('INSERT OR IGNORE INTO days (date) VALUES (?)', (dstr(d),))
def get_order(conn, oid): return conn.execute('SELECT * FROM orders WHERE id = ?', (oid,)).fetchone()
def has_helper(conn, d): return bool(conn.execute('SELECT helper FROM days WHERE date = ?', (dstr(d),)).fetchone()['helper'])
def hour_limit(conn, d): return conn.execute('SELECT total_hours FROM days WHERE date = ?', (dstr(d),)).fetchone()['total_hours']
def fetched(conn, d): return get_state(conn, 'fetched:' + dstr(d)) is not None


def used_hours(conn, d, exclude=None):
    """Oven hours taken on day d by accepted orders (leaving out order `exclude`, when a change is checked)."""
    return conn.execute("SELECT COALESCE(SUM(hours), 0) FROM orders WHERE pickup = ? AND status IN ('accepted', 'picked_up') AND id IS NOT ?",
                        (dstr(d), exclude)).fetchone()[0]


def start_week(conn):
    """Empty every table, then load the orders taken before Monday. Today is Monday 23 November."""
    for t in TABLES:
        conn.execute(f'DELETE FROM {t}')
    set_state(conn, 'today', dstr(WEEK_START))
    for i in range(7):
        ensure_day(conn, WEEK_START + timedelta(i))
    with open(os.path.join(FOLDER, 'existing_orders.csv'), newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            product, qty = parse_product(row['item'])
            pickup = parse_day(row['pickup_day'])
            if product is None or pickup is None:
                raise ValueError(f"existing_orders.csv: can't read {row}")
            ensure_day(conn, pickup)
            conn.execute('INSERT INTO orders (id, customer, product, quantity, pickup, hours, status, reason, received) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)',
                         (int(row['order'].strip().lstrip('#')), row['customer'].strip(), product, qty, dstr(pickup), hours_of(product, qty),
                          'accepted', 'Taken before Monday'))


# ----- the rules -----
def check_rules(conn, product, qty, pickup, sent_on, exclude=None):
    """(True, '') if an order can be taken; otherwise (False, the reason in plain words)."""
    gap = (pickup - sent_on).days
    if gap < NOTICE_DAYS:
        when = 'already past' if gap < 0 else 'the same day' if gap == 0 else 'the next day'
        return False, (f"Less than 2 days' notice: sent {label(sent_on)} for {label(pickup)} ({when}). "
                       f"A message sent {label(sent_on)} can be for {label(sent_on + timedelta(NOTICE_DAYS))} or later.")
    need, used, lim = hours_of(product, qty), used_hours(conn, pickup, exclude), hour_limit(conn, pickup)
    if used + need > lim:
        return False, f"Not enough oven time on {label(pickup)}: {h(used)} of {h(lim)} already booked, and this order needs {h(need)}."
    return True, ''


def refresh_waiting(conn):
    """Re-check every order still waiting for a decision (the oven time left can change after any action)."""
    for o in conn.execute("SELECT * FROM orders WHERE status IN ('requested', 'incompatible') ORDER BY id").fetchall():
        ok, reason = check_rules(conn, o['product'], o['quantity'], dparse(o['pickup']), dparse(o['received']), o['id'])
        conn.execute('UPDATE orders SET status = ?, reason = ? WHERE id = ?', ('requested' if ok else 'incompatible', reason, o['id']))


def log(conn, today, msg, customer, kind, outcome, result, order_id=None):
    conn.execute('INSERT INTO messages (day, time, customer, text, kind, outcome, result, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                 (dstr(today), msg['time'], customer, msg['text'], kind, outcome, result, order_id))


def find_open_order(conn, name, pickup):
    """The customer's open order, preferring one for the day named in the message."""
    return conn.execute("SELECT * FROM orders WHERE lower(customer) = lower(?) AND status IN ('accepted', 'requested', 'incompatible') "
                        "ORDER BY (pickup = ?) DESC, id DESC", (name, dstr(pickup))).fetchone()


def handle_message(conn, today, msg):
    f = msg['fields']
    name = (f.get('name') or msg['handle']).strip().title()
    order_text = f.get('order', '').strip()
    pickup = parse_day(f.get('pickup date') or f.get('pickup') or '')
    low = order_text.lower()
    kind = 'cancel' if low.startswith('cancel') else 'change' if low.startswith('change') else 'order'
    product, qty = (None, None) if kind == 'cancel' else parse_product(order_text)
    if pickup is None or (kind != 'cancel' and product is None):
        log(conn, today, msg, name, 'unclear', 'check', "Couldn't read the product or the pickup day. Reply to the customer yourself.")
        return
    ensure_day(conn, pickup)

    if kind == 'order':
        oid = conn.execute('SELECT COALESCE(MAX(id), 100) + 1 FROM orders').fetchone()[0]
        conn.execute('INSERT INTO orders (id, customer, product, quantity, pickup, hours, status, reason, received) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (oid, name, product, qty, dstr(pickup), hours_of(product, qty), 'requested', '', dstr(today)))
        refresh_waiting(conn)
        o = get_order(conn, oid)
        text = f"New order #{oid}: {item_text(product, qty)} for {label(pickup)}."
        text += ' Waiting for your yes or no.' if o['status'] == 'requested' else f" Can't be taken as it is: {o['reason']}"
        log(conn, today, msg, name, 'order', 'waiting', text, oid)
        return

    o = find_open_order(conn, name, pickup)
    if o is None:
        log(conn, today, msg, name, kind, 'refused', f"No open order from {name} to {kind}. Reply to the customer yourself.")
        return
    oid, old_pickup, old_item = o['id'], dparse(o['pickup']), item_text(o['product'], o['quantity'])

    if o['status'] in WAITING:   # not decided yet: the request itself changes
        if kind == 'cancel':
            conn.execute("UPDATE orders SET status = 'cancelled', reason = 'Cancelled before you decided' WHERE id = ?", (oid,))
            text = f"Request #{oid} cancelled before you decided."
        else:
            conn.execute('UPDATE orders SET product = ?, quantity = ?, pickup = ?, hours = ? WHERE id = ?',
                         (product, qty, dstr(pickup), hours_of(product, qty), oid))
            text = f"Request #{oid} changed to {item_text(product, qty)} for {label(pickup)}. Still waiting for your decision."
        refresh_waiting(conn)
        log(conn, today, msg, name, kind, 'done', text, oid)
        return

    days_left = (min(old_pickup, pickup) - today).days
    if days_left < NOTICE_DAYS:
        what = 'Cancellation' if kind == 'cancel' else 'Change'
        log(conn, today, msg, name, kind, 'refused',
            f"{what} refused: less than 2 days' notice ({label(old_pickup)} is {plural(days_left, 'day')} away). "
            f"Order #{oid} stays: {old_item} for {label(old_pickup)}. Let the customer know.", oid)
        return

    if kind == 'cancel':
        conn.execute("UPDATE orders SET status = 'cancelled', reason = ? WHERE id = ?", (f"Cancelled by the customer on {label(today)}", oid))
        refresh_waiting(conn)
        log(conn, today, msg, name, kind, 'done',
            f"Order #{oid} cancelled ({old_item} for {label(old_pickup)}). {h(hours_of(o['product'], o['quantity']))} of oven time freed.", oid)
        return

    need, used, lim = hours_of(product, qty), used_hours(conn, pickup, exclude=oid), hour_limit(conn, pickup)
    if used + need > lim:
        log(conn, today, msg, name, kind, 'refused',
            f"Change refused: not enough oven time on {label(pickup)} ({h(used)} of {h(lim)} booked without this order; the change needs {h(need)}). "
            f"Order #{oid} stays: {old_item} for {label(old_pickup)}. A helper for that day would make room.", oid)
        return
    conn.execute('UPDATE orders SET product = ?, quantity = ?, pickup = ?, hours = ?, reason = ? WHERE id = ?',
                 (product, qty, dstr(pickup), need, f"Changed on {label(today)} from {old_item} for {label(old_pickup)}", oid))
    refresh_waiting(conn)
    log(conn, today, msg, name, kind, 'done', f"Order #{oid} changed: {old_item} for {label(old_pickup)} → {item_text(product, qty)} for {label(pickup)}.", oid)


# ----- the baker's actions -----
def fetch(conn):
    today = get_today(conn)
    if fetched(conn, today):
        raise RuleError(f"{weekday(today)}'s messages are already loaded.")
    msgs = read_instagram(today)
    if msgs is None:
        raise RuleError(f"There is no Instagram messages file for {label(today)}.")
    set_state(conn, 'fetched:' + dstr(today), '1')
    for m in msgs:
        handle_message(conn, today, m)
    kinds = [r['kind'] for r in conn.execute('SELECT kind FROM messages WHERE day = ?', (dstr(today),))]
    parts = [plural(kinds.count('order'), 'new order'), plural(kinds.count('change'), 'change'), plural(kinds.count('cancel'), 'cancellation')]
    if kinds.count('unclear'):
        parts.append(plural(kinds.count('unclear'), 'message') + ' to check yourself')
    return f"{weekday(today)}'s messages loaded: {', '.join(parts)}."


def reject(conn, oid):
    o = get_order(conn, oid)
    if o is None:
        raise RuleError(f'There is no order #{oid}.')
    if o['status'] not in WAITING:
        raise RuleError(f"Order #{oid} is already {STATUS_WORD[o['status']]}.")
    conn.execute("UPDATE orders SET status = 'rejected', reason = ? WHERE id = ?", (('Rejected by you. ' + o['reason']).strip(), oid))
    refresh_waiting(conn)
    return f"Rejected #{oid} {o['customer']}. Let the customer know."


def accept_all_and_next_day(conn):
    """Accept the waiting orders one by one, oldest first, while each still fits; reject the rest.
    Then close today (its orders are picked up at 5 PM) and move to the next day."""
    today = get_today(conn)
    if read_instagram(today) is not None and not fetched(conn, today):
        raise RuleError(f"Get {weekday(today)}'s messages first.")
    done, auto = [], []
    for oid in [r['id'] for r in conn.execute("SELECT id FROM orders WHERE status IN ('requested', 'incompatible') ORDER BY id")]:
        refresh_waiting(conn)   # each accepted order uses oven time, so the next one is checked again
        o = get_order(conn, oid)
        if o['status'] == 'requested':
            conn.execute("UPDATE orders SET status = 'accepted', reason = '' WHERE id = ?", (oid,))
            done.append(f"#{oid} {o['customer']}")
        else:
            conn.execute("UPDATE orders SET status = 'rejected', reason = ? WHERE id = ?", ('Rejected automatically. ' + o['reason'], oid))
            auto.append(f"#{oid} {o['customer']}")
    picked = conn.execute("UPDATE orders SET status = 'picked_up' WHERE status = 'accepted' AND pickup <= ?", (dstr(today),)).rowcount
    new = today + timedelta(1)
    set_state(conn, 'today', dstr(new))
    ensure_day(conn, new)
    text = f"Accepted {plural(len(done), 'order')}" + (f" ({', '.join(done)})" if done else '') + '.'
    if auto:
        text += f" Rejected automatically because they break a rule: {', '.join(auto)}."
    return text + f" {label(today)} closed: {plural(picked, 'order')} picked up at 5 PM. It is now {weekday(new)}."


def hire_helper(conn, d):
    today = get_today(conn)
    if (d - today).days < NOTICE_DAYS:
        raise RuleError(f"Too late to book a helper for {label(d)}: helpers need 2 days' notice "
                        f"(today the earliest is {label(today + timedelta(NOTICE_DAYS))}).")
    ensure_day(conn, d)
    if has_helper(conn, d):
        raise RuleError(f"A helper is already booked for {label(d)}.")
    conn.execute('UPDATE days SET helper = 1, total_hours = ? WHERE date = ?', (DAY_HOURS + HELPER_HOURS, dstr(d)))
    refresh_waiting(conn)
    return f"Helper booked for {label(d)}: the oven has 12 hours that day. Cost {money(HELPER_COST)}."


ACTIONS = {
    '/api/fetch': lambda c, b: fetch(c),
    '/api/reject': lambda c, b: reject(c, int(b['id'])),
    '/api/accept_all_next_day': lambda c, b: accept_all_and_next_day(c),
    '/api/helper': lambda c, b: hire_helper(c, dparse(b['date'])),
}


def run_action(conn, path, body):
    try:
        with conn:   # one transaction: a broken rule changes nothing
            return {'ok': True, 'message': ACTIONS[path](conn, body)}
    except RuleError as e:
        return {'ok': False, 'message': str(e)}
    except (KeyError, ValueError, TypeError) as e:
        return {'ok': False, 'message': f"The app couldn't do that ({type(e).__name__}: {e})."}


# ----- everything the page shows -----
def state_json(conn):
    today = get_today(conn)
    orders = [dict(r) for r in conn.execute('SELECT * FROM orders ORDER BY pickup, id')]
    for o in orders:
        o['item'] = item_text(o['product'], o['quantity'])
        o['price'] = price_of(o['product'], o['quantity'])
        o['pickup_label'] = label(dparse(o['pickup']))

    days = []
    for r in conn.execute('SELECT * FROM day_totals ORDER BY date'):
        d = dparse(r['date'])
        notice = (d - today).days >= NOTICE_DAYS
        days.append({
            'date': r['date'], 'label': label(d), 'past': d < today, 'is_today': d == today,
            'helper': bool(r['helper']), 'limit': r['total_hours'], 'used': r['hours_used'], 'free': r['hours_left'],
            'revenue': r['revenue'], 'count': r['orders'],
            'orders': [o for o in orders if o['pickup'] == r['date']],
            'can_hire': not r['helper'] and notice,
            'helper_note': '' if notice or r['helper'] else "Needs 2 days' notice",
        })

    waiting = []
    for o in orders:
        if o['status'] not in WAITING:
            continue
        d = dparse(o['pickup'])
        lim, used = hour_limit(conn, d), used_hours(conn, d)
        w = dict(o, fits=o['status'] == 'requested', hours_left=lim - used, limit=lim, hint='')
        w['fit_text'] = 'Fits' if w['fits'] else o['reason']
        if not w['fits'] and (d - dparse(o['received'])).days >= NOTICE_DAYS and not has_helper(conn, d) \
                and (d - today).days >= NOTICE_DAYS and used + o['hours'] <= lim + HELPER_HOURS:
            w['hint'] = f"It would fit with a helper on {label(d)} (+4 h for {money(HELPER_COST)}); this order brings {money(o['price'])}."
        waiting.append(w)
    waiting.sort(key=lambda x: x['id'])

    msgs = [dict(r) for r in conn.execute('SELECT * FROM messages ORDER BY day DESC, id')]
    for m in msgs:
        m['day_label'] = label(dparse(m['day']))

    t = conn.execute('SELECT SUM(revenue) AS revenue, SUM(helper_cost) AS helper_cost, SUM(helper) AS helpers FROM day_totals').fetchone()
    totals = {'revenue': t['revenue'] or 0, 'helper_cost': t['helper_cost'] or 0, 'helpers': t['helpers'] or 0,
              'net': (t['revenue'] or 0) - (t['helper_cost'] or 0)}

    has_file, was_fetched = read_instagram(today) is not None, fetched(conn, today)
    new = today + timedelta(1)
    return {'today': dstr(today), 'today_label': f"{weekday(today)} {today.day} {MONTHS[today.month - 1]} {today.year}",
            'weekday': weekday(today), 'next_weekday': weekday(new),
            'can_fetch': has_file and not was_fetched,
            'fetch_note': ('' if has_file and not was_fetched else
                           f"{weekday(today)}'s messages are loaded." if was_fetched else f"No messages file for {label(today)}."),
            'can_next': was_fetched or not has_file,
            'waiting': waiting, 'days': days, 'messages': msgs, 'totals': totals}



# ----- the web page (plain HTML, CSS and JavaScript) -----
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bakery Orders</title>
<style>
:root { --bg:#faf6f0; --card:#fff; --ink:#2b2420; --muted:#6f625a; --line:#e7ddd2; --accent:#a8461f; --accent-2:#8d3a19;
        --ok:#23704a; --ok-bg:#e4f3ea; --bad:#b42318; --bad-bg:#fdeceb; --wait:#7a5200; --wait-bg:#fff3d1; --bar:#efe7de; --fill:#d08a5c; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
header.top { position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line); padding:12px 16px; }
.wrap { max-width:1100px; margin:0 auto; }
.toprow { display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; }
h1 { font-size:20px; margin:0; }
.today { color:var(--muted); font-size:14px; }
.today b { color:var(--ink); }
.btns { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
button { font:inherit; border:1px solid var(--line); background:var(--card); color:var(--ink); border-radius:8px; padding:7px 12px; cursor:pointer; }
button:hover:not(:disabled) { border-color:var(--accent); }
button:disabled { opacity:.5; cursor:not-allowed; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
button.primary:hover:not(:disabled) { background:var(--accent-2); }
button.yes { background:var(--ok); border-color:var(--ok); color:#fff; }
button.no { color:var(--bad); }
button.link { border:none; background:none; color:var(--accent); padding:0; text-decoration:underline; }
.status { margin:8px 0 0; padding:8px 12px; border-radius:8px; font-size:14px; }
.status:empty { display:none; }
.status.ok { background:var(--ok-bg); color:var(--ok); }
.status.bad { background:var(--bad-bg); color:var(--bad); }
.status.info { background:var(--wait-bg); color:var(--wait); }
.note { color:var(--muted); font-size:13px; margin-top:4px; }
main { padding:16px; }
section { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin:0 0 16px; }
h2 { font-size:17px; margin:0 0 10px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.pill { font-size:12px; font-weight:600; border-radius:999px; padding:2px 8px; background:var(--bar); color:var(--muted); }
.pill.warn { background:var(--wait-bg); color:var(--wait); }
.small { font-size:13px; color:var(--muted); }
.empty { color:var(--muted); font-size:14px; margin:4px 0; }
.req { border:1px solid var(--line); border-left:5px solid var(--ok); border-radius:10px; padding:10px 12px; margin:0 0 10px; display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; justify-content:space-between; }
.req.nofit { border-left-color:var(--bad); }
.req-info { min-width:0; flex:1 1 420px; }
.req-title { font-weight:600; }
.fit { font-size:14px; margin-top:3px; }
.fit.ok { color:var(--ok); } .fit.bad { color:var(--bad); }
.hint { font-size:13px; margin-top:4px; color:var(--wait); display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.req-btns { display:flex; gap:8px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; }
.tile { border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
.tile .n { font-size:22px; font-weight:700; }
.tile .t { font-size:13px; color:var(--muted); }
.day { display:grid; grid-template-columns:150px minmax(160px, 260px) 1fr; gap:6px 16px; padding:10px 0; border-top:1px solid var(--line); align-items:start; }
.day:first-of-type { border-top:none; }
.day.past { opacity:.55; }
.day.today .dname::after { content:"today"; margin-left:6px; font-size:11px; font-weight:700; color:#fff; background:var(--accent); border-radius:999px; padding:1px 7px; }
.dname { font-weight:600; }
.meter { height:12px; background:var(--bar); border-radius:6px; overflow:hidden; display:flex; }
.meter .used { background:var(--fill); }
.meter.full .used { background:var(--bad); }
.mtext { font-size:13px; margin-top:3px; }
.helper { font-size:13px; margin-top:6px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.chips { list-style:none; margin:0; padding:0; display:flex; flex-wrap:wrap; gap:6px; }
.chip { font-size:13px; border:1px solid var(--line); border-radius:999px; padding:2px 10px; background:#fff; }
.chip.accepted, .chip.picked_up { background:#fbf3ec; border-color:#ecd7c6; }
.chip.requested, .chip.incompatible { background:var(--wait-bg); border-style:dashed; }
.chip.rejected, .chip.cancelled { text-decoration:line-through; color:var(--muted); }
.msgday { font-size:13px; font-weight:700; color:var(--muted); margin:10px 0 4px; text-transform:uppercase; letter-spacing:.03em; }
.msg { display:grid; grid-template-columns:70px 1fr; gap:4px 12px; padding:6px 0; border-top:1px dashed var(--line); font-size:14px; }
.msg .raw { color:var(--muted); }
.tag { font-size:11px; font-weight:700; border-radius:4px; padding:1px 6px; margin-right:6px; }
.tag.done { background:var(--ok-bg); color:var(--ok); } .tag.refused { background:var(--bad-bg); color:var(--bad); }
.tag.waiting, .tag.check { background:var(--wait-bg); color:var(--wait); }
footer { padding:0 16px 24px; font-size:13px; color:var(--muted); }
@media (max-width:720px) { .day { grid-template-columns:1fr; } .msg { grid-template-columns:1fr; } }
</style></head>
<body>
<header class="top"><div class="wrap">
  <div class="toprow">
    <div><h1>Home Bakery · Orders</h1><div class="today" id="today">Loading…</div></div>
    <div class="btns"><button id="fetch" class="primary" type="button">Get the next day's messages</button></div>
  </div>
  <div class="note" id="note"></div>
  <div id="status" class="status" role="status" aria-live="polite"></div>
</div></header>
<main class="wrap">
  <section aria-labelledby="h-wait">
    <h2 id="h-wait">Waiting for your decision <span class="pill" id="wcount"></span>
      <button id="next" class="yes" type="button" style="margin-left:auto">Accept all and go to next day</button></h2>
    <div id="waiting"></div>
  </section>
  <section aria-labelledby="h-days">
    <h2 id="h-days">This week</h2>
    <p class="small" style="margin:0 0 6px">The oven runs 8 hours a day (12 with a helper). Orders are baked on their pickup day and picked up at 5 PM.
      Cupcakes: $40 and 30 min a dozen. Cake: $120 and 2 h each. Orders, changes, cancellations and helpers need 2 days' notice.</p>
    <div id="days"></div>
  </section>
  <section aria-labelledby="h-money">
    <h2 id="h-money">Money</h2>
    <div class="tiles" id="totals"></div>
  </section>
  <section aria-labelledby="h-msgs">
    <h2 id="h-msgs">Messages</h2>
    <div id="messages"></div>
  </section>
</main>
<footer class="wrap"><a href="/README.html" target="_blank" rel="noopener">How this app works (README)</a></footer>
<script>
'use strict';
const $ = id => document.getElementById(id);
let S = null, busy = false;
const money = n => '$' + Math.round(n).toLocaleString('en-US');
const hrs = x => (Math.round(x * 100) / 100) + ' h';
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
function say(text, kind) { const s = $('status'); s.textContent = text; s.className = 'status ' + kind; }
async function load() {
  try { const r = await fetch('/api/state'); S = await r.json(); render(); }
  catch (e) { say('Could not reach the app. Is it still running? Start it again with: python app.py', 'bad'); }
}
async function act(path, body, working) {
  if (busy) return;
  busy = true; document.body.style.cursor = 'progress';
  for (const b of document.querySelectorAll('button')) b.disabled = true;
  say(working || 'Working…', 'info');
  try {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    const d = await r.json();
    S = d.state; render(); say(d.message, d.ok ? 'ok' : 'bad');
  } catch (e) { say('Could not reach the app. Is it still running? (' + e.message + ')', 'bad'); render(); }
  finally { busy = false; document.body.style.cursor = ''; }
}

function render() {
  if (!S) return;
  $('today').replaceChildren('Today: ', el('b', {text: S.today_label}));
  const f = $('fetch');
  f.disabled = !S.can_fetch;
  f.onclick = () => act('/api/fetch', {}, `Loading ${S.weekday}'s messages…`);
  $('note').textContent = S.fetch_note;
  renderWaiting(); renderDays(); renderTotals(); renderMessages();
}

function renderWaiting() {
  const w = S.waiting, box = $('waiting');
  $('wcount').textContent = w.length; $('wcount').className = 'pill' + (w.length ? ' warn' : '');
  const fit = w.filter(x => x.fits).length, nofit = w.length - fit, nb = $('next');
  nb.disabled = !S.can_next;
  nb.title = S.can_next ? '' : `Get ${S.weekday}'s messages first`;
  nb.onclick = () => {
    let q = `Accept ${fit} order${fit === 1 ? '' : 's'} that fit${fit === 1 ? 's' : ''}`;
    if (nofit) q += ` and reject ${nofit} that break${nofit === 1 ? 's' : ''} a rule`;
    q += `, then close ${S.weekday} (its orders are picked up at 5 PM) and go to ${S.next_weekday}?\n\nThis can't be undone.`;
    if (confirm(q)) act('/api/accept_all_next_day', {}, `Accepting orders and going to ${S.next_weekday}…`);
  };
  if (!w.length) { box.replaceChildren(el('p', {class: 'empty', text: 'Nothing is waiting. New orders show up here when you get the day\'s messages.'})); return; }
  box.replaceChildren(...w.map(o => el('article', {class: 'req' + (o.fits ? '' : ' nofit')},
    el('div', {class: 'req-info'},
      el('div', {class: 'req-title', text: `#${o.id} ${o.customer} · ${o.item} · pickup ${o.pickup_label}`}),
      el('div', {class: 'small', text: `${money(o.price)} · ${hrs(o.hours)} in the oven · ${hrs(o.hours_left)} of ${hrs(o.limit)} left on ${o.pickup_label}`}),
      el('div', {class: 'fit ' + (o.fits ? 'ok' : 'bad'), text: (o.fits ? '✓ ' : '✗ ') + o.fit_text}),
      o.hint ? el('div', {class: 'hint', text: o.hint}) : null),
    el('div', {class: 'req-btns'},
      el('button', {class: 'no', type: 'button', text: 'Reject', onclick: () => act('/api/reject', {id: o.id}, `Rejecting #${o.id}…`)})))));
}

function renderDays() {
  $('days').replaceChildren(...S.days.map(d => {
    const pct = Math.max(0, Math.min(100, 100 * d.used / d.limit)).toFixed(1) + '%';
    const helper = d.helper ? el('div', {class: 'helper', text: 'Helper booked ($100, +4 h)'})
      : el('div', {class: 'helper'}, el('button', {type: 'button', text: 'Hire helper ($100)', disabled: !d.can_hire,
          title: d.can_hire ? null : d.helper_note,
          onclick: () => act('/api/helper', {date: d.date}, `Booking a helper for ${d.label}…`)}),
          d.can_hire ? null : el('span', {class: 'small', text: d.helper_note}));
    const orders = d.orders.length ? el('ul', {class: 'chips'}, d.orders.map(o => el('li', {class: 'chip ' + o.status,
      title: o.status.replace('_', ' ') + (o.reason ? ': ' + o.reason : ''),
      text: `#${o.id} ${o.customer} · ${o.item} · ${hrs(o.hours)}` + ({requested: ' · waiting', incompatible: ' · waiting', picked_up: ' · picked up', cancelled: ' · cancelled', rejected: ' · rejected'}[o.status] || '')})))
      : el('span', {class: 'small', text: 'No orders'});
    return el('div', {class: 'day' + (d.past ? ' past' : '') + (d.is_today ? ' today' : '')},
      el('div', {}, el('div', {class: 'dname', text: d.label}),
        el('div', {class: 'small', text: `Revenue ${money(d.revenue)} · ${d.count} order${d.count === 1 ? '' : 's'}`})),
      el('div', {},
        el('div', {class: 'meter' + (d.used >= d.limit ? ' full' : ''), role: 'img', 'aria-label': `${hrs(d.used)} of ${hrs(d.limit)} used`},
          el('span', {class: 'used', style: 'width:' + pct})),
        el('div', {class: 'mtext', text: `${hrs(d.used)} of ${hrs(d.limit)} used · ${hrs(d.free)} left`}),
        helper),
      orders);
  }));
}

function renderTotals() {
  const t = S.totals;
  const tile = (n, text) => el('div', {class: 'tile'}, el('div', {class: 'n', text: n}), el('div', {class: 't', text}));
  $('totals').replaceChildren(
    tile(money(t.revenue), 'Revenue from accepted orders'),
    tile(money(t.helper_cost), `Helper costs (${t.helpers} × $100)`),
    tile(money(t.net), 'Revenue minus helper costs'));
}

const TAG = {order: {waiting: 'New order'}, change: {done: 'Change applied', refused: 'Change refused'},
             cancel: {done: 'Cancellation applied', refused: 'Cancellation refused'}, unclear: {check: 'Check yourself'}};
function renderMessages() {
  const box = $('messages');
  if (!S.messages.length) { box.replaceChildren(el('p', {class: 'empty', text: 'No messages loaded yet.'})); return; }
  const out = []; let day = null;
  for (const m of S.messages) {
    if (m.day !== day) { day = m.day; out.push(el('div', {class: 'msgday', text: m.day_label})); }
    out.push(el('div', {class: 'msg'}, el('div', {class: 'small', text: m.time}),
      el('div', {}, el('div', {}, el('b', {text: m.customer + ': '}), el('span', {class: 'raw', text: m.text.replace(/\n/g, ' · ')})),
        el('div', {}, el('span', {class: 'tag ' + m.outcome, text: (TAG[m.kind] || {})[m.outcome] || m.outcome}), m.result))));
  }
  box.replaceChildren(...out);
}

load();
</script>
</body></html>
"""


# ----- the web server -----
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    db_path = DB_FILE

    def log_message(self, fmt, *args):
        pass   # keep the window quiet

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, code=200):
        self.send(code, json.dumps(value).encode('utf-8'), 'application/json')

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ('/', '/index.html'):
            self.send(200, PAGE.encode('utf-8'), 'text/html; charset=utf-8')
        elif path == '/README.html' and os.path.exists(os.path.join(FOLDER, 'README.html')):
            with open(os.path.join(FOLDER, 'README.html'), 'rb') as f:
                self.send(200, f.read(), 'text/html; charset=utf-8')
        elif path == '/api/state':
            with LOCK:
                conn = connect(self.db_path)
                try:
                    self.send_json(state_json(conn))
                finally:
                    conn.close()
        else:
            self.send(404, b'Not found', 'text/plain; charset=utf-8')

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ACTIONS:
            self.send_json({'ok': False, 'message': 'Unknown action.'}, 404)
            return
        try:
            size = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(size) or b'{}') if 0 < size <= 100_000 else {}
        except ValueError:
            body = {}
        with LOCK:
            conn = connect(self.db_path)
            try:
                result = run_action(conn, path, body)
                result['state'] = state_json(conn)
            finally:
                conn.close()
        self.send_json(result)


def main(argv):
    port, db = PORT, DB_FILE
    args = list(argv)
    if '--db' in args:   # for testing on a copy: python app.py --db test_copy.db
        i = args.index('--db')
        db = os.path.abspath(args[i + 1])
        del args[i:i + 2]
    if args:
        port = int(args[0])
    Handler.db_path = db
    connect(db).close()   # create bakery.db on the first run
    try:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError:
        print(f'Port {port} is busy (maybe the app is already running). Try another one: python app.py {port + 1}')
        return 1
    print(f'Bakery app is running. Open http://127.0.0.1:{port} in your browser.')
    print(f'Data file: {db}')
    print('To stop it, press Ctrl+C here.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
