import pytest

from app.utils import canonicalize_url, is_safe_relative_url


def test_job_url_canonicalization_preserves_functional_parameters():
    result = canonicalize_url(
        "HTTPS://Jobs.Example.com//apply/?gh_jid=123&ref=campus&utm_source=newsletter#top"
    )

    assert result == "https://jobs.example.com/apply?gh_jid=123&ref=campus"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/jobs?work_mode=remote", True),
        ("https://evil.example", False),
        ("//evil.example/path", False),
        ("/\\evil.example", False),
        ("/jobs\nLocation: https://evil.example", False),
        (None, False),
    ],
)
def test_safe_relative_urls(target, expected):
    assert is_safe_relative_url(target) is expected


def test_invalid_job_url_is_rejected():
    with pytest.raises(ValueError):
        canonicalize_url("javascript:alert(1)")

    with pytest.raises(ValueError):
        canonicalize_url("http://127.0.0.1/private-job")
