import React from 'react';
import RoomPanel from './RoomPanel.jsx';
import { renderEvent, t, vocab } from '../i18n.js';

/**
 * Right sidebar — compact ranking with slim per-filter views + hitos.
 * Chronicle lives below the map (ChroniclePanel). Stats are tabbed (StatsPanel).
 */
const ERA_NAMES = ['Stone Age', 'Bronze Age', 'Iron Age', 'Classical Era', 'Medieval Era', 'Renaissance', 'Enlightenment'];

export default function Sidebar({ tick, meta, paused, speed, onSpeed, onPause, selected, onSelect, worldId, worldMode, claims, standings, onClaim, lang = 'es', followId = null, onFollow, onWarClick }) {
    const [rankSort, setRankSort] = React.useState('points'); // points|pop|sites|tech|military|politics
    const [rankFilter, setRankFilter] = React.useState('all'); // all|war|peace|tribal|civilized
    const [showKingdoms, setShowKingdoms] = React.useState(true);
    const [showRoom, setShowRoom] = React.useState(false);
    const sortedKingdoms = React.useMemo(() => {
        if (!tick) return [];
        let filtered = [...tick.kingdoms.filter(k => (k.sites?.length ?? 0) > 0)];
        if (rankFilter === 'war') filtered = filtered.filter(k => k.at_war);
        else if (rankFilter === 'peace') filtered = filtered.filter(k => !k.at_war);
        else if (rankFilter === 'tribal') filtered = filtered.filter(k => k.government === 'Tribal');
        else if (rankFilter === 'civilized') filtered = filtered.filter(k => k.government !== 'Tribal');
        const ptsMap = new Map((standings ?? []).map(s => [s.kingdom_id, s.points ?? 0]));
        return filtered.sort((a, b) => {
            if (rankSort === 'pop') return b.population - a.population;
            if (rankSort === 'sites') return b.sites.length - a.sites.length;
            if (rankSort === 'tech') return (b.tech_level ?? 0) - (a.tech_level ?? 0);
            if (rankSort === 'military') {
                const pa = (a.population || 0) * (a.militarization_pct || 20) / 100;
                const pb = (b.population || 0) * (b.militarization_pct || 20) / 100;
                return pb - pa;
            }
            if (rankSort === 'politics') {
                const ha = a.happiness ?? 50, hb = b.happiness ?? 50;
                return hb - ha;
            }
            return (ptsMap.get(b.id) ?? 0) - (ptsMap.get(a.id) ?? 0);
        });
    }, [tick?.kingdoms, rankSort, rankFilter, standings]);

    if (!tick) return <aside style={aside}>waiting for simulation…</aside>;
    const kStyle = (k) => ({ color: `rgb(${k.color[0]},${k.color[1]},${k.color[2]})` });

    return (
        <aside style={aside}>
            <div style={{ marginBottom: 8 }}>
                <strong>Year {Math.floor(tick.month / 12)}</strong> — month {tick.month}
            </div>

            {/* controls */}
            <div style={{ marginBottom: 10, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                <button style={btn} onClick={onPause}>{paused ? '▶ run' : '⏸ pause'}</button>
                {[1, 3, 12, 48].map(s => (
                    <button key={s} style={{ ...btn, background: speed === s ? '#2f81f7' : btn.background }} onClick={() => onSpeed(s)}>x{s}</button>
                ))}
            </div>

            {/* Ranking — compact, per-filter slim cards */}
            <div style={{ ...sectionTitle, display: 'flex', alignItems: 'center', gap: 4 }}>
                Ranking — Kingdoms {tick.kingdoms.filter(k => k.sites.length).length}
                <button style={{ ...btn, fontSize: 10, padding: '1px 6px', marginLeft: 6 }} onClick={() => setShowKingdoms(s => !s)}>{showKingdoms ? '▾' : '▸'}</button>
            </div>
            {showKingdoms && (
                <div style={{ marginBottom: 6 }}>
                    <div style={{ display: 'flex', gap: 3, marginBottom: 4, flexWrap: 'wrap' }}>
                        {[
                            ['points', 'Pts'], ['pop', 'Pop'], ['sites', 'Sites'], ['tech', 'Tech'], ['military', 'Mil'], ['politics', 'Política'],
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
                    <div>
                        {sortedKingdoms.slice(0, 3).map((k, idx) => {
                            const pts = standings?.find(s => s.kingdom_id === k.id)?.points;
                            return (
                                <div
                                    key={k.id}
                                    onClick={() => onSelect(k)}
                                    style={{ ...panel, cursor: 'pointer', marginBottom: 4, border: selected?.id === k.id ? '1px solid #2f81f7' : panel.border }}
                                >
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                                        <span style={{ fontSize: 11, color: '#f0c674', minWidth: 18 }}>#{idx + 1}</span>
                                        <span style={{ ...kStyle(k), fontWeight: 600, flex: 1, fontSize: 12 }}>{k.at_war ? '⚔ ' : ''}{k.name}</span>
                                        {pts !== undefined && rankSort === 'points' && <span style={{ fontSize: 11, color: '#f0c674' }}>{pts}pts</span>}
                                        <button
                                            title={followId === k.id ? 'stop following' : 'follow capital'}
                                            onClick={(e) => { e.stopPropagation(); onFollow && onFollow(followId === k.id ? null : k.id); }}
                                            style={{ ...btn, fontSize: 10, padding: '0 5px', marginLeft: 2, background: followId === k.id ? '#2f81f7' : btn.background }}
                                        >◎</button>
                                    </div>
                                    <div style={{ fontSize: 11, color: '#8b949e', marginTop: 2 }}>
                                        {rankSort === 'pop' && <span>pop <b style={{ color: '#c9d1d9' }}>{k.population.toLocaleString()}</b> · {k.sites.length} aldeas</span>}
                                        {rankSort === 'sites' && <span><b style={{ color: '#c9d1d9' }}>{k.sites.length}</b> aldeas · pop {k.population.toLocaleString()}</span>}
                                        {rankSort === 'tech' && <span>{vocab(lang, ERA_NAMES[k.tech_level] ?? '')} · {k.sites.length} sites · pop {k.population.toLocaleString()}</span>}
                                        {rankSort === 'military' && <span>mil <b style={{ color: '#c9d1d9' }}>{k.military_power ?? '?'}</b> · {k.sites.length} aldeas · {vocab(lang, k.race)}</span>}
                                        {rankSort === 'politics' && <span>{vocab(lang, k.government)}{k.civilized ? ' · civ' : ''} · felicidad {Math.round(k.happiness ?? 50)}% · {vocab(lang, k.race)}</span>}
                                        {rankSort === 'points' && (
                                            <span>{vocab(lang, k.race)} · {vocab(lang, k.government)}{k.golden_age ? ' ✨' : ''} · pop {k.population.toLocaleString()}</span>
                                        )}
                                    </div>
                                </div>
                            );
                        })}
                        {sortedKingdoms.length > 3 && (
                            <div style={{ fontSize: 10, color: '#6e7681', textAlign: 'center', marginTop: 4 }}>
                                +{sortedKingdoms.length - 3} más — usa los filtros para ver otros
                            </div>
                        )}
                        {sortedKingdoms.length === 0 && (
                            <div style={{ fontSize: 11, color: '#6e7681', fontStyle: 'italic', textAlign: 'center', marginTop: 4 }}>Sin reinos en este filtro</div>
                        )}
                    </div>
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

            {/* hitos: open by default so they're visible — uses e.id keys */}
            <HitosPanel tick={tick} lang={lang} />
        </aside>
    );
}

function HitosPanel({ tick, lang }) {
    const [open, setOpen] = React.useState(true);
    const milestones = tick.milestones ?? [];
    const records = React.useMemo(() => computeRecords(tick), [tick]);
    return (
        <>
            <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                <button style={{ ...btn, fontSize: 11, flex: 1 }} onClick={() => setOpen(o => !o)}>
                    {open ? '▾' : '▸'} {t(lang, 'milestones')} ({milestones.length})
                </button>
            </div>
            {open && (
                <div style={{ maxHeight: 300, overflowY: 'auto', marginTop: 4, border: '1px solid #21262d', borderRadius: 4, padding: '6px 8px', background: '#161b22' }}>
                    {milestones.length === 0 && (
                        <div style={{ fontSize: 11, color: '#6e7681', fontStyle: 'italic' }}>
                            {lang === 'es' ? 'Aún no hay hitos — la historia se escribe…' : 'No milestones yet — history is being written…'}
                        </div>
                    )}
                    {milestones.slice().reverse().map((e, _idx) => (
                        <div key={e.id ?? `${_idx}-${e.month}-${e.type}`} style={{ fontSize: 11, marginBottom: 3, color: '#f0c674' }}>
                            <span style={{ color: '#58a6ff' }}>[y{Math.floor(e.month / 12)}]</span>{' '}{renderEvent(lang, e)}
                        </div>
                    ))}
                    {records.length > 0 && (
                        <>
                            <div style={{ ...sectionTitle, fontSize: 11 }}>{t(lang, 'records')}</div>
                            {records.map((r, i) => (
                                <div key={i} style={{ fontSize: 11, marginBottom: 2 }}>
                                    <span style={{ color: '#d2a8ff' }}>{r.icon} {r.label}:</span>{' '}{r.value}
                                </div>
                            ))}
                        </>
                    )}
                </div>
            )}
        </>
    );
}

function computeRecords(tick) {
    const out = [];
    if (!tick?.kingdoms?.length) return out;
    let city = null;
    for (const k of tick.kingdoms) for (const s of k.sites ?? []) if (!city || s.population > city.pop) city = { name: s.name, pop: s.population, k: k.name };
    if (city) out.push({ icon: '🏙', label: 'city', value: `${city.name} (${city.k}) — ${Math.floor(city.pop).toLocaleString()}` });
    const cells = tick.battle_heat?.cells ?? [];
    if (cells.length) { const top = cells.reduce((a, b) => (b.v > a.v ? b : a), cells[0]); out.push({ icon: '🩸', label: 'battle', value: `(${top.x},${top.y}) — ${Math.floor(top.v).toLocaleString()} dead` }); }
    const wars = [...(tick.wars_recent ?? []), ...(tick.wars_ongoing ?? []).map(w => ({ ...w, battles: w.battles }))];
    if (wars.length) { const top = wars.reduce((a, b) => ((b.battles ?? 0) > (a.battles ?? 0) ? b : a), wars[0]); out.push({ icon: '⚔', label: 'war', value: `${top.name} — ${top.battles ?? 0} battles` }); }
    let reign = null;
    for (const k of tick.kingdoms) if (k.leader && (!reign || k.leader.reign_years > reign.years)) reign = { name: `${k.leader.title} ${k.leader.name}`, years: k.leader.reign_years, k: k.name };
    if (reign) out.push({ icon: '👑', label: 'reign', value: `${reign.name} (${reign.k}) — ${reign.years}y` });
    const empire = tick.kingdoms.reduce((a, b) => ((b.sites?.length ?? 0) > (a.sites?.length ?? 0) ? b : a));
    if (empire) out.push({ icon: '🗺', label: 'empire', value: `${empire.name} — ${empire.sites?.length ?? 0} sites` });
    const aged = tick.kingdoms.filter(k => k.founded_month !== undefined);
    if (aged.length) { const oldest = aged.reduce((a, b) => (a.founded_month < b.founded_month ? a : b)); const y = Math.floor((tick.month - oldest.founded_month) / 12); out.push({ icon: '🏛', label: 'realm', value: `${oldest.name} — ${y}y old` }); }
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
