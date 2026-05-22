"""Deep tests for src/tools/messaging.py + src/tools/payments.py."""
import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch
import pytest


# ============================================================
# messaging.smsru_*
# ============================================================

def test_smsru_send_dry_run_skips_http():
    from src.tools import messaging
    with patch("urllib.request.urlopen") as mock_url:
        out = messaging.smsru_send("ID", "79991234567", "hi", dry_run=True)
    mock_url.assert_not_called()
    assert out["dry_run"] is True
    assert out["plan"]["to"] == "79991234567"


def test_smsru_send_estimates_segments_cyrillic():
    """Cyrillic = 70 chars/segment."""
    from src.tools import messaging
    out = messaging.smsru_send("ID", "79991234567", "x" * 71, dry_run=True)
    assert out["plan"]["estimated_segments"] == 2


def test_smsru_send_includes_optional_from():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{"status":"OK"}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.smsru_send("ID", "79991234567", "hi", from_="ShopName")
    assert "from=ShopName" in captured["url"]


def test_smsru_send_passes_test_flag():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{"status":"OK"}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.smsru_send("ID", "79991234567", "hi", test=1)
    assert "test=1" in captured["url"]


def test_smsru_balance_uses_balance_endpoint():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{"balance":"100.00"}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.smsru_balance("ID")
    assert "/my/balance" in captured["url"]


def test_smsru_status_passes_sms_id():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.smsru_status("ID", "SMS123")
    assert "sms_id=SMS123" in captured["url"]


# ============================================================
# messaging.smsc_*
# ============================================================

def test_smsc_send_dry_run_counts_recipients():
    from src.tools import messaging
    out = messaging.smsc_send("L", "P", "+79991234567,+79991234568,+79991234569",
                              "hi", dry_run=True)
    assert out["plan"]["recipient_count"] == 3


def test_smsc_send_dry_run_handles_trailing_comma():
    from src.tools import messaging
    out = messaging.smsc_send("L", "P", "+79991234567,,+79991234568", "hi", dry_run=True)
    assert out["plan"]["recipient_count"] == 2


def test_smsc_send_dry_run_includes_sender():
    from src.tools import messaging
    out = messaging.smsc_send("L", "P", "+79991234567", "hi",
                              sender="MyShop", dry_run=True)
    assert out["plan"]["sender"] == "MyShop"


def test_smsc_balance_url_contains_login():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{"balance":"50.00"}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.smsc_balance("mylogin", "mypass")
    assert "login=mylogin" in captured["url"]


# ============================================================
# messaging.tg_*
# ============================================================

def test_tg_send_message_post_json_body():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{"ok":true}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.tg_send_message("BOT_T", 12345, "hello")
    body = json.loads(captured["data"].decode())
    assert body["chat_id"] == 12345
    assert body["text"] == "hello"
    assert body["disable_web_page_preview"] is True
    assert "bot" in captured["url"].lower()


def test_tg_send_message_parse_mode_optional():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        m = MagicMock()
        m.read.return_value = b'{"ok":true}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.tg_send_message("BOT_T", 1, "hello", parse_mode="HTML")
    body = json.loads(captured["data"].decode())
    assert body["parse_mode"] == "HTML"


def test_tg_send_message_dry_run_preview():
    from src.tools import messaging
    out = messaging.tg_send_message("BOT", -100, "long " * 60, dry_run=True)
    assert out["dry_run"] is True
    assert out["plan"]["text_length"] > 200
    assert "..." in out["plan"]["text_preview"]


def test_tg_send_message_dry_run_short_text_no_ellipsis():
    from src.tools import messaging
    out = messaging.tg_send_message("BOT", -100, "short", dry_run=True)
    assert out["plan"]["text_preview"] == "short"


def test_tg_send_photo_payload_shape():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        m = MagicMock()
        m.read.return_value = b'{"ok":true}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.tg_send_photo("BOT", 1, "https://img/", caption="cap")
    body = json.loads(captured["data"].decode())
    assert body["photo"] == "https://img/"
    assert body["caption"] == "cap"


