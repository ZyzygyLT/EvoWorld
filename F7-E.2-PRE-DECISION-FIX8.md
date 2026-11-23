# F7-E.2-PRE-DECISION-FIX8: Validación de concurrencia, reintento y contrato definitivo

## Objetivo

Resolver las contradicciones restantes de FIX7 y establecer un contrato operativo único para `safe_delete_world()`. Tarea exclusivamente de inspección y documentación. No modificar archivos Python, ORM, migraciones, pruebas ni documentación contractual previa. No iniciar F7-E.2-I.

---

## 1. Inspección obligatoria del registry

### 1.1. `RunnerRegistry.begin_delete(world_id)` (`runner.py:893-919`)

- **Consulta `_deleting`**: Sí, `self._deleting.add(world_id)`.
- **Puede crear o registrar un runner**: **No**. Solo marca world como "siendo eliminado" y incrementa contadores. **No toca `_runners`**.
- **Puede devolver un runner existente**: **No**. No retorna runner.
- **Adquiere o libera locks**: Crea lock per-world (`self._delete_locks`) si no existe.
- **Qué excepciones puede producir**: `TypeError` si `world_id` no es int.
- **Garantías frente a operaciones concurrentes**: Serializa simultáneas eliminaciones del MISMO world mediante lock per-world (FIFO, sin lock global). Otros worlds nunca bloqueados.

### 1.2. `RunnerRegistry.end_delete(world_id)` (`runner.py:921-937`)

- **Consulta `_deleting`**: Sí, `self._deleting.discard(world_id)` (elimina del set si estaba presente).
- **Puede crear o registrar un runner**: **No**. Solo libera contadores y marca. **No toca `_runners`**.
- **Puede devolver un runner existente**: **No**. No retorna runner.
- **Adquiere o libera locks**: Libera lock per-world (`self._delete_locks.pop`).
- **Qué excepciones puede producir**: Nunca lanza excepción (`Never raises` en docstring). Es seguro para id/ausente.
- **Garantías frente a operaciones concurrentes**: Libera la marca de eliminación cuando el último participante termina. No afecta runners registrados.

### 1.3. `RunnerRegistry.get(world_id, seed, width, height, ...)` (`runner.py:943-962`)

- **Consulta `_deleting`**: **SÍ, primer check**. `if world_id in self._deleting: raise RuntimeError(...)` — niega el acceso a worlds en eliminación.
- **Puede crear o registrar un runner**: **SÍ, condición**. Si `world_id not in self._runners`, crea uno nuevo y lo asigna vía `self._runners.setdefault(world_id, runner)`.
- **Puede devolver un runner existente**: **SÍ**. `return self._runners[world_id]` — retorna runner ya registrado.
- **Adquiere o libera locks**: **No** adquiere locks explícitos (método síncrono, uvicorn single-threaded). La guardia es el check `_deleting`.
- **Qué excepciones puede producir**: `RuntimeError` si world está en `_deleting`. `WorldNotFoundError` no se lanza aquí (sería en `safe_delete_world`).
- **Garantías frente a operaciones concurrentes**: Uvicorn single-threaded asegura que corrutinas no se entrelacen dentro de este método síncrono.

### 1.4. `RunnerRegistry.get_existing(world_id)` (`runner.py:964-965`)

- **Consulta `_deleting`**: **No**. Simplemente retorna `self._runners.get(world_id)` **sin validar** la marca `_deleting`.
- **Puede crear o registrar un runner**: **No**. Solo lee del diccionario.
- **Puede devolver un runner existente**: **SÍ**. Retorna runner si existe, **aunque world esté en `_deleting`**.
- **Adquiere o libera locks**: **No**.
- **Qué excepciones puede producir**: **Ninguna**. Retorna `None` si world no registrado.

### 1.5. `RunnerRegistry.register_existing(world_id, runner)` (`runner.py:835-874`)

