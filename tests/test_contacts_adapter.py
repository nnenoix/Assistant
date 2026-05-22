"""Unit tests for src/tools/contacts.py (Google People API)."""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import contacts


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(contacts, "_service", return_value=svc):
        yield svc


def test_search_flattens_results(fake_service):
    fake_service.people().searchContacts().execute.return_value = {
        "results": [
            {"person": {
                "resourceName": "people/c1",
                "names": [{"displayName": "Иван Петров", "givenName": "Иван", "familyName": "Петров"}],
                "emailAddresses": [{"value": "ivan@x.com"}],
                "phoneNumbers": [{"value": "+7..."}],
            }},
        ],
    }
    result = contacts.search("Иван")
    assert result["_meta"]["count"] == 1
    c = result["contacts"][0]
    assert c["display_name"] == "Иван Петров"
    assert c["emails"] == ["ivan@x.com"]
    assert c["phones"] == ["+7..."]


def test_search_empty_flag(fake_service):
    fake_service.people().searchContacts().execute.return_value = {"results": []}
    result = contacts.search("nobody")
    assert result["_meta"]["empty_reason"] == "no_matches"


def test_get_returns_flat_dict(fake_service):
    fake_service.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "names": [{"displayName": "Test"}],
        "emailAddresses": [{"value": "t@x.com"}],
    }
    result = contacts.get("people/c1")
    assert result["display_name"] == "Test"
    assert result["emails"] == ["t@x.com"]
    assert result["_meta"]["resource_name"] == "people/c1"


def test_list_all_returns_meta_truncated(fake_service):
    fake_service.people().connections().list().execute.return_value = {
        "connections": [{"resourceName": "people/c1", "names": [{"displayName": "A"}]}],
        "nextPageToken": "tok_xxx",
    }
    result = contacts.list_all(max_results=1)
    assert result["_meta"]["truncated"] is True
    assert "more results" in result["_meta"]["truncation_reason"]


def test_create_builds_body(fake_service):
    fake_service.people().createContact().execute.return_value = {
        "resourceName": "people/c_new",
        "names": [{"displayName": "Test"}],
    }
    contacts.create("Test", family_name="One", emails=["t@x.com"], organization="ACME")
    body = fake_service.people().createContact.call_args.kwargs["body"]
    assert body["names"] == [{"givenName": "Test", "familyName": "One"}]
    assert body["emailAddresses"] == [{"value": "t@x.com"}]
    assert body["organizations"] == [{"name": "ACME"}]


def test_delete_contact(fake_service):
    fake_service.people().deleteContact().execute.return_value = None
    result = contacts.delete("people/c_target")
    fake_service.people().deleteContact.assert_called_with(resourceName="people/c_target")
    assert result["ok"]
