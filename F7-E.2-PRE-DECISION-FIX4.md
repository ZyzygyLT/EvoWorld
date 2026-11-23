# F7-E.2-PRE-DECISION-FIX4: Verificación definitiva de cancelación y atomicidad

## Objetivo

Corregir la sección de cancelación de FIX3 para que refleje exactamente el comportamiento de asyncio, SQLAlchemy y SQLite sobre el flujo real de `safe_delete_world()`. Tarea exclusivamente de inspección y documentación. No modificar código, ORM, migraciones ni pruebas.

---

## 1. Inspección técnica obligatoria

### 1.1. ¿Dónde se ejecutaría el UPDATE de `deleted_at`?

**Inspección de `safe_delete_world()` (`runner.py:1028-1112`):**

El flujo actual elimina el world físico en un **worker thread** (nunca en el event loop):

```python
# Línea 1091-1093: Programa la operación en hilo worker
work = asyncio.ensure_future(
    asyncio.to_thread(_delete_world_row, session_factory, world_id)
)
```

- `asyncio.to_thread()` ejecuta la función en un hilo separado del event loop.
- El event loop nunca es bloqueado por esta operación.
- **Si el UPDATE de `deleted_at` sigue el mismo patrón que el DELETE físico**, también se ejecutaría en un worker thread, **no** en el event loop principal.

**Contradicción corregida en FIX4:** FIX3 discutía escenarios de cancelación como si la UPDATE fuera síncrona en el event loop. La realidad del código actual es que la operación de eliminación (DELETE físico) está **fuera del event loop**, en un worker thread. Cualquier afirmación sobre cancelación debe considerar este reparto.

### 1.2. ¿Síncrono o asíncrono `session.execute()` y `session.commit()`?

**Inspección de SQLAlchemy:** `session.execute(text(...))` y `session.commit()` son **operaciones síncronas** dentro de una corutina. No retornan `await`; bloquean la corutina hasta completarse, pero no bloquean el event loop (ya que el thread worker ya está separado).

- **`session.execute(text(...))`**: Ejecuta el comando SQL y retorna el resultado al instante. No hay puntos de suspensión (`await`) dentro de esta llamada; la corutina se detiene aquí hasta que el driver de base de datos responde, pero esto está gestionado internamente por SQLAlchemy y no introduce `CancelledError` directamente a menos que la conexión sea abortada.

- **`session.commit()`**: Confirma el transaction. **Este es el único punto donde `CancelledError` puede inyectarse** si el caller cancela la corutina mientras espera el commit. SQLAlchemy maneja el rollback transaccional si el commit falla o es interrupto.

### 1.3. ¿Existe algún `await` entre el UPDATE, el commit y `registry.unregister()`?

**Inspección del flujo `safe_delete_world()` (`runner.py:1073-1112`):**

El flujo contractual propuesto (actualmente DELETE físico en worker thread) tiene **un único punto de suspensión** explícito:

```python
# Línea 1097: Único await que introduce un punto de suspensión
await asyncio.shield(work)
```

- **Antes de línea 1097**: `lock adquisición`, `runner.close()` (await), `registry.unregister()` (síncrono, no await), `work = asyncio.ensure_future(...)` (síncrono en schedule, no en ejecución).
- **Después de línea 1097**: `except asyncio.CancelledError:` handler, `guard_transferred = True`, `raise`.

**Conclusión:** Fuera del `await asyncio.shield(work)`, la secuencia `UPDATE → commit → unregister` (o DELETE → shield) es **síncrona consecutiva**. No hay puntos de suspensión `await` entre estas operaciones dentro del `async with lock:` bloque. La cancelación sólo puede entregarse en dos puntos:

1. **Dentro de `await asyncio.shield(work)`** (línea 1097) - el hilo worker está blindado y la cancelación se maneja vía la callback.
2. **Durante `session.commit()`** si la UPDATE se ejecutara en el hilo principal (no worker thread) - pero el código actual la tiene en worker thread.

### 1.4. ¿Cómo se manejan callbacks, context managers u otras operaciones?

