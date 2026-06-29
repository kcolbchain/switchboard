"""Tests for the Switchboard Lab PWA (web/manifest.json + web/sw.js).

Validates that the lab is installable + offline-capable:
- the manifest is valid JSON with the fields browsers require to offer install
- the icons it references exist on disk
- the service worker exists, is valid JS (node --check), precaches an app shell,
  and the shell entries it lists actually exist
- service-worker registration is wired into the pages
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
MANIFEST = WEB / "manifest.json"
SW = WEB / "sw.js"


def runnable_node() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        subprocess.run([node, "--version"], capture_output=True, check=False, timeout=5)
    except OSError:
        return None
    return node


# ─── manifest ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST.is_file(), f"missing {MANIFEST}"
    return json.loads(MANIFEST.read_text())


def test_manifest_has_install_fields(manifest: dict) -> None:
    for field in ("name", "short_name", "start_url", "display", "icons",
                  "background_color", "theme_color"):
        assert field in manifest, f"manifest missing required field: {field}"
    assert manifest["display"] in ("standalone", "fullscreen", "minimal-ui")


def test_manifest_icons_exist_and_cover_purposes(manifest: dict) -> None:
    icons = manifest["icons"]
    assert icons, "manifest declares no icons"
    purposes = set()
    for icon in icons:
        src = icon["src"]
        # resolve relative to the manifest location (scope "./" == web/)
        path = (WEB / src.lstrip("./")).resolve()
        assert path.is_file(), f"icon file missing: {src} -> {path}"
        purposes.update(icon.get("purpose", "any").split())
    assert "any" in purposes, "need at least one 'any' purpose icon"
    assert "maskable" in purposes, "need a maskable icon for a polished install"


def test_manifest_start_url_exists(manifest: dict) -> None:
    start = manifest["start_url"].lstrip("./")
    assert (WEB / start).is_file(), f"start_url target missing: {manifest['start_url']}"


def test_manifest_shortcuts_resolve(manifest: dict) -> None:
    for sc in manifest.get("shortcuts", []):
        target = sc["url"].lstrip("./")
        assert (WEB / target).is_file(), f"shortcut url target missing: {sc['url']}"


def test_theme_color_matches_manifest(manifest: dict) -> None:
    # The root page advertises the same theme-color so install chrome matches.
    home = (WEB / "index.html").read_text()
    assert manifest["theme_color"] in home


# ─── service worker ──────────────────────────────────────────────────────────


def test_sw_exists_and_caches_shell() -> None:
    assert SW.is_file(), f"missing {SW}"
    body = SW.read_text()
    # core lifecycle + strategy hooks must be present
    for hook in ("install", "activate", "fetch", "caches.open", "skipWaiting", "clients.claim"):
        assert hook in body, f"service worker missing: {hook}"
    # navigation offline fallback is the load-bearing offline behavior
    assert "navigate" in body
    assert "OFFLINE_FALLBACK" in body


def test_sw_shell_entries_exist_on_disk() -> None:
    """Every same-origin SHELL entry the SW precaches should exist (so install
    doesn't silently drop the offline app shell)."""
    body = SW.read_text()
    import re

    m = re.search(r"const SHELL\s*=\s*\[(.*?)\];", body, re.DOTALL)
    assert m, "SHELL array not found in sw.js"
    entries = re.findall(r'"([^"]+)"', m.group(1))
    assert entries, "SHELL is empty"
    for entry in entries:
        if entry in ("./",):
            continue  # directory index, served by start_url
        path = (WEB / entry.lstrip("./")).resolve()
        assert path.is_file(), f"SHELL precache target missing: {entry} -> {path}"


def test_sw_registered_from_root_and_lab() -> None:
    home = (WEB / "index.html").read_text()
    assert "serviceWorker" in home and "register('./sw.js'" in home, "root page does not register the SW"
    shared = (WEB / "lab" / "shared.js").read_text()
    assert "serviceWorker" in shared and "../sw.js" in shared, "lab pages do not register the SW"


def test_swap_page_links_manifest() -> None:
    swap = (WEB / "lab" / "swap.html").read_text()
    assert 'rel="manifest"' in swap, "swap page should link the PWA manifest"


def _node_check(source: str, suffix: str) -> tuple[int, str]:
    node = runnable_node()
    if node is None:
        pytest.skip("node not runnable on PATH")
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as f:
        f.write(source)
        path = f.name
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True,
                              check=False, timeout=15)
    finally:
        Path(path).unlink(missing_ok=True)
    return proc.returncode, (proc.stderr or proc.stdout)


def test_sw_is_valid_js() -> None:
    code, out = _node_check(SW.read_text(), ".js")
    assert code == 0, out


def test_swap_page_script_is_valid_js() -> None:
    import re

    html = (WEB / "lab" / "swap.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert scripts, "expected an inline <script> in swap.html"
    # the swap-scene IIFE is the last inline script block
    code, out = _node_check(scripts[-1], ".mjs")
    assert code == 0, out
