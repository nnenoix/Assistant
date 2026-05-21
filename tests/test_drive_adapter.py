from unittest.mock import MagicMock, patch

import pytest

from src.tools import drive


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(drive, "_service", return_value=svc):
        yield svc


def test_list_files_passes_query(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "1", "name": "f"}]}
    result = drive.list_files(folder_id="ROOT", query=None)
    fake_service.files().list.assert_called_with(
        q="'ROOT' in parents and trashed = false",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=50,
    )
    assert result["files"] == [{"id": "1", "name": "f"}]
    assert result["_meta"]["truncated"] is False
    assert result["_meta"]["empty_reason"] is None


def test_list_files_truncated_flagged_on_next_page_token(fake_service):
    fake_service.files().list().execute.return_value = {
        "files": [{"id": "1"}], "nextPageToken": "tok",
    }
    result = drive.list_files(folder_id="ROOT")
    assert result["_meta"]["truncated"] is True
    assert "more results available" in result["_meta"]["truncation_reason"]


def test_list_files_with_extra_query(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.list_files(folder_id="ROOT", query="name contains 'report'", page_size=10)
    fake_service.files().list.assert_called_with(
        q="'ROOT' in parents and trashed = false and (name contains 'report')",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=10,
    )


def test_list_files_page_size_clamped(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.list_files(folder_id="ROOT", page_size=500)
    args = fake_service.files().list.call_args.kwargs
    assert args["pageSize"] == 200  # clamped at upper bound


def test_list_shared_with_me(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "S1", "name": "shared sheet"}]}
    result = drive.list_shared_with_me()
    fake_service.files().list.assert_called_with(
        q="sharedWithMe = true and trashed = false",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime,owners(emailAddress,displayName))",
        orderBy="modifiedTime desc",
        pageSize=50,
    )
    assert result["files"] == [{"id": "S1", "name": "shared sheet"}]
    assert result["_meta"]["truncated"] is False


def test_create_folder(fake_service):
    fake_service.files().create().execute.return_value = {"id": "NEW", "name": "X"}
    result = drive.create_folder(parent_id="P", name="X")
    fake_service.files().create.assert_called_with(
        body={"name": "X", "mimeType": "application/vnd.google-apps.folder", "parents": ["P"]},
        fields="id,name,mimeType,parents",
    )
    assert result == {"id": "NEW", "name": "X"}


def test_delete(fake_service):
    fake_service.files().delete().execute.return_value = None
    drive.delete(file_id="ABC")
    fake_service.files().delete.assert_called_with(fileId="ABC")


def test_rename(fake_service):
    fake_service.files().update().execute.return_value = {"id": "ABC", "name": "newname"}
    drive.rename(file_id="ABC", new_name="newname")
    fake_service.files().update.assert_called_with(
        fileId="ABC", body={"name": "newname"}, fields="id,name"
    )


def test_move(fake_service):
    fake_service.files().get().execute.return_value = {"parents": ["OLD"]}
    fake_service.files().update().execute.return_value = {"id": "ABC", "parents": ["NEW"]}
    drive.move(file_id="ABC", new_parent_id="NEW")
    fake_service.files().update.assert_called_with(
        fileId="ABC",
        addParents="NEW",
        removeParents="OLD",
        fields="id,parents",
    )


def test_search(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "1"}]}
    drive.search("foo bar")
    fake_service.files().list.assert_called_with(
        q="name contains 'foo bar' and trashed = false",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents,owners(emailAddress))",
        pageSize=50,
    )


def test_search_escapes_quotes(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.search("user's file")
    fake_service.files().list.assert_called_with(
        q="name contains 'user\\'s file' and trashed = false",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents,owners(emailAddress))",
        pageSize=50,
    )


def test_search_with_mime_shortcut(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.search("idealnight", mime_type="spreadsheet")
    fake_service.files().list.assert_called_with(
        q="name contains 'idealnight' and trashed = false and mimeType = 'application/vnd.google-apps.spreadsheet'",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents,owners(emailAddress))",
        pageSize=50,
    )


