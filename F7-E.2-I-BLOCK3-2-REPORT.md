# F7-E.2-I-BLOCK3-2-REPORT — Exclusión de mundos eliminados durante startup (`recovery_coordinator.py`)

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK3-2 — filtro `WorldRow.deleted_at.is_(None)` en `_list_world_rows()`.
**Estado:** **implementado y probado** (no aceptado — pendiente de revisión).
**Punto de parada:** al terminar este informe. **B3-3 no fue iniciado.**

## 0. Archivos

| Archivo | Estado en este subbloque |
|---|---|
| `src/worldsim/server/recovery_coordinator.py` | **modificado** (único archivo de producción; solo `_list_world_rows()`) |
| `tests/test_deleted_world_recovery.py` | **nuevo** (único archivo de test) |
| `F7-E.2-I-BLOCK3-2-REPORT.md` | **nuevo** (este informe) |
| Cualquier otro archivo | **intacto** (verificado por manifiesto SHA256, §7) |

---

## 1. Diff exacto de `_list_world_rows()` (`recovery_coordinator.py:461`)

```diff
     def _list_world_rows(self) -> list[WorldRow]:
         with self.session_factory() as session:
-            rows = session.scalars(select(WorldRow).order_by(WorldRow.id)).all()
+            # F7-E.2: startup never recovers nor registers runners for
+            # logically deleted worlds (DEC-PRE-2, decision 2); the
+            # exclusion is part of the SQL query, not an in-memory filter.
+            rows = session.scalars(
+                select(WorldRow)
+                .where(WorldRow.deleted_at.is_(None))
+                .order_by(WorldRow.id)
+            ).all()
             # Attributes are already loaded; detached access is safe.
             return list(rows)
```

**Único cambio en todo el archivo** (verificado: el resto de `recovery_coordinator.py` es byte-idéntico — la única línea afectada es el cuerpo de este método).

**Preservación (§2):**
- **Orden:** `order_by(WorldRow.id)` conservado tras el `where` (el orden de cláusulas no altera la semántica SQL); test 4 asciende `[active_a, active_b]`.
- **Columnas:** `select(WorldRow)` intacto (misma proyección).
- **Sesiones:** `with self.session_factory() as session` intacto.
- **Errores:** sin `try`/`except` nuevos — las mismas excepciones se propagan igual.
- **Resto del flujo:** `_stage_a`, `_stage_b`, `run_startup`, `RunnerRegistry`, `get_existing()`, `register_existing()`, `safe_delete_world()` — **cero cambios**.
- **Semántica de activos:** los mundos activos siguen enumerándose, recuperándose y registrándose (tests 2, 4 y 5).
- **Filtro en SQL, no en memoria:** el predicado está dentro de la consulta (§2 y evidencia en §3).

---

## 2. Evidencia de que el filtro se ejecuta en SQL

Test `test_list_world_rows_sql_contains_deleted_at_is_null` adjunta un listener `before_cursor_execute` al engine y captura **la sentencia que `_list_world_rows()` realmente envía a SQLite**:

```text
… SELECT worlds.id, worlds.seed, … FROM worlds WHERE worlds.deleted_at IS NULL ORDER BY worlds.id
```

Aserciones: (a) existió un `SELECT … FROM worlds` capturado; (b) la sentencia contiene `deleted_at IS NULL`; (c) el resultado consistente con el predicado (solo la fila activa). Un filtrado posterior en memoria **no** aparecería en la sentencia capturada — por eso esto es evidencia de ejecución SQL, no solo de resultado.

---

## 3. Pruebas nuevas — `tests/test_deleted_world_recovery.py` (7/7 verdes)

Aislamiento (§3): SQLite propio por test con `Base.metadata.create_all` (sin `ensure_schema()`, sin `status`/`visibility`, fixtures propios, cero cambios de esquema global).

| # | Test | Cubre | Resultado |
|---|---|---|---|
| 1 | `test_list_world_rows_sql_contains_deleted_at_is_null` | (1) predicado `deleted_at IS NULL` **en la SQL ejecutada** (§2) | PASSED |
| 2 | `test_list_world_rows_returns_active_row` | (2) fila activa aparece (`deleted_at is None`) | PASSED |
| 3 | `test_list_world_rows_excludes_deleted_row` | (3) fila eliminada excluida → `[]` | PASSED |
| 4 | `test_list_world_rows_mixed_returns_only_active_in_order` | (4) colección mixta → solo activas, orden ascendente preservado | PASSED |
| 5 | `test_stage_a_does_not_recover_or_register_deleted_rows` | (5) punto de consumo (§4) | PASSED |
| 6 | `test_stage_a_with_real_recovery_manager_makes_zero_attempts_on_deleted` | (5) flujo real + barrera doble RM1 (§4) | PASSED |
| 7 | `test_startup_filter_is_in_list_world_rows_query` | corroboración estructural (AST) del ubicación del predicado | PASSED |

