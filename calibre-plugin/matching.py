#!/usr/bin/env python
"""Match Kindle books (engine records) to calibre library books, and find the
library's copy of a book file for highlight text."""
import re

from calibre_plugins.kindle_reading.engine import device_key

ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")
TEXT_FORMATS = ("AZW3", "AZW", "MOBI")     # what highlights.assemble reads


def norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


class Matcher(object):
    """Built in the GUI thread (it reads calibre's device book lists), used
    in the worker thread. Order: calibre's own device matching (UUID /
    APP_ID / title+author, as on the Device tab), ASIN identifier, then title
    when it is unambiguous."""

    def __init__(self, db, booklists=(), match_title=True):
        self.db = db
        self.match_title = match_title
        all_ids = set(db.all_book_ids())
        self.by_device = {}
        for bl in booklists:
            for book in (bl or ()):
                bid = getattr(book, "application_id", None)
                lpath = getattr(book, "lpath", None)
                if lpath and getattr(book, "in_library", None) and bid in all_ids:
                    self.by_device[device_key(lpath)] = bid
        self.by_asin, self.by_title, self.authors_of = {}, {}, {}
        for bid in all_ids:
            ids = db.field_for("identifiers", bid) or {}
            for k, v in ids.items():
                if k in ("amazon", "mobi-asin") or k.startswith("amazon_"):
                    self.by_asin[str(v).upper()] = bid
            self.by_title.setdefault(norm(db.field_for("title", bid) or ""), []).append(bid)
            names = set(db.field_for("authors", bid) or ())
            names.add(db.field_for("author_sort", bid) or "")
            self.authors_of[bid] = {norm(a) for a in names if a}

    def match(self, b):
        """(book id, method) or (None, "unmatched" / "ambiguous")."""
        key = b.get("device_key")
        if key and key in self.by_device:
            return self.by_device[key], "device"
        asin = str(b.get("asin") or "").upper()
        if asin and asin in self.by_asin:
            return self.by_asin[asin], "asin"
        if self.match_title and b.get("norm_title") in self.by_title:
            cands = self.by_title[b["norm_title"]]
            if len(cands) > 1 and b.get("author"):
                # same title more than once: the author has to decide
                cands = [c for c in cands if norm(b["author"]) in self.authors_of[c]]
            if len(cands) == 1:
                return cands[0], "title"
            return None, "ambiguous"
        return None, "unmatched"

    def book_file(self, bid):
        """Path of the library's AZW3 / AZW / MOBI for this book, or None."""
        for fmt in TEXT_FORMATS:
            try:
                p = self.db.format_abspath(bid, fmt)
            except Exception:  # noqa
                p = None
            if p:
                return p
        return None


def add_highlights(books, matcher, want_html=True, write_dat=False, log=None):
    """For every matched book with highlights: assemble the library copy's text
    (calibre's MOBI reader) and add b["highlights"] (HTML, for a comments
    column) and/or save it as the dashboard's assembled_text.dat. Books are
    matched here too (b["bid"], b["method"]). Returns how many got text."""
    from calibre_plugins.kindle_reading import highlights as hl
    n = 0
    for b in books:
        if "bid" not in b:
            b["bid"], b["method"] = matcher.match(b)
        spans = [a for a in b.get("annots_raw") or () if a.get("type") in hl.SPAN_TYPES]
        if not (spans and b["bid"] is not None):
            continue
        path = matcher.book_file(b["bid"])
        if not path:
            continue
        try:
            text = hl.assemble(path)
        except Exception as e:  # noqa  (DRM, KFX, damaged file)
            if log:
                log("highlights: %s: %r" % (path, e))
            continue
        if write_dat:
            hl.write_dat(b.get("sdr_dir"), text)
        if want_html:
            b["highlights"] = hl.to_html(hl.highlights(spans, text))
        n += 1
    return n
