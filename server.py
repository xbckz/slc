"""
Slice League · Flask backend
Run: pip install -r requirements.txt && python server.py
Admin credentials: create admin.txt with one line: username:password
"""
import os, re, sqlite3, secrets, json, threading, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_from_directory, g, abort
from flask_cors import CORS

# ─── Constants ────────────────────────────────────────────────────────────────
MATCHERINO_BOUNTY_URL      = "https://api.matcherino.com/__api/bounties/findById"
MATCHERINO_TOTAL_SPENT_URL = "https://api.matcherino.com/__api/bounties/totalSpent"
MATCHERINO_BRACKETS_URL    = "https://api.matcherino.com/__api/brackets"
MATCHERINO_MATCH_STATS_URL = "https://api.matcherino.com/__api/games/brawlstars/match/stats"
MATCHERINO_PRIZE_SHARE     = Decimal("0.75")
MATCHERINO_CACHE_TTL       = 3600   # 1 hour
MATCHERINO_ORG_ID          = 1180

_mat_cache = {}
_mat_lock  = threading.Lock()


# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.resolve()
DB_PATH    = BASE_DIR / "slice.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MATCHERINO_CACHE_DIR = BASE_DIR / "cache" / "matcherino"
MATCHERINO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
MATCHERINO_FRESH_TTL    = MATCHERINO_CACHE_TTL    # 1 h for in-progress events
MATCHERINO_FINISHED_TTL = 30 * 24 * 3600          # 30 d for finished events

AVATAR_CACHE_DIR = BASE_DIR / "cache" / "avatars"
AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_API_URL = "https://api.rnt.dev/profile"
AVATAR_CACHE_TTL      = 30 * 24 * 3600            # 30 d for successful hits
AVATAR_NEG_CACHE_TTL  = 6 * 3600                  # 6 h for null/error results
AVATAR_FETCH_TIMEOUT  = 12                        # s per request
AVATAR_BATCH_TIMEOUT  = 60                        # s for an entire batch
AVATAR_BATCH_WORKERS  = 5                         # be polite to api.rnt.dev


def _cleanup_stale_avatar_cache():
    """On startup, drop any null avatar entries older than the negative TTL.

    Prevents transient upstream failures from sticking around forever and
    surfaces real avatars as soon as the server is restarted.
    """
    cutoff = time.time() - AVATAR_NEG_CACHE_TTL
    purged = 0
    for path in AVATAR_CACHE_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("avatar_id") and path.stat().st_mtime < cutoff:
                path.unlink()
                purged += 1
        except Exception:
            continue
    if purged:
        print(f"[avatars] purged {purged} stale null cache entries")


_cleanup_stale_avatar_cache()

# ─── App ──────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SLICE_SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SLICE_COOKIE_SECURE", "0") == "1",
    SESSION_COOKIE_NAME="slice_sess",
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)
CORS(app, supports_credentials=False)

# ─── Database ─────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    abbr       TEXT NOT NULL,
    color      TEXT DEFAULT '#e8192c',
    wins       INTEGER DEFAULT 0,
    losses     INTEGER DEFAULT 0,
    points     INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'active',
    sort_order INTEGER DEFAULT 99,
    logo_url   TEXT
);
CREATE TABLE IF NOT EXISTS players (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id    INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    ign        TEXT NOT NULL,
    country    TEXT DEFAULT 'BE',
    ico        TEXT NOT NULL,
    elims      INTEGER DEFAULT 0,
    deaths     INTEGER DEFAULT 0,
    mvps       INTEGER DEFAULT 0,
    wr         REAL DEFAULT 0,
    sort_order INTEGER DEFAULT 99
);
CREATE TABLE IF NOT EXISTS matches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phase      TEXT NOT NULL DEFAULT 'league',
    round_num  INTEGER DEFAULT 0,
    match_date TEXT,
    team1_id   INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    team2_id   INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    score1     INTEGER,
    score2     INTEGER,
    status     TEXT DEFAULT 'upcoming',
    stream_url TEXT,
    notes      TEXT
);
CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    link       TEXT,
    active     INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

SEED_TEAMS = [
    ("Imagine Failing Pro", "IFP", "#e8192c", 6, 1, 18, "playoffs", 1),
    ("TOP Esports",          "TOP", "#f59e0b", 5, 2, 15, "playoffs", 2),
    ("Revoy Esports",        "REV", "#22c55e", 4, 3, 12, "playoffs", 3),
    ("Furies",               "FUR", "#8b5cf6", 4, 3, 12, "playoffs", 4),
    ("Nova Rising",          "NVR", "#06b6d4", 3, 4,  9, "eliminated", 5),
    ("Skepsis Gaming",       "SKP", "#ef4444", 2, 5,  6, "eliminated", 6),
    ("Paurigon-Z",           "PGZ", "#a855f7", 2, 5,  6, "eliminated", 7),
    ("Haneki Corp",          "HNK", "#64748b", 1, 6,  3, "eliminated", 8),
]

