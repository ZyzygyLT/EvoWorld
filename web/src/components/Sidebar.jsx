import React from 'react';
import RoomPanel from './RoomPanel.jsx';

/** Right sidebar: kingdoms list, ongoing wars, chronicle feed with filters.
 * (Per-kingdom stats live in the floating StatsPanel over the map.) */
export default function Sidebar({ tick, meta, paused, speed, onSpeed, onPause, selected, onSelect, worldId, worldMode, claims, standings, onClaim }) {
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

            {/* kingdoms: collapsed panel with its own scroll (crónica below keeps space) */}
            <div style={sectionTitle}>
                Kingdoms {tick.kingdoms.filter(k => k.sites.length).length}
                <button
                    style={{ ...btn, fontSize: 10, padding: '1px 6px', marginLeft: 6 }}
                    onClick={() => setShowKingdoms(s => !s)}
                >{showKingdoms ? '▾' : '▸'}</button>
            </div>
            {showKingdoms && (
                <div style={{ maxHeight: 260, overflowY: 'auto', marginBottom: 6 }}>
                    {tick.kingdoms.map(k => (
                        <div
                            key={k.id}
                            onClick={() => onSelect(k)}
                            style={{ ...panel, cursor: 'pointer', marginBottom: 6, border: selected?.id === k.id ? '1px solid #2f81f7' : panel.border }}
                        >
                            <div style={kStyle(k)}>
                                {k.at_war ? '⚔ ' : ''}{k.name}
                            </div>
                            <div style={{ fontSize: 11, color: '#8b949e' }}>
                                {k.race} · {k.government}{k.golden_age ? ' ✨' : ''}
                            </div>
                            {k.tech_level !== undefined && (
                                <div style={{ fontSize: 10, color: '#8b949e' }}>
                                    era {k.tech_level} · pop {k.population.toLocaleString()} · {k.sites.length}s
                                </div>
                            )}
                            {k.leader && (
                                <div style={{ fontSize: 11 }}>
                                    {k.leader.title} {k.leader.name} ({k.leader.reign_years}y)
                                    {k.hero && <span style={{ color: '#f0c674' }}> · champion {k.hero.name}</span>}
                                </div>
                            )}
                        </div>
                    ))}
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
            <ChronicleFeed events={tick.events} />

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

/** Chronicle feed with type filters, milestone highlighting, auto-scroll. */
function ChronicleFeed({ events }) {
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
                Chronicle
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
                            fontSize: 11, marginBottom: 3,
                            color: e.type === 'MILESTONE' ? '#f0c674' : undefined,
                            fontWeight: e.type === 'MILESTONE' ? 'bold' : undefined,
                            opacity: 0.92,
                        }}
                    >
                        <span style={{ color: '#58a6ff' }}>[y{Math.floor(e.month / 12)}]</span>{' '}
                        {e.text}
                    </div>
                ))}
            </div>
        </>
    );
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
    width: 340,
    minWidth: 280,
    background: '#0d1117',
    borderLeft: '1px solid #21262d',
    padding: 12,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',  // panels scroll internally; the aside itself never scrolls
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
