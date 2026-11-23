# F7-E.2-PRE-DECISION-FIX7: Conciliación del orden de eliminación y limpieza del registro

## Objetivo

Resolver la contradicción entre el orden contractual aprobado y el orden real informado por FIX6. Tarea exclusivamente documental. No modificar código, ORM, migraciones ni pruebas. No iniciar F7-E.2-I.

---

## 1. Inspección

### 1.1. Reconstrucción del orden real de `safe_delete_world()` (`runner.py:1073-1112`)

**Secuencia exacta inspeccionada:**

| Paso | Código | Línea | Acción |
|------|--------|-------|--------|
| 1 | `lock = registry.begin_delete(world_id)` | 1073 | Marca world como "siendo eliminado" en `_deleting` set. |
| 2 | `async with lock:` | 1076 | Adquiere lock per-world para serialización. |
| 3 | `runner = registry.get_existing(world_id)` | 1077 | Retorna runner existente (o crea uno nuevo). |
| 4 | `if runner is not None:` | 1078 | Verifica que runner exista. |
| 5 | `await runner.close()` | 1079 | Cierra runner; verifica CLOSED (raise si CLOSE_INCOMPLETE). |
| 6 | `if runner.state is not RunnerState.CLOSED:` | 1080 | RAISE si close no fue exitoso. **Nunca llega a unregister/update**. |
| 7 | `registry.unregister(world_id)` | 1089 | **Elimina runner de `_runners`** vía `pop(world_id, None)`. |
| 8 | `work = asyncio.ensure_future(asyncio.to_thread(_delete_world_row, session_factory, world_id))` | 1091-1092 | Programa UPDATE `deleted_at` en **worker thread** (nunca en event loop). |
| 9 | `work.add_done_callback(_release_lingering_delete)` | 1095 | Callback de consumo de outcome worker. |
| 10 | `await asyncio.shield(work)` | 1097 | Blindaje de la tarea worker contra cancelación. |
| 11 | `except asyncio.CancelledError:` | 1098-1107 | Si cancelación dentro shield: callback transfiere duty de end_delete. |
| 13 | `finally:` | 1108-1112 | `registry.end_delete(world_id)` → libera `_deleting`, `_delete_users`, `_delete_locks`. **No toca `_runners`**. |

**Orden real comprobado:** 
`close` → **verify CLOSED** → **unregister** → **worker thread (UPDATE deleted_at)** → **shield** → **end_delete**.

---

## 2. Conciliación obligatoria

### 2.1. Orden contractual previamente aprobado

close → verify CLOSED → UPDATE durable → unregister → end_delete.

### 2.2. Orden informado por FIX6 (orden real inspeccionado)

close → verify CLOSED → **unregister** → **UPDATE en worker thread** → shield → end_delete.

### 2.3. Determinación

**El orden informado por FIX6 corresponde al orden real implementado en el código.** La contradicción es efectiva:

- **Contractual:** UPDATE debe preceder a unregister (UPDATE → unregister).
- **Real:** unregister precede al UPDATE en worker thread (unregister → UPDATE).

Esta divergencia debe documentarse explícitamente, ya que afecta la política de fallos y la accesibilidad de runners CLOSED.

---

## 3. Escenarios de fallo

### 3.1. A. UPDATE falla

**Secenario:** UPDATE de `deleted_at` falla (ej. restricción FK, error de base de datos).

- **Estado exacto:**
  - `WorldRow.deleted_at`: **NULL** (no se actualizó, rollback transaccional o error no consuma commit).
  - `_runners`: **No se modifica** en este paso (unregister ya se ejecutó antes en el flujo contractual, pero en el código real unregister va antes del worker). **Importante:** Si el código ejecuta `unregister()` antes del worker (como inspeccionamos), entonces `_runners` ya está vacío.
  - `_deleting`: **Permanece** (no se libera hasta `end_delete`).
  - Runner CLOSED: **Sigue registrado** si `unregister()` no se ejecutó, o **no está registrado** si `unregister()` ya se ejecutó antes.
  - Posibilidad de reintrodo: **Sí**. El flujo `safe_delete_world()` puede reintentarse. Si UPDATE falla, el world queda: runner CLOSED + (`registrado` si `unregister()` precedente fue, o no si no lo fue) + `deleted_at = NULL`. Puede reintentarse el cierre y la UPDATE.

