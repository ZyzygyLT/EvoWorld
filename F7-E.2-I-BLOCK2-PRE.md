# F7-E.2-I-BLOCK2-PRE — Auditoría previa al Bloque 2 (eliminación lógica en `_delete_world_row`)

**Alcance de este documento:** inspección y planificación únicamente. **Ningún `.py`,
test, ORM ni migración fue modificado durante esta auditoría.** Este informe no
reabre DEC19, F7-D-I1 ni el plan aprobado F7-E.2-I-PLAN.md; solo lo descompone en
subbloques ejecutables y documenta la línea base contra la que se medirá el Bloque 2.

---

## 1. Estado Git

| Ítem | Valor verificado |
|---|---|
| Branch | `feature/engine-checkpoints` |
| HEAD | `e6f6983` (sin commits nuevos; **ninguna operación destructiva ejecutada**: sin `commit`, `reset`, `checkout`, `stash` ni limpieza) |
| Entradas en `git status` | 122 |
| Stash | vacío (`git stash list` sin resultados) |
| Trackeados modificados (preexistentes, preservados) | `app.py`, `db.py`, `runner.py`, `engine.py`, `models.py`, `conftest.py`, `test_auth.py`, `test_league.py`, `test_multiplayer.py`, `test_server.py`, `web/src/App.jsx`, `web/src/components/RoomPanel.jsx`, `web/src/components/StartScreen.jsx` |
| Untracked en `src/worldsim/server/` (preexistentes, preservados) | `challenge_service.py`, `errors.py`, `operation_service.py`, `persistence_service.py`, `recovery_coordinator.py`, `recovery_manager.py`, `repository.py`, `transitions.py` |
| Única edición de código del presente esfuerzo | `db.py` (Bloque 1, aceptado): import `DateTime` (línea 12) + `deleted_at = Column(DateTime, nullable=True)` (línea 38). `git diff HEAD -- db.py` confirma que **solo añade**, no elimina ni altera columnas existentes |
| mtimes relevantes | `db.py` 18/11 23:45 (Bloque 1); `app.py` 18/13:10, `runner.py` 17/00:14, `conftest.py` 17/11:48 — **anteriores** a esta auditoría PRE (solo lectura) |

**Documentos PRE:** no existía ningún `F7-E.2-I-BLOCK2*.md` (verificado por glob);
este archivo es nuevo, por lo que no se sobrescribe ningún PRE anterior.

---

## 2. Inventario de la eliminación actual (línea base de código)

### 2.1 Persistencia — `runner.py`

| Símbolo | Líneas | Estado actual |
|---|---|---|
| `_delete_world_row(session_factory, world_id)` | 1016–1025 | **Borrado físico**: `session.get` → `None` ⇒ `WorldNotFoundError`; `session.delete(row)` + `session.commit()`. Corre en worker thread (`to_thread`) |
| `safe_delete_world(...)` | 1028–1112 | Secuencia: `begin_delete` (1073) → `get_existing` (1077) → `close` (1079) → verify `CLOSED` (1080–1087) → `unregister` (1089) → `to_thread(_delete_world_row)` (1091–1092) → `asyncio.shield` (1097) → cancel ⇒ `guard_transferred` + callback `end_delete` (1098–1106) → `finally: end_delete` (1108–1112) |
| `WorldNotFoundError` | 990–999 | `LookupError` → traducción a 404 existente (sin códigos nuevos) |
| `_lingering_delete_work` / `_release_lingering_delete` | 987 / 1002 | Referencias fuertes al trabajo en vuelo (regla 5 / §8) |

### 2.2 Registro — `RunnerRegistry` (`runner.py:779–978`)

| Método | Líneas | Comportamiento relevante |
|---|---|---|
| `register_existing` | 835 | Rechaza con `RuntimeError` si `world_id ∈ _deleting` (≈859) |
| `unregister` | 876 | `pop(..., None)` — idempotente |
| `begin_delete` | 893 | Marca **síncrona** (≈913) + refcount (≈914) |
| `end_delete` | 921 | Refcount; el último descarta marca y lock |
| `is_deleting` | 939 | Consulta de marca |
| `get` | 943 | **Rechaza** `_deleting` (≈952) |
| `get_existing` | 964–965 | `self._runners.get(world_id)` **sin** chequear `_deleting` (distinción contractual con `get()` según FIX9; `safe_delete_world` depende de este comportamiento) |
| `all_runners` / `registry` | 967 / 978 | — |

