import React from 'react';

/** Right sidebar: kingdoms panel, ongoing wars, chronicle feed with filters. */
export default function Sidebar({ tick, meta, paused, speed, onSpeed, onPause, selected, onSelect }) {
    if (!tick) return <aside style={aside}>waiting for simulation…</aside>;
    const kStyle = (k) => ({ color: `rgb(${k.color[0]},${k.color[1]},${k.color[2]})` });
    const govDesc = meta?.governments?.find(g => g.name === selected?.government)?.description;

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

            {/* selected kingdom inspector */}
            {selected && (
                <div style={{ ...panel, border: '1px solid #30363d', marginBottom: 8 }}>
                    <div style={kStyle(selected)}><strong>{selected.name}</strong></div>
                    <div style={{ fontSize: 11, color: '#8b949e' }}>
                        {selected.race} · {selected.government}
                    </div>
                    {govDesc && (
                        <div style={{ fontSize: 11, marginTop: 4, fontStyle: 'italic', color: '#adbbca' }}>
                            “{govDesc}”
                        </div>
                    )}
                    {selected.leader && (
                        <div style={{ fontSize: 11, marginTop: 4 }}>
                            {selected.leader.title} {selected.leader.name} — reign{' '}
                            {selected.leader.reign_years}y ({selected.leader.battles_won}W/
                            {selected.leader.battles_lost}L)
                        </div>
                    )}
                    {selected.hero && (
                        <div style={{ fontSize: 11, color: '#f0c674' }}>
                            champion {selected.hero.name} (x{selected.hero.strength},{' '}
                            {selected.hero.deeds} deeds)
                        </div>
                    )}
                    <div style={{ fontSize: 11, color: '#8b949e' }}>
                        pop {selected.population.toLocaleString()} · {selected.sites.length} sites
                    </div>
                    <button style={{ ...btn, marginTop: 6, fontSize: 11 }} onClick={() => onSelect(null)}>
                        close
                    </button>
                </div>
            )}

            {/* kingdoms */}
            <div style={sectionTitle}>Kingdoms</div>
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
                        {k.race} · {k.government}
                    </div>
                    {k.leader && (
                        <div style={{ fontSize: 11 }}>
                            {k.leader.title} {k.leader.name} ({k.leader.reign_years}y)
                            {k.hero && <span style={{ color: '#f0c674' }}> · champion {k.hero.name}</span>}
                        </div>
                    )}
                    <div style={{ fontSize: 11, color: '#8b949e' }}>
                        pop {k.population.toLocaleString()} · {k.sites.length} sites
                    </div>
                </div>
            ))}

            {/* ongoing wars */}
            {tick.wars_ongoing.length > 0 && (
                <>
                    <div style={sectionTitle}>Ongoing wars</div>
                    {tick.wars_ongoing.map((w, i) => (
                        <div key={i} style={{ fontSize: 11, color: '#f85149', marginBottom: 4 }}>
                            ⚔ {w.name}: {w.a} vs {w.b} ({w.battles} battles, {w.dead} dead)
                        </div>
                    ))}
                </>
            )}

            {/* beasts */}
            {tick.beasts.filter(b => b.alive).length > 0 && (
                <>
                    <div style={sectionTitle}>Beasts</div>
                    {tick.beasts.filter(b => b.alive).map((b, i) => (
                        <div key={i} style={{ fontSize: 11, color: '#ff7800' }}>
                            ▲ {b.name} ({b.x},{b.y})
                        </div>
                    ))}
                </>
            )}

            {/* chronicle with filters */}
            <ChronicleFeed events={tick.events} />
        </aside>
    );
}

/** Chronicle feed with type filters and milestone highlighting. */
function ChronicleFeed({ events }) {
    const [filter, setFilter] = React.useState('ALL');
    const FILTERS = {
        ALL: () => true,
        MILESTONE: e => e.type === 'MILESTONE',
        WAR: e => ['WAR_DECLARED', 'BATTLE', 'WAR_ENDED', 'SITE_CAPTURED', 'ALLIANCE_JOINED_WAR'].includes(e.type),
        LEADER: e => ['LEADER_CROWNED', 'LEADER_DIED', 'GOVERNMENT_CHANGED', 'HERO_EMERGED', 'HERO_FELL'].includes(e.type),
        BEAST: e => ['BEAST_SIGHTED', 'BEAST_SLAIN', 'BEAST_RAMPAGE'].includes(e.type),
    };
    const shown = events.filter(FILTERS[filter]);
    return (
        <>
            <div style={sectionTitle}>Chronicle</div>
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
                         f === 'LEADER' ? '♛' : '▲'}
                    </button>
                ))}
            </div>
            <div style={{ overflowY: 'auto', flex: 1 }}>
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

const aside = {
    width: 340,
    minWidth: 280,
    background: '#0d1117',
    borderLeft: '1px solid #21262d',
    padding: 12,
    display: 'flex',
    flexDirection: 'column',
    overflowY: 'auto',
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
