import React from 'react';
import RoomPanel from './RoomPanel.jsx';
import { renderEvent, t, vocab } from '../i18n.js';

/** Right sidebar: kingdoms list, ongoing wars, chronicle feed with filters.
 * (Per-kingdom stats live in the floating StatsPanel over the map.) */
export default function Sidebar({ tick, meta, paused, speed, onSpeed, onPause, selected, onSelect, worldId, worldMode, claims, standings, onClaim, lang = 'es', followId = null, onFollow }) {
    const [rankSort, setRankSort] = React.useState('points'); // points|pop|sites|tech|military
    const [rankFilter, setRankFilter] = React.useState('all'); // all|war|peace|tribal|civilized
    const [showKingdoms, setShowKingdoms] = React.useState(true);
    const [showRoom, setShowRoom] = React.useState(false);
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
                    <button
                        key={s}
                        style={{ ...btn, background: speed === s ? '#2f81f7' : btn.background }}
                        onClick={() => onSpeed(s)}
                    >x{s}</button>
                ))}
            </div>

            {/* Ranking de Reinos — sortable */}
            <div style={{ ...sectionTitle, display: 'flex', alignItems: 'center', gap: 4 }}>
                Ranking — Kingdoms {tick.kingdoms.filter(k => k.sites.length).length}
                <button
                    style={{ ...btn, fontSize: 10, padding: '1px 6px', marginLeft: 6 }}
                    onClick={() => setShowKingdoms(s => !s)}
                >{showKingdoms ? '▾' : '▸'}</button>
            </div>
            {showKingdoms && (() => {
                const sortKey = rankSort;
                let filtered = [...tick.kingdoms.filter(k => (k.sites?.length ?? 0) > 0)];
                if (rankFilter === 'war') filtered = filtered.filter(k => k.at_war);
                else if (rankFilter === 'peace') filtered = filtered.filter(k => !k.at_war);
                else if (rankFilter === 'tribal') filtered = filtered.filter(k => k.government === 'Tribal');
                else if (rankFilter === 'civilized') filtered = filtered.filter(k => k.government !== 'Tribal');
                const sorted = filtered.sort((a,b) => {
                    if (sortKey === 'pop') return b.population - a.population;
                    if (sortKey === 'sites') return b.sites.length - a.sites.length;
                    if (sortKey === 'tech') return (b.tech_level ?? 0) - (a.tech_level ?? 0);
                    if (sortKey === 'military') {
                        const pa = (a.population||0)*(a.militarization_pct||20)/100;
                        const pb = (b.population||0)*(b.militarization_pct||20)/100;
                        return pb - pa;
                    }
                    // points (standings)
                    const sa = (standings?.find(s=>s.kingdom_id===a.id)?.points ?? 0);
                    const sb = (standings?.find(s=>s.kingdom_id===b.id)?.points ?? 0);
                    return sb - sa;
                });
                return (
                <div style={{ marginBottom: 6 }}>
                    <div style={{ display: 'flex', gap: 4, marginBottom: 4, flexWrap: 'wrap' }}>
                        {[
                            ['points','Pts'],['pop','Pop'],['sites','Sites'],['tech','Tech'],['military','Mil'],
                        ].map(([k,label]) => (
                            <button key={k} onClick={()=>setRankSort(k)}
                                style={{ ...btn, fontSize: 10, padding: '1px 6px', background: rankSort===k ? '#2f81f7' : btn.background }}>{label}</button>
                        ))}
                    </div>
                    <div style={{ display: 'flex', gap: 4, marginBottom: 6, flexWrap: 'wrap' }}>
                        {[
                            ['all','Todos'],['war','⚔ Guerra'],['peace','☮ Paz'],['civilized','Civil'],['tribal','Tribal'],
                        ].map(([k,label]) => (
                            <button key={k} onClick={()=>setRankFilter(k)}
                                style={{ ...btn, fontSize: 10, padding: '1px 6px', background: rankFilter===k ? '#2f81f7' : btn.background }}>{label}</button>
                        ))}
                    </div>
                    <div style={{ maxHeight: 320, overflowY: 'auto' }}>
                    {sorted.map((k, idx) => {
                        const pts = standings?.find(s=>s.kingdom_id===k.id)?.points;
                        return (
                        <div
                            key={k.id}
                            onClick={() => onSelect(k)}
                            style={{ ...panel, cursor: 'pointer', marginBottom: 6, border: selected?.id === k.id ? '1px solid #2f81f7' : panel.border, fontSize: 13 }}
                        >
                            <div style={{ display:'flex', alignItems:'center', gap: 4 }}>
                                <span style={{ fontSize: 11, color:'#f0c674', minWidth: 18 }}>#{idx+1}</span>
                                <span style={{ ...kStyle(k), fontWeight: 600, flex: 1 }}>{k.at_war ? '⚔ ' : ''}{k.name}</span>
                                {pts !== undefined && <span style={{ fontSize: 11, color:'#f0c674' }}>{pts}pts</span>}
                                <button
                                    title={followId === k.id ? 'stop following' : 'follow capital'}
                                    onClick={(e) => { e.stopPropagation(); onFollow && onFollow(followId === k.id ? null : k.id); }}
                                    style={{ ...btn, fontSize: 10, padding: '0 5px', marginLeft: 4,
                                             background: followId === k.id ? '#2f81f7' : btn.background }}
                                >◎</button>
                            </div>
                            <div style={{ fontSize: 12, color: '#8b949e' }}>
                                {vocab(lang, k.race)} · {vocab(lang, k.government)}{k.golden_age ? ' ✨' : ''} · {vocab(lang, ['Stone Age', 'Bronze Age', 'Iron Age', 'Classical Era', 'Medieval Era'][k.tech_level] ?? '')}
                            </div>
                            <div style={{ fontSize: 12, color: '#c9d1d9' }}>pop {k.population.toLocaleString()} · {k.sites.length} aldeas · mil {k.military_power ?? '?'}</div>
                            {k.leader && (
                                <div style={{ fontSize: 12 }}>
                                    {vocab(lang, k.leader.title)} {k.leader.name} ({k.leader.reign_years}y)
                                    {k.hero && <span style={{ color: '#f0c674' }}> · champion {k.hero.name}</span>}
                                </div>
                            )}
                        </div>
                    )})}
                    </div>
                </div>
                );
            })()}

            {/* fallen realms memorial: collapsed kingdoms, greyed out */}
            {((tick.fallen ?? []).length > 0 || tick.kingdoms.some(k => (k.sites?.length ?? 0) === 0)) && (
                <div style={{ fontSize: 10, color: '#6e7681', margin: '2px 0 6px 2px' }}>
                    🪦 {(tick.fallen ?? []).length > 0
                        ? tick.fallen.map(f => f.name).join(', ')
                        : tick.kingdoms.filter(k => (k.sites?.length ?? 0) === 0).map(k => k.name).join(', ')}
                </div>
            )}

            {/* ongoing wars: inline compact */}
            {tick.wars_ongoing.length > 0 && (
                <div style={{ fontSize: 11, color: '#f85149', marginBottom: 4 }}>
                    {tick.wars_ongoing.map((w, i) => (
                        <div key={i}>
                            ⚔ {w.name}: {w.a} vs {w.b} ({w.battles}b, {w.dead} dead)
                        </div>
                    ))}
                </div>
            )}

            {/* beasts: single line */}
            {tick.beasts.filter(b => b.alive).length > 0 && (
                <div style={{ fontSize: 11, color: '#ff7800', marginBottom: 4 }}>
                    ▲ {tick.beasts.filter(b => b.alive).map(b => b.name).join(', ')}
                </div>
            )}

            {/* chronicle: guaranteed space + internal scroll + auto-scroll */}
            <ChronicleFeed events={tick.events} lang={lang} tick={tick} />

            {/* hitos: world-firsts timeline + auto-computed records */}
            <HitosPanel tick={tick} lang={lang} />

            {/* multiplayer room (prophecies + chat + ranks), collapsible */}
            <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                <button
                    style={{ ...btn, fontSize: 11, flex: 1 }}
                    onClick={() => setShowRoom(s => !s)}
                >{showRoom ? '▾ room' : '▸ room (multiplayer)'}</button>
            </div>
            {showRoom && (
                <RoomPanel worldId={worldId} month={tick.month} meta={meta}
                    claims={claims} standings={standings} tick={tick} onClaim={onClaim} />
            )}
        </aside>
    );
}

