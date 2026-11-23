# F7-E.2-I-B3-4 — Exclusión de mundos eliminados en `league.py`

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-B3-4 (B3-1, B3-2 y B3-3 aceptados y cerrados).
**Estado:** **implementado y probado** (no aceptado — pendiente de dictamen).
**Punto de parada:** al terminar este informe. **B3-5 no fue iniciado; `challenge_service.py` no fue modificado.**

## 0. Archivos

| Archivo | Estado en este subbloque |
|---|---|
| `src/worldsim/server/league.py` | **modificado** (único archivo de producción; solo los 3 puntos inventariados) |
| `tests/test_deleted_world_league.py` | **nuevo** (único archivo de test) |
| `F7-E.2-I-B3-4-REPORT.md` | **nuevo** (este informe) |
| Cualquier otro archivo | **intacto** (manifiesto SHA256, §7) |

---

## 1. Inventario de las tres consultas de `WorldRow` (inspección previa, §2)

| # | Ubicación | Función | Información obtenida | Consumidores | Carácter |
|---|---|---|---|---|---|
| **P1** | `league.py:80` (pre-edit) | `claim_kingdom()` | Existencia del mundo + `mode` (¿es liga?) | `POST /api/worlds/{id}/claims` (app.py:912) → 400 `"world not found"` / `"this world is not a league world"` | **Ordinario** |
| **P2** | `league.py:125` (pre-edit) | `claim_kingdom()` (tras insertar el claim) | `state_json` → nombre del reino para el toast | payload de la misma respuesta (`kingdom_name`) | **Ordinario** |
| **P3** | `league.py:274` (pre-edit) | `maybe_end_season()` | `mode`, `season_over`, `season_years` — gate de la transición de fin de temporada (**escribe** `season_over = 1`) | `WorldRunner` (runner.py:763) tras cada tick; `test_league.py` | **Ordinario** (transición sobre la fila viva) |

**Ninguno requiere acceso histórico explícito:**
- P1: sin mundo vivo no hay claim que conceder → contrato `"world not found"` ya existente para el caso inexistente; un eliminado debe ser indistinguible.
- P2: solo produce metadatos de toast desde la fila viva; si la fila no está, el código ya contempla `kingdoms is None` (omite el nombre) — comportamiento previo para activos idéntico.
- P3: es una **escritura**; la decisión vinculante del BLOCK3 ("las transiciones nunca modifican filas eliminadas") exige que un eliminado no reciba `season_over = 1`. Con el filtro, fila→`None`→`return None` (mismo camino que un mundo inexistente). No hay contrato F7-D/DEC20/DEC21 que dependa de leer eliminados aquí.

**Comportamiento previo para mundos activos (registrado antes de editar):** P1/P2 entregaban la fila y el payload con `kingdom_name`; P3 congelaba la temporada y devolvía el mensaje de campeón (o `None` si ya estaba sobre/limitado). Las suites existentes están bloqueadas por el defecto `WorldRow.visibility` (§5), por lo que el comportamiento activo se re-establece y verifica con pruebas de función nueva (§4).

**Fuera de alcance (no modificado):** `get_claims()` y `standings()` leen **`ClaimRow`**, no `WorldRow` → no están en el inventario de tres puntos; su exposición HTTP ya queda bloqueada por la compuerta de `_load_row` filtrado (B3-3). No se añadieron accesos alternativos.

---

## 2. Cambios exactos (diff real de `git diff`, 22+/3−)

`league.py` estaba **limpio antes de B3-4** (no figuraba en el status base) → el diff contra HEAD es **100 % de este subbloque** (sin mezcla de cambios preexistentes, a diferencia de `app.py` en B3-3). Guardado en `Temp\opencode\b34_league_diff.txt`.

### P1 — `claim_kingdom()` gate (línea 80→85)

```diff
     with _session() as db:
+        # F7-E.2 (B3-4): ordinary read excludes logically deleted worlds —
+        # claiming on a deleted world is indistinguishable from a missing
+        # one ("world not found").
         row = db.scalars(
-            select(WorldRow).where(WorldRow.id == world_id)
+            select(WorldRow).where(
+                WorldRow.id == world_id,
+                WorldRow.deleted_at.is_(None),
+            )
         ).first()
         if row is None:
             return "world not found"
```

### P2 — `claim_kingdom()` lectura de metadatos del toast (línea 125→133)

