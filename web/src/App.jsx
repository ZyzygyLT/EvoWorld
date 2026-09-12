import React, { useEffect, useRef, useState } from 'react';
import WorldMap from './components/WorldMap.jsx';
import Sidebar from './components/Sidebar.jsx';
import StartScreen from './components/StartScreen.jsx';
import WorldBar from './components/WorldBar.jsx';
import { DEFAULT_LANG, t } from './i18n.js';
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
    const [followId, setFollowId] = useState(null);  // kingdom id the camera tracks
    const worldPopRef = useRef([]);  // [{month, pop}] world-population sparkline
    const [lang, setLang] = useState(() => localStorage.getItem('worldsim_lang') || DEFAULT_LANG);
    const [user, setUser] = useState(null);  // {username, user_id} when logged in
    const [showLogin, setShowLogin] = useState(false);
    const [showMine, setShowMine] = useState(false);
    const [showTutorial, setShowTutorial] = useState(
        () => !localStorage.getItem('worldsim_tutorial_done'));
    const wsRef = useRef(null);
    const mapCanvasRef = useRef(null);

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

    const setLangAndSave = (l) => {
        setLang(l);
        localStorage.setItem('worldsim_lang', l);
    };

    const authHeaders = () => {
        const tok = localStorage.getItem('worldsim_token');
        return tok ? { 'Authorization': `Bearer ${tok}` } : {};
    };

    const refreshList = () => {
        const q = showMine && user ? '?mine=1' : '';
        fetch('/api/worlds' + q, { headers: authHeaders() })
            .then(r => r.json()).then(setWorlds).catch(() => {});
    };
    useEffect(() => {
        refreshList();
        fetch('/api/meta').then(r => r.json()).then(setMeta);
        ensureIdentity();
        // restore login session if a token is stored
        const tok = localStorage.getItem('worldsim_token');
        if (tok) {
            fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${tok}` } })
                .then(r => r.ok ? r.json() : null)
                .then(me => { if (me) setUser({ username: me.username, user_id: me.user_id }); })
                .catch(() => {});
        }
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
                    // accumulate world population for the bottom-bar sparkline
                    const total = (data.kingdoms ?? []).reduce(
                        (a, k) => a + (k.population || 0), 0);
                    const hist = worldPopRef.current;
                    const last = hist[hist.length - 1];
                    if (!last || last.month !== data.month) {
                        hist.push({ month: data.month, pop: Math.round(total) });
                        if (hist.length > 500) hist.splice(0, hist.length - 500);
                    }
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
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
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

    const doLogin = async (username, password, register) => {
        const ep = register ? '/api/auth/register' : '/api/auth/login';
        const r = await fetch(ep, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) return d.detail || 'error';
        localStorage.setItem('worldsim_token', d.token);
        setUser({ username: d.username, user_id: d.user_id });
        refreshList();
        return null;
    };

    const doLogout = async () => {
        const tok = localStorage.getItem('worldsim_token');
        if (tok) {
            await fetch('/api/auth/logout', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${tok}` },
            }).catch(() => {});
            localStorage.removeItem('worldsim_token');
        }
        setUser(null);
        refreshList();
    };

    const exportPng = () => {
        const canvas = mapCanvasRef.current;
        if (!canvas) return;
        const a = document.createElement('a');
        a.href = canvas.toDataURL('image/png');
        a.download = `worldsim-${worldId}-y${Math.floor((tick?.month ?? 0) / 12)}.png`;
        a.click();
    };

    const dismissTutorial = () => {
        localStorage.setItem('worldsim_tutorial_done', '1');
        setShowTutorial(false);
    };

    // champion of a finished league season (for the banner)
    const openWorld = worlds.find(w => w.id === worldId);
    const champion = openWorld?.season_over && standings.length ? standings[0] : null;
    const championKingdom = champion && tick
        ? tick.kingdoms?.find(k => k.id === champion.kingdom_id)?.name ?? ''
        : '';

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
                    <span style={{ flex: 1 }} />
                    <LangToggle lang={lang} onChange={setLangAndSave} />
                    <AuthButton user={user} onLogin={() => setShowLogin(true)} onLogout={doLogout} t={(k) => t(lang, k)} />
                </header>
                <StartScreen
                    worlds={worlds}
                    meta={meta}
                    busy={busy}
                    onCreate={createWorld}
                    onOpen={openStream}
                    lang={lang}
                    user={user}
                    showMine={showMine}
                    onToggleMine={() => {
                        const next = !showMine;
                        setShowMine(next);
                        const h = next && user
                            ? { 'Authorization': `Bearer ${localStorage.getItem('worldsim_token')}` }
                            : {};
                        const q = next && user ? '?mine=1' : '';
                        fetch('/api/worlds' + q, { headers: h })
                            .then(r => r.json()).then(setWorlds).catch(() => {});
                    }}
                />
                {showLogin && (
                    <AuthModal
                        onClose={() => setShowLogin(false)}
                        onAuth={(u) => { setUser(u); setShowLogin(false); refreshList(); }}
                        t={(k) => t(lang, k)}
                    />
                )}
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
                <button style={{ ...btn, fontSize: 12 }} onClick={exportPng} title="download map PNG">
                    {t(lang, 'exportPng')}
                </button>
                <LangToggle lang={lang} onChange={setLangAndSave} />
                <AuthButton user={user} onLogin={() => setShowLogin(true)} onLogout={doLogout} t={(k) => t(lang, k)} />
            </header>
            {champion && (
                <div style={championBanner}>
                    🏆 {champion.user} {t(lang, 'championBanner')} {champion.points} {t(lang, 'points')}{championKingdom ? ` — ${championKingdom}` : ''}
                </div>
            )}
            {showTutorial && (
                <TutorialOverlay
                    onClose={dismissTutorial}
                    lang={lang}
                />
            )}
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
                    lang={lang}
                    canvasRef={mapCanvasRef}
                    followId={followId}
                    onFollowClear={() => setFollowId(null)}
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
                    lang={lang}
                    followId={followId}
                    onFollow={setFollowId}
                />
            </div>
            <WorldBar tick={tick} lang={lang} historyRef={worldPopRef} />
            {showLogin && (
                <AuthModal
                    onClose={() => setShowLogin(false)}
                    onAuth={(u) => { setUser(u); setShowLogin(false); refreshList(); }}
                    t={(k) => t(lang, k)}
                />
            )}
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

