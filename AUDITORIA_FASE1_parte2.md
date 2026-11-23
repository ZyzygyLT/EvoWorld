# EvoWorld — Auditoría Técnica Fase 1 (parte 2 de 3)

Continuación del diseño técnico recomendado y secciones restantes.

---

## E. Diseño Técnico Recomendado (continuación)

### Componente 5: Recuperación automática (continuación)

Al iniciar el servidor:
1. Listar todos los mundos en DB con `status != stopped`.
2. Para cada mundo, buscar el checkpoint más reciente válido (checksum verificado).
3. Si el último estado conocido (en `WorldRow.state_json`) es más antiguo que el checkpoint, restaurar desde el checkpoint.
4. Marcar mundos que tenían un tick "in_progress" como `error` y restaurar desde el checkpoint previo al fallo.
5. Reanudar la simulación automáticamente si `status == running` y no hay cliente conectado (el mundo evoluciona aunque no haya espectadores).

### Componente 6: API de control de ciclo de vida

Endpoints a añadir (ver sección H):
- `POST /api/worlds/{id}/checkpoint` — forzar guardado inmediato.
- `POST /api/worlds/{id}/pause` / `POST /api/worlds/{id}/resume` — controlar evolución.
- `POST /api/worlds/{id}/cancel` — detener y marcar como `stopped`.
- `GET /api/worlds/{id}/checkpoints` — listar checkpoints disponibles.
- `POST /api/worlds/{id}/restore` — restaurar desde un checkpoint específico.

### Estrategia de serialización híbrida

| Capa | Formato | Uso |
|---|---|---|
| API cliente (REST/WS) | JSON vía `engine_to_dict` | Vista ligera para el cliente React/Canvas. No pretende ser punto de restauración. |
| Checkpoint de recuperación | msgpack (o pickle con versión) | Estado completo del engine: RNG, contadores, mapas, crónica, cachés invalidadas. |
| Metadatos y scoring | SQL (PostgreSQL/SQLite) | Mundos, claims, predicciones, chat, usuarios, índices de checkpoints. |

¿Por qué no confiar solo en `engine_to_dict`?
- No serializa `np.random.Generator` state → imposible restaurar la secuencia aleatoria exacta.
- No serializa contadores internos (`_next_war_id`, etc.) → IDs colisionan al restaurar.
- No serializa `_occupancy`, `_battle_heat` completo, campos privados de `Kingdom`, `Leader.expected_death_month`, `Hero.battle_deeds` lista, `Notable` objetos, `War.cobelligerents`.
- Conclusión demostrada revisando el código: `engine_to_dict` es **insuficiente** para recuperación exacta. Debe complementarse, no reemplazarse.

---

## F. Estructura de Carpetas Propuesta

```
src/worldsim/
├── __init__.py
├── cli.py
├── data/
│   ├── Races.txt
│   ├── CivilizedGovernment.txt
│   └── namegen/
├── export/
│   ├── __init__.py
│   ├── state.py          # engine_to_dict (vista cliente) — SIN CAMBIOS estructurales
│   ├── checkpoint.py     # NUEVO: serialización completa para recuperación (msgpack/pickle)
│   └── png.py
├── i18n.py
├── sim/
│   ├── __init__.py
│   ├── engine.py         # SimEngine — añadir métodos to_checkpoint_state / from_checkpoint_state
│   ├── models.py         # dataclasses — añadir campos serializables faltantes
│   ├── scoring.py
│   └── tech.py
├── server/
│   ├── __init__.py
│   ├── app.py            # FastAPI — nuevos endpoints checkpoint/pause/resume/cancel
│   ├── auth.py
│   ├── db.py             # SQLAlchemy — nueva tabla checkpoints, migración estado_json
│   ├── league.py
│   ├── prediction_resolver.py
│   ├── runner.py         # WorldRunner — soporte multi-mundo, checkpoints periódicos, recuperación
│   └── worker.py         # NUEVO: worker de simulación independiente (opcional, con ARQ/Redis)
├── worldgen/
│   ├── __init__.py
│   ├── pipeline.py
│   └── ...
tests/
├── test_checkpoint.py    # NUEVO: determinismo y recuperación
├── test_recovery.py      # NUEVO: fallo durante tick y restauración
├── ...
```

---

## G. Modelos de Datos y Migraciones Necesarias

### Nueva tabla: `checkpoints`

```sql
CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id INTEGER NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    month INTEGER NOT NULL,
    checkpoint_type TEXT NOT NULL,        -- 'manual' | 'periodic' | 'pre_tick' | 'recovery'
    payload BLOB NOT NULL,                -- serializado (msgpack/pickle)
    checksum TEXT NOT NULL,               -- SHA-256 hex del payload
    created_at TEXT NOT NULL,
    is_valid INTEGER DEFAULT 1,
    UNIQUE(world_id, sequence)
);
CREATE INDEX idx_checkpoints_world_seq ON checkpoints(world_id, sequence DESC);
CREATE INDEX idx_checkpoints_world_month ON checkpoints(world_id, month);
```

### Modificaciones a `worlds`