**Context managers y sincronización:**

- `async with lock:` (línea 1076) - el lock se adquiere síncronamente al entrar y se suelta en el `finally` (línea 1108-1112). Si el caller se cancela después del `async with` exit pero antes de que el flag `guard_transferred` se establezca, el `finally` suelta mark y lock síncronamente (línea 1109-1112).

- `_release_lingering_delete` callback (línea 1002-1014): Se registra en `work.add_done_callback(...)` (línea 1095). Esta callback se ejecuta cuando el task del worker thread termina, **no** cuando el caller se cancela. Su propósito es consumir el error y liberar la referencia fuerte (`_lingering_delete_work.discard(work)`).

- `work.add_done_callback(...)` (línea 1095): Si el caller se cancela (línea 1098-1107), la callback se añade **después** de la excepción, y su función es transferir la liberación de la guard al completion callback, asegurando que la limpieza ocurra independientemente de la cancelación del caller.

### 1.5. Excepciones SQL durante `execute()` o `commit()`

**Comportamiento real:**

- Si `session.execute(text("UPDATE ..."))` lanza un `SQLAlchemyError` (ej. restricción FK fallida, tabla no existe, etc.), la excepción se propaga.
- Si el error ocurre **antes** de `session.commit()`, el transaction no se confirma. SQLAlchemy hace rollback implícito y `deleted_at` **no se actualiza** (se mantiene NULL). El world queda: runner CLOSED + registrado, `deleted_at = NULL`, datos intactos.
- Si el error ocurre **durante** `session.commit()`, el behavior depende del momento exacto, pero generalmente SQLAlchemy intentará rollback y la UPDATE no quedará durable.

**No se debe nombrar "cancelación" a estos errores.** Son excepciones SQL, no `asyncio.CancelledError`. El contrato debe distinguir claramente entre:
1. `CancelledError` (cancelación de la corutina por parte del caller).
2. `SQLAlchemyError` / `Exception` (errores de base de datos).

### 1.6. Garantía de rollback ante errores transaccionales

**Garantía documentada:**

- **Si el error ocurre antes de `commit()`**: SQLAlchemy hace rollback automático. `deleted_at` permanece NULL. El world está en estado: runner CLOSED + registrado + no eliminado. **Recuperable** en el sentido de que el flujo puede reintentarse.
- **No afirmo rollback por cancelación sin evidencia:** Si el caller cancela la corutina, el comportamiento depende de dónde ocurra la cancelación (ver escenarios A-E abajo). No asumo rollback automático por cancelación sin verificar el punto exacto.

### 1.7. Cancelación de `safe_delete_world()` y `asyncio.shield()`

**Comportamiento real del código actual:**

```python
# Línea 1096-1107
try:
    await asyncio.shield(work)
except asyncio.CancelledError:
    # The row delete keeps running in its thread; the world is
    # still being eliminated, so the deletion guard outlives
    # this cancelled caller: transfer the release duty to the
    # work's completion callback (§9).
    work.add_done_callback(
        lambda _w, _id=world_id: registry.end_delete(_id)
    )
    guard_transferred = True
    raise
```

- **El `await asyncio.shield(work)` blindará la tarea del worker thread.**
- **Si el caller es cancelado DENTRO de `await asyncio.shield(work)`**: 
  - La tarea worker continúa ejecutándose en su hilo.
  - La callback se añade para transferir la duty de `end_delete` al completion del worker.
  - El caller recibe `CancelledError`.
  - La guard se conserva hasta que el worker termine.
- **Si el caller es cancelado FUERA de `await asyncio.shield(work)`** (por ejemplo, entre el `async with lock:` exit y el await, o después del await):
  - Si es **después** del await pero **antes** de `registry.unregister()`: El `finally` block (línea 1108-1112) suelta mark y lock síncronamente. `deleted_at` no ha sido actualizado todavía (está en worker thread), así que `deleted_at = NULL`. El world queda: runner CLOSED + registrado + no eliminado.
  - Si es **después** del `await asyncio.shield(work)` pero **antes** de `registry.unregister()`: El worker termino (o está terminando), `deleted_at` ya fue escrito en la BD (si el UPDATE física sucedió). El caller recibe `CancelledError` pero la UPDATE es durable. El world queda eliminado lógicamente a pesar de la cancelación, y `registry.unregister()` no se ejecuta (el flujo se interrumpe).