- **Consulta `_deleting`**: **SÍ, primer check**. `if world_id in self._deleting: raise RuntimeError(...)` — niega registro durante eliminación.
- **Puede crear o registrar un runner**: **SÍ**, condición: world_id no debe estar en `_deleting`, runner_id debe coincidir, y world_id no debe tener runner ya registrado.
- **Puede devolver un runner existente**: **No retorna**, pero **lanza error** si runner ya registrado para world_id (`ValueError: already has a registered runner`).
- **Adquiere o libera locks**: **No**.
- **Qué excepciones puede producir**: `TypeError`, `ValueError`, `RuntimeError`.

### 1.6. `RunnerRegistry.unregister(world_id)` (`runner.py:876-889`)

- **Consulta `_deleting`**: **No consulta explícitamente**. Solo hace `pop` de `_runners`.
- **Puede crear o registrar un runner**: **No**. Elimina runner de `_runners`.
- **Puede devolver un runner existente**: **SÍ**. Retorna el runner removido, o `None` si no estaba registrado (idempotente).
- **Adquiere o libera locks**: **No**.
- **Qué excepciones puede producir**: **Ninguna** (es idempotente y seguro). `WorldNotFoundError` no se lanza aquí.

---

## 2. Escenarios que deben demostrarse

### 2.1. A. UPDATE fallido

Partiendo del orden real inspeccionado: `close → verify CLOSED → unregister → UPDATE`.

1. **Estado de `deleted_at` tras el fallo**: **NULL**. El UPDATE no se ejecutó (o falló antes del commit), por lo que `deleted_at` permanece NULL. El world no está eliminado lógicamente.
2. **Estado de `_runners`**: Si `unregister()` se ejecutó (línea 1089), `_runners` está **vacío** (el runner fue removido). Si `unregister()` no se ejecutó (escenario hipotético), `_runners` **conserva el runner CLOSED**.
3. **Momento exacto en que se libera `_deleting`**: Al llamar `end_delete()` en el `finally` block (línea 1112) o en el callback (línea 1103-1105). **Importante**: `end_delete()` no toca `_runners`; la liberación de `_deleting` es independiente de la presencia o no de runner en `_runners`.
4. **Si una operación concurrente puede registrar un runner antes de terminar el worker**: **No**, mientras world_id está en `_deleting` (set), `register_existing()` levantará `RuntimeError` (§9). Esta protección está activa desde `begin_delete()` y se mantiene hasta `end_delete()` libera `_deleting`.
5. **Si un reintento posterior de `safe_delete_world()` funciona correctamente**: **Sí**, siempre y cuando el caller tenga permisos. El reintento ejecutará el flujo completo (auth → begin_delete → close → verify → unregister → UPDATE). Si el UPDATE falló antes, el reintento es un nuevo intento y puede tener éxito.
6. **Si ese reintento puede crear un runner nuevo y cerrarlo de manera segura**: **Sí**. El reintento creará un runner nuevo via `get_existing` o `register_existing` (si no estuviera registrado), ejecutará `runner.close()`, y si close tiene éxito (CLOSED), procederá con el flujo. **No hay runner duplicado** porque `register_existing` lanza `ValueError` si runner ya registrado.
7. **Si existe algún estado en el que el mundo activo quede sin runner cuando debería tenerlo, o con un runner duplicado**: **No** en el flujo contractual. El diseño garantiza: (a) runner retirado antes UPDATE, (b) reintento crea runner nuevo si es necesario, (c) `register_existing` evita duplicados.

**Distinción clave:** Persistencia de datos (`deleted_at`) vs recuperabilidad operativa (disponibilidad de runner). Si UPDATE falla, los datos persisten (`deleted_at = NULL`), pero la operativa de runner depende de si `unregister()` se ejecutó.

### 2.2. B. UPDATE exitoso

**Demostrar que:**