```diff
         # attach the claimed kingdom's name for toasts
+        # F7-E.2 (B3-4): ordinary read excludes logically deleted worlds
+        # (no deleted world can ever supply toast metadata).
         kingdoms = db.scalars(
-            select(WorldRow).where(WorldRow.id == world_id)
+            select(WorldRow).where(
+                WorldRow.id == world_id,
+                WorldRow.deleted_at.is_(None),
+            )
         ).first()
         if kingdoms is not None:
```

### P3 — `maybe_end_season()` gate de transición (línea 274→289)

```diff
     with _session() as db:
-        row = db.scalars(select(WorldRow).where(WorldRow.id == world_id)).first()
+        # F7-E.2 (B3-4): ordinary read excludes logically deleted worlds —
+        # a deleted world is never frozen nor written (no season transition
+        # on a deleted row; falls through like a missing world).
+        row = db.scalars(
+            select(WorldRow).where(
+                WorldRow.id == world_id,
+                WorldRow.deleted_at.is_(None),
+            )
+        ).first()
         if row is None or row.mode != "league" or row.season_over:
             return None
```

**Preservado en los tres:** joins/condiciones existentes (`WorldRow.id == world_id`), orden, agregaciones y cálculos (ninguna línea de `SCORE_TABLE`, `_event_delta`, `standings`, `score_new_events` o `maybe_end_season` fue alterada más allá del `where`); sin parámetros `include_deleted`/`historical`; sin accesos alternativos; contratos DEC20/DEC21 y F7-D intactos; la condición `if row is None …` byte-idéntica en P1 y P3.

**Verificación de conteo:** `deleted_at` en `src/` pasó de **16 → 19** (+3, exactamente los tres filtros). `grep` de `league.py`: 3 × `select(WorldRow)` y 3 × `WorldRow.deleted_at.is_(None)`; cero menciones de `include_deleted`/`historical`; cero asignaciones a `deleted_at`.

---

## 3. Pruebas — `tests/test_deleted_world_league.py` (nuevo): **13 passed**

```text
13 passed in 3.22s
```

| # | Test | Req. §4 | Resultado |
|---|---|---|---|
| 1 | `test_p1_claim_on_deleted_world_returns_world_not_found` | 1 (P1) | PASSED — `claim_kingdom` → `"world not found"`, **cero claims insertados** |
| 2 | `test_p1_claim_on_missing_world_same_contract` | 1 | PASSED — inexistente → mismo string (indistinguible) |
| 3 | `test_p1_p2_claim_on_active_league_world_preserved` | 2 (P1+P2) | PASSED — claim persistido, `kingdom_name == "Avalon"` (P2 activo), reglas de duplicado intactas |
| 4 | `test_p1_p2_sql_of_both_world_reads_carries_predicate` | **4 (SQL)** | PASSED — captura `before_cursor_execute`: la reclamación exitosa ejecuta **exactamente 2** `SELECT … FROM worlds` (P1 y P2) y **ambas contienen `deleted_at IS NULL`** |
| 5 | `test_p3_maybe_end_season_deleted_world_no_transition_no_write` | 1 (P3) | PASSED — pasada la temporada: `None`, `season_over == 0` (**sin escritura**), `deleted_at` byte-idéntico |
| 6 | `test_p3_maybe_end_season_missing_world_same_contract` | 1 | PASSED — inexistente → `None` |
| 7 | `test_p3_maybe_end_season_active_world_ends_and_second_call_noop` | 2 (P3) | PASSED — congela + 🏆 con campeón; mundo bajo límite → `None`; segunda llamada → no-op (semántica previa) |
| 8 | `test_p3_sql_of_season_read_carries_predicate` | **4 (SQL)** | PASSED — el `SELECT` de `maybe_end_season` contiene `deleted_at IS NULL` |
| 9 | `test_mixed_active_and_deleted_only_active_affected` | **3 (mixta)** | PASSED — misma BD: claims solo en activo (1 vs 0); temporada termina solo en activo; fila y marca del eliminado intactas |
| 10 | `test_standings_order_and_classification_intact_for_active` | **5** | PASSED — orden por puntos desc y ranks 1..3 sobre claims activos |
| 11 | `test_scoring_helpers_untouched_for_active_data` | 5 | PASSED — `score_new_events` opera con datos activos (sin mutación con 0 eventos) |
| 12 | `test_http_claims_on_deleted_world_are_404_like_missing` | 1 e2e | PASSED — HTTP real: POST/GET claims sobre eliminado → 404 `world {id} not found` idéntico al inexistente; cero claims |
| 13 | `test_static_three_world_reads_all_filtered_in_sql` | estática | PASSED (AST) — exactamente 3 `select(WorldRow)`, cada `.where()` con `WorldRow.deleted_at.is_(None)`; sin `include_deleted`/`historical`; sin escritura de `deleted_at` |

