"""The company -> ATS registry (companies table) and the discovery queue (candidates)."""
import datetime

from . import adapters, db, names


def add_board(conn, company, ats, token, discovered_from, board_name="", careers_url="", notes="", active=1):
    """Insert a board if new; returns (id, created)."""
    if not ats or not token or adapters.get(ats) is None:
        return None, False
    if ats in ("greenhouse", "ashby", "workday"):
        pass  # case matters for Ashby/Workday site names; keep as given
    row = conn.execute("SELECT id, company, discovered_from FROM companies WHERE ats=? AND lower(token)=lower(?)", (ats, token)).fetchone()
    if row:
        if (not row["company"] or row["company"] == token) and company and company != token:
            conn.execute("UPDATE companies SET company=?, norm=? WHERE id=?", (company, names.norm_company(company), row["id"]))
        if discovered_from and discovered_from not in (row["discovered_from"] or ""):
            conn.execute("UPDATE companies SET discovered_from=? WHERE id=?",
                         (((row["discovered_from"] or "") + "," + discovered_from).strip(","), row["id"]))
        return row["id"], False
    url = careers_url or adapters.get(ats).public_url(token)
    cur = conn.execute(
        "INSERT INTO companies(company, norm, ats, token, careers_url, board_name, discovered_from, first_seen, next_due, active, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (company or token, names.norm_company(company or token), ats, token, url, board_name or "", discovered_from,
         db.now(), "1970-01-01T00:00:00+00:00", active, notes))
    return cur.lastrowid, True


def add_candidate(conn, company, source, website="", priority=5):
    n = names.norm_company(company)
    if not n or len(n) < 2:
        return False
    row = conn.execute("SELECT sources, website, priority FROM candidates WHERE norm=?", (n,)).fetchone()
    if row:
        srcs = db.jload(row["sources"], [])
        changed = False
        if source not in srcs:
            srcs.append(source)
            changed = True
        if website and not row["website"]:
            changed = True
        if changed or priority < row["priority"]:
            conn.execute("UPDATE candidates SET sources=?, website=COALESCE(NULLIF(website,''), ?), priority=MIN(priority, ?) WHERE norm=?",
                         (db.jdump(srcs), website or "", priority, n))
        return False
    conn.execute("INSERT INTO candidates(norm, company, sources, website, priority, first_seen) VALUES (?,?,?,?,?,?)",
                 (n, company.strip(), db.jdump([source]), website or "", priority, db.now()))
    return True


def boards_for(conn, company):
    n = names.norm_company(company)
    rows = conn.execute("SELECT * FROM companies WHERE norm=? ORDER BY active DESC, last_ok DESC", (n,)).fetchall()
    if not rows:
        core = "".join(names.core_words(company))
        if len(core) >= 4:
            rows = [r for r in conn.execute("SELECT * FROM companies WHERE norm LIKE ?", ("%" + core[:6] + "%",)).fetchall()
                    if "".join(names.core_words(r["company"])) == core]
    return rows


def probe_cached(conn, ats, token, miss_days):
    r = conn.execute("SELECT * FROM probes WHERE ats=? AND lower(token)=lower(?)", (ats, token)).fetchone()
    if not r:
        return None
    if r["status"] == "none":
        try:
            age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(r["checked_at"])
            if age.days >= miss_days:
                return None
        except (TypeError, ValueError):
            return None
    if r["status"] in ("blocked", "error"):
        return None
    return r


def save_probe(conn, ats, token, status, board_name="", jobs=None):
    conn.execute("INSERT OR REPLACE INTO probes(ats, token, status, board_name, jobs, checked_at) VALUES (?,?,?,?,?,?)",
                 (ats, token, status, board_name or "", jobs, db.now()))
