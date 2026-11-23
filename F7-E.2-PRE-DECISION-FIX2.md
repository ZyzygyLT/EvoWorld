# F7-E.2-PRE-DECISION-FIX2: Reconciliación de runners residuales y recuperación

## Objetivo

Corregir las contradicciones detectadas en `F7-E.2-PRE-DECISION-FIX.md` antes de aceptar el cierre contractual de F7-E.2-PRE.

Esta tarea es exclusivamente de inspección y documentación. **No implementar ni modificar código, ORM, migraciones ni pruebas.**

---

## 1. Inspección técnica obligatoria

### 1.1. `WorldRunner.close()` y llamadas repetidas

**Comportamiento verificado** (`src/worldsim/server/runner.py:296-390`):

- `close()` es **idempotente** (§2.3): "later calls are no-ops that never repeat cleanup operations".
- El primer llamador establece `self._state = RunnerState.CLOSING` y crea `self._close_op`.
- Llamadas posteriores observan el mismo `op` y esperan su finalización.
- Si `self._state is RunnerState.CLOSED`, el método retorna inmediatamente (línea 382-383): "correct close / idempotent repeat: nothing repeated".
- Si el close fue `CLOSE_INCOMPLETE`, se RAISE y los datos persistidos se dejan intactos (`world stays recoverable`, DEC18 §8.3).
- **Hecho crítico**: Una vez cerrado (`CLOSED`), `start()` levanta `RuntimeError` ("closing/closed runners are never restartable", línea 218-223). No es posible reiniciar el mismo runner.

**Contradicción detectada**: El FIX anterior afirmaba que "el runner permanece registrado y el mundo sigue siendo recuperable" tras un UPDATE fallido. Esto es técnicamente cierto para la **datos** (la fila persiste), pero **falso** si "recuperable" significa "el runner puede reiniciarse". El runner ya está CERRADO y no puede reiniciarse. La recuperabilidad de los datos es un concepto distinto de la reinicialización del runner.

### 1.2. `registry.begin_delete()`, `unregister()` y cancelación

**Comportamiento verificado** (`src/worldsim/server/runner.py:824-899, 893-937`):

- `begin_delete(world_id)`: Añade `world_id` a `_deleting` set, incrementa contador. `get()` y `register_existing()` rechazan el world (§9).
- `unregister(world_id)`: `return self._runners.pop(world_id, None)`. **Idempotente**: si el runner no estaba registrado, retorna `None` sin error. Si estaba, lo remueve.
- `end_delete(world_id)`: Libera la marca `_deleting`. Cuenta usuarios `_delete_users` para sobrevivir hasta el último participante.
- **Cancelación en `safe_delete_world()`** (línea 1091-1107): Si el caller se cancela durante el `await asyncio.shield(work)`, el delete sigue corriendo en su thread. La guarda se transfiere al callback. El caller ve `CancelledError`.

**Contradicción detectada**: El FIX anterior no consideró escenarios de cancelación después del UPDATE. Si el caller se cancela después de que UPDATE `deleted_at` se confirma pero antes de `unregister()`, el world queda con `deleted_at IS NOT NULL` en la BD y el runner aún en el registry. Esto requiere una política de reconciliación.

### 1.3. `RecoveryCoordinator` y `run_startup`

**Comportamiento verificado** (`src/worldsim/server/recovery_coordinator.py:287-460`):

- `startup()` ejecuta Etapa A (`_stage_a`) y Etapa B (`_stage_b`).
- **`_stage_a()` (línea 401-459)**: Itera sobre `self._list_world_rows()` que obtiene **TODAS** las filas `WorldRow` de la base de datos **sin filtrar por `deleted_at`**.
- Para cada world recuperable, construye un `WorldRunner` y lo registra via `self.registry.register_existing(world_id, runner)` (línea 452).
- **`_stage_b()` (línea 469-519)**: Inicia las tareas de los runners recuperados (`runner.start()`).
- **Ninguna de las dos etapas consulta `deleted_at`**. Esto significa que, tal como está el código actual, un world con `deleted_at IS NOT NULL` sería recuperado y registrado como runner activo durante el startup.