SEED_PLAYERS = [
    # (ign, abbr, country, ico, elims, deaths, mvps, wr, order)
    ("mikeyyy",     "IFP", "BE", "MK", 347, 108, 6, 78.0, 1),
    ("Yesse",       "IFP", "NL", "YS", 321, 111, 5, 78.0, 2),
    ("BronzeKing",  "TOP", "BE", "BK", 308, 110, 5, 67.0, 3),
    ("Rensboy",     "FUR", "NL", "RB", 298, 110, 4, 55.0, 4),
    ("Djpatatje",   "IFP", "BE", "DJ", 287, 110, 4, 78.0, 5),
    ("NovaMaster",  "NVR", "FR", "NM", 276, 110, 3, 56.0, 6),
    ("Beaver",      "FUR", "BE", "BV", 265, 110, 3, 55.0, 7),
    ("TopCarry",    "TOP", "BE", "TC", 258, 107, 4, 67.0, 8),
    ("RevoyAce",    "REV", "BE", "RA", 244, 111, 3, 56.0, 9),
    ("Wout",        "FUR", "BE", "WO", 238, 113, 2, 55.0, 10),
    ("SkepsisPro",  "SKP", "BE", "SP", 231, 115, 2, 44.0, 11),
    ("PauriZ",      "PGZ", "BE", "PZ", 224, 118, 2, 44.0, 12),
    ("NovaRush",    "NVR", "BE", "NR", 218, 121, 2, 56.0, 13),
    ("HanekiCore",  "HNK", "BE", "HC", 204, 125, 1, 22.0, 14),
    ("RevoyFlash",  "REV", "NL", "RF", 198, 127, 2, 56.0, 15),
    ("SkepsisX",    "SKP", "BE", "SX", 186, 130, 1, 44.0, 16),
    ("ZenithY",     "HNK", "BE", "ZY", 178, 134, 1, 22.0, 17),
    ("PauriGhost",  "PGZ", "NL", "PG", 172, 138, 1, 44.0, 18),
    ("HanekiSlice", "HNK", "BE", "HS", 161, 142, 0, 22.0, 19),
    ("SkepsisAce",  "SKP", "BE", "SA", 154, 148, 1, 44.0, 20),
    ("NovaZen",     "NVR", "BE", "NZ", 146, 152, 1, 56.0, 21),
    ("PauriMax",    "PGZ", "BE", "PM", 138, 156, 0, 44.0, 22),
    ("RevoyDash",   "REV", "BE", "RD", 131, 161, 1, 56.0, 23),
    ("TopSupport",  "TOP", "BE", "TS", 124, 166, 2, 67.0, 24),
]

DEFAULT_CONFIG = {
    "matcherino_id": "",
    "season_name":   "Season 2",
    "season_year":   "2026",
    "announcement":  "Playoffs starting June 15, 2026 · watch live on Twitch!",
    "announcement_link": "",
    "twitch_url":    "https://www.twitch.tv",
    "instagram_url": "https://www.instagram.com/slice.esports/",
    "tiktok_url":    "https://www.tiktok.com/@sliceesport",
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    # Seed default config values
    for k, v in DEFAULT_CONFIG.items():
        conn.execute("INSERT OR IGNORE INTO config(key,value) VALUES(?,?)", (k, v))

    # Seed teams if empty
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0:
        for t in SEED_TEAMS:
            conn.execute(
                "INSERT INTO teams(name,abbr,color,wins,losses,points,status,sort_order)"
                " VALUES(?,?,?,?,?,?,?,?)", t)

    # Seed players if empty (map abbr → team id)
    if conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0:
        abbr_to_id = {r[0]: r[1] for r in
                      conn.execute("SELECT abbr, id FROM teams").fetchall()}
        for p in SEED_PLAYERS:
            ign, abbr, country, ico, elims, deaths, mvps, wr, order = p
            tid = abbr_to_id.get(abbr)
            conn.execute(
                "INSERT INTO players(team_id,ign,country,ico,elims,deaths,mvps,wr,sort_order)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, ign, country, ico, elims, deaths, mvps, wr, order))

    # Seed default announcement if empty
    if conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO announcements(text,link,active) VALUES(?,?,1)",
            ("Playoffs starting June 15, 2026 · watch live on Twitch!", ""))

    conn.commit()
    conn.close()


init_db()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]


def cfg(key, default=""):
    db = get_db()
    row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


# ─── Matcherino helpers ───────────────────────────────────────────────────────

def _extract_bounty(raw):
    if isinstance(raw, dict):
        for key in ("body", "bounty", "data", "tournament"):
            if isinstance(raw.get(key), dict):
                return raw[key]
        return raw
    return {}


def _matcherino_image_url(url):
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if "_next/image" in url and "url=" in url:
        try:
            from urllib.parse import parse_qs, unquote
            raw = parse_qs(urlparse(url).query).get("url", [""])[0]
            url = unquote(raw) or url
        except Exception:
            pass
    return url if url.startswith(("http://", "https://")) else ""


def _find_matcherino_image(obj):
    if not isinstance(obj, dict):
        return ""
    meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
    for candidate in (meta.get("backgroundImg"), meta.get("bannerImg"),
                      meta.get("image"), obj.get("backgroundImg"),
                      obj.get("bannerUrl"), obj.get("banner"),
                      obj.get("imageUrl"), obj.get("heroImg")):
        url = _matcherino_image_url(candidate)
        if url:
            return url
    return ""


def _format_usd(amount):
    return f"${amount:,.0f}" if amount else None


def _fetch_matcherino(bounty_id):
    if not bounty_id:
        return None
    try:
        bid = int(bounty_id)
    except (TypeError, ValueError):
        return None

    now = time.time()
    with _mat_lock:
        cached = _mat_cache.get(bid)
        if cached and now - cached["ts"] < MATCHERINO_CACHE_TTL:
            return dict(cached["info"])

    try:
        r = requests.get(MATCHERINO_BOUNTY_URL, params={"id": bid}, timeout=10)
        r.raise_for_status()
        b = _extract_bounty(r.json())
    except Exception:
        return None

    # Prize pool
    prize = (b.get("prizePool") or b.get("totalPrize") or
             b.get("prizePoolTotal") or b.get("crowdfundedAmount"))
    try:
        if isinstance(prize, (int, float)):
            amount = prize / 100 if prize > 1000 else prize
            prize_str = _format_usd(amount)
        else:
            # try crowdfunding endpoint
            r2 = requests.get(MATCHERINO_TOTAL_SPENT_URL,
                              params={"bountyId": bid}, timeout=10)
            spent_raw = _extract_bounty(r2.json()).get("amount", 0)
            total = Decimal(str(spent_raw))
            prize_d = (total * MATCHERINO_PRIZE_SHARE).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)
            prize_str = _format_usd(int(prize_d))
    except Exception:
        prize_str = str(prize) if prize else None

    date_raw = (b.get("startAt") or b.get("startTime") or
                b.get("startDate") or b.get("scheduledStartAt"))

    info = {
        "matcherino_id": bid,
        "name":        b.get("name") or b.get("title") or "",
        "date":        date_raw,
        "prize":       prize_str,
        "image":       _find_matcherino_image(b),
        "description": b.get("description") or b.get("shortDescription") or "",
        "link":        f"https://matcherino.com/tournaments/{bid}",
    }

    if info["name"]:
        with _mat_lock:
            _mat_cache[bid] = {"ts": now, "info": dict(info)}

    return info


