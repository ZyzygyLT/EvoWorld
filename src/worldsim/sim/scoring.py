"""Universal endgame scoring for worldsim.

Usable from the server (tick dict) or directly in tests without DB.
Scores are intentionally transparent — every bonus is a cell in the results
table, so players can see why a realm won. Weights are small multiples of
the league SCORE_TABLE idea but extended for sessions 1-6 systems.

Components per living realm:
  * 2  pts per 100 pop
  * 12 pts per site
  * 14 pts per City, 6 per Town (+)
  * 10 pts per tech era step + 2 per tech node
  * 10 pts per golden age, survived plague, per temple
  * 14 pts per finished trade route, +2 per year of trading
  *  6 pts per faith temple beyond the first (keeps faith visible even without trade)
  * 18 pts for surviving to 100y, -30 for collapse
  *  8 pts per war won, -6 per war lost (gentle; league already tracks wars)
  *  exclusive empire bonus: biggest realm +15, biggest city holder +12

Ties are stable-sorted by (score, pop, tech). Result includes breakdown
so the results screen can render rows.
"""

from __future__ import annotations


def _faith_label(faith: str | None) -> str:
    return faith or "Animism"


def score_tick(tick: dict) -> list[dict]:
    """Rank all kingdoms (living and fallen) by endgame score.

    Accepts the WS tick snapshot dict: `tick.kingdoms`, `tick.fallen`,
    `tick.milestones`, `tick.month` (or currentMonth). Returns sorted
    descending by `total`, each entry contains `breakdown` for display.
    """
    month = int(tick.get("month", 0) or 0)
    kingdoms: list[dict] = tick.get("kingdoms") or []
    # build per-kingdom base
    scored: list[dict] = []
    max_sites = max((len(k.get("sites") or []) for k in kingdoms), default=0)
    max_city_pop = 0
    max_city_owner: int | None = None
    for k in kingdoms:
        for s in k.get("sites") or []:
            pop = int(s.get("population") or 0)
            cat = s.get("category") or "Village"
            if cat == "City" and pop > max_city_pop:
                max_city_pop = pop
                max_city_owner = int(k.get("id"))

    for k in kingdoms:
        kid = int(k.get("id"))
        name = str(k.get("name") or f"#{kid}")
        sites = k.get("sites") or []
        n_sites = len(sites)
        n_cities = sum(1 for s in sites if (s.get("category") or "") == "City")
        n_towns = sum(1 for s in sites if (s.get("category") or "") == "Town")
        pop = int(k.get("population") or 0)
        tech_level = int(k.get("tech_level") or 0)
        techs = k.get("techs") or []
        faith = _faith_label(k.get("faith"))
        temples = k.get("temples") or []
        n_temples = len(temples) if isinstance(temples, (list, set, tuple)) else 0
        trade_routes = k.get("trade_routes") or []
        n_routes = len(trade_routes) if isinstance(trade_routes, (list, set, tuple)) else 0
        is_fallen = n_sites == 0
        bd: dict[str, int] = {}

        # population
        bd["pop"] = (pop // 100) * 2
        # sites
        bd["sites"] = n_sites * 12
        bd["cities"] = n_cities * 14 + n_towns * 6
        # tech
        bd["tech"] = tech_level * 10 + len(techs) * 2
        if k.get("golden_age"):
            bd["golden"] = 10
        else:
            bd["golden"] = 0
        # faith
        bd["faith"] = n_temples * 6
        if k.get("happiness") is not None:
            try:
                h = float(k.get("happiness"))
                bd["happiness"] = int(max(-8, min(8, (h - 50) / 6)))
            except Exception:
                bd["happiness"] = 0
        # trade
        trade_months = 0
        for tr in trade_routes:
            if isinstance(tr, dict):
                trade_months += int(tr.get("months") or 0)
        bd["trade"] = n_routes * 14 + (trade_months // 12) * 2
        # wars (if tick includes stats-style fields)
        wars_won = int(k.get("wars_won", 0) or 0)
        wars_lost = int(k.get("wars_lost", 0) or 0)
        bd["wars"] = wars_won * 8 + wars_lost * -6
        # longevity / survival
        if not is_fallen and month >= 100 * 12:
            bd["survival"] = 18
        elif is_fallen:
            bd["survival"] = -30
        # empire + biggest city bonuses (mutually achievable)
        if not is_fallen and n_sites == max_sites and max_sites >= 4:
            bd["empire"] = 15
        if kid == max_city_owner:
            bd["city_crown"] = 12

        total = sum(bd.values())
        entry = {
            "id": kid,
            "name": name,
            "race": k.get("race"),
            "government": k.get("government"),
            "faith": faith,
            "color": k.get("color") or [140, 140, 140],
            "population": pop,
            "sites": n_sites,
            "tech_level": tech_level,
            "is_fallen": is_fallen,
            "temples": n_temples,
            "trade_routes": n_routes,
            "breakdown": bd,
            "total": int(total),
        }
        scored.append(entry)

    # fallen memory lane — tick.fallen carries only name/race/color/month, no pop
    _seen_names = {str(s.get("name") or "") for s in scored}
    _seen_ids: set[int] = set()
    for f in tick.get("fallen") or []:
        fname = str(f.get("name") or "Fallen realm")
        if fname in _seen_names:
            continue
        kid = int(f.get("id", 900 + len(scored) + len(_seen_ids)))
        if kid in _seen_ids:
            continue
        _seen_ids.add(kid)
        _seen_names.add(fname)
        scored.append({
            "id": kid,
            "name": fname,
            "race": f.get("race"),
            "government": None,
            "faith": _faith_label(f.get("faith")),
            "color": f.get("color") or [90, 90, 90],
            "population": 0,
            "sites": 0,
            "tech_level": 0,
            "is_fallen": True,
            "temples": 0,
            "trade_routes": 0,
            "breakdown": {"survival": -30},
            "total": -30,
        })

    scored.sort(key=lambda e: (e["total"], e["population"], e["tech_level"]), reverse=True)
    for rank, e in enumerate(scored, 1):
        e["rank"] = rank
    return scored


__all__ = ["score_tick"]
