// i18n: mirrors worldsim/i18n.py event templates + UI strings.
// Events arrive with {msg_key, params}; render locally in the chosen lang.
export const DEFAULT_LANG = 'es';

export const STRINGS = {
    en: {
        kingdom_founded: "{kingdom} ({race}, {government}) founded its capital {site}.",
        beast_sighted: "A monster was sighted: {name} roams the wilds.",
        leader_crowned: "{leader} becomes {title} of {kingdom}.",
        merchant_rose: "{name} the merchant rose to prominence in {kingdom}.",
        warlord_rose: "{name} the warlord took command of {kingdom}'s hosts.",
        leader_died: "{title} {leader} of {kingdom} died at age {age} after {victories} victories.",
        hero_emerged: "The champion {hero} rises in {kingdom} (battle strength x{strength}).",
        hero_fell: "The champion {hero} of {kingdom} fell in battle after {deeds} deeds.",
        beast_slain: "Hunters of {kingdom} slew the monster {beast} near {site}.",
        beast_rampage: "The monster {beast} ravaged {site} ({dead} dead).",
        milestone: "★ {text}",
        site_founded: "{kingdom} founded {site} ({category}).",
        alliance_formed: "{a} and {b} sealed an alliance.",
        war_declared: "{war} began: {a} and {b} clash over {cause}!",
        alliance_joined_war: "{joiner} joined {war} against {enemy}, honoring its alliance.",
        war_white_peace: "{war} ended in white peace between {a} and {b}.",
        war_won: "{victor} won {war} ({dead} dead over {months} months).",
        war_ended: "{war} ended.",
        battle: "{winner} defeated {loser} in battle ({dead} dead).",
        battle_champion: "{winner} defeated {loser} in battle ({dead} dead) — champion {champion} led the charge!",
        site_captured: "{winner} captured {site} from {loser}.",
        site_razed: "{winner} razed {site} — {loser} lost the settlement.",
        kingdom_collapsed: "{kingdom} has collapsed and is no more.",
        rebellion: "{n} settlements of {kingdom} rose up and founded {new_kingdom} under {title} {leader}!",
        tech_era: "{kingdom} entered the {era}.",
        government_civilized: "{kingdom} left behind the old {old} ways and formed a {government} as the {era} dawned.",
        golden_age: "A golden age dawns upon {kingdom} — its cities flourish.",
        plague: "A terrible plague ravaged {kingdom} — {dead} souls perished.",
        independence: "The distant colony {site} declared independence from {kingdom}, founding {new_kingdom}!",
        colony_founded: "{kingdom} founded the colony {site} in distant lands.",
        government_changed: "{kingdom} shifted from {old} to {government} {reason}.",
        milestone_first_city: "{kingdom} raised the world's first City: {site}.",
        milestone_first_battle: "The first battle in history was fought.",
        milestone_first_hero: "The first champion in history, {hero}, walks the land.",
        milestone_first_beast_slain: "A monster was slain for the first time.",
        milestone_first_empire: "{kingdom} became the first realm to hold {n} settlements.",
        milestone_ten_thousand: "{kingdom} became the first realm to reach 10,000 souls.",
        milestone_century: "A full century of recorded history has passed.",
    },
    es: {
        kingdom_founded: "{kingdom} ({race}, {government}) fundó su capital {site}.",
        beast_sighted: "Avistaron un monstruo: {name} merodea las tierras salvajes.",
        leader_crowned: "{leader} se convierte en {title} de {kingdom}.",
        merchant_rose: "El mercader {name} ganó prominencia en {kingdom}.",
        warlord_rose: "{name} el caudillo tomó el mando de las huestes de {kingdom}.",
        leader_died: "{title} {leader} de {kingdom} murió a los {age} años tras {victories} victorias.",
        hero_emerged: "El campeón {hero} se alza en {kingdom} (fuerza de batalla x{strength}).",
        hero_fell: "El campeón {hero} de {kingdom} cayó en batalla tras {deeds} hazañas.",
        beast_slain: "Cazadores de {kingdom} mataron al monstruo {beast} cerca de {site}.",
        beast_rampage: "El monstruo {beast} arrasó {site} ({dead} muertos).",
        milestone: "★ {text}",
        site_founded: "{kingdom} fundó {site} ({category}).",
        alliance_formed: "{a} y {b} sellaron una alianza.",
        war_declared: "¡Comenzó {war}: {a} y {b} chocan por {cause}!",
        alliance_joined_war: "{joiner} se unió a {war} contra {enemy}, honrando su alianza.",
        war_white_peace: "{war} terminó en paz blanca entre {a} y {b}.",
        war_won: "{victor} ganó {war} ({dead} muertos en {months} meses).",
        war_ended: "{war} terminó.",
        battle: "{winner} derrotó a {loser} en batalla ({dead} muertos).",
        battle_champion: "¡{winner} derrotó a {loser} en batalla ({dead} muertos) — el campeón {champion} lideró la carga!",
        site_captured: "{winner} capturó {site} de {loser}.",
        site_razed: "{winner} arrasó {site} — {loser} perdió el asentamiento.",
        kingdom_collapsed: "{kingdom} ha colapsado y ya no existe.",
        rebellion: "¡{n} asentamientos de {kingdom} se rebelaron y fundaron {new_kingdom} bajo {title} {leader}!",
        tech_era: "{kingdom} entró en {era}.",
        government_civilized: "{kingdom} dejó atrás las viejas costumbres de {old} y formó {government} al amanecer {era}.",
        golden_age: "Una edad dorada amanece sobre {kingdom} — sus ciudades florecen.",
        plague: "Una terrible plaga arrasó {kingdom} — {dead} almas perecieron.",
        independence: "¡La lejana colonia {site} declaró su independencia de {kingdom}, fundando {new_kingdom}!",
        colony_founded: "{kingdom} fundó la colonia {site} en tierras distantes.",
        government_changed: "{kingdom} cambió de {old} a {government} {reason}.",
        milestone_first_city: "★ {kingdom} alzó la primera Ciudad del mundo: {site}.",
        milestone_first_battle: "★ Se libró la primera batalla de la historia.",
        milestone_first_hero: "★ El primer campeón de la historia, {hero}, camina la tierra.",
        milestone_first_beast_slain: "★ Un monstruo fue asesinado por primera vez.",
        milestone_first_empire: "★ {kingdom} se convirtió en el primer reino con {n} asentamientos.",
        milestone_ten_thousand: "★ {kingdom} fue el primer reino en alcanzar 10.000 almas.",
        milestone_century: "★ Ha pasado un siglo completo de historia registrada.",
    },
};