### 2.3 Modelo — `db.py`

- `WorldRow.deleted_at` (línea 38): presente desde el Bloque 1; **única fuente
  persistida de verdad**; sin `is_deleted` ni indicador equivalente en todo `src/`.
- Columnas reales del ORM (16): `id, seed, width, height, months_simulated,
  land_pct, state_json, created_at, name, mode, season_years, season_over,
  enabled_races_json, owner_user_id, sea_level, deleted_at`.
- **Sin helper UTC en `src/`**: grep de `utcnow|timezone.utc|now(timezone` en
  `src/` y `tests/` ⇒ cero coincidencias; la única mención de `datetime` es el
  import `DateTime` de SQLAlchemy (`db.py:12`).
- `ensure_schema()` (`db.py:227`): firma **sin parámetros** (igual que en HEAD).

### 2.4 Callsites de `get_existing()` (8 localizados; FIX9 documentó 7)

`runner.py:1077` · `app.py:850` · `app.py:900` · `app.py:1290` (`_canonical_month`) ·
`app.py:2163` · `transitions.py:367` · **`transitions.py:473`** (`_critical_section`,
no incluido en FIX9) · `recovery_coordinator.py:490`.
Ninguno chequea `_deleting`; **no se globalizará ningún cambio** (§ restricción).

---

## 3. Secuencia de eliminación propuesta (solo Bloque 2)

Único cambio funcional autorizado en este Bloque: el cuerpo de `_delete_world_row`
(`runner.py:1016–1025`). **`safe_delete_world()` no se toca**: el orden
`begin_delete → lock → get_existing → close → verify CLOSED → unregister →
worker UPDATE → shield → end_delete` se mantiene intacto (Alternativa B ya
aceptada: `unregister → UPDATE`).

```python
def _delete_world_row(session_factory, world_id: int) -> None:
    """Blocking LOGICAL row delete; runs in a WORKER THREAD (never on the loop).

    Idempotente (DEC-PRE-3): una fila ya marcada no se re-marca ni se
    sobrescribe su ``deleted_at`` (equivale a
    ``UPDATE worlds SET deleted_at = deleted_at WHERE id = :id``).
    """
    from datetime import datetime, timezone

    from worldsim.server.db import WorldRow

    with session_factory() as session:
        row = session.get(WorldRow, world_id)
        if row is None:
            raise WorldNotFoundError(world_id)      # ausencia física legacy → 404
        if row.deleted_at is None:                  # única escritura válida
            row.deleted_at = datetime.now(timezone.utc)
        session.commit()                            # sin DELETE: la fila permanece
```

Decisiones incorporadas y su justificación:

| Decisión | Fundamento |
|---|---|
| `datetime.now(timezone.utc)` y **no** `datetime.utcnow()` | El PLAN Fase 2 (línea 149) sugiere `utcnow()`; la prohibición vigente de la tarea lo anula. **Desviación documentada del plan**: `utcnow()` es naive y está deprecado; se exige timezone-aware. No existe helper UTC previo en `src/` que reutilizar (grep verificado) → import local dentro de la función, junto al import local ya existente de `WorldRow`, para no alterar el resto de `runner.py` |
| Guarda `if row.deleted_at is None` | DEC-PRE-3: nunca sobrescribir una marca ya establecida; la repetición es idempotente (commit sin cambios ≡ `SET deleted_at = deleted_at`) |
| `row is None` ⇒ `WorldNotFoundError` se conserva | Contrato 404 existente para ausencia **física** (datos legacy/BD externa); no se elimina ese comportamiento |
| `session.commit()` sin `session.delete()` | La fila **nunca** se borra físicamente; `deleted_at` es la única verdad persistida |
| Sin cambios en imports de nivel módulo | Mínimo diff: solo la función (y, si se prefiere estilo módulo, un import de `datetime` arriba — decisión de ejecución, mismo archivo) |

