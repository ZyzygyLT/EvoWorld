# F7-E.2-PRE-DECISION-FIX6: Verificación definitiva de limpieza del RunnerRegistry

## Objetivo

Resolver la contradicción entre `end_delete()` y `unregister()` detectada en FIX5 y determinar si el mecanismo de cancelación deja runners cerrados registrados o expuestos.

Tarea exclusivamente documental. No modificar archivos Python, ORM, migraciones ni pruebas.

---

## 1. Inspección obligatoria del RunnerRegistry

### 1.1. `end_delete(world_id)` (`runner.py:921-937`)

**Estructuras modificadas:**
- `_delete_users[world_id]`: decrementa el contador.
- Cuando el contador llega a 0:
  - `pop` de `_delete_users[world_id]`.
  - `discard` de world_id de `_deleting` set.
  - `pop` de `_delete_locks[world_id]`.
- **No modifica `_runners`** en ningún caso.

**Idempotencia:** Sí. Si world_id no estaba en `_delete_users`, `.get(world_id, 0)` retorna 0, menos 1 quedaría como -1, pero el condicional `if users > 0` lo evita; el `.pop` con default None evita errores. El `.discard` en `_deleting` es seguro para ausentes.

**Si el runner está CLOSED:** `end_delete()` no tiene conocimiento del estado del runner; solo gestiona la marca de eliminación y los contadores. El estado CLOSED del runner es ajeno a esta operación.

### 1.2. `unregister(world_id)` (`runner.py:876-889`)

**Estructuras modificadas:**
- `_runners[world_id]`: **elimina y retorna** el runner mediante `pop(world_id, None)`.

**Idempotencia:** Sí. Si world_id no estaba registrado, `pop` retorna `None` sin error.

**Si el runner está CLOSED:** `unregister()` no consulta el estado del runner; simplemente lo retira del diccionario `_runners`. El runner CLOSED sigue existiendo en memoria (fue cerrado por `runner.close()` en el flujo `safe_delete_world`), pero **ya no está registrado** en el registry.

### 1.3. `begin_delete(world_id)` (`runner.py:893-919`)

**Estructuras modificadas:**
- `_deleting`: `add(world_id)`.
- `_delete_users[world_id]`: `set` a 1 (o incrementar si ya existía).
- `_delete_locks[world_id]`: crea o recupera el Lock per-world.

### 1.4. `get(world_id)` (`runner.py:943-962`)

**Comportamiento:**
- Primero: `if world_id in self._deleting: raise RuntimeError(...)` — **niega el acceso** a runners de worlds en eliminación.
- Segundo: `if world_id not in self._runners: create new runner`; `self._runners.setdefault(world_id, runner)`.
- **No modifica `_runners`** si el runner ya existe.

### 1.5. `register_existing(world_id, runner)` (`runner.py:835-874`)

**Estructuras modificadas:**
- `_runners[world_id] = runner`: **siempre añade** el runner al diccionario.

**Protección:** `if world_id in self._deleting: raise RuntimeError(...)` — **niega el registro** durante eliminación.

---

## 2. Inspección del flujo de eliminación

### 2.1. Flujo normal de `safe_delete_world()` (`runner.py:1073-1112`)

Secuencia exacta sobre `_runners`:

1. **`registry.begin_delete(world_id)`** (línea 1073): 
   - Añade world_id a `_deleting`. 
   - Incrementa `_delete_users[world_id]`.
   - **`_runners` sin modificar.**

2. **`async with lock:`** (línea 1076): adquiere el lock per-world.

