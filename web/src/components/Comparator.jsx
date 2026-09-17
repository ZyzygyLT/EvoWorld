import React from 'react';

const METRICS = {
    population: { key: 'population_history', label: 'Población' },
    military: { key: 'military_power', label: 'Militar' },
};

export default function Comparator({ tick }) {
    const [metric, setMetric] = React.useState('population');
    if (!tick?.kingdoms?.length) return null;

    // For population we have history arrays; for military we fall back to current value
    const isHistory = metric === 'population';
    const kingdoms = tick.kingdoms.filter(k => {
        if (isHistory) return (k.population_history?.length ?? 0) > 1;
        return k.military_power != null;
    });
    if (!kingdoms.length) return null;

    const W = 340, H = 100;
    let allVals, maxLen, getVals;
    if (isHistory) {
        allVals = kingdoms.flatMap(k => k.population_history);
        maxLen = Math.max(...kingdoms.map(k => k.population_history.length));
        getVals = k => k.population_history;
    } else {
        // single-point metric: show bar-like comparison
        allVals = kingdoms.map(k => k.military_power ?? 0);
        maxLen = kingdoms.length;
        getVals = null;
    }
    const lo = Math.min(...allVals);
    const hi = Math.max(...allVals);
    const rng = Math.max(1, hi - lo);

    return (
        <div style={{ marginTop: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 1 }}>Comparador</span>
                {Object.entries(METRICS).map(([k, v]) => (
                    <button key={k} onClick={() => setMetric(k)}
                        style={{
                            background: metric === k ? '#2f81f7' : '#21262d',
                            border: '1px solid #30363d', color: '#c9d1d9',
                            borderRadius: 4, padding: '1px 6px', cursor: 'pointer', fontSize: 10,
                        }}>{v.label}</button>
                ))}
            </div>
            {isHistory ? (
                <svg width={W} height={H} style={{ background: '#0a0e14', borderRadius: 4 }}>
                    {kingdoms.map(k => {
                        const vals = getVals(k);
                        const pts = vals.map((v,i) => {
                            const x = (i / Math.max(1, maxLen-1)) * W;
                            const y = H - 4 - ((v - lo)/rng)*(H-8);
                            return `${x.toFixed(1)},${y.toFixed(1)}`;
                        }).join(' ');
                        return <polyline key={k.id} points={pts} fill="none" stroke={`rgb(${k.color[0]},${k.color[1]},${k.color[2]})`} strokeWidth="1.2" opacity="0.85" />;
                    })}
                </svg>
            ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                    {[...kingdoms].sort((a,b) => (b.military_power??0)-(a.military_power??0)).map(k => {
                        const v = k.military_power ?? 0;
                        const pct = ((v - lo) / rng) * 100;
                        return (
                            <div key={k.id} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
                                <span style={{ color: `rgb(${k.color[0]},${k.color[1]},${k.color[2]})`, minWidth: 80, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{k.name}</span>
                                <div style={{ flex: 1, height: 8, background: '#161b22', borderRadius: 3, overflow: 'hidden' }}>
                                    <div style={{ width: `${Math.max(4, pct)}%`, height: '100%', background: `rgb(${k.color[0]},${k.color[1]},${k.color[2]})` }} />
                                </div>
                                <span style={{ color: '#8b949e', minWidth: 36, textAlign: 'right' }}>{Math.round(v).toLocaleString()}</span>
                            </div>
                        );
                    })}
                </div>
            )}
            {isHistory && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 4 }}>
                    {kingdoms.map(k => (
                        <span key={k.id} style={{ fontSize: 10, color: `rgb(${k.color[0]},${k.color[1]},${k.color[2]})` }}>● {k.name}</span>
                    ))}
                </div>
            )}
        </div>
    );
}
