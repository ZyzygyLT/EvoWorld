# F7-E.2-I-B3-3 — Filtros de eliminación lógica en `app.py` (A1, A2, A4, A6)

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-B3-3 (B3-1 y B3-2 aceptados y cerrados).
**Estado:** **implementado y probado** (no aceptado — pendiente de revisión).
**Punto de parada:** al terminar este informe. **B3-4 no fue iniciado.**

## 0. Archivos

| Archivo | Estado en este subbloque |
|---|---|
| `src/worldsim/server/app.py` | **modificado** (único archivo de producción; 4 puntos: A1, A2, A4, A6) |
| `tests/test_deleted_world_app.py` | **nuevo** (único archivo de test) |
| `F7-E.2-I-B3-3-REPORT.md` | **nuevo** (este informe) |
| Cualquier otro archivo | **intacto** (verificado por manifiesto SHA256, §7) |

---

## 1. Diff de cada punto

Nota sobre `app.py`: el archivo es **tracked con cambios preexistentes sin commitear** (1708+/93− vs HEAD, ya presentes antes de este subbloque — preservados íntegros). El `git diff` completo contra HEAD los mezcla; por eso a continuación se listan **mis 4 ediciones exactas** (las únicas con la marca `B3-3`), aplicadas sobre el estado de partida.

### A1 — `list_worlds` (GET `/api/worlds`, línea 467)

```diff
         if cursor_id is not None:
             q = q.where(WorldRow.id < cursor_id)
 
+        # F7-E.2 (B3-3): the ordinary listing excludes logically deleted
+        # worlds in SQL (privacy/order/pagination above are unchanged).
+        q = q.where(WorldRow.deleted_at.is_(None))
+
         # limit + 1 para detectar página siguiente
         rows = db_.execute(q.limit(page_limit + 1)).all()
```

- Filtro en **SQL**, encadenado antes de `limit` → no altera formato de respuesta.
- Preservados: predicados de privacidad (`WorldRow.visibility == "public"`, `WorldRow.owner_user_id == user.id`), orden, cursor/paginación (`limit + 1`), `search`, validaciones previas. Todo comprobado por test AST.

### A2 — `_load_row()` (línea 553→565)

```diff
 def _load_row(world_id: int) -> WorldRow:
     with db.SessionLocal() as db_:  # type: ignore[call-arg]
-        row = db_.get(WorldRow, world_id)
+        # F7-E.2 (B3-3): the central ordinary load excludes logically
+        # deleted worlds — a deleted world is indistinguishable from a
+        # missing one (same 404, even for the authenticated owner).
+        row = db_.scalars(
+            select(WorldRow).where(
+                WorldRow.id == world_id,
+                WorldRow.deleted_at.is_(None),
+            )
+        ).first()
         if row is None:
             raise HTTPException(404, f"world {world_id} not found")
         return row
```

- Mensaje contractual **byte-idéntico**: `f"world {world_id} not found"` (sin cambios).
- Un mundo eliminado → `None` → 404, **igual que uno inexistente, incluso para el propietario autenticado** (DEC-PRE-4; tests A2 con HTTP real + Bearer).
- Las reglas de autorización para mundos activos (`_require_visible`, `_is_owner`) no se tocaron.

### A4 — `put_world_visibility()` segunda lectura (línea 710→727)

```diff
     with db.SessionLocal() as db_:  # type: ignore[call-arg]
-        live = db_.get(WorldRow, world_id)
+        # F7-E.2 (B3-3): the second read excludes a world logically
+        # deleted between the initial load and this operation (TOCTOU).
+        live = db_.scalars(
+            select(WorldRow).where(
+                WorldRow.id == world_id,
+                WorldRow.deleted_at.is_(None),
+            )
+        ).first()
         if live is None:
             raise HTTPException(404, f"world {world_id} not found")
         live.visibility = req.visibility  # single-column atomic write
         db_.commit()
```

- Se cierra la ventana TOCTOU: si la fila se elimina entre la primera carga (gate) y esta segunda lectura, la operación se detiene con 404 y **no escribe nada**.
- Autorización (401→404→403/404) y semántica de visibilidad intactos; `if live is None` conservado (byte-idéntico).
- Test A4: fila activa en la primera lectura + `deleted_at` establecido entre lecturas (monkeypatch de `_load_row` con la fila obsoleta) → 404 + marca sin cambiar.

### A6 — `_canonical_month()` fallback (línea 1294→1318)