```text
7 passed in 2.00s
```

---

## 4. Qué parte del flujo de startup se probó realmente

**Ejecutado de verdad (código de producción real):**
- `RecoveryCoordinator.__init__` y `_list_world_rows()` — consulta SQL real contra SQLite (capturada, §2).
- **`RecoveryCoordinator._stage_a()` completo** (real): bucle de enumeración, construcción real de `OperationService`, creación de runner vía `runner_factory`, bookkeeping real (`recovery_results`, `_attempt_runners`, `_attempt_status`) y punto de consumo `registry.register_existing(...)` (línea 452).
- **`RecoveryManager.recover()` real** (test 6, vía subclase spy de solo observación): con BD solo-eliminada, `_stage_a()` completa con **cero** llamadas a `recover`; además, una llamada directa real `recover(id_eliminado)` devuelve `not_recoverable` con `"World {id} does not exist"` (barrera doble RM1 vía el filtro R1 de B3-1) sin tocar la fila ni registrar nada.

**Test 5 (punto de consumo), resultado:**
```text
recover_calls      == [id_activo]     # intento de recuperación SOLO para el activo
runner_factory     == [id_activo]     # construcción de runner SOLO para el activo
registry.registered== [id_activo]     # registro SOLO para el activo
recovery_results   == {id_activo}     # el id eliminado nunca aparece en ningún libro
id eliminado: ausente de recover_calls, _attempt_runners y recovery_results
```

**Stubbed (documentado, sin modificar producción):** en el test mixto (5), el *eligibility engine* es un doble que registra llamadas (el `RecoveryManager` real lee `world_row.status`, atributo no mapeado — defecto preexistente — y abortaría con un mundo activo); `registry` y `runner_factory` son dobles de observación (el `RunnerRegistry` real exige `isinstance(runner, WorldRunner)`). Las aserciones son sobre **intentos** (argumentos de las llamadas), que es exactamente lo que pide §3.5.

**NO probado (bloqueado por defectos preexistentes — §5):** el flujo completo `run_startup()` (Etapas A+B de punta a punta) y `_stage_b`.

---

## 5. Limitaciones debidas a defectos preexistentes (declaradas, sin corregir)

1. **`run_startup()` no es ejecutable:** línea 831 `db.ensure_schema(engine)` contra `def ensure_schema() -> None` (0 parámetros) → `TypeError` preexistente (una de las raíces de los 35 fallos de la suite). Por eso el flujo completo de lifespan **no** se probó; §3.5 lo autoriza: prueba aislada de `_list_world_rows()` + verificación del punto de consumo, **sin presentar nada estructural como ejecución integral**.
2. **`RecoveryManager` real + fila activa:** `world_row.status` (línea 104 de `recovery_manager.py`) es atributo no mapeado → `AttributeError` preexistente. Impide el flujo real mixto (activo+eliminado) en `_stage_a`; el test 6 usa el manager real **solo** con BD solo-eliminada (donde `recover` ni se invoca, o devuelve `not_recoverable` sin tocar `.status`).
3. **`register_existing` exige `WorldRunner` real:** doble de observación en su lugar (§4).
4. Test 7 es **estructural (AST)** y se declara como tal; no sustituye la evidencia SQL del test 1.
5. **Reserva 2c.7 — no aliviada:** antes del commit de `deleted_at`, esta enumeración sigue viendo `deleted_at IS NULL` y recupera el mundo (correcto: aún no es durable). La carrera de dos workers con lecturas previas al commit **sigue sin demostrarse resuelta**; nada de este subbloque afirma lo contrario.

---

## 6. Comparación con la línea base (identidad de tests Y causas, §5)

**Suite `tests/test_recovery_coordinator.py`:**

| Momento | Resultado | Causas |
|---|---|---|
| Línea base 2d (informe 2d §4) | 35f/2p, causa dominante `TypeError: 'status' … WorldRow` | familia documentada |
| Captura **antes** de editar (19/11, 12:5x) | **35f/2p** — 35 nombres guardados | — |
| **Replay baseline** (edición revertida temporalmente, §7) | **35f/2p** | **34 × TypeError + 1 × AssertionError** |
| **Después de B3-2** | **35f/2p** | **34 × TypeError + 1 × AssertionError** |

