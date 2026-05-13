from app.redaction import body_to_log_text, mask_secret, redact_headers, redact_json


def test_mask_secret_keeps_only_suffix():
    assert mask_secret("Bearer sk-abcdefghijkl") == "Bearer ***ijkl"
    assert mask_secret("abc") == "***"


def test_redact_headers():
    headers = {"Authorization": "Bearer sk-12345678", "User-Agent": "pytest"}
    assert redact_headers(headers, {"authorization"}) == {
        "Authorization": "Bearer ***5678",
        "User-Agent": "pytest",
    }


def test_redact_json_nested_values():
    value = {"messages": [{"role": "user", "content": "hi"}], "api_key": "sk-secret-key"}
    redacted = redact_json(value, {"api_key"})
    assert redacted["api_key"] == "***-key"
    assert redacted["messages"][0]["content"] == "hi"


def test_body_to_log_text_redacts_json():
    body = b'{"model":"gpt-4o","token":"abc123456789"}'
    text = body_to_log_text(body, max_bytes=1000, sensitive_json_keys={"token"})
    assert "abc123456789" not in text
    assert "***6789" in text
