import pytest

from jarvis_personal_runtime.web_search import extract_google_results


@pytest.mark.parametrize("message", ["Checking your browser", "Verify you are human"])
@pytest.mark.parametrize("dialog", [False, True])
def test_challenge_text_blocks_without_captcha_markup(message, dialog):
    html = (
        f'<div role="dialog"><h3>{message}</h3></div><div id="search">'
        '<div class="g"><a href="https://example.com"><h3>Source</h3></a></div></div>'
        if dialog
        else f"<html><h3>{message}</h3></html>"
    )
    assert extract_google_results(html, 5, 100) == {
        "outcome": "blocked",
        "results": [],
    }


def test_challenge_words_in_organic_result_are_not_an_interstitial():
    html = '<div id="search"><div class="g"><a href="https://example.com"><h3>Checking your browser explained</h3></a></div></div>'
    assert extract_google_results(html, 5, 100)["outcome"] == "ok"


def test_organic_sources_exclude_ads_and_synthesized_panels():
    html = """<div id="search">
      <div id="tads"><div class="g"><a href="https://ads.example/"><h3>Ad</h3></a></div></div>
      <div data-attrid="AIOverview"><div class="g"><a href="https://ai.example/"><h3>AI</h3></a></div></div>
      <div class="MjjYud"><div class="g"><a href="/url?q=https%3A%2F%2Fexample.com%2Fsource&amp;sa=U"><h3>Primary source</h3></a>
        <div class="VwiC3b">Published evidence.</div></div></div>
      <div class="g"><a href="https://127.0.0.1/private"><h3>Private</h3></a></div>
    </div>"""
    result = extract_google_results(html, 5, 1000)
    assert result == {
        "outcome": "ok",
        "results": [
            {
                "title": "Primary source",
                "url": "https://example.com/source",
                "snippet": "Published evidence.",
                "evidence_type": "search_result",
            }
        ],
    }


@pytest.mark.parametrize(
    ("html", "outcome"),
    [
        (
            '<div id="topstuff">Your search - xyz - did not match any documents.</div>',
            "no_results",
        ),
        (
            '<div id="search"><div class="new-layout"><h3>Changed results</h3></div></div>',
            "extraction_failed",
        ),
        ("<html>Something unexpected</html>", "extraction_failed"),
    ],
)
def test_empty_and_unknown_layouts_are_distinct(html, outcome):
    assert extract_google_results(html, 5, 100)["outcome"] == outcome


def test_result_count_text_and_redirect_bounds():
    cards = "".join(
        f'<div class="g"><a href="https://example.com/{i}"><h3>Source {i}</h3></a><div class="VwiC3b">Long evidence</div></div>'
        for i in range(8)
    )
    result = extract_google_results('<div id="search">' + cards + "</div>", 5, 10)
    assert len(result["results"]) == 5
    assert sum(len(r["snippet"]) for r in result["results"]) == 10


@pytest.mark.parametrize(
    "url",
    [
        "/url?q=http%3A%2F%2F127.0.0.1%2F",
        "/url?url=file%3A%2F%2F%2Fetc%2Fpasswd",
        "/url?q=https%3A%2F%2Fuser%3Asecret%40example.com%2F",
        "/search?q=other",
    ],
)
def test_unsafe_redirect_results_are_not_sources(url):
    html = f'<div id="search"><div class="g"><a href="{url}"><h3>Source</h3></a></div></div>'
    assert extract_google_results(html, 5, 100) == {
        "outcome": "extraction_failed",
        "results": [],
    }


def test_changed_layout_mentioning_empty_results_is_not_an_empty_search():
    html = '<div id="search"><div class="unknown"><a href="https://example.com"><h3>Why no results found for certain queries?</h3></a></div></div>'
    assert extract_google_results(html, 5, 100)["outcome"] == "extraction_failed"