# ─── Static serving ───────────────────────────────────────────────────────────

STATIC_EXTS = {".html", ".css", ".js", ".json", ".svg", ".ico",
               ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2"}


@app.route("/")
def root():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    candidate = (BASE_DIR / path).resolve()
    if not str(candidate).startswith(str(BASE_DIR)):
        abort(404)
    if candidate.is_file():
        resp = send_from_directory(BASE_DIR, path)
        if path.endswith((".css", ".js")):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp
    abort(404)


@app.route("/uploads/<path:fname>")
def uploads(fname):
    return send_from_directory(UPLOAD_DIR, fname)


# ─── Public API ───────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config():
    db = get_db()
    rows = db.execute("SELECT key, value FROM config").fetchall()
    data = {r["key"]: r["value"] for r in rows}
    # Strip matcherino_id from public response
    data.pop("matcherino_id", None)
    return jsonify(data)


@app.get("/api/tournament")
def api_tournament():
    mat_id = cfg("matcherino_id")
    info = _fetch_matcherino(mat_id) if mat_id else None
    return jsonify({
        "season_name": cfg("season_name", "Season 2"),
        "season_year": cfg("season_year", "2026"),
        "matcherino":  info,
    })


# ─── Season 2 qualifiers (Matcherino proxy) ───────────────────────────────────
# These four bounty IDs are the four open qualifiers for Slice League S2.
QUALIFIER_BOUNTY_IDS = [208621, 210821, 210822, 210823]
_QUAL_CACHE = {"ts": 0.0, "data": None}
_QUAL_TTL   = 120  # seconds

# In-memory cache is just a hot reference; the background refresher writes
# the assembled payload to disk so restarts pick up instantly.
_QUAL_DATA_CACHE = {"ts": 0.0, "data": None}
_QUAL_DATA_TTL   = float("inf")           # served from memory; refreshed by thread
_QUAL_DATA_DISK  = BASE_DIR / "cache" / "qualifier_data.json"
_QUAL_DATA_SCHEMA_VERSION = 2
_QUAL_REFRESH_INTERVAL = 10 * 60          # 10 minutes
_qual_data_lock = threading.Lock()


def _safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _abbr(name):
    parts = re.findall(r"[A-Za-z0-9]+", str(name or ""))
    if not parts:
        return "TBD"
    if len(parts) == 1:
        return parts[0][:4].upper()
    return "".join(p[0] for p in parts[:4]).upper()


def _score_text(wins, losses):
    return f"{wins}-{losses}"


def _round_label(round_num, total_rounds):
    round_num = _safe_int(round_num)
    total_rounds = _safe_int(total_rounds)
    if total_rounds and round_num == total_rounds:
        return "Final"
    if total_rounds and round_num == total_rounds - 1:
        return "Semi Finals"
    if total_rounds and round_num == total_rounds - 2:
        return "Quarter Finals"
    remaining = 2 ** max((total_rounds or 0) - round_num, 1)
    return f"Round of {remaining}" if remaining >= 8 else f"Round {round_num}"


def _qualifier_number_from_bounty(bounty_id, title=""):
    m = re.search(r"#\s*(\d+)", str(title or ""))
    if m:
        return int(m.group(1))
    return QUALIFIER_BOUNTY_IDS.index(bounty_id) + 1 if bounty_id in QUALIFIER_BOUNTY_IDS else 0


def _entrant_logo(entrant):
    if not isinstance(entrant, dict):
        return ""
    team = entrant.get("team") if isinstance(entrant.get("team"), dict) else {}
    nested = team.get("team") if isinstance(team.get("team"), dict) else {}
    return _matcherino_image_url(
        entrant.get("avatar") or team.get("avatar") or nested.get("avatar") or ""
    )


def _entrant_members(entrant):
    if not isinstance(entrant, dict):
        return []
    team = entrant.get("team") if isinstance(entrant.get("team"), dict) else {}
    nested = team.get("team") if isinstance(team.get("team"), dict) else {}
    raw_members = team.get("members") or nested.get("members") or []
    members = []
    for member in raw_members:
        if not isinstance(member, dict):
            continue
        name = (member.get("displayName") or member.get("userName") or "").strip()
        if not name:
            continue
        members.append({
            "id": member.get("userId"),
            "name": name,
            "avatar": _matcherino_image_url(member.get("avatar") or ""),
            "captain": bool(member.get("captain")),
        })
    return members


def _team_from_entrant(entrant):
    if not isinstance(entrant, dict):
        return None
    team = entrant.get("team") if isinstance(entrant.get("team"), dict) else {}
    nested = team.get("team") if isinstance(team.get("team"), dict) else {}
    name = entrant.get("name") or team.get("name") or nested.get("name") or "TBD"
    return {
        "entrant_id": entrant.get("id"),
        "team_id": team.get("id") or entrant.get("teamId"),
        "global_team_id": nested.get("id") or team.get("id") or entrant.get("teamId"),
        "name": name,
        "abbr": _abbr(name),
        "logo_url": _entrant_logo(entrant),
        "seed": entrant.get("seed"),
        "placement": entrant.get("placement"),
        "countPlace": entrant.get("countPlace"),
        "members": _entrant_members(entrant),
    }


def _team_key(team):
    if not team:
        return "unknown"
    if team.get("global_team_id"):
        return f"id:{team['global_team_id']}"
    return f"name:{str(team.get('name') or '').strip().casefold()}"


def _match_side(match, side, entrant_map):
    slot = match.get("entrantA") if side == "a" else match.get("entrantB")
    slot = slot if isinstance(slot, dict) else {}
    entrant_id = _safe_int(slot.get("entrantId"))
    entrant = entrant_map.get(entrant_id)
    team = _team_from_entrant(entrant) if entrant else None
    name = team.get("name") if team else ("BYE" if entrant_id == 1 else "TBD")
    return {
        "entrant_id": entrant_id,
        "name": name,
        "abbr": team.get("abbr") if team else _abbr(name),
        "logo_url": team.get("logo_url") if team else "",
        "score": slot.get("score"),
        "seed": team.get("seed") if team else None,
        "team_key": _team_key(team) if team else "",
    }


def _is_real_match(match):
    a = _safe_int(((match.get("entrantA") or {}).get("entrantId")))
    b = _safe_int(((match.get("entrantB") or {}).get("entrantId")))
    return a > 1 and b > 1


def _bounty_middle_star_tag(report):
    """Return the player tag for Matcherino's reported Bounty middle star.

    The public Brawl Stars match-stat payload used here does not include a
    generic MVP/Star Player field. The concrete star-related player flag it
    does expose is `statistics.bountyPickedMiddleStar`.
    """
    props = report.get("properties") if isinstance(report.get("properties"), dict) else {}
    teams = props.get("teams") if isinstance(props.get("teams"), list) else []
    for team in teams[:2]:
        players = team.get("players") if isinstance(team, dict) else []
        if not isinstance(players, list):
            continue
        for player in players:
            if not isinstance(player, dict):
                continue
            stats = player.get("statistics") if isinstance(player.get("statistics"), dict) else {}
            if stats.get("bountyPickedMiddleStar") is True and player.get("tag"):
                return str(player.get("tag")).strip()
    return None


def _star_player_tag(report):
    """Extract the star-related player tag from a game report.

    Matcherino currently exposes the Bounty middle-star pickup, not a generic
    Brawl Stars MVP. Keep the explicit-field checks for API compatibility, then
    use the real per-player flag present in today's response.
    """
    if not isinstance(report, dict):
        return None
    props = report.get("properties") if isinstance(report.get("properties"), dict) else {}
    for candidate in (
        report.get("starPlayer"),
        report.get("star_player"),
        report.get("starPlayerTag"),
        props.get("starPlayer"),
        props.get("star_player"),
    ):
        if isinstance(candidate, dict):
            tag = candidate.get("tag") or candidate.get("playerTag")
            if tag:
                return str(tag).strip()
        elif isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return _bounty_middle_star_tag(report)


def _played_status(status):
    return str(status or "").lower() in {"done", "in-progress", "active", "live", "started"}


_FINISHED_STATUSES = {"closed", "finished", "completed", "done"}


def _cache_path(url, params):
    endpoint = url.rstrip("/").rsplit("/", 1)[-1] or "endpoint"
    parts = "_".join(f"{k}-{v}" for k, v in sorted((params or {}).items()))
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", parts)[:96]
    return MATCHERINO_CACHE_DIR / f"{endpoint}__{safe}.json"


def _payload_is_finished(data):
    """True if the response represents a closed/finished Matcherino event
    whose data won't change anymore."""
    if not isinstance(data, dict):
        return False
    body = data.get("body", data)
    candidates = []
    if isinstance(body, dict):
        candidates.append(body)
    if isinstance(body, list):
        candidates.extend(x for x in body if isinstance(x, dict))
    for obj in candidates:
        status = str(obj.get("status") or obj.get("state") or "").lower()
        if status in _FINISHED_STATUSES:
            return True
    # Match stats: every individual match has its own status.
    # If we have matches and they're all done, the payload is immutable.
    if isinstance(body, dict):
        matches = body.get("matches")
        if isinstance(matches, list) and matches:
            if all(str(m.get("status") or "").lower() in _FINISHED_STATUSES
                   for m in matches if isinstance(m, dict)):
                return True
    # Bracket payload: if the Final round has a played winner, the tournament
    # is over even when Matcherino still reports it as active.
    for obj in candidates:
        matches = obj.get("matches")
        if not isinstance(matches, list) or not matches:
            continue
        max_round = 0
        for m in matches:
            if isinstance(m, dict):
                rn = m.get("roundNum") or 0
                if isinstance(rn, (int, float)) and rn > max_round:
                    max_round = int(rn)
        if max_round <= 0:
            continue
        for m in matches:
            if not isinstance(m, dict):
                continue
            if (m.get("roundNum") == max_round
                    and m.get("winner")
                    and str(m.get("status") or "").lower() in _FINISHED_STATUSES):
                return True
    return False


def _disk_cache_load(path, max_age):
    try:
        if not path.exists():
            return None
        if max_age is not None and (time.time() - path.stat().st_mtime) > max_age:
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _disk_cache_save(path, data):
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        tmp.replace(path)
    except Exception:
        pass


def _fetch_json(url, params):
    path = _cache_path(url, params)

    # Finished events: trust disk cache forever (until file removed manually).
    cached = _disk_cache_load(path, max_age=None)
    if cached is not None and _payload_is_finished(cached):
        return cached

    # Fresh in-progress data: short TTL on disk.
    fresh = _disk_cache_load(path, max_age=MATCHERINO_FRESH_TTL)
    if fresh is not None:
        return fresh

    # Cache miss / expired: hit the network.
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    _disk_cache_save(path, data)
    return data


_TAG_CLEAN_RE = re.compile(r"[^A-Z0-9]")


def _normalize_tag(tag):
    if not tag:
        return ""
    return _TAG_CLEAN_RE.sub("", str(tag).upper())


def _avatar_cache_path(clean_tag):
    return AVATAR_CACHE_DIR / f"{clean_tag}.json"


def _fetch_player_avatar(tag):
    """Return the brawl-stars profile_avatar id for a player tag, or None.

    Uses a disk cache (JSON per tag). Successful hits are cached for 30 days;
    null/error results are cached for only 6 hours so a single transient
    upstream failure doesn't lock the avatar out for a week.

    Safe to call from threads.
    """
    clean = _normalize_tag(tag)
    if not clean:
        return None
    path = _avatar_cache_path(clean)

    # Successful hits live for AVATAR_CACHE_TTL.
    cached = _disk_cache_load(path, max_age=AVATAR_CACHE_TTL)
    if cached is not None and cached.get("avatar_id"):
        return cached.get("avatar_id")

    # Null results live for AVATAR_NEG_CACHE_TTL only.
    cached_neg = _disk_cache_load(path, max_age=AVATAR_NEG_CACHE_TTL)
    if cached_neg is not None and not cached_neg.get("avatar_id"):
        return None

    # Cache miss / expired. Try the upstream API with backoff retries.
    # Treats 429 / 5xx / timeout as transient; 4xx (other than 429) as final.
    avatar_id = None
    fatal = False
    delays = (0.6, 1.4, 3.0)
    for attempt, delay in enumerate((0,) + delays):
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(AVATAR_API_URL, params={"tag": clean},
                             timeout=AVATAR_FETCH_TIMEOUT)
            code = r.status_code
            if code == 200:
                result = (r.json() or {}).get("result") or {}
                avatar_id = result.get("profile_avatar")
                break
            if 400 <= code < 500 and code != 429:
                fatal = True  # tag genuinely unknown / malformed
                break
            # 429 / 5xx -> retry
        except requests.RequestException:
            pass  # network blip -> retry

    # Only cache fatal "no such tag" results. Transient failures get no entry
    # so the next request retries instead of waiting 6h.
    if avatar_id or fatal:
        _disk_cache_save(path, {"avatar_id": avatar_id, "ts": time.time()})
    return avatar_id


def _avatar_url_for(avatar_id):
    if not avatar_id:
        return ""
    icon = BASE_DIR / "assets" / "icons" / f"{avatar_id}.png"
    if icon.is_file():
        return f"/assets/icons/{avatar_id}.png"
    return ""


def _enrich_with_avatars(players):
    """Mutate each player dict in-place, adding avatar_id and avatar_url.

    Runs lookups in parallel with a hard batch timeout so a slow upstream
    can't stall the whole response.
    """
    if not players:
        return
    tags = {p.get("tag") for p in players if p.get("tag")}
    tags = {t for t in (_normalize_tag(t) for t in tags) if t}
    if not tags:
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}
    with ThreadPoolExecutor(max_workers=AVATAR_BATCH_WORKERS) as ex:
        futures = {ex.submit(_fetch_player_avatar, t): t for t in tags}
        try:
            for fut in as_completed(futures, timeout=AVATAR_BATCH_TIMEOUT):
                tag = futures[fut]
                try:
                    results[tag] = fut.result()
                except Exception:
                    results[tag] = None
        except Exception:
            pass  # batch timeout: keep partial results

    for p in players:
        clean = _normalize_tag(p.get("tag"))
        avatar_id = results.get(clean)
        if avatar_id:
            url = _avatar_url_for(avatar_id)
            if url:
                p["avatar_id"] = avatar_id
                p["avatar_url"] = url


