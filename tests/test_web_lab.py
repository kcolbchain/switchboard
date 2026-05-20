"""Smoke tests for the agent-payments lab (``web/agents-demo.html``).

The lab is intentionally a no-build single-file frontend, so the test
surface here is correspondingly small but load-bearing:

- the file parses as HTML and contains the structural anchors we depend on
- every scene wired into ``SCENE_ORDER`` has a matching ``SCENES.<id>``
  definition with a name + caption
- the named cast (Patty, Abhi, Tridib) is wired through the canonical
  agent registry
- the home page links to the lab and the lab links back
- the embedded JavaScript has no syntax errors (run via ``node --check``)

Skip the node check if a ``node`` binary isn't on PATH.
"""

from __future__ import annotations

import html.parser
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
LAB = WEB / "agents-demo.html"
HOME = WEB / "index.html"


@pytest.fixture(scope="module")
def lab_html() -> str:
    return LAB.read_text()


@pytest.fixture(scope="module")
def home_html() -> str:
    return HOME.read_text()


@pytest.fixture(scope="module")
def lab_script(lab_html: str) -> str:
    m = re.search(r"<script>(.*?)</script>", lab_html, re.DOTALL)
    assert m, "expected one <script> block in lab HTML"
    return m.group(1)


# ─── shape ────────────────────────────────────────────────────────────────

def test_files_exist() -> None:
    assert LAB.is_file(), f"missing {LAB}"
    assert HOME.is_file(), f"missing {HOME}"


def test_html_parses(lab_html: str) -> None:
    class P(html.parser.HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.errors: list[str] = []

        def error(self, message: str) -> None:    # pragma: no cover
            self.errors.append(message)

    p = P()
    p.feed(lab_html)
    assert not p.errors, p.errors


def test_structural_anchors(lab_html: str) -> None:
    must_contain = [
        '<canvas id="canvas">',
        '<div class="scene-bar" id="sceneBar">',
        '<div class="tt" id="tooltip">',
        'id="sbTitle"',
        'class="sb-char"',
    ]
    for s in must_contain:
        assert s in lab_html, f"missing structural anchor: {s}"


# ─── split-flap title ─────────────────────────────────────────────────────

def test_switchboard_title_letters(lab_html: str) -> None:
    matches = re.findall(r'data-target="([^"]+)"', lab_html)
    # First 11 letters are the title; trailing ones may belong to scenes.
    title_letters = "".join(matches[:11])
    assert title_letters == "switchboard", title_letters


def test_scramble_logic_present(lab_script: str) -> None:
    assert "scrambleSwitchboard" in lab_script
    assert "mouseenter" in lab_script
    assert "settled" in lab_script and "scrambling" in lab_script


# ─── scenes ───────────────────────────────────────────────────────────────

EXPECTED_SCENES = [
    "x402", "escrow", "stream", "auction", "hitl", "fanout", "oracle", "pq", "stack",
]


def test_scene_order_matches(lab_script: str) -> None:
    m = re.search(r"SCENE_ORDER\s*=\s*\[([^\]]+)\]", lab_script)
    assert m, "SCENE_ORDER array not found"
    order = re.findall(r'"([^"]+)"', m.group(1))
    assert order == EXPECTED_SCENES, order


def test_every_scene_has_definition(lab_script: str) -> None:
    for scene_id in EXPECTED_SCENES:
        assert f"SCENES.{scene_id} = {{" in lab_script, f"SCENES.{scene_id} missing"
        # each should have a name and short label
        block_m = re.search(
            rf"SCENES\.{scene_id}\s*=\s*{{[^}}]*?id:\s*\"{scene_id}\".*?name:\s*\"([^\"]+)\".*?short:\s*\"([^\"]+)\"",
            lab_script, re.DOTALL,
        )
        assert block_m, f"scene {scene_id} missing name/short"
        name, short = block_m.group(1), block_m.group(2)
        assert name and short, f"scene {scene_id} has empty name or short"


def test_scene_short_codes_are_zero_padded(lab_script: str) -> None:
    shorts = re.findall(r'short:\s*"(\d+)"', lab_script)
    assert len(shorts) == len(EXPECTED_SCENES), shorts
    for s in shorts:
        assert len(s) == 2 and s.isdigit(), s


# ─── cast ─────────────────────────────────────────────────────────────────

def test_named_cast_includes_team(lab_script: str) -> None:
    for name in ("Patty", "Abhi", "Tridib"):
        assert f'name: "{name}"' in lab_script, f"agent display name {name!r} missing"


def test_no_stale_old_names_in_captions(lab_script: str) -> None:
    """Bob/Eli/Gus should have been swapped out everywhere they were
    rendered to the user."""
    # Inspect only string literals (caption / log / desc), not identifiers.
    rendered = re.findall(r'"((?:[^"\\]|\\.)*)"', lab_script)
    bag = " ".join(rendered)
    for stale in ("Bob ", " Eli ", " Gus ", "Bob's", "Eli's", "Gus's"):
        assert stale not in bag, f"stale name fragment still visible: {stale!r}"


# ─── home page link ───────────────────────────────────────────────────────

def test_home_links_to_lab(home_html: str) -> None:
    assert 'href="./agents-demo.html"' in home_html, "home page missing lab link"


def test_lab_links_back_to_home(lab_html: str) -> None:
    assert 'href="./index.html"' in lab_html


# ─── JS syntax ────────────────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_js_has_no_syntax_errors(lab_script: str) -> None:
    """Run the embedded script through ``node --check``.

    We pass it as ``module`` syntax via a temp ``.mjs`` so top-level returns
    aren't an issue. The script is wrapped in an IIFE so it parses standalone.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(lab_script)
        path = f.name
    try:
        proc = subprocess.run(
            ["node", "--check", path],
            capture_output=True, text=True, check=False, timeout=15,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


# ─── tooltip + hover affordances ──────────────────────────────────────────

def test_tooltip_wired(lab_script: str) -> None:
    assert "renderTooltip" in lab_script
    assert "ttEl.classList.add(\"show\")" in lab_script


def test_hover_ripple_and_scale(lab_script: str) -> None:
    # ripple state + hover ease are both required by the new hover treatment
    assert "_ripples" in lab_script
    assert "_hoverEase" in lab_script
