"""Download Proton's `proton-drive` CLI into the app's data folder, verified by SHA-512.

Proton publishes the CLI at a stable index page with one URL and one SHA-512 checksum per platform:
https://proton.me/download/drive/cli/index.html. Old versions stay downloadable at
https://proton.me/download/drive/cli/<version>/<platform>/proton-drive, but the index only lists the
latest one, so versions this app was tested against have their checksums pinned in KNOWN_SHA512 below.

The app downloads the pinned DEFAULT_VERSION unless told otherwise (`PROTON_CLI_VERSION`), because the
CLI's command-line details change between versions (conflict strategy names, how file names are
treated as glob patterns). `PROTON_CLI_VERSION=latest` follows Proton's index.
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import platform
import re
import subprocess
import tempfile
import threading
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("cli_install")

INDEX_URL = "https://proton.me/download/drive/cli/index.html"
URL_TEMPLATE = "https://proton.me/download/drive/cli/{version}/{platform}/proton-drive"
DEFAULT_VERSION = "0.6.0"

# SHA-512 of the official downloads the app was tested against (verified 2026-11-01).
KNOWN_SHA512 = {
    ("0.6.0", "linux-x64"): "e77f5b27a51a81063c23c15ac0a9f07e0ec5c868e78670f34b45b3c3c2e679ed769e6225796b900d0d02735a0c52a21eba72356f3ad617de076c405532e698dc",
    ("0.9.0", "linux-x64"): "3533025ba69ae112b64e3e01fbcc1ad0688136a4043f6cf6a72886967d85fdcd9ec235479c2e113171614be5225bfba93427509a05ca5aa6071d924fa7e91ca8",
}

_ROW = re.compile(
    r'href="(https://proton\.me/download/drive/cli/(\d+\.\d+\.\d+)/([a-z0-9-]+)/proton-drive(?:\.exe)?)"'
    r'[^<]*</a></td>\s*<td><code>([0-9a-f]{128})</code>')
_TITLE = re.compile(r"<title>Proton Drive CLI (\d+\.\d+\.\d+)</title>")


class InstallError(Exception):
    pass


def detect_platform() -> str:
    """Platform key used in Proton's URLs: linux-x64, linux-arm64, with `-musl` on Alpine-like systems."""
    if platform.system() != "Linux":
        raise InstallError(f"automatic download supports Linux only (this is {platform.system()})")
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise InstallError(f"no Proton CLI build for CPU type {machine!r}")
    musl = bool(glob.glob("/lib/ld-musl-*") or glob.glob("/lib/libc.musl-*"))
    return f"linux-{arch}" + ("-musl" if musl else "")


def parse_index(html: str) -> tuple[str, dict[str, tuple[str, str]]]:
    """Return (latest version, {platform: (url, sha512)}) from Proton's index page."""
    m = _TITLE.search(html)
    if not m:
        raise InstallError("could not read the version from Proton's CLI download page")
    plats = {plat: (url, sha) for url, ver, plat, sha in _ROW.findall(html) if ver == m.group(1)}
    if not plats:
        raise InstallError("could not read any download links from Proton's CLI download page")
    return m.group(1), plats


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "proton-drive-truenas-cli"}), timeout=60)


def installed_version(path: str) -> str | None:
    """Version of the binary at `path`, or None if it is missing or does not run."""
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return None
    try:
        out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, XDG_DATA_HOME=tempfile.gettempdir(),
                                      PROTON_DRIVE_CREDENTIALS_STORE="unsafe_file")).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"cli-drive@(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


@dataclass
class Plan:
    version: str
    platform: str
    url: str
    sha512: str


