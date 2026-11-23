# F7-E.2-I-BLOCK2-2B-REPORT — Adaptación del harness de eliminación lógica

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK2-2B (adaptación exclusiva del harness, tras la
aceptación de 2a).
**Alcance ejecutado:** un único archivo, `tests/test_world_deletion.py`.
Solo adaptación de harness: **sin tests nuevos (2c no iniciado), sin regresión
(2d no ejecutado), sin Bloque 3.**

---

## 1. Archivos modificados y diff resumido

| Archivo | Cambio propio en 2b | Estado |
|---|---|---|
| `tests/test_world_deletion.py` | **Único** — harness + expectativas localizadas (19 ediciones, §1.1) | Editado (archivo **untracked** preexistente) |
| Cualquier otro archivo | **Ninguno** | Intacto |

Evidencia de unicidad: barrido de mtimes de `*.py` en `src/` y `tests/` con
corte en 19/11 10:00 ⇒ solo `runner.py @ 10:02` (**2a**, previo) y
`test_world_deletion.py @ 10:19` (**2b**). `runner.py` conserva **idéntico** su
diffstat de fin de 2a (**944+/16−**) ⇒ no fue tocado en 2b.

> **Nota metodológica:** `tests/test_world_deletion.py` está **untracked** en
> HEAD (`??`), por lo que `git diff HEAD` sobre él es vacío y no puede mostrar
> el cambio. El "diff resumido" se lista abajo con las ediciones exactas
> ejecutadas (todas verificables contra el archivo actual), distinguiendo lo
> propio del contenido preexistente.

### 1.1 Diff resumido (ediciones localizadas)

**A. Harness (núcleo de la adaptación):**

| # | Ubicación | Antes | Después |
|---|---|---|---|
| 1 | Docstring `_DeleteHarness` | `ops records get/delete calls` | `ops records get/mark calls` |
| 2 | `on_commit()` comentario | `row stays intact: pop() never runs` | `rollback: la marca lógica no persiste` |
| 3 | `_FakeSession` (clase completa) | `get()` + **`delete()`** + `commit()` con `store.pop(...)` | ver §2 (sin `delete()`, sin `pop`, con undo/rollback y op `mark`) |

**B. `_FakeRow`:**

| # | Ubicación | Antes | Después |
|---|---|---|---|
| 4 | `_FakeRow.__init__` | `id`, `owner_user_id` | + `self.deleted_at = None  # eliminación lógica: None = mundo activo` |

**C. Expectativas de los tests (solo donde el contrato cambió):**

| # | Test | Antes | Después |
|---|---|---|---|
| 5 | docstring módulo (numeración 3 y 10) | "borrado persistente" | "marcado lógico persistente" / "persistencia de la marca lógica" |
| 6 | del_01 | `assert h.store == {}` · op `("delete", 7)` · docstring "borrado" | `assert 7 in h.store and h.store[7].deleted_at is not None` · op `("mark", 7)` · docstring "marcado lógico" |
| 7 | del_02 | `assert h.store == {}` · docstring/comentario "borrado/deleted" | fila presente + marcada · "marcado"/"marked" |
| 8 | del_03 | `assert h.store == {}` · probe "data was deleted" | fila presente + marcada · probe "row was marked"/"persisted mark" |
| 9 | del_04 (×2) | `assert 7 in h.store` | `assert 7 in h.store and h.store[7].deleted_at is None` (íntegro = **sin marca**) |
| 10 | del_05 (×2) | `assert 7 in h.store` · `assert h.store == {}` | tras fallo: presente + `deleted_at is None` (rollback) · tras retry: presente + marcada |
| 11 | del_06 | `pytest.raises(WorldNotFoundError)` en 2ª · `commit == 1` · `store == {}` | 2ª **idempotente con éxito** · `commit == 2` · `("mark",7)` == 1 · fila presente + marcada · **timestamp idéntico** (`is first_mark`) · comentario que documenta el motivo (§3) |
| 12 | del_07 | `assert h.store == {}` | fila presente + marcada |
| 13 | del_08 | `assert h.store == {}` | fila presente + marcada |
| 14 | del_09 (×2) | `assert 7 in h.store` · `assert h.store == {}` | tras cancel: presente + `is None` (intacta) · tras retry: presente + marcada |
| 15 | del_10 (×2) | `assert 7 in h.store` · `assert h.store == {}` | en vuelo: presente + marcada (UPDATE ya asignado, commit pendiente) · al final: presente + marcada |
| 16 | docstrings/comentarios de del_03/05/10 | "borrado persistente / row NOT deleted / persisted delete completed" | "marcado persistente / mark rolled back / logical mark completed" |

