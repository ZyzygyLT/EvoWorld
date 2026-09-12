"""Prediction kinds, scoring and resolution against the live chronicle."""

from __future__ import annotations

import json

from sqlalchemy import select

from worldsim.server.db import PredictionRow, SessionLocal
from worldsim.sim.engine import ERA_NAMES, SimEngine
from worldsim.sim.models import EventType

# points are weighted by how hard each prophecy is to land
KINDS: dict[str, dict] = {
    "war_victory": {
        "label": "{kingdom} will win its current/next war",
        "params": ["kingdom_id"],
        "points": 30,
        "deadline": 60,
        "hint": "who reads the military balance better",
    },
    "era_reach": {
        "label": "{kingdom} will reach the {era}",
        "params": ["kingdom_id", "era_level"],
        "points": 20,
        "deadline": 240,
        "hint": "who tracks knowledge progress",
    },
    "rebellion_any": {
        "label": "a rebellion will split some realm",
        "params": [],
        "points": 40,
        "deadline": 240,
        "hint": "who sees the strain building",
    },
    "first_city": {
        "label": "{kingdom} will raise the first City",
        "params": ["kingdom_id"],
        "points": 50,
        "deadline": 240,
        "hint": "who finds the most fertile land",
    },
    "plague": {
        "label": "a plague will ravage {kingdom}",
        "params": ["kingdom_id"],
        "points": 35,
        "deadline": 480,
        "hint": "who watches population density",
    },
    "beast_slain": {
        "label": "a monster will be slain",
        "params": [],
        "points": 25,
        "deadline": 240,
        "hint": "who follows the hunters",
    },
}


def create_prediction(
    world_id: int, user_token: str, user_name: str, kind: str, params: dict, month: int
) -> dict | None:
    """Seal a prophecy. Returns the row payload or an error string."""
    spec = KINDS.get(kind)
    if spec is None:
        return None
    row = PredictionRow(
        world_id=world_id,
        user_token=user_token,
        user_name=user_name,
        kind=kind,
        params_json=json.dumps(params),
        month=month,
        deadline_months=spec["deadline"],
        status="pending",
    )
    with SessionLocal() as db:  # type: ignore[call-arg]
        db.add(row)
        db.commit()
        db.refresh(row)
        return _row_dict(row)


def _row_dict(r: PredictionRow) -> dict:
    return {
        "id": r.id,
        "world_id": r.world_id,
        "user_token": r.user_token,
        "user_name": r.user_name,
        "kind": r.kind,
        "params": json.loads(r.params_json or "{}"),
        "month": r.month,
        "deadline_months": r.deadline_months,
        "status": r.status,
        "points": r.points,
        "resolved_month": r.resolved_month,
        "resolved_text": r.resolved_text,
    }


def list_predictions(world_id: int) -> list[dict]:
    with SessionLocal() as db:  # type: ignore[call-arg]
        rows = db.scalars(
            select(PredictionRow)
            .where(PredictionRow.world_id == world_id)
            .order_by(PredictionRow.id.desc())
            .limit(100)
        ).all()
        return [_row_dict(r) for r in rows]


def leaderboard(world_id: int) -> list[dict]:
    """Aggregate points per user for one world."""
    preds = list_predictions(world_id)
    users: dict[str, dict] = {}
    for p in preds:
        u = users.setdefault(
            p["user_token"], {"user": p["user_name"], "points": 0, "won": 0, "lost": 0}
        )
        u["points"] += p["points"]
        if p["status"] == "won":
            u["won"] += 1
        elif p["status"] == "lost":
            u["lost"] += 1
    board = sorted(users.values(), key=lambda u: -u["points"])
    for rank, u in enumerate(board, 1):
        u["rank"] = rank
    return board


# -- resolution ----------------------------------------------------------------


def _kingdom_by_id(eng: SimEngine, kid: int):
    return next((k for k in eng.kingdoms if k.id == kid), None)


def _check_pred(eng: SimEngine, p: dict) -> tuple[str, bool] | None:
    """Return (resolved_text, won) if the prophecy is decided, else None."""
    kind = p["kind"]
    params = p["params"]
    start = p["month"]
    events = [
        e for e in eng.chronicle.events if e.month > start
    ]  # only events after the seal

    if kind == "war_victory":
        k = _kingdom_by_id(eng, params.get("kingdom_id", -1))
        if k is None:
            return ("the realm vanished before the war ended", False)
        for w in eng.wars:
            if w.start_month > start and w.is_over and w.victor is k:
                return (f"{k.name} won a war as prophesied", True)
        return None

    if kind == "era_reach":
        k = _kingdom_by_id(eng, params.get("kingdom_id", -1))
        target = int(params.get("era_level", 1))
        if k is None:
            return ("the realm vanished before the era dawned", False)
        for e in events:
            if (
                e.type == EventType.TECH_ERA
                and k.id in e.kingdom_ids
                and ERA_NAMES[target] in e.text
            ):
                return (f"{k.name} reached the {ERA_NAMES[target]} as prophesied", True)
        return None

    if kind == "rebellion_any":
        for e in events:
            if e.type == EventType.REBELLION:
                return ("a realm split by rebellion, as prophesied", True)
        return None

    if kind == "first_city":
        k = _kingdom_by_id(eng, params.get("kingdom_id", -1))
        if k is None:
            return ("the realm vanished before raising a City", False)
        for e in events:
            if (
                e.type == EventType.SITE_FOUNDED
                and k.id in e.kingdom_ids
                and "(City)" in e.text
            ):
                return (f"{k.name} raised a City as prophesied", True)
        # special: if ANY other kingdom raised a city first, the prophecy dies
        for e in events:
            if e.type == EventType.SITE_FOUNDED and "(City)" in e.text:
                other = next(
                    (kid for kid in e.kingdom_ids if kid != k.id), None
                )
                if other is not None:
                    return ("another realm raised the first City first", False)
        return None

    if kind == "plague":
        k = _kingdom_by_id(eng, params.get("kingdom_id", -1))
        if k is None:
            return ("the realm vanished before the plague came", False)
        for e in events:
            if e.type == EventType.PLAGUE and k.id in e.kingdom_ids:
                return (f"a plague ravaged {k.name} as prophesied", True)
        return None

    if kind == "beast_slain":
        for e in events:
            if e.type == EventType.BEAST_SLAIN:
                return ("a monster was slain, as prophesied", True)
        return None

    return None


def resolve_pending(world_id: int, eng: SimEngine) -> list[dict]:
    """Check pending prophecies for this world; resolve won/lost, return them."""
    pending = [p for p in list_predictions(world_id) if p["status"] == "pending"]
    resolved: list[dict] = []
    for p in pending:
        deadline_month = p["month"] + p["deadline_months"]
        result = _check_pred(eng, p)
        status = None
        text = None
        if result is not None:
            text, won = result
            status = "won" if won else "lost"
        elif eng.month >= deadline_month:
            status = "expired"
            text = "the prophecy's time ran out"
        if status is None:
            continue
        points = KINDS[p["kind"]]["points"] if status == "won" else 0
        with SessionLocal() as db:  # type: ignore[call-arg]
            row = db.get(PredictionRow, p["id"])
            row.status = status
            row.points = points
            row.resolved_month = eng.month
            row.resolved_text = text
            db.commit()
        p.update(
            status=status, points=points, resolved_month=eng.month, resolved_text=text
        )
        resolved.append(
            {"user_name": p["user_name"], "text": text, "points": points}
        )
    return resolved
