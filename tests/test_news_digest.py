"""Hebrew summaries for news items: batching, honesty, caching, degradation.

The news screen now leads with what an item says rather than with a link to it.
Two things have to hold for that to be an improvement: the summaries must come
from the feed's own text and nowhere else, and a missing or broken AI must cost
the summaries and not the screen.
"""

import pytest

from stock_monitor import news, news_digest


class Analyst:
    """Records the calls and answers with whatever it was given."""

    def __init__(self, answer="", error=None):
        self.answer, self.error = answer, error
        self.calls = []

    def summarize_news(self, system_prompt, prompt, max_tokens=1400):
        self.calls.append({"system": system_prompt, "prompt": prompt,
                           "max_tokens": max_tokens})
        if self.error:
            raise self.error
        return self.answer


def item(title, ticker="AVGO", summary="", link=""):
    return {"ticker": ticker, "title": title, "summary": summary,
            "link": link or f"https://example.com/{title.replace(' ', '-')}",
            "publisher": "Reuters", "age_hours": 2.0, "published_at": None}


@pytest.fixture(autouse=True)
def clean_cache():
    news_digest.reset_cache()
    yield
    news_digest.reset_cache()


# ── One call for the whole screen ─────────────────────────────────────────────

def test_a_batch_of_items_costs_one_call():
    """Ten requests for ten headlines would be ten round trips and ten bills."""
    analyst = Analyst("\n".join(f"{n}| סיכום {n}" for n in range(1, 6)))
    items = [item(f"headline {n}") for n in range(5)]

    news_digest.summarize(items, analyst)

    assert len(analyst.calls) == 1


def test_each_summary_lands_on_its_own_item():
    analyst = Analyst("1| ראשון\n2| שני\n3| שלישי")
    items = [item("a"), item("b"), item("c")]

    result = news_digest.summarize(items, analyst)

    assert [i["hebrew"] for i in result] == ["ראשון", "שני", "שלישי"]
    assert [i["title"] for i in result] == ["a", "b", "c"], "order is preserved"


def test_a_long_feed_is_capped():
    analyst = Analyst("1| אחד")
    items = [item(f"headline {n}") for n in range(40)]

    result = news_digest.summarize(items, analyst)

    assert len(result) == news_digest.MAX_ITEMS


def test_the_numbering_is_tolerant_of_a_different_separator():
    """A model that writes "1." has done the job; dropping it would not help."""
    analyst = Analyst("1. ראשון\n2: שני")
    result = news_digest.summarize([item("a"), item("b")], analyst)

    assert [i["hebrew"] for i in result] == ["ראשון", "שני"]


def test_stray_prose_around_the_lines_is_ignored():
    analyst = Analyst("הנה הסיכומים:\n1| ראשון\n\nבהצלחה!")
    assert news_digest.summarize([item("a")], analyst)[0]["hebrew"] == "ראשון"


def test_an_out_of_range_number_is_discarded():
    analyst = Analyst("1| ראשון\n9| המצאה")
    result = news_digest.summarize([item("a")], analyst)

    assert result[0]["hebrew"] == "ראשון"
    assert len(result) == 1


# ── Not inventing anything ────────────────────────────────────────────────────

def test_the_prompt_carries_the_feeds_own_text():
    analyst = Analyst("1| סיכום")
    news_digest.summarize([item("Broadcom lifts guidance", summary="Revenue up 12%.")],
                          analyst)

    prompt = analyst.calls[0]["prompt"]
    assert "Broadcom lifts guidance" in prompt
    assert "Revenue up 12%." in prompt


def test_an_item_with_no_blurb_says_so_in_the_prompt():
    """So the model translates the headline instead of filling in the gap."""
    analyst = Analyst("1| סיכום")
    news_digest.summarize([item("Teva updates timeline")], analyst)

    assert "כותרת בלבד" in analyst.calls[0]["prompt"]


def test_an_item_with_no_blurb_is_flagged_to_the_reader():
    analyst = Analyst("1| כך וכך")
    result = news_digest.summarize(
        [item("with text", summary="Some detail."), item("bare")], analyst
    )

    assert result[0]["headline_only"] is False
    assert result[1]["headline_only"] is True


def test_the_instructions_forbid_adding_facts():
    """The one rule worth testing in the prompt itself."""
    analyst = Analyst("1| סיכום")
    news_digest.summarize([item("a")], analyst)

    system = analyst.calls[0]["system"]
    assert "אל תוסיף" in system
    assert "רק" in system
    assert "ייעוץ" in system, "and no buy/sell advice"


# ── Degrading without taking the screen down ──────────────────────────────────

def test_no_analyst_means_no_summaries_and_no_error():
    result = news_digest.summarize([item("a")], None)

    assert result[0]["hebrew"] == ""
    assert result[0]["title"] == "a", "the caller can still show the headline"


def test_an_api_failure_leaves_the_items_intact():
    analyst = Analyst(error=RuntimeError("credit balance is too low"))
    result = news_digest.summarize([item("a")], analyst)

    assert len(result) == 1 and result[0]["hebrew"] == ""


def test_an_unparseable_answer_leaves_the_items_intact():
    result = news_digest.summarize([item("a")], Analyst("I could not do that."))
    assert result[0]["hebrew"] == ""


def test_a_partial_answer_keeps_what_came_back():
    """Two of three summarised is two more than none."""
    result = news_digest.summarize(
        [item("a"), item("b"), item("c")], Analyst("1| ראשון\n3| שלישי")
    )

    assert [i["hebrew"] for i in result] == ["ראשון", "", "שלישי"]


def test_no_items_asks_nothing():
    analyst = Analyst("1| x")
    assert news_digest.summarize([], analyst) == []
    assert analyst.calls == []


