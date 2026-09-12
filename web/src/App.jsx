import React, { useEffect, useRef, useState } from 'react';
import WorldMap from './components/WorldMap.jsx';
import Sidebar from './components/Sidebar.jsx';
import StartScreen from './components/StartScreen.jsx';
import { ensureIdentity, getIdentity } from './identity.js';

/**
 * worldsim web client.
 * Start screen (lobby) -> create or open a world -> live WS stream.
 * ?world=N deep-links straight into a world (shareable room link).
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
    const [selectedStats, setSelectedStats] = useState(null);  // survives ticks
    const [mapMode, setMapMode] = useState('biome');
    const [worldId, setWorldId] = useState(null);
    const [worldMode, setWorldMode] = useState('single');
    const [claims, setClaims] = useState([]);
    const [standings, setStandings] = useState([]);
    const [toasts, setToasts] = useState([]);
    const wsRef = useRef(null);

    // toasts fade after 6 seconds
    useEffect(() => {
        if (!toasts.length) return;
        const t = setTimeout(() => setToasts(ts => ts.slice(1)), 6000);
        return () => clearTimeout(t);
    }, [toasts]);

    // live rich stats for the selected kingdom (private per-socket channel)
    const stats = selectedStats;
    const govDesc = stats
        ? meta?.governments?.find(g => g.name === stats.government)?.description
        : null;

    const refreshList = () => {
        fetch('/api/worlds').then(r => r.json()).then(setWorlds);
    };
    useEffect(() => {
        refreshList();
        fetch('/api/meta').then(r => r.json()).then(setMeta);
        ensureIdentity();
        // deep link: ?world=N opens that world straight away
        const q = new URLSearchParams(location.search).get('world');
        if (q) openStream(parseInt(q, 10));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // league claims polling (crowns on the map + dynasty tab need it)
    useEffect(() => {
        if (!worldId) return;
        const load = () => {
            fetch(`/api/worlds/${worldId}/claims`).then(r => r.json()).then(setClaims).catch(() => {});
            fetch(`/api/worlds/${worldId}/standings`).then(r => r.json()).then(setStandings).catch(() => {});
        };
        load();
        const iv = setInterval(load, 4000);
        return () => clearInterval(iv);
    }, [worldId]);

    const openStream = (id, modeHint) => {
        if (wsRef.current) wsRef.current.close();
        // refresh the world list first: mode must come from fresh data,
        // otherwise deep-linked league worlds render as single-player
        fetch('/api/worlds').then(r => r.json()).then(ws => {
            setWorlds(ws);
            const entry = ws.find(w => w.id === id);
            setWorldMode(modeHint || entry?.mode || 'single');
        });
        fetch(`/api/worlds/${id}`).then(r => r.json()).then(s => {
            setState(s);
            setWorldId(id);
            setPaused(true);   // every opened world starts paused
            setSpeed(1);
            setSelected(null);
            setSelectedStats(null);
            setClaims([]);
            setStandings([]);
            // world mode for conditional UI (dynasty tab, claim buttons)
            const entry = worlds.find(w => w.id === id);
            const m = modeHint || entry?.mode || 'single';
            setWorldMode(m);
            history.replaceState(null, '', `?world=${id}`);
            const ws = new WebSocket(`ws://${location.host}/api/ws/${id}`);
            ws.onmessage = (m) => {
                const data = JSON.parse(m.data);
                if (data.type === 'system') {
                    setToasts(t => [...t.slice(-3), { id: Date.now() + Math.random(), text: data.text }]);
                } else if (data.type === 'stats') {
                    // private per-socket channel: never clobbered by ticks
                    setSelectedStats(data.stats);
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

    const createWorld = (params) => {
        setBusy(true);
        fetch('/api/worlds', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params),
        })
            .then(r => r.json())
            .then(d => {
                setBusy(false);
                setState(d.state);
                setWorldId(d.id);
                setWorldMode(params.mode || 'single');
                openStream(d.id, params.mode);
                refreshList();
            });
    };

    const backToMenu = () => {
        if (wsRef.current) wsRef.current.close();
        setState(null);
        setTick(null);
        setWorldId(null);
        setSelected(null);
        setSelectedStats(null);
        setClaims([]);
        setStandings([]);
        history.replaceState(null, '', location.pathname);
        refreshList();
    };

    const copyInvite = () => {
        const url = `${location.origin}${location.pathname}?world=${worldId}`;
        navigator.clipboard?.writeText(url).catch(() => {});
        setToasts(t => [...t.slice(-3), { id: Date.now() + Math.random(), text: `🔗 invite link copied: ${url}` }]);
    };

    const doClaim = async (kingdomId) => {
        const { token } = getIdentity();
        let userName = getIdentity().name;
        if (!userName) {
            // no prompt(): auto-generate an oracle name, editable later in room tab
            userName = 'Oracle-' + Math.floor(Math.random() * 9000 + 1000);
            const { setIdentityName } = await import('./identity.js');
            setIdentityName(userName);
            setToasts(t => [...t.slice(-3), { id: Date.now() + Math.random(), text: `claiming as ${userName} — set your name in the room tab` }]);
        }
        const tok = token || await ensureIdentity();
        const r = await fetch(`/api/worlds/${worldId}/claims`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                kingdom_id: kingdomId,
                user_token: tok,
                user_name: userName,
            }),
        });
        if (r.ok) {
            fetch(`/api/worlds/${worldId}/claims`).then(r => r.json()).then(setClaims).catch(() => {});
        } else {
            const err = await r.json().catch(() => ({}));
            setToasts(t => [...t.slice(-3), { id: Date.now() + Math.random(), text: `⚠ claim failed: ${err.detail || r.status}` }]);
        }
    };

    // lobby: no world open yet
    if (!worldId) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
                <header style={header}>
                    <strong style={{ color: '#58a6ff' }}>worldsim</strong>
                    <span style={{ color: '#8b949e', fontSize: 12 }}>
                        fantasy worlds with emergent history
                    </span>
                </header>
                <StartScreen
                    worlds={worlds}
                    meta={meta}
                    busy={busy}
                    onCreate={createWorld}
                    onOpen={openStream}
                />
            </div>
        );
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
            <header style={header}>
                <strong style={{ color: '#58a6ff' }}>worldsim</strong>
                <button style={btn} onClick={backToMenu}>‹ worlds</button>
                {worldMode === 'league' && (
                    <button style={{ ...btn, fontSize: 12 }} onClick={copyInvite}
                            title="copy shareable room link">
                        🔗 invite
                    </button>
                )}
                <span style={{ color: '#8b949e', fontSize: 12 }}>
                    {worldMode === 'league' ? '👑 dynasty league' : '👁 observing'} · drag pan · wheel zoom · F re-fit · click site/kingdom
                </span>
                <span style={{ flex: 1 }} />
            </header>
            <div style={{ display: 'flex', flex: 1, minHeight: 0, position: 'relative' }}>
                <WorldMap
                    state={state}
                    tick={tick}
                    mapMode={mapMode}
                    onMapMode={setMapMode}
                    onSiteClick={(k) => { setSelected(k); setSelectedStats(null); }}
                    onBackgroundClick={() => { setSelected(null); setSelectedStats(null); }}
                    stats={stats}
                    govDesc={govDesc}
                    onClearSelection={() => { setSelected(null); setSelectedStats(null); }}
                    claims={claims}
                    worldMode={worldMode}
                    onClaim={doClaim}
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
                    worldMode={worldMode}
                    claims={claims}
                    standings={standings}
                    onClaim={doClaim}
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
