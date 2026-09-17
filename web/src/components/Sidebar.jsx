import React from 'react';
import { t, vocab } from '../i18n.js';

/**
 * Right sidebar — ranking with differentiated per-tab metrics + world records.
 * Chronicle lives below the map (ChroniclePanel). Stats are tabbed (StatsPanel).
 */
const ERA_NAMES = ['Stone Age', 'Bronze Age', 'Iron Age', 'Classical Era', 'Medieval Era', 'Renaissance', 'Enlightenment'];

// scoring weights (mirrors src/worldsim/sim/scoring.py)
function breakdown(k, tickMonth) {
    const sites = k.sites ?? [];
    const nSites = sites.length;
    const nCities = sites.filter(s => s.category === 'City').length;
    const nTowns = sites.filter(s => s.category === 'Town').length;
    const pop = k.population ?? 0;
    const techLevel = k.tech_level ?? 0;
    const techs = k.techs ?? [];
    const temples = k.temples ?? [];
    const routes = k.trade_routes ?? [];
    const bd = {};
    bd.pop = Math.floor(pop / 100) * 2;
    bd.sites = nSites * 12;
    bd.cities = nCities * 14 + nTowns * 6;
    bd.tech = techLevel * 10 + techs.length * 2;
    bd.golden = k.golden_age ? 10 : 0;
    bd.faith = temples.length * 6;
    let tradeMonths = 0;
    for (const tr of routes) if (tr && typeof tr.months === 'number') tradeMonths += tr.months;
    bd.trade = routes.length * 14 + Math.floor(tradeMonths / 12) * 2;
    bd.wars = (k.wars_won ?? 0) * 8 + (k.wars_lost ?? 0) * -6;
    if (nSites === 0) bd.survival = -30;
    else if ((tickMonth ?? 0) >= 100 * 12) bd.survival = 18;
    return bd;
}
function totalScore(k, tickMonth) {
    const bd = breakdown(k, tickMonth);
    return Object.values(bd).reduce((a, b) => a + b, 0);
}
function scoreLabel(k, tickMonth, lang) {
    const bd = breakdown(k, tickMonth);
    const total = totalScore(k, tickMonth);
    const parts = [];
    if (bd.pop) parts.push(`${bd.pop} hab`);
    if (bd.sites) parts.push(`${bd.sites} terr`);
    if (bd.cities) parts.push(`${bd.cities} urb`);
    if (bd.tech) parts.push(`${bd.tech} tec`);
    if (bd.faith) parts.push(`${bd.faith} fe`);
    if (bd.trade) parts.push(`${bd.trade} com`);
    if (bd.wars) parts.push(`${bd.wars > 0 ? '+' : ''}${bd.wars} guerra`);
    if (bd.golden) parts.push(`+${bd.golden} dorada`);
    return { total, parts: parts.join(' + ') };
}

function siteComposition(sites) {
    const cats = { City: 0, Town: 0, Village: 0, Capital: 0 };
    for (const s of sites ?? []) {
        if (s.category in cats) cats[s.category]++;
        else cats.Village++;
    }
    const pieces = [];
    if (cats.City) pieces.push(`${cats.City} Ciudad${cats.City > 1 ? 'es' : ''}`);
    if (cats.Town) pieces.push(`${cats.Town} Pueblo${cats.Town > 1 ? 's' : ''}`);
    // Capital is usually a city — avoid double counting; count remaining as villages
    const villages = cats.Village + cats.Capital; // capitals are shown as separate category in some saves
    // more robust: count by is_capital flag instead
    return { pieces, total: (sites ?? []).length };
}
function siteCompText(k, lang) {
    const sites = k.sites ?? [];
    let cities = 0, towns = 0, villages = 0;
    for (const s of sites) {
        if (s.category === 'City') cities++;
        else if (s.category === 'Town') towns++;
        else villages++;
    }
    const parts = [];
    if (cities) parts.push(`${cities} Ciudad${cities > 1 ? 'es' : ''}`);
    if (towns) parts.push(`${towns} Pueblo${towns > 1 ? 's' : ''}`);
    if (villages) parts.push(`${villages} Aldea${villages > 1 ? 's' : ''}`);
    const cap = sites.find(s => s.is_capital);
    const capName = cap ? ` · cap ${cap.name}` : '';
    return `${sites.length} sitio${sites.length !== 1 ? 's' : ''}: ${parts.join(' · ') || '—'}${capName}`;
}

