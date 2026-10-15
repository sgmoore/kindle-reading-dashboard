#!/usr/bin/env python3
"""
Update the MobileRead "[GUI Plugin] Kindle Reading Dashboard" first post after
a new plugin release: swap the attached zip and refresh the Version History.

Runs in the "Calibre plugin release" workflow. Plain HTTP (requests), no
browser. Credentials come from the environment:

    MOBILEREAD_USER, MOBILEREAD_PASSWORD   (repository secrets in CI)

Locally:

    pip install requests
    MOBILEREAD_USER=... MOBILEREAD_PASSWORD=... python tools/mobileread_update.py --dry-run

How vBulletin 3.8 behaves (learned the hard way, 2026-10-15):
- In edit mode an uploaded file is only *pending* (tied to the edit form's
  posthash) until the edit is saved with "Save Changes". Without a save it is
  lost.
- Uploading a file identical to one already attached re-uses that attachment
  (it moves to the pending upload) instead of creating a new one; if that edit
  is then not saved, the attachment disappears from the post.

So the flow is:
  1. compare the new zip with the attached one; identical -> no upload
  2. edit #1: upload the new zip, save (post now has old + new), verify
  3. edit #2: remove the old zip(s), save, verify exactly one zip remains
The post never has zero zips (calibre's plugin updater downloads from it).
"""
import argparse
import hashlib
import html
import io
import os
import re
import sys
import tempfile
import time
import urllib.request
import zipfile
from html.parser import HTMLParser

import requests

FORUM = "https://www.mobileread.com/forums/"
DEFAULT_POST = 4616175          # first post of showthread.php?t=375732
ZIP_NAME = "kindle_reading_dashboard.zip"
LATEST_ZIP = ("https://github.com/zevisvei/kindle-reading-dashboard/releases/"
              "latest/download/" + ZIP_NAME)