def _fetch_match_stats(bounty_id, match_ids):
    matches = []
    ids = [str(mid) for mid in match_ids if mid]
    for i in range(0, len(ids), 12):
        chunk = ",".join(ids[i:i + 12])
        if not chunk:
            continue
        try:
            raw = _fetch_json(MATCHERINO_MATCH_STATS_URL, {
                "bountyId": bounty_id,
                "matchIds": chunk,
            })
            body = raw.get("body", raw) if isinstance(raw, dict) else {}
            matches.extend(body.get("matches") or [])
        except Exception:
            continue
    return matches


def _add_player_total(players, tag, name, team, stats):
    tag = str(tag or "").strip()
    if not tag:
        return
    stats = stats if isinstance(stats, dict) else {}
    player = players.setdefault(tag, {
        "tag": tag,
        "ign": name or tag,
        "ico": _abbr(name or tag)[:2],
        "team": team.get("name") if team else "",
        "team_abbr": team.get("abbr") if team else "",
        "team_logo": team.get("logo_url") if team else "",
        "total_damage": 0,
        "kills": 0,
        "deaths": 0,
        "games": 0,
        "star_player": 0,
        "matches": set(),
        "qualifiers": set(),
    })
    if name and (not player["ign"] or player["ign"] == tag):
        player["ign"] = name
        player["ico"] = _abbr(name)[:2]
    if team and not player.get("team"):
        player["team"] = team.get("name") or ""
        player["team_abbr"] = team.get("abbr") or ""
        player["team_logo"] = team.get("logo_url") or ""
    player["total_damage"] += _safe_int(stats.get("damageDealt"))
    player["kills"] += _safe_int(stats.get("kills"))
    player["deaths"] += _safe_int(stats.get("deaths"))
    player["games"] += 1