def test_search_with_full_mime_string(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.search("plan", mime_type="application/pdf")
    args = fake_service.files().list.call_args.kwargs
    assert "mimeType = 'application/pdf'" in args["q"]


# ---- Phase 13E: account list form ----

def test_search_with_explicit_account_list_uses_aggregation():
    """When `account=["main"]` (a list), the call should aggregate even if
    the list has exactly one item — that's the explicit-list semantic."""
    from unittest.mock import patch as _patch

    # Patch the single-account search path so we can confirm aggregation
    # called us with the right account.
    with _patch("src.tools.drive._service") as mock_svc, \
         _patch("src.tools.drive._aggregate_across_accounts") as mock_agg:
        mock_agg.return_value = {"files": [], "_meta": {"truncated": False}}
        drive.search("foo", account=["main", "elena"])
        assert mock_agg.called
        kwargs = mock_agg.call_args.kwargs
        assert kwargs["accounts"] == ["main", "elena"]


def test_search_with_star_still_works_via_aggregation():
    """Backward-compat: `account='*'` calls aggregation through
    `_resolve_account_arg` resolving to list_accounts()."""
    from unittest.mock import patch as _patch

    with _patch("src.tools.drive._aggregate_across_accounts") as mock_agg, \
         _patch("src.auth.list_accounts", return_value=["main", "elena"]):
        mock_agg.return_value = {"files": [], "_meta": {"truncated": False}}
        drive.search("foo", account="*")
        assert mock_agg.called
        # accounts passed through expands from list_accounts()
        kwargs = mock_agg.call_args.kwargs
        assert kwargs["accounts"] == ["main", "elena"]


def test_search_with_single_string_account_stays_single_path():
    """`account="main"` (plain alias) → single-account call, no aggregation."""
    from unittest.mock import MagicMock as _MagicMock, patch as _patch

    fake = _MagicMock()
    fake.files().list().execute.return_value = {"files": [{"id": "1"}]}
    with _patch("src.tools.drive._service", return_value=fake), \
         _patch("src.tools.drive._aggregate_across_accounts") as mock_agg:
        drive.search("foo", account="main")
        assert not mock_agg.called  # single path


def test_aggregate_dedupes_duplicate_account_aliases():
    """Passing ['main', 'main'] should run once, not twice."""
    from unittest.mock import patch as _patch, MagicMock as _MagicMock

    fake = _MagicMock()
    fake.files().list().execute.return_value = {"files": [{"id": "1", "name": "x"}]}
    with _patch("src.tools.drive._service", return_value=fake):
        result = drive.search("foo", account=["main", "main", "main"])
    # Only one account in metadata (deduplicated)
    assert result["_meta"]["accounts_searched"] == ["main"]
    assert result["_meta"]["per_account_counts"] == {"main": 1}


def test_empty_list_account_falls_through_to_single_default():
    """`account=[]` → empty list treated as None → falls back to default
    single-account path (backward compat with the assumption that lists
    are explicit)."""
    from unittest.mock import patch as _patch, MagicMock as _MagicMock

    fake = _MagicMock()
    fake.files().list().execute.return_value = {"files": []}
    with _patch("src.tools.drive._service", return_value=fake):
        result = drive.search("foo", account=[])
    # Single-account-path output (no accounts_searched key)
    assert "accounts_searched" not in result["_meta"]


def test_list_files_with_account_list():
    from unittest.mock import patch as _patch

    with _patch("src.tools.drive._aggregate_across_accounts") as mock_agg:
        mock_agg.return_value = {"files": [], "_meta": {"truncated": False}}
        drive.list_files(folder_id="ROOT", account=["main", "elena"])
        assert mock_agg.called
        assert mock_agg.call_args.kwargs["accounts"] == ["main", "elena"]


def test_name_patterns_with_account_list():
    from unittest.mock import patch as _patch

    # name_patterns has its own multi-account path (not via _aggregate_across_accounts)
    # — verify it dispatches to that path on a list.
    with _patch("src.tools.drive.search") as mock_search:
        mock_search.return_value = {"files": [], "_meta": {}}
        result = drive.name_patterns("idealnight", account=["main", "elena"])
        # Should call search once per account
        assert mock_search.call_count == 2
        called_accounts = {c.kwargs.get("account") for c in mock_search.call_args_list}
        assert called_accounts == {"main", "elena"}


# ---------- bug_016 lockdown: per-account errors surface, not swallowed ----------

def test_aggregate_surfaces_per_account_errors():
    """REGRESSION: previously per-account exceptions were silently turned into
    `per_account_counts[acct] = -1` with no other signal. Rule 23 says errors
    must be visible — agents and users need to know results are partial.
    """
    from unittest.mock import patch as _patch

    def fake_search(query, account=None, **kwargs):
        if account == "broken":
            raise PermissionError("token expired")
        return {"files": [{"id": "f1", "name": "ok.txt"}], "_meta": {}}

    with _patch("src.tools.drive.search", side_effect=fake_search):
        result = drive._aggregate_across_accounts(
            "search", "q", accounts=["main", "broken", "elena"],
        )

    meta = result["_meta"]
    assert "errors" in meta
    assert meta["error_count"] == 1
    assert meta["errors"][0]["account"] == "broken"
    assert meta["errors"][0]["kind"] == "PermissionError"
    assert "token expired" in meta["errors"][0]["message"]
    assert "warning" in meta
    # Failed account flagged with -1 count, others have real counts
    assert meta["per_account_counts"] == {"main": 1, "broken": -1, "elena": 1}


def test_aggregate_no_errors_field_when_all_succeed():
    """Clean runs don't pollute _meta with empty errors arrays."""
    from unittest.mock import patch as _patch

    def fake_search(query, account=None, **kwargs):
        return {"files": [], "_meta": {}}

    with _patch("src.tools.drive.search", side_effect=fake_search):
        result = drive._aggregate_across_accounts(
            "search", "q", accounts=["main", "elena"],
        )

    meta = result["_meta"]
    assert "errors" not in meta
    assert "warning" not in meta
    assert meta["per_account_counts"] == {"main": 0, "elena": 0}


# ---- Phase 4: permissions ----

def test_list_permissions_returns_meta(fake_service):
    fake_service.permissions().list().execute.return_value = {
        "permissions": [
            {"id": "p1", "type": "user", "role": "writer", "emailAddress": "a@b.com"},
        ],
    }
    result = drive.list_permissions("F1")
    assert result["_meta"]["count"] == 1
    assert result["permissions"][0]["role"] == "writer"


def test_list_permissions_empty_flag(fake_service):
    fake_service.permissions().list().execute.return_value = {"permissions": []}
    result = drive.list_permissions("F1")
    assert result["_meta"]["empty_reason"] == "no_permissions"


def test_share_rejects_unknown_role(fake_service):
    with pytest.raises(ValueError, match="unknown role"):
        drive.share("F1", "a@b.com", role="superuser")


def test_share_user_writer(fake_service):
    fake_service.permissions().create().execute.return_value = {
        "id": "p9", "emailAddress": "a@b.com", "role": "writer",
    }
    result = drive.share("F1", "a@b.com", role="writer", notify=False)
    call_kwargs = fake_service.permissions().create.call_args.kwargs
    assert call_kwargs["fileId"] == "F1"
    assert call_kwargs["body"] == {"type": "user", "role": "writer", "emailAddress": "a@b.com"}
    assert call_kwargs["sendNotificationEmail"] is False
    assert result["permission_id"] == "p9"
    assert result["role"] == "writer"


def test_share_with_message_requires_notify(fake_service):
    """`message` only carried through when notify=True."""
    fake_service.permissions().create().execute.return_value = {"id": "p", "emailAddress": "x", "role": "reader"}
    drive.share("F1", "x@y.com", role="reader", notify=True, message="hi please review")
    call_kwargs = fake_service.permissions().create.call_args.kwargs
    assert call_kwargs.get("emailMessage") == "hi please review"


def test_revoke_permission_calls_delete(fake_service):
    fake_service.permissions().delete().execute.return_value = None
    drive.revoke_permission("F1", "perm-123")
    fake_service.permissions().delete.assert_called_with(
        fileId="F1", permissionId="perm-123", supportsAllDrives=True,
    )


def test_transfer_ownership_sets_transfer_flag(fake_service):
    fake_service.permissions().create().execute.return_value = {
        "id": "p1", "emailAddress": "new@owner.com", "pendingOwner": True,
    }
    result = drive.transfer_ownership("F1", "new@owner.com")
    call_kwargs = fake_service.permissions().create.call_args.kwargs
    assert call_kwargs["transferOwnership"] is True
    assert call_kwargs["body"]["role"] == "owner"
    assert result["pending_owner"] is True


# ---- Phase 4: revisions ----

def test_list_revisions_returns_meta(fake_service):
    fake_service.revisions().list().execute.return_value = {
        "revisions": [
            {"id": "r1", "modifiedTime": "2026-05-19T10:00:00Z"},
            {"id": "r2", "modifiedTime": "2026-05-20T10:00:00Z"},
        ],
    }
    result = drive.list_revisions("F1")
    assert result["_meta"]["count"] == 2


def test_list_revisions_empty_flag(fake_service):
    fake_service.revisions().list().execute.return_value = {"revisions": []}
    result = drive.list_revisions("F1")
    assert result["_meta"]["empty_reason"] == "no_revisions"


# ---- Phase 4: comments ----

def test_add_comment_passes_anchor(fake_service):
    fake_service.comments().create().execute.return_value = {
        "id": "c1", "content": "see B45", "anchor": "{kix.r5}",
    }
    result = drive.add_comment("F1", "see B45", anchor="{kix.r5}")
    call_kwargs = fake_service.comments().create.call_args.kwargs
    assert call_kwargs["body"]["anchor"] == "{kix.r5}"
    assert result["comment_id"] == "c1"


def test_list_comments_default_filters_resolved(fake_service):
    fake_service.comments().list().execute.return_value = {
        "comments": [
            {"id": "c1", "content": "open", "resolved": False},
            {"id": "c2", "content": "done", "resolved": True},
        ],
    }
    result = drive.list_comments("F1")
    assert result["_meta"]["count"] == 1
    assert result["comments"][0]["id"] == "c1"


def test_list_comments_include_resolved(fake_service):
    fake_service.comments().list().execute.return_value = {
        "comments": [
            {"id": "c1", "content": "open", "resolved": False},
            {"id": "c2", "content": "done", "resolved": True},
        ],
    }
    result = drive.list_comments("F1", include_resolved=True)
    assert result["_meta"]["count"] == 2


def test_resolve_comment_uses_replies_action(fake_service):
    fake_service.replies().create().execute.return_value = {"id": "r1", "action": "resolve"}
    result = drive.resolve_comment("F1", "c1")
    call_kwargs = fake_service.replies().create.call_args.kwargs
    assert call_kwargs["fileId"] == "F1"
    assert call_kwargs["commentId"] == "c1"
    assert call_kwargs["body"] == {"action": "resolve"}
    assert result["resolved"] is True
    assert result["reply_id"] == "r1"


# ---- Phase 4: trash ----

def test_list_trash_filters_query(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "T1", "name": "trashed"}]}
    result = drive.list_trash()
    call_kwargs = fake_service.files().list.call_args.kwargs
    assert call_kwargs["q"] == "trashed = true"
    assert result["files"][0]["id"] == "T1"


def test_restore_from_trash_sets_trashed_false(fake_service):
    fake_service.files().update().execute.return_value = {"id": "F1", "name": "x", "trashed": False}
    result = drive.restore_from_trash("F1")
    call_kwargs = fake_service.files().update.call_args.kwargs
    assert call_kwargs["body"] == {"trashed": False}
    assert result["trashed"] is False


def test_empty_trash_calls_api(fake_service):
    fake_service.files().emptyTrash().execute.return_value = None
    result = drive.empty_trash()
    fake_service.files().emptyTrash.assert_called()
    assert "permanently" in result["warning"]