- **Nombres: identidad EXACTA 35/35** en las tres capturas (antes ↔ revert ↔ después; `Compare-Object` sin diferencias).
- **Causas: idénticas** (34 TypeError + 1 AssertionError) entre replay baseline y después → **cero regresiones, cero fallos nuevos, cero causas cambiadas**.
- El único `AssertionError` es `test_i_02_importar_db_no_ejecuta_ensure_schema` (subprocess que solo ejecuta `import worldsim.server.db`) — imposible de influenciar por un `WHERE` en `recovery_coordinator.py`; y ya fallaba en la captura previa a la edición (mismo nombre).
- Las 34 `TypeError` son las raíces preexistentes documentadas (`'status' is an invalid keyword argument for WorldRow` y familia `ensure_schema`), que ocurren en fixtures/arranque **antes** de ejecutar `_list_world_rows()`.

**Otras suites (§5):**

| Suite | Línea base | Después | Delta |
|---|---|---|---|
| `tests/test_world_deletion.py` | 17/17 | **17/17** | 0 |
| `tests/test_deleted_world_repository.py` (B3-1) | 12/12 | **12/12** | 0 |
| Batería combinada de las 3 suites de B3-1/B3-2 + deletion | — | **36 passed** | — |

---

## 7. Técnica del replay baseline (control de integridad)

Para comparar **causas** (no solo nombres), se revirtió temporalmente ÚNICAMENTE la edición de `_list_world_rows()`:
1. Backup del archivo editado → Temp (`b32_rc_edited_backup.py`, SHA256 `6FDCBCAF7FA60A76E98C41E7BF9C81A2D8168D10BEE6F6EF54C415F864A9AF6A`).
2. Revert textual exacto de la edición → suite con `--tb=line` → causas baseline guardadas.
3. Restauración desde backup **en bloque `try/finally`**.
4. **Verificación:** hash del archivo restaurado == hash del backup (byte-idéntico) y `git diff --no-index` contra el backup = sin diferencias. La edición B3-2 quedó reintegrada exactamente.

Ninguna operación Git destructiva; el revert fue un edit de texto local reversible, verificado por hash.

---

## 8. Git antes y después

| Ítem | Antes | Después |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` (sin commits) |
| `git status --porcelain` | 131 entradas | **133** (solo + test nuevo + informe) |
| Staged / Stash | 0 / 0 | 0 / 0 |
| SHA256 de `.py` | 95 en manifiesto | 96 (95 + test nuevo); **de los 95 preexistentes, solo `recovery_coordinator.py` difiere** |
| `worlds.db` | `5D0B27BC493A2194…` | `5D0B27BC493A2194…` (intacto) |

Operaciones Git: solo lectura. Cambios staged/unstaged/untracked preexistentes preservados (delta de status = exactamente los 2 archivos nuevos de este subbloque).

---

## 9. Confirmaciones (§4)

- [x] Modificado **exclusivamente** `_list_world_rows()` con `WorldRow.deleted_at.is_(None)` **dentro de la consulta SQL** (evidencia §2).
- [x] Orden, columnas, sesiones, errores, resto del flujo y semántica de activos preservados.
- [x] `repository.py` **ni sus tests** modificados (hash idéntico al de B3-1).
- [x] `app.py`, `runner.py`, `transitions.py` ni ningún otro módulo modificados (manifiesto: solo `recovery_coordinator.py` difiere).
- [x] Firma de `ensure_schema()` sin tocar; atributos `status`/`visibility` de `WorldRow` sin tocar.
- [x] 8 callsites de `get_existing()` sin cambios; `safe_delete_world()` sin cambios (`runner.py` hash idéntico).
- [x] Sin arreglos oportunistas; tests preexistentes sin modificar.
- [x] B3-3 **no** iniciado; sin Git destructivos.

---

## 10. Estado y parada

**Implementado y probado.** 7/7 tests nuevos verdes (incluye evidencia SQL capturada); recovery suite **35f/2p con identidad exacta de nombres y causas** vs línea base (replay verificado por hash); `test_world_deletion.py` 17/17; `test_deleted_world_repository.py` 12/12; integridad verificada.

**Me detengo aquí. No se inició B3-3 (`app.py`) ni ningún subbloque posterior.** Pendiente de revisión y autorización expresa.