@pytest.mark.parametrize("attributes", ["hidden", 'aria-hidden="true"'])
def test_hidden_dialog_does_not_block_organic_results(attributes):
    html = f'<div role="dialog" {attributes}>Feedback</div><div id="search"><div class="g"><a href="https://example.com"><h3>Source</h3></a></div></div>'
    assert extract_google_results(html, 5, 100)["outcome"] == "ok"


def test_opaque_google_redirect_is_preserved_as_returned_evidence():
    html = """<div role="dialog">Date range</div><div id="search">
      <div class="tF2Cxc"><a href="/goto?url=CAESopaque-token"><h3>Example Domains</h3></a>
      <div class="VwiC3b">Domains reserved for documentation.</div></div></div>"""
    assert extract_google_results(html, 5, 1000) == {
        "outcome": "ok",
        "results": [
            {
                "title": "Example Domains",
                "url": "https://www.google.com/goto?url=CAESopaque-token",
                "snippet": "Domains reserved for documentation.",
                "evidence_type": "search_result",
            }
        ],
    }


@pytest.mark.parametrize(
    "marker",
    [
        '<div class="g-recaptcha"></div>',
        '<form action="https://consent.google.com/save"></form>',
        '<div role="dialog" aria-modal="true">Consent</div>',
    ],
)
def test_challenge_still_blocks_pages_with_opaque_result_links(marker):
    html = (
        marker
        + '<div id="search"><div class="tF2Cxc"><a href="/goto?url=token"><h3>Source</h3></a></div></div>'
    )
    assert extract_google_results(html, 5, 1000) == {
        "outcome": "blocked",
        "results": [],
    }


def test_unrecognized_links_with_optional_dialog_are_not_a_challenge():
    html = '<div role="dialog">Date range</div><div id="search"><div class="tF2Cxc"><a href="/unknown?url=token"><h3>Source</h3></a></div></div>'
    assert extract_google_results(html, 5, 1000)["outcome"] == "extraction_failed"


@pytest.mark.parametrize(
    "html",
    [
        # Minimized from the live Arabic Google DOM on the Ubuntu host.
        (
            '<div id="topstuff"><div role="heading">لا توجد نتائج لـ '
            '<b dir="ltr">"nonexistent-phrase"</b>.</div></div><div id="search"></div>'
        ),
        (
            '<div id="search"></div><div id="botstuff"><div class="JPMJ2c">'
            '<p role="heading">لم ينجح بحثك عن <em>nonexistent-phrase</em> '
            "في إظهار أي نتائج.</p></div></div>"
        ),
        (
            '<div id="search"></div><div id="botstuff"><div class="JPMJ2c">'
            '<p role="heading">Your search - nonexistent-phrase - '
            "did not match any documents.</p></div></div>"
        ),
    ],
)
def test_localized_and_bottom_empty_result_messages(html):
    assert extract_google_results(html, 5, 1000) == {
        "outcome": "no_results",
        "results": [],
    }


@pytest.mark.parametrize("recognized", [False, True])
def test_exact_phrase_notice_does_not_hide_unquoted_results(recognized):
    # Google may show unquoted results after saying the exact phrase has none.
    card_class = "g" if recognized else "future-organic-layout"
    html = (
        '<div id="topstuff">No results found for "exact phrase".</div>'
        f'<div id="search"><div class="{card_class}">'
        '<a href="https://example.com"><h3>Unquoted result</h3></a></div></div>'
    )
    assert extract_google_results(html, 5, 1000)["outcome"] == (
        "ok" if recognized else "extraction_failed"
    )


def test_localized_empty_words_in_an_unknown_source_are_not_an_empty_search():
    html = (
        '<div id="search"><div class="future-layout">'
        '<a href="https://example.com"><h3>لا توجد نتائج لـ بحثك</h3></a>'
        "</div></div>"
    )
    assert extract_google_results(html, 5, 1000)["outcome"] == "extraction_failed"