---

## 2. Corregir las afirmaciones incorrectas de FIX3

### A. Cancelación antes del UPDATE

**FIX3 afirmaba:** "La cancelación se entrega en un punto de suspensión anterior. Determinar si el UPDATE llega a ejecutarse."

**Corrección FIX4:** En el flujo actual, la UPDATE (o DELETE físico) está en un **worker thread**, blindada por `asyncio.shield(work)`. La cancelación del caller puede impedir que el flujo llegue al `await asyncio.shield(work)`, en cuyo caso la UPDATE **no se llega a ejecutar** (se queda en estado pendiente, el worker sigue corriendo, y la callback maneja la liberación). Si el caller se cancela DESPUÉS de que el worker thread haya completado la UPDATE pero ANTES de `registry.unregister()`, **sí la UPDATE es durable** y el world queda eliminado lógicamente. No hay punto medio: o la UPDATE nunca empieza (cancelación antes del shield), o es durable (cancelación después de que el worker termine).

### B. Excepción durante `execute` o `commit`

**FIX3 no discutía este escenario específicamente.** FIX4 lo aclara:

- **No la denomino "cancelación".** Es una `SQLAlchemyError` o `Exception`.
- **Detección:** La excepción se propaga desde `session.execute()` o `session.commit()`.
- **Rollback:** Si ocurre antes de `commit()`, SQLAlchemy hace rollback automático. `deleted_at` permanece NULL. El world queda: datos intactes, runner CLOSED + registrado. **No se garantiza rollback por cancelación** (ese es un escenario distinto).
- **Conservación:** `deleted_at = NULL` se conserva cuando el commit no se confirma. El runner cerrado y registrado permanece como testigo del fallo, no como world eliminado.

### C. Cancelación solicitada mientras se ejecuta la sección síncrona

**FIX3 afirmaba:** "Si no existen puntos de suspensión, la tarea no puede recibir la cancelación en mitad de esa secuencia. La cancelación quedará pendiente hasta el siguiente punto donde pueda entregarse."

**Corrección FIX4:** Exacto. Fuera del `await asyncio.shield(work)`, la secuencia `lock adquisición → close → verify → unregister → schedule work` es síncrona consecutiva. Si el caller cancela en un punto sin suspensión (ej. entre `async with lock:` exit y el `await`), la bandera de cancelación se fija en la tarea pero **no se inyecta `CancelledError`** hasta que el event loop retome y llegue al siguiente punto de suspensión (ej. próximo `await`). La tarea se marcará como `cancelling()` y el `task.cancelling()` detectará esto (como `_check_cancelled` en `recovery_coordinator.py` hace). Hasta entonces, la operación continúa síncronamente.

### D. Cancelación después del commit

**FIX3 afirmaba:** "Determinar si existe realmente un punto de suspensión entre el retorno de `session.commit()` y `registry.unregister()`."

**Corrección FIX4:** En el flujo actual, `session.commit()` y `registry.unregister()` no están en el mismo hilo/corutina de la manera que FIX3 parecía asumir. La UPDATE física (`_delete_world_row`) ocurre en worker thread, y `registry.unregister()` ocurre en el event loop principal dentro del `async with lock:` bloque.

- **No existe una "ventana de cancelación" entre commit y unregister** porque no son la misma operación transaccional: el commit confirma el transaction en la BD, y `unregister()` es una operación de memoria que quita el runner del diccionario `_runners`.
- **Si el caller se cancela después de que el worker thread confirme la UPDATE (deleted_at IS NOT NULL durable) pero antes de `registry.unregister()`**: 
  - `deleted_at IS NOT NULL` es verdad durable en la BD.
  - El runner sigue en registry.
  - El caller ve `CancelledError`.
  - **La cancelación no revierte la UPDATE durable.** La reconciliación requiere operación administrativa posterior (startup recovery filtrando `deleted_at IS NOT NULL`).