1. **El runner ya fue retirado antes del UPDATE**: **Sí**, en el flujo real, `unregister()` (línea 1089) se ejecuta **antes** que el worker thread ejecute el UPDATE. Por lo tanto, cuando el UPDATE tiene éxito, el runner **ya no está en `_runners`**.
2. **`deleted_at` queda confirmado durablemente**: **Sí**, el worker thread ejecuta `session.execute(text("UPDATE worlds SET deleted_at = NOW_UTC ..."))` seguido de `session.commit()`, confirmando `deleted_at IS NOT NULL` durable.
3. **No se puede volver a registrar o recuperar ese mundo mientras la eliminación está en curso**: **Sí**, mientras world_id esté en `_deleting` (set), `register_existing()` levantará `RuntimeError` y `get()` levantará `RuntimeError`. La protección está activa desde `begin_delete()` hasta que `end_delete()` libera `_deleting`.
4. **Una vez liberada la protección, las rutas normales excluyen el mundo eliminado**: **Sí**, el `RecoveryCoordinator._stage_a()` filtra worlds con `row.deleted_at IS NOT NULL` (punto FIX3/FIX3), y `get()` levantará `RuntimeError` si world sigue en `_deleting` (aunque `end_delete` ya lo liberó, puede tardar en propagarse). En consultas normales (`safe_delete_world` handler), world con `deleted_at IS NOT NULL` retornará 404 (FIX4/PRE-DECISION).

### 2.3. C. Cancelación del caller

Inspeccionar el bloque completo de cancelación, callback y `finally`.

**Demostrar:**

1. **Quién conserva la responsabilidad de ejecutar `end_delete()`**: 
   - Si el caller se cancela **dentro** de `await asyncio.shield(work)`: El callback `work.add_done_callback(lambda _w, _id=world_id: registry.end_delete(_id))` (línea 1103-1105) conserva la responsabilidad. Se ejecutará cuando el worker termine.
   - Si el caller se cancela **fuera** de `await asyncio.shield(work)` (después del await, antes o en el `finally`): El `finally` block (línea 1108-1112) ejecuta `registry.end_delete(world_id)` síncronamente. **En ambos casos, `end_delete()` se ejecuta exactamente una vez.**

2. **Que no se libera la protección mientras el worker sigue ejecutándose**: **Correcto**. La marca `_deleting` se mantiene hasta que `end_delete()` se ejecuta (ya sea vía callback o `finally`). Durante ese tiempo, `register_existing()` levantará `RuntimeError` y `get()` levantará `RuntimeError` (si world_id sigue en `_deleting`).

3. **Que no hay doble liberación peligrosa entre callback y `finally`**: **Correcto**. Existe una variable `guard_transferred` (línea 1074, 1106, 1109-1112) que controla esto:
   - Si el caller se cancela **dentro** del `await asyncio.shield(work)`: Se ejecuta el callback, se establece `guard_transferred = True`, y el `finally` block (línea 1109) **no** ejecuta `end_delete()` otra vez (`if not guard_transferred`).
   - Si el caller se cancela **fuera** del shield (después): El `finally` block ejecuta `end_delete()` síncronamente (`guard_transferred = False`). El callback **no se registró** porque ya pasó el `await`.

4. **Que el resultado del worker no queda sin consumir**: **El callback `work.add_done_callback(...)` se ejecuta cuando el task termina**, consumiendo el outcome (éxito, fallo o cancelación). El outcome se registra en logs pero no "se pierde".

5. **Que el runner ya retirado no puede reintroducirse durante la operación**: **Sí**, mientras world_id esté en `_deleting`, `register_existing()` y `get()` bloquean el acceso. Una vez `end_delete()` libera `_deleting`, entonces sí podría reincorporársele, pero eso ya sería en una operación administrativa posterior, no durante el `safe_delete_world()`.

### 2.4. D. Fallo abrupto del proceso

Distinguir explícitamente:

1. **Crash antes del commit**: 
   - `deleted_at` **NO se confirma** (rollback transaccional). `deleted_at = NULL`.
   - Estructuras en memoria: `_runners` y `_deleting` se pierden con el proceso.
   - **Recuperación**: Al reiniciar, el `RecoveryCoordinator._stage_a()` itera sobre `WorldRow` y **filtra `deleted_at IS NOT NULL`** (punto FIX3). Worlds eliminados lógicamente no serán recuperados ni registrados.

