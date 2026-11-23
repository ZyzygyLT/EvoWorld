"""F7-C: event query repository (CONTRACTS §16.2, §16.4, §16.5, §16.13).

Covers: confirmed-only visibility via the INNER JOIN with
``world_operations``, the total order ``(operation_sequence, position)``,
the ``limit + 1`` rule with ``next_cursor`` only inside the high-water
mark, structural ``kingdom_ids`` membership (json_each, never LIKE) and
``get_last_confirmed_event`` for the snapshot high-water mark.
"""

from __future__ import annotations

from worldsim.server.repository import EventRepository


def _page(env, wid, **kw):
    with env.Session() as s:
        events, next_cursor = EventRepository(s).get_events_page(wid, **kw)
        return list(events), next_cursor


def _hw(env, wid, **kw):
    with env.Session() as s:
        return EventRepository(s).get_last_confirmed_event(wid, **kw)


# ---------------------------------------------------------------------------
# Visibility: only events of CONFIRMED operations
# ---------------------------------------------------------------------------


def test_only_confirmed_operation_events_are_visible(f7c):
    wid = f7c.create_world()
    op_ok = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op_ok, 1, 3)
    op_pending = f7c.seed_op(wid, 2, status="pending")
    f7c.seed_events(wid, op_pending, 2, 4)
    op_exec = f7c.seed_op(wid, 3, status="executing")
    f7c.seed_events(wid, op_exec, 3, 2)

    events, _ = _page(f7c, wid, limit=100)
    assert [e.operation_sequence for e in events] == [1, 1, 1]

    # The pending operation becomes visible only once confirmed.
    f7c.set_op(wid, op_pending, "confirmed", "active")
    events, _ = _page(f7c, wid, limit=100)
    assert [e.operation_sequence for e in events] == [1, 1, 1, 2, 2, 2, 2]


def test_event_belonging_to_another_world_is_never_returned(f7c):
    wid = f7c.create_world()
    wid2 = f7c.create_world(name="w2")
    op = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op, 1, 2)
    events, _ = _page(f7c, wid2, limit=100)
    assert events == []


# ---------------------------------------------------------------------------
# Order (§16.5): operation_sequence ASC, position ASC
# ---------------------------------------------------------------------------


def test_total_order_across_operations(f7c):
    wid = f7c.create_world()
    op21 = f7c.seed_op(wid, 21, status="confirmed")
    f7c.seed_events(wid, op21, 21, 3, month=0)
    op22 = f7c.seed_op(wid, 22, status="confirmed")
    f7c.seed_events(wid, op22, 22, 2, month=10)
    # Seed out of insertion order: the query must still sort by (seq, pos).
    events, _ = _page(f7c, wid, limit=100)
    assert [(e.operation_sequence, e.position) for e in events] == [
        (21, 0), (21, 1), (21, 2), (22, 0), (22, 1)
    ]
    # month is a filterable attribute, never the sort key.
    assert [e.month for e in events] == [0, 1, 2, 10, 11]


# ---------------------------------------------------------------------------
# limit + 1 and next_cursor inside the high-water mark (§16.4, §16.13)
# ---------------------------------------------------------------------------


def test_next_cursor_only_when_more_rows_exist(f7c):
    wid = f7c.create_world()
    op = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op, 1, 5)

    # Exactly limit rows → final page, no cursor.
    events, cursor = _page(f7c, wid, limit=5)
    assert len(events) == 5
    assert cursor is None

    # Fewer rows → final page, no cursor.
    events, cursor = _page(f7c, wid, limit=10)
    assert len(events) == 5
    assert cursor is None

    # More rows exist → cursor points at the LAST DELIVERED event.
    events, cursor = _page(f7c, wid, limit=2)
    assert [e.position for e in events] == [0, 1]
    assert cursor == (1, 1)
    events, cursor = _page(f7c, wid, limit=2, cursor=(1, 1))
    assert [e.position for e in events] == [2, 3]
    assert cursor == (1, 3)
    events, cursor = _page(f7c, wid, limit=2, cursor=(1, 3))
    assert [e.position for e in events] == [4]
    assert cursor is None


def test_high_water_mark_is_an_inclusive_upper_bound(f7c):
    wid = f7c.create_world()
    op1 = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op1, 1, 3)
    op2 = f7c.seed_op(wid, 2, status="confirmed")
    f7c.seed_events(wid, op2, 2, 3)

    # Universe fixed at (1,1): events of sequence 2 are outside it.
    events, cursor = _page(f7c, wid, limit=100, snapshot_high_watermark=(1, 1))
    assert [(e.operation_sequence, e.position) for e in events] == [(1, 0), (1, 1)]
    assert cursor is None

    # Cursor equal to the hwm → nothing inside the universe remains.
    events, cursor = _page(
        f7c, wid, limit=100, cursor=(1, 1), snapshot_high_watermark=(1, 1)
    )
    assert events == []
    assert cursor is None


