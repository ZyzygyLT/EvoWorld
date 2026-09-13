import React, { useRef, useEffect, useState, useCallback } from 'react';
import { biomeColor } from '../biomes.js';
import { vocab } from '../i18n.js';
import StatsPanel from './StatsPanel.jsx';

/**
 * Canvas world map with pan/zoom + political overlay + site markers.
 * Re-fits the camera on every world load; survives canvas resizes.
 */
export default function WorldMap({ state, tick, mode = 'biome', mapMode = 'biome', onSiteClick, onBackgroundClick, onMapMode, stats, govDesc, onClearSelection, claims = [], worldMode = 'single', onClaim, lang = 'es', canvasRef: externalRef, followId = null, onFollowClear, warFocus = null }) {
    const canvasRef = useRef(null);
    // expose the canvas for PNG export
    useEffect(() => {
        if (externalRef) externalRef.current = canvasRef.current;
    });
    const viewRef = useRef(null);   // null => needs fit
    const dragRef = useRef(null);
    const [hover, setHover] = useState('');

    const world = state?.world;
    const W = world?.width ?? 0;
    const H = world?.height ?? 0;

    // static per-world layers: lake mask (ocean not connected to edge) + hillshade
    const staticRef = useRef(null);
    const staticKeyRef = useRef('');
    useEffect(() => {
        if (!world) return;
        const key = `${state?.seed}`;
        if (staticKeyRef.current === key && staticRef.current) return;
        staticKeyRef.current = key;
        const grid = world.biome;
        // elevation must be a flat W*H array; anything else is ignored
        // (a shape mismatch once produced NaN hillshade = black land)
        const rawElev = Array.isArray(world.elevation) && world.elevation.length === W * H
            ? world.elevation : null;
        // flood fill ocean from the edges -> anything wet left over is a lake
        const lake = new Uint8Array(W * H);
        if (grid) {
            const wet = (x, y) => grid[y][x] === 0;
            const seen = new Uint8Array(W * H);
            const stack = [];
            for (let x = 0; x < W; x++) { stack.push([x, 0], [x, H - 1]); }
            for (let y = 0; y < H; y++) { stack.push([0, y], [W - 1, y]); }
            while (stack.length) {
                const [x, y] = stack.pop();
                if (x < 0 || y < 0 || x >= W || y >= H) continue;
                const i = y * W + x;
                if (seen[i] || !wet(x, y)) continue;
                seen[i] = 1;
                stack.push([x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]);
            }
            for (let i = 0; i < W * H; i++) {
                const y = Math.floor(i / W), x = i % W;
                if (wet(x, y) && !seen[i]) lake[i] = 1;
            }
        }
        // hillshade 0..1 from elevation gradient (fake NW light)
        let shade = null;
        if (rawElev) {
            shade = new Float32Array(W * H);
            const at = (x, y) => rawElev[Math.max(0, Math.min(H - 1, y)) * W + Math.max(0, Math.min(W - 1, x))] / 255;
            for (let y = 0; y < H; y++) {
                for (let x = 0; x < W; x++) {
                    const dx = at(x - 1, y) - at(x + 1, y);
                    const dy = at(x, y - 1) - at(x, y + 1);
                    // light from NW: slopes facing NW brighter
                    shade[y * W + x] = Math.max(-0.35, Math.min(0.35, (dx + dy) * 1.4));
                }
            }
        }
        staticRef.current = { lake, shade };
    }, [world, state, W, H]);

    // paint the biome base into an offscreen canvas once per state/mode
    const baseRef = useRef(null);
    const baseKeyRef = useRef('');
    // deterministic per-tile tone variation (breaks the flat-color look)
    const tone = (x, y) => {
        let h = (x * 73856093) ^ (y * 19349663);
        h = (h ^ (h >> 13)) >>> 0;
        return 0.92 + (h % 17) / 17 * 0.16;  // 0.92..1.08
    };
    useEffect(() => {
        if (!world) return;
        // repaint only when the world, mode, or month-bucket changes (not every tick)
        const bucket = Math.floor((tick?.month ?? 0) / 6);
        const key = `${state?.seed}|${mapMode}|${bucket}`;
        if (baseKeyRef.current === key && baseRef.current) return;
        const is_new_world = !baseKeyRef.current.startsWith(`${state?.seed}|`);
        baseKeyRef.current = key;
        const cv = document.createElement('canvas');
        cv.width = W; cv.height = H;
        const ctx = cv.getContext('2d', { willReadFrequently: true });
        const img = ctx.createImageData(W, H);
        const grid = world.biome;
        const elev = Array.isArray(world.elevation) && world.elevation.length === W * H
            ? world.elevation : null;
        const { lake, shade } = staticRef.current ?? {};
        const isOcean = (x, y) => grid[y][x] === 0;
        const dim = mapMode === 'political' || mapMode === 'realms';
        const influence = mapMode === 'realms' ? tick?.influence : null;
        for (let y = 0; y < H; y++) {
            for (let x = 0; x < W; x++) {
                const i = (y * W + x) * 4;
                let [r, g, b] = biomeColor(grid[y][x]);
                const hgt = elev ? elev[y * W + x] / 255 : 0.5;
                // lakes render as dark teal, not deep ocean blue
                if (grid[y][x] === 0 && lake && lake[y * W + x]) {
                    r = 32; g = 110; b = 120;
                }
                // shallow coastal water: ocean touching land
                if (grid[y][x] === 0 && !(lake && lake[y * W + x])) {
                    const nearLand = (x > 0 && grid[y][x - 1] !== 0) || (x + 1 < W && grid[y][x + 1] !== 0)
                        || (y > 0 && grid[y - 1][x] !== 0) || (y + 1 < H && grid[y + 1][x] !== 0);
                    if (nearLand) { r = 38; g = 140; b = 200; }
                }
                // sandy beaches: land touching ocean (plains/desert)
                if (grid[y][x] !== 0 && hgt < 0.45) {
                    const nearSea = (x > 0 && grid[y][x - 1] === 0) || (x + 1 < W && grid[y][x + 1] === 0)
                        || (y > 0 && grid[y - 1][x] === 0) || (y + 1 < H && grid[y + 1][x] === 0);
                    if (nearSea && (grid[y][x] === 3 || grid[y][x] === 4 || grid[y][x] === 15)) {
                        r = 232; g = 210; b = 150;
                    }
                }
                // snow caps on very high land
                if (grid[y][x] !== 0 && hgt > 0.88 && grid[y][x] !== 10) {
                    r = 235; g = 238; b = 240;
                }
                // rivers: bright center (banks darkened below)
                if (world.rivers[y][x]) { r = 90; g = 160; b = 255; }
                // per-tile tone variation
                const t = tone(x, y);
                r *= t; g *= t; b *= t;
                // hillshade relief
                if (shade && grid[y][x] !== 0) {
                    const s = 1 + shade[y * W + x];
                    r *= s; g *= s; b *= s;
                }
                if (dim) { r = r * 0.3 + 12; g = g * 0.3 + 12; b = b * 0.3 + 12; }
                if (influence) {
                    const kid = influence[y][x];
                    const k = kid >= 0 && tick?.kingdoms?.[kid];
                    if (k) {
                        r = k.color[0] * 0.6 + r * 0.2;
                        g = k.color[1] * 0.6 + g * 0.2;
                        b = k.color[2] * 0.6 + b * 0.2;
                    }
                }
                img.data[i] = Math.max(0, Math.min(255, r));
                img.data[i + 1] = Math.max(0, Math.min(255, g));
                img.data[i + 2] = Math.max(0, Math.min(255, b));
                img.data[i + 3] = 255;
            }
        }
        // river banks: darken land hugging a river
        for (let y = 0; y < H; y++) {
            for (let x = 0; x < W; x++) {
                if (world.rivers[y][x]) continue;
                const nearRiver = (x > 0 && world.rivers[y][x - 1]) || (x + 1 < W && world.rivers[y][x + 1])
                    || (y > 0 && world.rivers[y - 1][x]) || (y + 1 < H && world.rivers[y + 1][x]);
                if (nearRiver) {
                    const i = (y * W + x) * 4;
                    img.data[i] *= 0.72; img.data[i + 1] *= 0.78; img.data[i + 2] *= 0.85;
                }
            }
        }
        // realm borders: darker edge where the neighbor tile belongs to another realm
        if (influence) {
            for (let y = 0; y < H; y++) {
                for (let x = 0; x < W; x++) {
                    const here = influence[y][x];
                    const right = x + 1 < W ? influence[y][x + 1] : here;
                    const down = y + 1 < H ? influence[y + 1][x] : here;
                    if (here !== right || here !== down) {
                        const i = (y * W + x) * 4;
                        img.data[i] *= 0.45; img.data[i + 1] *= 0.45; img.data[i + 2] *= 0.45;
                    }
                }
            }
        }
        ctx.putImageData(img, 0, 0);
        baseRef.current = cv;
        if (is_new_world) {
            viewRef.current = null;  // NEW world -> camera must re-fit
        }
    }, [world, mapMode, tick, state, W, H]);

    const draw = useCallback(() => {
        const canvas = canvasRef.current;
        if (!canvas || !baseRef.current || !W) return;
        const ctx = canvas.getContext('2d');
        // fresh world opens zoomed on the inhabited lands (not a tiny strip)
        const v = viewRef.current ?? fitInhabited(canvas);
        viewRef.current = v;
        const { x, y, scale } = v;
        // letterbox: deep slate with a subtle dot grid (not flat black)
        ctx.fillStyle = '#0a0e14';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#141b26';
        for (let gy = 8; gy < canvas.height; gy += 24) {
            for (let gx = 8; gx < canvas.width; gx += 24) {
                ctx.fillRect(gx, gy, 2, 2);
            }
        }
        // map frame - fixed to viewport, not to map content
        ctx.strokeStyle = '#30363d';
        ctx.lineWidth = 1;
        ctx.strokeRect(0.5, 0.5, canvas.width -1, canvas.height -1);

        ctx.imageSmoothingEnabled = false;
        // clip to viewport so zoomed map doesn't bleed outside fixed frame
        ctx.save();
        ctx.beginPath();
        ctx.rect(0, 0, canvas.width, canvas.height);
        ctx.clip();
        ctx.drawImage(baseRef.current, x, y, W * scale, H * scale);

        // live battle heatmap (cells with v>0) on top of the base
        if (tick?.battle_heat?.cells?.length && mapMode === 'battles') {
            const max = tick.battle_heat.max || 1;
            for (const c of tick.battle_heat.cells) {
                const n = c.v / max;
                ctx.fillStyle = `rgba(${Math.round(120 + n * 135)},${Math.round(80 * (1 - n) + 30)},${Math.round(60 * (1 - n) + 30)},${0.25 + n * 0.75})`;
                ctx.fillRect(x + c.x * scale, y + c.y * scale, scale, scale);
            }
        }

        if (tick) {
            const claimedBy = {};
            for (const c of claims) claimedBy[c.kingdom_id] = c.user_name;
            // territory cells (always in political; subtle in battles)
            const terrAlpha = mapMode === 'political' ? 0.35 : (mapMode === 'battles' ? 0.15 : 0.25);
            for (const k of tick.kingdoms) {
                const [r, g, b] = k.color;
                for (const s of k.sites) {
                    ctx.fillStyle = `rgba(${r},${g},${b},${terrAlpha})`;
                    ctx.fillRect(x + s.x * scale, y + s.y * scale, scale, scale);
                }
            }
            // site dots (+ crown for claimed dynasties)
            for (const k of tick.kingdoms) {
                const owner = claimedBy[k.id];
                for (const s of k.sites) {
                    const cx = x + (s.x + 0.5) * scale;
                    const cy = y + (s.y + 0.5) * scale;
                    const rad = s.is_capital ? Math.max(2, scale * 0.6) : Math.max(1, scale * 0.35);
                    const [r, g, b] = k.color;
                    ctx.beginPath();
                    ctx.arc(cx, cy, rad, 0, Math.PI * 2);
                    ctx.fillStyle = `rgb(${r},${g},${b})`;
                    ctx.fill();
                    ctx.strokeStyle = 'rgba(255,255,255,0.8)';
                    ctx.lineWidth = 1;
                    ctx.stroke();
                    if (s.is_capital && scale >= 3) {
                        // halo'd label for readability over any terrain
                        ctx.font = `${Math.max(8, scale * 2)}px monospace`;
                        ctx.strokeStyle = 'rgba(0,0,0,0.85)';
                        ctx.lineWidth = 3;
                        ctx.strokeText(k.name[0], cx - scale * 0.4, cy - scale * 0.8);
                        ctx.fillStyle = '#fff';
                        ctx.fillText(k.name[0], cx - scale * 0.4, cy - scale * 0.8);
                        if (owner) {
                            ctx.fillStyle = '#f0c674';
                            ctx.font = `${Math.max(9, scale * 2.4)}px monospace`;
                            ctx.fillText('👑', cx - scale * 0.4, cy + scale * 1.6);
                        }
                    } else if (owner && scale >= 3) {
                        // tiny crown dot on any site of a claimed realm
                        ctx.fillStyle = '#f0c674';
                        ctx.beginPath();
                        ctx.arc(cx, cy - rad - 1, Math.max(1, scale * 0.18), 0, Math.PI * 2);
                        ctx.fill();
                    }
                }
            }
            // beasts (alive: orange triangle)
            for (const beast of tick.beasts) {
                if (!beast.alive) continue;
                const bx = x + (beast.x + 0.5) * scale;
                const by = y + (beast.y + 0.5) * scale;
                ctx.fillStyle = '#ff7800';
                ctx.beginPath();
                ctx.moveTo(bx, by - scale * 0.5);
                ctx.lineTo(bx + scale * 0.5, by + scale * 0.4);
                ctx.lineTo(bx - scale * 0.5, by + scale * 0.4);
                ctx.closePath();
                ctx.fill();
            }
            // war fronts: dashed red line between capitals
            for (const w of tick.wars_ongoing) {
                const ka = tick.kingdoms.find(k => k.name === w.a);
                const kb = tick.kingdoms.find(k => k.name === w.b);
                if (!ka || !kb) continue;
                const ca = ka.sites[0], cb = kb.sites[0];
                if (!ca || !cb) continue;
                ctx.strokeStyle = 'rgba(255,60,60,0.6)';
                ctx.setLineDash([4, 3]);
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(x + (ca.x + 0.5) * scale, y + (ca.y + 0.5) * scale);
                ctx.lineTo(x + (cb.x + 0.5) * scale, y + (cb.y + 0.5) * scale);
                ctx.stroke();
                ctx.setLineDash([]);
            }
            ctx.restore();
        }
    }, [tick, W, H]);

    function fitView(canvas) {
        const sx = canvas.width / W, sy = canvas.height / H;
        const fit = Math.min(sx, sy);
        return {
            scale: fit,
            x: (canvas.width - W * fit) / 2,
            y: (canvas.height - H * fit) / 2,
        };
    }

    function fitInhabited(canvas) {
        // FIJO a pedido: siempre mundo completo, sin zoom al bbox habitado.
        // No crece el cuadrado con el contenido; escala fija contenida.
        return fitView(canvas);
    }

    function clampView(canvas, v) {
        // keep the map put: when zoomed in, never pan past the edges into
        // the void (small margin allowed); when zoomed out, keep it centered
        const mw = W * v.scale, mh = H * v.scale;
        const m = 48;  // px of slack at the edges
        if (mw >= canvas.width) {
            v.x = Math.max(canvas.width - mw - m, Math.min(m, v.x));
        } else {
            v.x = (canvas.width - mw) / 2;
        }
        if (mh >= canvas.height) {
            v.y = Math.max(canvas.height - mh - m, Math.min(m, v.y));
        } else {
            v.y = (canvas.height - mh) / 2;
        }
        return v;
    }

    // resize handling + initial fit (preserves world center on devtools open/close)
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ro = new ResizeObserver(() => {
            const oldW = canvas.width, oldH = canvas.height;
            const v = viewRef.current;
            // preserve world coordinate at viewport center
            const centerWorld = v ? { x: (oldW/2 - v.x)/v.scale, y: (oldH/2 - v.y)/v.scale } : null;
            canvas.width = canvas.clientWidth;
            canvas.height = canvas.clientHeight;
            if (v && centerWorld) {
                v.x = canvas.width/2 - centerWorld.x * v.scale;
                v.y = canvas.height/2 - centerWorld.y * v.scale;
                clampView(canvas, v);
            }
            draw();
        });
        ro.observe(canvas);
        canvas.width = canvas.clientWidth;
        canvas.height = canvas.clientHeight;
        draw();
        return () => ro.disconnect();
    }, [draw, W, H]);

    useEffect(() => { draw(); }, [tick, draw]);

    // follow-cam / war theater deshabilitados: mapa fijo no hace pan automático
    useEffect(() => {}, [tick, followId, draw]);
    useEffect(() => {}, [warFocus, draw]);

    // "F" re-fits the camera to the whole map (and breaks follow-cam)
    useEffect(() => {
        const onKey = (e) => {
            if (e.key === 'f' || e.key === 'F') {
                const canvas = canvasRef.current;
                if (canvas && W) {
                    if (onFollowClear) onFollowClear();
                    viewRef.current = fitView(canvas);
                    draw();
                }
            }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [draw]);

    // biome name for hover tooltip (IDs: 0-6, 8-16; 7 unused)
    const BIOME_LABELS = {
        0: 'OCEAN', 1: 'SWAMP', 2: 'MARSH', 3: 'GRASSLAND', 4: 'DESERT',
        5: 'CONIFER FOREST', 6: 'BROADLEAF FOREST', 7: 'WASTELAND', 8: 'BADLANDS', 9: 'MOUNTAIN',
        10: 'PEAK', 11: 'ICE', 12: 'ICE', 13: 'ICE', 14: 'SMALL HILLS',
        15: 'SHRUBLAND', 16: 'HILL',
    };
    const ERAS = ['Stone Age', 'Bronze', 'Iron', 'Classical', 'Medieval', 'Renaissance', 'Enlightenment'];
    const biomeName = (id) => vocab(lang, BIOME_LABELS[id] || '?');
    const eraName = (lvl) => vocab(lang, ERAS[lvl] ?? '?');

    const toGrid = (e) => {
        const canvas = canvasRef.current;
        const rect = canvas.getBoundingClientRect();
        const v = viewRef.current;
        if (!v || !W) return null;
        const gx = Math.floor((e.clientX - rect.left - v.x) / v.scale);
        const gy = Math.floor((e.clientY - rect.top - v.y) / v.scale);
        if (gx < 0 || gx >= W || gy < 0 || gy >= H) return null;
        return { gx, gy };
    };

    const onDown = (e) => {
        const g = toGrid(e);
        dragRef.current = {
            px: e.clientX, py: e.clientY,
            vx: viewRef.current.x, vy: viewRef.current.y,
            moved: false, gx: g?.gx, gy: g?.gy,
        };
    };
    const onMove = (e) => {
        if (dragRef.current) {
            const d = dragRef.current;
            if (Math.abs(e.clientX - d.px) + Math.abs(e.clientY - d.py) > 3) {
                d.moved = true;
            }
            // mapa fijo: sin pan — solo detectar click vs drag
            return;
        } else {
            const g = toGrid(e);
            if (g && world && tick) {
                const biome = biomeName(world.biome[g.gy][g.gx]);
                const river = world.rivers[g.gy][g.gx] ? ' · river' : '';
                let owner = '';
                for (const k of tick.kingdoms)
                    for (const s of k.sites)
                        if (s.x === g.gx && s.y === g.gy) {
                            const era = k.tech_level !== undefined ? ` · ${eraName(k.tech_level)}` : '';
                            owner = ` · ${k.name} (${s.name}${era})`;
                        }
                let beast = '';
                for (const b of tick.beasts)
                    if (b.alive && b.x === g.gx && b.y === g.gy) beast = ` · ${b.name}`;
                setHover(`(${g.gx},${g.gy}) ${biome}${river}${owner}${beast}`);
            } else setHover('');
        }
    };
    const onUp = (e) => {
        const d = dragRef.current;
        dragRef.current = null;
        if (!d || d.moved) return;
        // click (not drag): resolve what was hit, with tolerance
        if (tick && d.gx !== undefined) {
            const v = viewRef.current;
            const tol = Math.max(1, Math.ceil(3 / v.scale)); // ~3px tolerance in grid cells
            let hitSite = null, hitK = null;
            let bestDist = Infinity;
            for (const k of tick.kingdoms) {
                for (const s of k.sites) {
                    const dist = Math.abs(s.x - d.gx) + Math.abs(s.y - d.gy);
                    if (dist <= tol && dist < bestDist) {
                        bestDist = dist;
                        hitSite = s; hitK = k;
                    }
                }
            }
            if (hitSite && onSiteClick) { onSiteClick(hitK, hitSite); return; }
            // no site nearby: in realms mode, clicking claimed land selects the owner
            if (tick.influence && mapMode === 'realms') {
                const owner = tick.influence[d.gy]?.[d.gx];
                if (owner >= 0 && tick.kingdoms[owner] && onSiteClick) {
                    onSiteClick(tick.kingdoms[owner], tick.kingdoms[owner].sites[0]);
                    return;
                }
            }
            if (onBackgroundClick) onBackgroundClick();
        }
    };
    const onWheel = (e) => {
        e.preventDefault();
        // zoom deshabilitado a pedido: mapa fijo, sin escala
    };

    return (
        <div style={{ flex: 1, position: 'relative', minWidth: 0, minHeight: 0, height: '100%', overflow: 'hidden' }}>
            {/* map mode selector */}
            <div style={{ position: 'absolute', top: 6, left: 6, zIndex: 5, display: 'flex', gap: 4 }}>
                {['biome', 'realms', 'political', 'battles'].map(m => (
                    <button
                        key={m}
                        onClick={() => onMapMode && onMapMode(m)}
                        style={{
                            background: mapMode === m ? '#2f81f7' : '#161b22e0',
                            border: '1px solid #30363d',
                            color: mapMode === m ? '#fff' : '#8b949e',
                            borderRadius: 4,
                            padding: '2px 10px',
                            cursor: 'pointer',
                            fontSize: 12,
                            textTransform: 'capitalize',
                        }}
                    >{m}</button>
                ))}
            </div>
            <canvas
                ref={canvasRef}
                style={{ width: '100%', height: '100%', cursor: 'pointer', display: 'block' }}
                onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp}
                onMouseLeave={onUp} onWheel={onWheel}
            />
            {/* floating stats panel for the selected kingdom (always visible) */}
            {stats && (
                <StatsPanel
                    stats={stats}
                    govDesc={govDesc}
                    onClose={onClearSelection}
                    claimable={worldMode === 'league' && !claims.some(c => c.kingdom_id === stats.id)}
                    claimedBy={(claims.find(c => c.kingdom_id === stats.id) || {}).user_name}
                    onClaim={onClaim}
                    lang={lang}
                />
            )}
            {hover && (
                <div style={{ position: 'absolute', bottom: 4, left: 4, background: '#000c', padding: '2px 6px', fontSize: 11 }}>
                    {hover} · F = re-fit
                </div>
            )}
        </div>
    );
}