def _finalize_player(player):
    deaths = player.get("deaths") or 0
    kills = player.get("kills") or 0
    games = player.get("games") or 0
    stars = player.get("star_player") or 0
    out = dict(player)
    out["matches"] = len(player.get("matches") or [])
    out["qualifiers"] = sorted(player.get("qualifiers") or [])
    out["kd"] = round(kills / deaths, 2) if deaths else (float(kills) if kills else 0)
    out["damage"] = out["total_damage"]
    out["elims"] = kills
    out["country"] = ""
    out["star_player"] = stars
    out["star_player_rate"] = round((stars / games) * 100, 1) if games else 0
    out["mvps"] = stars
    out["wr"] = 0
    return out


def _qualifier_data(force=False):
    now = time.time()
    if (not force and _QUAL_DATA_CACHE["data"] and
            now - _QUAL_DATA_CACHE["ts"] < _QUAL_DATA_TTL):
        return _QUAL_DATA_CACHE["data"]

    qualifiers = []
    team_totals = {}
    player_totals = {}
    all_matches = []

    for bounty_id in QUALIFIER_BOUNTY_IDS:
        try:
            bounty_raw = _fetch_json(MATCHERINO_BOUNTY_URL, {"id": bounty_id})
            bounty = _extract_bounty(bounty_raw)
        except Exception:
            bounty = {}

        title = bounty.get("title") or f"Slice League Qualifier #{_qualifier_number_from_bounty(bounty_id)}"
        qualifier = {
            "id": bounty_id,
            "number": _qualifier_number_from_bounty(bounty_id, title),
            "title": title,
            "status": bounty.get("status") or "",
            "startAt": bounty.get("startAt"),
            "spots": bounty.get("teamLimit") or 128,
            "format": "BO3",
            "caster": "YF7",
            "link": f"https://matcherino.com/tournaments/{bounty_id}",
            "entrants": 0,
            "bracket": None,
            "image": _find_matcherino_image(bounty),
            "description": (bounty.get("shortDescription")
                            or bounty.get("description") or "").strip(),
        }

        try:
            bracket_raw = _fetch_json(MATCHERINO_BRACKETS_URL, {
                "bountyId": bounty_id,
                "id": 0,
                "isAdmin": "false",
            })
            brackets = bracket_raw.get("body", bracket_raw) if isinstance(bracket_raw, dict) else []
            bracket = brackets[0] if brackets else {}
        except Exception:
            bracket = {}

        entrants = bracket.get("entrants") or []
        entrant_map = {e.get("id"): e for e in entrants if isinstance(e, dict)}
        qualifier["entrants"] = len([e for e in entrants if isinstance(e, dict) and e.get("id", 0) > 1])

        for entrant in entrants:
            team = _team_from_entrant(entrant)
            if not team:
                continue
            key = _team_key(team)
            total = team_totals.setdefault(key, {
                "id": key,
                "name": team["name"],
                "abbr": team["abbr"],
                "logo_url": team["logo_url"],
                "qualifiers": set(),
                "placements": [],
                "players": {},
                "match_wins": 0,
                "match_losses": 0,
                "game_wins": 0,
                "game_losses": 0,
                "total_damage": 0,
                "kills": 0,
                "deaths": 0,
            })
            total["qualifiers"].add(qualifier["number"])
            if team.get("placement"):
                total["placements"].append({
                    "qualifier": qualifier["number"],
                    "placement": team.get("placement"),
                    "countPlace": team.get("countPlace"),
                })
            if team.get("logo_url") and not total.get("logo_url"):
                total["logo_url"] = team["logo_url"]
            for member in team.get("members") or []:
                total["players"].setdefault(member["name"], {
                    "ign": member["name"],
                    "ico": _abbr(member["name"])[:2],
                    "total_damage": 0,
                    "kills": 0,
                    "deaths": 0,
                    "kd": 0,
                })

        bracket_matches = bracket.get("matches") or []
        stat_match_ids = [
            m.get("id") for m in bracket_matches
            if _is_real_match(m) and _played_status(m.get("status"))
        ]
        stats_by_id = {m.get("id"): m for m in _fetch_match_stats(bounty_id, stat_match_ids)}

        grouped_rounds = {}
        for match in bracket_matches:
            if not isinstance(match, dict):
                continue
            mid = match.get("id")
            round_num = _safe_int(match.get("roundNum"))
            total_rounds = _safe_int(match.get("totalRounds") or len(bracket.get("rounds") or []))
            a = _match_side(match, "a", entrant_map)
            b = _match_side(match, "b", entrant_map)
            status = match.get("status") or ""
            winner = _safe_int(match.get("winner"))
            winner_side = "a" if winner and winner == a["entrant_id"] else ("b" if winner and winner == b["entrant_id"] else "")
            stat_match = stats_by_id.get(mid)
            stat_reports = []
            if isinstance(stat_match, dict):
                stat_reports = [
                    r for r in (stat_match.get("reports") or [])
                    if ((r.get("properties") or {}).get("teams"))
                ]

            if _is_real_match(match):
                all_matches.append({
                    "id": mid,
                    "qualifier": qualifier["number"],
                    "round": _round_label(round_num, total_rounds),
                    "status": status,
                    "teamA": a,
                    "teamB": b,
                    "scoreA": a["score"],
                    "scoreB": b["score"],
                    "winnerSide": winner_side,
                })
                if status == "done":
                    for side, team_side in (("a", a), ("b", b)):
                        team_total = team_totals.get(team_side.get("team_key"))
                        if not team_total:
                            continue
                        if winner_side == side:
                            team_total["match_wins"] += 1
                        elif winner_side:
                            team_total["match_losses"] += 1

            if stat_reports:
                side_teams = [
                    team_totals.get(a.get("team_key")),
                    team_totals.get(b.get("team_key")),
                ]
                name_lookup = {}
                for value in (stat_match.get("populateBrawlerNames") or {}).values():
                    if isinstance(value, dict) and value.get("playerTag"):
                        name_lookup[value["playerTag"]] = value.get("name") or value["playerTag"]
                for report in stat_reports:
                    winner_id = _safe_int(report.get("winner"))
                    if winner_id == a["entrant_id"]:
                        if side_teams[0]: side_teams[0]["game_wins"] += 1
                        if side_teams[1]: side_teams[1]["game_losses"] += 1
                    elif winner_id == b["entrant_id"]:
                        if side_teams[1]: side_teams[1]["game_wins"] += 1
                        if side_teams[0]: side_teams[0]["game_losses"] += 1
                    star_tag = _star_player_tag(report)
                    for index, game_team in enumerate(((report.get("properties") or {}).get("teams") or [])[:2]):
                        team_total = side_teams[index] if index < len(side_teams) else None
                        for player in game_team.get("players") or []:
                            tag = player.get("tag")
                            name = name_lookup.get(tag) or tag
                            stats = player.get("statistics") or {}
                            _add_player_total(player_totals, tag, name, team_total, stats)
                            player_totals[tag]["matches"].add(mid)
                            player_totals[tag]["qualifiers"].add(qualifier["number"])
                            if team_total:
                                team_total["total_damage"] += _safe_int(stats.get("damageDealt"))
                                team_total["kills"] += _safe_int(stats.get("kills"))
                                team_total["deaths"] += _safe_int(stats.get("deaths"))
                                roster_player = team_total["players"].setdefault(name, {
                                    "tag": tag,
                                    "ign": name,
                                    "ico": _abbr(name)[:2],
                                    "total_damage": 0,
                                    "kills": 0,
                                    "deaths": 0,
                                    "kd": 0,
                                })
                                if tag and not roster_player.get("tag"):
                                    roster_player["tag"] = tag
                                roster_player["total_damage"] += _safe_int(stats.get("damageDealt"))
                                roster_player["kills"] += _safe_int(stats.get("kills"))
                                roster_player["deaths"] += _safe_int(stats.get("deaths"))
                    if star_tag and star_tag in player_totals:
                        player_totals[star_tag]["star_player"] += 1

            match_payload = {
                "id": mid,
                "roundNum": round_num,
                "roundLabel": _round_label(round_num, total_rounds),
                "status": status,
                "teamA": a,
                "teamB": b,
                "scoreA": a["score"],
                "scoreB": b["score"],
                "winnerSide": winner_side,
                "hasStats": bool(stat_reports),
            }
            grouped_rounds.setdefault(round_num, []).append(match_payload)

        rounds = []
        total_rounds = _safe_int(len(bracket.get("rounds") or []))
        for round_num in sorted(grouped_rounds):
            matches = grouped_rounds[round_num]
            visible = [
                m for m in matches
                if (m["teamA"]["entrant_id"] > 1 or m["teamB"]["entrant_id"] > 1)
            ]
            rounds.append({
                "number": round_num,
                "label": _round_label(round_num, total_rounds),
                "matches": visible,
            })

        if bracket:
            qualifier["bracket"] = {
                "id": bracket.get("id"),
                "status": bracket.get("status") or "",
                "kind": bracket.get("kind") or "",
                "published": bool(bracket.get("published")),
                "startAt": bracket.get("startAt"),
                "totalRounds": total_rounds,
                "rounds": rounds,
            }

        # Matcherino sometimes keeps reporting "active" days after a final has
        # been played. If we can see a winner of the Final, treat the whole
        # qualifier as finished so caching + UI both reflect reality.
        if rounds:
            final_round = rounds[-1]
            for match in final_round.get("matches") or []:
                if (match.get("status") in ("done", "completed", "finished")
                        and match.get("winnerSide")):
                    qualifier["status"] = "finished"
                    if qualifier.get("bracket"):
                        qualifier["bracket"]["status"] = "finished"
                    break

        qualifiers.append(qualifier)

    players = sorted(
        (_finalize_player(p) for p in player_totals.values()),
        key=lambda p: (p["total_damage"], p["kd"], p["games"]),
        reverse=True,
    )
    for rank, player in enumerate(players, 1):
        player["rank"] = rank

    teams = []
    all_roster_players = []
    for total in team_totals.values():
        roster = []
        for player in total["players"].values():
            deaths = player.get("deaths") or 0
            kills = player.get("kills") or 0
            player["kd"] = round(kills / deaths, 2) if deaths else (float(kills) if kills else 0)
            roster.append(player)
        roster.sort(key=lambda p: (p.get("total_damage") or 0, p.get("kd") or 0), reverse=True)
        all_roster_players.extend(roster)
        wins = total["match_wins"]
        losses = total["match_losses"]
        games = wins + losses
        teams.append({
            "id": total["id"],
            "name": total["name"],
            "abbr": total["abbr"],
            "logo_url": total["logo_url"],
            "wins": wins,
            "losses": losses,
            "points": wins * 3,
            "record": _score_text(wins, losses),
            "wr": round((wins / games) * 100, 1) if games else 0,
            "game_wins": total["game_wins"],
            "game_losses": total["game_losses"],
            "total_damage": total["total_damage"],
            "kills": total["kills"],
            "deaths": total["deaths"],
            "kd": round(total["kills"] / total["deaths"], 2) if total["deaths"] else (float(total["kills"]) if total["kills"] else 0),
            "qualifiers": sorted(total["qualifiers"]),
            "placements": sorted(total["placements"], key=lambda x: (x["placement"], x["qualifier"])),
            "players": roster,
            "status": "active" if games else "registered",
        })
    teams.sort(key=lambda t: (t["wins"], -t["losses"], t["total_damage"]), reverse=True)
    for rank, team in enumerate(teams, 1):
        team["rank"] = rank

    # Enrich all player + roster dicts with brawl-stars profile avatars.
    # Single pass over the combined set so each tag is fetched at most once.
    try:
        _enrich_with_avatars(players + all_roster_players)
    except Exception:
        pass  # never let avatar enrichment break the response

    data = {
        "schemaVersion": _QUAL_DATA_SCHEMA_VERSION,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "matcherino",
        "qualifiers": sorted(qualifiers, key=lambda q: q["number"]),
        "teams": teams,
        "players": players,
        "matches": sorted(all_matches, key=lambda m: (m["qualifier"], m["id"])),
    }
    _QUAL_DATA_CACHE["ts"] = now
    _QUAL_DATA_CACHE["data"] = data
    _save_qualifier_data_disk(data)
    return data


