"""Deep tests for src/tools/social.py (Avito + VK) + src/tools/edo.py (СБИС + Диадок)."""
import json
from unittest.mock import MagicMock, patch
import pytest


def _str_url(req):
    return req if isinstance(req, str) else req.full_url


# ============================================================
# Avito
# ============================================================

def test_avito_auth_posts_client_credentials():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"access_token":"X","expires_in":86400}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = social.avito_auth("clid", "secret")
    assert "grant_type=client_credentials" in captured["data"].decode()
    assert "client_id=clid" in captured["data"].decode()
    assert "/token/" in captured["url"]
    assert out["ok"] is True


def test_avito_auth_handles_401():
    from src.tools import social
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'{"error":"invalid_client"}'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 401, "Unauthorized", {}, fake)):
        out = social.avito_auth("bad", "creds")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 401


def test_avito_self_info_bearer_header():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'{"id":99}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.avito_self_info("TOK")
    assert captured["headers"]["Authorization"] == "Bearer TOK"


def test_avito_user_items_status_filter():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"items":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.avito_user_items("T", 1234, per_page=50, status="removed")
    assert "per_page=50" in captured["url"]
    assert "status=removed" in captured["url"]


def test_avito_balance_url_includes_user_id():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"balance":1000}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.avito_balance("T", 5555)
    assert "/accounts/5555/balance" in captured["url"]


def test_avito_messenger_chats_pagination():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"chats":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.avito_messenger_chats("T", 100, limit=50, offset=200)
    assert "limit=50" in captured["url"]
    assert "offset=200" in captured["url"]


def test_avito_messenger_messages_uses_v3_path():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"messages":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.avito_messenger_messages("T", 100, "CHAT-1")
    assert "/messenger/v3/" in captured["url"]
    assert "/chats/CHAT-1/messages/" in captured["url"]


def test_avito_send_message_dry_run_skips_http():
    from src.tools import social
    with patch("urllib.request.urlopen") as mock_url:
        out = social.avito_send_message("T", 100, "CH", "hi", dry_run=True)
    mock_url.assert_not_called()
    assert out["dry_run"] is True


def test_avito_send_message_post_body_shape():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{"sent":true}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.avito_send_message("T", 100, "CH", "hello")
    assert captured["data"]["message"]["text"] == "hello"
    assert captured["data"]["type"] == "text"


# ============================================================
# VK
# ============================================================

def test_vk_users_get_joins_user_ids():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.vk_users_get("TOK", ["123", "456", "789"])
    assert "user_ids=123%2C456%2C789" in captured["url"] or "user_ids=123,456,789" in captured["url"]


def test_vk_users_get_default_fields():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.vk_users_get("TOK", ["1"])
    assert "city" in captured["url"] and "bdate" in captured["url"]


def test_vk_groups_get_members_pagination():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":{"items":[]}}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.vk_groups_get_members("TOK", "grp1", offset=500, count=500)
    assert "offset=500" in captured["url"]
    assert "count=500" in captured["url"]
    assert "group_id=grp1" in captured["url"]


def test_vk_wall_get_owner_id():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":{}}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.vk_wall_get("TOK", -123)  # group wall
    assert "owner_id=-123" in captured["url"]


def test_vk_wall_post_dry_run_categorizes_user_vs_group():
    from src.tools import social
    out_user = social.vk_wall_post("TOK", 12345, "hi", dry_run=True)
    out_group = social.vk_wall_post("TOK", -12345, "hi", dry_run=True)
    assert out_user["plan"]["wall_type"] == "user"
    assert out_group["plan"]["wall_type"] == "group"


def test_vk_messages_send_dry_run_categorizes_peer():
    from src.tools import social
    out_user = social.vk_messages_send("TOK", 1, "hi", dry_run=True)
    out_chat = social.vk_messages_send("TOK", 2_000_000_001, "hi", dry_run=True)
    out_group = social.vk_messages_send("TOK", -1, "hi", dry_run=True)
    assert out_user["plan"]["peer_kind"] == "user"
    assert out_chat["plan"]["peer_kind"] == "chat"
    assert out_group["plan"]["peer_kind"] == "group"