def test_the_original_items_are_not_mutated():
    """The news cache is shared with the dashboard; it must not gain AI text."""
    original = item("a")
    news_digest.summarize([original], Analyst("1| סיכום"))

    assert "hebrew" not in original


# ── Caching ───────────────────────────────────────────────────────────────────

def test_the_same_item_is_not_summarised_twice():
    """Published text does not change, and the screen is re-opened constantly."""
    analyst = Analyst("1| סיכום")
    news_digest.summarize([item("a")], analyst)
    second = news_digest.summarize([item("a")], analyst)

    assert len(analyst.calls) == 1
    assert second[0]["hebrew"] == "סיכום"


def test_only_the_new_items_go_in_the_second_call():
    analyst = Analyst("1| ראשון")
    news_digest.summarize([item("Broadcom guidance")], analyst)

    analyst.answer = "1| שני"
    result = news_digest.summarize(
        [item("Broadcom guidance"), item("Elbit contract")], analyst
    )

    second_prompt = analyst.calls[1]["prompt"]
    assert "Elbit contract" in second_prompt
    assert "Broadcom" not in second_prompt, "the cached item is not re-sent"
    assert [i["hebrew"] for i in result] == ["ראשון", "שני"]


def test_items_are_cached_by_link_not_by_headline():
    """Two outlets run the same wire headline under different links."""
    analyst = Analyst("1| מהמקור הראשון")
    news_digest.summarize([item("same wire", link="https://a.example/1")], analyst)

    analyst.answer = "1| מהמקור השני"
    result = news_digest.summarize([item("same wire", link="https://b.example/2")],
                                   analyst)

    assert result[0]["hebrew"] == "מהמקור השני"


def test_an_item_with_no_link_still_caches_by_title():
    analyst = Analyst("1| סיכום")
    news_digest.summarize([{**item("a"), "link": ""}], analyst)
    news_digest.summarize([{**item("a"), "link": ""}], analyst)

    assert len(analyst.calls) == 1


def test_an_expired_entry_is_re_summarised(monkeypatch):
    analyst = Analyst("1| ישן")
    news_digest.summarize([item("a")], analyst)

    monkeypatch.setattr(news_digest, "CACHE_TTL", -1)
    analyst.answer = "1| חדש"
    assert news_digest.summarize([item("a")], analyst)[0]["hebrew"] == "חדש"


def test_the_cache_does_not_grow_without_bound(monkeypatch):
    monkeypatch.setattr(news_digest, "CACHE_LIMIT", 8)
    analyst = Analyst("1| סיכום")
    for n in range(40):
        news_digest.summarize([item(f"headline {n}")], analyst)

    assert len(news_digest._cache) <= 8


# ── The blurb reaches the stock analysis too ──────────────────────────────────

def test_the_analysis_prompt_carries_the_blurb_not_just_the_headline():
    """A headline says guidance was raised; the blurb says by how much."""
    from stock_monitor.ai_analyst import _news_block

    block = _news_block(news.NewsResult(
        items=[{"title": "Broadcom lifts guidance", "publisher": "Reuters",
                "summary": "Raised the quarterly revenue forecast to $18.2B.",
                "age_hours": 2.0}],
        source="ticker",
    ))

    assert "Broadcom lifts guidance" in block
    assert "$18.2B" in block


def test_a_very_long_blurb_cannot_crowd_out_the_fundamentals():
    from stock_monitor.ai_analyst import _NEWS_BLURB_LIMIT, _news_block

    block = _news_block(news.NewsResult(
        items=[{"title": "T", "summary": "x" * 5000, "age_hours": 1.0}],
        source="ticker",
    ))

    assert block.count("x") <= _NEWS_BLURB_LIMIT


# ── The blurb the summary is built from ───────────────────────────────────────

def test_the_feeds_blurb_is_carried_through_normalization():
    normalized = news._normalize(
        {"content": {"title": "T", "summary": "A paragraph of detail."}}, "AVGO"
    )
    assert normalized["summary"] == "A paragraph of detail."


def test_a_description_counts_as_a_blurb_when_there_is_no_summary():
    normalized = news._normalize(
        {"content": {"title": "T", "description": "Some detail."}}, "AVGO"
    )
    assert normalized["summary"] == "Some detail."


def test_html_is_stripped_from_the_blurb():
    """RSS descriptions arrive wrapped in anchors and paragraph tags."""
    normalized = news._normalize(
        {"title": "T", "description": "<p>Shares <a href='#'>rose</a> 4%.</p>"}, "AVGO"
    )
    assert normalized["summary"] == "Shares rose 4%."


def test_entities_are_decoded_enough_to_read():
    normalized = news._normalize(
        {"title": "T", "description": "Q3&nbsp;results&amp;guidance"}, "AVGO"
    )
    assert normalized["summary"] == "Q3 results&guidance"


def test_a_very_long_blurb_is_bounded():
    """Ten chatty items must not crowd out the prompt."""
    normalized = news._normalize(
        {"title": "T", "description": "word " * 500}, "AVGO"
    )
    assert len(normalized["summary"]) <= news.SUMMARY_LIMIT


def test_an_item_with_no_blurb_normalizes_to_an_empty_string():
    assert news._normalize({"content": {"title": "T"}}, "AVGO")["summary"] == ""


def test_the_rss_source_keeps_the_description(monkeypatch):
    body = (b"<?xml version='1.0'?><rss><channel><item>"
            b"<title>Headline</title><description>The detail.</description>"
            b"</item></channel></rss>")

    class Resp:
        content, status_code = body, 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr(news, "_http_get", lambda url, **kw: Resp())
    assert news._from_rss("AVGO", 3)[0]["description"] == "The detail."