const REASONS = {
    en: {
        losing_war: "amid a losing war",
        golden_age: "in a golden age",
        decades_peace: "after decades of peace",
        junta_ceded: "as the wartime junta ceded power",
        border: "border friction",
    },
    es: {
        losing_war: "en medio de una guerra perdida",
        golden_age: "en una edad dorada",
        decades_peace: "tras décadas de paz",
        junta_ceded: "al ceder el poder la junta militar",
        border: "fricciones fronterizas",
    },
};

export const VOCAB = {    en: {},
    es: {
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
        "Renaissance": "Renacimiento",
        "Enlightenment": "Ilustración",
        "King": "Rey",
        "Chief": "Jefe",
        "Capital": "Capital",
        "Village": "Aldea",
        "Town": "Pueblo",
        "City": "Ciudad",
    },
};

const VOCAB_SLOTS = new Set(["government", "gov", "race", "era", "title", "category", "old"]);

export function vocab(lang, word) {
    if (lang === "en" || word == null) return word ?? "";
    return VOCAB.es[word] ?? word;
}

export const TECH_NAMES = {
    ambush: { en: "Ambush Tactics", es: "Tácticas de emboscada" },
    bronze_arms: { en: "Bronze Working", es: "Trabajo del bronce" },
    iron_arms: { en: "Iron Working", es: "Forja del hierro" },
    phalanx: { en: "Phalanx", es: "Falange" },
    siege: { en: "Siege Craft", es: "Arte del asedio" },
    steel: { en: "Steel", es: "Acero" },
    agriculture: { en: "Agriculture", es: "Agricultura" },
    irrigation: { en: "Irrigation", es: "Irrigación" },
    pottery: { en: "Pottery", es: "Alfarería" },
    metalworking: { en: "Metalworking", es: "Metalurgia" },
    guilds: { en: "Guilds", es: "Gremios" },
    architecture: { en: "Architecture", es: "Arquitectura" },
    fishing: { en: "Fishing", es: "Pesca" },
    coastal_trade: { en: "Coastal Trade", es: "Comercio costero" },
    currency: { en: "Currency", es: "Moneda" },
    merchant_fleet: { en: "Merchant Fleet", es: "Flota mercante" },
    cartography: { en: "Cartography", es: "Cartografía" },
    oral_tradition: { en: "Oral Tradition", es: "Tradición oral" },
    writing: { en: "Writing", es: "Escritura" },
    priesthood: { en: "Priesthood", es: "Sacerdocio" },
    philosophy: { en: "Philosophy", es: "Filosofía" },
    engineering: { en: "Engineering", es: "Ingeniería" },
    gunpowder: { en: "Gunpowder", es: "Pólvora" },
    banking: { en: "Banking", es: "Banca" },
    printing: { en: "Printing Press", es: "Imprenta" },
    renaissance_arts: { en: "Renaissance Arts", es: "Artes renacentistas" },
    artillery: { en: "Artillery", es: "Artillería" },
    steam_power: { en: "Steam Power", es: "Máquina de vapor" },
    encyclopedia: { en: "Encyclopedia", es: "Enciclopedia" },
};

