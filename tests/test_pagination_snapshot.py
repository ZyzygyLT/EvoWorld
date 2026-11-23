"""F7-C: stable snapshot pagination (CONTRACTS §16.3, §16.4, §16.13).

Covers the MANDATORY 20-event example (OP1 seq=21 E1–E7, OP2 seq=22
E8–E13, OP3 seq=23 E14–E20, limit=10), the immutable high-water mark
across concurrent confirmations, and every snapshot_token validation:
signature tampering, expiry, filter-fingerprint change, world binding and
malformed/version-mismatched tokens → 400 (never a 500, never silently
accepted).
"""

from __future__ import annotations

import itertools
import json

import pytest

import worldsim.server.app as appmod

_seq = itertools.count(1)

# E1..E20 of the mandatory example, expressed as their seeded texts.
E = (
    [f"event 21:{i}" for i in range(7)]      # E1–E7  → (21,0)..(21,6)
    + [f"event 22:{i}" for i in range(6)]    # E8–E13 → (22,0)..(22,5)
    + [f"event 23:{i}" for i in range(7)]    # E14–E20→ (23,0)..(23,6)
)


def _register(client) -> dict:
    n = next(_seq)
    r = client.post(
        "/api/auth/register",
        json={"username": f"pg{n}", "password": "pass1234"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_example(env, wid) -> None:
    """Seed OP1/OP2/OP3 of §16.13 with their 20 confirmed events."""
    op1 = env.seed_op(wid, 21, status="confirmed")
    env.seed_events(wid, op1, 21, 7, month=0)
    op2 = env.seed_op(wid, 22, status="confirmed")
    env.seed_events(wid, op2, 22, 6, month=7)
    op3 = env.seed_op(wid, 23, status="confirmed")
    env.seed_events(wid, op3, 23, 7, month=13)


def _get(client, wid, params=None, headers=None):
    return client.get(
        f"/api/worlds/{wid}/events",
        params=params or {},
        headers=headers or {},
    )


# ---------------------------------------------------------------------------
# Mandatory example (§16.13)
# ---------------------------------------------------------------------------


def test_first_page_of_mandatory_example(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)

    body = _get(client, wid, params={"limit": 10}).json()

    # High-water mark fixed at E20 = (23,6).
    assert body["snapshot_high_watermark"] == "23:6"
    # E1–E10: (21,0)..(21,6), (22,0)..(22,2).
    assert [e["event_text"] for e in body["events"]] == E[:10]
    # Continuation points at the LAST DELIVERED event (E10 = (22,2)).
    assert body["next_cursor"] == "22:2"
    assert isinstance(body["snapshot_token"], str)
    assert body["snapshot_token"]
    assert body["coverage"]["status"] == "confirmed"


def test_second_page_of_mandatory_example(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)

    first = _get(client, wid, params={"limit": 10}).json()
    second = _get(
        client, wid,
        params={
            "limit": 10,
            "cursor": first["next_cursor"],
            "snapshot_token": first["snapshot_token"],
        },
    ).json()

    # E11–E20: (22,3)..(22,5), (23,0)..(23,6) — exactly 10 events.
    assert [e["event_text"] for e in second["events"]] == E[10:]
    assert second["next_cursor"] is None  # universe exhausted
    # The navigation is stable: same token, same high-water mark.
    assert second["snapshot_token"] == first["snapshot_token"]
    assert second["snapshot_high_watermark"] == first[
        "snapshot_high_watermark"
    ]


def test_every_page_of_a_navigation_carries_the_same_token(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)

    p1 = _get(client, wid, params={"limit": 7}).json()
    p2 = _get(
        client, wid,
        params={"limit": 7, "cursor": p1["next_cursor"],
                "snapshot_token": p1["snapshot_token"]},
    ).json()
    p3 = _get(
        client, wid,
        params={"limit": 7, "cursor": p2["next_cursor"],
                "snapshot_token": p2["snapshot_token"]},
    ).json()

    assert p1["snapshot_token"] == p2["snapshot_token"] == p3["snapshot_token"]
    assert (
        p1["snapshot_high_watermark"]
        == p2["snapshot_high_watermark"]
        == p3["snapshot_high_watermark"]
        == "23:6"
    )
    # 7 + 7 + 6 = 20 events, no duplicates, in total order.
    texts = [e["event_text"] for e in p1["events"] + p2["events"] + p3["events"]]
    assert texts == E
    assert p3["next_cursor"] is None


# ---------------------------------------------------------------------------
# Concurrent insertion (§16.13)
# ---------------------------------------------------------------------------


def test_concurrent_confirmation_does_not_leak_into_navigation(f7c_api):
    """Between pages OP4 (seq=24, E21–E22) is confirmed: the original
    navigation keeps returning E11–E20 only; a NEW navigation without a
    token fixes a new high-water mark (24,1) and can include E21–E22."""
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)

    first = _get(client, wid, params={"limit": 10}).json()
    assert first["snapshot_high_watermark"] == "23:6"

    # --- concurrent OP4 confirmed while the client holds the token ---
    op4 = env.seed_op(wid, 24, status="confirmed")
    env.seed_events(wid, op4, 24, 2, month=20)  # E21, E22

    second = _get(
        client, wid,
        params={"limit": 10, "cursor": first["next_cursor"],
                "snapshot_token": first["snapshot_token"]},
    ).json()
    # Still E11–E20: the original universe is immutable.
    assert [e["event_text"] for e in second["events"]] == E[10:]
    assert second["next_cursor"] is None
    assert second["snapshot_high_watermark"] == "23:6"

    # A NEW navigation (no token) sees the new high-water mark.
    fresh = _get(client, wid, params={"limit": 100}).json()
    assert fresh["snapshot_high_watermark"] == "24:1"
    texts = [e["event_text"] for e in fresh["events"]]
    assert texts == E + ["event 24:0", "event 24:1"]
    assert fresh["snapshot_token"] != first["snapshot_token"]


def test_new_navigation_can_start_after_the_old_cursor(f7c_api):
    """A navigation started later may begin past the old cursor: the new
    high-water mark simply bounds a bigger universe."""
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)
    op4 = env.seed_op(wid, 24, status="confirmed")
    env.seed_events(wid, op4, 24, 2, month=20)

    body = _get(
        client, wid,
        params={"limit": 10, "cursor": "23:6"},  # no token: new navigation
    ).json()
    assert body["snapshot_high_watermark"] == "24:1"
    assert [e["event_text"] for e in body["events"]] == [
        "event 24:0", "event 24:1"
    ]
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# snapshot_token validation (§16.3 / §16.13) — every failure → 400
# ---------------------------------------------------------------------------


