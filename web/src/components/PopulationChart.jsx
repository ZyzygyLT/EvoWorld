import React from 'react';

/** Shared inline SVG population chart — used by StatsPanel + Sidebar + friends. */
export default React.memo(function PopulationChart({ values, color, width = 240, height = 40, title }) {
    const pts = React.useMemo(() => {
        if (!values?.length) return '';
        const lo = Math.min(...values);
        const hi = Math.max(...values);
        const rng = Math.max(1, hi - lo);
        return values.map((v, i) => {
            const x = (i / Math.max(1, values.length - 1)) * width;
            const y = height - 3 - ((v - lo) / rng) * (height - 6);
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
    }, [values, width, height]);

    if (!values?.length) return null;
    const lo = Math.min(...values);
    const hi = Math.max(...values);

    return (
        <div style={{ marginTop: 6 }}>
            {title && <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 2 }}>{title}</div>}
            {!title && (
                <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 2 }}>
                    population — {values.length}y · min {lo.toLocaleString()} · max {hi.toLocaleString()}
                </div>
            )}
            <svg width={width} height={height} style={{ background: '#0a0e14', borderRadius: 4, display: 'block' }}>
                <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
            </svg>
            {title && (
                <div style={{ fontSize: 10, color: '#8b949e' }}>
                    min {lo.toLocaleString()} · max {hi.toLocaleString()}
                </div>
            )}
        </div>
    );
});
