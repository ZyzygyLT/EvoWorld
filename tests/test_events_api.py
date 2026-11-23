"""F7-C: HTTP contract of ``GET /api/worlds/{id}/events`` (CONTRACTS §16.3,
§16.4, §16.6, §16.12).

Covers: the exact check order 404 → 409 → 400, every parameter validation,
the closed privacy allowlists (public vs owner projection, never
``state_json``), visibility gates for private worlds, live coverage states
with their discontinuity metadata, and the empty-navigation short-circuit
(no confirmed event → no token, no high-water mark).
"""

from __future__ import annotations

import itertools
import json

import pytest

import worldsim.server.app as appmod

_seq = itertools.count(1)

PUBLIC_KEYS = {"event_id", "month", "event_type", "event_text", "msg_key"}
OWNER_KEYS = PUBLIC_KEYS | {"kingdom_ids", "event_params"}
TOP_KEYS = {
    "events",
    "next_cursor",
    "snapshot_token",
    "snapshot_high_watermark",
    "coverage",
}


class _FakeResult:
    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail


class _FakeCoord:
    """Stand-in for the lifespan-installed RecoveryCoordinator (Etapa 5)."""

    def __init__(self, table: dict | None = None) -> None:
        self.not_recoverable = table or {}


def _register(client) -> tuple[dict, int]:
    n = next(_seq)
    r = client.post(
        "/api/auth/register",
        json={"username": f"usr{n}", "password": "pass1234"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['token']}"}, int(data["user_id"])


def _get(client, wid, params=None, headers=None):
    return client.get(
        f"/api/worlds/{wid}/events",
        params=params or {},
        headers=headers or {},
    )


def _seed_visible_events(env, wid, *, count=3, kingdom_ids=None):
    op = env.seed_op(wid, 1, status="confirmed")
    env.seed_events(wid, op, 1, count, kingdom_ids=kingdom_ids)
    return op


# ---------------------------------------------------------------------------
# Check order: 404 → 409 → 400 (§16.6)
# ---------------------------------------------------------------------------


def test_unknown_world_is_404_even_with_invalid_params(f7c_api):
    client, env = f7c_api
    r = _get(client, 999999, params={"limit": 0})
    assert r.status_code == 404


def test_private_world_third_party_is_404_even_when_not_recoverable(f7c_api):
    """Visibility gate (404) precedes the DEC16 guard (409): a third party
    cannot learn that a private world exists AND is blocked."""
    client, env = f7c_api
    wid = env.create_world(visibility="private")
    _seed_visible_events(env, wid)
    appmod.app.state.coordinator = _FakeCoord(
        {wid: _FakeResult("not_recoverable", "no checkpoint")}
    )
    assert _get(client, wid).status_code == 404
    # A private world owned by nobody is equally invisible to a stranger.
    other = env.create_world(visibility="private", name="ownerless")
    appmod.app.state.coordinator = _FakeCoord(
        {other: _FakeResult("not_recoverable", "no checkpoint")}
    )
    assert _get(client, other).status_code == 404


def test_not_recoverable_is_409_before_parameter_validation(f7c_api):
    """409 precedes 400: the guard runs before any query validation."""
    client, env = f7c_api
    wid = env.create_world()
    appmod.app.state.coordinator = _FakeCoord(
        {wid: _FakeResult("not_recoverable", "no checkpoint")}
    )
    r = _get(client, wid, params={"limit": 0})
    assert r.status_code == 409
    assert "not recoverable" in r.json()["detail"]
    # And with valid params the guard still applies (no query is served).
    r = _get(client, wid)
    assert r.status_code == 409
    # The record is scoped: another world is unaffected.
    wid2 = env.create_world(name="other")
    assert _get(client, wid2).status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": -5},
        {"limit": "abc"},
        {"cursor": "garbage"},
        {"cursor": "1:2:3"},
        {"cursor": "-1:0"},
        {"cursor": "1:-2"},
        {"month": -1},
        {"event_type": "NOT_AN_EVENT_TYPE"},
        {"kingdom_id": -1},
    ],
)
def test_invalid_parameters_return_400(f7c_api, params):
    client, env = f7c_api
    wid = env.create_world()
    _seed_visible_events(env, wid)
    r = _get(client, wid, params=params)
    assert r.status_code == 400, r.text


