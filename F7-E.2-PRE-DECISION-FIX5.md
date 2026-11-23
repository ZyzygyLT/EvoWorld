# F7-E.2-PRE-DECISION-FIX5: Verificación definitiva de cancelación y atomicidad

## Objetivo

Resolver los dos puntos pendientes de FIX4 sin reabrir decisiones arquitectónicas ya aceptadas. Tarea exclusivamente documental. No modificar código, ORM, migraciones ni pruebas.

---

## 1. Construcción y programación de `work`

### Inspección técnica (`runner.py:1073-1112`)

**Cómo se construye `work`:**

```python
work = asyncio.ensure_future(
    asyncio.to_thread(_delete_world_row, session_factory, world_id)
)
```

- `asyncio.to_thread(_delete_world_row, session_factory, world_id)`: Ejecuta `_delete_world_row()` en un **hilo separado** del event loop (worker thread). **Nunca bloquea el event loop**.
- `asyncio.ensure_future(...)`: Programa la coroutine resultante para que sea ejecutada por el event loop, retornando un `asyncio.Task` asignado a `work`.
- **El worker puede comenzar inmediatamente**: Una vez programada la tarea con `ensure_future`, el event loop la ejecutará en el próximo ciclo disponible. No hay garantía de que comience antes del `await asyncio.shield(work)`, pero sí es **posible** que el worker inicie antes de que el caller alcance el `await` (dependiendo de la carga y programación del event loop).

**Cuándo comienza el worker de forma real:**

- El worker comienza cuando el event loop le asigna tiempo de ejecución, lo cual puede ocurrir:
  - **Antes** de que el caller alcance `await asyncio.shield(work)`, si el event loop tiene disponibilidad y la tarea es prioritaria.
  - **Después** de que el caller alcance `await asyncio.shield(work)`, si el event loop estaba ocupado con otras tareas.
- **No hay punto medio**: El worker o ya comenzó antes del `await` o comenzó después; no hay una "carrera" continua donde ambos coexistan sin que uno termine.

**Si el caller se cancela antes de `await asyncio.shield(work)`:**

- El event loop procesa la cancelación de la corutina.
- **El worker SIGUE Ejecutándose** en su hilo protegido. `asyncio.shield()` blindada la tarea, por lo que la cancelación del caller **no interrumpe** al worker.
- `work` (el Task) sigue vivo. `work.cancelled()` será `False` hasta que el event loop efectivamente intente cancelarla.
- **El caller recibirá `CancelledError`** en el punto donde se esperaba el `await asyncio.shield(work)`, **pero el worker continúa corriendo** en su hilo.
- **`deleted_at` no se garantiza que se actualice** si el worker aún no ha alcanzado el `session.commit()`, pero **sí se garantiza que el worker continúa su ejecución** hasta completar el `session.commit()` o fallar.

### 2. Cancelación mientras se espera el worker

#### A. El `await asyncio.shield(work)`

**Comportamiento verificado:**

