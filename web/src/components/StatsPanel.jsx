import React from 'react';
import { vocab, techName, TECH_BRANCH, BRANCH_COLORS } from '../i18n.js';
import PopulationChart from './PopulationChart.jsx';

const REL_COLORS = { ALLIANCE: '#3fb950', PEACE: '#8b949e', TENSION: '#d29922', WAR: '#f85149' };

const TABS = [
    { id: 'overview', label: 'Info' },
    { id: 'sites', label: 'Sites' },
    { id: 'culture', label: 'Cultura' },
    { id: 'diplo', label: 'Diplo' },
    { id: 'history', label: 'Log' },
];

/**
 * Floating tabbed stats panel for the selected kingdom.
 * Only the active tab is rendered, maxHeight prevents overflow.
 */
function StatsPanelInner({ stats, govDesc, onClose, claimable, claimedBy, onClaim, lang = 'es' }) {
    const [tab, setTab] = React.useState('overview');
    if (!stats) return null;
    const kcolor = `rgb(${stats.color[0]},${stats.color[1]},${stats.color[2]})`;

    return (
        <div style={panel}>
            {/* header — always visible */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ ...dot, background: kcolor }} />
                <strong style={{ color: kcolor }}>{stats.name}</strong>
                {stats.golden_age && <span title="Golden Age">✨</span>}
                {stats.drought && <span title="Drought — growth suppressed">🏜️</span>}
                {claimedBy && <span title={`claimed by ${claimedBy}`} style={{ fontSize: 13 }}>👑</span>}
                <span style={{ flex: 1 }} />
                <button style={closeBtn} onClick={onClose}>✕</button>
            </div>
            {claimable && <button style={claimBtn} onClick={() => onClaim && onClaim(stats.id)}>⚽ claim this realm for the league</button>}
            {claimedBy && !claimable && <div style={{ fontSize: 10, color: '#f0c674' }}>claimed by {claimedBy}</div>}
            <div style={sub}>{vocab(lang, stats.race)} · {vocab(lang, stats.government)}{stats.civilized ? ' (civilized)' : ''}</div>
            {govDesc && <div style={desc}>"{govDesc}"</div>}

            {/* happiness */}
            {stats.happiness !== undefined && (
                <div style={{ marginBottom: 6 }}>
                    <div style={{ fontSize: 9, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 0.5 }}>
                        felicidad — {Math.round(stats.happiness)}%
                    </div>
                    <div style={bar}><div style={{ ...barFill, width: `${Math.max(0, Math.min(100, stats.happiness))}%`, background: stats.happiness >= 65 ? '#3fb950' : stats.happiness >= 35 ? '#d29922' : '#f85149' }} /></div>
                </div>
            )}
            {/* era */}
            <div style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 2 }}>conocimiento: {stats.era}</div>
                <div style={bar}><div style={{ ...barFill, width: `${stats.era_progress * 100}%`, background: '#d2a8ff' }} /></div>
            </div>

            {/* tab bar */}
            <div style={{ display: 'flex', gap: 3, marginBottom: 8, borderBottom: '1px solid #21262d', paddingBottom: 6 }}>
                {TABS.map(t => (
                    <button key={t.id} onClick={() => setTab(t.id)}
                        style={{ ...tabBtn, background: tab === t.id ? '#2f81f7' : '#21262d', color: tab === t.id ? '#fff' : '#8b949e' }}>{t.label}</button>
                ))}
            </div>

            {/* tab content — scrollable, only active tab rendered */}
            <div style={tabContent}>
                {tab === 'overview' && (
                    <>
                        <div style={row}><Stat label="Population" value={stats.population.toLocaleString()} /><Stat label="Peak" value={stats.peak_population.toLocaleString()} /><Stat label="Sites" value={stats.sites} /><Stat label="Territory" value={`${stats.influence_tiles} tiles`} /></div>
                        <div style={row}><Stat label="Era" value={stats.era} /><Stat label="Era progress" value={`${Math.round(stats.era_progress * 100)}%`} /><Stat label="Army" value={(stats.army_size ?? stats.military_power ?? 0).toLocaleString()} /><Stat label="Power" value={(stats.military_power ?? 0).toLocaleString()} /></div>
                        <div style={row}><Stat label="Militarization" value={`${stats.militarization_pct}%`} />{stats.faith && <Stat label="Faith" value={vocab(lang, stats.faith)} />}{stats.temples && <Stat label="Temples" value={stats.temples.length} />}</div>
                        <div style={row}><Stat label="Wars W/L" value={`${stats.wars_won}/${stats.wars_lost}`} highlight={stats.ongoing_wars > 0} /><Stat label="Ongoing" value={stats.ongoing_wars} />{stats.leader && <Stat label={stats.leader.title} value={`${stats.leader.name} · ${stats.leader.reign_years}y (${stats.leader.battles_won}W)`} />}{stats.hero && <Stat label="Champion" value={`${stats.hero.name} ×${stats.hero.strength}`} />}</div>
                        {stats.population_history?.length > 1 && <PopulationChart values={stats.population_history} color={kcolor} />}
                        {stats.trade_routes?.length > 0 && (
                            <div style={{ marginTop: 8 }}>
                                <div style={sectionLabel}>Trade routes</div>
                                {stats.trade_routes.map(r => (
                                    <div key={r.partner_id} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 4, marginTop: 2 }}>
                                        <span style={{ ...dot, background: `rgb(${r.color[0]},${r.color[1]},${r.color[2]})` }} /><span>↔ {r.partner}</span><span style={{ color: '#8b949e', fontSize: 10 }}>{Math.floor(r.months / 12)}y</span>
                                    </div>
                                ))}
                            </div>
                        )}
                        {stats.techs?.length > 0 && (
                            <div style={{ marginTop: 8 }}>
                                <div style={sectionLabel}>Tech ({stats.techs.length})</div>
                                {['warfare', 'craft', 'trade', 'lore'].map(br => {
                                    const nodes = stats.techs.filter(tt => TECH_BRANCH[tt] === br);
                                    if (!nodes.length) return null;
                                    return <div key={br} style={{ fontSize: 10, marginTop: 2 }}><span style={{ color: BRANCH_COLORS[br] }}>● </span>{nodes.map(tt => techName(lang, tt)).join(', ')}</div>;
                                })}
                            </div>
                        )}
                    </>
                )}
                {tab === 'sites' && (
                    stats.sites_list?.length > 0 ? (
                        <div>
                            <div style={sectionLabel}>Settlements ({stats.sites_list.length})</div>
                            {stats.sites_list.map((s, i) => {
                                const hasTemple = (stats.temples || []).includes(s.name);
                                return (
                                    <div key={i} style={{ fontSize: 10, display: 'flex', gap: 6, padding: '2px 0', borderBottom: '1px solid #21262d' }}>
                                        <span style={{ color: s.is_capital ? '#f0c674' : '#c9d1d9', flex: 1 }}>{s.is_capital ? '👑 ' : ''}{s.name}{hasTemple ? ' ⛪' : ''}</span>
                                        <span style={{ color: '#8b949e' }}>{s.category}</span>
                                        <span style={{ color: '#e6edf3', minWidth: 30, textAlign: 'right' }}>{Math.round(s.population)}</span>
                                        <span style={{ color: '#6e7681', fontSize: 9 }}>({s.x},{s.y})</span>
                                    </div>
                                );
                            })}
                        </div>
                    ) : <div style={{ fontSize: 11, color: '#6e7681' }}>No settlements.</div>
                )}
                {tab === 'culture' && (
                    stats.culture && Object.keys(stats.culture).length > 0 ? (
                        <div>
                            <div style={sectionLabel}>Culture</div>
                            <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                                {Object.entries(stats.culture).map(([axis, v]) => (
                                    <div key={axis} style={{ flex: 1, textAlign: 'center' }}>
                                        <div style={{ fontSize: 9, color: '#8b949e', textTransform: 'capitalize' }}>{axis}</div>
                                        <div style={cultureBarWrap}><div style={{ ...cultureBar, width: `${Math.round(v * 100)}%`, background: cultureColor(axis) }} /></div>
                                        <div style={{ fontSize: 10, color: '#e6edf3' }}>{Math.round(v * 100)}%</div>
                                    </div>
                                ))}
                            </div>
                            {stats.techs?.length > 0 && (
                                <div style={{ marginTop: 10 }}>
                                    <div style={sectionLabel}>Tech ({stats.techs.length})</div>
                                    {['warfare', 'craft', 'trade', 'lore'].map(br => {
                                        const nodes = stats.techs.filter(tt => TECH_BRANCH[tt] === br);
                                        if (!nodes.length) return null;
                                        return <div key={br} style={{ fontSize: 10, marginTop: 2 }}><span style={{ color: BRANCH_COLORS[br] }}>● </span>{nodes.map(tt => techName(lang, tt)).join(', ')}</div>;
                                    })}
                                </div>
                            )}
                        </div>
                    ) : <div style={{ fontSize: 11, color: '#6e7681' }}>No culture data.</div>
                )}
                {tab === 'diplo' && (
                    stats.relations?.length > 0 ? (
                        <div>
                            <div style={sectionLabel}>Diplomacy</div>
                            {stats.relations.map(r => (
                                <div key={r.name} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 4, padding: '2px 0' }}>
                                    <span style={{ ...dot, background: `rgb(${r.color[0]},${r.color[1]},${r.color[2]})` }} /><span>{r.name}</span><span style={{ color: REL_COLORS[r.relation] || '#8b949e' }}>{r.relation}</span>
                                </div>
                            ))}
                            {stats.trade_routes?.length > 0 && (
                                <div style={{ marginTop: 8 }}>
                                    <div style={sectionLabel}>Trade routes</div>
                                    {stats.trade_routes.map(r => (
                                        <div key={r.partner_id} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 4, marginTop: 2 }}>
                                            <span style={{ ...dot, background: `rgb(${r.color[0]},${r.color[1]},${r.color[2]})` }} /><span>↔ {r.partner}</span><span style={{ color: '#8b949e', fontSize: 10 }}>{Math.floor(r.months / 12)}y</span>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    ) : <div style={{ fontSize: 11, color: '#6e7681' }}>No relations.</div>
                )}
                {tab === 'history' && (
                    stats.recent_events?.length > 0 ? (
                        <div>
                            <div style={sectionLabel}>Recent history</div>
                            {stats.recent_events.slice().reverse().map((ev, _idx) => (
                                <div key={ev.id ?? `${_idx}-${ev.month}-${ev.type}`} style={{ fontSize: 10, opacity: 0.9, padding: '2px 0', borderBottom: '1px solid #21262d' }}>
                                    <span style={{ color: '#58a6ff' }}>[y{Math.floor(ev.month / 12)}]</span> {ev.text}
                                </div>
                            ))}
                        </div>
                    ) : <div style={{ fontSize: 11, color: '#6e7681' }}>No recent events.</div>
                )}
            </div>
        </div>
    );
}

