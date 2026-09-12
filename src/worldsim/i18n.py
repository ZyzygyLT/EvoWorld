"""Internationalization: structured chronicle events rendered in ES/EN.

The engine logs events with a message key + params (names stay as proper
nouns); text is rendered from these templates. Stored snapshots keep the
English text for backward compatibility, plus params for re-rendering.
"""

from __future__ import annotations

DEFAULT_LANG = "es"
SUPPORTED = ("es", "en")

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "kingdom_founded": "{kingdom} ({race}, {government}) founded its capital {site}.",
        "beast_sighted": "A monster was sighted: {name} roams the wilds.",
        "leader_crowned": "{leader} becomes {title} of {kingdom}.",
        "merchant_rose": "{name} the merchant rose to prominence in {kingdom}.",
        "warlord_rose": "{name} the warlord took command of {kingdom}'s hosts.",
        "leader_died": "{title} {leader} of {kingdom} died at age {age} after"
            "{victories} victories.",
        "hero_emerged": "The champion {hero} rises in {kingdom} (battle strength x{strength}).",
        "hero_fell": "The champion {hero} of {kingdom} fell in battle after {deeds} deeds.",
        "beast_slain": "Hunters of {kingdom} slew the monster {beast} near {site}.",
        "beast_rampage": "The monster {beast} ravaged {site} ({dead} dead).",
        "milestone": "★ {text}",
        "site_founded": "{kingdom} founded {site} ({category}).",
        "alliance_formed": "{a} and {b} sealed an alliance.",
        "war_declared": "{war} began: {a} and {b} clash over {cause}!",
        "alliance_joined_war": "{joiner} joined {war} against {enemy}, honoring its alliance.",
        "war_white_peace": "{war} ended in white peace between {a} and {b}.",
        "war_won": "{victor} won {war} ({dead} dead over {months} months).",
        "war_ended": "{war} ended.",
        "battle": "{winner} defeated {loser} in battle ({dead} dead).",
        "battle_champion": "{winner} defeated {loser} in battle ({dead} dead) — champion"
            "{champion} led the charge!",
        "site_captured": "{winner} captured {site} from {loser}.",
        "site_razed": "{winner} razed {site} — {loser} lost the settlement.",
        "kingdom_collapsed": "{kingdom} has collapsed and is no more.",
        "rebellion": "{n} settlements of {kingdom} rose up and founded {new_kingdom}"
            "under {title} {leader}!",
        "tech_era": "{kingdom} entered the {era}.",
        "government_civilized": "{kingdom} left behind the old {old} ways and formed a"
            "{government} as the {era} dawned.",
        "golden_age": "A golden age dawns upon {kingdom} — its cities flourish.",
        "plague": "A terrible plague ravaged {kingdom} — {dead} souls perished.",
        "independence": "The distant colony {site} declared independence from"
            "{kingdom}, founding {new_kingdom}!",
        "colony_founded": "{kingdom} founded the colony {site} in distant lands.",
        "government_changed": "{kingdom} shifted from {old} to {government} {reason}.",
        "milestone_first_city": "★ {kingdom} raised the world's first City: {site}.",
        "milestone_first_battle": "★ The first battle in history was fought.",
        "milestone_first_hero": "★ The first champion in history, {hero}, walks the land.",
        "milestone_first_beast_slain": "★ A monster was slain for the first time.",
        "milestone_first_empire": "★ {kingdom} became the first realm to hold {n} settlements.",
        "milestone_ten_thousand": "★ {kingdom} became the first realm to reach 10,000 souls.",
        "milestone_century": "★ A full century of recorded history has passed.",
    },
    "es": {
        "kingdom_founded": "{kingdom} ({race}, {government}) fundó su capital {site}.",
        "beast_sighted": "Avistaron un monstruo: {name} merodea las tierras salvajes.",
        "leader_crowned": "{leader} se convierte en {title} de {kingdom}.",
        "merchant_rose": "El mercader {name} ganó prominencia en {kingdom}.",
        "warlord_rose": "{name} el caudillo tomó el mando de las huestes de {kingdom}.",
        "leader_died": "{title} {leader} de {kingdom} murió a los {age} años tras"
            "{victories} victorias.",
        "hero_emerged": "El campeón {hero} se alza en {kingdom} (fuerza de batalla x{strength}).",
        "hero_fell": "El campeón {hero} de {kingdom} cayó en batalla tras {deeds} hazañas.",
        "beast_slain": "Cazadores de {kingdom} mataron al monstruo {beast} cerca de {site}.",
        "beast_rampage": "El monstruo {beast} arrasó {site} ({dead} muertos).",
        "milestone": "★ {text}",
        "site_founded": "{kingdom} fundó {site} ({category}).",
        "alliance_formed": "{a} y {b} sellaron una alianza.",
        "war_declared": "¡Comenzó {war}: {a} y {b} chocan por {cause}!",
        "alliance_joined_war": "{joiner} se unió a {war} contra {enemy}, honrando su alianza.",
        "war_white_peace": "{war} terminó en paz blanca entre {a} y {b}.",
        "war_won": "{victor} ganó {war} ({dead} muertos en {months} meses).",
        "war_ended": "{war} terminó.",
        "battle": "{winner} derrotó a {loser} en batalla ({dead} muertos).",
        "battle_champion": "¡{winner} derrotó a {loser} en batalla ({dead} muertos) — el"
            "campeón {champion} lideró la carga!",
        "site_captured": "{winner} capturó {site} de {loser}.",
        "site_razed": "{winner} arrasó {site} — {loser} perdió el asentamiento.",
        "kingdom_collapsed": "{kingdom} ha colapsado y ya no existe.",
        "rebellion": "¡{n} asentamientos de {kingdom} se rebelaron y fundaron"
            "{new_kingdom} bajo {title} {leader}!",
        "tech_era": "{kingdom} entró en {era}.",
        "government_civilized": "{kingdom} dejó atrás las viejas costumbres de {old} y formó"
            "{government} al amanecer {era}.",
        "golden_age": "Una edad dorada amanece sobre {kingdom} — sus ciudades florecen.",
        "plague": "Una terrible plaga arrasó {kingdom} — {dead} almas perecieron.",
        "independence": "¡La lejana colonia {site} declaró su independencia de"
            "{kingdom}, fundando {new_kingdom}!",
        "colony_founded": "{kingdom} fundó la colonia {site} en tierras distantes.",
        "government_changed": "{kingdom} cambió de {old} a {government} {reason}.",
        "milestone_first_city": "★ {kingdom} alzó la primera Ciudad del mundo: {site}.",
        "milestone_first_battle": "★ Se libró la primera batalla de la historia.",
        "milestone_first_hero": "★ El primer campeón de la historia, {hero}, camina la tierra.",
        "milestone_first_beast_slain": "★ Un monstruo fue asesinado por primera vez.",
        "milestone_first_empire": "★ {kingdom} se convirtió en el primer reino con {n}"
            "asentamientos.",
        "milestone_ten_thousand": "★ {kingdom} fue el primer reino en alcanzar 10.000 almas.",
        "milestone_century": "★ Ha pasado un siglo completo de historia registrada.",
    },
}

