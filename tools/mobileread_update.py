#!/usr/bin/env python3
"""
Update the MobileRead "[GUI Plugin] Kindle Reading Dashboard" first post after
a new plugin release: swap the attached zip and refresh the Version History.

Runs automatically in the "Calibre plugin release" workflow. Plain HTTP
(requests), no browser. Credentials come from the environment:

    MOBILEREAD_USER, MOBILEREAD_PASSWORD   (repository secrets in CI)

Locally:

    pip install requests
    MOBILEREAD_USER=... MOBILEREAD_PASSWORD=... python tools/mobileread_update.py --dry-run

Order matters: the new zip is uploaded BEFORE the old one is removed, so the
post (and calibre's plugin updater, which downloads from it) never has zero
zips. The calibre index expects exactly one zip in the first post.
"""
import argparse
import hashlib
import html
import os
import re
import sys
import tempfile
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


def check_errors(page, what):
    i = page.find("The following errors")
    if i >= 0:
        msg = re.sub(r"<[^>]+>", " ", page[i:i + 1500])
        sys.exit("MobileRead rejected %s: %s" % (what, re.sub(r"\s+", " ", html.unescape(msg))[:400]))


class Forum:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "kindle-reading-dashboard release script (+https://github.com/zevisvei/kindle-reading-dashboard)"

    def get(self, path):
        r = self.s.get(FORUM + path, timeout=60)
        r.raise_for_status()
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


def zip_attachment_ids(page):
    """delete[ID] buttons whose row mentions a .zip (manage-attachments page)."""
    ids = []
    for m in re.finditer(r'name="delete\[(\d+)\]"', page):
        row = page[max(0, m.start() - 600):m.start()]
        if re.search(r"\.zip\b", row.rsplit("<tr", 1)[-1], re.I):
            ids.append(m.group(1))
    return ids


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--post", type=int, default=DEFAULT_POST, help="first-post id")
    ap.add_argument("--zip", help="plugin zip (default: latest GitHub release)")
    ap.add_argument("--changelog", default=os.path.join(ROOT, "calibre-plugin", "CHANGELOG.md"))
    ap.add_argument("--dry-run", action="store_true", help="log in and show the new post text, change nothing")
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

    f = Forum()
    f.login(user, password)
    print("logged in as", user)

    edit_path = "editpost.php?do=editpost&p=%d" % args.post
    edit_page = f.get(edit_path)
    fields, _ = parse_form(edit_page, "vbform")
    if "message" not in fields or "posthash" not in fields:
        sys.exit("could not read the edit form of post %d (not your post?)" % args.post)
    msg = new_message(fields["message"], version_history_bbcode(sections))

    att_path = "newattachment.php?p=%d&editpost=1&poststarttime=%s&posthash=%s" % (
        args.post, fields["poststarttime"], fields["posthash"])
    att_page = f.get(att_path)
    old_ids = zip_attachment_ids(att_page)

    if args.dry_run:
        print("---- new post text ----\n" + msg)
        print("---- would upload %s (v%s), then remove zip attachment(s) %s ----"
              % (zpath, zver, old_ids))
        return

    # 1) upload the new zip first ...
    att_fields, _ = parse_form(att_page, "manageattach")
    with open(zpath, "rb") as fh:
        att_page = f.post("newattachment.php?do=manageattach&p=%d" % args.post,
                          dict(att_fields, upload="Upload"),
                          files={"attachment[]": (ZIP_NAME, fh, "application/zip")})
    check_errors(att_page, "the upload")
    new_ids = [i for i in zip_attachment_ids(att_page) if i not in old_ids]
    if not new_ids:
        sys.exit("upload did not show up in the attachment list; nothing removed")
    print("uploaded attachment", new_ids)

    # ... then remove the old one(s)
    for aid in old_ids:
        att_fields, _ = parse_form(att_page, "manageattach")
        att_page = f.post("newattachment.php?do=manageattach&p=%d" % args.post,
                          dict(att_fields, **{"delete[%s]" % aid: "Remove"}))
        check_errors(att_page, "removing attachment %s" % aid)
    left = zip_attachment_ids(att_page)
    print("zip attachments now:", left)
    if len(left) != 1:
        print("WARNING: post has %d zips; check the attachments by hand" % len(left))

    # 2) save the refreshed text
    fields["message"] = msg
    fields["reason"] = "v%s" % zver
    page = f.post("editpost.php?do=updatepost&p=%d" % args.post,
                  dict(fields, sbutton="Save Changes"))
    check_errors(page, "the edit")
    print("updated post %d (v%s)" % (args.post, zver))


if __name__ == "__main__":
    main()