def test_tg_get_updates_passes_offset():
    from src.tools import messaging
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        m = MagicMock()
        m.read.return_value = b'{"ok":true,"result":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        messaging.tg_get_updates("BOT", offset=42, timeout=10)
    body = json.loads(captured["data"].decode())
    assert body["offset"] == 42
    assert body["timeout"] == 10


def test_tg_get_me_handles_invalid_token():
    from src.tools import messaging
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'{"ok":false,"description":"Unauthorized"}'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 401, "Unauthorized", {}, fake)):
        out = messaging.tg_get_me("BAD_TOKEN")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 401


# ============================================================
# messaging.imap_*
# ============================================================

def test_imap_recent_returns_error_dict_on_login_fail():
    """If imaplib.IMAP4_SSL.login throws, we return {ok=False, error}."""
    from src.tools import messaging
    with patch("imaplib.IMAP4_SSL") as MockSSL:
        instance = MockSSL.return_value
        instance.login.side_effect = Exception("bad password")
        out = messaging.imap_recent("imap.x.com", 993, "u", "p")
    assert out["ok"] is False
    assert "bad password" in out["error"]


def test_imap_fetch_body_returns_error_dict_on_fetch_fail():
    from src.tools import messaging
    with patch("imaplib.IMAP4_SSL") as MockSSL:
        instance = MockSSL.return_value
        instance.uid.return_value = ("NO", [None])
        out = messaging.imap_fetch_body("imap.x.com", 993, "u", "p", "1")
    assert out["ok"] is False


# ============================================================
# payments.yookassa_*
# ============================================================

def test_yookassa_request_basic_auth_format():
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'{"items":[]}'
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments.yookassa_payments_list("SHOP", "KEY")
    auth = captured["headers"]["Authorization"]
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth[6:]).decode() == "SHOP:KEY"


def test_yookassa_request_uses_idempotence_key_header_if_provided():
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments._yk_request("/p", "S", "K", method="POST", body={}, idempotence_key="key-1")
    assert captured["headers"]["Idempotence-key"] == "key-1"


def test_yookassa_payments_list_passes_status_filter():
    from src.tools import payments
    captured = {}

    def fake(path, s, k, **kw):
        captured["params"] = kw.get("params")
        return {"ok": True, "data": {"items": []}, "_meta": {}}

    with patch.object(payments, "_yk_call", side_effect=fake):
        payments.yookassa_payments_list("S", "K", status="succeeded")
    assert captured["params"]["status"] == "succeeded"


def test_yookassa_payments_list_date_filters_with_iso_keys():
    from src.tools import payments
    captured = {}

    def fake(path, s, k, **kw):
        captured["params"] = kw.get("params")
        return {"ok": True}

    with patch.object(payments, "_yk_call", side_effect=fake):
        payments.yookassa_payments_list("S", "K",
                                         created_gte="2026-05-01T00:00:00Z",
                                         created_lte="2026-05-31T00:00:00Z")
    assert captured["params"]["created_at.gte"] == "2026-05-01T00:00:00Z"
    assert captured["params"]["created_at.lte"] == "2026-05-31T00:00:00Z"


def test_yookassa_payment_get_path_id():
    from src.tools import payments
    captured = {}

    def fake(path, s, k, **kw):
        captured["path"] = path
        return {"ok": True}

    with patch.object(payments, "_yk_call", side_effect=fake):
        payments.yookassa_payment_get("S", "K", "PAYID-99")
    assert "/payments/PAYID-99" in captured["path"]


def test_yookassa_call_classifies_401_permission():
    from src.tools import payments
    with patch.object(payments, "_yk_request", return_value=(401, {}, b'{"type":"unauthorized"}')):
        out = payments._yk_call("/p", "S", "K")
    assert out["error_kind"] == "permission"


def test_yookassa_call_classifies_429_rate_limit():
    from src.tools import payments
    with patch.object(payments, "_yk_request", return_value=(429, {}, b"")):
        out = payments._yk_call("/p", "S", "K")
    assert out["error_kind"] == "rate_limit"