def _first_page(client, wid, **params):
    r = _get(client, wid, params={"limit": 10, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _second_page(client, wid, token, cursor="22:2", **params):
    return _get(
        client, wid,
        params={"limit": 10, "cursor": cursor, "snapshot_token": token,
                **params},
    )


def test_tampered_token_signature_is_400(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)
    token = _first_page(client, wid)["snapshot_token"]

    version, body, sig = token.split(".")
    tampered_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    r = _second_page(client, wid, f"{version}.{body}.{tampered_sig}")
    assert r.status_code == 400


def test_tampered_token_payload_is_400(f7c_api):
    """Flipping a byte of the signed payload breaks the HMAC: never
    silently accepted, never a 500."""
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)
    token = _first_page(client, wid)["snapshot_token"]

    version, body, sig = token.split(".")
    flipped = ("A" if body[0] != "A" else "B") + body[1:]
    r = _second_page(client, wid, f"{version}.{flipped}.{sig}")
    assert r.status_code == 400


def test_expired_token_is_400(f7c_api, monkeypatch):
    """§16.4: tokens carry an expiry (5 min default).  An expired token
    forces the client to start a new navigation without one."""
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)

    monkeypatch.setattr(appmod, "SNAPSHOT_TOKEN_TTL_SECONDS", -1)
    expired = _first_page(client, wid)["snapshot_token"]
    # Restore the contractual TTL (NOT monkeypatch.undo(): that would also
    # revert the fixture's SessionLocal/registry patches).
    monkeypatch.setattr(appmod, "SNAPSHOT_TOKEN_TTL_SECONDS", 300)

    r = _second_page(client, wid, expired)
    assert r.status_code == 400
    assert "snapshot_token" in r.json()["detail"]

    # A fresh navigation (no token) still works afterwards.
    assert _get(client, wid, params={"limit": 10}).status_code == 200