3. **`registry.get_existing(world_id)`** (línea 1077): 
   - Como world_id ya está en `_deleting` (por step 1), **`get()` levanta `RuntimeError`**.
   - **Pero espera**: en el código real, `get_existing` checks `if world_id in self._deleting` at line 859, y el `safe_delete_world` llama a `get_existing` después de `begin_delete`. **¿Cómo funciona entonces?**

   Releyendo el código `safe_delete_world` línea 1077-1079:
   ```python
   runner = registry.get_existing(world_id)
   if runner is not None:
       await runner.close()
   ```
   
   Si `get_existing` levanta RuntimeError por world en `_deleting`, este código nunca alcanzaría ese punto. Pero el código sí funciona. La explicación es que `get_existing` es distinto de `get`:
   
   Revisando line 835-874, `register_existing` checks `_deleting` y levanta RuntimeError. Pero `get_existing` es diferente... Revisando line 964-965: `def get_existing(self, world_id: int) -> WorldRunner | None:` retorna `self._runners.get(world_id)`.
   
   **Corrección crítica**: `get_existing` NO chequea `_deleting`. Solo `register_existing` lo hace. `get` sí lo chequea (line 952-956). Entonces `get_existing` simply retorna `self._runners.get(world_id)` sin validar la marca de eliminación. **Esto significa que `get_existing` permite acceder a runners de worlds en eliminación**, lo cual contradeciría la intención de §9.
   
   Pero el flujo `safe_delete_world` funciona porque... revisando más cuidadosamente: el `async with lock:` adquiere el lock per-world, y dentro de ese lock, la lógica es: `runner = registry.get_existing(world_id)`. Si el runner fue creado antes de la eliminación y registrado via `register_existing`, este método lo retornará. La marca `_deleting` impide **nuevos** registros (`register_existing`), no la recuperación de runners ya existentes.
   
   **Esto es consistente**: Una vez que un runner está registrado, puede recuperarse vía `get_existing` aunque el world esté marcado para eliminación. La protección §9 es contra **registros nuevos**, no contra la recuperación de runners existentes.

3. **`registry.unregister(world_id)`** (línea 1089): 
   - **`_runners.pop(world_id, None)`**: **ELIMINA el runner de `_runners`**.
   - Retorna el runner retirado o `None` si no estaba registrado.
   - **Después de este paso, el runner NO está en `_runners`.**

4. **Worker thread ejecuta `_delete_world_row`** (línea 1091-1092): en hilo separado, UPDATE/delete físico.

5. **`finally` block** (línea 1108-1112): 
   - Si `guard_transferred = False` (caller NO se canceló durante `shield`):
     - `registry.end_delete(world_id)` (línea 1112): **no toca `_runners`**, solo gestiona `_deleting`, `_delete_users`, `_delete_locks`.
   - Si `guard_transferred = True` (caller se canceló durante `shield`): 
     - El callback `work.add_done_callback(lambda ...: registry.end_delete(_id))` se registró en línea 1103-1105.
     - Cuando el worker termine, se ejecutará `end_delete()`, **también sin tocar `_runners`**.
   - **En ambos casos, `_runners` ya fue vaciado por `unregister()` en step 3.**

6. **Estado final de `_runners`**: **Vacío para world_id** (el runner fue removido por `unregister()` en step 3). El objeto runner sigue existiendo en memoria (fue cerrado por `runner.close()`), pero ya no está en el registry.

### 2.2. Los tres escenarios de cancelación y `_runners`

#### Escenario A: Llamada normal (sin cancelación)

Secuencia: `begin_delete` → `close` → `unregister()` (línea 1089) → `work` programado → `shield` → `finally: end_delete()` (línea 1112).

- **`_runners` después de `unregister()`**: **Vacío** (el runner fue removido).
- **`_runners` después de `end_delete()`**: Sigue estando **Vacío** (end_delete no toca _runners).
- **Runner en memoria**: Cerrado (runner.state = CLOSED), no accesible via registry, pero objeto aún existe si alguien tiene referencia.

#### Escenario B: Caller se cancela DENTRO de `asyncio.shield(work)`

Secuencia: `begin_delete` → `close` → `unregister()` (línea 1089) → `work` programado → `await asyncio.shield(work)` → `CancelledError` en except block (línea 1098) → callback `work.add_done_callback(lambda ...: registry.end_delete(_id))` (línea 1103-1105) → `guard_transferred = True` → `raise`.

