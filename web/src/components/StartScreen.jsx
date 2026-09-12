import React from 'react';
import { t, vocab } from '../i18n.js';

/**
 * Start screen: pick single player or a dynasty-league room,
 * then configure the world (seed, size, composition, races, era head start).
 */
export default function StartScreen({ worlds, meta, busy, onCreate, onOpen, lang = 'es', user = null, showMine = false, onToggleMine }) {
    const [mode, setMode] = React.useState('single');  // single | league
    const [seed, setSeed] = React.useState(() => String(Math.floor(Math.random() * 99999)));
    const [size, setSize] = React.useState('medium');
    const [nCiv, setNCiv] = React.useState(2);
    const [nTribal, setNTribal] = React.useState(2);
    const [nBeasts, setNBeasts] = React.useState(2);
    const [races, setRaces] = React.useState(null);  // null = all
    const [headStartYears, setHeadStartYears] = React.useState(0);
    const [seasonYears, setSeasonYears] = React.useState(300);
    const [worldName, setWorldName] = React.useState('');
    const [mapStyle, setMapStyle] = React.useState('normal');  // normal | pangea | archipelago

    const SIZES = {
        small: { label: 'Small 120×60', width: 120, height: 60 },
        medium: { label: 'Medium 200×80', width: 200, height: 80 },
        large: { label: 'Large 320×128', width: 320, height: 128 },
    };

    // sea level presets: lower sea = single supercontinent, higher = islands
    const MAP_STYLES = {
        normal: { label: lang === 'es' ? 'Normal' : 'Normal', sea_level: 0.2 },
        pangea: { label: lang === 'es' ? 'Pangea (un continente)' : 'Pangea (one continent)', sea_level: 0.12 },
        archipelago: { label: lang === 'es' ? 'Archipiélago (islas)' : 'Archipelago (islands)', sea_level: 0.32 },
    };

    const toggleRace = (name) => {
        setRaces((prev) => {
            const all = (meta?.races ?? []).map(r => r.name);
            const cur = prev ?? all;
            if (cur.includes(name)) {
                const next = cur.filter(r => r !== name);
                return next.length ? next : all;  // unchecking all resets to all
            }
            return [...cur, name];
        });
    };

    const submit = () => {
        onCreate({
            seed: parseInt(seed || '42', 10),
            ...SIZES[size],
            n_civilized: nCiv,
            n_tribal: nTribal,
            n_beasts: nBeasts,
            months: headStartYears * 12,
            name: worldName.trim(),
            mode,
            season_years: seasonYears,
            enabled_races: races,
            sea_level: MAP_STYLES[mapStyle].sea_level,
        });
    };

    const L = (k) => t(lang, k);
    const V = (w) => vocab(lang, w);

    return (
        <div style={wrap}>
            <h1 style={{ color: '#58a6ff', margin: '8px 0 2px' }}>worldsim</h1>
            <p style={{ color: '#8b949e', fontSize: 13, marginTop: 0 }}>
                fantasy worlds with emergent history — watch civilizations rise, scheme, and fall
            </p>

            <div style={{ display: 'flex', gap: 12, margin: '12px 0' }}>
                {[
                    { id: 'single', title: L('single'), desc: lang === 'es'
                        ? 'Observa la simulación. Pausa, mira el reloj, inspecciona cada reino.'
                        : 'Observe the simulation. Pause, rewind the clock, inspect every realm.' },
                    { id: 'league', title: L('league'), desc: lang === 'es'
                        ? 'Reclama un reino. Su crónica puntúa TUS puntos. Campeón al final.'
                        : 'Claim a kingdom. Its chronicle scores YOUR points. Champion at the end.' },
                ].map(m => (
                    <button
                        key={m.id}
                        onClick={() => setMode(m.id)}
                        style={{
                            ...card,
                            borderColor: mode === m.id ? '#2f81f7' : '#30363d',
                            background: mode === m.id ? '#0d2a5c' : '#161b22',
                        }}
                    >
                        <div style={{ fontSize: 15, marginBottom: 4 }}><strong>{m.title}</strong></div>
                        <div style={{ fontSize: 11, color: '#8b949e' }}>{m.desc}</div>
                    </button>
                ))}
            </div>

            <div style={form}>
                <label style={lbl}>World name <input style={input} value={worldName} maxLength={48}
                    placeholder="e.g. The Shattered Vale" onChange={e => setWorldName(e.target.value)} /></label>
                <label style={lbl}>Seed <input style={input} value={seed} inputMode="numeric"
                    onChange={e => setSeed(e.target.value.replace(/\D/g, '').slice(0, 6))} />
                    <button style={btn} onClick={() => setSeed(String(Math.floor(Math.random() * 99999)))}>🎲</button></label>
                <label style={lbl}>Map size
                    <select style={input} value={size} onChange={e => setSize(e.target.value)}>
                        {Object.entries(SIZES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
                    </select></label>
                <label style={lbl}>Civilized kingdoms
                    <input style={{ ...input, width: 46 }} type="number" min={0} max={8} value={nCiv}
                        onChange={e => setNCiv(Math.max(0, Math.min(8, +e.target.value || 0)))} /></label>
                <label style={lbl}>Tribal kingdoms
                    <input style={{ ...input, width: 46 }} type="number" min={0} max={8} value={nTribal}
                        onChange={e => setNTribal(Math.max(0, Math.min(8, +e.target.value || 0)))} /></label>
                <label style={lbl}>Beasts
                    <input style={{ ...input, width: 46 }} type="number" min={0} max={8} value={nBeasts}
                        onChange={e => setNBeasts(Math.max(0, Math.min(8, +e.target.value || 0)))} /></label>
                <label style={lbl}>Start in year
                    <input style={{ ...input, width: 64 }} type="number" min={0} max={1000} value={headStartYears}
                        onChange={e => setHeadStartYears(Math.max(0, Math.min(1000, +e.target.value || 0)))} /></label>
                <label style={lbl}>Map style
                    <select style={input} value={mapStyle} onChange={e => setMapStyle(e.target.value)}>
                        {Object.entries(MAP_STYLES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
                    </select></label>
                {mode === 'league' && (
                    <label style={lbl}>Season ends at year
                        <input style={{ ...input, width: 64 }} type="number" min={12} max={2000} value={seasonYears}
                            onChange={e => setSeasonYears(Math.max(12, Math.min(2000, +e.target.value || 300)))} /></label>
                )}
            </div>

            <div style={{ marginTop: 8 }}>
                <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>
                    {lang === 'es' ? 'razas en este mundo' : 'races in this world'}
                </div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                    {(meta?.races ?? []).map(r => {
                        const on = races === null || races.includes(r.name);
                        return (
                            <button key={r.name}
                                style={{ ...btn, fontSize: 11, background: on ? '#1f6feb' : btn.background, opacity: on ? 1 : 0.55 }}
                                onClick={() => toggleRace(r.name)}
                                title={`${r.form} · strength ${r.strength}`}>
                                {V(r.name)}
                            </button>
                        );
                    })}
                </div>
            </div>

            <button style={{ ...btn, fontSize: 15, padding: '8px 28px', marginTop: 16 }} onClick={submit} disabled={busy}>
                {busy ? 'generating world…' : mode === 'league' ? '⚔ found the league' : '🌍 generate & observe'}
            </button>

            {worlds.length > 0 && (
                <div style={{ marginTop: 22, width: '100%', maxWidth: 640 }}>
                    <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 6, display: 'flex', gap: 8, alignItems: 'center' }}>
                        <span>{lang === 'es' ? 'mundos guardados' : 'saved worlds'}</span>
                        {user && (
                            <button
                                style={{ ...btn, fontSize: 11, background: showMine ? '#2f81f7' : btn.background }}
                                onClick={onToggleMine}>
                                {L('myWorlds')}
                            </button>
                        )}
                    </div>
                    {worlds.map(w => (
                        <button key={w.id} style={{ ...btn, fontSize: 12, margin: '0 6px 6px 0' }}
                            onClick={() => onOpen(w.id)}
                            title={`seed ${w.seed} · ${w.months_simulated}mo`}>
                            {w.mode === 'league' ? '👑 ' : ''}{w.name} · y{Math.floor(w.months_simulated / 12)}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}

const wrap = {
    display: 'flex', flexDirection: 'column', alignItems: 'center',
    padding: '28px 16px', overflowY: 'auto', flex: 1,
};

const card = {
    border: '2px solid #30363d', borderRadius: 10, padding: '12px 16px',
    cursor: 'pointer', color: '#e6edf3', textAlign: 'left', width: 260,
};

const form = {
    display: 'flex', gap: 12, flexWrap: 'wrap', justifyContent: 'center',
    maxWidth: 640, marginTop: 6,
};

const lbl = { fontSize: 11, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 6 };

const input = {
    background: '#0d1117', border: '1px solid #30363d', color: '#e6edf3',
    borderRadius: 4, padding: '3px 8px', fontSize: 13, outline: 'none', width: 110,
};

const btn = {
    background: '#21262d', border: '1px solid #30363d', color: '#c9d1d9',
    borderRadius: 4, padding: '3px 10px', cursor: 'pointer', fontSize: 13,
};