def test_filter_change_between_pages_invalidates_token(f7c_api):
    """The token is bound to the filter fingerprint (month, event_type,
    kingdom_id, limit): changing ANY of them mid-navigation → 400."""
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)
    token = _first_page(client, wid)["snapshot_token"]

    # Different limit.
    r = _get(client, wid, params={
        "limit": 5, "cursor": "22:2", "snapshot_token": token,
    })
    assert r.status_code == 400
    # Different month filter.
    r = _get(client, wid, params={
        "limit": 10, "cursor": "22:2", "snapshot_token": token, "month": 3,
    })
    assert r.status_code == 400
    # Different event_type filter.
    r = _get(client, wid, params={
        "limit": 10, "cursor": "22:2", "snapshot_token": token,
        "event_type": "WAR_DECLARED",
    })
    assert r.status_code == 400
    # Different kingdom filter.
    r = _get(client, wid, params={
        "limit": 10, "cursor": "22:2", "snapshot_token": token,
        "kingdom_id": 4,
    })
    assert r.status_code == 400


def test_token_bound_to_its_world_is_400_elsewhere(f7c_api):
    client, env = f7c_api
    wid_a = env.create_world()
    _seed_example(env, wid_a)
    wid_b = env.create_world(name="b")
    op = env.seed_op(wid_b, 1, status="confirmed")
    env.seed_events(wid_b, op, 1, 3)

    token_a = _first_page(client, wid_a)["snapshot_token"]
    r = _second_page(client, wid_b, token_a, cursor="0:1")
    assert r.status_code == 400


@pytest.mark.parametrize(
    "bad_token",
    [
        "not-a-token",
        "",
        "s1",                       # wrong arity
        "s9.abc.def",               # unknown version
        "s1.!!!not-base64!!.!!!",   # malformed body
        "s1." + "A" * 40 + ".",     # empty signature
    ],
)
def test_malformed_token_is_400_not_500(f7c_api, bad_token):
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)
    r = _second_page(client, wid, bad_token)
    assert r.status_code == 400, r.text


def test_cursor_beyond_high_water_mark_is_empty_page(f7c_api):
    """A cursor past the fixed universe is a valid cut point (the record
    need not exist): the page inside the universe is simply empty."""
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)
    first = _first_page(client, wid)

    body = _second_page(
        client, wid, first["snapshot_token"], cursor="50:0"
    )
    assert body.status_code == 200
    data = body.json()
    assert data["events"] == []
    assert data["next_cursor"] is None
    assert data["snapshot_high_watermark"] == "23:6"
    assert data["snapshot_token"] == first["snapshot_token"]


def test_first_page_never_requires_a_token(f7c_api):
    """The token is ABSENT on the first query by definition: requesting
    the first page with no cursor and no token always starts a navigation."""
    client, env = f7c_api
    wid = env.create_world()
    _seed_example(env, wid)
    r = _get(client, wid, params={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot_token"]
    assert body["next_cursor"] == "22:2"


def test_owner_sees_same_pagination_as_public(f7c_api):
    """Pagination semantics are independent of the projection: the owner
    gets the same cursors, token and high-water mark (plus extra fields)."""
    client, env = f7c_api
    headers = _register(client)
    uid = None  # owner identity comes from the session token
    # Resolve the owner id through the session so the world is really hers.
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    uid = int(me.json()["user_id"])

    wid = env.create_world(visibility="private", owner_user_id=uid)
    _seed_example(env, wid)

    anon = _get(client, wid, params={"limit": 10})
    assert anon.status_code == 404  # private: third party cannot even see it

    mine = _get(client, wid, params={"limit": 10}, headers=headers).json()
    assert mine["next_cursor"] == "22:2"
    assert mine["snapshot_high_watermark"] == "23:6"
    assert [e["event_text"] for e in mine["events"]] == E[:10]
    # Owner projection carries the extra bounded fields.
    assert "kingdom_ids" in mine["events"][0]