Contenido **preexistente preservado sin cambios**: docstring de cabecera del
módulo (excepto la numeración 3/10), imports, `_SpyRegistry`, `_StubbornMain`,
`_runner_for`, `_assert_guard_released`, y toda la orquestación asíncrona de
los 10 tests (barreras, eventos, cancelaciones, monkeypatch de `CLOSE_TIMEOUT`).

---

## 2. Adaptaciones del fake row y del fake session

### `_FakeRow`
```python
self.deleted_at = None  # eliminación lógica: None = mundo activo
```
Atributo inicial `None`, presente antes de cualquier eliminación.

### `_FakeSession` (reescrito para reflejar el ciclo de sesión real)

El código real asigna `row.deleted_at` **directamente sobre el objeto** devuelto
por `get()` (SQLAlchemy fija el atributo de inmediato y emite el UPDATE en el
commit). El fake modela el ciclo de sesión alrededor:

1. **`get()`** — registra `("get", id)` y hace **snapshot** del valor de
   `deleted_at` (el punto de deshacer), como la lectura que fija la fila en la
   sesión.
2. **`commit()`** — registra el evento `"commit"` (misma posición de siempre
   para las aserciones de orden), ejecuta `on_commit()` (probe/bloqueo/error) y,
   si la marca **cambió** respecto al snapshot, registra la op
   `("mark", id)` = *marca lógica persistida*. Una repetición idempotente
   (marca sin cambio) **no** registra op.
3. **`__exit__` con excepción** — si el commit falló (`commit_error`), restaura
   el valor del snapshot: modela el rollback implícito de SQLAlchemy. Verificado
   por sonda: `commit_error` ⇒ `deleted_at is None` y la fila permanece presente.
4. **Sin `delete()` ni `store.pop()`**: no existe ninguna vía de borrado físico
   en el harness (verificado por AST: cero atributos `.delete`, cero
   `store.pop`, cero `store == {}`, cero ops `("delete", …)`).

### Cobertura de los requisitos de comportamiento (§2 del encargo)

