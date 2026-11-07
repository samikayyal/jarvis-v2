from __future__ import annotations

import pytest

from jarvis_personal_runtime.web_evidence import is_access_challenge


@pytest.mark.parametrize(
    ("body", "title", "challenge", "blocked"),
    [
        ("Verify you are human", "", False, True),
        ("Please wait", "Access denied", False, True),
        ("Cloudflare\nChecking your browser...\nPlease wait", "", False, True),
        ("Public article about access denied errors.", "Documentation", False, False),
        ("How to resolve access denied\nUse these steps", "Guide", False, False),
        ("Verify you are human\n" + "Article prose. " * 200, "Guide", False, False),
        ("Public text", "", True, True),
    ],
)
def test_challenge_detection_distinguishes_interstitials_from_prose(
    body, title, challenge, blocked
):
    assert is_access_challenge(body, title, challenge=challenge) is blocked
