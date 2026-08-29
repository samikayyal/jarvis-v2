from __future__ import annotations

from email.message import Message
from http.client import BadStatusLine, IncompleteRead
from io import BytesIO
from typing import Any
from urllib.error import HTTPError

import pytest

import jarvis_control_plane.openwa as openwa_module
from jarvis_control_plane import OpenWAHttpError, UrllibOpenWAHttpTransport

from .helpers import _BrokenHttpResponse, _ControlledUrlOpener


def test_urllib_transport_rejects_redirects_before_forwarding_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect_headers = Message()
    redirect_headers["Location"] = "https://attacker.test/collect"
    redirect = HTTPError(
        "http://openwa.test:2785/api/messages",
        302,
        "Found",
        redirect_headers,
        BytesIO(b"redirect rejected"),
    )
    opener = _ControlledUrlOpener(error=redirect)
    installed_handlers: list[Any] = []

    def controlled_build_opener(*handlers: object) -> _ControlledUrlOpener:
        installed_handlers.extend(handlers)
        return opener

    monkeypatch.setattr(
        openwa_module, "build_opener", controlled_build_opener, raising=False
    )

    with pytest.raises(OpenWAHttpError) as raised:
        UrllibOpenWAHttpTransport().request(
            method="POST",
            url="http://openwa.test:2785/api/messages",
            headers={"X-API-Key": "owa_k1_must-not-redirect"},
            body=b"{}",
            timeout_seconds=5.0,
        )

    assert raised.value.code == "redirect_rejected"
    assert raised.value.may_have_sent is True
    assert len(opener.requests) == 1
    assert len(installed_handlers) == 1
    assert installed_handlers[0].redirect_request(None, None, 302, "", {}, "") is None


@pytest.mark.parametrize(
    ("opener_error", "response_error"),
    (
        (BadStatusLine("broken status"), None),
        (None, IncompleteRead(b"partial", 10)),
    ),
)
def test_urllib_transport_maps_protocol_failures_to_ambiguous_post_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    opener_error: Exception | None,
    response_error: Exception | None,
) -> None:
    response = None if response_error is None else _BrokenHttpResponse(response_error)
    opener = _ControlledUrlOpener(response=response, error=opener_error)
    monkeypatch.setattr(
        openwa_module,
        "build_opener",
        lambda *_handlers: opener,
        raising=False,
    )

    with pytest.raises(OpenWAHttpError) as raised:
        UrllibOpenWAHttpTransport().request(
            method="POST",
            url="http://openwa.test:2785/api/messages",
            headers={"X-API-Key": "owa_k1_protocol-failure"},
            body=b"{}",
            timeout_seconds=5.0,
        )

    assert raised.value.code == "invalid_response"
    assert raised.value.may_have_sent is True
    if response is not None:
        assert response.closed is True
