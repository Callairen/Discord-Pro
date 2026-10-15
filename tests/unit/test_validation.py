import pytest
from pydantic import ValidationError

from discord_pro.backend.activity import Activity, ActivityButton, validate_client_id


def test_valid_activity_to_kwargs():
    a = Activity(details="Coding", state="VS Code", large_image="vscode", large_text="Writing",
                 buttons=[{"label": "Site", "url": "https://example.com"}])
    kw = a.to_rpc_kwargs(start=1000)
    assert kw == {"activity_type": 0, "details": "Coding", "state": "VS Code", "large_image": "vscode",
                  "large_text": "Writing", "buttons": [{"label": "Site", "url": "https://example.com"}],
                  "start": 1000}


def test_omitted_fields_not_sent_and_elapsed_optional():
    kw = Activity(state="Only state", show_elapsed=False).to_rpc_kwargs(start=5)
    assert kw == {"activity_type": 0, "state": "Only state"}


@pytest.mark.parametrize("field,value", [
    ("details", "x"),            # too short
    ("details", "y" * 129),      # too long
    ("state", "z" * 200),
    ("large_text", "a"),
    ("large_image", "has space"),
    ("large_image", "javascript:alert(1)"),
    ("small_image", "ftp://x.y/z.png"),
])
def test_invalid_fields(field, value):
    with pytest.raises(ValidationError):
        Activity(**{field: value})


def test_text_is_trimmed_and_control_chars_removed():
    a = Activity(details="  Hello\x00World  ")
    assert a.details == "Hello World"


def test_boundaries_accepted():
    Activity(details="ab", state="c" * 128, large_image="https://example.com/" + "a" * 200)


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "ftp://example.com", "https://", "https://user:pw@example.com",
    "https://exa mple.com", "https://example.com/" + "a" * 600, "",
])
def test_bad_button_urls(url):
    with pytest.raises(ValidationError):
        ActivityButton(label="ok", url=url)


def test_button_limits():
    with pytest.raises(ValidationError):
        ActivityButton(label="x" * 33, url="https://a.b")
    with pytest.raises(ValidationError):
        ActivityButton(label="", url="https://a.b")
    with pytest.raises(ValidationError):
        Activity(buttons=[{"label": "a", "url": "https://a.b"}] * 3)
    Activity(buttons=[{"label": "x" * 32, "url": "https://a.b"}] * 2)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        Activity(details="ok", party_id="nope")


def test_small_tooltip_requires_image():
    with pytest.raises(ValidationError):
        Activity(small_text="tooltip")


@pytest.mark.parametrize("value,ok", [
    ("123456789012345678", True), ("1468102806786281596", True), ("", True),
    ("12345", False), ("abc123456789012345", False), ("123456789012345678901", False),
])
def test_client_id(value, ok):
    if ok:
        assert validate_client_id(value) == value
    else:
        with pytest.raises(ValueError):
            validate_client_id(value)


def test_has_content():
    assert not Activity().has_content()
    assert Activity(large_image="logo").has_content()