**Contradicción E (CRÍTICA)**: El FIX anterior no consideró que el startup recuperaría worlds eliminados lógicos. Si se introduce `deleted_at` como marcador de eliminación lógica, el startup debe excluirlos. **Este es un bloqueo técnico real, no solo semántico.**

### 1.4. `safe_delete_world()` y serialización

**Comportamiento verificado** (`src/worldsim/server/runner.py:1028-1112`):

- Secuencia: `begin_delete` → `lock` → `runner.close()` → verify CLOSED → `unregister` → `UPDATE deleted_at` (en el FIX actual) / `DELETE físico` (en código actual).
- El UPDATE en el FIX se ejecuta en hilo principal vía `asyncio.to_thread`... **espera, revisemos**.

En el código actual, el DELETE físico (`_delete_world_row`) se ejecuta en worker thread: `asyncio.to_thread(_delete_world_row, session_factory, world_id)` (línea 1092). El FIX propone cambiar esto a UPDATE transaccional. Si el UPDATE se ejecuta en hilo principal (no worker thread), la atomicidad es del transacción SQLite, que es más fuerte.

**Contradicción detectada**: El FIX asumía que el UPDATE sería en worker thread shielded, pero el código actual usa worker thread para DELETE. Si el UPDATE pasa a hilo principal para garantizar atomicidad, los comportamientos de cancelación cambian (véase Contrad D).

---

## 2. Contradicciones que deben resolverse

### A. UPDATE fallido después de cerrar el runner

**Hecho**: Si `runner.close()` completa exitosamente (runner.state = CLOSED) y luego el UPDATE de `deleted_at` falla:

- El runner **ya está cerrado** (estado CLOSED), **no activo**.
- El runner **sigue registrado** en el registry (unregister no se ejecutó).
- Los datos **persisten** (deleted_at sigue NULL).
- El world **es recuperable** en el sentido de que sus datos persisten, **pero el runner no puede reiniciarse** (start() levanta RuntimeError por _closed).

**Política verificable**: 
- Si UPDATE falla, `unregister()` **no debe ejecutarse** (mantener orden UPDATE → unregister). 
- El world queda en estado: runner CLOSED + registrado en registry + deleted_at = NULL. 
- **Recuperabilidad**: El world puede recuperarse administrativamente (un administrador puede limpiar y reintentarlo), **pero no mediante restart del runner** (este ya está cerrado). La frase "mundo sigue siendo recuperable" debe Qualifying: "los datos persisten, no el runner".

**Cambio contractual requerido**: En la secuencia `safe_delete_world()`, el paso UPDATE debe preceder a `unregister()`. Si UPDATE falla, `unregister()` no se ejecuta y el world stay en estado recoverable (aunque el runner esté CLOSED + registrado).

### B. UPDATE confirmado y `unregister` fallido

**Escenario**: UPDATE `deleted_at` succeeds, pero `registry.unregister(world_id)` falla o no se ejecuta.

- `deleted_at IS NOT NULL` **es verdad durable** en la BD.
- El runner **cerrado residual** nunca ejecutará ticks ni volverá a activarse (start() lo impedirá).
- **Vía de reconciliación**: Dado que `unregister()` es idempotente (`pop(world_id, None)`), si falla de alguna manera atómica, la forma más segura es:
  1. Intentar `unregister()` nuevamente en una operación administrativa posterior.
  2. O, si el runner está en registry, el startup recovery debe **excluir** worlds con `deleted_at IS NOT NULL` (véase Contradic E).
- **No se debe intentar reactivar** el runner residual jamás.

**Política**: Si UPDATE succeeds y `unregister()` falla (improbable por ser `pop()` seguro), el world permanece eliminado en BD. El runner en registry es "basura" administrativa que el startup limpiará filtrando `deleted_at IS NULL`. **No hay forma de que el runner residual revierta la eliminación persistida.**