Añadir columnas:
- `status TEXT DEFAULT 'running'` — `running | paused | stopped | error`
- `last_checkpoint_at TEXT` — timestamp del último checkpoint válido
- `checkpoint_count INTEGER DEFAULT 0`

Migración SQLite (`ensure_schema` en `db.py`): usar `ALTER TABLE worlds ADD COLUMN ...` idempotente, igual que ya se hace para otras columnas.

### Migración de snapshots anteriores

Mundos existentes solo tienen `state_json` (sin estado de RNG ni contadores internos). Estrategia:
1. Al abrir un mundo existente sin checkpoint válido, reconstruir el engine desde `state_json` usando `_build_engine` + re-ejecución de ticks (como hace hoy `get_kingdom_stats`).
2. Al primer tick tras la recuperación, guardar un checkpoint completo que incluya el estado del RNG capturado en ese momento.
3. Marcar el mundo como "recovered" para que el cliente sepa que la continuidad exacta desde el inicio no está garantizada (divergencia posible respecto a una ejecución ininterrumpida original).

---

## H. APIs que Habría que Modificar o Crear

### Modificar
- `POST /api/worlds` — tras crear, guardar un checkpoint inicial completo además del `state_json`.
- `GET /api/worlds/{id}` — opcionalmente devolver metadatos del último checkpoint (`last_checkpoint_at`, `checkpoint_count`).
- `WebSocket /api/ws/{world_id}` — al conectar, restaurar desde checkpoint si `state_json` está desactualizado respecto al checkpoint.

### Crear
- `POST /api/worlds/{world_id}/checkpoint` — forzado inmediato, devuelve ID del checkpoint.
- `GET /api/worlds/{world_id}/checkpoints` — lista paginada de checkpoints (id, sequence, month, type, created_at, valid).
- `POST /api/worlds/{world_id}/pause` — establece `paused = true` en DB y runner.
- `POST /api/worlds/{world_id}/resume` — establece `paused = false`, reanuda worker.
- `POST /api/worlds/{world_id}/cancel` — `status = stopped`, detiene worker.
- `POST /api/worlds/{world_id}/restore` — recibe `checkpoint_id`, restaura el motor desde ese punto y marca como punto de continuación.
- `GET /api/worlds/{world_id}/status` — devuelve estado del mundo (`running`, `paused`, `stopped`, `error`, mes actual).

---

## I. Estrategia de Checkpoints y Recuperación

### Tipos de checkpoint
1. **Periódico:** cada N segundos o cada M ticks (configurable por mundo).
2. **Pre-tick:** inmediatamente antes de ejecutar un tick, para permitir rollback si falla.
3. **Manual:** disparado por el usuario vía API.
4. **Recovery:** generado automáticamente tras restaurar desde un checkpoint previo.

### Retención
- Mantener los últimos K checkpoints por mundo (ej. K=10), borrando los más antiguos.
- Mantener al menos uno por día si el mundo estuvo activo, para permitir "viaje en el tiempo" reciente.

### Detección de corrupción
- Cada payload lleva `checksum` SHA-256. Al leer, verificar checksum antes de deserializar.
- Si checksum falla, descartar ese checkpoint e intentar el anterior inmediato.
- Si todos los checkpoints de un mundo están corruptos, marcar mundo como `error` y notificar al usuario.

### Recuperación automática
- Al iniciar el servidor, escanear mundos con `status != stopped`.
- Si el mundo tiene un checkpoint más reciente que `state_json`, restaurar desde checkpoint.
- Si un worker falló mid-tick (estado `error`), restaurar desde el último pre-tick checkpoint y reintentar.

### Tamaño de snapshots
- Para evitar snapshots excesivamente grandes: almacenar la crónica completa solo en checkpoints periódicos espaciados; en checkpoints pre-tick, almacenar solo deltas o excluir crónica antigua.
- Opcional: comprimir payload (gzip/lz4) antes de guardar en BLOB.

---

## J. Plan de Pruebas y Criterios de Aceptación

### Pruebas de determinismo y recuperación

1. **Crear un mundo** — `POST /api/worlds` con `months=0`, verificar que se crea en DB y devuelve estado.
2. **Simular meses** — avanzar el runner N ticks, verificar `month` incrementa.
3. **Guardar checkpoint** — `POST /api/worlds/{id}/checkpoint`, verificar que existe en tabla `checkpoints`.
4. **Detener el proceso** — simular reinicio (detener worker/runner).
5. **Recuperar el mundo** — al reiniciar, verificar que el mundo se restaura desde el checkpoint y el mes coincide.
6. **Continuar la simulación** — reanudar runner, verificar que los ticks continúan desde el mes restaurado.
7. **Comparar con ejecución continua** — ejecutar dos mundos con la misma semilla: uno ininterrumpido, otro con paradas y recuperaciones; al mes M, los estados deben ser idénticos (poblaciones, sitios, guerras, crónica).
8. **Varios mundos simultáneamente** — crear 3 mundos con semillas diferentes, ejecutar en paralelo, verificar que no hay interferencia ni duplicación de IDs.
9. **Recuperarse de fallo durante ejecución** — matar el worker a mitad de un tick, verificar que al reiniciar se restaura desde el pre-tick checkpoint y el mundo continúa sin corrupción.
10. **Evitar duplicaciones de eventos y puntuaciones** — ejecutar `score_new_events` dos veces sobre el mismo rango, verificar que los puntos no se duplican (usar tabla de eventos ya puntuados o cursor atómico).