**Evidencia SQL (req. 4):** los tests 4 y 8 capturan las sentencias **realmente ejecutadas** en SQLite mediante `before_cursor_execute` — un filtrado en memoria no aparecería ahí. La prueba 13 es **AST complementaria**, presentada como tal (no sustituye a las comportamentales 1, 5, 9).

**Nota de transparencia:** en la primera ejecución el test 7 falló por un **defecto de diseño del propio test** (sin claims, `standings` vacío → mensaje alternativo `"The age of the league has ended."` en lugar del de campeón). Se corrigió el test insertando un claim; **ningún archivo de producción fue tocado por este fallo**. Resultado final: 13/13.

---

## 4. Comparación con la línea base (identidad de nombres y causas)

**Línea base capturada antes de editar** (mismo día, post-B3-3).

### `tests/test_league.py`

| Momento | Resultado | Causas |
|---|---|---|
| Pre-B3-4 | **6f/0p** — 6 nombres guardados | 6 × `TypeError: 'visibility' … WorldRow` |
| Post-B3-4 | **6f/0p** | 6 × `TypeError: 'visibility' … WorldRow` |

- **Nombres: identidad exacta 6/6** (`Compare-Object` sin diferencias). **Causas idénticas.**
- Raíz preexistente: `_world()` usa `POST /api/worlds` (roto por `WorldRow(visibility=…)`), idéntico al baseline de `test_server`/`test_visibility`. **Cero regresiones, cero fallos nuevos.**

### Suites de regresión (baseline = estado inmediatamente pre-B3-4)

| Suite | Pre-B3-4 | Post-B3-4 | Identidad de nombres |
|---|---|---|---|
| `test_server.py` | 4f/1p | **4f/1p** | **4/4 idénticos** |
| `test_visibility.py` | 52f/4p | **52f/4p** | **52/52 idénticos** |
| `test_etapa5_http.py` | 12f/1p | **12f/1p** | **12/12 idénticos** |

### Suites de eliminación lógica (todas verdes)

| Suite | Resultado |
|---|---|
| `test_world_deletion.py` | **17/17** |
| `test_deleted_world_repository.py` (B3-1) | **12/12** |
| `test_deleted_world_recovery.py` (B3-2) | **7/7** |
| `test_deleted_world_app.py` (B3-3) | **12 passed + 1 xfail** |
| `test_deleted_world_league.py` (B3-4) | **13/13** |
| **Batería combinada** | **60 passed, 1 xfailed** |

### Categorías (§6)

- **Pasadas:** 13 nuevas + 17+12+7+12 de las suites aceptadas.
- **Fallos nuevos:** **ninguno** (identidad 100 % en las 4 suites de comparación).
- **Fallos preexistentes reproducidos:** `test_league` 6, `test_server` 4, `test_visibility` 52, `test_etapa5_http` 12 — mismos tests, mismas causas (`WorldRow.visibility` no mapeado y familia) que sus respectivas líneas base.
- **No ejecutables por defectos preexistentes (declaradas, sin corregir):** el camino **HTTP de claims sobre mundo ACTIVO** (tras la compuerta de existencia, `_visibility()` lee `row.visibility` → `AttributeError` preexistente) y la creación HTTP de mundos (`POST /api/worlds`). Por eso el comportamiento activo de P1/P2/P3 se probó **a nivel de función real** (funciones de producción con SQL real) y el HTTP solo cubre la compuerta eliminado/inexistente, que dispara 404 **antes** de alcanzar el defecto.

---

## 5. Limitaciones por defectos preexistentes (declaradas, sin corregir)

