import React, { useState, useEffect, useRef } from 'react';

const EMPTY_KINDS = {};

/**
 * Multiplayer room: prophecy game + chat + leaderboard for one world.
 * Identity: anonymous token in localStorage.
 */
export default function RoomPanel({ worldId, month, meta }) {
    const [identity, setIdentity] = useState(null);
    const [name, setName] = useState('');
    const [nameLocked, setNameLocked] = useState(false);
    const [tab, setTab] = useState('prophecies');
    const [predictions, setPredictions] = useState([]);
    const [board, setBoard] = useState([]);
    const [chat, setChat] = useState([]);
    const [chatInput, setChatInput] = useState('');
    const [kinds, setKinds] = useState(EMPTY_KINDS);
    const [pending, setPending] = useState(null);  // {kind, params}
    const chatBoxRef = useRef(null);

    // identity bootstrap
    useEffect(() => {
        let tok = localStorage.getItem('worldsim_token');
        let storedName = localStorage.getItem('worldsim_name');
        if (!tok) {
            fetch('/api/identity').then(r => r.json()).then(d => {
                localStorage.setItem('worldsim_token', d.token);
                setIdentity(d.token);
            });
        } else {
            setIdentity(tok);
        }
        if (storedName) { setName(storedName); setNameLocked(true); }
        if (meta?.prediction_kinds) setKinds(meta.prediction_kinds);
    }, [meta]);

    // poll game state every few seconds (light polling; live push later)
    useEffect(() => {
        if (!worldId) return;
        const load = () => {
            fetch(`/api/worlds/${worldId}/predictions`).then(r => r.json()).then(setPredictions).catch(() => {});
            fetch(`/api/worlds/${worldId}/leaderboard`).then(r => r.json()).then(setBoard).catch(() => {});
        };
        const loadChat = () => {
            fetch(`/api/worlds/${worldId}/chat`).then(r => r.json()).then(setChat).catch(() => {});
        };
        load(); loadChat();
        const iv = setInterval(() => { load(); loadChat(); }, 4000);
        return () => clearInterval(iv);
    }, [worldId]);

    useEffect(() => {
        if (tab === 'chat' && chatBoxRef.current) {
            chatBoxRef.current.scrollTop = chatBoxRef.current.scrollHeight;
        }
    }, [chat, tab]);

    const ensureIdentity = () => {
        if (!identity || !name.trim()) return false;
        localStorage.setItem('worldsim_name', name.trim());
        return true;
    };

    const sealPrediction = async () => {
        if (!ensureIdentity() || !pending) return;
        const body = {
            kind: pending.kind,
            params: pending.params,
            user_token: identity,
            user_name: name.trim(),
        };
        const r = await fetch(`/api/worlds/${worldId}/predictions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (r.ok) {
            setPending(null);
            const preds = await r.json.call ? null : null;
            fetch(`/api/worlds/${worldId}/predictions`).then(r => r.json()).then(setPredictions);
        }
    };

    const sendChat = async (e) => {
        e.preventDefault();
        if (!ensureIdentity() || !chatInput.trim()) return;
        await fetch(`/api/worlds/${worldId}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_token: identity, user_name: name.trim(), text: chatInput.trim(),
            }),
        });
        setChatInput('');
        fetch(`/api/worlds/${worldId}/chat`).then(r => r.json()).then(setChat);
    };

    const KIND_ORDER = ['war_victory', 'era_reach', 'rebellion_any', 'first_city', 'plague', 'beast_slain'];
    const kindLabel = (k, params) => {
        const spec = kinds[k];
        if (!spec) return k;
        return spec.label.replace('{kingdom}', params?.kingdom_name || 'the realm')
                         .replace('{era}', params?.era_name || 'the era');
    };

    const myPending = predictions.filter(p => p.status === 'pending');
    const myResolved = predictions.filter(p => p.status !== 'pending');

    return (
        <div style={{ borderTop: '1px solid #21262d', marginTop: 6, paddingTop: 6, display: 'flex', flexDirection: 'column', minHeight: 170 }}>
            {/* identity bar */}
            {!nameLocked ? (
                <div style={{ display: 'flex', gap: 4, marginBottom: 4 }}>
                    <input
                        style={input}
                        placeholder="tu nombre de oráculo…"
                        value={name}
                        maxLength={24}
                        onChange={e => setName(e.target.value)}
                    />
                    {name.trim() && (
                        <button style={btn} onClick={() => setNameLocked(true)}>ok</button>
                    )}
                </div>
            ) : (
                <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 4 }}>
                    oráculo: <span style={{ color: '#d2a8ff' }}>{name}</span>
                </div>
            )}

            {/* tabs */}
            <div style={{ display: 'flex', gap: 3, marginBottom: 4 }}>
                {['prophecies', 'chat', 'ranks'].map(t => (
                    <button
                        key={t}
                        style={{ ...btn, fontSize: 11, background: tab === t ? '#2f81f7' : btn.background }}
                        onClick={() => setTab(t)}
                    >{t === 'prophecies' ? '🔮 prophecies' : t === 'chat' ? '💬 chat' : '🏆 ranks'}</button>
                ))}
            </div>

            {tab === 'prophecies' && (
                <div style={{ flex: 1, overflowY: 'auto', minHeight: 100 }}>
                    {/* quick-seal buttons */}
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, marginBottom: 4 }}>
                        {KIND_ORDER.map(k => (
                            <button
                                key={k}
                                style={{ ...btn, fontSize: 10, padding: '1px 5px' }}
                                onClick={() => setPending({ kind: k, params: {} })}
                            >+ {k.replace('_', ' ')}</button>
                        ))}
                    </div>
                    {pending && (
                        <div style={{ ...panelBox, marginBottom: 4 }}>
                            <div style={{ fontSize: 10 }}>
                                Seal prophecy: <strong>{kindLabel(pending.kind, pending.params)}</strong>
                                {kinds[pending.kind]?.deadline &&
                                    <span> — resolves within {Math.round(kinds[pending.kind].deadline / 12)}y
                                          (worth {kinds[pending.kind].points} pts)</span>}
                            </div>
                            <button
                                style={{ ...btn, fontSize: 10, marginTop: 3 }}
                                disabled={!nameLocked}
                                onClick={sealPrediction}
                            >{nameLocked ? 'seal it' : 'set your name first'}</button>
                            <button style={{ ...btn, fontSize: 10, marginTop: 3, marginLeft: 4 }} onClick={() => setPending(null)}>cancel</button>
                        </div>
                    )}
                    {[...myPending].reverse().map(p => (
                        <div key={p.id} style={{ fontSize: 10, color: '#d29922', marginBottom: 2 }}>
                            🔮 {p.user_name}: {kindLabel(p.kind, p.params)} — {p.status === 'pending'
                                ? `until y${Math.floor((p.month + p.deadline_months) / 12)}`
                                : p.status}
                        </div>
                    ))}
                    {myResolved.slice(0, 20).reverse().map((p, i) => (
                        <div key={i} style={{ fontSize: 10, opacity: 0.85, marginBottom: 2,
                                               color: p.status === 'won' ? '#3fb950' : '#f85149' }}>
                            {p.status === 'won' ? '✓' : '✗'} {p.user_name}: {p.resolved_text}
                            {p.status === 'won' && <span> (+{p.points})</span>}
                        </div>
                    ))}
                </div>
            )}

            {tab === 'chat' && (
                <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 100 }}>
                    <div ref={chatBoxRef} style={{ flex: 1, overflowY: 'auto', minHeight: 80 }}>
                        {chat.map((m, i) => (
                            <div key={i} style={{ fontSize: 10, marginBottom: 1 }}>
                                <span style={{ color: '#58a6ff' }}>[y{Math.floor(m.month / 12)}]</span>{' '}
                                <span style={{ color: '#d2a8ff' }}>{m.user_name}:</span> {m.text}
                            </div>
                        ))}
                        {chat.length === 0 && (
                            <div style={{ fontSize: 10, color: '#8b949e' }}>the hall is silent…</div>
                        )}
                    </div>
                    <form onSubmit={sendChat} style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                        <input
                            style={{ ...input, flex: 1 }}
                            placeholder={nameLocked ? 'speak, oracle…' : 'set your name first (above)'}
                            value={chatInput}
                            maxLength={300}
                            disabled={!nameLocked}
                            onChange={e => setChatInput(e.target.value)}
                        />
                        <button style={btn} disabled={!nameLocked}>say</button>
                    </form>
                </div>
            )}

            {tab === 'ranks' && (
                <div style={{ flex: 1, overflowY: 'auto', minHeight: 100 }}>
                    {board.length === 0 && (
                        <div style={{ fontSize: 10, color: '#8b949e' }}>no oracles have scored yet</div>
                    )}
                    {board.map(u => (
                        <div key={u.user} style={{ fontSize: 11, display: 'flex', gap: 6 }}>
                            <span style={{ width: 18, color: '#f0c674' }}>#{u.rank}</span>
                            <span style={{ flex: 1 }}>{u.user}</span>
                            <span>{u.points} pts</span>
                            <span style={{ color: '#3fb950' }}>{u.won}W</span>
                            <span style={{ color: '#f85149' }}>{u.lost}L</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

const btn = {
    background: '#21262d',
    border: '1px solid #30363d',
    color: '#c9d1d9',
    borderRadius: 4,
    padding: '2px 8px',
    cursor: 'pointer',
    fontSize: 12,
};

const input = {
    background: '#0d1117',
    border: '1px solid #30363d',
    color: '#c9d1d9',
    borderRadius: 4,
    padding: '3px 6px',
    fontSize: 12,
    outline: 'none',
};

const panelBox = {
    background: '#161b22',
    border: '1px solid #d29922',
    borderRadius: 4,
    padding: '4px 6px',
};
