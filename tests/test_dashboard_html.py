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


def test_the_tabs_are_present_and_in_order():
    tabs = re.findall(r'data-panel="(\w+)"', _HTML)
    assert tabs == ["live", "portfolio", "chat", "journal", "news", "atr", "sector"]


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


# ── Currency rendering ────────────────────────────────────────────────────────

def test_prices_are_rendered_in_their_own_currency():
    """A $ on an agorot figure misreports the price by a factor of 360."""
    assert "const nativeMoney =" in _HTML
    for field in ("p.entry_price", "p.current_price"):
        assert f"nativeMoney({field}, p.currency)" in _HTML, field


def test_no_price_column_still_uses_the_dollar_only_formatter():
    """money() hardcodes a $ — correct for values, wrong for foreign prices."""
    rows = _HTML.split("<tbody>${data.positions.map(", 1)[1].split("</tbody>", 1)[0]
    for field in ("entry_price", "current_price", "extended_price"):
        assert f"money(p.{field})" not in rows, f"{field} bypasses nativeMoney"


def test_agorot_and_shekels_render_differently():
    body = _HTML.split("const nativeMoney =", 1)[1].split("};", 1)[0]
    assert "'ILA'" in body and "'ILS'" in body
    assert "אג" in body, "agorot must be labelled, not shown with a ₪"


def test_values_and_pnl_stay_in_dollars():
    """The totals column is one currency by definition, or it cannot be summed."""
    rows = _HTML.split("<tbody>${data.positions.map(", 1)[1].split("</tbody>", 1)[0]
    assert "money(p.market_value)" in rows
    assert "money(p.pnl_value)" in rows


def test_the_exchange_rate_is_shown_when_it_is_being_applied():
    """A total that moves overnight without a trade is otherwise unexplainable."""
    assert "data.fx_rate" in _HTML
    assert "has_foreign" in _HTML


def test_the_entry_form_says_which_unit_it_wants():
    """The one place the hundredfold mistake actually gets made."""
    assert "function updateCurrencyHint(" in _HTML
    hint = _HTML.split("function updateCurrencyHint(", 1)[1].split("\n}", 1)[0]
    assert ".TA" in hint
    assert "אגורות" in hint


def test_the_unit_hint_is_refreshed_when_the_modal_opens():
    """Editing pre-fills the ticker without firing an input event."""
    modal = _HTML.split("function openModal(", 1)[1].split("\nfunction closeModal", 1)[0]
    assert "updateCurrencyHint()" in modal


# ── The live-monitor tab ──────────────────────────────────────────────────────

def test_the_live_tab_has_no_hardcoded_dollar_signs():
    """A '$' before a Tel Aviv price misreports it by roughly four hundred."""
    rows = _HTML.split("function renderStocks(", 1)[1].split("\n}", 1)[0]
    assert "'$'+" not in rows
    assert "$${s.price" not in rows


def test_live_prices_are_rendered_with_the_symbols_currency():
    rows = _HTML.split("function renderStocks(", 1)[1].split("\n}", 1)[0]
    for field in ("s.price", "s.day_high", "s.day_low"):
        assert f"nativeMoney({field}, s.currency)" in rows, field


def test_a_tel_aviv_row_is_not_labelled_with_a_new_york_session():
    """Teva was tagged Pre-Market at 12:45 ET, hours after TASE had closed."""
    cell = _HTML.split("function sessionCell(", 1)[1].split("\n}", 1)[0]
    assert "'ILA'" in cell and "'ILS'" in cell
    rows = _HTML.split("function renderStocks(", 1)[1].split("\n}", 1)[0]
    assert "sessionCell(s)" in rows
    assert "sessionLabel(s.session)" not in rows, "the raw session label bypasses the check"


# ── Portfolio chat ────────────────────────────────────────────────────────────

def test_the_chat_panel_exists_and_is_wired():
    assert 'id="panel-chat"' in _HTML
    assert "function sendChat(" in _HTML
    assert "chatLoaded = true; loadChat()" in _HTML


def test_chat_messages_are_inserted_as_text_not_markup():
    """Model output and typed history are neither markup nor trusted."""
    body = _HTML.split("function chatBubble(", 1)[1].split("\n}", 1)[0]
    # Comments stripped first: the rule is about what the code does, and the
    # comment explaining the rule naturally names the thing it forbids.
    code = re.sub(r"//.*", "", body)
    assert "textContent" in code
    assert "innerHTML" not in code


def test_the_stream_reader_buffers_partial_frames():
    """A network chunk can split an SSE frame; parsing it half-read throws."""
    body = _HTML.split("async function sendChat(", 1)[1].split("\n}", 1)[0]
    assert "buffer" in body
    assert "frames.pop()" in body


def test_enter_sends_and_shift_enter_does_not():
    body = _HTML.split("function chatKey(", 1)[1].split("\n}", 1)[0]
    assert "shiftKey" in body


def test_the_chat_says_what_it_is_and_is_not():
    assert "לא ייעוץ השקעות" in _HTML


def test_the_chat_warns_that_history_is_not_persisted():
    assert "נמחקת בהפעלה מחדש" in _HTML


def test_the_page_script_is_valid_javascript():
    """The page lives in a non-raw Python string, so escapes are a live hazard.

    A '\\n' written for JavaScript becomes a real newline in the Python literal
    and breaks the JS string it was inside — which kills the entire script tag,
    not just that function. Every button on the page stops working and nothing
    in the HTML looks wrong. Parsing the rendered script is the only check that
    catches it; substring assertions read the Python source, where it looks fine.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available to parse the script")

    script = _HTML.split("<script>")[-1].split("</script>")[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        # Wrapped in a function: the script touches `document` at load, which a
        # syntax check must not execute.
        handle.write("function __page() {\n" + script + "\n}\n")
        path = handle.name

    result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