1. `WorldRow.visibility` / `WorldRow.status` sin mapear → HTTP de claims/standings activos no ejecutable; `POST /api/worlds` roto. **No corregidos en B3-4.**
2. `ensure_schema()` (aridad) sin tocar → sin lifespan en los tests (aislamiento por diseño).
3. `test_league.py` preexistente sigue en 6f por (1) — sus asserts cubren los caminos que mis pruebas de función reprodujeron de forma aislada.

---

## 6. Estado Git inicial / final

| Ítem | Inicial (pre-B3-4) | Final |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` (sin commits) |
| `git status --porcelain` | 135 entradas | **138** (solo + `league.py` modificado + test nuevo + informe) |
| Staged / Stash | 0 / 0 | 0 / 0 |
| SHA256 `.py` | 97 en manifiesto | 98 (97 + test nuevo); **de los 97 preexistentes, solo `league.py` difiere** |
| `worlds.db` | `5D0B27BC493A2194…` | `5D0B27BC493A2194…` (intacto) |

Delta de status verificado: exactamente ` M src/worldsim/server/league.py` y `?? tests/test_deleted_world_league.py` (más este informe). Nada staged/unstaged/untracked preexistente alterado. Operaciones Git: solo lectura; cero destructivas.

**Precisión:** `tests/test_league.py` figura como ` M` también, pero **es un cambio preexistente sin commitear** (29+/13−: patrón `_seq`/`_world` con autenticación) **ya presente en los tres baselines** (pre-B3-3, pre-B3-4 y el delta before→after de este subbloque, que no lo incluye). Fue preservado byte a byte — B3-4 no lo tocó.

---

## 7. Confirmación de archivos de producción modificados (§6)

Manifiesto SHA256 de los 97 `.py` preexistentes (antes/después): **única diferencia `src/worldsim/server/league.py`** (más el test nuevo).

- [x] **Solo `league.py`** modificado en producción, y **solo en los tres puntos** inventariados (diff §2: 3 hunks, 22+/3−).
- [x] `app.py`, `repository.py`, `recovery_coordinator.py` **intactos** (hashes idénticos a sus estados aceptados en B3-1/B3-2/B3-3).
- [x] `challenge_service.py` **intacto** (corresponde a bloques posteriores — B3-5 **no** iniciado).
- [x] `runner.py`, `transitions.py`, `recovery_manager.py`, `db.py` **intactos**.
- [x] Las 8 llamadas a `get_existing()` sin cambios; `safe_delete_world()`, recuperación y comportamiento de eliminación sin alterar.
- [x] Defectos `WorldRow.status`/`visibility`/`ensure_schema()` **sin corregir**.
- [x] Sin `include_deleted`/`historical`; sin restauración (0 escrituras de `deleted_at`); contratos DEC20/DEC21 y F7-D sin cambios; B3-1/B3-2/B3-3 no reabiertos.
- [x] Tests preexistentes sin modificar; sin arreglos oportunistas.

---

## 8. Reservas técnicas pendientes

1. **Concurrencia 2c.7 (dos workers, pre-commit):** **sigue explícita y abierta** — antes del commit de `deleted_at`, estos filtros leen `NULL` y verían el mundo como activo. Nada de B3-4 la demuestra ni la resuelve.
2. **`challenge_service.py`** (y sus accesos a `WorldRow`) queda **pendiente para bloques posteriores** (B3-5+), no autorizados en esta intervención.
3. **`get_claims()`/`standings()`** no leyeron `WorldRow` (fuera del inventario); su gate HTTP depende del `_load_row` de B3-3. Si el BLOCK3 posterior exige exclusión adicional a nivel de función, deberá autorizarse explícitamente.
4. **Camino HTTP activo de claims/standings** bloqueado hasta corregir el mapeo ORM `visibility` (fuera de alcance).
5. El xfail de A1 (B3-3) y la no-ejecutabilidad de `test_league.py`/`test_server`/`test_visibility` permanecen hasta dicha corrección autorizada.

---

## 9. Estado y parada

**Implementado y probado.** 13/13 pruebas nuevas verdes (2 con captura de SQL ejecutada); `test_league.py` con identidad exacta de 6 nombres y causas vs su línea base; regresión con identidad 4/4, 52/52 y 12/12; batería lógica **60 passed + 1 xfail**; integridad verificada (solo `league.py` cambió, +3 `deleted_at` en `src`).

**Me detengo aquí. No se inició B3-5 y `challenge_service.py` no fue modificado.** Entregado para revisión — espero dictamen y autorización expresa.
