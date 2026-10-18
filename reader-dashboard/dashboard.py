#!/usr/bin/env python3
"""
Kindle Reader Dashboard
=======================
Pulls the Kindle library DB (cc.db) + reading-metadata sidecars
(AZW3 .azw3f/.azw3r, KFX .yjf/.yjr, MOBI .mbs/.mbp1),
decodes them, and serves a local web UI:
  - a list of every book with basic metadata
  - a detail page per book with ALL metadata (cc.db + KRDS sidecars)

Usage:
    python dashboard.py serve              # SSH sync (default) + build + serve
    python dashboard.py serve --local D:/documents   # read from a USB/local folder
    python dashboard.py serve --no-sync    # reuse the existing cache, just build+serve
    python dashboard.py sync               # only pull files into cache/
    python dashboard.py build              # only (re)build web/library.json

    --ccdb PATH / --fmcache PATH   use a cc.db / fmcache.db copied somewhere else
                                   (read only; never overwritten by a sync)
    --cache DIR                    keep the synced files in DIR instead of ./cache

Without any cc.db the library is built from the sidecar files alone (title from
the .sdr folder name; read status from fmcache.db when available).

Connection: SSH defaults to the device in ksh.py (192.168.15.244, root/kindle).

If the environment variable KINDLE_NAME is set a CACHE subdirectory named KINDLE_NAME will be used. This allows for having more than one kindle reader.
"""
import argparse
import datetime
import glob
import json
import logging
import os
import posixpath
import re
import sqlite3
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

KINDLE_NAME = os.environ.get("KINDLE_NAME", "")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(HERE, "web")
# repo layout: ../reading-metadata/scripts/krds.py; bundled in the calibre
# plugin: krds.py next to this file
KRDS_DIR = HERE if os.path.isfile(os.path.join(HERE, "krds.py")) else \
    os.path.join(ROOT, "reading-metadata", "scripts")
REMOTE_DOCS = "/mnt/us/documents"
# cc.db / fmcache.db copied somewhere else (--ccdb / --fmcache): read only
CCDB_OVERRIDE = None
FMCACHE_OVERRIDE = None


def set_cache(path):
    """Where synced files live (cc.db, fmcache.db, documents/)."""
    global CACHE, DB_PATH, FMCACHE_PATH, DOCS_CACHE
    CACHE = path
    DB_PATH = os.path.join(CACHE, "cc.db")
    FMCACHE_PATH = os.path.join(CACHE, "fmcache.db")
    DOCS_CACHE = os.path.join(CACHE, "documents")


set_cache(os.path.join(HERE, "cache", KINDLE_NAME))

# KRDS sidecar extensions, by container format: AZW3, KFX, legacy MOBI.
# METRICS_* hold timer.model / book.info.store / page.history.store,
# READER_* hold annotations / font.prefs / apnx.key.
METRICS_EXTS = (".azw3f", ".yjf", ".mbs")
READER_EXTS = (".azw3r", ".yjr", ".mbp1")
SIDECAR_EXTS = METRICS_EXTS + READER_EXTS

sys.path.insert(0, KRDS_DIR)
import krds  # noqa: E402  (the patched parser)

_log = logging.getLogger("krds")
_log.setLevel(logging.ERROR)

# Optional: highlight text extraction (azw3text/). When KRD_ASSEMBLE_AZW3 is set
# and an unencrypted .azw3 is reachable, the sync step assembles the book text so
# highlights show their actual words. Degrades gracefully if the module or
# KindleUnpack is missing.
AZW3TEXT_DIR = os.path.join(HERE, "azw3text") if os.path.isdir(os.path.join(HERE, "azw3text")) \
    else os.path.join(ROOT, "azw3text")
ASSEMBLE_AZW3 = os.environ.get("KRD_ASSEMBLE_AZW3", "").lower() in ("1", "true", "yes", "on")
BOOK_EXTS = (".azw3", ".azw", ".mobi")
try:
    sys.path.insert(0, AZW3TEXT_DIR)
    import extract_highlights as _hl  # noqa: E402
    import assemble_azw3_text as _asm  # noqa: E402