**Consecuencia deliberada y conocida:** tras el Bloque 2, eliminar un mundo deja
la fila presente con `deleted_at ≠ NULL`, pero **los GET/listados todavía no la
ocultan**: DEC-PRE-4 (404 indistinguible para owner incluido) queda **pendiente
del Bloque 3** (filtros). El Bloque 2 no puede declarar por sí solo "eliminación
visible al usuario".

---

## 4. Análisis de cancelación / worker thread

| Aseguranza | Evidencia tras el cambio | ¿Se conserva? |
|---|---|---|
| El UPDATE corre en worker thread (loop nunca bloqueado) | `to_thread(_delete_world_row)` en 1091–1092, sin cambios | ✅ Sí |
| Caller cancelado ⇒ el borrado lógico **termina** igualmente | `asyncio.shield(work)` (1097) + `_lingering_delete_work` (987) con referencia fuerte | ✅ Sí (estructura intacta; el trabajo pasa de "DELETE físico" a "UPDATE lógico", mismo ciclo de vida) |
| Marca `_deleting` sobrevive a la cancelación hasta terminar | `guard_transferred` + callback `registry.end_delete` (1098–1106); `finally` solo libera si no hubo transfer (1108–1112) | ✅ Sí |
| Fallo de commit ⇒ nada parcial en persistencia | `with session_factory() as session:` sin commit ⇒ rollback implícito al propagarse la excepción ⇒ `deleted_at` conserva su valor previo (NULL o la marca anterior, nunca una marca "a medias") | ✅ Equivalente al comportamiento previo (rollback dejaba la fila intacta) |
| `WorldNotFoundError` en commit-failure path | `commit_error` del harness (`test_del_05`) sigue viendo excepción propagada desde el worker; `shield` la re-lanza al caller | ✅ Sí |
| Ningún worker anterior sobrevive a `unregister` | `close()` espera el cierre real (shield + re-wait, `runner.py:296–348`) y el verify 1080–1087 exige `CLOSED` **antes** de `unregister` y del UPDATE | ✅ Sí: no se liberan protecciones con workers vivos |
| Worker de transición concurrente (`transitions._write_status`, worker thread) | Bloqueado **síncronamente** por `is_deleting` en `_critical_section:468` antes de `get_existing:473`; reconcilia antes de liberar (505+) | ✅ Sí para la ventana de la marca; **residual** tras `end_delete` → ver §10 |

Diferencia sustantiva única: tras el UPDATE exitoso, **la fila sigue existiendo**,
de modo que cualquier lector posterior sin filtro la verá (hasta el Bloque 3).

---

## 5. Estrategia de reintento

1. **Fallo de commit en el worker** (`commit_error`, caso `test_del_05`):
   rollback total → `deleted_at` sin cambios → `finally` libera marca y lock →
   el caller recibe la excepción. **Reintento seguro**: nueva llamada a
   `safe_delete_world()`; si la fila quedó marcada por un intento previo, la
   guarda `deleted_at is None` la convierte en no-op idempotente (DEC-PRE-3).
2. **Cancelación del caller** (`test_del_10`): el trabajo continúa con la guarda
   transferida; mientras el lock/marca persistan, un reintento se serializa en
   el lock por mundo y verá la marca activa → no se solapa. Tras `end_delete`,
   reintento idempotente.
3. **Crash del proceso entre `unregister` y el UPDATE**: la fila queda **sin
   marca** (world activo) y el runner cerrado/desregistrado → el mundo es
   recuperable en el próximo startup y la eliminación debe reintentarse
   explícitamente. Jamás queda "eliminado a medias" (consistente con DEC10 §8:
   los datos persistidos solo se tocan tras CLOSE verificado). **Nota:** hoy el
   recovery de startup está caído por un defecto preexistente (§7); el reintento
   end-to-end queda condicionado al Bloque 3/arreglo ajeno.
4. **Eliminación repetida del mismo mundo** (segunda llamada tras éxito):
   `begin_delete` → `get_existing() is None` (ya desregistrado) → sin close →
   worker halla `deleted_at ≠ None` → commit no-op → éxito idempotente, sin
   sobrescribir la marca original.