def plan_download(version: str, plat: str, opener=_open) -> Plan:
    """Decide what to download and which checksum to trust. Never returns an unverifiable download."""
    env_sha = os.environ.get("PROTON_CLI_SHA512", "").strip().lower()
    if version == "latest":
        html = opener(INDEX_URL).read().decode("utf-8", "replace")
        latest, plats = parse_index(html)
        if plat not in plats:
            raise InstallError(f"Proton's index has no {plat} build (it lists: {', '.join(sorted(plats))})")
        url, sha = plats[plat]
        return Plan(latest, plat, url, KNOWN_SHA512.get((latest, plat), sha))
    sha = KNOWN_SHA512.get((version, plat)) or env_sha
    if not sha:
        # Not pinned here: accept the index's checksum only if the index is showing exactly this version.
        try:
            latest, plats = parse_index(opener(INDEX_URL).read().decode("utf-8", "replace"))
            if latest == version and plat in plats:
                sha = plats[plat][1]
        except Exception as e:  # network or page-format problems must not turn into "trust anything"
            log.warning("could not read Proton's CLI index: %s", e)
    if not sha:
        raise InstallError(f"no checksum is known for CLI {version} ({plat}). Use a pinned version "
                           f"({', '.join(sorted({v for v, _ in KNOWN_SHA512}))}), 'latest', or set PROTON_CLI_SHA512")
    return Plan(version, plat, URL_TEMPLATE.format(version=version, platform=plat), sha)


def download(plan: Plan, dest: str, opener=_open) -> None:
    """Download to a temp file next to `dest`, check SHA-512, make it executable, then move it into place."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".proton-drive.", dir=os.path.dirname(dest))
    try:
        h = hashlib.sha512()
        with os.fdopen(fd, "wb") as f, opener(plan.url) as r:
            while chunk := r.read(1024 * 1024):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != plan.sha512.lower():
            raise InstallError("downloaded file does not match the expected SHA-512 checksum; it was discarded")
        os.chmod(tmp, 0o755)
        got = installed_version(tmp)
        if got is None:
            raise InstallError("the downloaded file does not run on this system")
        if got != plan.version:
            raise InstallError(f"downloaded CLI reports version {got}, expected {plan.version}")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure_cli(path: str, version: str | None = None, opener=_open) -> str:
    """Make sure a working CLI is at `path`. Returns a human-readable status; raises InstallError."""
    explicit = version is not None
    version = version or DEFAULT_VERSION
    have = installed_version(path)
    if have and (not explicit or version == have):  # an existing binary is kept unless a version was asked for
        return f"CLI {have} already installed at {path}"
    if os.path.exists(path) and not explicit:
        raise InstallError(f"{path} exists but does not run; remove it or fix its permissions")
    plan = plan_download(version, detect_platform(), opener)
    if have == plan.version:
        return f"CLI {have} already installed at {path}"
    log.warning("downloading Proton CLI %s (%s) from proton.me", plan.version, plan.platform)
    try:
        download(plan, path, opener)
    except OSError as e:
        raise InstallError(f"could not write {path}: {e}") from e
    return f"CLI {plan.version} downloaded to {path} (SHA-512 verified)"


class Installer:
    """Runs ensure_cli in the background at startup so the page is available immediately."""

    def __init__(self, path: str, version: str | None = None, enabled: bool = True, opener=_open):
        self.path, self.version, self.enabled, self.opener = path, version, enabled, opener
        self.status = "idle"  # idle | installing | ready | failed
        self.message = ""
        self.on_ready = None  # called when a working CLI is in place (the app clears its cached status)
        self._lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="cli-install").start()

    def run(self) -> None:
        with self._lock:
            self.status, self.message = "installing", "checking the Proton CLI"
            try:
                have = installed_version(self.path)
                if not self.enabled:
                    if not have:
                        raise InstallError("automatic download is turned off (PROTON_CLI_AUTO_DOWNLOAD=0) and "
                                           f"there is no working CLI at {self.path}")
                    self.message = f"CLI {have} found at {self.path} (automatic download is off)"
                else:
                    self.message = ensure_cli(self.path, self.version, self.opener)
                self.status = "ready"
                log.info("%s", self.message)
                if self.on_ready:
                    self.on_ready()
            except Exception as e:  # keep the app up; the page explains and offers a retry
                self.status, self.message = "failed", str(e)
                log.error("Proton CLI install failed: %s", e)
