import React from 'react';
import WorldMap from './WorldMap.jsx';
import ChroniclePanel from './ChroniclePanel.jsx';
import { adaptHistoricalWorld, validateHistoricalArchive } from '../historicalArchive.js';

const button = {
    background: '#21262d', border: '1px solid #30363d', color: '#c9d1d9',
    borderRadius: 5, padding: '6px 10px', cursor: 'pointer', fontSize: 12,
};

export default function HistoricalArchive({ onClose, lang = 'es' }) {
    const [entries, setEntries] = React.useState([]);
    const [selectedId, setSelectedId] = React.useState(null);
    const [error, setError] = React.useState('');
    const [fileName, setFileName] = React.useState('');
    const [mapMode, setMapMode] = React.useState('biome');

    const selected = entries.find(entry => entry.archiveId === selectedId) ?? null;
    const adapted = selected ? adaptHistoricalWorld(selected) : null;

    const loadFile = async (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        setError('');
        setEntries([]);
        setSelectedId(null);
        setFileName('');
        if (file.size > 50 * 1024 * 1024) {
            setError('El paquete supera el límite de 50 MB.');
            event.target.value = '';
            return;
        }
        try {
            const archive = JSON.parse(await file.text());
            const worlds = validateHistoricalArchive(archive);
            setEntries(worlds);
            setFileName(file.name);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'No se pudo leer el archivo.');
        }
        event.target.value = '';
    };

    const backToList = () => {
        setSelectedId(null);
        setMapMode('biome');
    };

    return (
        <div style={overlay}>
            <div style={shell}>
                <header style={header}>
                    <strong style={{ color: '#58a6ff' }}>📚 Archivo histórico local</strong>
                    <span style={{ flex: 1 }} />
                    {selected && <button style={button} onClick={backToList}>‹ Mundos del archivo</button>}
                    <button style={button} onClick={onClose}>Cerrar</button>
                </header>

                {!selected ? (
                    <main style={listPane}>
                        <h2 style={{ margin: '8px 0', fontSize: 18 }}>Snapshots históricos</h2>
                        <p style={muted}>
                            Visor privado de solo lectura. El archivo se abre desde tu equipo y no se envía al servidor.
                            No restaura ni ejecuta la simulación.
                        </p>
                        <label style={fileLabel}>
                            Seleccionar paquete JSON
                            <input type="file" accept=".json,application/json" onChange={loadFile}
                                style={{ display: 'block', marginTop: 8 }} />
                        </label>
                        {error && <p style={errorStyle}>{error}</p>}
                        {fileName && <p style={muted}>Archivo cargado: {fileName} · {entries.length} mundos</p>}
                        {entries.map(entry => {
                            const state = entry.record.state;
                            const name = entry.record.name || state.kingdoms?.[0]?.name || `Mundo ${entry.record.source_world_id}`;
                            return (
                                <button key={entry.archiveId} style={worldButton}
                                    onClick={() => setSelectedId(entry.archiveId)}>
                                    <strong>{name}</strong>
                                    <span style={muted}>
                                        ID histórico {entry.record.source_world_id} · año {Math.floor((state.month ?? 0) / 12)}
                                        {' · '}{state.kingdoms.length} reinos
                                    </span>
                                    <span style={muted}>Origen: {entry.sourceName}</span>
                                </button>
                            );
                        })}
                    </main>
                ) : (
                    <>
                        <div style={archiveMeta}>
                            <strong>{selected.record.name || `Mundo histórico ${selected.record.source_world_id}`}</strong>
                            <span style={muted}>
                                Snapshot estático · año {Math.floor((adapted.tick.month ?? 0) / 12)} · ID {selected.record.source_world_id}
                            </span>
                        </div>
                        <div style={viewer}>
                            <section style={mapColumn}>
                                <WorldMap
                                    key={selected.archiveId}
                                    state={adapted.state}
                                    tick={adapted.tick}
                                    mapMode={adapted.mapModes.includes(mapMode) ? mapMode : 'biome'}
                                    onMapMode={setMapMode}
                                    availableMapModes={adapted.mapModes}
                                    fixed={false}
                                    lang={lang}
                                />
                                <ChroniclePanel events={adapted.tick.events} tick={adapted.tick} lang={lang} />
                            </section>
                            <aside style={kingdomPane}>
                                <h3 style={{ margin: '0 0 8px', fontSize: 13 }}>Reinos registrados</h3>
                                {adapted.tick.kingdoms.map(kingdom => (
                                    <div key={kingdom.id} style={kingdomRow}>
                                        <span style={{ ...swatch, background: `rgb(${kingdom.color.join(',')})` }} />
                                        <div style={{ minWidth: 0 }}>
                                            <strong>{kingdom.name}</strong>
                                            <div style={muted}>
                                                {kingdom.sites.length} asentamientos
                                                {Number.isFinite(kingdom.population) ? ` · población ${kingdom.population.toLocaleString()}` : ''}
                                            </div>
                                        </div>
                                    </div>
                                ))}
                                <p style={{ ...muted, marginTop: 14 }}>
                                    Solo se muestran atributos presentes en el snapshot original. No hay controles de simulación,
                                    estadísticas recalculadas ni reproducción temporal.
                                </p>
                            </aside>
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}

const overlay = {
    position: 'fixed', inset: 0, zIndex: 1000, background: '#0d1117',
    color: '#c9d1d9', display: 'flex', flexDirection: 'column',
};
const shell = { display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' };
const header = {
    display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px',
    background: '#161b22', borderBottom: '1px solid #30363d',
};
const listPane = { overflow: 'auto', padding: 18, maxWidth: 900, width: '100%', margin: '0 auto' };
const muted = { display: 'block', color: '#8b949e', fontSize: 11, lineHeight: 1.5 };
const fileLabel = {
    display: 'block', margin: '16px 0', padding: 12, border: '1px dashed #484f58',
    borderRadius: 6, fontSize: 13,
};
const errorStyle = { color: '#ff7b72', fontSize: 12 };
const worldButton = {
    display: 'flex', flexDirection: 'column', gap: 3, width: '100%', textAlign: 'left',
    padding: 10, margin: '8px 0', background: '#161b22', color: '#c9d1d9',
    border: '1px solid #30363d', borderRadius: 6, cursor: 'pointer',
};
const archiveMeta = {
    display: 'flex', flexDirection: 'column', gap: 2, padding: '8px 12px',
    background: '#0d1117', borderBottom: '1px solid #21262d',
};
const viewer = { display: 'flex', flex: 1, minHeight: 0 };
const mapColumn = { display: 'flex', flex: 1, flexDirection: 'column', minWidth: 0, minHeight: 0 };
const kingdomPane = {
    width: 260, overflowY: 'auto', padding: 12, borderLeft: '1px solid #30363d',
    background: '#0d1117',
};
const kingdomRow = {
    display: 'flex', gap: 8, alignItems: 'flex-start', padding: '7px 0',
    borderBottom: '1px solid #21262d', fontSize: 12,
};
const swatch = { display: 'inline-block', flex: '0 0 10px', width: 10, height: 10, marginTop: 3, borderRadius: 2 };