CHARSET = "iso-8859-1"          # what the forum serves and expects
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# --------------------------------------------------------------------------- #
#  changelog -> Version History
# --------------------------------------------------------------------------- #
def changelog_sections(path):
    """[(version, [bullet, ...]), ...] newest first, from CHANGELOG.md."""
    text = open(path, encoding="utf-8").read()
    out = []
    for m in re.finditer(r"^## (\d+\.\d+\.\d+)\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M):
        bullets = [b.strip() for b in re.findall(r"^- (.+)$", m.group(2), re.M)]
        out.append((m.group(1), bullets))
    return out


def version_history_bbcode(sections):
    lines = ["%s - %s" % (v, " ".join(b)) for v, b in sections]
    return "Version History\n[SPOILER]\n" + "\n".join(lines) + "\n[/SPOILER]"


def zip_version(path):
    with zipfile.ZipFile(path) as z:
        src = z.read("__init__.py").decode("utf-8")
    m = re.search(r"version\s*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
    return ".".join(m.groups()) if m else None


def new_message(old, history):
    pat = re.compile(r"Version History\s*\[SPOILER\].*?\[/SPOILER\]", re.S | re.I)
    if pat.search(old):
        return pat.sub(lambda _m: history, old, count=1)
    return old.rstrip() + "\n\n" + history + "\n"


def make_test_variant(path):
    """Copy of the zip with a marker file, so its hash differs (tests only)."""
    out = os.path.join(tempfile.mkdtemp(prefix="mr_variant_"), ZIP_NAME)
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("TEST_VARIANT.txt", "test upload %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    return out


# --------------------------------------------------------------------------- #
#  vBulletin 3.8 plumbing
# --------------------------------------------------------------------------- #
def _utf16_units(s):
    """The string as JavaScript sees it: one char per UTF-16 code unit."""
    b = s.encode("utf-16-le", "surrogatepass")
    return "".join(chr(int.from_bytes(b[i:i + 2], "little")) for i in range(0, len(b), 2))


def _js_bytes(s):
    """What vBulletin's md5.js hashes: each UTF-16 code unit & 0xFF."""
    return bytes(ord(c) & 0xFF for c in s)


def _str_to_ent(s):
    return "".join(c if ord(c) <= 255 else "&#%d;" % ord(c) for c in s)


def form_encode(s):
    """Text field value as the browser submits it on an ISO-8859-1 page."""
    return _str_to_ent(s).encode(CHARSET)


class FormParser(HTMLParser):
    """Default values of one <form> (by name or action substring), like a
    browser would submit them: hidden/text inputs, checked boxes/radios,
    selected options, textareas. Also collects submit button names."""

    def __init__(self, match):
        super().__init__(convert_charrefs=True)
        self.match, self.inside, self.fields, self.submits = match, False, {}, {}
        self._select = self._textarea = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = self.match in (a.get("name") or "") or self.match in (a.get("action") or "")
            return
        if not self.inside:
            return
        name = a.get("name")
        if tag == "input" and name:
            t = (a.get("type") or "text").lower()
            if t == "submit":
                self.submits[name] = a.get("value", "")
            elif t in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields[name] = a.get("value", "on")
            elif t != "file":
                self.fields[name] = a.get("value", "")
        elif tag == "select" and name:
            self._select = name
        elif tag == "option" and self._select and "selected" in a:
            self.fields[self._select] = a.get("value", "")
        elif tag == "textarea" and name:
            self._textarea, self._text = name, []

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self._select = None
        elif tag == "textarea" and self._textarea:
            self.fields[self._textarea] = "".join(self._text)
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self._text.append(data)


def parse_form(page, match):
    p = FormParser(match)
    p.feed(page)
    return p.fields, p.submits


def page_errors(page):
    """vBulletin's error block, if the response is a rejected form. Thread
    pages carry the same heading in the hidden Quick Reply template
    (qr_error_tbody), which is not an error."""
    for m in re.finditer("The following errors", page):
        i = m.start()
        if "qr_error_tbody" in page[max(0, i - 300):i]:
            continue
        msg = re.sub(r"<[^>]+>", " ", page[i:i + 1500])
        return re.sub(r"\s+", " ", html.unescape(msg))[:400]
    return None


def summary(page):
    """One line about a response, for the log (no tokens, no message text)."""
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    title = re.sub(r"\s+", " ", html.unescape(title.group(1))).strip() if title else "?"
    err = page_errors(page)
    return "title=%r%s" % (title, (" ERROR=" + err) if err else "")


def zip_attachment_ids(page):
    """delete[ID] buttons whose row mentions a .zip (manage-attachments page)."""
    ids = []
    for m in re.finditer(r'name="delete\[(\d+)\]"', page):
        row = page[max(0, m.start() - 600):m.start()]
        if re.search(r"\.zip\b", row.rsplit("<tr", 1)[-1], re.I):
            ids.append(m.group(1))
    return ids


class Forum:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "kindle-reading-dashboard release script (+https://github.com/zevisvei/kindle-reading-dashboard)"

    def get(self, path, raw=False):
        r = self.s.get(FORUM + path, timeout=60)
        r.raise_for_status()
        if raw:
            return r.content
        r.encoding = CHARSET
        return r.text

    def post(self, path, fields, files=None):
        data = {k: form_encode(v) if isinstance(v, str) else v for k, v in fields.items()}
        r = self.s.post(FORUM + path, data=data, files=files, timeout=120)
        r.raise_for_status()
        r.encoding = CHARSET
        return r.text

    def login(self, user, password):
        pw = _utf16_units(password.strip(" "))     # md5.js trim() strips spaces only
        self.post("login.php?do=login", {
            "vb_login_username": user,
            "vb_login_password": "",
            "vb_login_md5password": hashlib.md5(_js_bytes(_str_to_ent(pw))).hexdigest(),
            "vb_login_md5password_utf": hashlib.md5(_js_bytes(pw)).hexdigest(),
            "cookieuser": "1", "s": "", "securitytoken": "guest", "do": "login",
        })
        if "Welcome, " not in self.get("index.php"):
            sys.exit("MobileRead login failed for user %r" % user)

    # -- the post as readers see it ------------------------------------------
    def post_zips(self, post):
        """{attachment id: filename} of .zip files shown on the post."""
        page = self.get("showpost.php?p=%d" % post)
        return {m.group(1): m.group(2) for m in re.finditer(
            r'attachment\.php\?attachmentid=(\d+)[^"]*"[^>]*>([^<]+\.zip)</a>', page, re.I)}

    def attachment_bytes(self, aid):
        return self.get("attachment.php?attachmentid=%s" % aid, raw=True)

    # -- one edit session ---------------------------------------------------
    def open_edit(self, post):
        page = self.get("editpost.php?do=editpost&p=%d" % post)
        fields, _ = parse_form(page, "vbform")
        if "message" not in fields or "posthash" not in fields:
            sys.exit("could not read the edit form of post %d (not your post?) %s" % (post, summary(page)))
        return fields

    def attach_page(self, post, edit):
        return self.get("newattachment.php?p=%d&editpost=1&poststarttime=%s&posthash=%s" % (
            post, edit["poststarttime"], edit["posthash"]))

    def manage_attach(self, post, att_page, extra, files=None):
        fields, _ = parse_form(att_page, "manageattach")
        return self.post("newattachment.php?do=manageattach&p=%d" % post,
                         dict(fields, **extra), files=files)

    def save_edit(self, post, edit, reason):
        page = self.post("editpost.php?do=updatepost&p=%d" % post,
                         dict(edit, reason=reason, sbutton="Save Changes"))
        print("  save:", summary(page))
        if page_errors(page):
            sys.exit("MobileRead rejected the edit")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--post", type=int, default=DEFAULT_POST, help="post id to update")
    ap.add_argument("--zip", help="plugin zip (default: latest GitHub release)")
    ap.add_argument("--changelog", default=os.path.join(ROOT, "calibre-plugin", "CHANGELOG.md"))
    ap.add_argument("--dry-run", action="store_true", help="log in and show what would change")
    ap.add_argument("--attachments-only", action="store_true", help="leave the post text alone")
    ap.add_argument("--test-variant", action="store_true",
                    help="tests only: add a marker file so the zip differs from the attached one")
    args = ap.parse_args()

    user = os.environ.get("MOBILEREAD_USER")
    password = os.environ.get("MOBILEREAD_PASSWORD")
    if not (user and password):
        sys.exit("set MOBILEREAD_USER and MOBILEREAD_PASSWORD")

    sections = changelog_sections(args.changelog)
    if not sections:
        sys.exit("no '## X.Y.Z' sections in %s" % args.changelog)
    latest = sections[0][0]

    zpath = args.zip
    if not zpath:
        zpath = os.path.join(tempfile.mkdtemp(prefix="mr_"), ZIP_NAME)
        print("downloading", LATEST_ZIP)
        urllib.request.urlretrieve(LATEST_ZIP, zpath)
    zver = zip_version(zpath)
    if zver != latest:
        sys.exit("zip is version %s but CHANGELOG's newest section is %s" % (zver, latest))
    if args.test_variant:
        zpath = make_test_variant(zpath)
    new_zip = open(zpath, "rb").read()
    new_md5 = hashlib.md5(new_zip).hexdigest()

    f = Forum()
    f.login(user, password)
    print("logged in as", user)

    before = f.post_zips(args.post)
    print("zips on post %d now: %s" % (args.post, before or "none"))
    same = [a for a in before if hashlib.md5(f.attachment_bytes(a)).hexdigest() == new_md5]
    need_upload = not same
    old_ids = [a for a in before if a not in same]

    edit = f.open_edit(args.post)
    msg = edit["message"] if args.attachments_only else new_message(
        edit["message"], version_history_bbcode(sections))
    text_changes = msg.replace("\r\n", "\n").strip() != edit["message"].replace("\r\n", "\n").strip()

    print("plan: %s; %s; text %s" % (
        "upload %s (v%s, md5 %s)" % (os.path.basename(zpath), zver, new_md5[:8]) if need_upload
        else "zip identical to attachment %s, no upload" % same[0],
        "then remove %s" % old_ids if old_ids else "nothing to remove",
        "updated" if text_changes else "unchanged"))
    if args.dry_run:
        if text_changes:
            print("---- new post text ----\n" + msg)
        return
    if not (need_upload or old_ids or text_changes):
        print("nothing to do")
        return

    # edit #1: upload (pending until saved) + new text, then save
    if need_upload:
        att = f.attach_page(args.post, edit)
        att = f.manage_attach(args.post, att, {"upload": "Upload"},
                              files={"attachment[]": (ZIP_NAME, io.BytesIO(new_zip), "application/zip")})
        print("  upload:", summary(att), "pending zips:", zip_attachment_ids(att))
        if page_errors(att):
            sys.exit("upload rejected; post not saved, nothing removed")
    edit["message"] = msg
    f.save_edit(args.post, edit, "v%s" % zver)

    after = f.post_zips(args.post)
    print("zips on post after save:", after)
    if need_upload:
        added = [a for a in after if a not in before]
        if not added or hashlib.md5(f.attachment_bytes(added[0])).hexdigest() != new_md5:
            sys.exit("new zip is not on the post after saving; old zip(s) kept: %s" % list(after))
        print("new zip attached:", added[0])

    # edit #2: remove the old zip(s), then save
    if old_ids:
        edit = f.open_edit(args.post)
        att = f.attach_page(args.post, edit)
        for aid in old_ids:
            att = f.manage_attach(args.post, att, {"delete[%s]" % aid: "Remove"})
            print("  remove %s:" % aid, summary(att))
        f.save_edit(args.post, edit, "v%s: remove old zip" % zver)

    final = f.post_zips(args.post)
    print("zips on post %d: %s" % (args.post, final))
    if len(final) != 1 or hashlib.md5(f.attachment_bytes(next(iter(final)))).hexdigest() != new_md5:
        sys.exit("expected exactly the new zip on the post; check it by hand")
    print("done: post %d has v%s" % (args.post, zver))


if __name__ == "__main__":
    main()
