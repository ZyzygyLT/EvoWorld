import React, { useEffect, useRef, useState } from 'react';
import WorldMap from './components/WorldMap.jsx';
import Sidebar from './components/Sidebar.jsx';

/**
 * worldsim web client.
 * Flow: create a world (POST /api/worlds) -> connect to the live WS stream.
 * New worlds start PAUSED at x1 — look at the map first, then press run.
 */
export default function App() {
    const [worlds, setWorlds] = useState([]);
    const [state, setState] = useState(null);   // full snapshot
    const [tick, setTick] = useState(null);      // live WS payload
    const [meta, setMeta] = useState(null);      // /api/meta (govs, races)
    const [paused, setPaused] = useState(true);  // start paused!
    const [speed, setSpeed] = useState(1);
    const [busy, setBusy] = useState(false);
    const [selected, setSelected] = useState(null);
    const [mapMode, setMapMode] = useState('biome');
    const [worldId, setWorldId] = useState(null);
    const [toasts, setToasts] = useState([]);
    const wsRef = useRef(null);

    // toasts fade after 6 seconds
    useEffect(() => {
        if (!toasts.length) return;
        const t = setTimeout(() => setToasts(ts => ts.slice(1)), 6000);
        return () => clearTimeout(t);
    }, [toasts]);

    // live rich stats for the selected kingdom (streamed by the server)
    const stats = tick?.selected_stats ?? null;
    const govDesc = stats
        ? meta?.governments?.find(g => g.name === stats.government)?.description
        : null;

    const refreshList = () => {
        fetch('/api/worlds').then(r => r.json()).then(setWorlds);
    };
    useEffect(() => {
        refreshList();
        fetch('/api/meta').then(r => r.json()).then(setMeta);
    }, []);

    const openStream = (id) => {
        if (wsRef.current) wsRef.current.close();
        fetch(`/api/worlds/${id}`).then(r => r.json()).then(s => {
            setState(s);
            setWorldId(id);
            setPaused(true);   // every opened world starts paused
            setSpeed(1);
            setSelected(null);
            const ws = new WebSocket(`ws://${location.host}/api/ws/${id}`);
            ws.onmessage = (m) => {
                const data = JSON.parse(m.data);
                if (data.type === 'system') {
                    setToasts(t => [...t.slice(-3), { id: Date.now() + Math.random(), text: data.text }]);
                } else if (data.type === 'tick') {
                    setTick(data);
                }
            };
            ws.onopen = () => ws.send(JSON.stringify({ speed: 1, pause: true }));
            wsRef.current = ws;
        });
    };

    // keep the server informed about speed/pause/selection changes
    useEffect(() => {
        if (wsRef.current && wsRef.current.readyState === 1) {
            wsRef.current.send(JSON.stringify({
                speed,
                pause: paused,
                selected: selected?.id ?? null,
            }));
        }
    }, [speed, paused, selected]);

    const createWorld = () => {
        setBusy(true);
        fetch('/api/worlds', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ seed: Math.floor(Math.random() * 99999), months: 0 }),
        })
            .then(r => r.json())
            .then(d => { setBusy(false); setState(d.state); setWorldId(d.id); openStream(d.id); refreshList(); });
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
            <header style={header}>
                <strong style={{ color: '#58a6ff' }}>worldsim</strong>
                <button style={btn} onClick={createWorld} disabled={busy}>
                    {busy ? 'generando mundo…' : '+ new world'}
                </button>
                <span style={{ color: '#8b949e', fontSize: 12 }}>
                    {worlds.length} saved · drag pan · wheel zoom · F re-fit · click site/kingdom
                </span>
                <span style={{ flex: 1 }} />
                {worlds.slice(0, 8).map(w => (
                    <button
                        key={w.id}
                        style={{ ...btn, fontSize: 11 }}
                        onClick={() => openStream(w.id)}
                        title={`seed ${w.seed}`}
                    >
                        #{w.id} (seed {w.seed})
                    </button>
                ))}
            </header>
            <div style={{ display: 'flex', flex: 1, minHeight: 0, position: 'relative' }}>
                <WorldMap
                    state={state}
                    tick={tick}
                    mapMode={mapMode}
                    onMapMode={setMapMode}
                    onSiteClick={(k) => setSelected(k)}
                    onBackgroundClick={() => setSelected(null)}
                    stats={stats}
                    govDesc={govDesc}
                    onClearSelection={() => setSelected(null)}
                />
                {/* system toasts (prophecy resolutions, chat) */}
                <div style={{ position: 'absolute', top: 40, right: 360, zIndex: 20, display: 'flex', flexDirection: 'column', gap: 4, maxWidth: 340 }}>
                    {toasts.map(t => (
                        <div key={t.id} style={{
                            background: 'rgba(22,27,34,0.95)',
                            border: '1px solid #f0c674',
                            borderRadius: 6,
                            padding: '6px 10px',
                            fontSize: 12,
                            color: '#f0c674',
                        }}>{t.text}</div>
                    ))}
                </div>
                <Sidebar
                    tick={tick}
                    meta={meta}
                    paused={paused}
                    speed={speed}
                    onPause={() => setPaused(p => !p)}
                    onSpeed={s => setSpeed(s)}
                    selected={selected}
                    onSelect={setSelected}
                    worldId={worldId}
                />
            </div>
        </div>
    );
}

const header = {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '6px 12px',
    background: '#161b22',
    borderBottom: '1px solid #21262d',
};

const btn = {
    background: '#21262d',
    border: '1px solid #30363d',
    color: '#c9d1d9',
    borderRadius: 4,
    padding: '3px 10px',
    cursor: 'pointer',
    fontSize: 13,
};