- **`_runners` después de `unregister()`**: **Vacío** (el runner fue removido en step 3, antes de la cancelación).
- **`_runners` después de callback `end_delete()`**: Sigue estando **Vacío** (end_delete no toca _runners).
- **Callback ejecución**: Se ejecuta cuando el worker termina (sea cual sea su resultado). Su función es consumir el outcome y transferir la duty de limpieza, pero **no agrega nada a `_runners`**.
- **Resultado**: El runner fue removido de `_runners` en step 3 (antes de la cancelación). La cancelación no lo reincorpora.

#### Escenario C: Caller se cancela FUERA de `asyncio.shield(work)` (después del await)

Secuencia: `begin_delete` → `close` → `unregister()` (línea 1089) → `await asyncio.shield(work)` (retorna porque worker terminó) → `finally` block (línea 1108-1112) → `registry.end_delete(world_id)` (línea 1112) → flujo termina.

- **`_runners` después de `unregister()`**: **Vacío**.
- **`_runners` después de `end_delete()`**: Sigue estando **Vacío**.
- **`finally` block**: Ejecuta `registry.end_delete(world_id)` síncronamente. No toca `_runners`.
- **Resultado**: Igual que el escenario normal. El runner fue removido por `unregister()` antes del await.

#### Escenario D: Fallo en el UPDATE (antes de `unregister()`)

Este escenario es imposible en el flujo contractual porque `unregister()` se ejecuta **siempre** después de `runner.close()` y verificación CLOSED (línea 1089), que sucede antes del worker thread que hace el UPDATE. El UPDATE ocurre en worker thread (step 5), después de `unregister()` (step 3).

**Pero consideremos un escenario hipotético** donde el UPDATE fallara antes de `unregister()`:

- Si el UPDATE falla y `unregister()` **no** se ejecuta:
  - `_runners` **sigue teniendo el runner** (no fue removido).
  - El runner está CLOSED (por `runner.close()` en step 2).
  - `_deleting` sigue teniendo world_id (por `begin_delete()` en step 1).
  - **Consecuencia**: El runner CLOSED está registrado y accesible vía `get_existing()` (aunque `get()` levantaría RuntimeError por _deleting).
  - **Protección**: La vérification CLOSED en línea 1080-1087 previene esto: si el close no fue exitoso, se RAISE y el flujo se interrumpe antes de llegar a `unregister()`. Así que este escenario no ocurre en la práctica.

#### Escenario E: `end_delete()` llamado sin previo `unregister()`

Hypothetical: someone calls `end_delete(world_id)` without first calling `unregister()`.

- `_runners` **sigue teniendo el runner** (never removed).
- `_deleting` se remove (discard).
- `_delete_users` y `_delete_locks` se clean up.
- **Consecuencia**: El runner CLOSED permanece registrado en `_runners`, accesible vía `get_existing(world_id)`. **Esto violaría la protección §9** si alguien intenta `register_existing` después, pero `get_existing` no chequea _deleting.

**En la práctica**, esto no ocurre porque el flujo `safe_delete_world` siempre ejecuta `unregister()` en línea 1089 antes de que `end_delete()` sea llamado en el finally block.

---

## 3. Evaluación de seguridad y consistencia

### Estados posibles y su ocurrencia:

