import React, { useRef, useEffect, useState, useCallback } from 'react';
import { biomeColor } from '../biomes.js';

/**
 * Canvas world map with pan/zoom + political overlay + site markers.
 * Re-fits the camera on every world load; survives canvas resizes.
 */
export default function WorldMap({ state, tick, mode = 'biome', mapMode = 'biome', onSiteClick, onBackgroundClick, onMapMode }) {    const canvasRef = useRef(null);
    const viewRef = useRef(null);   // null => needs fit
    const dragRef = useRef(null);
    const [hover, setHover] = useState('');

    const world = state?.world;
    const W = world?.width ?? 0;
    const H = world?.height ?? 0;

    // paint the biome base into an offscreen canvas once per state/mode
    const baseRef = useRef(null);
    const baseKeyRef = useRef('');
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
        const dim = mapMode === 'political' || mapMode === 'realms';
        const influence = mapMode === 'realms' ? tick?.influence : null;
        for (let y = 0; y < H; y++) {
            for (let x = 0; x < W; x++) {
                const c = biomeColor(grid[y][x]);
                const i = (y * W + x) * 4;
                let [r, g, b] = c;
                if (world.rivers[y][x]) { r = 90; g = 160; b = 255; }
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
                img.data[i] = r; img.data[i + 1] = g; img.data[i + 2] = b; img.data[i + 3] = 255;
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
        const v = viewRef.current ?? fitView(canvas);
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
        // map frame
        ctx.strokeStyle = '#30363d';
        ctx.lineWidth = 2;
        ctx.strokeRect(x - 1, y - 1, W * scale + 2, H * scale + 2);

        ctx.imageSmoothingEnabled = false;
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
            // territory cells (always in political; subtle in battles)
            const terrAlpha = mapMode === 'political' ? 0.35 : (mapMode === 'battles' ? 0.15 : 0.25);
            for (const k of tick.kingdoms) {
                const [r, g, b] = k.color;
                for (const s of k.sites) {
                    ctx.fillStyle = `rgba(${r},${g},${b},${terrAlpha})`;
                    ctx.fillRect(x + s.x * scale, y + s.y * scale, scale, scale);
                }
            }
            // site dots
            for (const k of tick.kingdoms) {
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

    // resize handling + initial fit
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ro = new ResizeObserver(() => {
            canvas.width = canvas.clientWidth;
            canvas.height = canvas.clientHeight;
            draw();
        });
        ro.observe(canvas);
        canvas.width = canvas.clientWidth;
        canvas.height = canvas.clientHeight;
        draw();
        return () => ro.disconnect();
    }, [draw, W, H]);

    useEffect(() => { draw(); }, [tick, draw]);

    // "F" re-fits the camera
    useEffect(() => {
        const onKey = (e) => {
            if (e.key === 'f' || e.key === 'F') {
                viewRef.current = null;
                draw();
            }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [draw]);

    // biome name for hover tooltip (IDs: 0-6, 8-16; 7 unused)
    const BIOME_LABELS = {
        0: 'OCEAN', 1: 'SWAMP', 2: 'MARSH', 3: 'GRASSLAND', 4: 'DESERT',
        5: 'CONIFER FOREST', 6: 'BROADLEAF FOREST', 8: 'BADLANDS', 9: 'MOUNTAIN',
        10: 'PEAK', 11: 'ICE', 12: 'ICE', 13: 'ICE', 14: 'SMALL HILLS',
        15: 'SHRUBLAND', 16: 'HILL',
    };
    const ERAS = ['Stone Age', 'Bronze', 'Iron', 'Classical', 'Medieval'];
    const biomeName = (id) => BIOME_LABELS[id] || '?';

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
            if (Math.abs(e.clientX - d.px) + Math.abs(e.clientY - d.py) > 3) d.moved = true;
            viewRef.current.x = d.vx + (e.clientX - d.px);
            viewRef.current.y = d.vy + (e.clientY - d.py);
            draw();
        } else {
            const g = toGrid(e);
            if (g && world && tick) {
                const biome = biomeName(world.biome[g.gy][g.gx]);
                const river = world.rivers[g.gy][g.gx] ? ' · river' : '';
                let owner = '';
                for (const k of tick.kingdoms)
                    for (const s of k.sites)
                        if (s.x === g.gx && s.y === g.gy) {
                            const era = k.tech_level !== undefined ? ` · ${ERAS[k.tech_level]}` : '';
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
        // click (not drag): did we hit a site?
        if (tick && d.gx !== undefined) {
            const hit = (() => {
                for (const k of tick.kingdoms)
                    for (const s of k.sites)
                        if (s.x === d.gx && s.y === d.gy) return { k, s };
                return null;
            })();
            if (hit && onSiteClick) { onSiteClick(hit.k, hit.s); return; }
            if (onBackgroundClick) onBackgroundClick();
        }
    };
    const onWheel = (e) => {
        e.preventDefault();
        const canvas = canvasRef.current;
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left, my = e.clientY - rect.top;
        const v = viewRef.current;
        const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
        const ns = Math.max(1, Math.min(32, v.scale * factor));
        v.x = mx - ((mx - v.x) * ns) / v.scale;
        v.y = my - ((my - v.y) * ns) / v.scale;
        v.scale = ns;
        draw();
    };

    return (
        <div style={{ flex: 1, position: 'relative', minWidth: 0 }}>
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
                style={{ width: '100%', height: '100%', cursor: 'grab', display: 'block' }}
                onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp}
                onMouseLeave={onUp} onWheel={onWheel}
            />
            {hover && (
                <div style={{ position: 'absolute', bottom: 4, left: 4, background: '#000c', padding: '2px 6px', fontSize: 11 }}>
                    {hover} · F = re-fit
                </div>
            )}
        </div>
    );
}
