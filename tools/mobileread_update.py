#!/usr/bin/env python3
"""
Update the MobileRead "[GUI Plugin] Kindle Reading Dashboard" first post after
a new plugin release: swap the attached zip and refresh the Version History.

Run locally, after the GitHub release workflow has published the zip:

    pip install playwright            # uses your installed Chrome, no browser download
    python tools/mobileread_update.py --dry-run     # show what would change
    python tools/mobileread_update.py

It drives a real Chrome window with its own profile (default
~/.mobileread-profile). The first run waits for you to log in to MobileRead in
that window; the login is remembered after that. No password is stored here.

Order matters: the new zip is uploaded BEFORE the old one is removed, so the
post (and calibre's plugin updater, which downloads from it) never has zero
zips. The calibre index expects exactly one zip in the first post.
"""
import argparse
import os
import re
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FORUM = "https://www.mobileread.com/forums/"
DEFAULT_POST = 4616175          # first post of showthread.php?t=375732
ZIP_NAME = "kindle_reading_dashboard.zip"
LATEST_ZIP = ("https://github.com/zevisvei/kindle-reading-dashboard/releases/"
              "latest/download/" + ZIP_NAME)


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


def ensure_logged_in(page):
    page.goto(FORUM + "index.php")
    if "Welcome, " in page.inner_text("body"):
        return
    print("Log in to MobileRead in the Chrome window (waiting up to 5 minutes)...")
    page.wait_for_function("() => document.body.innerText.includes('Welcome, ')",
                           timeout=300000, polling=2000)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--post", type=int, default=DEFAULT_POST, help="first-post id")
    ap.add_argument("--zip", help="plugin zip (default: latest GitHub release)")
    ap.add_argument("--changelog", default=os.path.join(ROOT, "calibre-plugin", "CHANGELOG.md"))
    ap.add_argument("--profile", default=os.path.join(os.path.expanduser("~"), ".mobileread-profile"))
    ap.add_argument("--dry-run", action="store_true", help="print the new post text, change nothing")
    args = ap.parse_args()

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
    history = version_history_bbcode(sections)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(args.profile, channel="chrome", headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page)
            page.goto(FORUM + "editpost.php?do=editpost&p=%d" % args.post)
            ta = page.locator("#vB_Editor_001_textarea")
            old = ta.input_value()
            msg = new_message(old, history)
            if args.dry_run:
                print("---- new post text ----\n" + msg)
                print("---- would upload %s (v%s) and remove the old zip ----" % (zpath, zver))
                return

            # 1) attachments: upload new first, then remove the old zip(s)
            with page.expect_popup() as pop:
                page.locator('input[value="Manage Attachments"]').click()
            popup = pop.value
            popup.wait_for_load_state("load")
            old_ids = popup.evaluate(
                """() => [...document.querySelectorAll('input[name^="delete["]')]
                       .filter(b => /\\.zip/i.test(b.closest('tr, div, li').innerText))
                       .map(b => b.name)""")
            popup.locator('input[name="attachment[]"]').first.set_input_files(zpath)
            with popup.expect_navigation():
                popup.locator('input[name="upload"][value="Upload"]').first.click()
            for name in old_ids:
                with popup.expect_navigation():
                    popup.locator('input[name="%s"]' % name).click()
            zips = popup.evaluate(
                "() => (document.body.innerText.match(/\\S+\\.zip \\(/g) || []).length")
            popup.close()
            if zips != 1:
                print("WARNING: post now has %d zips; check the attachments by hand" % zips)

            # 2) text: refresh Version History and save
            ta.fill(msg)
            page.fill('input[name="reason"]', "v%s" % zver)
            with page.expect_navigation():
                page.locator('input[name="sbutton"]').first.click()
            body = page.inner_text("body")
            if "The following errors" in body:
                sys.exit("MobileRead rejected the edit:\n" + body[body.index("The following errors"):][:400])
            print("updated:", page.url, "(v%s)" % zver)
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
