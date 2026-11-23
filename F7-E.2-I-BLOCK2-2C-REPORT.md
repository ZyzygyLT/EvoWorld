# F7-E.2-I-BLOCK2-2C-REPORT — Pruebas de comportamiento de eliminación lógica

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK2-2C (siete pruebas nuevas; los diez tests
existentes conservados íntegramente).
**Alcance ejecutado:** un único archivo, `tests/test_world_deletion.py`.
**Producción NO modificada. 2d NO iniciado. Bloque 3 NO iniciado.**

---

## 1. Identificación de los siete tests nuevos

Conteo verificado por AST: **17** funciones `test_*` en el archivo —
**10 originales** (`test_del_01`…`test_del_10`, nombres y cuerpos de 2b sin
tocar) + **exactamente 7 nuevos**:

| # | Función | Caso del encargo |
|---|---|---|
| 11 | `test_del_11_row_remains_after_logical_delete` | 2c.1 |
| 12 | `test_del_12_repeated_delete_is_idempotent` | 2c.2 |
| 13 | `test_del_13_existing_mark_is_never_overwritten` | 2c.3 |
| 14 | `test_del_14_timestamp_is_utc_aware` | 2c.4 |
| 15 | `test_del_15_missing_row_raises_and_commits_nothing` | 2c.5 |
| 16 | `test_del_16_commit_failure_keeps_row_unmarked` | 2c.6 |
| 17 | `test_del_17_cancellation_while_db_worker_in_flight` | 2c.7 |

El docstring de cabecera del módulo se extendió con el índice 11–17 (además de
la entrada de numeración 1–10 existente, intacta).

### Cambio menor de harness (neutral, documentado)

- `_DeleteHarness.__init__`: dos campos nuevos, `commit_second` (`None` por
  defecto) y `commit_arrivals = 0`.
- `on_commit()`: contador de llegadas + `commit_second.set()` **solo** en la
  2ª llegada y solo si el test configuró el evento.

Ninguno de los diez tests originales referencia estos campos ni deja de pasar:
comportamiento idéntico para ellos (17/17 lo demuestra). No se modificó ningún
test existente.

---

## 2. Qué propiedad verifica cada uno y cómo

### 2c.1 — La fila permanece después de eliminar
- **Propiedad:** tras `safe_delete_world`, la clave sigue en el almacén y
  `deleted_at` no es `None`.
- **Cómo:** `list(h.store) == [7]` (la clave nunca desaparece),
  `deleted_at is not None`, y en el exterior: op `("mark", 7)` exactamente 1,
  `commit == 1`.

### 2c.2 — Repetición idempotente
- **Propiedad:** dos eliminaciones seguidas ⇒ fila presente, el timestamp de la
  primera **idéntico** (identidad de objeto, `is first_mark`), y la segunda
  operación **no genera una marca nueva**.
- **Cómo:** captura de `first_mark` tras la 1ª; 2ª llamada completa; asserts
  `is first_mark` + `h.ops.count(("mark", 7)) == 1` (una sola marca real) y
  `commit == 2` (ambas confirman; la 2ª sin cambios) + guard liberado.

### 2c.3 — No sobrescribir una marca previa
- **Propiedad:** fila preparada con `deleted_at` fijo
  (`datetime(2020,5,4,3,2,1,987654, tzinfo=utc)`) ⇒ tras eliminar, el valor
  **no cambia**.
- **Cómo:** `assert h.store[7].deleted_at is preset` (mismo objeto), op
  `("mark") == 0` (sin marca nueva) y `commit == 1` (solo el no-op de
  confirmación).

### 2c.4 — Timestamp UTC consciente
- **Propiedad:** el valor **generado por la operación** es un `datetime`
  consciente de zona horaria y en UTC.
- **Cómo:** `isinstance(mark, datetime)` + `mark.tzinfo is not None` +
  `mark.utcoffset() == timedelta(0)` sobre el valor devuelto por el harness
  (que es exactamente lo asignado por `datetime.now(timezone.utc)`).