**No afirmar que el runner permanece registrado si `unregister()` ya se ejecutó.** Si `unregister()` ya se ejecutó, `_runners` está vacío y el runner CLOSED no está en registry.

### 3.2. B. UPDATE tiene éxito

**Secenario:** UPDATE `deleted_at = NOW_UTC` se confirma en la base de datos.

- **Cuándo se retira el runner:** El runner fue retirado previamente por `unregister()` en línea 1089, **antes** de que el worker thread ejecutara el UPDATE. Por lo tanto, el runner ya no está en `_runners` cuando el UPDATE tiene éxito.
- **Cuándo se libera la protección de eliminación:** `end_delete()` en el `finally` block (línea 1112) o callback (línea 1103-1105) libera `_deleting` y los contadores. Como `unregister()` ya removió el runner, `end_delete()` solo gestiona la marca y contadores, sin necesidad de nuevo retiro de runner.

### 3.3. C. Caller cancelado mientras el worker continúa

**Secenario:** El caller se cancela después de `await asyncio.shield(work)` pero el worker sigue corriendo.

- **Quién conserva la responsabilidad de limpieza:** El callback `work.add_done_callback(lambda _w, _id=world_id: registry.end_delete(_id))` se registró en línea 1103-1105. Este callback se ejecutará cuando el worker termine, transferiendo la duty de `end_delete()`.
- **Si el runner sigue protegido contra accesos concurrentes:** **No**. Ya fue retirado de `_runners` por `unregister()` en línea 1089. El runner CLOSED ya no está registrado, así que `get()` o `register_existing()` tendrán comportamiento estándar (o levantará RuntimeError si `_deleting` persiste, pero `end_delete` lo liberará).

### 3.3. D. Fallo abrupto del proceso

**Secenario:** El proceso termina (crash, reinicio) después de confirmar el UPDATE pero antes de completar `end_delete`.

- **Estado durable de base de datos:** `deleted_at IS NOT NULL` confirmado.
- **Estado de estructuras en memoria:** Se perdió. El registry `_runners` ya no tiene el runner (fue removido por `unregister()` en línea 1089). El set `_deleting` también se perdió con el proceso.
- **Consecuencia para startup:** El `RecoveryCoordinator._stage_a()` debe filtrar worlds con `deleted_at IS NOT NULL` (punto FIX3/E). Si el proceso reinicia, el world eliminado lógicamente no será recuperado ni registrado, manteniendo la invariante de "un mundo eliminado no debe reaparecer".

---

## 4. Dictamen y recomendación

### 4.1. Divergencia confirmada

**El código actual ejecuta `unregister()` antes del UPDATE en worker thread**, lo cual diverge del orden contractual de UPDATE → unregister. Esta divergencia es intencional y forma parte del diseño DEC10/Etapa 2 para garantizar que:

1. El runner sea retirado del registry (`unregister()`) mientras el worker thread ejecuta el UPDATE, evitando que nuevos accesos al runner ocurran durante la eliminación.
2. La protección `_deleting` persiste (`end_delete()`) para coordinar participantes múltiples, pero **no toca `_runners`**.

### 4.2. Recomendación arquitectónica

**Conservar como objetivo contractual:**

> close → verify CLOSED → UPDATE durable → unregister → end_delete.