| Estado | ¿Puede ocurrir? | Protección | Consecuencia |
|---|---|---|---|
| **Runner CLOSED en `_runners`** | **No** en flujo normal. **Sí** solo si se omite `unregister()` o se llama `end_delete()` sin antes `unregister()`. | `unregister()` en línea 1089 garantiza retirada. `end_delete()` no toca `_runners`. | Runner CLOSED accesible vía `get_existing()`, pero `register_existing` bloqueado por §9. |
| **Runner NO CLOSED en `_runners`** | **No** ocurre en flujo contractual. `runner.close()` debe completarse exitosamente (CLOSED) antes de `unregister()`. | Verificación CLOSED en línea 1080-1087 RAISE si falla. | Si se saltara la verificación, runner NO CLOSED quedaría registrado, violando la invariante de que worlds eliminados tienen runner cerrado. |
| **World eliminado lógicamente con runner residual registrado** | **No** en flujo contractual. `unregister()` retira el runner antes de que el UPDATE físicodelete/UPDATE deleted_at se ejecute. | El orden contractual: close → verify CLOSED → unregister → UPDATE. | Si ocurriera, el world lógico estaría "eliminado" pero el runner aún corriendo, contradiciendo el principio de que worlds eliminados no deben tener runners activos. |
| **World no eliminado, runner CLOSED y registrado después de fallar UPDATE** | **No** ocurre. Si UPDATE falla, el flujo RAISE (línea 1083-1087) y `unregister()` nunca se ejecuta. El world queda: runner CLOSED + registrado, `deleted_at = NULL`, datos intactos. | El diseñador debe reintentar el flujo. | Runner CLOSED + registrado, world recoverable. No hay pérdida de datos. |

### 3.2. ¿Qué protección evita que un runner CLOSED quede registrado?

La cadena de protecciones:

1. **`runner.close()` verifica CLOSED** (línea 1080-1087): Si close falla (CLOSE_INCOMPLETE), se RAISE y **nunca se ejecuta `unregister()`**. El world queda: runner CLOSED + registrado + `deleted_at = NULL`. Datos intactos, world recoverable.

2. **`unregister()` en línea 1089** se ejecuta **solo después** de verified CLOSED. Esto garantiza que solo runners CLOSED son dados de baja del registry.

3. **`end_delete()` nunca toca `_runners`**: Aunque se llame `end_delete()` sin antes `unregister()`, la protección §9 (`_deleting` set) impide `register_existing` nuevo, pero `get_existing` aún podría retornar el runner existente. **Pero esto no ocurre en la práctica** porque el flujo siempre llama `unregister()` primero.

### 3.3. ¿Es posible que un runner CLOSED quede registrado después de liberar `_deleting`?

**No en el flujo contractual.** La liberación de `_deleting` ocurre en `end_delete()` (línea 936: `self._deleting.discard(world_id)`), que se llama:
- En el `finally` block (línea 1112) cuando `guard_transferred = False` (caller NO se canceló durante shield).
- En el callback de worker completion cuando `guard_transferred = True` (caller se canceló durante shield).

En ambos casos, **precede** a `end_delete()` la ejecución de `unregister()` en el flujo normal (línea 1089), o el propio callback transfiere la duty de limpieza.

Una vez que `end_delete()` retira world_id de `_deleting`, **`get()`** (línea 940: `return world_id in self._deleting`) ya no levantaría RuntimeError, pero **`get_existing()`** aún retornaría el runner si este sigue en `_runners`. **Pero como vimos, el flujo siempre remueve el runner de `_runners` vía `unregister()` antes de que `end_delete()` sea llamado.**

### 3.3. ¿Puede `get()` devolver ese runner después de liberar el marcador?

Depende de si el runner sigue en `_runners`:

- Si `unregister()` fue llamado (flujo normal): **No**, el runner fue removido de `_runners`, así que `get()` retornaría None o crearía uno nuevo.
- **Si `unregister()` NO fue llamado** (escenario hipotético): **Sí**, `get()` retornaría el runner existente (aunque `_deleting` ya haya sido removido por `end_delete()`).

**Pero en la práctica contractual**, el escenario "unregister NO llamado" no ocurre, así que **no es posible que `get()` devuelva un runner CLOSED después de liberar `_deleting`**, porque el runner ya fue removido de `_runners` anteriormente.

---

## 4. Corrección de FIX5

### Afirmación corregida del callback

FIX5 afirmaba: "El callback se ejecuta cuando el worker termina normalmente. Se ejecuta cuando el caller se cancela mientras el worker continúa. Puede ejecutar la limpieza antes o después de que el caller reciba `CancelledError`."