- **Alcance explícito (según encargo):** el test **no** afirma que SQLite
  conserve `tzinfo` al releer un `DateTime`; solo verifica el valor producido.
  La docstring del test lo declara.

### 2c.5 — Fila inexistente
- **Propiedad:** fila físicamente ausente ⇒ `WorldNotFoundError` y **ninguna
  modificación confirmada**.
- **Cómo:** `pytest.raises(WorldNotFoundError)` alrededor de la llamada real;
  `events.count("commit") == 0` (nunca se intenta commit), op `("mark") == 0`,
  `7 not in h.store`, guard y conjunto de trabajos huérfanos limpios.
  (Previamente verificado también por sonda inline en 2b.)

### 2c.6 — Fallo de commit
- **Propiedad:** la excepción se propaga, la fila sigue almacenada, la marca
  **no** queda persistida, y el fallo no se confunde con borrado físico.
- **Cómo:** `h.commit_error = RuntimeError("disk full")` →
  `pytest.raises(RuntimeError, match="disk full")` (error original intacto,
  jamás traducido en éxito); `7 in h.store` y `deleted_at is None` (rollback
  del `__exit__` del fake, fiel al rollback de SQLAlchemy);
  `events.count("commit") == 1` (se intentó y falló), op `("mark") == 0`
  (sin marca persistida), fila presente (cero borrado físico).

### 2c.7 — Cancelación durante el worker de eliminación
Ver §3. Cubre las cuatro sub-propiedades exigidas:

| Sub-propiedad | Aserciones |
|---|---|
| (1) el worker continúa tras cancelar la coroutine que lo espera | tras `dele.cancel()` + `CancelledError`: `not work1.done()`, trabajo vivo en `_lingering_delete_work`; tras liberar: `await work1` completa y `deleted_at is first_mark` (la marca asignada antes de la cancelación acaba persistida) |
| (2) la cancelación no libera prematuramente la guarda | `reg.is_deleting(7)` y `_delete_users[7] == 1` tras cancelar (refcount transferido, ni un decremento); `== 2` con la 2ª participante; `== {}` solo tras terminar los workers |
| (3) una 2ª eliminación no compite con el worker activo | barrera 2 alcanzada ⇒ `not second.done()` (no completa), `deleted_at is first_mark` (no altera la primera marca), guard marcada, `len(_lingering_delete_work) == 2` |
| (4) terminado el worker: guarda liberada y reintento procede | `_assert_guard_released(reg)` + `_lingering_delete_work == set()` + `safe_delete_world` de reintento devuelve `None` con la misma marca (`is first_mark`) |
| evidencia numérica final | `ops.count(("mark", 7)) == 1` (una sola marca real en toda la prueba) y `events.count("commit") == 3` (worker1 + 2ª no-op + reintento) |

---

## 3. Evidencia de que 2c.7 ejercita el mecanismo REAL

1. **Código real, no sustituto:** el test importa y ejecuta
   `safe_delete_world` y `RunnerRegistry` tal cual se definen en
   `worldsim.server.runner` (sin monkeypatch de esa función ni del registro).
   Único doble: la sesión persistente (`_DeleteHarness`).
2. **`asyncio.to_thread` real:** un `probe` instalado como `commit_probe`
   corre **dentro** de `session.commit()` del worker y afirma
   `threading.current_thread() is not threading.main_thread()` — la
   persistencia ocurre fuera del event loop. Si el worker no fuera un hilo
   real, la afirmación fallaría y el test conllevaría la falla.
3. **`asyncio.shield` real:** la cancelación se lanza sobre la coroutine que
   espera el `shield`; se observa `CancelledError` en el caller **y** el
   trabajo sobrevive (`work1` sin completar, referencia fuerte mantenida) —
   exactamente el mecanismo de transferencia de guarda del código
   (callback `end_delete` registrado en el `except`).
4. **Guarda real por refcount:** las aserciones sobre `_delete_users`
   (`1 → 2 → {}`), `_deleting` y `_delete_locks` leen el estado real del
   registro, incluida la transferencia de refcount al cancelar (no hubo
   `end_delete` del caller cancelado).
