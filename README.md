# worldsim

Simulador de mundos fantasy: generador procedural de planetas + civilizaciones emergentes + crónica histórica.

Rescate y modernización de [df-style-worldgen](https://github.com/Dozed12/df-style-worldgen) (Dozed12, MIT) —
reimplementado en Python 3.12 + numpy + [python-tcod](https://github.com/HexDecimal/python-tcod).

## Uso

```bash
uv sync                         # instalar dependencias
uv run worldsim --help          # CLI
uv run worldsim generate --seed 42 --export world.json   # generar mundo batch
uv run worldsim run --seed 42 --ticks 500                # simulación headless
uv run python clients/tcod_app/main.py                  # cliente interactivo (tcod)
```

## Créditos

- Algoritmos base: **Dozed12/df-style-worldgen** (MIT)
- Datos de razas/gobiernos/biomas: adaptados de los archivos originales del repo
- Nombres fantasy: syllable config `jice_fantasy.cfg` (Jice / libtcod)
- Tileset: Andux CP866-ish (distribuido con el proyecto original)