---

## 6. Matriz de concurrencia (estado tras el Bloque 2, previo al Bloque 3)

| # | Escenario | Ventana | Comportamiento previsto | Estado |
|---|---|---|---|---|
| 1 | DELETE vs GET de mundo (`app.py:850` / `_load_row:555` / listing 430–466) | Antes de `unregister` | Runner vivo servido el GET (comportamiento actual) | Sin cambio |
| 2 | DELETE vs GET | Tras UPDATE, antes del Bloque 3 | **La fila sigue visible** (ningún filtro `deleted_at IS NULL`) ⇒ GET puede devolver 200 con `deleted_at` ya fijado | ⚠️ Residual conocido → Bloque 3 (DEC-PRE-4 pendiente aquí) |
| 3 | DELETE vs transición (`_critical_section`) | Durante la marca `_deleting` | Rechazo **síncrono** `TransitionError("being deleted")` (468), sin await intermedio | ✅ Protegido |
| 4 | DELETE vs transición | Tras `end_delete`, antes del Bloque 3 | `repo.get_world` (repository.py:119, sin filtro) devuelve la fila marcada → `_write_status` **podría escribir `status` sobre un mundo eliminado** | ⚠️ Residual → Bloque 3 (y hoy bloqueado por defecto preexistente de `status`, §7) |
| 5 | DELETE doble concurrente (mismo mundo) | Ambas en vuelo | Lock por mundo serializa; la segunda halla `deleted_at ≠ None` → no-op idempotente | ✅ DEC-PRE-3 |
| 6 | DELETE vs worker de persistencia/checkpoint | Cierre | `close()` espera y verify exige `CLOSED` antes de `unregister` y UPDATE | ✅ Sin workers vivos al soltar protecciones |
| 7 | DELETE vs cancelación del caller | Durante `shield` | Trabajo termina en su thread; guarda transferida; caller ve `CancelledError`, nunca éxito | ✅ Sin cambios estructurales |
| 8 | DELETE vs `register_existing` | Durante la marca | Rechazada (`RuntimeError`, ≈859) | ✅ Sin runner nuevo |
| 9 | DELETE vs recovery de startup (`_list_world_rows` 461–465) | Próximo arranque | Lista **todas** las filas, incluidas las marcadas ⇒ recuperaría mundos eliminados (hoy: startup roto por defecto preexistente, §7) | ⚠️ Residual → Bloque 3 |
| 10 | DELETE vs `_canonical_month` (`app.py:1288–1295`) | Tras UPDATE | `get_existing` → `None` → lee `row.months_simulated` de la fila marcada (mes histórico) | ⚠️ Residual → Bloque 3 |

---

## 7. Inventario de filtros pendientes (material del Bloque 3 — **no se tocan ahora**)

Puntos que deben filtrar `deleted_at IS NULL` (o excluir `deleted_at IS NOT NULL`)
cuando el Bloque 3 los implemente:

| Archivo | Líneas | Contexto |
|---|---|---|
| `recovery_coordinator.py` | 461–465 | `_list_world_rows`: `select(WorldRow)` sin filtro |
| `recovery_coordinator.py` | 831 | `db.ensure_schema(engine)` — **defecto preexistente**, ver §7.1 |
| `app.py` | 430–466 (uso en 441/450) | Listing público/privado con `WorldRow.visibility` |
| `app.py` | 553–558 / 611–617 | `_load_row` / `_load_visible_row` |
| `app.py` | 710 / 729 / 1294 | `db_.get(WorldRow, …)` en handler de borrado y `_canonical_month` |
| `repository.py` | 119 / 126 / 143 / 171 / 275 | `get_world`, `get_worlds_by_status`, `s.get(WorldRow, …)` |
| `transitions.py` | 165–176 (`_write_status`), 179–198 (`_revert_status`), 473 | Lecturas/escrituras de `row.status` en worker thread |
| `league.py` | 80 / 125 / 274 | `select(WorldRow).where(WorldRow.id == …)` |
| `challenge_service.py` | 752 (crea) / 1039 (carga) | Carga de fila de participante |
| `recovery_manager.py` | 95 / 104 | Elegibilidad de recuperación |