def test_yookassa_call_classifies_500_server():
    from src.tools import payments
    with patch.object(payments, "_yk_request", return_value=(500, {}, b"")):
        out = payments._yk_call("/p", "S", "K")
    assert out["error_kind"] == "server"


def test_yookassa_refunds_list_date_param():
    from src.tools import payments
    captured = {}

    def fake(path, s, k, **kw):
        captured["params"] = kw.get("params")
        return {"ok": True}

    with patch.object(payments, "_yk_call", side_effect=fake):
        payments.yookassa_refunds_list("S", "K", created_gte="2026-05-01T00:00:00Z")
    assert captured["params"]["created_at.gte"] == "2026-05-01T00:00:00Z"


def test_yookassa_payouts_list_limit_param():
    from src.tools import payments
    captured = {}

    def fake(path, s, k, **kw):
        captured["params"] = kw.get("params")
        return {"ok": True}

    with patch.object(payments, "_yk_call", side_effect=fake):
        payments.yookassa_payouts_list("S", "K", limit=200)
    assert captured["params"]["limit"] == 200


def test_yookassa_receipts_list_path():
    from src.tools import payments
    captured = {}

    def fake(path, s, k, **kw):
        captured["path"] = path
        return {"ok": True}

    with patch.object(payments, "_yk_call", side_effect=fake):
        payments.yookassa_receipts_list("S", "K")
    assert captured["path"] == "/receipts"


# ============================================================
# payments.tinkoff_*
# ============================================================

def test_tinkoff_token_deterministic():
    """Sorting + concat + sha256 must produce same token for same input."""
    from src.tools import payments
    captured1 = {}
    captured2 = {}

    def make_fake(captured):
        def fake_urlopen(req, timeout):
            captured["body"] = json.loads(req.data.decode())
            m = MagicMock()
            m.read.return_value = b'{"Success":true}'
            m.status = 200
            m.__enter__ = lambda s: s
            m.__exit__ = lambda s, *a: None
            return m
        return fake_urlopen

    with patch("urllib.request.urlopen", side_effect=make_fake(captured1)):
        payments.tinkoff_get_state("TK", "PWD", "PAYID")
    with patch("urllib.request.urlopen", side_effect=make_fake(captured2)):
        payments.tinkoff_get_state("TK", "PWD", "PAYID")
    assert captured1["body"]["Token"] == captured2["body"]["Token"]


def test_tinkoff_check_order_passes_order_id():
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments.tinkoff_check_order("TK", "PWD", "ORDER-99")
    assert captured["body"]["OrderId"] == "ORDER-99"
    assert captured["body"]["TerminalKey"] == "TK"


def test_tinkoff_get_customer_endpoint():
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments.tinkoff_get_customer("TK", "PWD", "CUST-1")
    assert "/GetCustomer" in captured["url"]


def test_tinkoff_get_terminal_payouts_dates():
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments.tinkoff_get_terminal_payouts("TK", "PWD", "2026-05-01", "2026-05-31")
    assert captured["body"]["From"] == "2026-05-01"
    assert captured["body"]["To"] == "2026-05-31"


def test_tinkoff_post_handles_4xx():
    from src.tools import payments
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'{"Success":false}'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 400, "Bad Request", {}, fake)):
        out = payments.tinkoff_get_state("TK", "PWD", "X")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 400


def test_tinkoff_post_dict_values_serialized():
    """Tinkoff's token computation skips dict/list values per the spec."""
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    # Call with a payload that includes a list — token should be computed
    # excluding the list value (per Tinkoff doc: arrays/objects skipped).
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments._tinkoff_post("TK", "PWD", "/Init", {"Amount": 100, "Items": [{"a": 1}]})
    body = captured["body"]
    # Items wasn't part of the token source per the spec.
    expected_source = "100" + "TK" + "PWD"
    expected_token = hashlib.sha256(expected_source.encode()).hexdigest()
    assert body["Token"] == expected_token