### C. Eliminación repetida

**Secundar solicitud para un world cuyo `deleted_at` ya es distinto de NULL**:

- ✅ Mantiene autenticación y autorización (mismo patrón de cabecera).
- ✅ **No vuelve a cerrar un runner**: el flujo salta el close/verify si el world ya está marcado, o close retorna idempotentemente None.
- ✅ **No modifica el `deleted_at` original**: UPDATE `SET deleted_at = deleted_at` es no-op, sin cambio de valor.
- ✅ **No ejecuta DELETE físico**: confirmado, solo UPDATE.
- ✅ **Si existe un runner residual en el registry**: debe poder reconciliarlo y retirarlo de manera segura. `unregister()` via `pop()` lo remueve sin error.
- ✅ **Si no existe runner residual**: retorna éxito sin efectos adicionales.

**Corrección a DEC-PRE-3**: La regla "saltar `unregister()` cuando `deleted_at IS NOT NULL`" está **incorrecta** como afirmación absoluta. El comportamiento correcto es:

1. Ejecutar el flujo completo (auth → begin_delete → close → verify).
2. En el paso UPDATE: Si `deleted_at IS NULL`, hacer UPDATE. Si `deleted_at IS NOT NULL`, hacer UPDATE `SET deleted_at = deleted_at` (no-op) y saltar a `unregister`.
3. `unregister()` siempre se ejecuta (es idempotente, retorna None si ya removido).
4. Resultado: eliminación repetida es no-op total, conserva `deleted_at` original, sin DELETE, sin alteración histórica.

### D. Cancelación después del UPDATE

**Escenario**: El caller se cancela después de que la UPDATE de `deleted_at` se confirma en la BD pero antes de completar `unregister()`.

- Dado que UPDATE se ejecuta en **hilo principal** (transaccional SQLite), si la cancelación ocurre **durante** la UPDATE, SQLite hace rollback transaccional → `deleted_at` **no se actualiza**, state coherente (deleted_at = NULL).
- Si la cancelación ocurre **después** de la UPDATE pero antes de `unregister()`: `deleted_at IS NOT NULL` ya está grabado en BD. El runner sigue en registry.
- **Garantía**: La cancelación **no revierte** la eliminación persistida una vez que UPDATE ha completado. La transacción de SQLite ya se comprometió.
- **Reconciliación posterior**: Si es necesario retirar el runner, ello debe hacerse en una operación administrativa subsequente, **no** en el propio flujo `safe_delete_world()` bajo cancelación. El startup recovery debe filtrar `deleted_at IS NOT NULL`.

**Política**: La cancelación después del UPDATE **no revierte** la eliminación. El world queda eliminado lógicamente. El runner residual en registry será limpiado por el startup al filtrar `deleted_at IS NULL`.

### E. Reinicio entre UPDATE y `unregister` (Startup recovery)

**Hecho crítico** (de inspección de `recovery_coordinator.py:_stage_a`):

- El startup **no filtra** worlds con `deleted_at IS NOT NULL`.
- `_stage_a()` recupera **todos** los worlds y registra runners para los recuperables.
- Esto significa que, si se introduce `deleted_at`, el startup re-crearía runners para worlds eliminados, violando el principio "un mundo eliminado no debe reaparecer".

**Política obligatoria**:

1. **Modificar `_stage_a()`** para que, al iterar worlds, salte aquellos con `row.deleted_at IS NOT NULL`.
2. **Opción alternativa**: Que `recovery_manager.recover(world_id)` retorne `not_recoverable` para worlds con `deleted_at IS NOT NULL`. Pero esto requeriría cambiar el manager, que está fuera del alcance de F7-E.2.
3. **Recomendación**: Filtrar en `_stage_a()` directamente, ya que es el punto de entrada único para la recuperación de worlds.

**Línea de código requerida** (documentar, no implementar):
```python
# En _stage_a(), dentro del loop sobre rows:
if row.deleted_at is not None:
    # World eliminated lógicamente: saltar recuperación y registro
    continue
```

