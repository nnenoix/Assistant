"""Avito + VK API clients (read-only ads / messages / responses).

Avito uses OAuth2 client_credentials. VK uses access_token in query.
Both return JSON; we surface {ok, data, _meta}.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from src.tools._vendor_http import get_json as _get_json, post_json as _post_json


# ============================================================
# Avito API
# ============================================================

_AVITO_BASE = "https://api.avito.ru"


def _avito_auth_uncached(client_id: str, client_secret: str) -> dict:
    """One-shot OAuth fetch — bypass the cache. Used by `avito_auth` and on
    forced refresh after a 401."""
    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_AVITO_BASE}/token/",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode("utf-8")),
                    "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read()[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": e.code}}


def avito_auth(client_id: str, client_secret: str) -> dict:
    """OAuth2 client_credentials → access_token (lifetime ~24h).

    Token is cached via `_vendor_helpers.get_cached_oauth_token`. The
    cache key is `sha256(client_id||client_secret)` — NOT `client_id`
    alone — so a caller that knows the public client_id but a wrong
    client_secret can't ride a cached token: they'd still have to
    present matching credentials to populate (or hit) the cache."""
    import hashlib
    from src.tools._vendor_helpers import get_cached_oauth_token
    account_key = hashlib.sha256(
        f"{client_id}::{client_secret}".encode("utf-8")
    ).hexdigest()[:24]
    return get_cached_oauth_token(
        "avito", account_key,
        lambda: _avito_auth_uncached(client_id, client_secret),
    )


def avito_self_info(token: str) -> dict:
    """Get seller account info via /core/v1/accounts/self."""
    return _get_json(f"{_AVITO_BASE}/core/v1/accounts/self",
                     headers={"Authorization": f"Bearer {token}"})


def avito_user_items(token: str, user_id: int, per_page: int = 100, page: int = 1,
                     status: str = "active") -> dict:
    """List seller's own listings. status: active, removed, old, blocked, rejected."""
    return _get_json(
        f"{_AVITO_BASE}/core/v1/items?per_page={per_page}&page={page}&status={status}",
        headers={"Authorization": f"Bearer {token}"},
    )


def avito_balance(token: str, user_id: int) -> dict:
    """Avito wallet balance."""
    return _get_json(
        f"{_AVITO_BASE}/core/v1/accounts/{user_id}/balance/",
        headers={"Authorization": f"Bearer {token}"},
    )


def avito_messenger_chats(token: str, user_id: int, limit: int = 100, offset: int = 0) -> dict:
    """List Avito messenger chats for a seller."""
    return _get_json(
        f"{_AVITO_BASE}/messenger/v2/accounts/{user_id}/chats?limit={limit}&offset={offset}",
        headers={"Authorization": f"Bearer {token}"},
    )


def avito_messenger_messages(token: str, user_id: int, chat_id: str,
                             limit: int = 100, offset: int = 0) -> dict:
    """Messages in one chat."""
    return _get_json(
        f"{_AVITO_BASE}/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/?limit={limit}&offset={offset}",
        headers={"Authorization": f"Bearer {token}"},
    )


def avito_send_message(token: str, user_id: int, chat_id: str, text: str,
                       dry_run: bool = False) -> dict:
    """Send message in a chat. `dry_run=True` returns a preview without sending."""
    if dry_run:
        return {
            "ok": True, "dry_run": True, "executed": False,
            "plan": {
                "would_call": "avito /messenger/v1/.../messages",
                "user_id": user_id, "chat_id": chat_id,
                "text_length": len(text),
                "text_preview": text[:200] + ("..." if len(text) > 200 else ""),
                "reversibility": "NOT REVERSIBLE — Avito messenger doesn't expose delete-message API.",
            },
            "_meta": {"native_preview": True},
        }
    return _post_json(
        f"{_AVITO_BASE}/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
        body={"message": {"text": text}, "type": "text"},
        headers={"Authorization": f"Bearer {token}"},
    )


# ============================================================
# VK API
# ============================================================

_VK_BASE = "https://api.vk.com/method"
_VK_V = "5.199"


def _vk_call(method: str, params: dict, timeout: int = 30) -> dict:
    params = {**params, "v": _VK_V}
    return _get_json(f"{_VK_BASE}/{method}?" + urllib.parse.urlencode(params))


def vk_users_get(access_token: str, user_ids: list[str], fields: str = "city,bdate,sex") -> dict:
    """Resolve user IDs / screen-names to profile data."""
    return _vk_call("users.get", {
        "access_token": access_token,
        "user_ids": ",".join(str(u) for u in user_ids),
        "fields": fields,
    })


def vk_groups_get_members(access_token: str, group_id: str,
                          offset: int = 0, count: int = 1000) -> dict:
    """List a group's members."""
    return _vk_call("groups.getMembers", {
        "access_token": access_token, "group_id": group_id,
        "offset": offset, "count": count, "sort": "id_desc",
    })


def vk_wall_get(access_token: str, owner_id: int, count: int = 100, offset: int = 0) -> dict:
    """Get wall posts. owner_id negative = group, positive = user."""
    return _vk_call("wall.get", {
        "access_token": access_token, "owner_id": owner_id,
        "count": count, "offset": offset,
    })


def vk_wall_post(access_token: str, owner_id: int, message: str,
                 attachments: str | None = None,
                 dry_run: bool = False) -> dict:
    """Post to wall. `dry_run=True` returns a preview."""
    if dry_run:
        return {
            "ok": True, "dry_run": True, "executed": False,
            "plan": {
                "would_call": "vk.com /method/wall.post",
                "owner_id": owner_id,
                "wall_type": "group" if owner_id < 0 else "user",
                "message_length": len(message),
                "message_preview": message[:200] + ("..." if len(message) > 200 else ""),
                "attachments": attachments,
                "reversibility": "Limited: editable via wall.edit / deletable via wall.delete within VK retention.",
            },
            "_meta": {"native_preview": True},
        }
    params: dict = {"access_token": access_token, "owner_id": owner_id, "message": message}
    if attachments:
        params["attachments"] = attachments
    return _vk_call("wall.post", params)


def vk_messages_send(access_token: str, peer_id: int, message: str,
                     random_id: int = 0, dry_run: bool = False) -> dict:
    """Send a private message. peer_id can be user / chat (2000000000+id) / group (-id).
    `dry_run=True` returns a preview without sending."""
    if dry_run:
        return {
            "ok": True, "dry_run": True, "executed": False,
            "plan": {
                "would_call": "vk.com /method/messages.send",
                "peer_id": peer_id,
                "peer_kind": (
                    "group" if peer_id < 0 else
                    "chat" if peer_id >= 2_000_000_000 else
                    "user"
                ),
                "message_length": len(message),
                "message_preview": message[:200] + ("..." if len(message) > 200 else ""),
                "reversibility": "Limited: messages.edit / messages.delete within VK retention.",
            },
            "_meta": {"native_preview": True},
        }
    return _vk_call("messages.send", {
        "access_token": access_token, "peer_id": peer_id,
        "message": message, "random_id": random_id,
    })


def vk_ads_get_campaigns(access_token: str, account_id: int) -> dict:
    """List VK ad campaigns."""
    return _vk_call("ads.getCampaigns", {
        "access_token": access_token, "account_id": account_id,
    })
