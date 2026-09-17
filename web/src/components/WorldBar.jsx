import React from 'react';
import { t } from '../i18n.js';

/**
 * Fixed bottom strip with live world statistics + population sparkline.
 * Always visible regardless of zoom (it lives outside the canvas).
 */
function WorldBarInner({ tick, lang = 'es', historyRef }) {
    if (!tick) return null;
    const alive = (tick.kingdoms ?? []).filter(k => (k.sites?.length ?? 0) > 0);
    const total = alive.reduce((a, k) => a + (k.population || 0), 0);
    const wars = (tick.wars_ongoing ?? []).length;
    const eras = alive.map(k => k.tech_level ?? 0);
    const avgEra = eras.length ? (eras.reduce((a, b) => a + b, 0) / eras.length) : 0;
    const biggest = alive.reduce(
        (best, k) => (!best || (k.population || 0) > best.population ? k : best), null);
    const beasts = (tick.beasts ?? []).filter(b => b.alive).length;
    const hist = historyRef?.current ?? [];

    return (
        <div style={bar}>
            <span><strong>{lang === 'es' ? 'Año' : 'Year'} {Math.floor(tick.month / 12)}</strong></span>
            <span>👥 {Math.round(total).toLocaleString()}</span>
            <span>🏰 {alive.length}</span>
            <span style={{ color: wars ? '#f85149' : undefined }}>⚔ {wars}</span>
            <span>📜 {avgEra.toFixed(1)}</span>
            {biggest && <span>👑 {biggest.name}</span>}
            <span>▲ {beasts}</span>
            {hist.length > 1 && <WorldSpark values={hist.map(h => h.pop)} />}
        </div>
    );
}

function WorldSpark({ values }) {
    const W = 160, H = 28;
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const rng = Math.max(1, hi - lo);
    const pts = values.map((v, i) => {
        const x = (i / Math.max(1, values.length - 1)) * W;
        const y = H - 2 - ((v - lo) / rng) * (H - 4);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    return (
        <svg width={W} height={H} style={{ background: '#0a0e14', borderRadius: 3, flexShrink: 0 }}>
            <polyline points={pts} fill="none" stroke="#58a6ff" strokeWidth="1.4" />
        </svg>
    );
}

export default React.memo(WorldBarInner);

const bar = {
    display: 'flex',
    alignItems: 'center',
    gap: 16,
    padding: '6px 14px',
    background: '#161b22',
    borderTop: '1px solid #21262d',
    fontSize: 13,
    color: '#c9d1d9',
    flexShrink: 0,
    minHeight: 32,
};