def _save_qualifier_data_disk(data):
    """Persist the assembled qualifier payload so server restarts are instant."""
    try:
        _QUAL_DATA_DISK.parent.mkdir(parents=True, exist_ok=True)
        tmp = _QUAL_DATA_DISK.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), default=str)
        tmp.replace(_QUAL_DATA_DISK)
    except Exception as e:
        print(f"[quals] failed to persist data: {e}")


def _load_qualifier_data_disk():
    try:
        if not _QUAL_DATA_DISK.exists():
            return None
        with _QUAL_DATA_DISK.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schemaVersion") != _QUAL_DATA_SCHEMA_VERSION:
            return None
        return data
    except Exception as e:
        print(f"[quals] failed to load persisted data: {e}")
        return None


def _refresh_qualifier_data_loop():
    """Background thread: refresh qualifier data every 10 minutes.

    Finished qualifiers are served from the per-bounty disk cache (see
    `_payload_is_finished`), so this is cheap once the season is over.
    Only in-progress qualifiers actually hit Matcherino.
    """
    while True:
        time.sleep(_QUAL_REFRESH_INTERVAL)
        try:
            with _qual_data_lock:
                _qualifier_data(force=True)
            print("[quals] background refresh complete")
        except Exception as e:
            print(f"[quals] background refresh failed: {e}")