function coloredParts(text, tick) {
    if (!tick?.kingdoms) return text;
    // Build sorted by length desc to avoid substring collisions
    const names = [...tick.kingdoms].sort((a,b)=>b.name.length-a.name.length);
    let parts = [text];
    for (const k of names) {
        const color = `rgb(${k.color[0]},${k.color[1]},${k.color[2]})`;
        const next = [];
        for (const p of parts) {
            if (typeof p !== 'string') { next.push(p); continue; }
            const idx = p.indexOf(k.name);
            if (idx === -1) next.push(p);
            else {
                if (idx>0) next.push(p.slice(0, idx));
                next.push(<span key={k.id+idx} style={{ color, fontWeight: 600 }}>{k.name}</span>);
                if (idx + k.name.length < p.length) next.push(p.slice(idx + k.name.length));
            }
        }
        parts = next;
    }
    // also colorize site names (smaller, italic)
    return parts;
}

/** Chronicle feed with type filters, milestone highlighting, auto-scroll. */
function ChronicleFeed({ events, lang, tick }) {
    const [filter, setFilter] = React.useState('ALL');
    const boxRef = React.useRef(null);
    const [autoScroll, setAutoScroll] = React.useState(true);
    const FILTERS = {
        ALL: () => true,
        MILESTONE: e => e.type === 'MILESTONE',
        WAR: e => ['WAR_DECLARED', 'BATTLE', 'WAR_ENDED', 'SITE_CAPTURED', 'ALLIANCE_JOINED_WAR'].includes(e.type),
        LEADER: e => ['LEADER_CROWNED', 'LEADER_DIED', 'GOVERNMENT_CHANGED', 'HERO_EMERGED', 'HERO_FELL'].includes(e.type),
        BEAST: e => ['BEAST_SIGHTED', 'BEAST_SLAIN', 'BEAST_RAMPAGE'].includes(e.type),
        ERA: e => ['TECH_ERA', 'GOLDEN_AGE', 'PLAGUE', 'COLONY_FOUNDED', 'INDEPENDENCE', 'REBELLION'].includes(e.type),
    };
    const shown = events.filter(FILTERS[filter]);
    React.useEffect(() => {
        if (autoScroll && boxRef.current) {
            boxRef.current.scrollTop = boxRef.current.scrollHeight;
        }
    }, [events, autoScroll, filter]);
    const onScroll = () => {
        const el = boxRef.current;
        if (!el) return;
        setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
    };
    return (
        <>
            <div style={{ ...sectionTitle, display: 'flex', alignItems: 'center' }}>
                {t(lang, 'chronicle')}
                <span style={{ flex: 1 }} />
                <span style={{ fontSize: 9, color: autoScroll ? '#58a6ff' : '#8b949e' }}>
                    {autoScroll ? 'auto ▼' : '▲'}
                </span>
            </div>
            <div style={{ display: 'flex', gap: 3, marginBottom: 4, flexWrap: 'wrap' }}>
                {Object.keys(FILTERS).map(f => (
                    <button
                        key={f}
                        style={{ ...btn, fontSize: 10, padding: '1px 5px',
                                 background: filter === f ? '#2f81f7' : btn.background }}
                        onClick={() => setFilter(f)}
                    >
                        {f === 'ALL' ? 'all' :
                         f === 'MILESTONE' ? '★' :
                         f === 'WAR' ? '⚔' :
                         f === 'LEADER' ? '♛' :
                         f === 'BEAST' ? '▲' : '✦'}
                    </button>
                ))}
            </div>
            <div
                ref={boxRef}
                onScroll={onScroll}
                style={{ overflowY: 'auto', flex: 1, minHeight: 200 }}
            >
                {shown.slice().reverse().map((e, i) => (
                    <div
                        key={i}
                        style={{
                            fontSize: 13, marginBottom: 4,
                            color: e.type === 'MILESTONE' ? '#f0c674' : undefined,
                            fontWeight: e.type === 'MILESTONE' ? 'bold' : undefined,
                            opacity: 0.92,
                        }}
                    >
                        <span style={{ color: '#58a6ff' }}>[y{Math.floor(e.month / 12)}]</span>{' '}
                        {coloredParts(renderEvent(lang, e), tick)}
                    </div>
                ))}
            </div>
        </>
    );
}

