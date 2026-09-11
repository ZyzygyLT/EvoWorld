"""tcod interactive client — map modes, chronicle sidebar, kingdom inspector.

Run:  uv run python clients/tcod_app/main.py [--seed N]
Keys: r new world | space pause/run | 1-8 map modes | +/- speed
      mouse hover: tile info | ESC: quit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import tcod

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from worldsim.data.loaders import load_governments, load_races  # noqa: E402
from worldsim.sim.engine import SimEngine  # noqa: E402
from worldsim.worldgen import WorldConfig  # noqa: E402
from worldsim.worldgen.biomes import Biome  # noqa: E402
from worldsim.worldgen.pipeline import generate_world  # noqa: E402

W, H = 140, 70  # screen (console) size
MAP_W, MAP_H = 110, 70  # map panel
SIDEBAR_W = W - MAP_W  # chronicle sidebar

FONT = Path(__file__).resolve().parents[2] / "data" / "fonts" / "Andux_cp866ish.png"
DATA = Path(__file__).resolve().parents[2] / "data"

# tcod colors per biome (port of the original ColorDictionary)
COLORS = {
    Biome.OCEAN: (13, 103, 196),
    Biome.SWAMP: (68, 158, 53),
    Biome.MARSH: (131, 212, 82),
    Biome.GRASSLAND: (131, 212, 82),
    Biome.DESERT: (255, 218, 90),
    Biome.CONIFER_FOREST: (68, 158, 53),
    Biome.BROADLEAF_FOREST: (34, 120, 42),
    Biome.BADLANDS: (204, 159, 81),
    Biome.MOUNTAIN: (185, 192, 162),
    Biome.PEAK: (230, 230, 230),
    Biome.ICE: (176, 223, 215),
    Biome.SMALL_HILLS: (150, 190, 90),
    Biome.SHRUBLAND: (150, 190, 90),
    Biome.HILL: (150, 190, 90),
}

MODES = [
    "biome",
    "political",
    "height",
    "temperature",
    "precipitation",
    "drainage",
    "prosperity",
    "rivers",
]


def build_engine(seed: int) -> SimEngine:
    cfg = WorldConfig(seed=seed, width=MAP_W, height=MAP_H)
    world = generate_world(cfg)
    races = load_races(DATA / "Races.txt")
    govs = load_governments(DATA / "CivilizedGovernment.txt")
    return SimEngine(world, races, govs, seed=seed)


def draw_map(console: tcod.console.Console, engine: SimEngine, mode: str) -> None:
    w = engine.world
    world_w, world_h = w.width, w.height_tiles

    if mode == "biome":
        ch = np.full((world_h, world_w), 250, dtype=np.int32)  # '·' dot
        fg = np.zeros((world_h, world_w, 3), dtype=np.uint8)
        for b, color in COLORS.items():
            mask = w.biome == int(b)
            fg[mask] = color
        bg = fg // 2
        sub = console.rgb[:world_h, :world_w]
        sub["ch"] = ch
        sub["fg"] = fg
        sub["bg"] = bg
        sub[w.rivers]["ch"] = 247  # '≈'
        sub[w.rivers]["fg"] = (120, 180, 255)

    elif mode == "political":
        ch = np.full((world_h, world_w), ord("#"), dtype=np.int32)
        fg = np.zeros((world_h, world_w, 3), dtype=np.uint8)
        bg = np.zeros((world_h, world_w, 3), dtype=np.uint8)
        for b, color in COLORS.items():
            mask = w.biome == int(b)
            bg[mask] = tuple(c // 4 for c in color)  # type: ignore[index]
        terr = engine.territory_map()
        for k in engine.kingdoms:
            mask = terr == k.id
            bg[mask] = k.color
            fg[mask] = (255, 255, 255)
        sub = console.rgb[:world_h, :world_w]
        sub["ch"] = ch
        sub["fg"] = fg
        sub["bg"] = bg
        # sites as letters (over the base)
        for k in engine.kingdoms:
            for s in k.sites:
                glyph = "A" if s.is_capital else "o"
                console.rgb[s.y, s.x]["ch"] = ord(glyph)
                console.rgb[s.y, s.x]["fg"] = (255, 255, 255)
                console.rgb[s.y, s.x]["bg"] = k.color

    else:  # gradient modes
        if mode == "height":
            layer = w.height
            hot, cold = (255, 255, 255), (20, 20, 40)
        elif mode == "temperature":
            layer = w.temperature
            hot, cold = (255, 60, 40), (230, 230, 255)
        elif mode == "precipitation":
            layer = w.precipitation
            hot, cold = (60, 120, 255), (245, 245, 245)
        elif mode == "drainage":
            layer = w.drainage
            hot, cold = (160, 110, 60), (245, 245, 245)
        else:  # prosperity / rivers share the prosperity gradient base
            layer = w.prosperity
            hot, cold = (60, 200, 90), (245, 245, 245)
        fg = np.zeros((world_h, world_w, 3), dtype=np.uint8)
        for c in range(3):
            fg[..., c] = (
                np.array(cold[c]) * (1 - layer) + np.array(hot[c]) * layer
            ).astype(np.uint8)
        ch = np.full((world_h, world_w), 250, dtype=np.int32)
        sub = console.rgb[:world_h, :world_w]
        sub["ch"] = ch
        sub["fg"] = fg
        sub["bg"] = np.zeros_like(fg)
        if mode == "rivers" or mode == "height":
            sub[w.rivers]["ch"] = 247
            sub[w.rivers]["fg"] = (120, 180, 255)


def draw_sidebar(console: tcod.console.Console, engine: SimEngine) -> None:
    x0 = MAP_W + 1
    console.draw_frame(x0 - 1, 0, SIDEBAR_W, H)
    console.print(x0 + 1, 0, "Chronicle", fg=(255, 255, 255))
    y = 2
    console.print(x0, y, f"Year {engine.month // 12}  Month {engine.month}")
    y += 1
    for k in engine.kingdoms:
        if y >= H - 1:
            break
        color = tuple(k.color)
        mark = "!" if k.at_war else " "
        line = f"{mark}{k.name[:20]:<20} pop={k.total_population:>7} {len(k.sites)}s"
        console.print(x0, y, line, fg=color)
        y += 1
    y += 1
    console.print(x0, y, "-- recent events --")
    y += 1
    for ev in engine.chronicle.recent(60):
        if y >= H - 1:
            break
        txt = f"[y{ev.month // 12}] {ev.text}"[: SIDEBAR_W - 3]
        console.print(x0, y, txt, fg=(200, 200, 200))
        y += 1


def draw_status(
    console: tcod.console.Console, mode: str, speed: int, paused: bool
) -> None:
    console.print(1, 0, f"[{mode}]{' PAUSED' if paused else f' x{speed}'}", fg=(255, 255, 0))


def tile_info(engine: SimEngine, mx: int, my: int) -> str:
    w = engine.world
    if not (0 <= mx < w.width and 0 <= my < w.height_tiles):
        return ""
    parts = [
        f"({mx},{my}) {Biome(w.biome[my, mx]).name}",
        f"h={w.height[my, mx]:.2f} t={w.temperature[my, mx]:.2f}",
        f"p={w.precipitation[my, mx]:.2f} d={w.drainage[my, mx]:.2f}",
    ]
    terr = engine.territory_map()
    if terr[my, mx] >= 0:
        k = engine.kingdoms[terr[my, mx]]
        parts.append(f"{k.name} ({k.race.name})")
    if w.rivers[my, mx]:
        parts.append("river")
    return "  ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    # Andux_cp866ish.png: 128x192 px = 16x16 grid of 8x12 tiles (ASCII in-row)
    tileset = tcod.tileset.load_tilesheet(FONT, 16, 16, list(range(256)))
    with tcod.context.new(
        columns=W, rows=H, tileset=tileset, title="worldsim"
    ) as context:
        console = context.new_console()

        engine = build_engine(args.seed)
        mode_idx = 0
        speed = 1
        paused = True
        hover = ""

        while True:
            console.clear()
            draw_map(console, engine, MODES[mode_idx])
            draw_sidebar(console, engine)
            draw_status(console, MODES[mode_idx], speed, paused)
            if hover:
                console.print(1, H - 1, hover[: MAP_W - 2], fg=(255, 255, 255))
            context.present(console)

            # advance simulation according to speed
            if not paused:
                for _ in range(speed):
                    engine.tick()

            # input: drain all pending events
            for ev in tcod.event.get():
                context.convert_event(ev)
                if isinstance(ev, tcod.event.Quit):
                    raise SystemExit
                if isinstance(ev, tcod.event.KeyDown):
                    if ev.sym == tcod.event.KeySym.ESCAPE:
                        raise SystemExit
                    elif ev.sym == tcod.event.KeySym.SPACE:
                        paused = not paused
                    elif ev.sym == tcod.event.KeySym.r:
                        seed = int(np.random.default_rng().integers(0, 99999))
                        engine = build_engine(seed)
                    elif ev.sym in (
                        tcod.event.KeySym.MINUS,
                        tcod.event.KeySym.KP_MINUS,
                    ):
                        speed = max(1, speed // 2)
                    elif ev.sym in (
                        tcod.event.KeySym.EQUAL,
                        tcod.event.KeySym.KP_PLUS,
                    ):
                        speed = min(32, speed * 2)
                    else:
                        digits = {
                            tcod.event.KeySym.N1: 0,
                            tcod.event.KeySym.N2: 1,
                            tcod.event.KeySym.N3: 2,
                            tcod.event.KeySym.N4: 3,
                            tcod.event.KeySym.N5: 4,
                            tcod.event.KeySym.N6: 5,
                            tcod.event.KeySym.N7: 6,
                            tcod.event.KeySym.N8: 7,
                        }
                        if ev.sym in digits:
                            mode_idx = digits[ev.sym]
                if isinstance(ev, tcod.event.MouseMotion):
                    if ev.tile is not None:
                        hover = tile_info(engine, ev.tile.x, ev.tile.y)


if __name__ == "__main__":
    main()