def test_valid_parameters_return_200(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    _seed_visible_events(env, wid)
    r = _get(
        client, wid,
        params={
            "limit": 50, "month": 0, "event_type": "BATTLE",
            "kingdom_id": 1,
        },
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Privacy projections (§16.4 allowlists)
# ---------------------------------------------------------------------------


def test_anonymous_on_public_world_gets_public_projection(f7c_api):
    client, env = f7c_api
    wid = env.create_world(visibility="public")
    _seed_visible_events(env, wid, count=2, kingdom_ids=[5])

    body = _get(client, wid).json()
    assert set(body) == TOP_KEYS
    assert len(body["events"]) == 2
    for ev in body["events"]:
        assert set(ev) == PUBLIC_KEYS  # never kingdom_ids/event_params
    # No internal payload leaks anywhere in the response.
    assert "state_json" not in json.dumps(body)
    assert "operation_id" not in json.dumps(body)


def test_authenticated_third_party_on_public_world_still_public(f7c_api):
    """Being logged in is not ownership: a third party sees the same
    bounded projection as an anonymous spectator."""
    client, env = f7c_api
    wid = env.create_world(visibility="public")
    _seed_visible_events(env, wid, count=1, kingdom_ids=[7])
    other_headers, _ = _register(client)

    ev = _get(client, wid, headers=other_headers).json()["events"][0]
    assert set(ev) == PUBLIC_KEYS


def test_owner_gets_owner_projection(f7c_api):
    client, env = f7c_api
    headers, uid = _register(client)
    wid = env.create_world(visibility="private", owner_user_id=uid)
    _seed_visible_events(env, wid, count=1, kingdom_ids=[5])

    ev = _get(client, wid, headers=headers).json()["events"][0]
    assert set(ev) == OWNER_KEYS
    assert ev["kingdom_ids"] == [5]
    assert isinstance(ev["event_params"], dict)


def test_owner_of_public_world_also_gets_owner_projection(f7c_api):
    client, env = f7c_api
    headers, uid = _register(client)
    wid = env.create_world(visibility="public", owner_user_id=uid)
    _seed_visible_events(env, wid, count=1, kingdom_ids=[9])

    ev = _get(client, wid, headers=headers).json()["events"][0]
    assert set(ev) == OWNER_KEYS
    assert ev["kingdom_ids"] == [9]


def test_private_world_owner_ok_third_party_404(f7c_api):
    client, env = f7c_api
    headers, uid = _register(client)
    wid = env.create_world(visibility="private", owner_user_id=uid)
    _seed_visible_events(env, wid)

    assert _get(client, wid, headers=headers).status_code == 200
    assert _get(client, wid).status_code == 404  # anonymous
    other_headers, _ = _register(client)
    assert _get(client, wid, headers=other_headers).status_code == 404


def test_events_of_unconfirmed_operations_are_never_served(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    op_ok = env.seed_op(wid, 1, status="confirmed")
    env.seed_events(wid, op_ok, 1, 2)
    op_pending = env.seed_op(wid, 2, status="pending")
    env.seed_events(wid, op_pending, 2, 5)

    body = _get(client, wid, params={"limit": 100}).json()
    texts = [e["event_text"] for e in body["events"]]
    assert texts == ["event 1:0", "event 1:1"]


# ---------------------------------------------------------------------------
# Coverage metadata (§16.12)
# ---------------------------------------------------------------------------


def test_coverage_unknown_without_operations(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    cov = _get(client, wid).json()["coverage"]
    assert cov["status"] == "unknown"
    assert cov["first_checkpoint_month"] is None
    assert cov["last_checkpoint_month"] is None
    # Metadata present (status ≠ confirmed) but honestly null — no
    # discontinuity is invented where there is no evidence of one.
    assert cov["discontinuity_sequence"] is None
    assert cov["discontinuity_reason"] is None
    assert cov["last_confirmed_sequence"] is None


def test_coverage_confirmed_for_contiguous_confirmed_operations(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    op1 = env.seed_op(wid, 1, status="confirmed")  # checkpoint month 0
    env.seed_events(wid, op1, 1, 2)
    op2 = env.seed_op(wid, 2, status="confirmed")  # checkpoint month 1
    env.seed_events(wid, op2, 2, 3)

    cov = _get(client, wid).json()["coverage"]
    assert cov["status"] == "confirmed"
    assert cov["first_checkpoint_month"] == 0
    assert cov["last_checkpoint_month"] == 1
    # Discontinuity metadata is ONLY for non-confirmed statuses.
    assert "discontinuity_sequence" not in cov
    assert "discontinuity_reason" not in cov
    assert "last_confirmed_sequence" not in cov


def test_coverage_potentially_incomplete_on_canceled_operation(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    op1 = env.seed_op(wid, 1, status="confirmed")
    env.seed_events(wid, op1, 1, 2)
    env.seed_op(wid, 2, status="canceled", continuity_state="released")

    cov = _get(client, wid).json()["coverage"]
    assert cov["status"] == "potentially_incomplete"
    assert cov["discontinuity_sequence"] == 2
    assert cov["discontinuity_reason"] == "canceled"
    assert cov["last_confirmed_sequence"] == 1


def test_coverage_discontinuity_reason_null_for_sequence_hole(f7c_api):
    """A hole has no state reason: the reason field stays null instead of
    being invented (§16.12 never fabricates an explanation)."""
    client, env = f7c_api
    wid = env.create_world()
    env.seed_op(wid, 1, status="confirmed")
    env.seed_op(wid, 3, status="confirmed")  # seq 2 never happened

    cov = _get(client, wid).json()["coverage"]
    assert cov["status"] == "potentially_incomplete"
    assert cov["discontinuity_sequence"] == 2
    assert cov["discontinuity_reason"] is None
    assert cov["last_confirmed_sequence"] == 1


def test_coverage_confirmed_without_checkpoint_reason_null(f7c_api):
    """``confirmed`` without its T2 checkpoint breaks the atomicity proof:
    detected as a data inconsistency, reason honestly null."""
    client, env = f7c_api
    wid = env.create_world()
    env.seed_op(wid, 1, status="confirmed", with_checkpoint=False)

    cov = _get(client, wid).json()["coverage"]
    assert cov["status"] == "potentially_incomplete"
    assert cov["discontinuity_sequence"] == 1
    assert cov["discontinuity_reason"] is None
    assert cov["last_confirmed_sequence"] is None


# ---------------------------------------------------------------------------
# Empty navigation (§16.4)
# ---------------------------------------------------------------------------


def test_no_confirmed_events_returns_empty_page_without_token(f7c_api):
    """No confirmed event visible → no events, no token, no high-water
    mark: a navigation cannot start from nothing."""
    client, env = f7c_api
    wid = env.create_world()
    op = env.seed_op(wid, 1, status="pending")
    env.seed_events(wid, op, 1, 4)  # exist durably, but unconfirmed

    body = _get(client, wid).json()
    assert body["events"] == []
    assert body["next_cursor"] is None
    assert body["snapshot_token"] is None
    assert body["snapshot_high_watermark"] is None
    assert body["coverage"]["status"] == "potentially_incomplete"
    assert body["coverage"]["discontinuity_reason"] == "pending"


def test_world_without_any_data_is_empty_not_error(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    body = _get(client, wid).json()
    assert body["events"] == []
    assert body["snapshot_token"] is None
    assert body["snapshot_high_watermark"] is None
    assert body["coverage"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# Filters through HTTP (§16.3)
# ---------------------------------------------------------------------------


def test_month_filter_through_http(f7c_api):
    client, env = f7c_api
    wid = env.create_world()
    op = env.seed_op(wid, 1, status="confirmed")
    env.seed_events(wid, op, 1, 3, month=5)  # months 5, 6, 7

    assert len(_get(client, wid, params={"month": 5}).json()["events"]) == 1
    assert len(_get(client, wid, params={"month": 6}).json()["events"]) == 1
    assert _get(client, wid, params={"month": 99}).json()["events"] == []


def test_kingdom_filter_through_http_is_structural(f7c_api):
    """json_each membership: kingdom 1 must not match [10] or [21]."""
    client, env = f7c_api
    wid = env.create_world()
    op = env.seed_op(wid, 1, status="confirmed")
    env.seed_events(wid, op, 1, 1, kingdom_ids=[1])
    env.seed_events(wid, op, 1, 1, kingdom_ids=[10])
    env.seed_events(wid, op, 1, 1, kingdom_ids=[21])

    texts = [
        e["event_text"]
        for e in _get(client, wid, params={"kingdom_id": 1}).json()["events"]
    ]
    assert texts == ["event 1:0"]
    texts = [
        e["event_text"]
        for e in _get(client, wid, params={"kingdom_id": 10}).json()["events"]
    ]
    assert texts == ["event 1:1"]
    assert _get(client, wid, params={"kingdom_id": 2}).json()["events"] == []