export default function Sidebar({ tick, meta, paused, speed, onSpeed, onPause, selected, onSelect, worldId, worldMode, claims, standings, onClaim, lang = 'es', followId = null, onFollow, onWarClick }) {
    const [rankSort, setRankSort] = React.useState('points');
    const [rankFilter, setRankFilter] = React.useState('all');
    const [showKingdoms, setShowKingdoms] = React.useState(true);
    const sortedKingdoms = React.useMemo(() => {
        if (!tick) return [];
        let filtered = [...tick.kingdoms.filter(k => (k.sites?.length ?? 0) > 0)];
        if (rankFilter === 'war') filtered = filtered.filter(k => k.at_war);
        else if (rankFilter === 'peace') filtered = filtered.filter(k => !k.at_war);
        else if (rankFilter === 'tribal') filtered = filtered.filter(k => k.government === 'Tribal');
        else if (rankFilter === 'civilized') filtered = filtered.filter(k => k.government !== 'Tribal');
        return filtered.sort((a, b) => {
            if (rankSort === 'pop') return b.population - a.population;
            if (rankSort === 'sites') return b.sites.length - a.sites.length;
            if (rankSort === 'tech') {
                const ta = (a.tech_level ?? 0) * 100 + (a.techs?.length ?? 0);
                const tb = (b.tech_level ?? 0) * 100 + (b.techs?.length ?? 0);
                return tb - ta;
            }
            if (rankSort === 'military') return (b.military_power ?? 0) - (a.military_power ?? 0);
            if (rankSort === 'politics') {
                const ha = a.happiness ?? 50, hb = b.happiness ?? 50;
                return hb - ha;
            }
            // points: league standings if available, else computed totalScore
            const pa = standings?.find(s => s.kingdom_id === a.id)?.points ?? totalScore(a, tick.month);
            const pb = standings?.find(s => s.kingdom_id === b.id)?.points ?? totalScore(b, tick.month);
            return pb - pa;
        });
    }, [tick?.kingdoms, tick?.month, rankSort, rankFilter, standings]);

    if (!tick) return <aside style={aside}>waiting for simulation…</aside>;
    const kStyle = (k) => ({ color: `rgb(${k.color[0]},${k.color[1]},${k.color[2]})` });

    return (
        <aside style={aside}>
            <div style={{ marginBottom: 8 }}>
                <strong>{lang === 'es' ? 'Año' : 'Year'} {Math.floor(tick.month / 12)}</strong> — {lang === 'es' ? 'mes' : 'month'} {tick.month}
            </div>

            {/* controls */}
            <div style={{ marginBottom: 10, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                <button style={btn} onClick={onPause}>{paused ? (lang === 'es' ? '▶ correr' : '▶ run') : (lang === 'es' ? '⏸ pausa' : '⏸ pause')}</button>
                {[1, 3, 12, 48].map(s => (
                    <button key={s} style={{ ...btn, background: speed === s ? '#2f81f7' : btn.background }} onClick={() => onSpeed(s)}>x{s}</button>
                ))}
            </div>

            {/* Ranking */}
            <div style={{ ...sectionTitle, display: 'flex', alignItems: 'center', gap: 4 }}>
                Ranking — {tick.kingdoms.filter(k => k.sites.length).length} {lang === 'es' ? 'reinos' : 'kingdoms'}
                <button style={{ ...btn, fontSize: 10, padding: '1px 6px', marginLeft: 6 }} onClick={() => setShowKingdoms(s => !s)}>{showKingdoms ? '▾' : '▸'}</button>
            </div>
            {showKingdoms && (
                <div style={{ marginBottom: 6 }}>
                    <div style={{ display: 'flex', gap: 3, marginBottom: 4, flexWrap: 'wrap' }}>
                        {[
                            ['points', '★ Pts'], ['pop', '👥 Hab'], ['sites', '🏰 Terr'], ['tech', '📜 Tec'], ['military', '⚔ Mil'], ['politics', '🏛 Pol'],
                        ].map(([k, label]) => (
                            <button key={k} onClick={() => setRankSort(k)}
                                style={{ ...btn, fontSize: 10, padding: '1px 6px', background: rankSort === k ? '#2f81f7' : btn.background }}>{label}</button>
                        ))}
                    </div>
                    <div style={{ display: 'flex', gap: 3, marginBottom: 6, flexWrap: 'wrap' }}>
                        {[
                            ['all', 'Todos'], ['war', '⚔ Guerra'], ['peace', '☮ Paz'], ['civilized', 'Civil'], ['tribal', 'Tribal'],
                        ].map(([k, label]) => (
                            <button key={k} onClick={() => setRankFilter(k)}
                                style={{ ...btn, fontSize: 10, padding: '1px 6px', background: rankFilter === k ? '#2f81f7' : btn.background }}>{label}</button>
                        ))}
                    </div>
                    <div style={{ maxHeight: 175, overflowY: 'auto', paddingRight: 2 }}>
                        {sortedKingdoms.map((k, idx) => {
                            const leaguePts = standings?.find(s => s.kingdom_id === k.id)?.points;
                            const sc = scoreLabel(k, tick.month, lang);
                            return (
                                <div
                                    key={k.id}
                                    onClick={() => onSelect(k)}
                                    style={{ ...panel, cursor: 'pointer', marginBottom: 4, border: selected?.id === k.id ? '1px solid #2f81f7' : panel.border }}
                                >
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                                        <span style={{ fontSize: 11, color: '#f0c674', minWidth: 18 }}>#{idx + 1}</span>
                                        <span style={{ ...kStyle(k), fontWeight: 600, flex: 1, fontSize: 12 }}>{k.at_war ? '⚔ ' : ''}{k.name}</span>
                                        {rankSort === 'points' && (
                                            <span style={{ fontSize: 11, color: '#f0c674', fontWeight: 700 }} title={sc.parts}>
                                                {leaguePts !== undefined ? `${leaguePts}★` : `${sc.total} pts`}
                                            </span>
                                        )}
                                        <button
                                            title={followId === k.id ? 'stop following' : 'follow capital'}
                                            onClick={(e) => { e.stopPropagation(); onFollow && onFollow(followId === k.id ? null : k.id); }}
                                            style={{ ...btn, fontSize: 10, padding: '0 5px', marginLeft: 2, background: followId === k.id ? '#2f81f7' : btn.background }}
                                        >◎</button>
                                    </div>
                                    <div style={{ fontSize: 11, color: '#8b949e', marginTop: 2, lineHeight: 1.35 }}>
                                        {rankSort === 'pop' && <PopLine k={k} />}
                                        {rankSort === 'sites' && <SitesLine k={k} lang={lang} />}
                                        {rankSort === 'tech' && <TechLine k={k} lang={lang} />}
                                        {rankSort === 'military' && <MilitaryLine k={k} lang={lang} />}
                                        {rankSort === 'politics' && <PoliticsLine k={k} lang={lang} />}
                                        {rankSort === 'points' && <PointsLine k={k} tickMonth={tick.month} lang={lang} sc={sc} />}
                                    </div>
                                </div>
                            );
                        })}
                        {sortedKingdoms.length === 0 && (
                            <div style={{ fontSize: 11, color: '#6e7681', fontStyle: 'italic', textAlign: 'center', marginTop: 4 }}>Sin reinos en este filtro</div>
                        )}
                    </div>
                    {rankSort === 'points' && (
                        <div style={{ fontSize: 9, color: '#6e7681', marginTop: 4, lineHeight: 1.3 }}>
                            {lang === 'es'
                                ? '★ pts = 2/100 hab +12/sitio +14/ciudad +6/pueblo +10/era +2/tech +6/templo +14/ruta +8V−6D'
                                : '★ pts = 2/100 pop +12/site +14/city +6/town +10/era +2/tech +6/temple +14/route +8W−6L'}
                        </div>
                    )}
                </div>
            )}

            {((tick.fallen ?? []).length > 0 || tick.kingdoms.some(k => (k.sites?.length ?? 0) === 0)) && (
                <div style={{ fontSize: 10, color: '#6e7681', margin: '2px 0 6px 2px' }}>
                    🪦 {(tick.fallen ?? []).length > 0
                        ? tick.fallen.map(f => f.name).join(', ')
                        : tick.kingdoms.filter(k => (k.sites?.length ?? 0) === 0).map(k => k.name).join(', ')}
                </div>
            )}

            {tick.wars_ongoing.length > 0 && (
                <div style={{ fontSize: 11, color: '#f85149', marginBottom: 4 }}>
                    {tick.wars_ongoing.map((w, i) => (
                        <div key={i} onClick={() => onWarClick && onWarClick(w)} style={{ cursor: onWarClick ? 'pointer' : 'default', textDecoration: onWarClick ? 'underline' : 'none' }}>
                            ⚔ {w.name}: {w.a} vs {w.b} ({w.battles}b, {w.dead} dead) {onWarClick ? ' →' : ''}
                        </div>
                    ))}
                </div>
            )}

            {tick.beasts.filter(b => b.alive).length > 0 && (
                <div style={{ fontSize: 11, color: '#ff7800', marginBottom: 4 }}>
                    ▲ {tick.beasts.filter(b => b.alive).map(b => b.name).join(', ')}
                </div>
            )}

            <HitosPanel tick={tick} lang={lang} />
        </aside>
    );
}

// ── per-tab sub-lines (each shows unique, non-overlapping info) ──

function PopLine({ k }) {
    const sites = k.sites ?? [];
    const cap = sites.find(s => s.is_capital);
    const avg = sites.length ? Math.round(k.population / sites.length) : 0;
    const hist = k.population_history ?? [];
    const peak = hist.length ? Math.max(...hist) : k.population;
    const trend = peak > 0 ? (k.population >= peak ? '▲' : k.population > peak * 0.85 ? '─' : '▼') : '';
    return (
        <span>
            <b style={{ color: '#c9d1d9' }}>{Math.round(k.population).toLocaleString()}</b> hab
            {cap && <span> · cap <b style={{ color: '#c9d1d9' }}>{Math.round(cap.population).toLocaleString()}</b></span>}
            <span> · Ø {avg.toLocaleString()}/sitio</span>
            {hist.length > 1 && <span> · pico {Math.round(peak).toLocaleString()} {trend}</span>}
        </span>
    );
}

function SitesLine({ k, lang }) {
    const sites = k.sites ?? [];
    let cities = 0, towns = 0, villages = 0;
    for (const s of sites) {
        if (s.category === 'City') cities++;
        else if (s.category === 'Town') towns++;
        else villages++;
    }
    const parts = [];
    if (cities) parts.push(`${cities} Ciudad${cities > 1 ? 'es' : ''}`);
    if (towns) parts.push(`${towns} Pueblo${towns > 1 ? 's' : ''}`);
    if (villages) parts.push(`${villages} Aldea${villages > 1 ? 's' : ''}`);
    const cap = sites.find(s => s.is_capital);
    return (
        <span>
            <b style={{ color: '#c9d1d9' }}>{sites.length}</b> sitio{sites.length !== 1 ? 's' : ''}: {parts.join(' · ') || '—'}
            {cap && <span style={{ color: '#6e7681' }}> · cap {cap.name}</span>}
        </span>
    );
}

function TechLine({ k, lang }) {
    const era = vocab(lang, ERA_NAMES[k.tech_level] ?? `Era ${k.tech_level}`);
    const nTechs = (k.techs ?? []).length;
    const totalTechs = 24; // approx total nodes
    return (
        <span>
            <b style={{ color: '#d2a8ff' }}>{era}</b> · {nTechs} techs
            {nTechs > 0 && <span style={{ color: '#6e7681' }}> ({Math.round(nTechs / totalTechs * 100)}%)</span>}
            {k.golden_age && <span style={{ color: '#f0c674' }}> ✨ dorada</span>}
            {k.drought && <span style={{ color: '#ff7800' }}> 🏜️ sequía</span>}
        </span>
    );
}

function MilitaryLine({ k, lang }) {
    const army = k.army_size ?? Math.round(k.population * (k.militarization_pct ?? 20) / 100);
    const power = k.military_power ?? '?';
    const pct = k.militarization_pct ?? 20;
    const hero = k.hero ? ` · ♛ ${k.hero.name} ×${k.hero.strength}` : '';
    const leader = k.leader ? ` · ${k.leader.title} ${k.leader.reign_years}y` : '';
    return (
        <span>
            ej <b style={{ color: '#c9d1d9' }}>{Math.round(army).toLocaleString()}</b>
            {' '}· poder <b style={{ color: '#f85149' }}>{typeof power === 'number' ? Math.round(power).toLocaleString() : power}</b>
            <span style={{ color: '#6e7681' }}> · {pct}% milit</span>
            <span style={{ color: '#8b949e' }}>{hero}{leader}</span>
        </span>
    );
}

function PoliticsLine({ k, lang }) {
    const happy = Math.round(k.happiness ?? 50);
    const happyColor = happy >= 65 ? '#3fb950' : happy >= 35 ? '#d29922' : '#f85149';
    const happyIcon = happy >= 65 ? '😊' : happy >= 35 ? '😐' : '😡';
    const faith = k.faith ? vocab(lang, k.faith) : '';
    const nTemples = (k.temples ?? []).length;
    const nRoutes = (k.trade_routes ?? []).length;
    const wars = (k.wars_won ?? 0) || (k.wars_lost ?? 0) ? ` · ${k.wars_won ?? 0}V/${k.wars_lost ?? 0}D` : '';
    return (
        <span>
            {vocab(lang, k.government)}{k.civilized ? '' : ` (${lang === 'es' ? 'tribal' : 'tribal'})`}
            <span style={{ color: happyColor }}> · {happyIcon} {happy}%</span>
            {faith && <span> · {faith}{nTemples ? ` ${nTemples}⛪` : ''}</span>}
            {nRoutes > 0 && <span style={{ color: '#58a6ff' }}> · {nRoutes} ruta{nRoutes > 1 ? 's' : ''}</span>}
            <span style={{ color: '#8b949e' }}>{wars}</span>
        </span>
    );
}

function PointsLine({ k, tickMonth, lang, sc }) {
    return (
        <span title={sc.parts}>
            <b style={{ color: '#f0c674' }}>{sc.total} pts</b>
            <span style={{ color: '#6e7681', fontSize: 10 }}> — {sc.parts || '—'}</span>
            <span style={{ color: '#8b949e' }}> · {vocab(lang, k.race)}</span>
            {k.golden_age && <span style={{ color: '#f0c674' }}> ✨</span>}
        </span>
    );
}

function HitosPanel({ tick, lang }) {
    const [open, setOpen] = React.useState(true);
    const records = React.useMemo(() => computeRecords(tick, lang), [tick, lang]);
    if (!records.length) return null;
    return (
        <>
            <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                <button style={{ ...btn, fontSize: 11, flex: 1 }} onClick={() => setOpen(o => !o)}>
                    {open ? '▾' : '▸'} {t(lang, 'records')} ({records.length})
                </button>
            </div>
            {open && (
                <div style={{ marginTop: 4, border: '1px solid #21262d', borderRadius: 4, padding: '6px 8px', background: '#161b22' }}>
                    {records.map((r, i) => (
                        <div key={i} style={{ fontSize: 11, marginBottom: 2 }}>
                            <span style={{ color: '#d2a8ff' }}>{r.icon} {r.label}:</span>{' '}{r.value}
                        </div>
                    ))}
                </div>
            )}
        </>
    );
}

function computeRecords(tick, lang) {
    const out = [];
    if (!tick?.kingdoms?.length) return out;
    let city = null;
    for (const k of tick.kingdoms) for (const s of k.sites ?? []) if (!city || s.population > city.pop) city = { name: s.name, pop: s.population, k: k.name };
    if (city) out.push({ icon: '🏙', label: vocab(lang, 'city'), value: `${city.name} (${city.k}) — ${Math.floor(city.pop).toLocaleString()}` });
    const cells = tick.battle_heat?.cells ?? [];
    if (cells.length) { const top = cells.reduce((a, b) => (b.v > a.v ? b : a), cells[0]); out.push({ icon: '🩸', label: vocab(lang, 'battle'), value: `(${top.x},${top.y}) — ${Math.floor(top.v).toLocaleString()} dead` }); }
    const wars = [...(tick.wars_recent ?? []), ...(tick.wars_ongoing ?? []).map(w => ({ ...w, battles: w.battles }))];
    if (wars.length) { const top = wars.reduce((a, b) => ((b.battles ?? 0) > (a.battles ?? 0) ? b : a), wars[0]); out.push({ icon: '⚔', label: vocab(lang, 'war'), value: `${top.name} — ${top.battles ?? 0} battles` }); }
    let reign = null;
    for (const k of tick.kingdoms) if (k.leader && (!reign || k.leader.reign_years > reign.years)) reign = { name: `${k.leader.title} ${k.leader.name}`, years: k.leader.reign_years, k: k.name };
    if (reign) out.push({ icon: '👑', label: vocab(lang, 'reign'), value: `${reign.name} (${reign.k}) — ${reign.years}y` });
    const empire = tick.kingdoms.reduce((a, b) => ((b.sites?.length ?? 0) > (a.sites?.length ?? 0) ? b : a));
    if (empire) out.push({ icon: '🗿', label: vocab(lang, 'empire'), value: `${empire.name} — ${empire.sites?.length ?? 0} sites` });
    const aged = tick.kingdoms.filter(k => k.founded_month !== undefined);
    if (aged.length) { const oldest = aged.reduce((a, b) => (a.founded_month < b.founded_month ? a : b)); const y = Math.floor((tick.month - oldest.founded_month) / 12); out.push({ icon: '🏛', label: vocab(lang, 'realm'), value: `${oldest.name} — ${y}y old` }); }
    return out;
}

const aside = {
    width: 360,
    minWidth: 300,
    background: '#0d1117',
    borderLeft: '1px solid #21262d',
    padding: 12,
    display: 'flex',
    flexDirection: 'column',
    overflowY: 'auto',
    overflowX: 'hidden',
    fontSize: 14,
    flexShrink: 0,
};

const panel = { padding: '5px 7px', background: '#161b22', borderRadius: 4, border: '1px solid #21262d' };
const sectionTitle = { color: '#58a6ff', fontSize: 12, marginTop: 8, marginBottom: 4, textTransform: 'uppercase', letterSpacing: 1 };
const btn = { background: '#21262d', border: '1px solid #30363d', color: '#c9d1d9', borderRadius: 4, padding: '2px 8px', cursor: 'pointer', fontSize: 12 };