/** Hitos: world-firsts timeline + auto-computed world records. */
function HitosPanel({ tick, lang }) {
    const [open, setOpen] = React.useState(false);
    const milestones = tick.milestones ?? [];
    const records = React.useMemo(() => computeRecords(tick), [tick]);
    return (
        <>
            <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                <button
                    style={{ ...btn, fontSize: 11, flex: 1 }}
                    onClick={() => setOpen(o => !o)}
                >{open ? '▾' : '▸'} {t(lang, 'milestones')} ({milestones.length})</button>
            </div>
            {open && (
                <div style={{ maxHeight: 300, overflowY: 'auto', marginTop: 4 }}>
                    {milestones.slice().reverse().map((e, i) => (
                        <div key={i} style={{ fontSize: 11, marginBottom: 3, color: '#f0c674' }}>
                            <span style={{ color: '#58a6ff' }}>[y{Math.floor(e.month / 12)}]</span>{' '}
                            {renderEvent(lang, e)}
                        </div>
                    ))}
                    {records.length > 0 && (
                        <>
                            <div style={{ ...sectionTitle, fontSize: 11 }}>{t(lang, 'records')}</div>
                            {records.map((r, i) => (
                                <div key={i} style={{ fontSize: 11, marginBottom: 2 }}>
                                    <span style={{ color: '#d2a8ff' }}>{r.icon} {r.label}:</span>{' '}
                                    {r.value}
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
    // largest city
    let city = null;
    for (const k of tick.kingdoms) {
        for (const s of k.sites ?? []) {
            if (!city || s.population > city.pop) city = { name: s.name, pop: s.population, k: k.name };
        }
    }
    if (city) out.push({ icon: '🏙', label: 'city', value: `${city.name} (${city.k}) — ${Math.floor(city.pop).toLocaleString()}` });
    // bloodiest battle spot (live heat cells)
    const cells = tick.battle_heat?.cells ?? [];
    if (cells.length) {
        const top = cells.reduce((a, b) => (b.v > a.v ? b : a), cells[0]);
        out.push({ icon: '🩸', label: 'battle', value: `(${top.x},${top.y}) — ${Math.floor(top.v).toLocaleString()} dead` });
    }
    // longest war (finished recent + ongoing)
    const wars = [...(tick.wars_recent ?? []), ...(tick.wars_ongoing ?? []).map(w => ({ ...w, battles: w.battles }))];
    if (wars.length) {
        const top = wars.reduce((a, b) => ((b.battles ?? 0) > (a.battles ?? 0) ? b : a), wars[0]);
        out.push({ icon: '⚔', label: 'war', value: `${top.name} — ${top.battles ?? 0} battles` });
    }
    // longest reign among living leaders
    let reign = null;
    for (const k of tick.kingdoms) {
        if (k.leader && (!reign || k.leader.reign_years > reign.years)) {
            reign = { name: `${k.leader.title} ${k.leader.name}`, years: k.leader.reign_years, k: k.name };
        }
    }
    if (reign) out.push({ icon: '👑', label: 'reign', value: `${reign.name} (${reign.k}) — ${reign.years}y` });
    // largest empire
    const empire = tick.kingdoms.reduce((a, b) => ((b.sites?.length ?? 0) > (a.sites?.length ?? 0) ? b : a));
    if (empire) out.push({ icon: '🗺', label: 'empire', value: `${empire.name} — ${empire.sites?.length ?? 0} sites` });
    // oldest realm
    const aged = tick.kingdoms.filter(k => k.founded_month !== undefined);
    if (aged.length) {
        const oldest = aged.reduce((a, b) => (a.founded_month < b.founded_month ? a : b));
        const y = Math.floor((tick.month - oldest.founded_month) / 12);
        out.push({ icon: '🏛', label: 'realm', value: `${oldest.name} — ${y}y old` });
    }
    return out;
}

/** Inline SVG population history chart for a kingdom. */
function PopulationChart({ values, color }) {
    const W = 300, H = 50;
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const rng = Math.max(1, hi - lo);
    const pts = values.map((v, i) => {
        const x = (i / Math.max(1, values.length - 1)) * W;
        const y = H - 4 - ((v - lo) / rng) * (H - 8);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    return (
        <div style={{ marginTop: 6 }}>
            <svg width={W} height={H} style={{ background: '#0d1117', borderRadius: 4 }}>
                <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
            </svg>
            <div style={{ fontSize: 10, color: '#8b949e' }}>
                population over {values.length} years — min {lo.toLocaleString()} · max {hi.toLocaleString()}
            </div>
        </div>
    );
}

const aside = {
    width: 420,
    minWidth: 360,
    background: '#0d1117',
    borderLeft: '1px solid #21262d',
    padding: 14,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',  // panels scroll internally; the aside itself never scrolls
    fontSize: 14,
};

const panel = {
    padding: '4px 6px',
    background: '#161b22',
    borderRadius: 4,
    border: '1px solid #21262d',
};

const sectionTitle = {
    color: '#58a6ff',
    fontSize: 12,
    marginTop: 8,
    marginBottom: 4,
    textTransform: 'uppercase',
    letterSpacing: 1,
};

const btn = {
    background: '#21262d',
    border: '1px solid #30363d',
    color: '#c9d1d9',
    borderRadius: 4,
    padding: '2px 8px',
    cursor: 'pointer',
    fontSize: 12,
};