def _bootstrap_qualifier_data():
    """Hydrate the in-memory cache from disk on startup (instant first response)
    and kick off a background refresh + the periodic refresher thread."""
    cached = _load_qualifier_data_disk()
    if cached is not None:
        _QUAL_DATA_CACHE["ts"] = time.time()
        _QUAL_DATA_CACHE["data"] = cached
        print(f"[quals] loaded persisted snapshot "
              f"({len(cached.get('qualifiers') or [])} qualifiers, "
              f"{len(cached.get('players') or [])} players)")

    def _initial_refresh():
        try:
            with _qual_data_lock:
                _qualifier_data(force=True)
            print("[quals] initial refresh complete")
        except Exception as e:
            print(f"[quals] initial refresh failed: {e}")

    # Kick off the first refresh in the background so the server boots fast.
    threading.Thread(target=_initial_refresh, daemon=True).start()
    threading.Thread(target=_refresh_qualifier_data_loop, daemon=True).start()


_bootstrap_qualifier_data()


@app.get("/api/qualifiers")
def api_qualifiers():
    try:
        return jsonify(_qualifier_data()["qualifiers"])
    except Exception:
        now = time.time()
        if _QUAL_CACHE["data"] and now - _QUAL_CACHE["ts"] < _QUAL_TTL:
            return jsonify(_QUAL_CACHE["data"])

        out = []
        for bid in QUALIFIER_BOUNTY_IDS:
            try:
                r = requests.get(MATCHERINO_BOUNTY_URL,
                                 params={"id": bid}, timeout=10)
                r.raise_for_status()
                b = _extract_bounty(r.json())
            except Exception:
                b = {}
            title = b.get("title") or ""
            out.append({
                "id":      bid,
                "number":  _qualifier_number_from_bounty(bid, title),
                "title":   title or f"Qualifier #{_qualifier_number_from_bounty(bid)}",
                "status":  b.get("status") or "",
                "startAt": b.get("startAt"),
                "spots":   b.get("teamLimit") or 128,
                "format":  "BO3",
                "caster":  "YF7",
                "image":   _find_matcherino_image(b),
                "description": (b.get("shortDescription") or b.get("description") or "").strip(),
                "link":    f"https://matcherino.com/tournaments/{bid}",
            })

        _QUAL_CACHE["ts"]   = now
        _QUAL_CACHE["data"] = out
        return jsonify(out)