function LangToggle({ lang, onChange }) {
    return (
        <button
            style={{ ...btn, fontSize: 12 }}
            onClick={() => onChange(lang === 'es' ? 'en' : 'es')}
            title="idioma / language"
        >
            {lang === 'es' ? 'ES ✓' : 'EN ✓'}
        </button>
    );
}

function AuthButton({ user, onLogin, onLogout, t }) {
    if (user) {
        return (
            <span style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12 }}>
                <span style={{ color: '#d2a8ff' }}>🧙 {user.username}</span>
                <button style={{ ...btn, fontSize: 11 }} onClick={onLogout}>{t('logout')}</button>
            </span>
        );
    }
    return (
        <button style={{ ...btn, fontSize: 12 }} onClick={onLogin}>🧙 {t('login')}</button>
    );
}

function AuthModal({ onClose, onAuth, t }) {
    const [mode, setMode] = React.useState('login');
    const [username, setUsername] = React.useState('');
    const [password, setPassword] = React.useState('');
    const [error, setError] = React.useState(null);
    const [busy, setBusy] = React.useState(false);

    const submit = async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
            const ep = mode === 'register' ? '/api/auth/register' : '/api/auth/login';
            const r = await fetch(ep, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: username.trim(), password }),
            });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                setError(d.detail || 'error');
            } else {
                localStorage.setItem('worldsim_token', d.token);
                onAuth({ username: d.username, user_id: d.user_id });
                onClose();
            }
        } finally {
            setBusy(false);
        }
    };

    return (
        <div style={modalBg} onClick={onClose}>
            <form style={modalBox} onClick={e => e.stopPropagation()} onSubmit={submit}>
                <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
                    {['login', 'register'].map(m => (
                        <button key={m} type="button"
                            style={{ ...btn, background: mode === m ? '#2f81f7' : btn.background }}
                            onClick={() => setMode(m)}>
                            {m === 'login' ? t('login') : t('register')}
                        </button>
                    ))}
                    <span style={{ flex: 1 }} />
                    <button type="button" style={btn} onClick={onClose}>✕</button>
                </div>
                <input style={modalInput} placeholder={t('username')} value={username}
                    maxLength={24} onChange={e => setUsername(e.target.value)} />
                <input style={modalInput} type="password" placeholder={t('password')}
                    value={password} onChange={e => setPassword(e.target.value)} />
                {error && <div style={{ color: '#f85149', fontSize: 12 }}>{error}</div>}
                <button style={{ ...btn, marginTop: 8 }} disabled={busy}>
                    {mode === 'login' ? t('login') : t('register')}
                </button>
            </form>
        </div>
    );
}

function TutorialOverlay({ onClose, lang }) {
    const steps = lang === 'es' ? [
        '① Crea un mundo (o abre uno guardado) — empieza en la Edad de Piedra, pausado.',
        '② Pulsa ▶ para correr el tiempo: mira reinos fundarse, guerras y eras en la crónica.',
        '③ Haz click en cualquier reino o sitio para ver sus estadísticas. F re-encuadra el mapa.',
    ] : [
        '① Create a world (or open a saved one) — it starts in the Stone Age, paused.',
        '② Press ▶ to run time: watch realms found, wars and eras in the chronicle.',
        '③ Click any realm or site for its stats. F re-fits the map.',
    ];
    return (
        <div style={modalBg} onClick={onClose}>
            <div style={modalBox} onClick={e => e.stopPropagation()}>
                <div style={{ fontSize: 14, marginBottom: 8, color: '#58a6ff' }}>
                    {lang === 'es' ? 'Cómo jugar' : 'How to play'}
                </div>
                {steps.map((s, i) => (
                    <div key={i} style={{ fontSize: 12, marginBottom: 6 }}>{s}</div>
                ))}
                <button style={{ ...btn, marginTop: 6 }} onClick={onClose}>OK</button>
            </div>
        </div>
    );
}

const modalBg = {
    position: 'fixed', inset: 0, zIndex: 100,
    background: 'rgba(0,0,0,0.6)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
};

const modalBox = {
    background: '#161b22',
    border: '1px solid #30363d',
    borderRadius: 8,
    padding: 16,
    width: 300,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
};

const modalInput = {
    background: '#0d1117',
    border: '1px solid #30363d',
    color: '#e6edf3',
    borderRadius: 4,
    padding: '6px 8px',
    fontSize: 13,
    outline: 'none',
};

const championBanner = {
    background: 'linear-gradient(90deg, #3d2b00, #1c1600)',
    borderBottom: '1px solid #f0c674',
    color: '#f0c674',
    fontSize: 13,
    padding: '6px 12px',
    textAlign: 'center',
};
