#!/usr/bin/env python
"""
Optional SSH support: install paramiko (+ its compiled dependencies) into
calibre's config folder, so SSH with a password works on calibre's own Python
(which has no pip) without installing a separate Python.

Wheels come straight from PyPI (pypi.org JSON API), are picked for this
calibre's Python version / OS / CPU, checked against PyPI's sha256 and
unzipped into  <calibre config>/plugins/kindle_reading_libs/<tag>/ .
The tag includes the Python version, so after a calibre upgrade that changes
its Python the old folder is simply ignored and Configure offers to install
again (cffi is built per Python version).
"""
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import urllib.request
import zipfile

# Known-good set (tested on calibre 9 / Python 3.14). A package whose pinned
# version has no wheel for this Python falls back to its latest release.
PACKAGES = [
    ("paramiko", "5.1.0"),
    ("cryptography", "50.0.2"),
    ("bcrypt", "5.0.0"),
    ("pynacl", "1.6.2"),
    ("cffi", "2.1.1"),
    ("pycparser", "3.11"),
    ("invoke", "3.0.3"),
]
PYPI = "https://pypi.org/pypi/%s/json"
PYPI_VER = "https://pypi.org/pypi/%s/%s/json"
MARKER = ".complete"


def _tag():
    return "%s-%s-%s" % (sys.implementation.cache_tag, sys.platform,
                         platform.machine().lower() or "unknown")


def lib_dir():
    from calibre.constants import config_dir
    return os.path.join(config_dir, "plugins", "kindle_reading_libs", _tag())


def installed_info():
    """The marker's {package: version} dict, or None when not installed."""
    try:
        with open(os.path.join(lib_dir(), MARKER), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return None


def activate():
    """Put the installed libs on sys.path (no-op when not installed)."""
    if installed_info() is None:
        return False
    d = lib_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    return True


def paramiko_version():
    """paramiko's version if importable (ours or any other), else None."""
    activate()
    try:
        import paramiko
        return getattr(paramiko, "__version__", "?")
    except Exception:  # noqa
        return None


# --------------------------------------------------------------------------- #
#  wheel tag matching (a small subset of PEP 425, enough for these packages)
# --------------------------------------------------------------------------- #
def _py_ok(pys, abis):
    minor = sys.version_info[1]
    for py in pys.split("."):
        for abi in abis.split("."):
            if py == "py3" and abi == "none":
                return True
            m =re.match(r"cp3(\d+)$", py) or re.match(r"py3(\d+)$", py)
            if not m:
                continue
            n = int(m.group(1))
            if abi == "abi3" and py.startswith("cp") and n <= minor:
                return True
            if abi == "none" and n <= minor:
                return True
            if abi == "cp3%d" % minor and n == minor:
                return True
    return False


def _glibc():
    try:
        v = os.confstr("CS_GNU_LIBC_VERSION").split()[1]
    except Exception:  # noqa
        v = platform.libc_ver()[1]
    try:
        major, minor = v.split(".")[:2]
        return int(major), int(minor)
    except Exception:  # noqa
        return None


def _plat_ok(plats):
    mach = platform.machine().lower()
    for p in plats.split("."):
        if p == "any":
            return True
        if sys.platform == "win32":
            want = {"amd64": "win_amd64", "x86_64": "win_amd64",
                    "arm64": "win_arm64"}.get(mach, "win32")
            if sys.maxsize <= 2 ** 32:
                want = "win32"
            if p == want:
                return True
        elif sys.platform == "darwin":
            m = re.match(r"macosx_(\d+)_(\d+)_(\w+)$", p)
            if not m:
                continue
            try:
                cur = tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
            except Exception:  # noqa
                cur = (99, 0)
            if m.group(3) in (mach, "universal2") and \
                    (int(m.group(1)), int(m.group(2))) <= cur:
                return True
        elif sys.platform.startswith("linux"):
            alias = {"manylinux1": (2, 5), "manylinux2010": (2, 12),
                     "manylinux2014": (2, 17)}
            m = re.match(r"(manylinux1|manylinux2010|manylinux2014)_(\w+)$", p)
            if m:
                need, arch = alias[m.group(1)], m.group(2)
            else:
                m = re.match(r"manylinux_(\d+)_(\d+)_(\w+)$", p)
                if not m:
                    continue
                need, arch = (int(m.group(1)), int(m.group(2))), m.group(3)
            g = _glibc()
            if arch == mach and g and need <= g:
                return True
    return False


def _rank(filename):
    """None if the wheel does not fit this calibre; lower = preferred."""
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    pys, abis, plats = parts[-3:]
    if not (_py_ok(pys, abis) and _plat_ok(plats)):
        return None
    if abis.startswith("cp"):
        return 0          # built for exactly this Python
    if abis == "abi3":
        return 1
    return 2              # pure Python


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "kindle-reading-dashboard"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _pick(name, version):
    """(filename, url, sha256) of the best wheel; pinned version first."""
    for url in (PYPI_VER % (name, version), PYPI % name):
        try:
            files = json.loads(_get(url))["urls"]
        except Exception:  # noqa
            continue
        best = None
        for f in files:
            if f.get("packagetype") != "bdist_wheel" or f.get("yanked"):
                continue
            r = _rank(f["filename"])
            if r is not None and (best is None or r < best[0]):
                best = (r, f)
        if best:
            f = best[1]
            return f["filename"], f["url"], f["digests"]["sha256"]
    raise RuntimeError(
        "PyPI has no %s wheel for this calibre (Python %d.%d, %s %s)."
        % (name, sys.version_info[0], sys.version_info[1], sys.platform,
           platform.machine()))


def install(progress=lambda msg, frac: None):
    """Download + unpack every package. Raises on any failure, leaving the
    previous install (if any) untouched."""
    dest = lib_dir()
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    versions = {}
    try:
        n = len(PACKAGES)
        for i, (name, version) in enumerate(PACKAGES):
            progress("Finding %s…" % name, i / n)
            filename, url, sha = _pick(name, version)
            progress("Downloading %s…" % filename, (i + 0.3) / n)
            data = _get(url, timeout=300)
            if hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError("Checksum mismatch for %s" % filename)
            # zipfile.extractall drops absolute paths and '..' components
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            versions[name] = filename.split("-")[1]
        with open(os.path.join(tmp, MARKER), "w", encoding="utf-8") as f:
            json.dump(versions, f)
        if os.path.exists(dest):
            # fails on Windows if the old copy is loaded: then a restart is needed
            shutil.rmtree(dest)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    progress("Done", 1.0)
    activate()
    return versions
