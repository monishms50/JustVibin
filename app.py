from flask import Flask, render_template, request, jsonify, session
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime, date, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'career.db')

DEFAULT_USER = 'monish'
DEFAULT_PASS = 'changeme'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            industry TEXT,
            size TEXT,
            location TEXT,
            why TEXT,
            status TEXT DEFAULT 'Tracking',
            notes TEXT,
            created_at TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            department TEXT,
            date_found TEXT,
            status TEXT DEFAULT 'Tracking',
            notes TEXT,
            status_note TEXT DEFAULT '',
            created_at TEXT DEFAULT (date('now')),
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            role TEXT,
            linkedin TEXT,
            email TEXT,
            source TEXT DEFAULT 'Cold',
            warmth TEXT DEFAULT 'Stranger',
            referral_likelihood TEXT DEFAULT 'Medium',
            followup_date TEXT,
            followup_comment TEXT DEFAULT '',
            notes TEXT,
            created_at TEXT DEFAULT (date('now')),
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            type TEXT,
            notes TEXT,
            next_action TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL,
            position_id INTEGER NOT NULL,
            date TEXT,
            status TEXT DEFAULT 'Pending',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
            FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS intelligence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            source_type TEXT NOT NULL,
            contact_id INTEGER,
            note TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL
        );
    ''')
    # Safe migrations for existing DBs
    cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
    if 'followup_comment' not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN followup_comment TEXT DEFAULT ''")
    pcols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
    if 'status_note' not in pcols:
        conn.execute("ALTER TABLE positions ADD COLUMN status_note TEXT DEFAULT ''")
    exists = conn.execute('SELECT id FROM users WHERE username=?', (DEFAULT_USER,)).fetchone()
    if not exists:
        conn.execute('INSERT INTO users (username, password) VALUES (?,?)',
                     (DEFAULT_USER, hash_pw(DEFAULT_PASS)))
    conn.commit()
    conn.close()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    return render_template('index.html')

# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route('/api/dashboard')
def dashboard():
    conn = get_db()
    today = date.today().isoformat()

    total_companies  = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
    total_contacts   = conn.execute('SELECT COUNT(*) FROM contacts').fetchone()[0]
    total_positions  = conn.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
    active_positions = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status NOT IN ('Rejected','Closed','Offer')"
    ).fetchone()[0]

    # Follow-ups
    due = conn.execute('''
        SELECT c.id, c.name, c.followup_date, c.followup_comment, c.warmth, c.role,
               co.name as company_name, co.id as company_id
        FROM contacts c
        JOIN companies co ON c.company_id = co.id
        WHERE c.followup_date IS NOT NULL AND c.followup_date != ''
        ORDER BY c.followup_date ASC
    ''').fetchall()

    # Pipeline
    pipeline = conn.execute(
        'SELECT status, COUNT(*) as count FROM positions GROUP BY status'
    ).fetchall()

    # Active positions with referral count — ordered by deadline soonest first, nulls last
    active_pos = conn.execute('''
        SELECT p.id, p.title, p.date_found, p.status, p.status_note, p.notes, p.url,
               co.name as company_name, co.id as company_id,
               COUNT(DISTINCT r.id) as referral_count
        FROM positions p
        JOIN companies co ON p.company_id = co.id
        LEFT JOIN referrals r ON r.position_id = p.id
        WHERE p.status NOT IN ('Rejected','Closed','Offer')
        GROUP BY p.id
        ORDER BY
            CASE WHEN p.date_found IS NULL OR p.date_found = '' THEN 1 ELSE 0 END,
            p.date_found ASC
    ''').fetchall()

    conn.close()

    def days_overdue(d):
        if not d: return 0
        return (date.today() - date.fromisoformat(d)).days

    return jsonify({
        'stats': {
            'companies': total_companies,
            'contacts': total_contacts,
            'positions': total_positions,
            'active_positions': active_positions
        },
        'due': [{**dict(r), 'days_overdue': days_overdue(r['followup_date'])} for r in due],
        'pipeline': [dict(r) for r in pipeline],
        'active_positions': [dict(p) for p in active_pos]
    })

# ─── Companies ────────────────────────────────────────────────────────────────

@app.route('/api/companies', methods=['GET'])
def get_companies():
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    conn = get_db()
    q = '''SELECT c.*,
           COUNT(DISTINCT p.id) as position_count,
           COUNT(DISTINCT ct.id) as contact_count
           FROM companies c
           LEFT JOIN positions p ON p.company_id = c.id
           LEFT JOIN contacts ct ON ct.company_id = c.id
           WHERE 1=1'''
    params = []
    if search:
        q += ' AND c.name LIKE ?'
        params.append(f'%{search}%')
    if status:
        q += ' AND c.status = ?'
        params.append(status)
    q += ' GROUP BY c.id ORDER BY c.name ASC'
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/companies', methods=['POST'])
def add_company():
    d = request.json
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO companies (name, industry, size, location, why, status, notes) VALUES (?,?,?,?,?,?,?)',
        (d['name'], d.get('industry',''), d.get('size',''), d.get('location',''),
         d.get('why',''), d.get('status','Tracking'), d.get('notes',''))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({'status': 'ok', 'id': new_id})

@app.route('/api/companies/<int:cid>', methods=['GET'])
def get_company(cid):
    conn = get_db()
    company = conn.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()
    if not company:
        conn.close()
        return jsonify({'error': 'Not found'}), 404

    positions = conn.execute('''
        SELECT p.*,
        (SELECT COUNT(*) FROM referrals r WHERE r.position_id = p.id) as referral_count,
        (SELECT GROUP_CONCAT(c.name, ', ')
         FROM referrals r JOIN contacts c ON r.contact_id = c.id
         WHERE r.position_id = p.id) as referral_names
        FROM positions p WHERE p.company_id=?
        ORDER BY
            CASE WHEN p.date_found IS NULL OR p.date_found = '' THEN 1 ELSE 0 END,
            p.date_found ASC
    ''', (cid,)).fetchall()

    contacts = conn.execute('''
        SELECT c.*,
        MAX(i.date) as last_contact,
        COUNT(DISTINCT i.id) as interaction_count,
        COUNT(DISTINCT r.id) as referral_count
        FROM contacts c
        LEFT JOIN interactions i ON i.contact_id = c.id
        LEFT JOIN referrals r ON r.contact_id = c.id
        WHERE c.company_id=?
        GROUP BY c.id ORDER BY c.name ASC
    ''', (cid,)).fetchall()

    intelligence = conn.execute('''
        SELECT ig.*, ct.name as contact_name
        FROM intelligence ig
        LEFT JOIN contacts ct ON ig.contact_id = ct.id
        WHERE ig.company_id=?
        ORDER BY ig.date DESC, ig.created_at DESC
    ''', (cid,)).fetchall()

    conn.close()
    return jsonify({
        'company': dict(company),
        'positions': [dict(p) for p in positions],
        'contacts': [dict(c) for c in contacts],
        'intelligence': [dict(i) for i in intelligence]
    })

@app.route('/api/companies/<int:cid>', methods=['PUT'])
def update_company(cid):
    d = request.json
    conn = get_db()
    conn.execute(
        'UPDATE companies SET name=?, industry=?, size=?, location=?, why=?, status=?, notes=? WHERE id=?',
        (d['name'], d.get('industry',''), d.get('size',''), d.get('location',''),
         d.get('why',''), d.get('status','Tracking'), d.get('notes',''), cid)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/companies/<int:cid>', methods=['DELETE'])
def delete_company(cid):
    conn = get_db()
    conn.execute('DELETE FROM companies WHERE id=?', (cid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ─── Positions ────────────────────────────────────────────────────────────────

@app.route('/api/positions', methods=['POST'])
def add_position():
    d = request.json
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO positions (company_id, title, url, department, date_found, status, notes, status_note) VALUES (?,?,?,?,?,?,?,?)',
        (d['company_id'], d['title'], d.get('url',''), d.get('department',''),
         d.get('date_found',''), d.get('status','Tracking'),
         d.get('notes',''), d.get('status_note',''))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({'status': 'ok', 'id': new_id})

@app.route('/api/positions/<int:pid>', methods=['PUT'])
def update_position(pid):
    d = request.json
    conn = get_db()
    conn.execute(
        'UPDATE positions SET title=?, url=?, department=?, date_found=?, status=?, notes=?, status_note=? WHERE id=?',
        (d['title'], d.get('url',''), d.get('department',''), d.get('date_found',''),
         d.get('status','Tracking'), d.get('notes',''), d.get('status_note',''), pid)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/positions/<int:pid>', methods=['DELETE'])
def delete_position(pid):
    conn = get_db()
    conn.execute('DELETE FROM positions WHERE id=?', (pid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/positions/<int:pid>/referrals', methods=['GET'])
def get_position_referrals(pid):
    conn = get_db()
    rows = conn.execute('''
        SELECT r.*, c.name as contact_name, c.role as contact_role
        FROM referrals r JOIN contacts c ON r.contact_id = c.id
        WHERE r.position_id = ?
    ''', (pid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ─── Contacts ─────────────────────────────────────────────────────────────────

@app.route('/api/contacts', methods=['POST'])
def add_contact():
    d = request.json
    conn = get_db()
    cur = conn.execute(
        '''INSERT INTO contacts
           (company_id, name, role, linkedin, email, source, warmth, referral_likelihood, followup_date, followup_comment, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (d['company_id'], d['name'], d.get('role',''), d.get('linkedin',''),
         d.get('email',''), d.get('source','Cold'), d.get('warmth','Stranger'),
         d.get('referral_likelihood','Medium'), d.get('followup_date') or None,
         d.get('followup_comment',''), d.get('notes',''))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({'status': 'ok', 'id': new_id})

@app.route('/api/contacts/<int:cid>', methods=['GET'])
def get_contact(cid):
    conn = get_db()
    contact = conn.execute('''
        SELECT c.*, co.name as company_name, co.id as company_id
        FROM contacts c JOIN companies co ON c.company_id = co.id
        WHERE c.id=?
    ''', (cid,)).fetchone()
    if not contact:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    interactions = conn.execute(
        'SELECT * FROM interactions WHERE contact_id=? ORDER BY date DESC', (cid,)
    ).fetchall()
    referrals = conn.execute('''
        SELECT r.*, p.title as position_title, p.status as position_status
        FROM referrals r JOIN positions p ON r.position_id = p.id
        WHERE r.contact_id=? ORDER BY r.date DESC
    ''', (cid,)).fetchall()
    positions = conn.execute(
        'SELECT id, title, status FROM positions WHERE company_id=?',
        (contact['company_id'],)
    ).fetchall()
    conn.close()
    return jsonify({
        'contact': dict(contact),
        'interactions': [dict(i) for i in interactions],
        'referrals': [dict(r) for r in referrals],
        'positions': [dict(p) for p in positions]
    })

@app.route('/api/contacts/<int:cid>', methods=['PUT'])
def update_contact(cid):
    d = request.json
    conn = get_db()
    conn.execute(
        '''UPDATE contacts SET name=?, role=?, linkedin=?, email=?, source=?,
           warmth=?, referral_likelihood=?, followup_date=?, followup_comment=?, notes=? WHERE id=?''',
        (d['name'], d.get('role',''), d.get('linkedin',''), d.get('email',''),
         d.get('source','Cold'), d.get('warmth','Stranger'),
         d.get('referral_likelihood','Medium'), d.get('followup_date') or None,
         d.get('followup_comment',''), d.get('notes',''), cid)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/contacts/<int:cid>', methods=['DELETE'])
def delete_contact(cid):
    conn = get_db()
    conn.execute('DELETE FROM contacts WHERE id=?', (cid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ─── Interactions ─────────────────────────────────────────────────────────────

@app.route('/api/interactions', methods=['POST'])
def add_interaction():
    d = request.json
    conn = get_db()
    conn.execute(
        'INSERT INTO interactions (contact_id, date, type, notes, next_action) VALUES (?,?,?,?,?)',
        (d['contact_id'], d.get('date', date.today().isoformat()),
         d.get('type','LinkedIn DM'), d.get('notes',''), d.get('next_action',''))
    )
    if d.get('followup_date'):
        conn.execute(
            'UPDATE contacts SET followup_date=?, followup_comment=? WHERE id=?',
            (d['followup_date'], d.get('followup_comment',''), d['contact_id'])
        )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/interactions/<int:iid>', methods=['DELETE'])
def delete_interaction(iid):
    conn = get_db()
    conn.execute('DELETE FROM interactions WHERE id=?', (iid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ─── Referrals ────────────────────────────────────────────────────────────────

@app.route('/api/referrals', methods=['POST'])
def add_referral():
    d = request.json
    conn = get_db()
    exists = conn.execute(
        'SELECT id FROM referrals WHERE contact_id=? AND position_id=?',
        (d['contact_id'], d['position_id'])
    ).fetchone()
    if exists:
        conn.close()
        return jsonify({'status': 'exists'})
    conn.execute(
        'INSERT INTO referrals (contact_id, position_id, date, status, notes) VALUES (?,?,?,?,?)',
        (d['contact_id'], d['position_id'], d.get('date', date.today().isoformat()),
         d.get('status','Pending'), d.get('notes',''))
    )
    conn.execute("UPDATE contacts SET warmth='Referred' WHERE id=?", (d['contact_id'],))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/referrals/<int:rid>', methods=['DELETE'])
def delete_referral(rid):
    conn = get_db()
    conn.execute('DELETE FROM referrals WHERE id=?', (rid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ─── Intelligence ─────────────────────────────────────────────────────────────

@app.route('/api/intelligence', methods=['POST'])
def add_intelligence():
    d = request.json
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO intelligence (company_id, date, source_type, contact_id, note) VALUES (?,?,?,?,?)',
        (d['company_id'], d.get('date', date.today().isoformat()),
         d['source_type'], d.get('contact_id') or None, d['note'])
    )
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute('''
        SELECT ig.*, ct.name as contact_name
        FROM intelligence ig LEFT JOIN contacts ct ON ig.contact_id = ct.id
        WHERE ig.id=?
    ''', (new_id,)).fetchone()
    conn.close()
    return jsonify({'status': 'ok', 'entry': dict(row)})

@app.route('/api/intelligence/<int:iid>', methods=['DELETE'])
def delete_intelligence(iid):
    conn = get_db()
    conn.execute('DELETE FROM intelligence WHERE id=?', (iid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except:
        local_ip = '127.0.0.1'
    print("\n" + "="*50)
    print("  Career Network Tracker")
    print("="*50)
    print(f"  Local:    http://localhost:5001")
    print(f"  Network:  http://{local_ip}:5001")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5001, debug=False)