def test_hwm_with_cursor_narrows_navigation(f7c):
    wid = f7c.create_world()
    op = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op, 1, 6)
    events, cursor = _page(
        f7c, wid, limit=3, cursor=(1, 1), snapshot_high_watermark=(1, 5)
    )
    assert [e.position for e in events] == [2, 3, 4]
    # More rows remain inside the hwm (position 5) → cursor is offered.
    assert cursor == (1, 4)
    events, cursor = _page(
        f7c, wid, limit=3, cursor=(1, 4), snapshot_high_watermark=(1, 5)
    )
    assert [e.position for e in events] == [5]
    assert cursor is None  # last row inside the hwm delivered

    # A tighter hwm (position 4) leaves position 5 outside the universe:
    # after delivering position 4 nothing remains inside → no cursor.
    events, cursor = _page(
        f7c, wid, limit=3, cursor=(1, 1), snapshot_high_watermark=(1, 4)
    )
    assert [e.position for e in events] == [2, 3, 4]
    assert cursor is None


# ---------------------------------------------------------------------------
# Filters (§16.3)
# ---------------------------------------------------------------------------


def test_month_and_event_type_filters(f7c):
    wid = f7c.create_world()
    op = f7c.seed_op(wid, 1, status="confirmed")
    # 3 events at months 5, 6, 7 (month + position).
    f7c.seed_events(wid, op, 1, 3, month=5, event_type="BATTLE")

    events, _ = _page(f7c, wid, limit=100, month=5)
    assert [e.position for e in events] == [0]
    events, _ = _page(f7c, wid, limit=100, month=6)
    assert [e.position for e in events] == [1]
    events, _ = _page(f7c, wid, limit=100, month=99)
    assert events == []
    events, _ = _page(f7c, wid, limit=100, event_type="BATTLE")
    assert len(events) == 3
    events, _ = _page(f7c, wid, limit=100, event_type="WAR_DECLARED")
    assert events == []


def test_kingdom_filter_is_structural_membership_not_like(f7c):
    """``kingdom_ids`` is a JSON array: membership is exact.  A LIKE
    '%1%' would false-positive on [10] and [21]."""
    wid = f7c.create_world()
    op = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op, 1, 1, kingdom_ids=[1])     # matches 1
    f7c.seed_events(wid, op, 1, 1, kingdom_ids=[10])    # must NOT match 1
    f7c.seed_events(wid, op, 1, 1, kingdom_ids=[21])    # must NOT match 1
    f7c.seed_events(wid, op, 1, 1, kingdom_ids=[1, 2])  # matches 1 and 2
    f7c.seed_events(wid, op, 1, 1, kingdom_ids=[])      # matches nothing

    events, _ = _page(f7c, wid, limit=100, kingdom_id=1)
    texts = [e.event_text for e in events]
    assert texts == ["event 1:0", "event 1:3"]

    events, _ = _page(f7c, wid, limit=100, kingdom_id=10)
    assert [e.event_text for e in events] == ["event 1:1"]

    events, _ = _page(f7c, wid, limit=100, kingdom_id=2)
    assert [e.event_text for e in events] == ["event 1:3"]

    # Filters compose (month + kingdom + type): kingdom 1 events sit at
    # positions 0 (month 0) and 3 (month 3).
    events, _ = _page(
        f7c, wid, limit=100, kingdom_id=1, month=0, event_type="BATTLE"
    )
    assert [e.position for e in events] == [0]


def test_empty_result_returns_empty_page_and_no_cursor(f7c):
    wid = f7c.create_world()
    assert _page(f7c, wid, limit=20) == ([], None)
    assert _hw(f7c, wid) is None


# ---------------------------------------------------------------------------
# get_last_confirmed_event — snapshot high-water mark (§16.13)
# ---------------------------------------------------------------------------


def test_last_confirmed_event_is_the_descending_one(f7c):
    wid = f7c.create_world()
    op1 = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op1, 1, 3)             # months 0, 1, 2
    op2 = f7c.seed_op(wid, 2, status="confirmed")
    f7c.seed_events(wid, op2, 2, 2, month=10)   # months 10, 11
    assert _hw(f7c, wid) == (2, 1)

    # With filters the hwm is the last event VISIBLE for that query.
    assert _hw(f7c, wid, kingdom_id=99) is None
    assert _hw(f7c, wid, month=0) == (1, 0)     # only op1 pos 0 is at month 0
    assert _hw(f7c, wid, month=2) == (1, 2)
    assert _hw(f7c, wid, month=11) == (2, 1)


def test_last_confirmed_event_ignores_unconfirmed_operations(f7c):
    wid = f7c.create_world()
    op1 = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op1, 1, 3)
    op2 = f7c.seed_op(wid, 2, status="pending")
    f7c.seed_events(wid, op2, 2, 5)
    # The pending operation's events never raise the high-water mark.
    assert _hw(f7c, wid) == (1, 2)
    f7c.set_op(wid, op2, "confirmed", "active")
    assert _hw(f7c, wid) == (2, 4)