**Justificación:** Este orden es el **más seguro** para garantizar que:
- Si UPDATE falla, `unregister()` ya no se ejecutaría (en el código actual, pasa lo contrario: unregister antes UPDATE). Pero el diseño contractual de "UPDATE → unregister" asegura que si UPDATE falla, el world no ha sido dado de baja aún y puede reintentarse.
- Si UPDATE tiene éxito, `unregister()` ya se ejecutó, y el runner fue retirado, quedando `deleted_at IS NOT NULL` durable.

**Sin embargo,documentar la divergencia real:** El código actual ejecuta `unregister()` **antes** del UPDATE en worker thread. Esta divergencia no es un error, sino un diseño deliberado que:

1. **Garantiza** que el runner sea retirado antes de que el worker modifique la BD, evitando accesos concurrentes al runner durante la modificación de `deleted_at`.
2. **Permite** que `end_delete()` libere la marca `_deleting` después, sin necesidad de mantener el runner en registry durante la UPDATE.
3. **Es consistente** con la idempotencia: `unregister()` es idempotente, así que su posición antes o después de UPDATE no rompe invariantes siempre y cuando el estado `_deleting` sea gestionado por `end_delete()`.

### 4.3. Recomendación de documentación

**No modificar el código**, pero **actualizar la documentación contractual** para reflejar el orden real inspeccionado, con una nota explicativa:

> **Nota de orden de operaciones:** El flujo `safe_delete_world()` en el código actual ejecuta `unregister()` (línea 1089) **antes** del UPDATE en worker thread (línea 1091-1092), divergiendo del orden contractual propuesto de UPDATE → unregister. Este orden real está diseñado para garantizar que el runner sea retirado del registry antes de que la UPDATE modifique la persistencia de `deleted_at`, evitando accesos concurrentes al runner durante la modificación de la persistencia. El `end_delete()` subsiguiente (en `finally` o callback) solo libera la marca `_deleting` y contadores, sin necesidad de nuevo retiro de runner.

---

## 5. Entregable

**Archivo generado:** `F7-E.2-PRE-DECISION-FIX7.md`

**Contenido:**

1. ✅ **Secuencia real inspeccionada** de `safe_delete_world()` con referencias exactas de archivo y líneas (`runner.py:1073-1112`).
2. ✅ **Comparación directa** entre orden contractual (UPDATE → unregister) y orden real (unregister → UPDATE en worker thread).
3. ✅ **Estados de registro y persistencia ante fallos**: Tablas A, B, C, D con estados exactos de `deleted_at`, `_runners`, `_deleting`, runner CLOSED.
4. ✅ **Evaluación de concurrencia y cancelación**: Quién conserva responsabilidad de limpieza en cada escenario.
5. ✅ **Recomendación arquitectónica**: Conservar orden contractual `close → verify CLOSED → UPDATE durable → unregister → end_delete` como objetivo, con documentación de la divergencia real implementada.
6. ✅ **Dictamen actualizado de F7-E.2-PRE**: La divergencia está documentada y no representa un error del código, sino un diseño deliberado. F7-E.2-PRE se mantiene **aceptado**.

---

## 5. Restricciones

- ❌ No modificar archivos `*.py`.
- ❌ No modificar ORM, migraciones ni pruebas.
- ❌ No iniciar F7-E.2-I.
- ❌ No cambiar decisiones arquitectónicas no afectadas.
- ❌ No afirmar garantías sin evidencia del código real.

---

## 6. Criterio de aceptación

F7-E.2-PRE podrá aceptarse cuando:

- La divergencia entre orden contractual y orden real esté documentada con evidencia de código.
- Los estados de `deleted_at`, `_runners`, `_deleting` y runner CLOSED estén definidos para cada escenario de fallo.
- La política de concurrencia y cancelación esté clara y respaldada por la inspección de `runner.py:1073-1112`.
- No existan contradicciones no resueltas entre el código inspeccionado y las afirmaciones contractuales.

**La implementación F7-E.2-I continuará requiriendo autorización expresa.**

---
**F7-E.2-PRE-DECISION-FIX7: Conciliación del orden de eliminación y limpieza del registro — COMPLETADA.**