5. **`_lingering_delete_work` real:** transiciones observadas `0 → 1 → 2 → 0`
   (precondición limpia, dos trabajos en vuelo, drenaje final).
6. **Sincronización determinista:** barreras `threading.Event` del harness
   (`commit_started` = worker 1 dentro del commit; `commit_second` = worker 2
   dentro del commit; `commit_release` = liberación única) esperadas con
   `asyncio.to_thread(event.wait, 10)` — **sin `sleep()` como mecanismo
   principal de coordinación**; los tres `asyncio.sleep(0)` finales solo
   drenan callbacks FIFO (mismo patrón que el original del_10).
7. **Estabilidad:** 5 corridas consecutivas de 2c.7 → **5 passed**, ~0.53 s
   cada una (las esperas de10 s jamás se disparan).

---

## 4. Resultado completo de los diecisiete tests

```
collected 17 items
test_del_01_delete_without_runner PASSED          test_del_09_cancellation_during_close PASSED
test_del_02_delete_with_active_runner PASSED      test_del_10_cancellation_during_persisted_delete PASSED
test_del_03_close_happens_before_persisted_delete PASSED   test_del_11_row_remains_after_logical_delete PASSED
test_del_04_close_failure_keeps_persistence_intact PASSED   test_del_12_repeated_delete_is_idempotent PASSED
test_del_05_persistence_failure_after_close PASSED          test_del_13_existing_mark_is_never_overwritten PASSED
test_del_06_simultaneous_deletes PASSED           test_del_14_timestamp_is_utc_aware PASSED
test_del_07_delete_concurrent_with_runner_access PASSED     test_del_15_missing_row_raises_and_commits_nothing PASSED
test_del_08_no_new_runner_while_deleting PASSED   test_del_16_commit_failure_keeps_row_unmarked PASSED
                                                  test_del_17_cancellation_while_db_worker_in_flight PASSED

============================= 17 passed in 1.33s ==============================
```

**Fallores: NINGUNO.** No hay resultados que ocultar ni atribuir a problemas
preexistentes en esta suite (su línea base —10/10 en 2b, 10/10 históricamente—
sigue íntegra y ampliada a 17/17). Los fallos preexistentes de otras suites
(columnas `status`/`visibility`, `ensure_schema(engine)`; PRE §7.1) no se
tocaron ni se corrieron aquí.

---

## 5. Diferencias entre el comportamiento del fake y el real

| # | Aspecto | Fake (harness) | Real (SQLAlchemy/SQLite) | Impacto en las afirmaciones |
|---|---|---|---|---|
| 1 | Visibilidad de la marca en vuelo | Objeto **compartido**: el worker 2 ve la marca que el worker 1 asignó (aún sin confirmar) y ejecuta su guard de idempotencia | Cada sesión tiene su **propia instancia**; el worker 2 leería la BD (estado anterior al commit del worker 1) | Las afirmaciones **secuenciales** (2c.2, 2c.3, reintento) son idénticas en ambos mundos: un mark **ya confirmado** jamás se sobrescribe. En el escenario **concurrente** de 2c.7 (cancelación + 2ª eliminación con worker 1 sin confirmar), el fake muestra el guard contra la marca visible; en una BD real el worker 2 leería `NULL` y asignaría su propio timestamp (carrera de dos timestamps frescos, último-escritor-gana; nunca `NULL`, nunca borrado físico, coordinación sin cambios). **Limitación documentada** (§2.4 del encargo): eliminarla exige cambiar producción (p. ej. `UPDATE … WHERE deleted_at IS NULL` condicional), prohibido en este bloque — **no se hizo ningún cambio de producción ni se debilitó ninguna afirmación contractual**. Nótese que en del_06 (dos eliminaciones sin cancelación) la sección crítica completa del 2º caller **sí** está serializada por el lock a través del `shield`, de modo que esa carrera no existe ahí |
| 2 | Rollback de commit | `__exit__` restaura el snapshot del objeto | Rollback implícito de la sesión: la fila relée con su valor previo | Semántica observable equivalente (verificada en 2c.6 y del_05) |
| 3 | `tzinfo` en lectura | El objeto conserva el `tzinfo` con el que se escribió | SQLite devuelve `naive` al releer (probado en BLOCK2-PRE) | 2c.4 **solo afirma el valor generado** (tz-aware + `utcoffset == 0`), por diseño del encargo; ninguna prueba depende del `tzinfo` leído |
| 4 | Commits "sin cambios" | `commit()` siempre registra evento/op | SQLAlchemy no emite `UPDATE` si no hay atributos sucios; el `commit` de sesión existe igualmente | Los conteos de `commit` del harness miden **confirmaciones de sesión**, no filas SQL afectadas; las afirmaciones de marca (`("mark")`) sí distinguen escritura real vs no-op |