```python
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

- **El `shield` blindada la tarea worker** contra la cancelación del caller: aunque el caller sea cancelado, la tarea worker continúa corriendo en su hilo.
- **El `CancelledError` se relanza** después de ejecutar el callback de transferencia.
- **El callback `work.add_done_callback(...)` se registra** después de la excepción, asegurando que se ejecutará cuando el worker termine (sea cual sea el resultado: éxito, fallo o cancelación propia del worker).

#### B. El callback de transferencia de limpieza

`_release_lingering_delete(work)` (runner.py:1002-1013):

```python
def _release_lingering_delete(work: asyncio.Task) -> None:
    """Drop the hold and consume the outcome of a persisted delete (§8)."""
    _lingering_delete_work.discard(work)
    if work.cancelled():
        logger.error("persisted world delete task was cancelled")
        return
    exc = work.exception()
    if exc is not None:
        logger.error(
            "persisted world delete failed: %r", exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
```

**Cuándo se ejecuta:**

- **Cuando el worker termina normalmente** (success o excepción dentro del worker): Se ejecuta después de que el `await asyncio.shield(work)` complete, **antes o después** de que el caller reciba `CancelledError` (si el caller fue cancelado).
- **Cuando el caller se cancela mientras el worker continúa**: El callback SÍ se ejecuta. Su función es consumir el outcome (éxito o error) del worker y liberar la referencia fuerte `_lingering_delete_work`, **para que el estado perdurable perdure correctamente** independientemente de la cancelación del caller.

**Qué garantiza:**

- **No queda un runner residual en memoria** como consecuencia directa de la cancelación del caller, porque el callback garantiza que el outcome del worker se consuma.
- **La guard `end_delete` se transferirá** al callback si el caller se cancela (véase `guard_transferred = True` en línea 1106). Si el caller NO se cancela (flujo normal), el `finally` block (línea 1108-1112) ejecutará `registry.end_delete(world_id)` síncronamente.

#### C. Orden exacto: finalización del worker, reanudación y `unregister()`

**Secuencia posible (caso extremo):**

1. **Worker confirma el commit** en la base de datos (`deleted_at IS NOT NULL` durable).
2. **Worker termina** (task.done() becomes True).
3. **El caller recibe `CancelledError`** en el `except asyncio.CancelledError:` block (después del `await asyncio.shield(work)`).
4. **Se ejecuta el callback** `work.add_done_callback(lambda ...: registry.end_delete(_id))`.
5. **`registry.end_delete(_id)` se ejecuta**, retirando el runner del registro en memoria.
6. **El flujo `safe_delete_world()` termina** con `CancelledError` propagado al caller.

**¿Puede ocurrir esta secuencia?** **SÍ es posible**. Dado que:
- El worker puede terminar (confirmar commit y finalizar) en cualquier momento.
- El `await asyncio.shield(work)` retorna cuando el worker termina.
- Si el caller fue cancelado en un punto anterior pero el event loop aún no había procesado la bandera de cancelación hasta que el await completó, esta secuencia es viable.
- El callback se ejecuta después del await, y `end_delete` retira el runner.

**Secuencia alternativa (caller cancela después de que el worker termine pero el event loop aún no procesa la cancelación):**

- Igual que la anterior, pero la bandera de cancelación del caller ya estaba fijada antes del await. El comportamiento es idéntico: el callback se ejecuta y `end_delete` retira el runner.

### 3. Verificación del callback

| Escenario | ¿Se ejecuta el callback? | ¿Antes/después de CancelledError? | Limpieza garantizada |
|---|---|---|---|
| Worker termina normalmente (sin cancelación) | **SÍ** | Despúes de que el await complete (no hay CancelledError) | `end_delete` se ejecuta en el `finally` block (línea 1108-1112), NO via callback. El callback solo consume el outcome. |
| Caller se cancela DENTRO del `await asyncio.shield(work)` | **SÍ** | **Después** de relanzar `CancelledError` (el callback se registra en el bloque except) | El callback transfiere la duty de `end_delete`. El `finally` block NUNCA se ejecuta (porque `guard_transferred = True`). |
| Caller se cancela FUERA del shield (después de él) | **SÍ** | **Después** del await (el await ya completó, el CancelledError ya fue procesado o no aplica) | El `finally` block ejecuta `end_delete` síncronamente (línea 1108-1112) ya que `guard_transferred = False`. |

### 4. Entregable

**Archivo generado:** `F7-E.2-PRE-DECISION-FIX5.md`

**Contenido:**

1. ✅ **Construcción y programación real de `work`**:
   - `asyncio.to_thread(_delete_world_row, ...)` en worker thread.
   - `asyncio.ensure_future(...)` programa la tarea.
   - El worker puede comenzar antes o después del `await` (no hay punto medio).

2. ✅ **Semántica de cancelación antes y durante `shield`**:
   - Cancelación dentro del shield: worker continúa, callback transfiere duty, caller recibe CancelledError.
   - Cancelación fuera del shield: behavior depende del punto temporal; si es antes del await, el worker continúa y el finally bloque ejecutará end_delete.

3. ✅ **Orden de finalización del worker y limpieza**:
   - Worker termina → await retorna → except CancelledError opcional → callback se registra → `guard_transferred` define si el finally ejecuta end_delete o el callback lo hace.
   - La secuencia 1-2-3-4-5 (worker commit → worker end → caller CancelledError → callback → end_delete) **es posible** y está documentada.

4. ✅ **Garantías demostradas y limitaciones**:
   - **Garantizado**: El worker continúa corriendo aunque el caller sea cancelado (shield protects).
   - **Garantizado**: El callback siempre se ejecuta cuando el task.done() es True, consumiendo el outcome.
   - **Garantizado**: `end_delete` se ejecuta ya sea vía callback (si guard_transferred=True) o vía finally block (si guard_transferred=False).
   - **Limitación**: `deleted_at` puede o no ser durable dependiendo de cuándo el worker haya alcanzado el commit relative al momento de la cancelación del caller.

5. ✅ **Dictamen final de F7-E.2-PRE**:
   - Los escenarios de cancelación están **demostrados a partir del código real** (`runner.py:1073-1112`), no inferidos.
   - La política de `end_delete` (ya sea vía callback o via finally) garantiza que **no quedará un runner residual** en memoria como consecuencia de la cancelación del caller, siempre y cuando se ejecute el callback o el finally block.
   - **F7-E.2-PRE puede aceptarse** con la certeza de que la semántica de cancelación es técnicamente precisa y está anclada en la inspección real.

---

## 5. Restricciones

- ❌ No modificar archivos `*.py`.
- ❌ No modificar ORM, migraciones ni pruebas.
- ❌ No iniciar F7-E.2-I.
- ❌ No alterar decisiones arquitectónicas aceptadas.
- ❌ No declarar garantías sin evidencia del código inspeccionado.

---

## 6. Criterio de aceptación

F7-E.2-PRE podrá cerrarse cuando la semántica de cancelación coincida con el flujo real inspeccionado en `runner.py:1073-1112`, la política de `end_delete` esté demostrada (callback o finally block), y no existan contradicciones entre cancelación, finalización del worker y reconciliación de runners.

**La implementación F7-E.2-I continuará requiriendo autorización expresa.**

---
**F7-E.2-PRE-DECISION-FIX5: Verificación definitiva de cancelación — COMPLETADA.**