"""Smoke test and structural verification for showcase.html (Phase 7).

Validates:
1. Presence and non-emptiness of showcase.html.
2. Mandatory section IDs: hero, architecture, simulator, benchmarks, monitors, timeline, roadmap.
3. Presence of empirical benchmark data and PROJECTED_BASED_ON_MOCK_EXECUTION tag.
4. Headless browser execution via Playwright:
   - File loads cleanly without unhandled console errors.
   - Slider updates DOM stat cards and badge.
   - WebArena and OSWorld tables render properly.
   - Canvas element for Chart.js exists.
"""

from __future__ import annotations

import pathlib
import re
import pytest

HTML_PATH = pathlib.Path(__file__).parent.parent / "showcase.html"
EXPLAIN_PATH = pathlib.Path(__file__).parent.parent / "explain.html"
MANDATORY_SECTIONS = [
    "hero",
    "architecture",
    "simulator",
    "benchmarks",
    "monitors",
    "timeline",
    "roadmap",
]


def test_showcase_html_exists_and_structure():
    """Verify that showcase.html exists and contains all required sections and data."""
    assert HTML_PATH.is_file(), f"showcase.html not found at {HTML_PATH}"
    html_content = HTML_PATH.read_text(encoding="utf-8")
    assert len(html_content) > 1000, "showcase.html is suspiciously short"

    # Verify all 7 mandatory sections by ID
    for sec_id in MANDATORY_SECTIONS:
        pattern = rf'id=["\']{sec_id}["\']'
        assert re.search(pattern, html_content, re.IGNORECASE), f"Missing section id='{sec_id}' in showcase.html"

    # Verify required data points from FINAL_RESEARCH_REPORT.md
    assert "99.69%" in html_content or "99.7%" in html_content, "Missing headline cost reduction"
    assert "2.31 ms" in html_content or "2.31ms" in html_content, "Missing reflex step latency"
    assert "152" in html_content, "Missing 152 tests stat"
    assert "PROJECTED_BASED_ON_MOCK_EXECUTION" in html_content, "Missing mock projection status label"

    # Verify monitor efficacy rates
    assert "94.2%" in html_content, "Missing Stuck Monitor 94.2% rate"
    assert "98.1%" in html_content, "Missing Milestone Monitor 98.1% rate"
    assert "100.0%" in html_content or "100%" in html_content, "Missing Session Guard 100% rate"

    # Verify benchmark tables
    assert "WebArena" in html_content
    assert "OSWorld" in html_content


def test_showcase_html_headless_browser():
    """Run Playwright headless browser to verify rendering, slider interaction, and console logs."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright package not installed in environment")

    file_url = HTML_PATH.resolve().as_uri()

    console_errors = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium browser not available on this host: {exc}")

        page = browser.new_page()

        # Capture console error logs
        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text)

        page.on("console", on_console)

        # Navigate to file URL
        page.goto(file_url, wait_until="load")

        # Confirm title
        title = page.title()
        assert "Solari" in title

        # Verify presence of all key section elements in DOM
        for sec_id in MANDATORY_SECTIONS:
            locator = page.locator(f"#{sec_id}")
            assert locator.count() == 1, f"Section #{sec_id} element not found in DOM"
            assert locator.is_visible(), f"Section #{sec_id} is not visible"

        # Verify slider interaction
        slider = page.locator("#step-slider")
        assert slider.count() == 1
        
        badge = page.locator("#slider-value-badge")
        solari_cost = page.locator("#solari-cost-display")
        frontier_cost = page.locator("#frontier-cost-display")

        # Change slider value to 5,000 steps via evaluate
        page.evaluate("""
            const slider = document.getElementById('step-slider');
            slider.value = 5000;
            slider.dispatchEvent(new Event('input'));
        """)

        # Confirm DOM updated
        assert "5,000" in badge.inner_text()
        assert "$241" in frontier_cost.inner_text() or "$241.00" in frontier_cost.inner_text()
        assert "$0.75" in solari_cost.inner_text() or "$0.7520" in solari_cost.inner_text()

        # Verify canvas exists
        canvas = page.locator("canvas#costSimulatorChart")
        assert canvas.count() == 1

        browser.close()

    # Verify no unhandled javascript errors occurred in console
    assert len(console_errors) == 0, f"Encountered unexpected console errors: {console_errors}"


def test_explain_html_headless_browser():
    """Verify explain.html loads, contains ELI5 analogies, and executes cleanly in headless browser."""
    assert EXPLAIN_PATH.is_file(), f"explain.html not found at {EXPLAIN_PATH}"
    content = EXPLAIN_PATH.read_text(encoding="utf-8")
    assert "Hot Stove" in content or "hot stove" in content
    assert "Reflex" in content
    assert "Switzerland" in content

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright package not installed in environment")

    file_url = EXPLAIN_PATH.resolve().as_uri()
    console_errors = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium browser not available on this host: {exc}")

        page = browser.new_page()

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text)

        page.on("console", on_console)
        page.goto(file_url, wait_until="load")

        title = page.title()
        assert "ELI5" in title or "Bang-Motion" in title

        # Verify scenes exist
        assert page.locator("#scene-1").count() == 1
        assert page.locator("#timeline-scrubber").count() == 1
        assert page.locator("#race-trigger-btn").count() == 1

        # Trigger race button
        page.click("#race-trigger-btn")
        page.wait_for_timeout(300)

        # Check that Solari bar completed quickly
        solari_bar = page.locator("#solari-bar")
        assert solari_bar.count() == 1

        browser.close()

    assert len(console_errors) == 0, f"Encountered unexpected console errors in explain.html: {console_errors}"