def test_vk_messages_send_default_random_id_0():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":1}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.vk_messages_send("TOK", 1, "hi")
    assert "random_id=0" in captured["url"]


def test_vk_ads_get_campaigns_endpoint():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.vk_ads_get_campaigns("TOK", 100)
    assert "ads.getCampaigns" in captured["url"]


def test_vk_wall_post_includes_attachments_when_provided():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":{}}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social.vk_wall_post("TOK", -123, "post", attachments="photo123_456")
    assert "attachments=photo123_456" in captured["url"]


def test_vk_call_version_param_included():
    from src.tools import social
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"response":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        social._vk_call("users.get", {"access_token": "T"})
    assert "v=5." in captured["url"]


# ============================================================
# СБИС
# ============================================================

def test_sbis_auth_builds_jsonrpc_envelope():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{"result":{"session":"S123"}}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.sbis_auth("user1", "pass1")
    assert captured["data"]["jsonrpc"] == "2.0"
    assert captured["data"]["method"] == "СБИС.Аутентифицировать"
    assert captured["data"]["params"]["Логин"] == "user1"
    assert captured["data"]["params"]["Пароль"] == "pass1"


def test_sbis_docs_list_adds_session_header():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'{"result":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.sbis_docs_list("SESS-1")
    assert captured["headers"]["X-sbissessionid"] == "SESS-1"


def test_sbis_docs_list_date_filter():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{"result":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.sbis_docs_list("S", from_date="01.05.2026", to_date="31.05.2026", limit=100)
    f = captured["data"]["params"]["Фильтр"]
    assert f["с"] == "01.05.2026"
    assert f["по"] == "31.05.2026"
    assert f["Лимит"] == 100


def test_sbis_doc_get_passes_identifier():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{"result":{}}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.sbis_doc_get("S", "DOC-1")
    assert captured["data"]["params"]["Идентификатор"] == "DOC-1"


def test_sbis_changes_since_method():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{"result":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.sbis_changes_since("S", "2026-05-01T00:00:00+03:00")
    assert captured["data"]["method"] == "СБИС.СписокИзменений"
    assert captured["data"]["params"]["С"] == "2026-05-01T00:00:00+03:00"


def test_sbis_doc_type_filter_defaults_incoming():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        m = MagicMock()
        m.read.return_value = b'{"result":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.sbis_docs_list("S")
    assert captured["data"]["params"]["Тип"] == "ВходящийДокумент"


# ============================================================
# Контур.Диадок
# ============================================================

def test_diadoc_authenticate_password_flow():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        captured["data"] = req.data
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'TOKEN-XYZ'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = edo.diadoc_authenticate("APIKEY", "user1", "pass1")
    assert "type=password" in captured["url"]
    assert "login=user1" in captured["data"].decode()
    assert "password=pass1" in captured["data"].decode()
    assert out["data"]["auth_token"] == "TOKEN-XYZ"


def test_diadoc_my_organizations_auth_header_format():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'{"Organizations":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.diadoc_my_organizations("APIKEY", "AUTHTOK")
    auth = captured["headers"]["Authorization"]
    assert "ddauth_api_client_id=APIKEY" in auth
    assert "ddauth_token=AUTHTOK" in auth


def test_diadoc_docs_list_filter_category_default():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"Documents":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.diadoc_docs_list("APIKEY", "AUTH", "BOX-1")
    assert "filterCategory=Any.Inbound" in captured["url"]


def test_diadoc_docs_list_date_params():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"Documents":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.diadoc_docs_list("APIKEY", "AUTH", "BOX",
                             from_date="01.05.2026", to_date="31.05.2026")
    assert "fromDocumentDate=01.05.2026" in captured["url"]
    assert "toDocumentDate=31.05.2026" in captured["url"]


def test_diadoc_get_event_box_message_params():
    from src.tools import edo
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edo.diadoc_get_event("AK", "AT", "BOX-1", "MSG-1")
    assert "boxId=BOX-1" in captured["url"]
    assert "messageId=MSG-1" in captured["url"]


def test_diadoc_authenticate_handles_400():
    from src.tools import edo
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'wrong creds'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 400, "Bad", {}, fake)):
        out = edo.diadoc_authenticate("AK", "u", "p")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 400
