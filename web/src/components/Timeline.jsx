import React from 'react';

export default function Timeline({ history, currentMonth, scrubIndex, onScrub }) {
    if (!history.length) return null;
    const maxIdx = history.length - 1;
    const isLive = scrubIndex === null;
    const idx = isLive ? maxIdx : scrubIndex;
    const tick = history[idx];
    return (
        <div style={wrap}>
            <input type="range" min={0} max={maxIdx} value={idx}
                onChange={e => onScrub(parseInt(e.target.value,10))}
                style={{ flex: 1 }} />
            <span style={{ fontSize: 11, color: '#8b949e', minWidth: 80, textAlign: 'right' }}>
                {tick ? `Año ${Math.floor(tick.month/12)}` : ''} {isLive ? '• LIVE' : '◀ REPLAY'}
            </span>
            <button onClick={() => onScrub(null)} style={btn} disabled={isLive}>Live</button>
            <div style={{ display: 'flex', gap: 2, marginLeft: 8 }}>
                {history.filter((_,i)=>i%10===0).slice(-12).map((h,i)=> (
                    <div key={i} title={`Año ${Math.floor(h.month/12)}`}
                        style={{ width: 12, height: 18, background: `hsl(${120 + h.kingdoms.length*15},60%,40%)`, borderRadius: 2, opacity: 0.8 }} />
                ))}
            </div>
        </div>
    );
}
const wrap = { display: 'flex', alignItems: 'center', gap: 8, padding: '4px 12px', background: '#0d1117', borderTop: '1px solid #21262d' };
const btn = { background: '#21262d', border: '1px solid #30363d', color: '#c9d1d9', borderRadius: 4, padding: '2px 8px', cursor: 'pointer', fontSize: 11 };
