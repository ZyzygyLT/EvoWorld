"""CLI: worldsim generate / run / summary."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_DATA = Path(__file__).resolve().parents[2] / "data"


def _find_data() -> Path:
    """Locate the data dir whether running from source or installed."""
    if (_DATA / "Races.txt").exists():
        return _DATA
    # wheel-installed layout fallback
    alt = Path.cwd() / "data"
    if (alt / "Races.txt").exists():
        return alt
    raise FileNotFoundError("Could not locate data/ directory (Races.txt)")


def cmd_generate(args: argparse.Namespace) -> int:
    from worldsim.export.png import save_png, world_to_json, world_to_rgb
    from worldsim.worldgen import WorldConfig
    from worldsim.worldgen.pipeline import generate_world

    cfg = WorldConfig(seed=args.seed, width=args.width, height=args.height)
    world = generate_world(cfg, verbose=True)

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    png_path = save_png(world_to_rgb(world, args.mode), out / f"world_{args.mode}.png")
    json_path = world_to_json(world, out / "world.json")
    print(f"PNG  -> {png_path}")
    print(f"JSON -> {json_path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from worldsim.data.loaders import load_governments, load_races
    from worldsim.sim.engine import SimEngine
    from worldsim.worldgen import WorldConfig
    from worldsim.worldgen.pipeline import generate_world

    data = _find_data()
    cfg = WorldConfig(seed=args.seed, width=args.width, height=args.height)
    world = generate_world(cfg, verbose=True)
    races = load_races(data / "Races.txt")
    govs = load_governments(data / "CivilizedGovernment.txt")
    engine = SimEngine(world, races, govs, seed=args.seed)

    report_every = max(1, args.ticks // 10)
    for t in range(1, args.ticks + 1):
        engine.tick()
        if t % report_every == 0 or t == args.ticks:
            print(f"--- tick {t} (month {engine.month}) ---")
            print(engine.summary())

    if args.chronicle:
        out = Path(args.chronicle)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"[month {e.month}] {e.text}" for e in engine.chronicle.events]
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"Chronicle -> {out} ({len(engine.chronicle.events)} events)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="worldsim", description="Fantasy world simulator")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="Generate a world and export maps")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--width", type=int, default=200)
    g.add_argument("--height", type=int, default=80)
    modes = ["biome", "height", "temperature", "precipitation", "drainage", "prosperity"]
    g.add_argument("--mode", choices=modes, default="biome")
    g.add_argument("--outdir", default="outputs")
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("run", help="Run headless simulation")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--width", type=int, default=200)
    r.add_argument("--height", type=int, default=80)
    r.add_argument("--ticks", type=int, default=240)
    r.add_argument("--chronicle", default=None, help="Write chronicle log to file")
    r.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
