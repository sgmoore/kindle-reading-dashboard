#!/usr/bin/env python
"""
Highlight / note text for Kindle books, using calibre's own MOBI/KF8 reader.

The Kindle stores highlights as byte offsets into the book's assembled text
(KindleUnpack's assembled_text.dat). calibre's Mobi8Reader builds the same
parts: joined, they are byte-identical to KindleUnpack's file (checked on a
real book), so no KindleUnpack is needed inside calibre.

The text slicing mirrors azw3text/extract_highlights.py (highlight_text /
_snap / _clean_span); keep the two in step.
"""
import html
import os
import re

SPAN_TYPES = {"highlight", "note", "underline"}
DAT_NAME = "assembled_text.dat"     # what the dashboard's load_assembled reads
_MAX_SNAP = 40
_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"\s+")


def assemble(path):
    """Assembled text (bytes) of an AZW3 / MOBI / AZW file, or None."""
    from calibre.ebooks.mobi.reader.mobi6 import MobiReader
    from calibre.ebooks.mobi.reader.mobi8 import Mobi8Reader
    from calibre.utils.logging import default_log
    with open(path, "rb") as f:
        mr = MobiReader(f, default_log)
    mr.check_for_drm()
    if mr.kf8_type is None:                    # plain MOBI 6: offsets into the raw text
        mr.extract_text()
        return mr.mobi_html
    # the steps of Mobi8Reader.__call__ up to build_parts()
    offset = mr.kf8_boundary + 2 if mr.kf8_type == "joint" else 1
    m8 = Mobi8Reader(mr, default_log)
    m8.processed_records = mr.extract_text(offset=offset)
    m8.raw_ml = mr.mobi_html
    m8.kf8_sections = mr.sections[offset - 1:]
    m8.read_indices()
    m8.build_parts()
    return b"".join(p if isinstance(p, bytes) else p.encode("utf-8") for p in m8.parts)


def _clean_span(seg):
    text = seg.decode("utf-8", "replace")
    text = _TAG.sub(" ", text)
    gt, lt = text.find(">"), text.find("<")
    if gt != -1 and (lt == -1 or gt < lt):
        text = text[gt + 1:]
    lt = text.rfind("<")
    if lt != -1 and ">" not in text[lt:]:
        text = text[:lt]
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'").replace("&quot;", '"'))
    return _WS.sub(" ", text).strip()


def _snap(buf, start, end):
    ls, steps = start, 0
    while ls > 0 and not buf[ls - 1:ls].isspace() and steps < _MAX_SNAP:
        ls -= 1
        steps += 1
    if steps >= _MAX_SNAP:
        ls = start
    le, steps, n = end, 0, len(buf)
    while le < n and not buf[le:le + 1].isspace() and steps < _MAX_SNAP:
        le += 1
        steps += 1
    if steps >= _MAX_SNAP:
        le = end
    return ls, le


def highlight_text(buf, start, end):
    try:
        start, end = int(start), int(end)
    except (TypeError, ValueError):
        return ""
    if start > end:
        start, end = end, start
    n = len(buf)
    start, end = max(0, min(start, n)), max(0, min(end, n))
    ls, le = _snap(buf, start, end)
    return _clean_span(buf[ls:le])


def highlights(annots, text):
    """[{type, text, note, pos}] for the span annotations, in book order."""
    out = []
    for a in annots or ():
        if a.get("type") not in SPAN_TYPES:
            continue
        out.append({
            "type": a["type"],
            "text": highlight_text(text, a.get("startPosition"), a.get("endPosition"))
            if text else "",
            "note": a.get("note") or "",
            "pos": _int(a.get("startPosition")),
        })
    out.sort(key=lambda h: h["pos"])
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def to_html(items):
    """Highlights as HTML for a calibre comments column."""
    parts = []
    for h in items:
        if not (h["text"] or h["note"]):
            continue
        p = "<blockquote>%s</blockquote>" % html.escape(h["text"]) if h["text"] else ""
        if h["note"]:
            p += "<p><i>%s</i></p>" % html.escape(h["note"])
        parts.append(p)
    return "<hr/>".join(parts) or None


def write_dat(sdr_dir, text):
    """Save the assembled text where the dashboard looks for it."""
    if sdr_dir and text and os.path.isdir(sdr_dir):
        with open(os.path.join(sdr_dir, DAT_NAME), "wb") as f:
            f.write(text)