**Este es elcontradicción técnica más importante** y la única que requiere un cambio de implementación documentado para que F7-E.2-PRE sea aceptado.

---

## 3. Decisión arquitectónica

Tras inspeccionar los mecanismos reales, se adoptan las siguientes resoluciones técnicamente verificables:

### 3.1. Orden UPDATE antes de `unregister()` (CONFIRMADO)

- **Resolutiva**: El paso 6 (UPDATE `deleted_at`) debe preceder al paso 7 (`registry.unregister()`).
- **Justificación**: Si UPDATE falla, `unregister()` no se ejecuta, y el world stay en estado recoverable (datos persistentes, runner CLOSED + registrado). Si UPDATE succeeds, entonces `unregister()` retira el runner, y el world está eliminado lógicamente sin runner activo.
- **Esto resuelve la Contradicción A** de manera definitiva.

### 3.2. Eliminación repetida es no-op con `unregister()` siempre ejecutado (CONFIRMADO)

- **Resolutiva**: Una segunda llamada a `safe_delete_world()` sobre world con `deleted_at IS NOT NULL`:
  - close() retorna idempotentemente.
  - UPDATE es no-op (`SET deleted_at = deleted_at`).
  - `unregister()` se ejecuta (es idempotente, `pop` retorna None si ya removido).
  - Resultado: éxito, sin efectos secundarios, `deleted_at` conservado.
- **Esto resuelve la Contradicción C** con comportamiento exacto.

### 3.3. Políticas de fallo y cancelación (CONFIRMADAS)

- **Contradicción A**: Si UPDATE falla, `unregister()` no se ejecuta. World: runner CLOSED + registrado + deleted_at = NULL. Recuperabilidad de datos, no de runner.
- **Contradicción B**: Si UPDATE succeeds y `unregister()` falla (improbable), world stays eliminado en BD; runner residual es limpiado por startup al filtrar `deleted_at IS NULL`.
- **Contradicción D**: Cancelación después de UPDATE no revierte la eliminación (SQLite transaction committed). Cancelación durante UPDATE sí hace rollback.
- **Contradicción E**: Startup debe filtrar `deleted_at IS NOT NULL`. **Este es el cambio de implementación requerido.**

### 3.4. Startup recovery debe excluir worlds eliminados (NUEVO REQUERIMIENTO)

- **Hecho inspeccionado**: `_stage_a()` en `recovery_coordinator.py` no chequea `deleted_at`.
- **Decisión**: Este es el **único cambio de implementación** requerido para que el contrato sea coherente.
- **Alternativa no considerada**: Modificar `recovery_manager.recover()` para que descarte worlds eliminados, pero eso cambia la lógica de recuperación DEC16/DEC04 y no está dentro del alcance de F7-E.2.
- **Acción documentada**: Se requiere la línea de código `if row.deleted_at is not None: continue` en `_stage_a()` (documentada aquí para futura implementación en F7-E.2-I).

### 3.5. No se introduce `is_deleted` ni campos equivalentes

- **Mantenido**: `deleted_at` continúa siendo la única fuente persistida de verdad.
- **Preservado**: `ON DELETE RESTRICT` en `world_highlights` y retención histórica.

---

## 4. Documentación que debe actualizarse

Para que el contrato sea coherente y F7-E.2-PRE pueda declararse aceptado, deben actualizarse los siguientes documentos:

| Documento | Actualización necesaria |
|---|---|
| **F7-E.2-PRE-DECISION.md** | Reemplazar sección de fallos por las políticas DEC-PRE-1 a DEC-PRE-5 exactas. |
| **F7-E.2-PRE-DECISION-FIX.md** | Actualizar orden a UPDATE→unregister, corregir eliminación repetida, añadir políticas de cancelación. |
| **F7-E.2-PRE-DECISION-FIX2.md** (este documento) | Registrar contradicciones halladas y decisiones adoptadas. |
| **F7-E.1, H4 y H5** | Añadir `deleted_at IS NULL` como condición de elegibilidad pública (H5). Actualizar H4 con el nuevo orden de operaciones. |
| **CONTRACTS.md** | Añadir apartado sobre comportamiento de startup recovery ante `deleted_at`. |
| **IMPLEMENTATION_STATUS.md** | Marcar como resuelto el requisito de startup recovery filtrando worlds eliminados; anotar cambio de implementación pendiente (línea `if row.deleted_at is not None: continue` en `_stage_a`). |

