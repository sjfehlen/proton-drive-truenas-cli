import hashlib
import io
import os
import stat

import pytest

from app import cli_install as ci
from tests.test_runner import env  # noqa: F401  (shared fixture)

SCRIPT = b"#!/bin/sh\necho 'Proton Drive CLI cli-drive@0.6.0+abc123'\n"
SHA = hashlib.sha512(SCRIPT).hexdigest()


class Opener:
    """Stands in for the network: serves canned bytes per URL and records what was asked for."""

    def __init__(self, pages=None):
        self.pages, self.asked = pages or {}, []

    def __call__(self, url):
        self.asked.append(url)
        if url not in self.pages:
            raise OSError(f"no route to {url}")
        data = self.pages[url]
        return io.BytesIO(data if isinstance(data, bytes) else data.encode())


@pytest.fixture(autouse=True)
def linux_x64(monkeypatch):
    monkeypatch.setattr(ci, "detect_platform", lambda: "linux-x64")
    monkeypatch.delenv("PROTON_CLI_SHA512", raising=False)
    monkeypatch.setattr(ci, "KNOWN_SHA512", {("0.6.0", "linux-x64"): SHA})


URL060 = ci.URL_TEMPLATE.format(version="0.6.0", platform="linux-x64")


def index_html(version, sha, plat="linux-x64"):
    return (f"<title>Proton Drive CLI {version}</title><table><tr><td>linux/x64</td>"
            f'<td><a href="https://proton.me/download/drive/cli/{version}/{plat}/proton-drive">u</a></td>'
            f"<td><code>{sha}</code></td></tr></table>")


def test_platform_detection(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(ci.platform, "system", lambda: "Linux")
    for machine, want in [("x86_64", "linux-x64"), ("aarch64", "linux-arm64"), ("AMD64", "linux-x64")]:
        monkeypatch.setattr(ci.platform, "machine", lambda m=machine: m)
        monkeypatch.setattr(ci.glob, "glob", lambda pattern: [])
        assert ci.detect_platform() == want
    monkeypatch.setattr(ci.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ci.glob, "glob", lambda pattern: ["/lib/ld-musl-x86_64.so.1"] if "musl" in pattern else [])
    assert ci.detect_platform() == "linux-x64-musl"
    monkeypatch.setattr(ci.platform, "machine", lambda: "riscv64")
    with pytest.raises(ci.InstallError):
        ci.detect_platform()
    monkeypatch.setattr(ci.platform, "system", lambda: "Windows")
    with pytest.raises(ci.InstallError):
        ci.detect_platform()


def test_parse_index():
    ver, plats = ci.parse_index(index_html("0.9.0", "a" * 128))
    assert ver == "0.9.0" and plats["linux-x64"][1] == "a" * 128 and "0.9.0/linux-x64" in plats["linux-x64"][0]
    with pytest.raises(ci.InstallError):
        ci.parse_index("<html>nothing here</html>")


def test_downloads_verifies_and_installs(tmp_path):
    dest = str(tmp_path / "bin" / "proton-drive")
    op = Opener({URL060: SCRIPT})
    msg = ci.ensure_cli(dest, opener=op)
    assert "0.6.0" in msg and "SHA-512 verified" in msg
    assert os.stat(dest).st_mode & stat.S_IXUSR
    assert ci.installed_version(dest) == "0.6.0"
    assert os.listdir(tmp_path / "bin") == ["proton-drive"]  # no temp files left behind
    assert op.asked == [URL060]  # a pinned version needs no index lookup


def test_wrong_checksum_is_refused_and_leaves_nothing(tmp_path):
    dest = str(tmp_path / "bin" / "proton-drive")
    with pytest.raises(ci.InstallError, match="SHA-512"):
        ci.ensure_cli(dest, opener=Opener({URL060: SCRIPT + b"tampered"}))
    assert not os.path.exists(dest) and os.listdir(tmp_path / "bin") == []


def test_binary_that_does_not_run_is_refused(tmp_path, monkeypatch):
    junk = b"not an executable"
    monkeypatch.setattr(ci, "KNOWN_SHA512", {("0.6.0", "linux-x64"): hashlib.sha512(junk).hexdigest()})
    with pytest.raises(ci.InstallError, match="does not run"):
        ci.ensure_cli(str(tmp_path / "p"), opener=Opener({URL060: junk}))
    assert os.listdir(tmp_path) == []


def test_binary_reporting_a_different_version_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "KNOWN_SHA512", {("0.7.0", "linux-x64"): SHA})
    url = ci.URL_TEMPLATE.format(version="0.7.0", platform="linux-x64")
    with pytest.raises(ci.InstallError, match="reports version 0.6.0"):
        ci.ensure_cli(str(tmp_path / "p"), "0.7.0", Opener({url: SCRIPT}))


def test_unpinned_version_without_a_checksum_is_refused(tmp_path):
    with pytest.raises(ci.InstallError, match="no checksum is known for CLI 0.7.0"):
        ci.ensure_cli(str(tmp_path / "p"), "0.7.0", Opener())  # the index is unreachable too


