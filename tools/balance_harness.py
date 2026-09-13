"""Balance harness: corre N mundos y reporta métricas para tuning.

Uso:
    uv run python tools/balance_harness.py --runs 20 --years 200

Métricas: guerras/siglo, edad media de reinos, población media, reinos vivos
"""
import argparse
import statistics
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from worldsim.data.loaders import load_governments, load_races
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

def run_once(seed, years, width=120, height=80):
    w = generate_world(WorldConfig(seed=seed, width=width, height=height))
    races = load_races(Path("data")/"Races.txt")
    govs = load_governments(Path("data")/"CivilizedGovernment.txt")
    e = SimEngine(w, races, govs, seed=seed)
    for _ in range(years*12):
        e.tick()
    wars = len([x for x in e.wars if x.start_month < years*12])
    alive = len([k for k in e.kingdoms if k.sites])
    avg_pop = sum(k.total_population for k in e.kingdoms)/max(1,len([k for k in e.kingdoms if k.sites]))
    avg_age = sum(k.years_since_founding for k in e.kingdoms)/max(1,len(e.kingdoms))
    return {"wars": wars, "alive": alive, "avg_pop": avg_pop, "avg_age": avg_age, "total_kingdoms": len(e.kingdoms)}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--years", type=int, default=200)
    args = p.parse_args()
    results = [run_once(i, args.years) for i in range(args.runs)]
    wars = [r["wars"] for r in results]
    pops = [r["avg_pop"] for r in results]
    print(f"Runs: {args.runs} x {args.years}y")
    print(f" Guerras/siglo: {statistics.mean(wars)/args.years*100:.1f} ± {statistics.stdev(wars) if len(wars)>1 else 0:.1f}")
    print(f" Población media: {statistics.mean(pops):.0f}")
    print(f" Reinos vivos: {statistics.mean([r['alive'] for r in results]):.1f}")

if __name__ == "__main__":
    main()
