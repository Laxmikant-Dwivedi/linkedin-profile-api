import pytest

from app.linkedin_client import _is_self_redirect


@pytest.mark.parametrize(
    "request_url,location,expected",
    [
        # Exact self-redirect (the challenge behavior observed live).
        (
            "https://www.linkedin.com/voyager/api/identity/profiles/jane/profileView",
            "https://www.linkedin.com/voyager/api/identity/profiles/jane/profileView",
            True,
        ),
        # Same target, relative Location header (as LinkedIn sends it).
        (
            "https://www.linkedin.com/feed/",
            "/feed/",
            True,
        ),
        # Trailing-slash-only difference still counts as the same target.
        (
            "https://www.linkedin.com/feed",
            "https://www.linkedin.com/feed/",
            True,
        ),
        # A real redirect elsewhere (e.g. an authwall) is NOT a self-redirect.
        (
            "https://www.linkedin.com/voyager/api/identity/profiles/jane/profileView",
            "https://www.linkedin.com/authwall?trk=bf",
            False,
        ),
        # Different query string on the same path is a different target.
        (
            "https://www.linkedin.com/voyager/api/search/dash/clusters?q=all&query=a",
            "https://www.linkedin.com/voyager/api/search/dash/clusters?q=all&query=b",
            False,
        ),
    ],
)
def test_is_self_redirect(request_url, location, expected):
    assert _is_self_redirect(request_url, location) is expected


def test_is_self_redirect_handles_empty_location():
    assert _is_self_redirect("https://www.linkedin.com/feed/", "") is False
