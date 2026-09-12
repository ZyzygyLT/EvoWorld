import React from 'react';

const ERAS = ['Stone Age', 'Bronze Age', 'Iron Age', 'Classical', 'Medieval'];
const REL_COLORS = {
    ALLIANCE: '#3fb950',
    PEACE: '#8b949e',
    TENSION: '#d29922',
    WAR: '#f85149',
};

/**
 * Floating always-visible stats panel for the selected kingdom.
 * Data comes from the live `selected_stats` WS payload (rich engine stats).
 */
export default function StatsPanel({ stats, govDesc, onClose, claimable, claimedBy, onClaim }) {
    if (!stats) return null;
    const kcolor = `rgb(${stats.color[0]},${stats.color[1]},${stats.color[2]})`;

    return (
        <div style={panel}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ ...dot, background: kcolor }} />
                <strong style={{ color: kcolor }}>{stats.name}</strong>
                {stats.golden_age && <span title="Golden Age">✨</span>}
                {claimedBy && <span title={`claimed by ${claimedBy}`} style={{ fontSize: 13 }}>👑</span>}
                <span style={{ flex: 1 }} />
                <button style={closeBtn} onClick={onClose}>✕</button>
            </div>
            {claimable && (
                <button
                    style={{ ...claimBtn }}
                    onClick={() => onClaim && onClaim(stats.id)}
                >⚽ claim this realm for the league</button>
            )}
            {claimedBy && !claimable && (
                <div style={{ fontSize: 10, color: '#f0c674' }}>claimed by {claimedBy}</div>
            )}
            <div style={sub}>{stats.race} · {stats.government}{stats.civilized ? ' (civilized)' : ''}</div>
            {govDesc && <div style={desc}>“{govDesc}”</div>}

            <div style={row}>
                <Stat label="Population" value={stats.population.toLocaleString()} />
                <Stat label="Peak" value={stats.peak_population.toLocaleString()} />
                <Stat label="Sites" value={stats.sites} />
                <Stat label="Territory" value={`${stats.influence_tiles} tiles`} />
            </div>
            <div style={row}>
                <Stat label="Era" value={stats.era} />
                <Stat label="Era progress" value={`${Math.round(stats.era_progress * 100)}%`} />
                <Stat label="Military power" value={stats.military_power.toLocaleString()} />
                <Stat label="Militarization" value={`${stats.militarization_pct}%`} />
            </div>
            <div style={row}>
                <Stat label="Wars W/L" value={`${stats.wars_won}/${stats.wars_lost}`} highlight={stats.ongoing_wars > 0} />
                <Stat label="Ongoing wars" value={stats.ongoing_wars} />
                {stats.leader && (
                    <Stat
                        label={`${stats.leader.title}`}
                        value={`${stats.leader.name} · ${stats.leader.reign_years}y (${stats.leader.battles_won}W)`}
                    />
                )}
                {stats.hero && (
                    <Stat label="Champion" value={`${stats.hero.name} ×${stats.hero.strength}`} />
                )}
            </div>

            {/* era progress bar */}
            <div style={{ marginTop: 6 }}>
                <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 2 }}>
                    knowledge: {stats.era}
                </div>
                <div style={bar}>
                    <div style={{ ...barFill, width: `${stats.era_progress * 100}%`, background: '#d2a8ff' }} />
                </div>
            </div>

            {/* population chart */}
            {stats.population_history?.length > 1 && (
                <PopulationChart values={stats.population_history} color={kcolor} />
            )}

            {/* relations */}
            {stats.relations?.length > 0 && (
                <div style={{ marginTop: 8 }}>
                    <div style={{ fontSize: 10, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 1 }}>
                        Diplomacy
                    </div>
                    {stats.relations.map(r => (
                        <div key={r.name} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}>
                            <span style={{ ...dot, background: `rgb(${r.color[0]},${r.color[1]},${r.color[2]})` }} />
                            <span>{r.name}</span>
                            <span style={{ color: REL_COLORS[r.relation] || '#8b949e' }}>{r.relation}</span>
                        </div>
                    ))}
                </div>
            )}

            {/* recent events of this kingdom */}
            {stats.recent_events?.length > 0 && (
                <div style={{ marginTop: 8, maxHeight: 120, overflowY: 'auto' }}>
                    <div style={{ fontSize: 10, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 1 }}>
                        Recent history
                    </div>
                    {stats.recent_events.slice().reverse().map((ev, i) => (
                        <div key={i} style={{ fontSize: 10, opacity: 0.9 }}>
                            <span style={{ color: '#58a6ff' }}>[y{Math.floor(ev.month / 12)}]</span> {ev.text}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function Stat({ label, value, highlight }) {
    return (
        <div style={{ flex: 1, minWidth: 80 }}>
            <div style={{ fontSize: 9, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 0.5 }}>{label}</div>
            <div style={{ fontSize: 12, color: highlight ? '#f85149' : '#e6edf3' }}>{value}</div>
        </div>
    );
}

function PopulationChart({ values, color }) {
    const W = 240, H = 40;
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const rng = Math.max(1, hi - lo);
    const pts = values.map((v, i) => {
        const x = (i / Math.max(1, values.length - 1)) * W;
        const y = H - 3 - ((v - lo) / rng) * (H - 6);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    return (
        <div style={{ marginTop: 8 }}>
            <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 2 }}>
                population — {values.length}y · min {lo.toLocaleString()} · max {hi.toLocaleString()}
            </div>
            <svg width={W} height={H} style={{ background: '#0a0e14', borderRadius: 4, display: 'block' }}>
                <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
            </svg>
        </div>
    );
}

const panel = {
    position: 'absolute',
    top: 40,
    left: 12,
    zIndex: 10,
    width: 280,
    background: 'rgba(13,17,23,0.93)',
    border: '1px solid #30363d',
    borderRadius: 8,
    padding: 10,
    pointerEvents: 'auto',
};

const sub = { fontSize: 11, color: '#8b949e', marginBottom: 4 };
const desc = { fontSize: 10, fontStyle: 'italic', color: '#adbbca', marginBottom: 6 };
const row = { display: 'flex', gap: 8, marginTop: 4, flexWrap: 'wrap' };

const dot = {
    display: 'inline-block',
    width: 8,
    height: 8,
    borderRadius: '50%',
    flexShrink: 0,
};

const bar = {
    height: 5,
    background: '#161b22',
    borderRadius: 3,
    overflow: 'hidden',
};

const barFill = { height: '100%', borderRadius: 3, transition: 'width 0.3s' };

const closeBtn = {
    background: 'none',
    border: 'none',
    color: '#8b949e',
    cursor: 'pointer',
    fontSize: 14,
    padding: '0 4px',
};

const claimBtn = {
    background: '#1f6feb',
    border: '1px solid #388bfd',
    color: '#fff',
    borderRadius: 4,
    padding: '3px 8px',
    cursor: 'pointer',
    fontSize: 11,
    margin: '4px 0',
    width: '100%',
};