except Exception:                      # module not present -> feature just off
    _hl = _asm = None


# --------------------------------------------------------------------------- #
#  SSH sync (single persistent connection, streams files via `cat`)
# --------------------------------------------------------------------------- #
def _ssh_connect():
    import paramiko
    sys.path.insert(0, ROOT)
    import ksh  # HOST / USER / PW live here
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # ksh.connect_kwargs() handles password AND public-key login (key-only
    # devices with password auth disabled). Falls back to legacy kwargs.
    if hasattr(ksh, "connect_kwargs"):
        c.connect(ksh.HOST, **ksh.connect_kwargs())
    else:
        c.connect(ksh.HOST, username=ksh.USER, password=ksh.PW,
                  timeout=15, look_for_keys=False, allow_agent=False)
    return c, ksh.HOST


def _shq(path):
    """Single-quote a path for the remote sh, escaping embedded single quotes."""
    return "'" + path.replace("'", "'\\''") + "'"


def _ssh_read(c, remote, retries=3):
    last = b""
    for _ in range(retries):
        _in, out, err = c.exec_command("cat %s" % _shq(remote))
        data = out.channel.makefile("rb").read()
        out.channel.recv_exit_status()
        if data:
            return data
        last = data
    return last


def sync_ssh():
    os.makedirs(CACHE, exist_ok=True)
    c, host = _ssh_connect()
    print("connected to %s" % host)
    # 1) the library DB
    db = _ssh_read(c, "/var/local/cc.db")
    with open(DB_PATH, "wb") as f:
        f.write(db)
    print("cc.db (%d bytes)" % len(db))
    # 1b) fast-metrics cache holds the modern (KPP) read-state marks + device
    #     reading sessions, which are NOT in cc.db. Best-effort.
    try:
        fm = _ssh_read(c, "/mnt/us/system/fmcache/fmcache.db")
        if fm:
            with open(FMCACHE_PATH, "wb") as f:
                f.write(fm)
            print("fmcache.db (%d bytes)" % len(fm))
    except Exception as e:
        print("fmcache.db skipped (%s)" % e)
    # 2) every sidecar under documents
    find_expr = " -o ".join("-name '*%s'" % ext for ext in SIDECAR_EXTS)
    _in, out, _e = c.exec_command(
        "find '%s' \\( %s \\)" % (REMOTE_DOCS, find_expr))
    files = [l for l in out.read().decode("utf-8", "replace").splitlines() if l.strip()]
    print("found %d sidecars" % len(files))
    for i, remote in enumerate(files, 1):
        rel = posixpath.relpath(remote, REMOTE_DOCS)
        local = os.path.join(DOCS_CACHE, *rel.split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        data = _ssh_read(c, remote)
        with open(local, "wb") as f:
            f.write(data)
        if i % 10 == 0 or i == len(files):
            print("  %d/%d" % (i, len(files)))

    # 3) optionally pull the .azw3 for annotated books and assemble their text
    if ASSEMBLE_AZW3:
        def _ssh_azw3(sdr_local):
            rel = os.path.relpath(sdr_local, DOCS_CACHE).replace(os.sep, "/")
            base = rel[:-4] if rel.endswith(".sdr") else rel
            for ext in BOOK_EXTS:
                data = _ssh_read(c, posixpath.join(REMOTE_DOCS, base + ext))
                if data:
                    return data
            return None
        assemble_pass(_ssh_azw3)

    c.close()
    print("sync complete -> %s" % CACHE)


def sync_local(docs):
    """Mirror cc.db + sidecars from a locally-mounted Kindle (e.g. D:/documents)."""
    import shutil
    os.makedirs(DOCS_CACHE, exist_ok=True)
    # cc.db: try the mount root's /system or var? On USB mass-storage cc.db is NOT
    # exposed, so allow a pre-pulled cache/cc.db to stand. Only sidecars come from USB.
    n = 0
    for pat in ["**/*" + ext for ext in SIDECAR_EXTS]:
        for src in glob.glob(os.path.join(docs, pat), recursive=True):
            rel = os.path.relpath(src, docs)
            dst = os.path.join(DOCS_CACHE, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    print("copied %d sidecars from %s" % (n, docs))

    # assemble text for annotated books straight from the local .azw3 files
    if ASSEMBLE_AZW3:
        def _local_azw3(sdr_local):
            rel = os.path.relpath(sdr_local, DOCS_CACHE)
            base = rel[:-4] if rel.endswith(".sdr") else rel
            for ext in BOOK_EXTS:
                src = os.path.join(docs, base + ext)
                if os.path.isfile(src):
                    with open(src, "rb") as f:
                        return f.read()
            return None
        assemble_pass(_local_azw3)

    # a few Kindles expose cc.db / fmcache.db on the USB partition too
    root = os.path.dirname(os.path.normpath(docs))
    for src, dst in ((os.path.join(root, "cc.db"), DB_PATH),
                     (os.path.join(root, "system", "fmcache", "fmcache.db"), FMCACHE_PATH)):
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            print("copied %s" % os.path.basename(src))

    if not (os.path.exists(DB_PATH) or CCDB_OVERRIDE):
        print("note: no cc.db (it is not on the USB partition of most Kindles); the "
              "library is built from the sidecars alone. For titles from the device "
              "library and read status, pass --ccdb with a copy of /var/local/cc.db.")


# --------------------------------------------------------------------------- #
#  Decode + build library.json
# --------------------------------------------------------------------------- #
def decode_sidecar(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return krds.KindleReaderDataStore(_log, data).deserialize()
    except Exception as e:
        return {"_error": "%s: %s" % (type(e).__name__, e)}


# --------------------------------------------------------------------------- #
#  Assembled book text -> highlight text (optional, azw3text/)
# --------------------------------------------------------------------------- #
def _assembled_path(sdr_local):
    return os.path.join(sdr_local, "assembled_text.dat")


def load_assembled(sdr_local):
    """Return cached assembled book text (bytes) for a .sdr dir, or None."""
    if not sdr_local:
        return None
    p = _assembled_path(sdr_local)
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            pass
    return None


def _iter_cached_sdr():
    if not os.path.isdir(DOCS_CACHE):
        return
    for dirpath, dirnames, _files in os.walk(DOCS_CACHE):
        for d in dirnames:
            if d.endswith(".sdr"):
                yield os.path.join(dirpath, d)


def _first_azw3r(sdr_local):
    for f in os.listdir(sdr_local):
        if f.endswith(READER_EXTS):
            return decode_sidecar(os.path.join(sdr_local, f))
    return None


def ensure_assembled(sdr_local, azw3r_obj, get_azw3_bytes):
    """Build + cache assembled_text.dat for one book if it has annotations.

    get_azw3_bytes() must return the raw .azw3 bytes (or None). Returns True if a
    new assembled_text.dat was written. No-op unless KRD_ASSEMBLE_AZW3 is set and
    the azw3text module imported.
    """
    if not (_asm and ASSEMBLE_AZW3):
        return False
    if os.path.exists(_assembled_path(sdr_local)):
        return False
    if not (_hl and _hl.has_text_annotations(azw3r_obj)):
        return False                     # only bookmarks (or none) -> don't fetch the book
    import tempfile
    tmp = None
    try:
        data = get_azw3_bytes()
    except Exception as e:
        print("  azw3 fetch failed: %s" % e)
        return False
    if not data:
        return False
    try:
        with tempfile.NamedTemporaryFile(suffix=".azw3", delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        assembled = _asm.assemble_azw3_text(tmp)
        with open(_assembled_path(sdr_local), "wb") as f:
            f.write(assembled)
        print("  assembled %s" % os.path.relpath(sdr_local, CACHE))
        return True
    except _asm.AssembleError as e:
        print("  assemble skipped (%s): %s" % (os.path.basename(sdr_local), e))
    except Exception as e:
        print("  assemble error (%s): %s" % (os.path.basename(sdr_local), e))
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return False


def assemble_pass(resolver):
    """Walk cached .sdr dirs; assemble text for annotated books missing it.

    resolver(sdr_local) -> raw azw3 bytes or None.
    """
    if not (ASSEMBLE_AZW3 and _asm):
        return
    if not _hl:
        print("KRD_ASSEMBLE_AZW3 set but azw3text/ not importable - skipping")
        return
    n = 0
    for sdr_local in _iter_cached_sdr():
        if os.path.exists(_assembled_path(sdr_local)):
            continue
        azw3r = _first_azw3r(sdr_local)
        if not _hl.has_text_annotations(azw3r):
            continue
        if ensure_assembled(sdr_local, azw3r, lambda s=sdr_local: resolver(s)):
            n += 1
    if n:
        print("assembled text for %d annotated book(s)" % n)


def _jload(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _author(row):
    cr = _jload(row.get("j_credits"), [])
    if cr and isinstance(cr, list):
        first = cr[0]
        nm = first.get("name") if isinstance(first, dict) else None
        if isinstance(nm, dict):
            return nm.get("display") or nm.get("collation") or ""
        if isinstance(nm, str):
            return nm
    return row.get("p_credits_0_name_collation") or ""


def _stats_from_timer(tm, bi):
    """Derive human numbers from timer.model + book.info.store."""
    out = {}
    if not isinstance(tm, dict):
        return out
    tt = tm.get("totalTime") or 0
    tw = tm.get("totalWords") or 0
    tp = tm.get("totalPercent") or 0.0
    mins = tt / 60000.0
    out["minutes"] = round(mins, 1)
    out["hours"] = round(mins / 60.0, 2)
    out["words_read"] = tw
    out["percent_read"] = round(tp * 100, 1)
    out["wpm_overall"] = round(tw / mins) if mins else 0
    # clean reading speed = words/min across the normal distributions
    ac = tm.get("averageCalculator") or {}
    dists = ac.get("normalDistributions") or []
    s = sum(d.get("sum", 0) for d in dists if isinstance(d, dict))
    n = sum(d.get("count", 0) for d in dists if isinstance(d, dict))
    out["wpm_reading"] = round(s / n) if n else out["wpm_overall"]
    out["speed_samples"] = n
    if isinstance(bi, dict):
        out["book_words"] = bi.get("numberOfWords", 0)
    return out


def _connect_ro(path):
    """Open a sqlite file read-only (a cc.db/fmcache.db the user copied is
    never modified)."""
    import pathlib
    return sqlite3.connect(pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True)


def load_fmcache():
    """Parse fmcache.db (fast-metrics) for modern read-state marks + device
    reading sessions. Returns {read_state:{asin:..}, sessions:{asin:[..]},
    aggregate:{read,manual_unread,unknown}}. Empty if file absent."""
    out = {"read_state": {}, "sessions": {}, "aggregate": None, "records": {}}
    path = FMCACHE_OVERRIDE or FMCACHE_PATH
    if not os.path.exists(path):
        return out
    try:
        con = _connect_ro(path)
        cur = con.cursor()
    except sqlite3.Error:
        return out
    # read-state marks (latest per asin wins, by created_timestamp)
    latest = {}
    try:
        for rec, ts in cur.execute(
                "SELECT record, created_timestamp FROM records "
                "WHERE schema_name='mar_content_readstate_update_success'"):
            d = json.loads(rec)
            asin = d.get("updated_asin")
            if asin and ts >= latest.get(asin, -1):
                latest[asin] = ts
                out["read_state"][asin] = d.get("read_state")  # READ / UNREAD
    except (sqlite3.Error, ValueError):
        pass
    # all raw records grouped by the book they reference (asin/cde_key/book_asin)
    try:
        for sch, rec, ts in cur.execute(
                "SELECT schema_name, record, created_timestamp FROM records"):
            try:
                d = json.loads(rec)
            except ValueError:
                continue
            asin = (d.get("updated_asin") or d.get("cde_key")
                    or d.get("book_asin") or d.get("asin"))
            if asin:
                out["records"].setdefault(asin, []).append(
                    {"schema": sch, "time": ts, "data": d})
    except sqlite3.Error:
        pass
    # library-wide aggregate (last one)
    try:
        rows = cur.execute("SELECT record FROM records WHERE "
                           "schema_name='mar_book_read_stats'").fetchall()
        if rows:
            out["aggregate"] = json.loads(rows[-1][0])
    except (sqlite3.Error, ValueError):
        pass
    # device reading sessions
    try:
        for (sess,) in cur.execute("SELECT session FROM reading_sessions"):
            p = json.loads(sess).get("payload", {})
            asin = p.get("asin")
            if not asin:
                continue
            st, en = p.get("start_timestamp"), p.get("end_timestamp")
            out["sessions"].setdefault(asin, []).append({
                "start": datetime.datetime.fromtimestamp(st / 1000).isoformat() if st else None,
                "end": datetime.datetime.fromtimestamp(en / 1000).isoformat() if en else None,
                "minutes": round((en - st) / 60000.0, 1) if (st and en) else None,
                "startLoc": p.get("start_reading_location"),
                "endLoc": p.get("end_reading_location"),
                "complete": p.get("is_complete"),
            })
    except (sqlite3.Error, ValueError):
        pass
    con.close()
    return out


def _sidecar_data(sdr_local):
    """Decoded sidecars + derived stats/annotations for one .sdr folder."""
    azw3f, azw3r = {}, {}
    if sdr_local and os.path.isdir(sdr_local):
        for f in os.listdir(sdr_local):
            p = os.path.join(sdr_local, f)
            if f.endswith(METRICS_EXTS):
                azw3f = decode_sidecar(p) or {}
            elif f.endswith(READER_EXTS):
                azw3r = decode_sidecar(p) or {}
    tm = azw3f.get("timer.model")
    stats = _stats_from_timer(tm, azw3f.get("book.info.store"))

    # annotations (+ the highlighted text itself, if the book was assembled)
    assembled = load_assembled(sdr_local)
    if _hl:
        annots = (_hl.extract_highlights(assembled, azw3r)
                  if assembled else _hl.annotations_from_azw3r(azw3r))
    else:
        annots = []
        aco = (azw3r.get("annotation.cache.object") or {})
        if isinstance(aco, dict):
            for cls, items in aco.items():
                if isinstance(items, list):
                    for it in items:
                        annots.append({"type": cls.split(".")[-1], **it})
    return {"azw3f": azw3f, "azw3r": azw3r, "stats": stats,
            "annotations": annots, "has_sidecar": bool(tm or azw3r)}


def progress_percent(azw3f, azw3r, cc_percent=None):
    """How far into the book (0-100). timer.model's totalPercent is NOT this:
    it is the share of the book read in total, so re-reading and paging back
    and forth push it past 100%. Order: cc.db p_percentFinished (the device's
    own value), else furthest page / total pages (apnx page map), else
    totalPercent capped at 100."""
    if cc_percent is not None:
        try:
            return round(min(max(float(cc_percent), 0.0), 100.0), 1)
        except (TypeError, ValueError):
            pass
    arr = ((azw3r or {}).get("apnx.key") or {}).get("oPNToPosition")
    pos = None
    for k in ("fpr", "lpr"):
        v = (azw3f or {}).get(k)
        if isinstance(v, dict) and v.get("position") is not None:
            try:
                pos = int(v["position"])
                break
            except (TypeError, ValueError):
                pass
    if isinstance(arr, list) and len(arr) > 1 and pos is not None:
        import bisect
        page = bisect.bisect_right(arr, pos) - 1
        return round(min(100.0, 100.0 * max(page, 0) / (len(arr) - 1)), 1)
    tp = ((azw3f or {}).get("timer.model") or {}).get("totalPercent")
    if isinstance(tp, (int, float)):
        return round(min(100.0, tp * 100), 1)
    return None


def _read_status(fm_rs, rstate):
    """Modern read-state (fmcache) overrides legacy cc.db p_readState."""
    if fm_rs == "READ" or rstate == 2:
        return "read"
    if fm_rs == "UNREAD":
        return "unread"
    if rstate == 1:
        return "reading"
    return "unread"


def _fm_fields(fm, key):
    fm_rs = fm["read_state"].get(key)            # 'READ' / 'UNREAD' / None
    sessions = sorted(fm["sessions"].get(key, []), key=lambda s: s["start"] or "")
    raw = {"read_state": fm_rs, "reading_sessions": sessions,
           "records": fm["records"].get(key, [])}
    return fm_rs, sessions, raw


def build():
    os.makedirs(WEB, exist_ok=True)
    fm = load_fmcache()
    ccdb = CCDB_OVERRIDE or DB_PATH
    if os.path.exists(ccdb):
        books, used_sdr = _books_from_ccdb(ccdb, fm)
        # books on the device that this cc.db does not know (e.g. an older copy)
        extra = _books_from_sidecars(fm, skip=used_sdr)
        if extra:
            print("%d book(s) not in cc.db, added from their sidecars" % len(extra))
        books += extra
        source = "cc.db"
    elif any(True for _ in _iter_cached_sdr()):
        print("no cc.db - building from the sidecar files only")
        books, source = _books_from_sidecars(fm), "sidecars"
    else:
        sys.exit("nothing to build: no cc.db and no sidecars in %s. Run 'sync' first." % CACHE)

    # sort: most-read first
    books.sort(key=lambda b: -(b["stats"].get("minutes") or 0))

    totals = {
        "books": len(books),
        "with_sidecar": sum(1 for b in books if b["has_sidecar"]),
        "hours": round(sum((b["stats"].get("minutes") or 0) for b in books) / 60, 1),
        "words": sum((b["stats"].get("words_read") or 0) for b in books),
        "annotations": sum(len(b["annotations"]) for b in books),
        "read": sum(1 for b in books if b["read_status"] == "read"),
        "reading": sum(1 for b in books if b["read_status"] == "reading"),
        "fm_aggregate": fm["aggregate"],   # account-wide {read, manual_unread, unknown}
        "source": source,                  # "cc.db" or "sidecars"
        "sync_mode": Handler.sync_mode[0],  # ssh / local / none (page's sync button)
    }
    tmin = sum((b["stats"].get("minutes") or 0) for b in books)
    twords = totals["words"]
    totals["avg_wpm"] = round(twords / tmin) if tmin else 0

    out = {"totals": totals, "books": books}
    with open(os.path.join(WEB, "library.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("built library.json from %s: %d books, %d with reading data, %.1f h, %d words"
          % (source, totals["books"], totals["with_sidecar"], totals["hours"], totals["words"]))


# store / Send-to-Kindle books: documents/Downloads/Items01/<title>_<ASIN or doc id>.sdr
_ITEMS_SUFFIX = re.compile(r"^(.+)_([A-Z0-9]{10}|[A-Z2-7]{32})$")


def _books_from_sidecars(fm, skip=()):
    """One book per cached .sdr folder with a metrics sidecar (minus `skip`).
    Title/author come from the folder name ("<title> - <author>.sdr"), the
    ASIN from book.info.store or the Items01 folder suffix. Read status: the
    fmcache mark if there is one, else reading/unread by progress."""
    books = []
    skip = {os.path.normcase(os.path.normpath(p)) for p in skip}
    for sdr_local in sorted(_iter_cached_sdr()):
        if os.path.normcase(os.path.normpath(sdr_local)) in skip:
            continue
        if not any(f.endswith(METRICS_EXTS) for f in os.listdir(sdr_local)):
            continue
        sc = _sidecar_data(sdr_local)
        folder = os.path.basename(sdr_local)[:-4]
        bi = sc["azw3f"].get("book.info.store")
        asin = None
        if isinstance(bi, dict):
            asin = next((str(bi[k]) for k in ("asin", "ASIN", "cdeKey", "cde_key")
                         if bi.get(k)), None)
        m = _ITEMS_SUFFIX.match(folder)
        if m and os.path.basename(os.path.dirname(sdr_local)).startswith("Items"):
            folder = m.group(1)
            asin = asin or m.group(2)
        title, author = folder, ""
        if " - " in folder:
            title, author = folder.rsplit(" - ", 1)
        rel = os.path.relpath(sdr_local, CACHE).replace(os.sep, "/")
        fm_rs, dev_sessions, fm_raw = _fm_fields(fm, asin)
        pct = progress_percent(sc["azw3f"], sc["azw3r"]) or 0
        books.append({
            "uuid": "sdr:" + rel,
            "title": title or "(ללא כותרת)",
            "author": author,
            "cdeKey": asin,
            "cdeType": None,
            "publisher": None,
            "language": None,
            "percentFinished": None,
            "progress": pct,
            "readState": None,
            "read_status": _read_status(fm_rs, 1 if pct > 0 else 0),
            "fm_read_state": fm_rs,
            "device_sessions": dev_sessions,
            "fmcache_raw": fm_raw,
            "lastAccess": None,
            "location": "/mnt/us/" + rel[:-len(".sdr")],
            "isArchived": None,
            "contentSize": None,
            "series": None,
            "stats": sc["stats"],
            "annotations": sc["annotations"],
            "has_sidecar": sc["has_sidecar"],
            "cc_raw": {},
            "azw3f": sc["azw3f"],
            "azw3r": sc["azw3r"],
        })
    return books


def _books_from_ccdb(path, fm):
    con = _connect_ro(path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # series membership: cdeKey -> {seriesId, position label}
    series_of = {}
    try:
        for r in cur.execute("SELECT d_itemCdeKey, d_seriesId, d_itemPositionLabel, "
                             "d_itemPosition FROM Series"):
            series_of[r[0]] = {"seriesId": r[1], "label": r[2], "pos": r[3]}
    except sqlite3.Error:
        pass
    # series container names: seriesId(asin/cdeGroup) -> title
    series_name = {}
    try:
        for r in cur.execute("SELECT p_cdeKey, p_cdeGroup, p_titles_0_nominal FROM "
                             "Entries WHERE p_type='Entry:Item:Series'"):
            for k in (r[0], r[1]):
                if k:
                    series_name[k] = r[2]
    except sqlite3.Error:
        pass

    cols = [d[1] for d in cur.execute("PRAGMA table_info(Entries)")]
    rows = cur.execute(
        "SELECT * FROM Entries WHERE p_type='Entry:Item'").fetchall()

    books, used_sdr = [], []
    for row in rows:
        r = {k: row[k] for k in cols}
        loc = r.get("p_location") or ""
        # locate sidecars: <loc minus ext>.sdr/  under the cache
        sdr_local = None
        if loc.startswith("/mnt/us/"):
            rel = loc[len("/mnt/us/"):]              # documents/.../book.azw3
            base, _ext = os.path.splitext(rel)
            sdr_local = os.path.join(CACHE, *(base + ".sdr").split("/"))
            used_sdr.append(sdr_local)
        sc = _sidecar_data(sdr_local)

        cde = r.get("p_cdeKey")
        ser = series_of.get(cde)
        fm_rs, dev_sessions, fm_raw = _fm_fields(fm, cde)
        book = {
            "uuid": r.get("p_uuid"),
            "title": r.get("p_titles_0_nominal") or "(ללא כותרת)",
            "author": _author(r),
            "cdeKey": cde,
            "cdeType": r.get("p_cdeType"),
            "publisher": r.get("p_publisher"),
            "language": r.get("p_languages_0"),
            "percentFinished": r.get("p_percentFinished"),
            "progress": progress_percent(sc["azw3f"], sc["azw3r"], r.get("p_percentFinished")),
            "readState": r.get("p_readState"),
            "read_status": _read_status(fm_rs, r.get("p_readState")),
            "fm_read_state": fm_rs,              # READ / UNREAD / None (modern mark)
            "device_sessions": dev_sessions,     # from fmcache reading_sessions
            "fmcache_raw": fm_raw,               # raw data from the new DB (fmcache)
            "lastAccess": r.get("p_lastAccess"),
            "location": loc,
            "isArchived": r.get("p_isArchived"),
            "contentSize": r.get("p_contentSize"),
            "series": {
                "label": ser["label"] if ser else None,
                "name": series_name.get(ser["seriesId"]) if ser else None,
                "seriesId": ser["seriesId"] if ser else None,
            } if ser else None,
            "stats": sc["stats"],
            "annotations": sc["annotations"],
            "has_sidecar": sc["has_sidecar"],
            # raw blobs for the detail page
            "cc_raw": {k: v for k, v in r.items() if v is not None},
            "azw3f": sc["azw3f"],
            "azw3r": sc["azw3r"],
        }
        books.append(book)
    con.close()
    return books, used_sdr


# --------------------------------------------------------------------------- #
#  Serve
# --------------------------------------------------------------------------- #
class Handler(SimpleHTTPRequestHandler):
    sync_mode = ("ssh", None)  # (mode, local_path)

    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB, **k)

    def log_message(self, *a):
        pass

    def end_headers(self):
        # never cache HTML/JS/JSON so edits + rebuilds show on a plain refresh
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_POST(self):
        if self.path.rstrip("/") == "/api/refresh":
            try:
                mode, local = Handler.sync_mode
                if mode == "ssh":
                    sync_ssh()
                elif mode == "local" and local:
                    sync_local(local)
                build()                 # mode "none": data is synced by the caller
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_error(404)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind the same port, so the
    # "already running" check below never fired and every launch left one
    # more server running. Ask for exclusive use there instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(port=8742):
    os.chdir(WEB)
    url = "http://127.0.0.1:%d/" % port
    try:
        httpd = _Server(("127.0.0.1", port), Handler)
    except OSError:
        # a dashboard is already running on this port (it serves the same web/
        # folder, so it already has the library.json just built): reuse it
        print("dashboard already running at %s" % url)
        webbrowser.open(url)
        return
    print("serving dashboard at %s  (Ctrl-C to stop)" % url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Kindle Reader Dashboard")
    ap.add_argument("cmd", choices=["serve", "sync", "build"], nargs="?", default="serve")
    ap.add_argument("--local", metavar="DOCS_DIR",
                    help="read sidecars from a local/USB documents folder instead of SSH")
    ap.add_argument("--no-sync", action="store_true", help="skip syncing, reuse cache")
    ap.add_argument("--port", type=int, default=8742)
    ap.add_argument("--cache", metavar="DIR", help="folder for synced files (default ./cache)")
    ap.add_argument("--ccdb", metavar="PATH",
                    help="use this cc.db (e.g. a copy) instead of the synced one; read only")
    ap.add_argument("--fmcache", metavar="PATH",
                    help="use this fmcache.db instead of the synced one; read only")
    args = ap.parse_args()

    global CCDB_OVERRIDE, FMCACHE_OVERRIDE
    if args.cache:
        set_cache(os.path.abspath(args.cache))
    for opt, path in (("--ccdb", args.ccdb), ("--fmcache", args.fmcache)):
        if path and not os.path.isfile(path):
            sys.exit("%s: no such file: %s" % (opt, path))
    CCDB_OVERRIDE = args.ccdb and os.path.abspath(args.ccdb)
    FMCACHE_OVERRIDE = args.fmcache and os.path.abspath(args.fmcache)

    if args.local:
        mode = ("local", args.local)
    elif args.no_sync:
        mode = ("none", None)            # the page's refresh button only rebuilds
    else:
        mode = ("ssh", None)
    Handler.sync_mode = mode

    if args.cmd == "sync":
        sync_local(args.local) if args.local else sync_ssh()
        return
    if args.cmd == "build":
        build()
        return
    # serve
    if not args.no_sync:
        try:
            sync_local(args.local) if args.local else sync_ssh()
        except Exception as e:
            print("sync failed (%s) - serving existing cache" % e)
    build()
    serve(args.port)


if __name__ == "__main__":
    main()
