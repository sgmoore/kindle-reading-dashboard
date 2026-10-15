# Calibre plugin changelog

One `## X.Y.Z` section per release. The release workflow uses the matching
section as the GitHub release notes, and `tools/mobileread_update.py` builds
the MobileRead "Version History" from the first line of each section.

## 1.3.0
- Open web dashboard works out of the box: the dashboard ships inside the plugin and reads the Kindle like the import does (USB, MTP or SSH); no dashboard.py path, no Python install.
- Works without cc.db: the dashboard is built from the reading-data files alone when cc.db is not available.
- Optional cc.db / fmcache.db copies from anywhere (Configure, with Browse) for exact titles, series and read status, in the import and the dashboard; books missing from an older cc.db copy are still included.
- Help in Configure explaining what each source gives, with links.

## 1.2.0
- Browse buttons for the documents folder, SSH key and dashboard.py in Configure.
- Open web dashboard works without installing Python (uses Calibre's own when no system Python is found).

## 1.1.0
- Support for MTP Kindles (Scribe 3, Colorsoft, 2024 Paperwhite); requires Calibre 7.25+.
- Clean title/ASIN for books in documents/Downloads/Items01.

## 1.0.0
- Initial release.
