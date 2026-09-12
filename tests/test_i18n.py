"""Tests for i18n: structured events render in ES and EN."""

from __future__ import annotations

from worldsim.i18n import DEFAULT_LANG, SUPPORTED, render, vocab


def test_every_event_type_has_templates():
    """Every EventType used by the engine must have EN+ES templates reachable."""
    from worldsim.sim.engine import SimEngine  # noqa: F401  (ensures keys stay in sync)

    # keys the engine actually emits (msg_key values)
    used = {
        "kingdom_founded", "beast_sighted", "leader_crowned", "merchant_rose",
        "warlord_rose", "leader_died", "hero_emerged", "hero_fell",
        "beast_slain", "beast_rampage", "site_founded", "alliance_formed",
        "war_declared", "alliance_joined_war", "war_white_peace", "war_won",
        "war_ended", "battle", "battle_champion", "site_captured", "site_razed",
        "kingdom_collapsed", "rebellion", "tech_era", "government_civilized",
        "golden_age", "plague", "independence", "colony_founded",
        "government_changed", "milestone_first_city", "milestone_first_battle",
        "milestone_first_hero", "milestone_first_beast_slain",
        "milestone_first_empire", "milestone_ten_thousand", "milestone_century",
    }
    from worldsim.i18n import STRINGS

    for key in used:
        assert key in STRINGS["en"], f"missing EN template: {key}"
        assert key in STRINGS["es"], f"missing ES template: {key}"


def test_spanish_renders_without_placeholders():
    """ES render of representative events must contain no {braces} leftovers."""
    cases = [
        ("war_declared", {"war": "W", "a": "A", "b": "B", "cause_key": "disputed", "site": "S"}),
        ("war_declared", {"war": "W", "a": "A", "b": "B", "cause_key": "border", "site": ""}),
        ("government_changed", {"kingdom": "K", "old": "X", "government": "Y",
                                "reason_key": "golden_age"}),
        ("battle_champion", {"winner": "A", "loser": "B", "dead": 5, "champion": "C"}),
        ("leader_died", {"title": "King", "leader": "L", "kingdom": "K",
                         "age": 40, "victories": 3}),
        ("rebellion", {"n": 4, "kingdom": "K", "new_kingdom": "N",
                         "title": "Chief", "leader": "L"}),
    ]
    for key, params in cases:
        for lang in ("es", "en"):
            text = render(lang, key, params)
            assert "{" not in text and "}" not in text, f"{lang}/{key}: {text}"


def test_default_language_is_spanish():
    assert DEFAULT_LANG == "es"
    assert set(SUPPORTED) == {"es", "en"}


def test_vocab_covers_governments_and_eras():
    for gov in ("Democracy", "Technocracy", "Military Dictatorship", "Tribal"):
        assert vocab("es", gov) != gov or gov == "Tribal"
    assert vocab("es", "Medieval Era") == "Era Medieval"
    assert vocab("en", "Democracy") == "Democracy"  # passthrough


def test_engine_events_carry_params(engine):
    """After ticks, stored events must include msg_key+params for re-render."""
    for _ in range(120):
        engine.tick()
    keyed = [e for e in engine.chronicle.events if e.msg_key]
    assert keyed, "no structured events logged"
    for e in keyed:
        # every keyed event re-renders cleanly in both languages
        for lang in ("es", "en"):
            text = render(lang, e.msg_key, e.params)
            assert "{" not in text, f"{lang}/{e.msg_key}: {text}"