```diff
     with db.SessionLocal() as db_:  # type: ignore[call-arg]
-        row = db_.get(WorldRow, world_id)
-        return row.months_simulated if row else 0
+        # F7-E.2 (B3-3): a logically deleted world never supplies the
+        # fallback month (falls through to 0 like a missing world).
+        row = db_.scalars(
+            select(WorldRow).where(
+                WorldRow.id == world_id,
+                WorldRow.deleted_at.is_(None),
+            )
+        ).first()
+        return row.months_simulated if row else 0
```

- Lógica de cálculo del mes intacta (runner vivo → `runner.eng.month`; sin runner → fila; sin fila → 0). Un mundo eliminado devuelve **0**, igual que uno ausente.
- Sin comparaciones temporales: solo `WorldRow.deleted_at.is_(None)` en SQL (compatible con SQLite).

---

## 2. Evidencia de que A5 permanece intacto

`grep` de todo `app.py` — solo **5** ocurrencias de `deleted_at`/`db_.get(WorldRow`, cada una en su función esperada:

| Línea | Función | Contenido |
|---|---|---|
| 467 | `list_worlds` | `q = q.where(WorldRow.deleted_at.is_(None))` (A1) |
| 565 | `_load_row` | `WorldRow.deleted_at.is_(None)` (A2) |
| 727 | `put_world_visibility` | `WorldRow.deleted_at.is_(None)` (A4) |
| **748** | **`delete_world` (DELETE)** | **`row = db_.get(WorldRow, world_id)` — SIN filtro** |
| 1318 | `_canonical_month` | `WorldRow.deleted_at.is_(None)` (A6) |

- **La única lectura directa `db_.get(WorldRow, …)` restante de `app.py` está dentro de `delete_world`** (A5): sin `deleted_at` en toda la función → puede localizar una fila ya eliminada → idempotencia de eliminaciones repetidas preservada (DEC-PRE-3).
- Test estático `test_static_a5_delete_read_remains_unfiltered`: exige `"db_.get(WorldRow, world_id)"` dentro del segmento de `delete_world` **y** prohíbe `"deleted_at"` en él.
- Test ejecutado `test_a5_delete_endpoint_repeated_delete_is_idempotent`: DELETE HTTP sobre fila ya eliminada → **200** `{"deleted": <id>}` y el valor crudo de `deleted_at` **byte-idéntico** al previo.
- Verificación global (test estático): `source.count("WorldRow.deleted_at.is_(None)") == 4`, `source.count("deleted_at") == 4` (cero otras menciones), ninguna asignación a `deleted_at` en todo el archivo (**cero restauración**), sin parámetros `include_deleted`/`allow_deleted`/`historical` en ninguna función.

---

## 3. Pruebas ejecutadas y resultados

### 3.1 Nuevas — `tests/test_deleted_world_app.py` (**11 passed, 1 xfail**)

```text
11 passed, 1 xfailed in 4.85s
```

| # | Test | Req. | Resultado |
|---|---|---|---|
| 1 | `test_a2_http_get_deleted_world_is_404` | 1 | PASSED (HTTP anónimo → 404 con mensaje) |
| 2 | `test_a2_owner_http_get_deleted_is_identical_404` | 2 | PASSED (propieta**rio** con Bearer real → 404 **byte-idéntico** al anónimo y al mundo inexistente) |
| 3 | `test_a3_active_world_keeps_existing_access_rules` | 3 | PASSED (`_load_row` carga el activo; PUT owner 200 `{"id":…,"visibility":"public"}`; PUT anónimo 401; contrato de inexistente intacto) |
| 4 | `test_a1_list_worlds_filters_deleted_in_sql_ast` | 4 (primaria) | PASSED — **estructural (AST)**: predicado + privacidad + owner + `limit(page_limit + 1)` preservados |
| 5 | `test_a1_list_worlds_excludes_deleted_behavioral` | 4 (comportamiento) | **XFAIL** (no estricto) — bloqueado por defecto preexistente (§5) |
| 6 | `test_a4_put_visibility_deleted_between_reads_404` | 5 | PASSED (TOCTOU: 404 + marca byte-idéntica + sin escritura) |
| 7 | `test_a4_put_visibility_already_deleted_is_404` | 5 | PASSED (ruta real sin parchear) |
| 8 | `test_a6_canonical_month_never_uses_deleted_row` | 6 | PASSED (activo → 42, eliminado → 0, ausente → 0) |
| 9 | `test_a5_delete_endpoint_repeated_delete_is_idempotent` | 7 | PASSED (200 + `deleted_at` inmutable) |
| 10 | `test_static_a2_a4_a6_filters_and_404_messages` | estática | PASSED (3 filtros + mensajes byte-idénticos) |
| 11 | `test_static_a5_delete_read_remains_unfiltered` | estática A5 | PASSED |
| 12 | `test_static_app_filters_is_none_no_restoration_no_historical_params` | estática global | PASSED (4 filtros, 4 lecturas `.is_(None)`, 0 escrituras, 0 params históricos) |

