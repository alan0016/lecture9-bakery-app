"""Tests for app.py. They run on a temporary copy of bakery.db, never on bakery.db itself.

Run:  python test_app.py
The main test is the one the prompt asks for: play the whole week, Monday to Sunday,
pressing "Accept all and go to next day" every day, check that no day ever needs more oven
hours than it has, and report what happened to every change and cancellation.
"""
import os
import shutil
import sys
import tempfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')   # the Windows console can't print "→" otherwise
FAILS = []
D =lambda d: date(2026, 11, d) if d > 3 else date(2026, 12, d)   # D(27) = Fri 27 Nov, D(1) = Tue 1 Dec


def check(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


def fresh(tmp, name):
    """A copy of bakery.db, started over at Monday 23 November (the real file is never opened for writing)."""
    path = os.path.join(tmp, name)
    real = os.path.join(HERE, 'bakery.db')
    if os.path.exists(real):
        shutil.copy(real, path)
    conn = app.connect(path)
    with conn:
        app.start_week(conn)
    return conn


def act(conn, path, body=None): return app.run_action(conn, path, body or {})
def status(conn, oid): return app.get_order(conn, oid)['status']
def hours_left(conn, d): return conn.execute('SELECT hours_left FROM day_totals WHERE date = ?', (app.dstr(d),)).fetchone()[0]


def over_limit(conn):
    return [r['date'] for r in conn.execute('SELECT * FROM day_totals') if r['hours_used'] > r['total_hours']]


def test_parsing():
    print('reading the messages')
    check(app.parse_day('Wed Nov 25') == D(25), "'Wed Nov 25'")
    check(app.parse_day('Mon 23 Nov') == D(23), "'Mon 23 Nov'")
    check(app.parse_day('Tue Dec 1') == D(1), "'Tue Dec 1'")
    check(app.parse_product('3 Dozen Cupcakes') == ('cupcakes', 3), "'3 Dozen Cupcakes'")
    check(app.parse_product('Change to 1 Dozen Cupcakes') == ('cupcakes', 1), "'Change to 1 Dozen Cupcakes'")
    check(app.parse_product('1 Cake') == ('cake', 1), "'1 Cake'")
    check(len(app.read_instagram(D(23))) == 3, 'Monday has 3 messages')


def test_whole_week(tmp):
    print('\nthe whole week, accepting all orders each day')
    conn = fresh(tmp, 'week.db')
    check(hours_left(conn, D(23)) == 4 and hours_left(conn, D(26)) == 2, 'orders from before Monday: Mon 4 h left, Thu 2 h left')
    r = act(conn, '/api/accept_all_next_day')
    check(not r['ok'], "can't go to the next day before getting the day's messages: " + r['message'])

    for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'):
        r = act(conn, '/api/fetch')
        check(r['ok'] and r['message'].startswith(f"{day}'s messages loaded"), r['message'])
        r = act(conn, '/api/fetch')
        check(not r['ok'], 'fetching the same day twice is refused')
        check(not over_limit(conn), f'{day}: no day over its oven hours after the messages')
        r = act(conn, '/api/accept_all_next_day')
        check(r['ok'], r['message'])
        check(not over_limit(conn), f'{day}: no day over its oven hours after accepting')
        check(not conn.execute("SELECT COUNT(*) FROM orders WHERE status IN ('requested', 'incompatible')").fetchone()[0],
              f'{day}: nothing left waiting')

    # what the week should give, worked out by hand from the messages
    expect = {114: 'picked_up', 115: 'rejected', 116: 'picked_up', 107: 'cancelled', 117: 'picked_up', 118: 'picked_up',
              119: 'rejected', 120: 'rejected', 121: 'picked_up', 122: 'picked_up', 123: 'rejected', 124: 'picked_up',
              125: 'rejected', 126: 'cancelled', 127: 'rejected', 128: 'picked_up', 129: 'accepted', 130: 'accepted',
              131: 'accepted', 132: 'accepted'}
    wrong = {oid: status(conn, oid) for oid, s in expect.items() if status(conn, oid) != s}
    check(not wrong, f'every order ends as worked out by hand {wrong or ""}')
    check(app.get_order(conn, 112)['quantity'] == 6, '#112 Moreau: change to 6 dozen applied')
    check(app.get_order(conn, 122)['quantity'] == 2, '#122 Lam: change refused, still 2 dozen')
    check(app.get_order(conn, 128)['quantity'] == 4, '#128 Zhou: change refused, still 4 dozen')

    print('\n  every change and cancellation:')
    for m in conn.execute("SELECT * FROM messages WHERE kind IN ('change', 'cancel') ORDER BY id"):
        print(f"    {app.label(app.dparse(m['day']))} {m['time']:>8}  {m['customer']:<7} {m['outcome'].upper():<8} {m['result']}")

    print('\n  the two questions the tables must answer:')
    left = hours_left(conn, D(27))
    rev = conn.execute("SELECT SUM(revenue) FROM day_totals WHERE date BETWEEN '2026-11-23' AND '2026-11-29'").fetchone()[0]
    print(f'    oven hours left on Friday: {left:g} h;  revenue this week (pickups Mon-Sun): {app.money(rev)}')
    check(left == 0, 'Friday is full: 0 h left')

    print('\n  the week, day by day:')
    for r in conn.execute('SELECT * FROM day_totals ORDER BY date'):
        print(f"    {app.label(app.dparse(r['date']))}: {r['hours_used']:g} of {r['total_hours']:g} h, "
              f"{r['orders']} orders, revenue {app.money(r['revenue'])}")
    t = app.state_json(conn)['totals']
    print(f"    total: revenue {app.money(t['revenue'])}, helpers {app.money(t['helper_cost'])}, net {app.money(t['net'])}")
    conn.close()


def test_helper_and_reject(tmp):
    print('\nhire a helper, and reject')
    conn = fresh(tmp, 'helper.db')
    act(conn, '/api/fetch'); act(conn, '/api/accept_all_next_day')   # Monday
    act(conn, '/api/fetch')                                           # Tuesday
    check(status(conn, 119) == 'incompatible', '#119 Park does not fit Fri (7 + 1.5 > 8 h)')
    r = act(conn, '/api/helper', {'date': '2026-11-25'})
    check(not r['ok'], "a helper for Wed is refused on Tue (1 day's notice): " + r['message'])
    r = act(conn, '/api/helper', {'date': '2026-11-27'})
    check(r['ok'] and status(conn, 119) == 'requested', 'with a helper on Fri, Park fits and becomes Requested')
    check(app.hour_limit(conn, D(27)) == 12, 'Fri now has 12 oven hours')
    r = act(conn, '/api/helper', {'date': '2026-11-27'})
    check(not r['ok'], 'a second helper for the same day is refused')
    r = act(conn, '/api/reject', {'id': 118})
    check(r['ok'] and status(conn, 118) == 'rejected', '#118 Tan rejected by the baker')
    r = act(conn, '/api/reject', {'id': 118})
    check(not r['ok'], 'rejecting twice is refused')
    act(conn, '/api/accept_all_next_day')
    check(status(conn, 119) == 'accepted', 'Park accepted with the helper')
    t = app.state_json(conn)['totals']
    check(t['helper_cost'] == 100 and t['net'] == t['revenue'] - 100, 'Money: helper cost $100 taken off the revenue')
    check(not over_limit(conn), 'no day over its oven hours')
    conn.close()


if __name__ == '__main__':
    tmp = tempfile.mkdtemp(prefix='bakery-test-')
    try:
        test_parsing()
        test_whole_week(tmp)
        test_helper_and_reject(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\nALL TESTS PASSED' if not FAILS else f'\n{len(FAILS)} FAILED: {FAILS}')
    sys.exit(1 if FAILS else 0)