# fixed-vocabulary translations (governments, races, eras, biomes, titles)
VOCAB: dict[str, dict[str, str]] = {
    "es": {
        "Tribal": "Tribal",
        "Absolute Monarchy": "Monarquía Absoluta",
        "Constitutional monarchy": "Monarquía Constitucional",
        "Democracy": "Democracia",
        "Plutocracy": "Plutocracia",
        "Technocracy": "Tecnocracia",
        "Fanatic Theocracy": "Teocracia Fanática",
        "Theocracy": "Teocracia",
        "Military Dictatorship": "Dictadura Militar",
        "Dwarf": "Enano",
        "Human": "Humano",
        "Elf": "Elfo",
        "Orc": "Orco",
        "Goblin": "Goblin",
        "Giant": "Gigante",
        "Stone Age": "Edad de Piedra",
        "Bronze Age": "Edad de Bronce",
        "Iron Age": "Edad de Hierro",
        "Classical Era": "Era Clásica",
        "Medieval Era": "Era Medieval",
        "King": "Rey",
        "Chief": "Jefe",
        "Capital": "Capital",
        "Village": "Aldea",
        "Town": "Pueblo",
        "City": "Ciudad",
        "OCEAN": "Océano",
        "SWAMP": "Pantano",
        "MARSH": "Marisma",
        "GRASSLAND": "Pastizal",
        "DESERT": "Desierto",
        "CONIFER_FOREST": "Bosque de coníferas",
        "BROADLEAF_FOREST": "Bosque frondoso",
        "BADLANDS": "Tierras baldías",
        "MOUNTAIN": "Montaña",
        "PEAK": "Pico",
        "ICE": "Hielo",
        "SMALL_HILLS": "Colinas",
        "SHRUBLAND": "Matorral",
        "HILL": "Cerro",
        "amid a losing war": "en medio de una guerra perdida",
        "in a golden age": "en una edad dorada",
        "after decades of peace": "tras décadas de paz",
        "as the wartime junta ceded power": "al ceder el poder la junta militar",
        "border friction": "fricciones fronterizas",
    },
}


def render(lang: str, key: str, params: dict | None = None) -> str:
    """Render a message template. Falls back to English, then to the key."""
    params = dict(params or {})
    # fixed-vocabulary slots are translated too (proper names pass through)
    if lang != "en":
        for slot in ("government", "gov", "race", "era", "title", "category", "old"):
            if slot in params and isinstance(params[slot], str):
                params[slot] = vocab(lang, params[slot])
    table = STRINGS.get(lang, STRINGS["en"])
    template = table.get(key) or STRINGS["en"].get(key) or key
    # composed slots that differ per language
    if key == "war_declared":
        if params.get("cause_key") == "disputed" and params.get("site"):
            params["cause"] = (
                f"tierras en disputa alrededor de {params['site']}"
                if lang == "es"
                else f"disputed lands around {params['site']}"
            )
        else:
            params["cause"] = vocab(lang, "border friction")
    if key == "government_changed":
        reasons = {
            "losing_war": "amid a losing war" if lang != "es" else "en medio de una guerra perdida",
            "golden_age": "in a golden age" if lang != "es" else "en una edad dorada",
            "decades_peace": "after decades of peace" if lang != "es" else "tras décadas de paz",
            "junta_ceded": "as the wartime junta ceded power"
            if lang != "es"
            else "al ceder el poder la junta militar",
        }
        params["reason"] = reasons.get(
            params.get("reason_key", ""), params.get("reason_key", "")
        )
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return STRINGS["en"].get(key, key)


def vocab(lang: str, word: str) -> str:
    """Translate fixed vocabulary (government/race/era names)."""
    if lang == "en":
        return word
    return VOCAB.get(lang, {}).get(word, word)


__all__ = ["DEFAULT_LANG", "SUPPORTED", "STRINGS", "VOCAB", "render", "vocab"]