Aislamiento: SQLite propio por test vía monkeypatch de `dbmod.SessionLocal`/`runnermod.registry`/`appmod.registry` + `Base.metadata.create_all`; `TestClient` **sin** context manager → sin lifespan → sin `run_startup`/`ensure_schema`; propietario vía `POST /api/auth/register` real; filas insertadas con columnas mapeadas **sin** `status`/`visibility` (nunca `WorldRow(visibility=…)`); sin `sleep()`; sin fixtures compartidos; sin tocar esquema global.

### 3.2 Suites de verificación

| Suite | Resultado | Estado |
|---|---|---|
| `tests/test_world_deletion.py` | **17/17 passed** | sin cambios |
| `tests/test_deleted_world_repository.py` (B3-1) | **12/12 passed** | sin cambios |
| `tests/test_deleted_world_recovery.py` (B3-2) | **7/7 passed** | sin cambios |
| Batería lógica combinada | **36 passed** | — |

### 3.3 Regresión — identidad de nombres vs línea base (post-B3-2, pre-B3-3)

| Suite | Línea base (pre) | Después (post) | Identidad de nombres |
|---|---|---|---|
| `test_server.py` | 4f/1p | **4f/1p** | **4/4 idénticos** |
| `test_visibility.py` | 52f/4p | **52f/4p** | **52/52 idénticos** |
| `test_etapa5_http.py` | 12f/1p | **12f/1p** | **12/12 idénticos** |

`Compare-Object` sobre los nombres `FAILED` guardados en `Temp\opencode\b33_*_failed_before.txt` vs `*_after.txt`: **cero diferencias en las tres suites** → cero fallos nuevos, cero regresiones.

### 3.4 Distinguiendo categorías (§7)

- **Fallos nuevos:** **ninguno** (identidad de nombres 100% en las tres suites de regresión; las suites lógicas 17+12+7 íntegras).
- **Fallos preexistentes reproducidos:** `test_server` 4, `test_visibility` 52, `test_etapa5_http` 12 — mismos tests, mismas causas que la línea base (raíz dominante: `WorldRow` sin mapear `visibility`/`status` y familia `ensure_schema`; ya documentadas desde 2d/BLOCK2-PRE). **No se ejecutó ningún baseline por revert en este subbloque:** la línea base fue capturada *fresca antes de editar* (mismo día, §3.3) y comparada por identidad de nombres.
- **No ejecutables por defectos preexistentes:** el comportamiento HTTP de `GET /api/worlds` (A1) y `GET` de mundo activo — declarados, no corregidos (§5).

---

## 4. Prueba del propietario por HTTP real (§5)

`test_a2_owner_http_get_deleted_is_identical_404` realiza **peticiones HTTP reales** con token `Bearer` verdadero (registro vía `/api/auth/login`… vía `/api/auth/register` real): el propietario autenticado sobre un mundo eliminado recibe **404 con `detail` byte-idéntico** al de una petición anónima sobre el mismo id y al del contrato de mundo inexistente. No es una invocación directa del helper.

---

## 5. Limitaciones debidas a defectos preexistentes (declaradas, sin corregir)

1. **A1 — endpoint no ejecutable hoy:** construir el `select` de `list_worlds` toca `WorldRow.visibility`, que **no está mapeado** (`hasattr(WorldRow, 'visibility') == False`, verificado) → `AttributeError`. Mismo defecto raíz que la línea base de `test_visibility` (52f) y de `test_server` (4f). Por eso:
   - la evidencia **primaria** de A1 es la prueba **estructural (AST)** (declarada como estructural, jamás como ejecución integral);
   - el test **comportamental** existe pero corre como **xfail no estricto** documentando el bloqueo; **no** cuenta como validación ejecutada.
