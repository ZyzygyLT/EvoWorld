# worldsim

Simulador de mundos fantasy: generador procedural de planetas + civilizaciones emergentes + crónica histórica.

Rescate y modernización de [df-style-worldgen](https://github.com/Dozed12/df-style-worldgen) (Dozed12, MIT) —
reimplementado en Python 3.12 + numpy, con servidor FastAPI y cliente web React.

## Arquitectura

```
worldsim/
├── src/worldsim/
│   ├── worldgen/     # generación procedural: heightmap fBm, polos, tectónica,
│   │                 # erosión, clima, 14 biomas, ríos, prosperidad (numpy, seeds)
│   ├── sim/          # motor headless: reinos, eras tech, líderes, héroes, bestias,
│   │                 # guerras nombradas, rebeliones, colonias, pestes, crónica
│   ├── data/         # parsers de los .txt originales (razas, gobiernos) + namegen
│   ├── export/       # PNG/JSON + snapshot completo del estado
│   ├── server/       # FastAPI: REST + WebSocket en vivo + persistencia
│   └── cli.py        # worldsim generate / run (batch)
├── clients/tcod_app/ # cliente terminal interactivo (tcod)
├── web/              # cliente React + Canvas (mapa, stats, crónica)
└── tests/            # 61 tests
```

## Uso rápido

```bash
uv sync --extra dev --extra png --extra server   # dependencias

# CLI batch
uv run worldsim generate --seed 42 --outdir outputs   # mundo -> PNG/JSON
uv run worldsim run --seed 7 --ticks 500 --export outputs/state.json

# Cliente terminal
uv run python clients/tcod_app/main.py

# Servidor + cliente web
uv run uvicorn worldsim.server.app:app --port 8420
cd web && npm install && npm run start    # -> http://localhost:5173
```

El web: `+ new world` crea el planeta (empieza en **Edad de Piedra**, pausado).
▶ run / velocidades x1-x48 · modos de mapa: **Biome / Realms** (heatmap de reinos con
provincias) / Political / Battles (heatmap de muertes) · click en un reino o sitio →
**panel de estadísticas flotante** (era, poder militar, guerras W/L, diplomacia,
gráfico de población, historia propia) · crónica con filtros ★⚔♛▲✦ · F re-encuadra.

## Simulación (tick = 1 mes)

- **Eras**: Piedra → Bronce → Hierro → Clásica → Medieval. La tribus se civilizan
  al llegar a Hierro (adoptan gobierno real). Cada era: +25% popcap, +15% poder
  militar, +2 radio de influencia. Post-Medieval: crecimiento urbano +1%/año.
- **Líderes**: reinados ~40 años con sucesión; **campeones** emergen en guerra (×1.5-3
  poder) y pueden caer en batalla.
- **Guerras nombradas** por el sitio en disputa ("The War of Kananeo"), con batallas,
  capturas/razeos, alianzas cobeligerantes, white peace.
- **Rebeliones**: reinos grandes bajo presión se escinden (cap 12 reinos).
- **Colonias**: reinos maduros fundan asentamientos lejanos; los muy lejanos
  declaran independencia pacífica como reinos nuevos.
- **Pestes** (reinos densos) y **Edades Doradas** (×1.5 popcap).
- **Bestias**: gigantes con territorio (bloquean expansión), raidean y son cazados.
- **Milestones** únicos: ★ primera ciudad, primera batalla, primer imperio, 10k
  almas, siglo de historia...

## Persistencia

SQLite por defecto. Para PostgreSQL:
```powershell
$env:WORLDSIM_DB="postgresql+psycopg://user:pass@host/worldsim"
```

## API

| Endpoint | Descripción |
|---|---|
| `GET /api/meta` | gobiernos (con descripciones/stats) y razas |
| `GET/POST /api/worlds` | listar / crear mundos (con meses pre-simulados) |
| `GET/DELETE /api/worlds/{id}` | snapshot completo / borrar |
| `GET /api/worlds/{id}/stats/{kid}` | estadísticas ricas de un reino |
| `WS /api/ws/{id}` | stream en vivo: `{speed, pause, selected}` |

## Créditos

- Algoritmos base: **Dozed12/df-style-worldgen** (MIT)
- Datos de razas/gobiernos/biomas: adaptados de los archivos originales
- Nombres: syllable config `jice_fantasy.cfg` (Jice / libtcod)
- Tileset terminal: Andux CP866-ish (proyecto original)