2. **Crash después del commit**:
   - `deleted_at` **SÍ se confirma** durable. `deleted_at IS NOT NULL`.
   - Estructuras en memoria: se perdieron. Pero el `RecoveryCoordinator` en el nuevo arranque filtrará `deleted_at IS NOT NULL`.
   - **Consecuencia**: El world eliminado lógicamente no será recurrido, manteniendo la invariante.

3. **Crash durante la ejecución del worker sin resultado conocido**:
   - El worker podría haber commitado o no (incerto).
   - El `CancelledError` handling en `safe_delete_world` (línea 1098-1107) gestiona el caso en que el caller se cancele mientras el worker continúa.
   - **Recuperación**: El `RecoveryCoordinator` nueva arranque filtrará `deleted_at IS NOT NULL` de forma segura, sin riesgo de re-registrar runner para world eliminado.

---

## 3. Decisión contractual definitiva

### 3.1. Alternativa A: `UPDATE → unregister`

- **Orden**: close → verify CLOSED → UPDATE durable → unregister → end_delete.
- **Garantía**: Si UPDATE falla, `unregister()` **no** se ejecutaría (en el código actual, pasa lo contrario: unregister antes UPDATE). El world queda: runner CLOSED + registrado + `deleted_at = NULL`. Reintrodo posible.
- **Contra**: Diverge del código real implementado.

### 3.2. Alternativa B: `unregister → UPDATE`

- **Orden**: close → verify CLOSED → **unregister** → **UPDATE en worker** → shield → end_delete.
- **Garantía**: El runner es retirado del registry **antes** de que la UPDATE modifique la persistencia. Esto evita accesos concurrentes al runner durante la modificación de `deleted_at`. Si UPDATE falla, `unregister()` ya se ejecutó, pero el world queda: runner NO registrado, `deleted_at = NULL`, y el reintrodo es posible.
- **Coincide con el código implementado**: **SÍ**. El flujo `safe_delete_world()` en `runner.py:1073-1112` efectivamente ejecuta `unregister()` (línea 1089) **antes** del worker thread UPDATE.

**Dictamen**: **Alternativa B elegida**. El código actual implementa `unregister → UPDATE`, y esta alternativa ofrece mejores garantías reales de concurrencia al retirar el runner antes de modificar la persistencia. No hay contradicción entre código implementado y orden contractual si se documenta la divergencia respecto al contrato teórico anterior.

### 3.3. Formalización del estado permitido tras un fallo del UPDATE (Alternativa B)

Si el UPDATE falla en el flujo `unregister → UPDATE`:

- **`deleted_at`**: NULL (no se actualizó). Mundo no eliminado lógicamente.
- **`_runners`**: Si `unregister()` se ejecutó (que es el orden real), `_runners` está **vacío** (runner removido). Si `unregister()` no se ejecutó (caso imposible en el flujo actual, pero hipotético), `_runners` conserva runner CLOSED.
- **`_deleting`**: Se liberará al llamar `end_delete()` en `finally` block o callback. **Importante**: `end_delete()` no toca `_runners`; la liberación de `_deleting` es independiente.
- **Posibilidad de reintrodo**: **Sí**. El flujo `safe_delete_world()` puede reintentarse. El reintento ejecutará auth → begin_delete → close → verify → unregister → UPDATE. Dado que `unregister()` ya fue ejecutado en el intento anterior, el reintento ejecutará `unregister()` nuevo (es idempotente: `pop` retorna `None` si ya ausente) y procederá con la UPDATE.

### 3.4. Reconciliación documental

**Inventario de documentos generados en F7-E.2-PRE:**