**Restricción de forma al filtrar:** la sonda SQLite hecha en esta auditoría
demuestra que un `datetime` tz-aware escrito **se lee naive** (tzinfo perdida,
wall-clock preservado). Los filtros deben usar **`WorldRow.deleted_at.is_(None)`**
/ `IS NOT NULL` — nunca comparaciones de rango entre el valor naive leído y un
`datetime` aware (TypeError `naive vs aware`), ni depender de `tzinfo` en lectura.

### 7.1 Hallazgo: defecto preexistente de columnas ausentes (NO causado por F7-E.2-I)

Evidencia recogida en esta auditoría:

- `WorldRow` **no tiene** `status` ni `visibility` (`hasattr` → `False`; grep en
  `db.py` solo halla `status` en otras tablas, líneas 67/140/157).
- Constructores y queries con esas columnas fallan hoy:
  `TypeError: 'status'|'visibility' is an invalid keyword argument for WorldRow`
  (p. ej. `tests/test_pause_transitions.py::test_14`, `tests/test_server.py::test_server_rest`)
  y `AttributeError: type object 'WorldRow' has no attribute 'status'|'visibility'`.
- `ensure_schema()` (sin parámetros, `db.py:227`, idéntico en HEAD) es invocado
  con un engine en 3 puntos: `tests/conftest.py:70`,
  `recovery_coordinator.py:831`, `tests/test_visibility.py:159/188`
  ⇒ `TypeError: ensure_schema() takes 0 positional arguments but 1 was given`
  ⇒ **la app no arranca** (`TestClient(app)` falla en lifespan por
  `run_startup → ensure_schema`).
- **No es del Bloque 1**: `git diff HEAD -- db.py` solo añade `deleted_at`/
  `DateTime`; `git show HEAD:db.py` no contiene `visibility`;
  `git log --all -S "visibility = Column"` y `-S "def ensure_schema(engine"` vacíos;
  una sola `class WorldRow` en todo el repo; sin backups (`.bak/.orig/.rej`);
  sin stash. Los archivos que referencian las columnas son modificaciones o
  untracked preexistentes (`app.py`, `repository.py`, `transitions.py`,
  `recovery_manager.py`, `conftest.py`).
- Los `_out.txt` de fallos (18/11 17:47–18:14) son **anteriores** a la edición
  de `db.py` del Bloque 1 (18/11 23:45) ⇒ los fallos ya existían antes de F7-E.2-I.
- La BD física `src/worldsim/server/worlds.db` **sí** tiene
  `status, last_checkpoint_at, checkpoint_count, checkpoint_version, visibility,
  deleted_at` — el esquema adelantado al ORM.

**Consecuencia para el Bloque 2:** toda validación end-to-end (arranque de app,
recovery, transiciones, listing) es **imposible** mientras este defecto exista;
el Bloque 2 solo puede validarse con las suites unitarias de eliminación (harness
de fakes) y contra la línea base. Cualquier fallo preexistente de las suites
generales **no puede atribuirse** al Bloque 2 ni declararse "arreglado" por él.

### 7.2 Línea base de pruebas (medida en esta auditoría, previa al Bloque 2)

| Suite | Resultado |
|---|---|
| `tests/test_world_deletion.py` | **10 passed** ✅ (del_01…del_10) |
| `test_world_deletion + test_runner_close + test_shutdown + test_runner_foundation + test_pause_transitions` (corrida combinada) | **1 failed, 53 passed, 20 errors** — el fallo es `test_lifespan_startup_then_shutdown_closed` (preexistente); los 20 errors son `test_pause_transitions` por `TypeError: 'status' is an invalid keyword argument for WorldRow` (construcción del fixture) |
| `test_repository.py` | 34 failed / 60 passed (raíz: `WorldRow(status=…)` en el test) |
| `test_recovery_coordinator.py` | 35 failed / 2 passed (raíz: `ensure_schema(engine)`) |
| `test_f7d_state_machine.py + test_schema_migrations.py` | 46 failed |
| `test_server.py::test_server_rest` | failed — `TypeError: 'visibility' … WorldRow` |
| Históricos `visibility_out.txt` / `server_out.txt` / `state_machine_out.txt` | 52 / 4 / 26 failed (todos con raíz preexistente) |

