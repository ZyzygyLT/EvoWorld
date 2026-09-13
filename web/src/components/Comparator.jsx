import React from 'react';

export default function Comparator({ tick }) {
    if (!tick?.kingdoms?.length) return null;
    const kingdoms = tick.kingdoms.filter(k => (k.population_history?.length ?? 0) > 1);
    if (!kingdoms.length) return null;
    const W = 340, H = 100;
    const allVals = kingdoms.flatMap(k => k.population_history);
    const lo = Math.min(...allVals);
    const hi = Math.max(...allVals);
    const rng = Math.max(1, hi - lo);
    const maxLen = Math.max(...kingdoms.map(k => k.population_history.length));
    return (
        <div style={{ marginTop: 8 }}>
            <div style={{ fontSize: 10, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 4 }}>Comparador — población</div>
            <svg width={W} height={H} style={{ background: '#0a0e14', borderRadius: 4 }}>
                {kingdoms.map(k => {
                    const pts = k.population_history.map((v,i) => {
                        const x = (i / Math.max(1, maxLen-1)) * W;
                        const y = H - 4 - ((v - lo)/rng)*(H-8);
                        return `${x.toFixed(1)},${y.toFixed(1)}`;
                    }).join(' ');
                    return <polyline key={k.id} points={pts} fill="none" stroke={`rgb(${k.color[0]},${k.color[1]},${k.color[2]})`} strokeWidth="1.2" opacity="0.85" />;
                })}
            </svg>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 4 }}>
                {kingdoms.map(k => (
                    <span key={k.id} style={{ fontSize: 10, color: `rgb(${k.color[0]},${k.color[1]},${k.color[2]})` }}>● {k.name}</span>
                ))}
            </div>
        </div>
    );
}