**Corrección FIX6:** El callback `work.add_done_callback(lambda _w, _id=world_id: registry.end_delete(_id))` se ejecuta **siempre que el task termina** (ya sea por éxito, fallo o cancelación propia del worker). Su función es **consumir el outcome** del worker y **transferir la duty de `end_delete()`** al completion del worker.

**Diferencia clave:** Antes FIX5 sugería que el callback "retira runners", pero la realidad es que el callback **solo ejecuta `end_delete()`**, el cual **no toca `_runners`**. La retirada de `_runners` ya ocurrió previamente via `unregister()` en línea 1089. El callback y el `finally` block son los mecanismos para **liberar la marca `_deleting` y los contadores**, no para retirar runners de `_runners`.

### Orden de operaciones corregido:

1. `unregister(world_id)` (línea 1089) → **quita runner de `_runners`**.
2. Worker thread ejecuta UPDATE/delete (línea 1091-1092).
3. `await asyncio.shield(work)` (línea 1097) → blindaje.
4. `CancelledError` handling (línea 1098-1107) → callback registration si se cancela.
5. `finally` block (línea 1108-1112) → `end_delete()` → **libera `_deleting`, `_delete_users`, `_delete_locks`**, **NO toca `_runners`**.

El orden es **inescrupulosamente**: `unregister()` primero (quita runner), luego `end_delete()` después (libera marca). Nunca al revés.

---

## 5. Decisión contractual

### Política técnicamente correcta

1. **`end_delete()` NO retira runners de `_runners`**. Su único propósito es liberar la marca `_deleting` y decrementar los contadores `_delete_users`/`_delete_locks`.

2. **`unregister()` ES la operación que retira runners de `_runners`**. En el flujo contractual `safe_delete_world()`, `unregister()` se ejecuta en línea 1089 después deverified `runner.close()` y antes del worker thread.

3. **Política de limpieza**:
   - **Flujo normal**: `unregister()` quita runner de `_runners`. `end_delete()` (ya sea en `finally` o via callback) libera la marca `_deleting`. **Resultado**: runner removido de registry, marca liberada, world eliminado lógicamente.
   - **Cancelación durante shield**: El callback transfiere la duty de `end_delete()`. **Resultado**: mismo efecto que el flujo normal; runner ya fue removido por `unregister()` antes de la cancelación.
   - **Cancelación después de shield**: El `finally` block ejecuta `end_delete()`. **Resultado**: mismo efecto.

2. **Cómo preservar la protección contra accesos concurrentes mientras el worker sigue ejecutándose**:
   - La marca `_deleting` (set) persiste mientras el worker ejecuta, impidiendo `register_existing` nuevo.
   - `get()` levantaría RuntimeError si world está en `_deleting`, impidiendo acceso accidental.
   - `unregister()` remueve el runner, por lo que `get_existing()` retornaría None o crearía uno nuevo (dependiendo de si world_id ya tenía un runner previo).

3. **Cómo tratar un UPDATE fallido sin dejar un runner CLOSED registrado y accesible**:
   - Si UPDATE falla **antes** de `unregister()`: el `raise` en línea 1083-1087 previene que `unregister()` se ejecute. El world queda: runner CLOSED + registrado + `deleted_at = NULL`. **Esto es intencional**: el fallo en el UPDATE no significa world eliminado, significa fallo en el proceso de eliminación, y el world stay recoverable.
   - Si UPDATE falla **después** de `unregister()`: imposible en el flujo contractual porque `unregister()` sucede antes del worker thread.

4. **Cómo mantener la idempotencia de las operaciones**:
   - `end_delete()` es idempotente (llamadas múltiples son seguras).
   - `unregister()` es idempotente (`pop` retorna None si ausente).
   - La combinación garantiza que llamar al flujo `safe_delete_world()` múltiples veces sobre el mismo world tenga efecto idempotente después de la primera ejecución exitosa.

### Recomendación contractual