2. **`GET` de mundo activo:** `_require_visible` lee `row.visibility` → `AttributeError` (preexistente). La accesibilidad de activos se acredita a nivel de helper (`_load_row`) y **end-to-end en el camino owner `PUT /visibility`** (que nunca lee `visibility`), además del AST.
3. **`POST /api/worlds`** sigue roto por `WorldRow(visibility=…)` (preexistente) → las filas de test se insertan directamente (patrón ya usado en B3-1/B3-2).
4. **`ensure_schema()`** (aridad 0 vs llamada con engine) sin tocar → sin lifespan/`run_startup` en los tests (por diseño del aislamiento).
5. **Nada de esto fue corregido** dentro de B3-3 (§6).

---

## 6. Estado Git inicial / final

| Ítem | Inicial (pre-B3-3) | Final |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` (sin commits) |
| `git status --porcelain` | 133 entradas | **135** (solo + test nuevo + informe) |
| Staged / Stash | 0 / 0 | 0 / 0 |
| SHA256 `.py` | 96 en manifiesto | 97 (96 + test nuevo); **de los 96 preexistentes, solo `app.py` difiere** |
| `worlds.db` | `5D0B27BC493A2194…` | `5D0B27BC493A2194…` (intacto) |

- Delta de status verificado: **exactamente** `?? tests/test_deleted_world_app.py` (y, tras este informe, el propio informe). Nada staged/unstaged/untracked preexistente alterado.
- Cambios preexistentes de `app.py` (1708+/93− vs HEAD) **preservados**; mis 4 ediciones añaden sus hunks sobre ellos (diff completo guardado en `Temp\opencode\b33_app_full_diff.txt`).
- Operaciones Git: solo lectura. Cero destructivas (sin commit/reset/checkout/stash/clean).

---

## 7. Confirmación de no-modificación de otros módulos (§6)

Manifiesto SHA256 de los 96 `.py` preexistentes (antes/después): **única diferencia `src/worldsim/server/app.py`** (más el test nuevo inexistente en el baseline).

- [x] `repository.py` **intacto** (hash idéntico al aceptado en B3-1).
- [x] `recovery_coordinator.py` **intacto** (hash idéntico al aceptado en B3-2).
- [x] `league.py`, `challenge_service.py`, `runner.py`, `transitions.py`, `db.py`, `auth.py`… **intactos**.
- [x] `RunnerRegistry`: `get_existing`/`get`/`register_existing`/`unregister`/`begin_delete`/`end_delete`/`safe_delete_world` **sin cambios**; los 8 callsites de `get_existing()` sin tocar.
- [x] Firma `ensure_schema()` y atributos ORM `status`/`visibility` **sin corregir**.
- [x] Sin params generales `include_deleted`/`allow_deleted`/`historical`; sin restauración (0 escrituras de `deleted_at`); DEC20/DEC21 sin cambios; DEC19 y F7-D-I1 sin reabrir.
- [x] Tests preexistentes **sin modificar** (ningún archivo de test previo alterado).
- [x] Sin arreglos oportunistas de defectos ajenos al bloque.

---

## 8. Reservas técnicas pendientes

1. **A1 ejecutable + `GET` de activo** quedan **bloqueados** hasta corregir (en un bloque autorizado) el mapeo de `WorldRow.visibility`/`status` — fuera del alcance de B3-3.
2. **2c.7 — carrera de dos workers pre-commit:** **no demostrada ni resuelta** (los filtros leen `NULL` antes del commit y verían el mundo como activo). **Esta reserva sigue abierta**; nada de B3-3 la alivia.
3. **A4 y el resto de accesos ordinarios** dependen de `_load_row`; los accesos que no pasan por él (`league.py`, `challenge_service.py`) pertenecen a bloques posteriores (B3-4…B3-7) **no autorizados aún**.
4. El **xfail de A1** debe reconvertirse en test ejecutable cuando se autorice la corrección del mapeo ORM.

---

## 9. Estado y parada

**Implementado y probado.** 11/12 pruebas nuevas verdes + 1 xfail declarado; 17+12+7 de las suites lógicas íntegras; regresión con **identidad exacta de nombres** en `test_server` (4), `test_visibility` (52) y `test_etapa5_http` (12); A5 intacto con evidencia estática **y** ejecutada; integridad verificada.

**Me detengo aquí. No se inició B3-4 (`league.py`/`challenge_service.py`) ni ningún subbloque posterior.** Entregado para revisión — pendiente de autorización expresa.