**Regla de medición del Bloque 2:** ninguna cifra de esta tabla puede empeorar;
las únicas mejoras admisibles provienen de cambios del Bloque 2 (harness de
`test_world_deletion` y tests nuevos), nunca de arreglar el defecto de §7.1.

---

## 8. Pruebas de regresión propuestas

### 8.1 Adaptación exigida por el plan (Fase 2, paso 2)

`tests/test_world_deletion.py` asume hoy borrado **físico**:

- `_FakeRow` (45–48) solo tiene `id` y `owner_user_id` → **carece de `deleted_at`**
  ( AttributeError con el nuevo código ⇒ hay que añadir `deleted_at = None`).
- `_DeleteHarness` (51+): `store: dict`, `ops` registra `("get", id)` /
  `("delete", id)`, `commit_probe`/`commit_error`/`commit_release`.
  El evento `"delete"` y las aserciones `h.store == {}` (test_del_01:168,
  test_del_02 y hermanos) deben pasar a: fila **presente** con
  `h.store[7].deleted_at is not None` y op actualizada a `"update"` (o
  equivalente), preservando los órdenes ya assertados
  (`["close:start","close:done","unregister:7","commit"]`, test_del_03).

Los 10 tests conservan su significado contractual (orden, shield, cancelación,
guard) solo si se adapta el harness; **ninguna aserción de comportamiento de
`safe_delete_world` se elimina**.

### 8.2 Tests nuevos propuestos (alcance Bloque 2)

1. `test_del_logical_row_remains_present`: tras `safe_delete_world`, la fila
   **existe** en el store con `deleted_at` fijado (no `None`).
2. `test_del_idempotent_repeated_delete`: segunda `safe_delete_world` ⇒ éxito
   sin error y **`deleted_at` byte-idéntico al primer valor** (DEC-PRE-3).
3. `test_del_never_overwrites_existing_mark`: fila pre-marcada antes de llamar
   ⇒ se conserva el valor original.
4. `test_del_timestamp_is_utc_aware`: `deleted_at.tzinfo is not None` y
   `utcoffset() == timedelta(0)` (prohibición `utcnow()` verificada).
5. `test_del_missing_row_still_raises_world_not_found`: fila físicamente
   ausente ⇒ `WorldNotFoundError` (contrato 404 legacy intacto).
6. `test_del_commit_failure_leaves_mark_untouched`: `commit_error` ⇒ excepción
   propagada y `deleted_at` sin cambios (equivalente lógico del actual del_05).
7. `test_del_10_cancellation_during_persisted_delete` (adaptado): sin cambios de
   semántica — el trabajo shielded termina y la guarda se transfiere.

### 8.3 Baterías de regresión (comparar contra línea base §7.2)

`test_world_deletion.py` (10/10) · `test_runner_close.py` · `test_shutdown.py` ·
`test_runner_foundation.py` · `test_pause_transitions.py` · `test_repository.py` ·
`test_recovery_coordinator.py` · `test_f7d_state_machine.py` ·
`test_schema_migrations.py` · `test_visibility.py` · `test_server.py`.
Criterio: **cero regresiones nuevas**; los fallos preexistentes de §7.1 se
reportan intactos y sin adjudicar al Bloque 2.

---

## 9. Orden de ejecución por subbloques

| Subbloque | Contenido | Archivos | Condición de salida |
|---|---|---|---|
| **2a** | Reescribir `_delete_world_row` (UPDATE lógico + guarda idempotencia + `datetime.now(timezone.utc)`) | **solo** `src/worldsim/server/runner.py` | Dif mínimo en 1016–1025; import de UTC local |
| **2b** | Adaptar harness: `_FakeRow.deleted_at`, op `"update"`, asserts de fila presente con marca (plan Fase 2, paso 2) | **solo** `tests/test_world_deletion.py` | 10/10 con nuevo significado (fila marcada, no ausente) |
| **2c** | Añadir los 7 tests nuevos del §8.2 | **solo** `tests/test_world_deletion.py` (o archivo dedicado si el plan lo permite) | 17/17 verdes |
| **2d** | Correr batería §8.3 vs línea base §7.2 y emitir dictamen del Bloque 2 | ninguno (solo ejecución) | Cero regresiones; dictamen distinguiendo **implementado / probado / aceptado** |

