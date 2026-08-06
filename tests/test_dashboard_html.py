"""Structural checks on the dashboard's embedded HTML/CSS.

The page lives inside a Python string, so nothing type-checks or lints it. An
unbalanced CSS comment does not raise — it silently swallows the rules that
follow, which is exactly how a bidi fix once disappeared without a trace.
"""

import re

import pytest

from stock_monitor.dashboard import _HTML

CSS = _HTML.split("<style>", 1)[1].split("</style>", 1)[0]


# ── CSS integrity ─────────────────────────────────────────────────────────────

def test_css_comments_are_balanced():
    assert CSS.count("/*") == CSS.count("*/")


def test_no_comment_prose_leaks_into_the_stylesheet():
    """A stray line without braces or a semicolon means a comment broke open."""
    stripped = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert "/*" not in stripped and "*/" not in stripped
    leaked = [
        line for line in stripped.splitlines()
        if line.strip() and not re.search(r"[{};,]|^\s*}|^\s*@", line)
    ]
    assert leaked == [], f"comment text leaked into CSS: {leaked}"


def test_braces_are_balanced():
    stripped = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert stripped.count("{") == stripped.count("}")


# ── RTL ───────────────────────────────────────────────────────────────────────

def test_the_page_declares_hebrew_and_rtl():
    assert '<html lang="he" dir="rtl">' in _HTML


def test_no_hardcoded_left_right_margins_or_padding():
    """Logical properties only, so the layout cannot drift out of sync with dir."""
    stripped = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    physical = re.findall(r"(?:margin|padding)-(?:left|right)\s*:", stripped)
    assert physical == [], f"use margin/padding-inline-* instead: {physical}"


def test_numbers_are_bidi_isolated():
    """money() and pctCell() must emit <bdi>, or '-$134.00' flips next to Hebrew."""
    assert "'<bdi>' + (n < 0 ? '-$' : '$')" in _HTML
    assert '<bdi class="${cls}">${sign}${val.toFixed(2)}%</bdi>' in _HTML


def test_timestamps_are_pinned_to_ltr():
    """Their only strong character is at the end, so auto-detection flips them."""
    assert "#server-time, #build-label, .alert-ts { direction: ltr;" in CSS


@pytest.mark.parametrize("selector", ["th", ".entry-stats b", ".summary b"])
def test_single_string_cells_auto_detect_direction(selector):
    rule = CSS.split("unicode-bidi: plaintext")[0].rsplit("*/", 1)[-1]
    assert selector in rule


def test_table_cells_are_not_plaintext():
    """td must follow the page, so the first row action stays rightmost."""
    rule = CSS.split("unicode-bidi: plaintext")[0].rsplit("*/", 1)[-1]
    assert not re.search(r"(^|[\s,])td([\s,{]|$)", rule)


def test_no_leftover_per_string_rtl_patches():
    """These existed only to survive an LTR page; dir=rtl makes them redundant.

    The <html> tag is of course exempt — it is what makes them redundant.
    """
    body = _HTML.split("<body>", 1)[1]
    assert 'dir="rtl"' not in body
    assert "direction: rtl" not in CSS


# ── Tabs and panels stay in sync ──────────────────────────────────────────────

def test_every_tab_has_a_panel():
    tabs = set(re.findall(r'class="tab[^"]*" data-panel="(\w+)"', _HTML))
    panels = set(re.findall(r'id="panel-(\w+)"', _HTML))
    assert tabs == panels, f"tabs {tabs ^ panels} have no matching panel"


def test_all_six_tabs_are_present():
    tabs = re.findall(r'data-panel="(\w+)"', _HTML)
    assert tabs == ["live", "portfolio", "journal", "news", "atr", "sector"]


# ── The stale-feed banner ─────────────────────────────────────────────────────

def test_the_stale_feed_banner_is_rendered_not_just_defined():
    """A helper nobody calls is how the last silent-failure mode looked."""
    assert "function staleFeedNote(" in _HTML
    assert "staleFeedNote(data)" in _HTML.replace("function staleFeedNote(data)", "")


def test_the_banner_reads_the_portfolio_level_lag_field():
    body = _HTML.split("function staleFeedNote(", 1)[1].split("\n}", 1)[0]
    assert "feed_lag_days" in body
    assert "latest_bar_date" in body, "the date it stopped at has to be named"


def test_a_current_feed_renders_no_banner():
    body = _HTML.split("function staleFeedNote(", 1)[1].split("\n}", 1)[0]
    assert "if (!lag) return ''" in body


def test_the_as_of_label_turns_red_when_the_feed_is_behind():
    """Grey subtext is what let a day-old portfolio pass for a live one."""
    header = _HTML.split("const asOf =", 1)[1].split(";", 1)[0]
    assert "feed_lag_days" in header
    assert "down" in header