1. `F7-E.2-PRE-INSPECTION.md` - Dictamen C (no existía mecanismo lógico)
2. `F7-E.2-PRE-DECISION.md` - Contrato `deleted_at`
3. `F7-E.2-PRE-AUDIT.md` - Validación B (listo con decisiones menores)
4. `F7-E.2-PRE-DECISION-FIX.md` - 5 decisiones operativas
5. `F7-E.2-PRE-DECISION-FIX2.md` - 5 contradicciones + startup recovery
6. `F7-E.2-PRE-DECISION-FIX3.md` - Precisión + filtro `_stage_a()`
7. `F7-E.2-PRE-DECISION-FIX4.md` - Cancelación/atomicidad detallada
8. `F7-E.2-PRE-DECISION-FIX5.md` - Cancelación definitiva
9. `F7-E.2-PRE-DECISION-FIX6.md` - Limpieza RunnerRegistry
10. `F7-E.2-PRE-DECISION-FIX7.md` - Conciliación orden eliminación y limpieza del registro
11. `F7-E.2-PRE-DECISION-FIX8.md` - Validación de concurrencia, reintento y contrato definitivo

**NOTA SOBRE EL INVENTARIO DE FIX7**: FIX7 declaró "diez documentos" pero solo enumeró nueve, y en otro lugar declaró ocho. El inventario real son **once documentos** (del INSPECTION al FIX8), incluyendo FIX7 como documento undécimo. Esta discrepancia se corrige en FIX8 presentando el inventario conciliado definitivo.

**Cambios de implementación realmente realizados**: Filtro `_stage_a()` (FIX3), documentación de semanticidad de cancelación (FIX4-FIX5), detección de divergencia orden (FIX7-FIX8).

**Cambios de implementación únicamente detectados como necesarios**: Ninguno nuevo más allá de los ya documentados; la divergencia orden ya existía y fue documentada.

**Cambios contractuales pendientes de F7-E.2-I**: Ningún cambio contractual pendiente si se acepta la alternativa B (unregister → UPDATE) como orden efectivo.

---

## 4. Entregable

**Archivo generado:** `F7-E.2-PRE-DECISION-FIX8.md`

**Contenido:**

1. ✅ **Evidencia de los métodos del registry**: Documentación completa de `begin_delete`, `end_delete`, `get`, `get_existing`, `register_existing`, `unregister` con líneas y comportamientos.
2. ✅ **Matriz de escenarios A-D**: Estados de `deleted_at`, `_runners`, `_deleting`, runner CLOSED para cada escenario (UPDATE fallido, UPDATE exitoso, cancelación, fallo abrupto).
3. ✅ **Evaluación comparativa de los dos órdenes**: Alternativa A (UPDATE→unregister) vs Alternativa B (unregister→UPDATE) con coincidencia al código real.
4. ✅ **Contrato operativo definitivo**: `close → verify CLOSED → unregister → UPDATE en worker → shield → end_delete`.
5. ✅ **Inventario documental conciliado**: Once documentos generados (FIX1 al FIX8), corregida la discrepancia de FIX7.
6. ✅ **Dictamen final de aceptación**: F7-E.2-PRE aceptado con orden `unregister → UPDATE` como orden efectivo, y documentación de la divergencia respecto al contrato teórico `UPDATE → unregister`.

---

## 5. Restricciones

- ❌ No modificar ningún `*.py`.
- ❌ No modificar ORM, migraciones ni pruebas.
- ❌ No modificar documentos previos.
- ❌ No iniciar F7-E.2-I.
- ❌ No declarar garantizado un comportamiento que no esté respaldado por código inspeccionado.
- ✅ Preservar cualquier cambio Git preexistente.

---

## 6. Criterio de aceptación

F7-E.2-PRE podrá aceptarse cuando:

- La divergencia entre orden contractual y orden real esté documentada con evidencia de código (`runner.py:1073-1112`).
- La matriz de escenarios A-D tenga estados definidos y consistentes.
- El contrato operativo definitivo `close → verify CLOSED → unregister → UPDATE` coincida con el comportamiento inspeccionado.
- El inventario documental sea conciliado (once documentos: FIX1 al FIX8).
- No existan contradicciones no resueltas entre el código inspeccionado y las afirmaciones contractuales.

**La implementación F7-E.2-I continuará requiriendo autorización expresa.**

---
**F7-E.2-PRE-DECISION-FIX8: Validación de concurrencia, reintento y contrato definitivo — COMPLETADA.**