### Criterios de aceptación

- Dos ejecuciones con la misma semilla y misma secuencia de paradas/recuperaciones producen crónicas e estados idénticos.
- Un checkpoint restaurado permite continuar sin pérdida de mes ni eventos.
- Un worker caído durante un tick no deja el mundo en estado inconsistente.
- No hay puntuación duplicada en la liga tras múltiples scorings del mismo rango.
- El servidor puede reiniciarse y los mundos en curso recuperan su progreso hasta el último checkpoint.

---

## K. Implementación Dividida en Tareas Pequeñas y Ordenadas

1. **Auditoría y documentación** (esta fase) — completar este informe.
2. **Añadir campos serializables a `models.py`** — asegurar que `Kingdom`, `Leader`, `Hero`, `Notable`, `Beast`, `War`, `Site` expongan todos los campos necesarios (incluidos privados como `_last_disaster_month`).
3. **Crear `export/checkpoint.py`** — funciones `checkpoint_to_dict(engine)` y `engine_from_checkpoint(state)` con msgpack/pickle, incluyendo RNG state, contadores, mapas, crónica completa.
4. **Añadir métodos a `SimEngine`** — `to_checkpoint_state()` y `@classmethod from_checkpoint_state(state)`.
5. **Crear tabla `checkpoints` en `db.py`** — modelo SQLAlchemy + migración `ensure_schema`.
6. **Modificar `app.py` `create_world`** — guardar checkpoint inicial tras crear el mundo.
7. **Añadir tarea de checkpoint periódico en `runner.py`** — cada N segundos/ticks, guardar checkpoint.
8. **Añadir checkpoint pre-tick en `runner._run`** — antes de cada tick, guardar estado para rollback.
9. **Añadir lógica de recuperación al iniciar servidor** — escanear mundos, restaurar desde checkpoint más reciente válido.
10. **Crear endpoints API** — checkpoint, pause, resume, cancel, restore, listar checkpoints, status.
11. **Modificar league scoring** — evitar duplicación con cursor atómico / tabla de eventos puntuados.
12. **Pruebas unitarias de checkpoint/restauración** — determinismo, comparación con ejecución continua.
13. **Pruebas de recuperación ante fallo** — matar worker mid-tick, verificar restauración.
14. **Pruebas de múltiples mundos** — ejecución simultánea sin interferencia.
15. **Documentar migración para usuarios existentes** — cómo se tratan los mundos con solo `state_json`.

---

## L. Riesgos Técnicos y Medidas Preventivas

| Riesgo | Severidad | Medida preventiva |
|---|---|---|
| Divergencia al restaurar mundos antiguos (solo `state_json`, sin RNG state) | Alto | Marcar como "recovered"; advertir que la continuidad exacta desde el origen no está garantizada; generar nuevo checkpoint al primer tick. |
| Corrupción de checkpoint (payload dañado) | Medio | SHA-256 checksum por payload; fallback al checkpoint anterior válido. |
| Duplicación de puntuación de liga | Alto | Cursor atómico en DB o tabla `scored_events(event_id, claim_id)` con unique constraint. |
| Dos workers ejecutando el mismo mundo | Alto | Advisory lock por `world_id` (PostgreSQL `pg_advisory_lock` o Redis SETNX con TTL) antes de ejecutar ticks. |
| Snapshots demasiado grandes | Medio | Excluir crónica antigua de checkpoints frecuentes; comprimir payload; retención limitada (últimos K). |
| SQLite con escrituras concurrentes | Medio | Habilitar WAL mode y `busy_timeout`; migrar a PostgreSQL para multi-worker. |
| Pérdida de RNG state entre versiones de NumPy | Medio | Versionar el formato de checkpoint; almacenar versión de NumPy; probar restauraciones entre versiones. |
| `Chronicle._EVENT_ID` contador global no restaurado | Medio | Al deserializar crónica, restaurar contador desde `max(event.id) + 1`. |
| Cachés internas (`_terr_cache`, `_inf_cache`) obsoletas tras restauración | Bajo | Invalidar cachés al restaurar (se recomputan bajo demanda). |

---

## Conclusión

El motor de simulación de EvoWorld es **determinista y funcional**, pero el sistema de persistencia actual solo guarda una instantánea al crear el mundo. No existen checkpoints, no se conserva el estado del RNG ni los contadores internos, y no hay recuperación ante fallos ni reinicios. El plan propuesto es incremental: añadir serialización completa del motor, tabla de checkpoints, ejecución periódica de checkpoints, recuperación automática al iniciar, y APIs de control de ciclo de vida — sin reescribir el motor ni romper simulaciones existentes.

---

**Fin del informe de auditoría Fase 1.** Este documento sirve como plan técnico para que otro agente implemente los cambios de manera controlada, respetando la regla de no implementar nada en esta fase.