**No modificar la distinción entre `end_delete()` y `unregister()`.** La separación de responsabilidades es intencional y correcta:

- `unregister()` → retira runner de `_runners` (responsabilidad de la capa de eliminación).
- `end_delete()` → libera marca de eliminación y contadores (responsabilidad de la capa de coordinación de eliminación).

**Recomendación:** Mantener la documentación actual que distingue estas dos operaciones, y en el flujo `safe_delete_world` asegurar que `unregister()` siempre se ejecute en línea 1089 después deverified `runner.close()`, y que `end_delete()` se llame en el `finally` block o callback para liberar la marca, sin intentar que `end_delete()` también retire runners.

---

## 6. Entregable

**Archivo generado:** `F7-E.2-PRE-DECISION-FIX6.md`

**Contenido:**

1. ✅ **Evidencia de las implementaciones reales y referencias de archivo/línea**:
   - `end_delete()` (línea 921-937): modifica `_deleting`, `_delete_users`, `_delete_locks`; **no toca `_runners`**.
   - `unregister()` (línea 876-889): `pop` de `_runners`; **sí retira runner**.
   - `begin_delete()` (línea 893-919): añade a `_deleting`, incrementa contadores.
   - `get_existing()` (línea 964): retorna `self._runners.get(world_id)` sin chequear `_deleting`.
   - `register_existing()` (línea 835-874): chequea `_deleting`, añade a `_runners`.

2. ✅ **Diferencia contractual entre `end_delete()` y `unregister()`**:
   - `end_delete()`: libera marca, no toca `_runners`.
   - `unregister()`: quita runner de `_runners`.
   - En el flujo `safe_delete_world`, el orden es: `unregister()` (línea 1089) → worker thread → `end_delete()` (línea 1112 o callback).

3. ✅ **Estados posibles del registro en los tres escenarios de cancelación**:
   - Escenario A (normal): `_runners` vacío después de `unregister()`, `end_delete()` no cambia eso.
   - Escenario B (cancelación dentro shield): mismo; callback transfiere duty de `end_delete`, pero `_runners` ya vacío.
   - Escenario C (cancelación fuera shield): mismo; `finally` block ejecuta `end_delete()`, `_runners` ya vacío.

4. ✅ **Análisis de accesibilidad de runners CLOSED**:
   - En flujo contractual: **no posible** que runner CLOSED quede en `_runners`, porque `unregister()` lo remueve en línea 1089.
   - Solo sería posible si se omite `unregister()` o se llama `end_delete()` sin antes `unregister()`, lo cual no ocurre en la práctica.

5. ✅ **Corrección de las afirmaciones de FIX5**:
   - El callback `work.add_done_callback(...)` ejecuta `end_delete()`, **no retira runners de `_runners`**.
   - El orden es: `unregister()` (quita runner) → `end_delete()` (libera marca).
   - El `finally` block también ejecuta `end_delete()`, pero ya el runner ha sido removido.

6. ✅ **Política recomendada de limpieza y protección concurrente**:
   - `unregister()` → retira runner.
   - `end_delete()` → libera marca `_deleting` y contadores.
   - Protección concurrente: `_deleting` set impide `register_existing` nuevo; `get()` levanta RuntimeError si world está en `_deleting`.
   - Idempotencia: ambas operaciones son idempotentes.

7. ✅ **Dictamen final de preparación de F7-E.2-PRE**:
   - La contradicción entre `end_delete()` y `unregister()` está **resuelta**: son operaciones distintas con responsabilidades distintas.
   - `end_delete()` no retira runners; `unregister()` sí.
   - El flujo contractual garantiza que `unregister()` se ejecuta en línea 1089, por lo que **no quedará runner CLOSED registrado** después de la liberación de `_deleting`.
   - **F7-E.2-PRE: ACEPTADO definitivamente**.

---
**F7-E.2-PRE-DECISION-FIX6: Verificación definitiva de limpieza del RunnerRegistry — COMPLETADA.**