---

## 6. Estado Git (propio vs preexistente)

| Ítem | Valor |
|---|---|
| Branch / HEAD | `feature/engine-checkpoints` / `e6f6983` — **sin cambios** |
| Entradas `git status --porcelain` | **125** (igual que al cerrar 2b; el archivo ya era untracked) |
| Índice (staged) / stash | **vacíos** (se preservan las entradas intent-to-add `A` preexistentes sin tocarlas) |
| Operaciones Git | Solo lectura (`status`, `diff`, `rev-parse`, `stash list`); **cero** destructivas |

**Cambios propios de 2c:** solo `tests/test_world_deletion.py` (mtime
19/11 11:22) — docstring de cabecera, import de `datetime`, dos campos de
harness + lógica de llegadas en `on_commit`, y los 7 tests nuevos.

**Preservación de preexistentes:**
- `src/worldsim/server/runner.py`: diffstat **idéntico al cierre de 2a**
  (**944+/16−**) y mtime de 2a ⇒ **intacto en 2c**.
- `src/worldsim/server/db.py`: mtime 18/11 23:45 (fuera de la ventana de hoy)
  ⇒ **intacto**; su diff (120+/4−) es íntegramente preexistente.
- Barrido de `*.py` en `src/` y `tests/` con corte en 19/11 10:00 ⇒ solo
  `runner.py @ 10:02` (2a) y `test_world_deletion.py` (2b + 2c).
- Los diez tests originales: conservados (verificación AST §1) y en verde.

---

## 7. Confirmaciones expresas

- **Producción NO modificada:** cero ediciones en `runner.py`, `db.py` ni
  ningún otro `.py` fuera de `tests/test_world_deletion.py` (mtimes +
  diffstats como evidencia). No se cambió `safe_delete_world()`,
  `RunnerRegistry` ni el worker.
- **Sin filtros de lectura de `deleted_at`** introducidos (corresponden al
  Bloque 3): el archivo solo lee/escribe la marca en el propio harness y en
  los asserts de los tests.
- **Otros tests intactos:** los 10 de 2b/originales sin modificación; ningún
  test fuera de alcance tocado; ningún fallo preexistente corregido ni
  ocultado.
- **2d NO ejecutado** y **Bloque 3 NO iniciado**.
- **Límite de 2c.7 documentado (§2.4):** la sub-propiedad "el worker 2 no
  puede alterar la primera marca" se demuestra contra la marca visible del
  objeto compartido del fake; la carrera real equivalente (dos workers con
  lecturas previas al commit ajeno) exigiría un `UPDATE` condicional en
  producción para eliminarla — fuera de este alcance, declarada sin
  debilitar las afirmaciones restantes (las sub-propiedades 1, 2 y 4 y la
  no-completación del punto 3 son propiedades estructurales del mecanismo
  real y quedan verificadas contra él).

---

## Dictamen del subbloque 2c

- **Implementado y probado:** 7 pruebas nuevas, **17/17 verdes** (2c.7
  estable 5/5), propiedades y mecanismo real evidenciados, diferencias
  fake/real documentadas sin ocultamiento.
- **Alcance respetado:** un archivo; producción intacta; sin 2d; sin
  Bloque 3; sin operaciones Git destructivas.

**Estado:** entregable de 2c completo. **Se detiene aquí — no se inicia 2d ni
el Bloque 3. Esperando revisión y autorización expresa.**
