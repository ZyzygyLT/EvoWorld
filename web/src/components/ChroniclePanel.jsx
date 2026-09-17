import React from 'react';
import { renderEvent, t } from '../i18n.js';

/**
 * Chronicle strip below the map: filterable event feed with auto-scroll.
 * Uses tick.kingdoms to color kingdom names.
 */
export default function ChroniclePanel({ events, lang = 'es', tick }) {
    const [filter, setFilter] = React.useState('ALL');
    const boxRef = React.useRef(null);
    const [autoScroll, setAutoScroll] = React.useState(true);

    const FILTERS = {
        ALL: () => true,
        WAR: e => ['WAR_DECLARED', 'BATTLE', 'WAR_ENDED', 'SITE_CAPTURED', 'HOLY_WAR', 'ALLIANCE_FORMED', 'ALLIANCE_JOINED_WAR'].includes(e.type),
        LEADER: e => ['LEADER_CROWNED', 'LEADER_DIED', 'GOVERNMENT_CHANGED', 'HERO_EMERGED', 'HERO_FELL'].includes(e.type),
        BEAST: e => ['BEAST_SIGHTED', 'BEAST_SLAIN', 'BEAST_RAMPAGE'].includes(e.type),
        PROGRESS: e => ['TECH_ERA', 'TECH_DISCOVERY', 'GOLDEN_AGE'].includes(e.type),
        COLONY: e => ['COLONY_FOUNDED', 'KINGDOM_FOUNDED', 'SITE_FOUNDED', 'INDEPENDENCE', 'REBELLION'].includes(e.type),
        DISASTER: e => ['DROUGHT', 'FLOOD', 'FAMINE', 'EARTHQUAKE', 'PLAGUE'].includes(e.type),
    };

    const list = events ?? [];
    const shown = list.filter(FILTERS[filter] ?? FILTERS.ALL);

    React.useEffect(() => {
        if (autoScroll && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }, [events, autoScroll, filter]);

    const onScroll = () => {
        const el = boxRef.current;
        if (!el) return;
        setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
    };

    // counts per filter for badges
    const counts = {};
    for (const [k, fn] of Object.entries(FILTERS)) counts[k] = list.filter(fn).length;

    return (
        <div style={wrap}>
            <div style={bar}>
                <span style={{ fontSize: 11, color: '#58a6ff', textTransform: 'uppercase', letterSpacing: 1 }}>
                    {t(lang, 'chronicle')}
                </span>
                <span style={{ flex: 1 }} />
                {Object.keys(FILTERS).map(f => (
                    <button
                        key={f}
                        onClick={() => setFilter(f)}
                        title={`${f} (${counts[f]})`}
                        style={{
                            ...fBtn,
                            background: filter === f ? '#2f81f7' : '#21262d',
                            color: filter === f ? '#fff' : '#8b949e',
                        }}
                    >
                        {label(f)} <span style={{ opacity: 0.7, fontSize: 9 }}>{counts[f] || ''}</span>
                    </button>
                ))}
                <span style={{ fontSize: 9, color: autoScroll ? '#58a6ff' : '#6e7681', marginLeft: 6 }}>
                    {autoScroll ? 'auto ▼' : '▲'}
                </span>
            </div>
            <div ref={boxRef} onScroll={onScroll} style={feed}>
                {shown.length === 0 && (
                    <div style={{ fontSize: 11, color: '#6e7681', fontStyle: 'italic', padding: 4 }}>
                        {filter === 'ALL'
                            ? (lang === 'es' ? 'Aún no hay eventos…' : 'No events yet…')
                            : (lang === 'es' ? 'Nada en este filtro.' : 'Nothing in this filter.')}
                    </div>
                )}
                {shown.slice().reverse().map((e, _idx) => (
                    <div
                        key={e.id ?? `${_idx}-${e.month}-${e.type}`}
                        style={{
                            fontSize: 12, marginBottom: 3,
                            color: e.type === 'MILESTONE' ? '#f0c674' : '#c9d1d9',
                            fontWeight: e.type === 'MILESTONE' ? 600 : undefined,
                        }}
                    >
                        <span style={{ color: '#58a6ff' }}>[y{Math.floor(e.month / 12)}]</span>{' '}
                        {coloredParts(renderEvent(lang, e), tick)}
                    </div>
                ))}
            </div>
        </div>
    );
}

function label(f) {
    return f === 'ALL' ? 'all'
        : f === 'WAR' ? '⚔'
        : f === 'LEADER' ? '♛'
        : f === 'BEAST' ? '▲'
        : f === 'PROGRESS' ? '⬢'
        : f === 'COLONY' ? '⛵'
        : f === 'DISASTER' ? '☠'
        : f;
}

function coloredParts(text, tick) {
    if (!tick?.kingdoms || typeof text !== 'string') return text;
    const names = [...tick.kingdoms].sort((a, b) => b.name.length - a.name.length);
    let parts = [text];
    for (const k of names) {
        const color = `rgb(${k.color[0]},${k.color[1]},${k.color[2]})`;
        const next = [];
        for (const p of parts) {
            if (typeof p !== 'string') { next.push(p); continue; }
            const idx = p.indexOf(k.name);
            if (idx === -1) next.push(p);
            else {
                if (idx > 0) next.push(p.slice(0, idx));
                next.push(<span key={k.id + idx} style={{ color, fontWeight: 600 }}>{k.name}</span>);
                if (idx + k.name.length < p.length) next.push(p.slice(idx + k.name.length));
            }
        }
        parts = next;
    }
    return parts;
}

const wrap = {
    background: '#0d1117',
    borderTop: '1px solid #21262d',
    display: 'flex',
    flexDirection: 'column',
    height: 148,
    flexShrink: 0,
};

const bar = {
    display: 'flex',
    alignItems: 'center',
    gap: 3,
    padding: '4px 8px',
    borderBottom: '1px solid #21262d',
    flexWrap: 'wrap',
};

const fBtn = {
    border: '1px solid #30363d',
    borderRadius: 4,
    padding: '1px 6px',
    cursor: 'pointer',
    fontSize: 10,
};

const feed = {
    overflowY: 'auto',
    padding: '6px 10px',
    flex: 1,
    minHeight: 0,
};