| Requisito | Cómo queda representado | Evidencia |
|---|---|---|
| La fila existente permanece almacenada tras eliminarla | El store nunca pierde la clave | del_01/02/03/05/07/08/09/10 (verde) |
| La fila queda marcada con valor no nulo | `deleted_at is not None` | mismos tests (verde) |
| Una segunda eliminación no elimina la fila ni altera el timestamp | del_06: fila presente, `deleted_at is first_mark` (identidad), op `mark == 1`, `commit == 2` | del_06 (verde) |
| Fila inexistente ⇒ `WorldNotFoundError` | `get()` devuelve `None` ⇒ el código real lanza (runner.py:1031); **mecanismo intacto** — su ejercicio como test corresponde a 2c (PRE §8.2 #5) | sonda inline abajo (verde) |
| `commit()` representa la persistencia de la marca | op `("mark", id)` registrada en commit exitoso; evento `"commit"` conservado en la posición de orden | del_01/03/06 (verde) |
| Sin SQL `DELETE` ni `session.delete(row)` | Método `delete()` eliminado; verificación AST/texto | §5.4 (verde) |
| Sin filtros de lectura ni restauración | Ningún `deleted_at` filtrado en el harness; `store` nunca se restaura desde marca | §5.4 (verde) |

---

## 3. Correspondencia de los diez tests (antes → después)

| # | Nombre (inalterado) | Objetivo semántico (conservado) | Cambio de expectativa y motivo |
|---|---|---|---|
| 1 | `test_del_01_delete_without_runner` | Validación + persistencia sin runner; guard liberado; sin trabajo huérfano | `store == {}` → **fila presente + marcada**; op `delete` → `mark` — el contrato pasó de desaparición a marca lógica |
| 2 | `test_del_02_delete_with_active_runner` | Runner cerrado → retirado → datos persistidos | igual: persistencia ahora = fila presente + marcada |
| 3 | `test_del_03_close_happens_before_persisted_delete` | Orden exacto `close → unregister → commit` | secuencia de eventos **intacta**; solo el estado final del store cambió |
| 4 | `test_del_04_close_failure_keeps_persistence_intact` | Fallo de cierre ⇒ persistencia jamás tocada | "íntegro" **estrechido**: presente **y sin marca** (`deleted_at is None`) — más fuerte y fiel al nuevo contrato |
| 5 | `test_del_05_persistence_failure_after_close` | Error conservado; runner no recreado; retry re-solo persistencia | fallo ⇒ marca **revertida** (rollback del fake); retry ⇒ fila presente + marcada |
| 6 | `test_del_06_simultaneous_deletes` | Serialización por mundo de dos eliminaciones | **Cambio de contrato documentado:** la 2ª ya no puede recibir `WorldNotFoundError` (la fila nunca desaparece): ahora es **no-op idempotente exitoso** (DEC-PRE-3), conservando la 1ª marca (`is first_mark`), `commit == 2`, `mark == 1` |
| 7 | `test_del_07_delete_concurrent_with_runner_access` | Acceso rechazado durante la marca; runner visible hasta el final | solo el estado final del store cambió; toda la parte de registro intacta |
| 8 | `test_del_08_no_new_runner_while_deleting` | `get()`/`register_existing()` rechazan durante la eliminación | solo el estado final del store cambió |
| 9 | `test_del_09_cancellation_during_close` | Cancelación coherente + retry seguro | "intacta" = presente **sin marca**; retry ⇒ presente + marcada |
| 10 | `test_del_10_cancellation_during_persisted_delete` | El trabajo persistente no se abandona; guarda transferida | en vuelo: marca ya asignada en el objeto (fiel a SQLAlchemy) y commit pendiente; al completarse: fila presente + marcada |

Ningún test eliminado ni renombrado; ninguno añadido.

---

## 4. Resultado real de la ejecución

**Corrida principal (verbose):**
```
collected 10 items
test_del_01_delete_without_runner  PASSED   ... test_del_10_cancellation_during_persisted_delete PASSED
============================= 10 passed in 0.99s ==============================
```

**Corrida final (tras añadir la aserción de timestamp en del_06):**
```
..........  [100%]
10 passed in 0.71s
```

**Sondas inline (sin añadir tests, ejecutadas con el harness real):**
- Fila **inexistente** → `WorldNotFoundError` propagada desde
  `runner.py:1031` a través de `safe_delete_world` (los tracebacks impresos son
  el log deliberado "persisted world delete failed … never swallowed" del propio
  `runner.py`, no fallos): `ops=[('get', 7)]`, `commits: 0` ✓
- `commit_error` → `rollback -> deleted_at: None | fila presente: True` ✓
  (rollback del fake fiel al de SQLAlchemy)

**Fallo registrados:** **ninguno** en esta suite. La línea base del PRE era
también 10/10 (con semántica física), de modo que no hay fallos que distinguir
como preexistentes **dentro de esta suite**. Los fallos preexistentes de otras
suites (columnas `status`/`visibility` ausentes y `ensure_schema(engine)`,
PRE §7.1) **no se tocaron, no se ocultaron y no se adjudican a 2b** — ninguna
corrida de esas suites se modificó ni se incluyó en este entregable.

---

## 5. Verificaciones ejecutadas (§5 del encargo)

| # | Exigida | Resultado |
|---|---|---|
| 1 | `tests/test_world_deletion.py` completo | **10 passed** (dos corridas) |
| 2 | Los diez tests originales siguen presentes | AST: **exactamente 10** funciones `test_del_*`, nombres idénticos a la línea base; pytest: `collected 10 items` |
| 3 | Diff confina al archivo autorizado | mtimes (§1) + diffstat de `runner.py` sin variación (944+/16−) + `git status` sin entradas nuevas |
| 4 | Sin `session.delete()` ni expectativas de borrado físico | Texto/AST del archivo: `session.delete` **False**, `.delete` attribute **False**, `def delete(` **False**, `store.pop` **False**, `store == {}` **False**, ops `("delete", …)` **False** |
| 5 | Fallos registrados y distinguibles | Sin fallos en la suite; sin intervención fuera de alcance |

---

## 6. Estado Git

| Ítem | Valor |
|---|---|
| Branch / HEAD | `feature/engine-checkpoints` / `e6f6983` — **sin cambios** |
| Entradas `git status --porcelain` | **124** (igual que al cerrar 2a; el archivo ya era untracked, editarla no altera el conteo) |
| Índice (staged) | **vacío** (nada staged; se preservan las entradas intent-to-add preexistentes `A` de checkpoint sin tocarlas) |
| Stash | vacío |
| Operaciones Git ejecutadas | Solo lectura (`status`, `diff`, `rev-parse`, `stash list`); **cero** operaciones destructivas |

**Propio vs preexistente:** propio = las 19 ediciones de §1.1 sobre
`tests/test_world_deletion.py` (único `.py` con mtime de 2b). Preexistente =
todo lo demás, incluidos los 935+/16− originales de `runner.py` (2a dejó
944+/16− y aquí permanece idéntico), los demás modificados/untracked del
worktree y las entradas intent-to-add.

---

## 7. Confirmaciones expresas

- **2c NO iniciado:** se mantienen exactamente **10** tests; los 7 tests nuevos
  del PRE §8.2 (idempotencia, no-sobrescritura, tz-aware, 404 por ausencia,
  rollback de marca, etc.) **no existen todavía**.
- **2d NO ejecutado:** no se corrió la batería de regresión completa del PRE §8.3.
- **Bloque 3 NO tocado:** ningún filtro de `deleted_at` introducido (grep en
  `src/` invariable: `db.py:38` + `runner.py` de 2a); ningún archivo fuera de
  alcance modificado; DEC19 y F7-D-I1 intactos.
- **`runner.py` y `db.py` sin modificar en 2b** (diffstat y mtimes como prueba).
- Import de `WorldNotFoundError` **conservado** en la suite: es el símbolo del
  contrato 404 del módulo y su ejercicio como test corresponde a 2c (PRE §8.2 #5);
  en el código actual solo se menciona en el comentario de del_06 que explica
  por qué la 2ª eliminación ya no lo provoca.
- Documentación de la única expectativa **sustituida** (del_06): motivada por el
  cambio contractual de 2a (sin borrado físico no hay ausencia de fila) y
  anotada en el propio test (§3 de este informe).

---

## Dictamen del subbloque 2b

- **Implementado y probado:** harness adaptado a eliminación lógica; **10/10
  verdes** con semántica nueva; mecanismos de rollback, idempotencia y
  `WorldNotFoundError` verificados por sonda.
- **Alcance respetado:** un archivo; sin 2c/2d/Bloque 3; sin cambios Git
  destructivos; cambios preexistentes preservados.
- **Pendiente:** 2c (batería nueva), 2d (regresión vs línea base) y Bloque 3
  (filtros → DEC-PRE-4).

**Estado:** entregable de 2b completo. **Se detiene aquí — no se inicia 2c, 2d
ni el Bloque 3. Esperando revisión y autorización expresa.**