function Stat({ label, value, highlight }) {
    return <div style={{ flex: 1, minWidth: 70 }}><div style={{ fontSize: 9, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 0.5 }}>{label}</div><div style={{ fontSize: 11, color: highlight ? '#f85149' : '#e6edf3' }}>{value}</div></div>;
}
function cultureColor(axis) { const m = { martial: '#f85149', mercantile: '#d29922', pious: '#a371f7', curious: '#58a6ff' }; return m[axis] || '#8b949e'; }

export default React.memo(StatsPanelInner);

const panel = { position: 'absolute', top: 40, left: 12, right: 12, zIndex: 10, maxWidth: 340, maxHeight: 'calc(100% - 56px)', display: 'flex', flexDirection: 'column', background: 'rgba(13,17,23,0.97)', border: '1px solid #30363d', borderRadius: 8, padding: 10, pointerEvents: 'auto', overflow: 'hidden', boxSizing: 'border-box' };
const tabContent = { overflowY: 'auto', flex: 1, minHeight: 0, paddingRight: 2 };
const tabBtn = { border: '1px solid #30363d', borderRadius: 4, padding: '2px 6px', cursor: 'pointer', fontSize: 10, flex: 1 };
const sectionLabel = { fontSize: 10, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 4 };
const sub = { fontSize: 11, color: '#8b949e', marginBottom: 4 };
const desc = { fontSize: 10, fontStyle: 'italic', color: '#adbbca', marginBottom: 6 };
const row = { display: 'flex', gap: 6, marginTop: 4, flexWrap: 'wrap' };
const dot = { display: 'inline-block', width: 8, height: 8, borderRadius: '50%', flexShrink: 0 };
const bar = { height: 5, background: '#161b22', borderRadius: 3, overflow: 'hidden' };
const barFill = { height: '100%', borderRadius: 3, transition: 'width 0.3s' };
const cultureBarWrap = { height: 4, background: '#161b22', borderRadius: 2, overflow: 'hidden', marginTop: 2 };
const cultureBar = { height: '100%', borderRadius: 2, transition: 'width 0.3s' };
const closeBtn = { background: 'none', border: 'none', color: '#8b949e', cursor: 'pointer', fontSize: 14, padding: '0 4px' };
const claimBtn = { background: '#1f6feb', border: '1px solid #388bfd', color: '#fff', borderRadius: 4, padding: '3px 8px', cursor: 'pointer', fontSize: 11, margin: '4px 0', width: '100%' };