**Fronteras explícitas del Bloque 2:** NO se tocan `transitions.py`,
`recovery_coordinator.py`, `recovery_manager.py`, `repository.py`, `app.py`,
`league.py`, `challenge_service.py`, `db.py` (ya tiene `deleted_at`), migraciones,
DEC19, F7-D-I1 ni el defecto preexistente de §7.1. Los filtros de §6/§7 y DEC-PRE-4
son **Bloque 3**.

---

## 10. Riesgos residuales

1. **DEC-PRE-4 incompleto hasta el Bloque 3:** tras el Bloque 2, un GET sigue
   pudiendo devolver 200 para un mundo eliminado (sin filtros). El Bloque 2
   **no debe declararse** como "404 indistinguible logrado".
2. **Escrituras sobre fila eliminada:** `transitions._write_status`,
   `repository.*` y `_canonical_month` pueden leer/escribir la fila marcada
   hasta el Bloque 3 (matriz §6, filas 2/4/10).
3. **Recovery reviviría mundos eliminados** (`_list_world_rows` sin filtro) —
   hoy enmascarado porque el startup está roto por el defecto de §7.1.
4. **Defecto preexistente §7.1 bloquea validación end-to-end:** app no arranca,
   20+ suites en error. Ningún resultado del Bloque 2 puede presentarse como
   prueba de sistema completo.
5. **SQLite devuelve `deleted_at` naive al leer:** comparaciones de rango con
   datetimes aware lanzarán `TypeError`; los futuros filtros deben ser
   `IS NULL`/`IS NOT NULL` (§7).
6. **Harness de tests:** `_FakeRow` sin `deleted_at` rompería los 10 tests con
   `AttributeError` si 2a avanza sin 2b; ejecutar en el orden de §9.
7. **`get_existing()` sin chequeo `_deleting`** es contrato (FIX9) del que
   `safe_delete_world` depende: prohibido globalizar cambios a sus 8 callsites
   (incluido `transitions.py:473`, no listado en FIX9).
8. **Desviación del plan documentada:** PLAN Fase 2 línea 149 sugiere
   `datetime.utcnow()`; se usa `datetime.now(timezone.utc)` por la prohibición
   vigente. Debe quedar reflejada en el dictamen del Bloque 2.
9. **`test_pause_transitions::test_14` fase (c)** (712–731) asume fila
   **físicamente ausente** (`WorldNotFoundError` en escritura en vuelo); con
   eliminación lógica esa premisa cambia (la fila existirá marcada) → la
   adaptación corresponde al Bloque 3 (una vez los filtros hagan que
   `_write_status` trate la fila marcada como inexistente). Hoy el test ni siquiera
   llega a ejecutarse (error preexistente, §7.2).
10. **Sin helper UTC reutilizable:** la decisión de import local vs import de
    módulo queda para la ejecución 2a; ambas son solo `runner.py` y ninguna
    crea contrato nuevo.

---

## Dictamen del PRE del Bloque 2

- **Inspección completa:** las 10 secciones exigidas cubiertas con evidencia de
  archivo y línea; línea base de pruebas medida; defecto preexistente
  documentado con prueba de no causalidad.
- **Alcance respetado:** cero modificaciones de `.py`, tests, ORM o migraciones
  durante esta auditoría; cero operaciones Git destructivas; ningún PRE previo
  sobreescrito.
- **Plan ejecutable:** subbloques 2a→2d con archivos y condiciones de salida;
  fronteras hacia Bloque 3 explícitas.
- **Pendiente por diseño:** DEC-PRE-4 (404) y todos los filtros de §7 son
  material del Bloque 3; el presente documento no los declara cumplidos.

**Estado:** informe PRE entregado. **Se detiene aquí — no se inicia la
implementación del Bloque 2.**