def test_checksum_can_be_supplied_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTON_CLI_SHA512", SHA)
    monkeypatch.setattr(ci, "KNOWN_SHA512", {})
    url = ci.URL_TEMPLATE.format(version="0.6.0", platform="linux-x64")
    assert "downloaded" in ci.ensure_cli(str(tmp_path / "p"), "0.6.0", Opener({url: SCRIPT}))


def test_index_checksum_is_trusted_only_for_the_version_the_index_shows(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "KNOWN_SHA512", {})
    url = ci.URL_TEMPLATE.format(version="0.6.0", platform="linux-x64")
    op = Opener({ci.INDEX_URL: index_html("0.6.0", SHA), url: SCRIPT})
    assert "downloaded" in ci.ensure_cli(str(tmp_path / "ok"), "0.6.0", op)
    stale = Opener({ci.INDEX_URL: index_html("0.9.0", "b" * 128), url: SCRIPT})
    with pytest.raises(ci.InstallError, match="no checksum"):
        ci.ensure_cli(str(tmp_path / "no"), "0.6.0", stale)


def test_latest_follows_the_index(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "KNOWN_SHA512", {})
    script9 = SCRIPT.replace(b"0.6.0", b"0.9.0")
    url9 = ci.URL_TEMPLATE.format(version="0.9.0", platform="linux-x64")
    op = Opener({ci.INDEX_URL: index_html("0.9.0", hashlib.sha512(script9).hexdigest()), url9: script9})
    dest = str(tmp_path / "p")
    assert "0.9.0" in ci.ensure_cli(dest, "latest", op) and ci.installed_version(dest) == "0.9.0"
    assert "already installed" in ci.ensure_cli(dest, "latest", op)  # nothing newer: no second download


def test_existing_working_binary_is_left_alone_unless_a_version_is_requested(tmp_path):
    dest = tmp_path / "proton-drive"
    dest.write_bytes(SCRIPT)
    dest.chmod(0o755)
    op = Opener()
    assert "already installed" in ci.ensure_cli(str(dest), None, op) and op.asked == []
    assert "already installed" in ci.ensure_cli(str(dest), "0.6.0", op) and op.asked == []


def test_requesting_a_different_version_replaces_the_binary(tmp_path, monkeypatch):
    dest = tmp_path / "proton-drive"
    dest.write_bytes(SCRIPT)
    dest.chmod(0o755)
    script9 = SCRIPT.replace(b"0.6.0", b"0.9.0")
    monkeypatch.setattr(ci, "KNOWN_SHA512", {("0.9.0", "linux-x64"): hashlib.sha512(script9).hexdigest()})
    url9 = ci.URL_TEMPLATE.format(version="0.9.0", platform="linux-x64")
    ci.ensure_cli(str(dest), "0.9.0", Opener({url9: script9}))
    assert ci.installed_version(str(dest)) == "0.9.0"


def test_a_broken_existing_file_is_not_silently_overwritten(tmp_path):
    dest = tmp_path / "proton-drive"
    dest.write_bytes(b"garbage")
    with pytest.raises(ci.InstallError, match="exists but does not run"):
        ci.ensure_cli(str(dest), None, Opener())


def test_installer_reports_ready_and_failed(tmp_path):
    ok = ci.Installer(str(tmp_path / "a" / "proton-drive"), opener=Opener({URL060: SCRIPT}))
    ok.run()
    assert ok.status == "ready" and "verified" in ok.message
    bad = ci.Installer(str(tmp_path / "b" / "proton-drive"), opener=Opener())  # offline
    bad.run()
    assert bad.status == "failed" and bad.message


def test_installer_with_auto_download_off(tmp_path):
    off = ci.Installer(str(tmp_path / "proton-drive"), enabled=False, opener=Opener())
    off.run()
    assert off.status == "failed" and "turned off" in off.message
    (tmp_path / "proton-drive").write_bytes(SCRIPT)
    (tmp_path / "proton-drive").chmod(0o755)
    off.run()
    assert off.status == "ready" and off.opener.asked == []


def test_installer_tells_the_app_when_the_cli_is_ready(tmp_path):
    seen = []
    inst = ci.Installer(str(tmp_path / "bin" / "proton-drive"), opener=Opener({URL060: SCRIPT}))
    inst.on_ready = lambda: seen.append(inst.status)
    inst.run()
    assert seen == ["ready"]
    bad = ci.Installer(str(tmp_path / "x" / "proton-drive"), opener=Opener())
    bad.on_ready = lambda: seen.append("should not be called")
    bad.run()
    assert seen == ["ready"]


def test_a_missing_cli_result_is_never_cached(env, tmp_path):
    from app.cli import ProtonCli
    db, cli, runner, src, log = env
    good = cli.binary
    cli.binary = str(tmp_path / "later" / "proton-drive")
    assert cli.session_status_cached().code == 127
    assert cli._status is None  # not remembered
    os.makedirs(tmp_path / "later")
    import shutil
    shutil.copy(good, cli.binary)
    assert cli.session_status_cached().ok  # picks the binary up at once, no waiting for a cache to expire
    cli._version = None
    cli.binary = "/nonexistent/proton-drive"
    assert cli.version() is None and not cli._version  # a failed version lookup is not remembered either