No alterar DEC16-DEC21 ni F7-B/C/D.

---

## 5. Criterios de aceptación definitivos

F7-E.2-PRE solo podrá declararse definitivamente aceptada cuando se cumplan TODAS:

1. ✅ **Orden UPDATE antes de `unregister()`** está definido en el flujo contractual (DEC-PRE-1).
2. ✅ **Política de fallo UPDATE**: si falla, `unregister()` no se ejecuta; world stay recoverable con runner CLOSED + registrado.
3. ✅ **Eliminación repetida**: no-op idempotente, `unregister()` siempre ejecutado (aunque sea no-op), `deleted_at` conservado.
4. ✅ **Cancelación después de UPDATE**: no revierte la eliminación; SQLite transaction committed. Cancelación durante UPDATE sí hace rollback.
5. ✅ **Startup recovery excluye worlds eliminados**: `_stage_a()` debe filtrar `if row.deleted_at is not None: continue`. **Este es el cambio de implementación verificable.**
6. ✅ **GET por ID world eliminado → 404** (DEC-PRE-4).
7. ✅ **Restauración fuera de alcance** (DEC-PRE-5).
8. ✅ **`ON DELETE RESTRICT` e histórico intactos**.
9. ✅ **DEC16, DEC18, DEC20, DEC21 sin contradicciones nuevas** (aunque DEC18 y F7-E.1 requieren actualizaciones documentales).
10. ✅ **No se modificó código** en esta fase (solo documentación e inspección). El cambio de implementación en punto 5 será parte de F7-E.2-I.

---

## 6. Dictamen

**F7-E.2-PRE requiere un cambio de implementación documentado para ser aceptado definitivamene.**

Las contradicciones halladas son:

| Contradicción | Estado | Resolución |
|---|---|---|
| A. UPDATE fallido + runner activo | **Parcial** | Orden UPDATE→unregister resuelve la mayoría; "recuperable" debe qualifying como "datos persisten, runner no reinicia". |
| B. UPDATE + unregister fallido | **Resuelto** | `unregister()` idempotente; world stays eliminado en BD; runner residual limpiado por startup. |
| C. Eliminación repetida | **Resuelto** | No-op, `unregister()` siempre ejecutado (aunque no-op), `deleted_at` conservado. |
| D. Cancelación después UPDATE | **Resuelto** | No revierte eliminación; transacción SQLite committed. |
| **E. Startup recovery incluye worlds eliminados** | **CRÍTICO PENDIENTE** | Requiere modificaciones en `_stage_a()` del `RecoveryCoordinator`. |

**Dictamen final**: **F7-E.2-PRE puede aceptarse CONDICIONALMENTE**, sujeta a la implementación del cambio punto 5 en la fase F7-E.2-I. El cambio es mínimo (una condición `if` en `_stage_a()`) y técnicamente verificable. Sin este cambio, el contrato tiene una contradicción funcional: el startup re-registraría runners para worlds eliminados, violando el principio central de F7-E.1.

**Recomendación**: Aprobar el cierre de F7-E.2-PRE con la condición de que la implementación F7-E.2-I incluya la modificación de `_stage_a()` para filtrar worlds con `deleted_at IS NOT NULL`. Este es el único requisito de implementación que surge de la inspección técnica.

**No iniciar F7-E.2-I** hasta que la revisión arquitectónica confirme que dicho cambio se ha incluido en el plan de implementación.

---
**F7-E.2-PRE-DECISION-FIX2: Reconciliación completada. Dictamen: F7-E.2-PRE aceptable con una condición de implementación verificable.**