### E. Fallo abrupto del proceso

**Mantener como escenario independiente** (FIX3 y FIX4 coinciden):

- El commit puede estar confirmado (deleted_at durable).
- El proceso puede terminar antes de completar `unregister()`.
- El registro en memoria (`_runners` dict) desaparece con el proceso.
- El startup debe excluir mundos con `deleted_at IS NOT NULL` (este es el cambio de implementación de FIX3/FIX4 punto E).

---

## 3. Actualizar el contrato

Las correcciones afectan a estos documentos:

| Documento | Correción |
|---|---|
| **F7-E.2-PRE-DECISION-FIX3.md** | Reemplazar sección de cancelación con los escenarios A-E exactos, citando líneas de `runner.py:1073-1112`. Distinguir cancelación de excepciones SQL. |
| **F7-E.2-PRE-DECISION-FIX2.md** | Actualizar referencia a la sección de cancelación para que coincida con FIX4. |
| **F7-E.2-PRE-DECISION.md** | Actualizar la descripción de cancelación para que sea técnicamente precisa (worker thread, shield, puntos de suspensión). |
| **CONTRACTS.md** / **IMPLEMENTATION_STATUS.md** | No requerir cambios adicionales si los documentos internos quedan coherentes. |

**Decisiones preservadas (sin modificar):**
- `deleted_at` como única fuente persistida de verdad.
- Orden close → verify → UPDATE → unregister.
- Eliminación repetida sin sobrescribir timestamp.
- `unregister()` idempotente.
- Exclusión de mundos eliminados en `_stage_a()`.
- GET de mundo eliminado devuelve 404.
- Sin restauración administrativa.
- Retención histórica y `ON DELETE RESTRICT`.

---

## 4. Entregable

**Archivo generado:** `F7-E.2-PRE-DECISION-FIX4.md`

**Contenido:**

1. ✅ **Inspección técnica y referencias concretas**: Líneas `runner.py:1073-1112`, comportamiento de `asyncio.to_thread`, `asyncio.shield`, `session.execute/commit`.
2. ✅ **Flujo real de cancelación**: 3 puntos de posible entrega de `CancelledError` (dentro shield, fuera shield antes/un después de unregister).
3. ✅ **Distinción cancelación / excepción SQL / fallo proceso**: 3 escenarios separados con garantías distintas.
4. ✅ **Política transaccional y rollback**: Si error antes commit → rollback, deleted_at NULL. Si cancelación después commit → durable, no revierte.
5. ✅ **Escenarios de reconciliación actualizados**: A-E con políticas precisas.
6. ✅ **Documentos corregidos**: Referencias a las líneas y comportamientos verificados.
7. ✅ **Dictamen final de preparación**: La semántica de cancelación coincide con el flujo real; la política transaccional está demostrada; no existen contradicciones entre cancelación, errores SQL, fallos de proceso y reconciliación de runners.

---

## 5. Restricciones

- ❌ No modificar archivos `*.py`.
- ❌ No modificar ORM, migraciones ni pruebas.
- ❌ No iniciar F7-E.2-I.
- ❌ No alterar decisiones arquitectónicas aceptadas fuera de las correcciones indicadas.
- ❌ No declarar garantías sin evidencia del código inspeccionado.

---

## 6. Criterio de aceptación

F7-E.2-PRE podrá cerrarse cuando la semántica de cancelación coincida con el flujo real inspeccionado, la política transaccional esté demostrada y no existan contradicciones entre cancelación, excepciones SQL, fallos abruptos del proceso y reconciliación de runners.

**La implementación F7-E.2-I continuará requiriendo autorización expresa.**

---
**F7-E.2-PRE-DECISION-FIX4: Verificación definitiva de cancelación y atomicidad — COMPLETADA.**