@app.get("/api/season-data")
def api_season_data():
    return jsonify(_qualifier_data(force=request.args.get("refresh") == "1"))


@app.get("/api/standings")
def api_standings():
    try:
        data = _qualifier_data()
        return jsonify(data["teams"])
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/players")
def api_players():
    try:
        data = _qualifier_data()
        return jsonify(data["players"])
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/teams")
def api_teams():
    try:
        data = _qualifier_data()
        return jsonify(data["teams"])
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/teams/<int:tid>")
def api_team(tid):
    db = get_db()
    team = row_to_dict(db.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone())
    if not team:
        abort(404)
    team["players"] = rows_to_list(db.execute(
        "SELECT * FROM players WHERE team_id=? ORDER BY elims DESC", (tid,)).fetchall())
    return jsonify(team)


@app.get("/api/bracket")
def api_bracket():
    try:
        data = _qualifier_data()
        return jsonify({"qualifiers": data["qualifiers"], "matches": data["matches"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/schedule")
def api_schedule():
    try:
        data = _qualifier_data()
        if data["matches"]:
            schedule = []
            for match in data["matches"]:
                a = match.get("teamA") or {}
                b = match.get("teamB") or {}
                status = str(match.get("status") or "")
                legacy_status = "completed" if status.lower() == "done" else (
                    "upcoming" if status.lower() == "waiting" else status
                )
                schedule.append({
                    **match,
                    "phase": f"Qualifier {match.get('qualifier')}",
                    "round_num": match.get("round"),
                    "match_date": match.get("startAt") or match.get("startTime") or None,
                    "team1_name": a.get("name"),
                    "team1_abbr": a.get("abbr"),
                    "team1_logo": a.get("logo_url") or "",
                    "team1_color": "#8B5CF6",
                    "team2_name": b.get("name"),
                    "team2_abbr": b.get("abbr"),
                    "team2_logo": b.get("logo_url") or "",
                    "team2_color": "#A3E635",
                    "score1": a.get("score"),
                    "score2": b.get("score"),
                    "status": legacy_status,
                    "stream_url": "",
                    "notes": "Matcherino live bracket data",
                })
            return jsonify(schedule)
        return jsonify([])
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/announcements")
def api_announcements():
    db = get_db()
    rows = rows_to_list(db.execute(
        "SELECT * FROM announcements WHERE active=1 ORDER BY created_at DESC"
    ).fetchall())
    return jsonify(rows)


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Slice League running on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