export function techName(lang, id) {
    return (TECH_NAMES[id] ?? {})[lang] ?? TECH_NAMES[id]?.en ?? id;
}

export const TECH_BRANCH = {
    ambush: "warfare", bronze_arms: "warfare", iron_arms: "warfare",
    phalanx: "warfare", siege: "warfare", steel: "warfare",
    gunpowder: "warfare", artillery: "warfare",
    agriculture: "craft", irrigation: "craft", pottery: "craft",
    metalworking: "craft", guilds: "craft", architecture: "craft",
    renaissance_arts: "craft", steam_power: "craft",
    fishing: "trade", coastal_trade: "trade", currency: "trade",
    merchant_fleet: "trade", cartography: "trade", banking: "trade",
    oral_tradition: "lore", writing: "lore", priesthood: "lore",
    philosophy: "lore", engineering: "lore", printing: "lore", encyclopedia: "lore",
};

export const BRANCH_COLORS = {
    warfare: "#f85149",
    craft: "#d29922",
    trade: "#58a6ff",
    lore: "#d2a8ff",
};

export function renderEvent(lang, ev) {
    // ev: {msg_key, params, text} — re-render from key+params, fallback to text
    if (!ev.msg_key) return ev.text;
    const params = { ...(ev.params || {}) };
    if (lang !== "en") {
        for (const slot of VOCAB_SLOTS) {
            if (typeof params[slot] === "string") params[slot] = vocab(lang, params[slot]);
        }
    }
    if (ev.msg_key === "war_declared") {
        params.cause = params.cause_key === "disputed" && params.site
            ? (lang === "es" ? `tierras en disputa alrededor de ${params.site}` : `disputed lands around ${params.site}`)
            : (lang === "es" ? "fricciones fronterizas" : "border friction");
    }
    if (ev.msg_key === "government_changed") {
        const r = REASONS[lang] || REASONS.en;
        params.reason = r[params.reason_key] ?? params.reason_key ?? "";
    }
    const table = STRINGS[lang] || STRINGS.en;
    const template = table[ev.msg_key] || STRINGS.en[ev.msg_key] || ev.text;
    try {
        return template.replace(/\{(\w+)\}/g, (_, k) =>
            params[k] !== undefined ? String(params[k]) : `{${k}}`);
    } catch {
        return ev.text;
    }
}

// ---- UI strings ----
export const UI = {
    en: {
        run: "▶ run", pause: "⏸ pause", kingdoms: "Kingdoms", chronicle: "Chronicle",
        milestones: "Milestones", room: "room", newWorld: "+ new world", generating: "generating world…",
        single: "👁 Single player", league: "👑 Dynasty League", invite: "🔗 invite",
        myWorlds: "My worlds", allWorlds: "All worlds", login: "log in", logout: "log out",
        register: "register", username: "username", password: "password",
        records: "World records", largestCity: "Largest city", bloodiestBattle: "Bloodiest battle",
        longestWar: "Longest war", longestReign: "Longest reign", largestEmpire: "Largest empire",
        oldestRealm: "Oldest realm", fitView: "F = re-fit", exportPng: "📷 PNG",
        championBanner: "crowned champion with", points: "points",
    },
    es: {
        run: "▶ correr", pause: "⏸ pausa", kingdoms: "Reinos", chronicle: "Crónica",
        milestones: "Hitos", room: "sala", newWorld: "+ mundo nuevo", generating: "generando mundo…",
        single: "👁 Un jugador", league: "👑 Liga de Dinastías", invite: "🔗 invitar",
        myWorlds: "Mis mundos", allWorlds: "Todos", login: "entrar", logout: "salir",
        register: "registrarse", username: "usuario", password: "contraseña",
        records: "Récords mundiales", largestCity: "Ciudad más grande", bloodiestBattle: "Batalla más sangrienta",
        longestWar: "Guerra más larga", longestReign: "Reinado más largo", largestEmpire: "Imperio más extenso",
        oldestRealm: "Reino más antiguo", fitView: "F = encuadrar", exportPng: "📷 PNG",
        championBanner: "coronado campeón con", points: "puntos",
    },
};

export function t(lang, key) {
    const table = UI[lang] || UI.en;
    return table[key] ?? UI.en[key] ?? key;
}
