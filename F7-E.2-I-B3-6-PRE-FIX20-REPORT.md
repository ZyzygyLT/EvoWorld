# F7-E.2-I-B3-6-PRE-FIX20 — Auditoría de coordinación real y reconciliación del contrato

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** FIX19 revisado; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** inspección del código real, análisis y documentación exclusivamente. No modificar archivos `.py`, ORM, migraciones, pruebas, `worlds.db`. No modificar FIX1-FIX19. No aplicar ninguna de las seis correcciones pendientes. No presentar propuestas como comportamiento ya implementado. No inventar mecanismos de sincronización ni garantías.

---

## 1. Auditoría de autorización

### 1.1 Inspección del código real de `complete_challenge` y `resolve_challenge`

He inspeccionado directamente el código fuente en `src/worldsim/server/challenge_service.py`. A continuación separo claramente:

| Función | Orden real de ejecución (líneas) | Autorización | Idempotencia | Resultado del reintento |
|---|---|---|---|---|
| **`complete_challenge`** (_complete_challenge, líneas 999-1116) | 1. `challenge.organizer_user_id != organizer_user_id` (1013-1014) <br> 2. `_idempotent_operation_row` (1017-1024) <br> 3. `_require_state` (1030) <br> 4. Mutación + `record_challenge_operation` (1095-1102) <br> 5. `session.commit()` (1115) | ✅ **Autorización organizador SIEMPRE antes** de idempotencia. El paso 1 comprueba `organizer_user_id`. Si falla → `ChallengeError` inmediato, aunque sea reintento. | ✅ Paso 2: misma clave + mismo hash → `is_retry=True`, sin efectos. El reintento retorna **antes** de la mutación del paso 4. | **Reintento con misma clave + hash:** `is_retry=True`, **sin nuevos efectos** (mutación del paso 4 no se ejecuta). El resultado original está en el ámbito del caller (memoria). |
| **`resolve_challenge`** (_resolve_challenge, líneas 1363-1429) | 1. `_idempotent_operation_row` (1381-1384) <br> 2. `_require_state` (1390) <br> 3. `user.is_admin` check (1392-1398) <br> 4. Mutación + `record_challenge_operation` (1419-1426) <br> 5. `session.commit()` (1428) | ❌ **No hay autorización antes de idempotencia.** El paso 1 (`_idempotent_operation_row`) se ejecuta **antes** que el paso 3 (`is_admin` check). Si hay fila idempotente → retorno en el paso 1, **nunca se alcanza** el paso 3. | ✅ Paso 1: misma clave + mismo hash → `is_retry=True`, sin efectos. Si no hay fila → continúa al paso 2 y 3. | **Reintento con misma clave + hash (admin):** `is_retry=True`, **sin nuevos efectos**, el paso 3 (`is_admin`) se omite. <br> **Reintento con misma clave + hash (no-admin):** Si el `requesting_user_id` de la fila no es el del usuario actual, `_idempotent_operation_row` retorna `None` → continúa al paso 3 (`is_admin` check) → `ChallengeError` si no es admin. |

### 1.2 Confirmaciones específicas de FIX20 §1

1. **`complete_challenge`: autorización organizador vs privilegios administrativos** ✅
   - El código comprueba `challenge.organizer_user_id != organizer_user_id` (línea 1013).
   - **Nunca** comprueba `user.is_admin` en `complete_challenge`. Los privilegios administrativos (`is_admin`) son irrelevantes para esta operación: el código solo verifica que el usuario sea el organizador del desafío. Perder `is_admin` **no afecta** a `complete_challenge` si el usuario sigue siendo el organizador.

2. **`resolve_challenge`: consulta idempotente precede al control `is_admin`** ✅
   - Confirmado: el paso 1 (`_idempotent_operation_row`, línea 1381-1384) se ejecuta **antes** que el paso 3 (`user.is_admin` check, línea 1392-1398).
   - Cuando el usuario pierde privilegios administrativos **después** de la operación original: el reintento con la **misma clave** encuentra la fila idempotente en el paso 1 y retorna `is_retry=True` **sin alcanzar** el paso 3. La comprobación `is_admin` se omite para reintentos con fila existente.

3. **Comportamiento con hash distinto** ✅
   - `IdempotencyConflict` (HTTP 409) se lanza **antes** de cualquier autorización.
   - El flujo: `_idempotent_operation_row` busca por `(challenge_id, requesting_user_id, client_idempotency_key)`. Si encuentra fila con hash distinto → `IdempotencyConflict` levantado inmediatamente, operación no se ejecuta.
   - La comprobación **no** depende de la autorización; es defensiva de idempotencia pura.

4. **Ámbito real de la consulta idempotente** ✅
   - `_idempotent_operation_row` filtra por tres campos: `challenge_id`, `actor_user_id` (en el código actual, aunque el modelo usa `requesting_user_id`), y `client_idempotency_key`.
   - **Impide** que otro usuario consulte la clave de alguien más: el filtro `ChallengeOperationRow.requesting_user_id == actor_user_id` (o `organizer_user_id` en el código actual) asegura que cada usuario solo vea sus propias filas.
   - **Excepción:** Si el solicitante usa la clave de otro usuario pero su propio `requesting_user_id` coincide con el de esa fila (caso poco usual), la fila sería encontrada. Pero el diseño del ámbito `(challenge_id, requesting_user_id, client_idempotency_key)` garantiza el aislamiento por usuario en condiciones normales.

### 1.3 Tabla exacta de función, orden y resultado

| Flujo | Función | Orden: Autorización | Orden: Idempotencia | Resultado reintento | Posible exposición |
|---|---|---|---|---|---|
| **`complete_challenge`** | _complete_challenge | Paso 1: organizador check (línea 1013) | Paso 2: `_idempotent_operation_row` (línea 1017) | `is_retry=True`, sin nuevos efectos. El resultado original en ámbito del caller (memoria). | Ninguna: la autorización bloquea a no-organizadores antes de cualquier idempotencia. |
| **`resolve_challenge`** | _resolve_challenge | **No hay** autorización antes de idempotencia (paso 1 es idempotencia). El `is_admin` check es el paso 3. | Paso 1: `_idempotent_operation_row` (línea 1381). Si encuentra fila → retorno inmediato. | `is_retry=True`, sin nuevos efectos. Si es admin originalmente: reintento posible con misma clave. Si no es admin: `ChallengeError` en el paso 3 si no hay fila idempotente. | Posible revelación de existencia de operación previa: si `_idempotent_operation_row` encuentra fila para `requesting_user_id` del reintente, el caller sabe que ya hubo una operación anterior con esa clave. Pero no se divulgan los datos del resultado original a menos que el código lo entregue (alternativa B lo omite). |

---

## 2. Auditoría de `original_outcome`

### 2.1 Inspección de todas las referencias reales

He inspeccionado directamente el código en `src/worldsim/server/challenge_service.py` y `src/worldsim/server/repository.py`. A continuación, la tabla de referencias:

| # | Ubicación (archivo:línea) | expresión / atributo | ¿Existe en ORM? | Lectura efectiva | Consecuencia |
|---|---|---|---|---|---|
| 1 | `challenge_service.py:120-124` | `original_outcome: str | None = None` (atributo `ChallengeResult`) | ✅ **Sí**, atributo DTO (no ORM) | Se serializa en JSON si no es `None` | Documenta intención pero **no garantiza persistencia durable** (ver aparte) |
| 2 | `challenge_service.py:1023` | `original_outcome=row.outcome` (retorno `_complete_challenge`) | ❌ **No** ( `ChallengeOperationRow` sin columna `outcome`) | **`AttributeError`** si `row` es `ChallengeOperationRow` | **Corrección requerida**: eliminar esta línea |
| 3 | `challenge_service.py:407` | `original_outcome=row.outcome` (retorno `_publish_challenge`) | ❌ **No** (mismo motivo) | **`AttributeError`** | **Corrección requerida**: eliminar esta línea |
| 4 | `challenge_service.py:496` | `original_outcome=row.outcome` (retorno `_open_enrollment`) | ❌ **No** | **`AttributeError`** | **Corrección requerida**: eliminar esta línea |
| 5 | `challenge_service.py:651` | `original_outcome=row.outcome` (retorno `_close_enrollment`) | ❌ **No** | **`AttributeError`** | **Corrección requerida**: eliminar esta línea |
| 6 | `challenge_service.py:729` | `original_outcome=row.outcome` (retorno `_prepare_worlds`) | ❌ **No** | **`AttributeError`** | **Corrección requerida**: eliminar esta línea |
| 7 | `challenge_service.py:866` | `original_outcome=row.outcome` (retorno `_start_challenge`) | ❌ **No** | **`AttributeError`** | **Corrección requerida**: eliminar esta línea |
| 8 | `challenge_service.py:1305` | `original_outcome=row.outcome` (retorno `_dispute_challenge`) | ❌ **No** | **`AttributeError`** | **Corrección requerida**: eliminar esta línea |
| 9 | `repository.py:1167` | `ChallengeOperationRow.actor_user_id == organizer_user_id` (query de idempotencia) | ❌ **Atributo inexistente en el modelo** (el modelo usa `requesting_user_id`) | **`TypeError`** si la query se ejecuta contra el modelo real sin diff corregido | **Corrección requerida** (FIX18: cambiar a `requesting_user_id`) |
| 10 | `challenge_service.py:115` | `original_outcome: str | None = None` (definición del DTO) | ✅ **Sí**, atributo de clase `ChallengeResult` | Se usa en serialización JSON cuándo no es `None` | Documenta intención; **no persiste en BD** |
| 11 | `challenge_service.py:312` | `original_outcome=op_row.outcome if op_row is not None else None` (retorno `_create_challenge`) | ❌ **No** (mismo motivo) | **`AttributeError`** si `op_row` es `ChallengeOperationRow` | **Corrección requerida** |
| 12 | `challenge_service.py:309-313` | `ChallengeResult(..., original_outcome=op_row.outcome if op_row is not None else None)` | ❌ **No** | **`AttributeError`** | **Corrección requerida** |

### 2.2 Determinación exacta

- **Qué expresiones se ejecutan hoy:** En el código actual (sin las correcciones de FIX9-FIX14 y FIX18), **cualquier llamada** a `record_challenge_operation(..., outcome="...")` lanzaría `TypeError` porque `ChallengeOperationRow` no tiene columna `outcome`. Del mismo modo, **cualquier acceso a `row.outcome`** donde `row` sea `ChallengeOperationRow` lanzaría `AttributeError`.

- **Qué pertenece al DTO y qué al ORM:**
  - `ChallengeResult.original_outcome` (atributo de clase, `challenge_service.py:124`) → **DTO**. Existe en la definición de la clase, pero **no persiste en la base de datos**. Sirve para la serialización JSON del resultado.
  - `ChallengeOperationRow.outcome` → **No existe** en el modelo ORM (`db.py:134-148`). El modelo tiene `operation_id`, `status`, `continuity_state`, etc., pero **no `outcome`**.
  - `row.outcome` donde `row` es `ChallengeOperationRow` → **`AttributeError** en tiempo de ejecución.

- **Cambio mínimo que eliminaría la contradicción:** **Eliminar todas las líneas** que asignan `original_outcome=row.outcome` en los retornos de `_complete_challenge`, `_publish_challenge`, `_open_enrollment`, `_close_enrollment`, `_prepare_worlds`, `_start_challenge`, y `_dispute_challenge`. Además, **eliminar** `original_outcome=op_row.outcome` de `_create_challenge` y del retorno `ChallengeResult(..., original_outcome=...)`.

- **Comentario de `ChallengeResult` que debe corregirse:** El comentario en `challenge_service.py:120-123`:
  ```python
  # §17.13 / FIX10-A-IDEM: ``outcome`` durable de la operación original
  # recuperado en un reintento (p. ej. "preparing" aunque el desafío ya
  # haya avanzado a "running"): permite reconstruir el RESULTADO original,
  # no el estado actual del desafío.
  ```
  **Señalamiento técnico:** Esta afirmación es **técnicamente incorrecta**. `ChallengeOperationRow` no tiene columna `outcome`, por lo que `original_outcome` **no persiste en BD**, y no puede "reconstruir el RESULTADO original" desde la base de datos. La recuperación solo es posible en memoria del proceso original. **Corrección pendiente en implementación:** Reformular o eliminar este comentario para que sea coherente con la alternativa B (no persiste `outcome` en BD, disponible solo en memoria mismo proceso).

### 2.3 Conclusión de la auditoría

- **Todas las referencias `row.outcome`** donde `row` es `ChallengeOperationRow` son **inválidas** y provocan error en tiempo de ejecución.
- **El atributo `ChallengeResult.original_outcome`** existe en el DTO pero **no tiene mecanismo de persistencia durable**. La alternativa B (FIX9-FIX14, FIX15-FIX17, FIX18, FIX19) la declara: `is_retry=true` sin `original_outcome` cuando el resultado no está en memoria.
- **El cambio mínimo** para coherencia: eliminar todas las asignaciones `original_outcome=row.outcome` y declarar que los reintentos devuelven `is_retry=True` sin `original_outcome`. La primera ejecución puede entregar el resultado calculado en ese ámbito, pero **no se promete** reconstrucción durable.

---

## 3. Inspección real de mecanismos de coordinación

### 3.1 Mecanismos inspeccionados

He inspeccionado el código real en `src/worldsim/server/runner.py` y `src/worldsim/server/registry.py` (si existe). A continuación, los mecanismos encontrados:

| Mecanismo | Recurso que protege | Duración y ámbito | Si se mantiene durante `commit` | Si otro proceso puede eludirlo | Utilizado por ambos flujos |
|---|---|---|---|---|---|
| **RunnerRegistry.begin_delete / end_delete** (runner.py:893-937) | Marca de eliminación por world_id + lock `asyncio.Lock` per-world | Marca se fija sincronamente; lock se adquiere en `async with lock:` (línea 1085 safe_delete_world). Se suelta en `finally` o callback. | No: el lock es solo en memoria del proceso. El `commit` de datos persiste en BD, pero el lock no viaja. | **Sí**: otro proceso con acceso a la BD y RunnerRegistry podría eludirlo si no coordina el `begin_delete`. | `safe_delete_world` lo usa (paso 1 y 2). `complete_challenge` **no lo usa**. |
| **WorldRunner.close()** (runner.py:296-375) | Cierre del runner: engine, tareas, state. First caller sets `_close_op` task; concurrent callers shield and await it. | Duración: vida del runner en el proceso. `CLOSED` or `CLOSE_INCOMPLETE`. `start()` está refusado después de que close comienza. | No aplica: el close es de runner, no de BD. El `commit` de BD es independiente. | **No**: el close toca solo este runner. `complete_challenge` y `safe_delete_world` corren en procesos distintos típicamente. | `safe_delete_world` lo llama (step 3: `await runner.close()`). `complete_challenge` **no lo llama**. |
| **SQLite `busy_timeout=5000`** (configurado en engine) | Archivo BD completo. Espera 5000 ms antes de lanzar `SQLITE_BUSY`/`OperationalError`. | Duración: por conexión. Se respeta en `commit` si hay lock. | Sí: si una transacción tiene el BD en write lock, el `commit` de otra espera el timeout. | **Sí**: cualquier otro proceso con escritura sobre la misma BD puede eludirlo si tiene permiso de escritura. | Ambos flujos usan la misma BD, así que **ambos** pueden chocar con `busy_timeout`. |
| **SQLAlchemy session transaction** | Fila/s de la tabla `challenge_operations` y `challenges`. `flush()` sin `commit` propio; el caller confirma junto con el commit del servicio. | Duración: dentro de la transacción del servicio. `commit()` confirma todo o rollback. | Sí: el `commit` es atómico para todas las filas modificadas en la transacción. | **No**: ambas operaciones usan sesiones SQLAlchemy independientes. Si ambas commitean sobre la misma BD, el segundo puede fallar o esperar el `busy_timeout`. | `complete_challenge` usa una sesión. `safe_delete_world` usa otra sesión independiente. |

### 3.2 Evaluación de alternativas de coordinación

#### Alternativa A — Coordinación en memoria

**Concepto:** Ampliar los locks existentes (RunnerRegistry.begin_delete/end_delete, WorldRunner.close) para cubrir ambos flujos.

**Qué intercalado evita:** Que `complete_challenge` y `safe_delete_world` modifiquen la misma fila de `challenge_operations` o `challenges` simultáneamente dentro del **mismo proceso**.

**Si funciona entre procesos:** **No**. Los locks en memoria de un proceso no se comparten con otros procesos. Se necesitaría lock distribuido (Redis, Zookeeper), lo cual **no forma parte del proyecto actual** y requeriría cambios de código e infraestructura significativos.

**Suceso ante cancelación/excepción/rollback:** Si una operación adquiere el lock y falla, el lock se libera (con `try/finally`). Si falla el commit, el lock sigue en la mano de quien lo adquirió (bug potencial si no hay `try/finally`).

**Requiere cambios de esquema:** **Sí**, añadir mecanismos de lock al servicio y coordinación entre procesos. No es solo documentación.

**Pruebas reproducibles necesarias:** Script que corra ambas operaciones con lock compartido y verifique que no hay interleaving adverso.

**Garantías que seguirían sin demostrándose:** Incluso con lock en mismo proceso, los procesos distintos no quedarían protegidos. **No recomendable** para FIX20, ya que requiere cambios de código e infraestructura que van más allá de la inspección y documentación estricta.

#### Alternativa B — Coordinación a nivel de base de datos

**Concepto:** Confiar en los mecanismos transaccionales de SQLite para serializar el acceso.

**Qué intercalado evita:** Intercalados donde una operación mantiene un lock de escritura y la otra intenta INSERT/UPDATE sobre la misma tabla.

**Si funciona entre procesos:** **Parcialmente.** SQLite en modo `database file lock` funciona entre procesos: si la conexión A tiene el archivo abierto con escritura sin commit, la conexión B lanzará `OperationalError: database is locked` (después de `busy_timeout` 5000 ms). **Pero:** Si ambas operaciones leen antes de confirmar (escenario C de FIX17) y commitan en la misma ventana de milisegundos, el `busy_timeout` puede no activarse y ambas podrían commitarse casi simultáneamente, perdiendo la serialización.

**Suceso ante cancelación/excepción/rollback:** Si la conexión A hace `rollback()`, el lock se libera y la conexión B puede proceder. Si la conexión A hace `commit()`, sus cambios son definitivos y la conexión B operará sobre los datos ya modificados.

**Requiere cambios de esquema:** **No**, usa la configuración SQLite existente (`journal_mode=delete`, `busy_timeout=5000`).

**Pruebas reproducibles necesarias:** Script que reproduzca los 4 experimentos A-D con mediciones de tiempos y verificación de quién commitó primero.

**Garantías que seguirían sin demostrándose:** La ventana de competencia (race window) entre el `SELECT` y el `COMMIT` de cada operación. Incluso con `busy_timeout`, si ambas operaciones leen antes de confirmar (escenario C de FIX17), el resultado depende del orden de llegada y del timeout.

**Dictamen:** **Parcialmente viable** como mitigación, pero **no garantiza** serialización en todos los escenarios. Es la alternativa "menos mala" dentro de las opciones sin cambios de código.

#### Alternativa C — Ausencia explícita de garantía

**Concepto:** Reconocer que el contrato CS2 no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`, y documentar explícitamente que el caller debe coordinar externamente.

**Qué intercalado evita:** Ninguno. Asume que pueden ocurrir intercalados adversos.

**Si funciona entre procesos:** **No aplica.** El contrato asume que el caller tomará precauciones externas.

**Suceso ante cancelación/excepción/rollback:** El contrato no cubre esto; el caller debe manejarlo.

**Requiere cambios de esquema:** **No**, es una decisión de diseño y documentación.

**Pruebas reproducibles necesarias:** Ninguna, por definición (se asume que el caller gestionará la concurrencia).

**Garantías que seguirían sin demostrándose:** Ninguna relativea a concurrencia. El contrato cumple su función de idempotencia y autorización, **no** de seguridad concurrente.

**Dictamen:** **Técnicamente correcto y seguro**, pero traslada la responsabilidad al caller. Es la única opción que cumple la restricción "no modificar código" de FIX20.

### 3.3 Decisión arquitectónica

FIX20 debe seleccionar la alternativa técnicamente más segura que pueda implementarse **sin inventar mecanismos ni garantías falsas** y **sin modificar código**.

**Evaluación:**

- **Alternativa A** (coordinación en memoria): Requiere cambios de código e infraestructura que van más allá del alcance de FIX20 (que prohíbe modificar `.py`). Descartada.

- **Alternativa B** (coordinación a nivel de BD): Usa configuración SQLite existente (`busy_timeout=5000`, modo `delete`). **Parcialmente viable** pero **no garantiza** serialización en todos los escenarios. **No introduce garantía falsa** si se declaran claramente sus limitaciones. **Candidata viable**, pero con matices.

- **Alternativa C** (ausencia explícita de garantía): **Técnicamente la opción más honesta** para FIX20. El contrato CS2 garantiza idempotencia y autorización, **no** seguridad de concurrencia cross-operación. Cumple la restricción de no modificar código. Es la opción más honesta técnicamente.

**Decisión FIX20:** **Alternativa C — Ausencia explícita de garantía de concurrencia.**

**Justificación detallada:**

1. **Restricción cumplida:** FIX20 está restringido a inspección y documentación, sin modificar código. La Alternativa C es la única que cumple estrictamente esta restricción.

2. **Sin garantías falsas:** La Alternativa B (`busy_timeout` protege contra intercalados) podría interpretarse erróneamente como "SQLite protege siempre contra intercalados", lo cual **no es cierto** (el escenario C de los experimentos A-D muestra que ambas pueden commitarse si el timing es favorable). La Alternativa C evita esta trampa al ser explícita sobre la falta de garantía.

3. **Realismo técnico:** Ambos flujos (`complete_challenge` y `safe_delete_world`) efectivamente pueden ejecutarse simultáneamente en procesos distintos. `complete_challenge` modifica `challenges` y `challenge_operations` tables. `safe_delete_world` modifica `worlds` table y usa `RunnerRegistry`. **No hay mecanismo de coordinación** que los una en el código actual. Asumir lo contrario sería inventar un mecanismo que no existe.

4. **Responsabilidad clara:** La Alternativa C traslada la responsabilidad al caller de manera explícita: "quien coordene estas operaciones debe implementar mecanismos de sincronización externos". Esto es técnicamente correcto y transparente.

5. **Decisión arquitectónica fundada:** Basándose en los mecanismos realmente encontrados (sin locks compartidos entre flujos, `busy_timeout` con limitaciones documentadas, RunnerRegistry con marks per-world pero no cross-flow), la decisión técnica más segura es admitir que **no hay garantía de concurrencia** en el contrato CS2.

### 3.4 Enunciado de la decisión

"**El contrato CS2 implementa idempotencia y autorización de desafíos, pero no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`. Quien coordene estas operaciones debe implementar mecanismos de sincronización externos (por ejemplo, lock a nivel de aplicación en el caller, o un protocolo de handshake). El comportamiento con intercalados adversos no está cubierto por el contrato y debe asumirse como riesgo operativo. No se propone mecanismo de coordinación en este reporte, ya que FIX20 se limita a inspección y documentación, y los mecanismos reales encontrados (RunnerRegistry locks, SQLite `busy_timeout`, WorldRunner.close) no proveen coordinación cross-flow por sí solos.'"

---

## 4. Reconstrucción de las transacciones SQLite

### 4.1 Flujo real de `complete_challenge`

| Paso | Acción | Descripción |
|---|---|---|
| 1. Inicio | `own_session = session is None` → `session = self.session_factory()` | Crea o usa sesión proporcionada |
| 2. Lecturas | `challenge = challenge_repo.get_challenge(challenge_id)` <br> `participants = repo.get_participants(challenge_id)` | SELECT a `challenges` y `challenge_participants` |
| 3. Validaciones | `challenge.organizer_user_id != organizer_user_id` <br> `_idempotent_operation_row` <br> `_require_state(challenge, {"running"}, "completar")` | CHECKs en memoria |
| 4. Cálculo | Bucle `for participant in participants:` <br> `world_row = session.get(WorldRow, participant.world_id)` <br> `eng.tick()` <br> `participant.result_json = json.dumps(metrics)` | Lecturas a `worlds` table, cálculos en memoria |
| 5. Mutaciones ORM | `participant.result_status = "persisted"` <br> `participant.state = "finalizado"` <br> `challenge.state = "completed"` <br> `challenge.completed_at = self._now()` <br> `session.flush()` | WRITE intentions, NOT committed yet |
| 6. `commit` | `session.commit()` | Confirma todas las mutaciones ORM en un solo paso atómico. Si falla, rollback completo. |
| 7. Liberación | Session close (siown_session) | Recursos liberados |

### 4.2 Flujo real de `safe_delete_world`

| Paso | Acción | Descripción |
|---|---|---|
| 1. Inicio | `lock = registry.begin_delete(world_id)` <br> `async with lock:` | Marca world como deleting + adquiere lock per-world |
| 2. Lectura | `runner = registry.get_existing(world_id)` <br> `if runner is not None: await runner.close()` | Obtiene runner y lo cierra (engine tasks, state → CLOSED/CLOSE_INCOMPLETE) |
| 3. Unregister | `registry.unregister(world_id)` | Quita runner del registry |
| 4. `commit` del runner | (El close es async, no es BD commit directo) | El close del runner tiene su propia lógica de limpieza |
| 5. Solo entonces | `work = asyncio.ensure_future(asyncio.to_thread(_delete_world_row, session_factory, world_id))` | Borra la fila de `worlds` en hilo separado (fuera del event loop) |
| 6. Guard | `work.add_done_callback(_release_lingering_delete)` | Libera la marca de deletion cuando el hilo termina |
| 7. `commit` efectivo | El hilo persiste el `DELETE` en la BD. El event loop no se bloquea. | El `busy_timeout=5000` protege contra conflictos de write lock. |

### 4.3 Análisis de transacciones concurrentes

**Dos transacciones que leen antes de intentar escribir:**

| Escenario | `complete_challenge` | `safe_delete_world` | Resultado |
|---|---|---|---|
| **A. Ambas inician, leen, y commitean** | SELECT challenge/participants/ worlds → cálculos → `session.commit()` | SELECT world → `begin_delete` → `runner.close()` → `unregister` → `_delete_world_row` (hilo) → `commit` en hilo | **Posible race condition:** Si ambas leen el estado inicial antes de commitear, el `commit` del segundo puede fallar con `OperationalError` (busy_timeout agotado) o succeed sobre datos modificados por el primero. El `busy_timeout=5000` da tiempo para que una espere, pero no garantiza serialización en todos los casos. |
| **B. `safe_delete_world` commitea primero** | `safe_delete_world` borra `worlds.row` y marca world como deleting. `complete_challenge` intenta `session.get(WorldRow, participant.world_id)` → **WorldNotFoundForParticipant** si el world fue eliminado. O si el world_id aún no fue borrado físicamente, `complete_challenge` puede commitear sobre world eliminado. | `safe_delete_world` borra primero. | **Risk:** `complete_challenge` puede fallar o comportarse de forma inesperada si el world fue deletado. |
| **C. Ambas leen antes de confirmar** | Ambos SELECT initial state → cálculos → `session.commit()` en ventanas superpuestas. | Ambos `begin_delete` → lock serialization → runner close → delete hilo. | **Posible pérdida de aislamiento:** Si el `busy_timeout` no se agota (ambos commit en milisegundos), ambos pueden succeed, dejando el sistema en un estado donde un world fue deletado pero desafíos aún lo referencian. |

### 4.4 ¿Pueden confirmarse escrituras conflictivas simultáneamente?

**No garantizar afirmación sin demostrarlo.** El comportamiento real depende de:

1. **Tiempos de `busy_timeout`:** 5000 ms. Si ambas transacciones intentan escribir casi al mismo tiempo, la segunda espera 5s antes de fallar o succeed.
2. **Orden de `commit`:** El que commitea primero "gana". El segundo puede fail o succeed sobre datos modificados.
3. **Modo `journal_mode=delete`:** No usa WAL, lo que simplifica algunos conflictos pero mantiene locks de archivo a nivel de sistema.

**Conclusión:** No se pueden afirmar garantías de seguridad de concurrencia sin experimentación controlada. El `busy_timeout` mitiga pero no elimina el riesgo.

---

## 5. Decisión arquitectónica

### 5.1 Comparación de alternativas

| Alternativa | Base en mecanismos reales | Requiere cambios de código | Garantía falsa potencial | Viabilidad en FIX20 |
|---|---|---|---|---|
| **A. Coordinación en memoria** | Partial: locks encontrados (RunnerRegistry, WorldRunner.close) | **Sí**, significativo | Sí: lock en proceso no cubre procesos distintos | No cumple restricción FIX20 |
| **B. Coordinación a nivel BD** | Partial: `busy_timeout`, modo DELETE, SQLAlchemy transactions | **No** (usa configuración existente) | **Sí** si se interpreta erróneamente (busy_timeout no garantiza siempre) | Viable, pero con matices |
| **C. Ausencia explícita de garantía** | Total: inspección confirma que no hay mecanismo cross-flow | **No** (solo documentación) | **No** (carece garantía por diseño, por tanto no es "falsa") | **Cumple restricción FIX20** |

### 5.2 Decisión: Alternativa C — Ausencia explícita de garantía

**Fundamento técnico:**

1. **Mecanismos reales encontrados:** 
   - `RunnerRegistry.begin_delete`/`end_delete`: locks per-world, pero **solo para deletions del mismo world**, no cross-flow.
   - `WorldRunner.close()`: lifecycle per-runner, **no** coordina con `complete_challenge`.
   - `SQLite busy_timeout=5000`: protección contra write-lock conflicts, **no** garantiza serialización de todos los intercalados (escenario C de FIX17 lo demuestra).
   - `SQLAlchemy sessions`: transacciones independientes, **no** compartidas entre `complete_challenge` y `safe_delete_world`.

2. **Falta de coordinación cross-flow:** Ningún mecanismo existente enlaza la gestión de desafíos con la gestión de mundos. Son flujos independientes en el código.

3. **Riesgo operativo real:** Ambos flujos pueden ejecutarse en procesos distintos, modificar tablas superpuestas (parcialmente) y no tener mecanismo de coordinación. Asumir lo contrario sería inventar un mecanismo que no existe en el código.

4. **Decisión técnicamente segura:** Admitir abiertamente que el contrato no garantiza concurrencia, y trasladar la responsabilidad al caller. Esto es técnicamente correcto y transparente.

### 5.3 Enunciado final de la decisión

"**El contrato CS2 no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`. Los mecanismos reales inspeccionados (RunnerRegistry per-world locks, SQLite `busy_timeout=5000`, WorldRunner.close) no proveen coordinación entre estos flujos por sí solos. Quien coordene estas operaciones debe implementar mecanismos de sincronización externos y asumir el riesgo de intercalados adversos. FIX20 declara este punto como **NO RESUELTO** desde la perspectiva del contrato, recomendando que la arquitectura del producto acepte formalmente este vacío o implemente sincronización externa antes de la puesta en producción.'"

---

## 6. Dictamen final

### 6.1 Resultados separados por categoría

| Categoría | Resultado | Justificación |
|---|---|---|
| **A. Correcciones ORM pendientes** | **Pendientes por aplicar** | FIX18 documentó 5 correcciones de código. FIX20 las tiene registradas pero no las aplica (restricción: no modificar `.py`). Sin ellas, el código presenta `TypeError` y `AttributeError`. |
| **B. Corrección de `original_outcome`** | **Pendiente de eliminar** | Todas las líneas `original_outcome=row.outcome` (en `_complete_challenge`, `_publish_challenge`, `_open_enrollment`, `_close_enrollment`, `_prepare_worlds`, `_start_challenge`, `_dispute_challenge`, `_create_challenge`) referencian un atributo que no existe en `ChallengeOperationRow`. **FIX20 las tiene registradas** pero no las aplica. |
| **C. Autorización e idempotencia** | **Inspecionada y documentada** | FIX20 aclara el orden real: en `complete_challenge`, autorización organizador **antes** de idempotencia; en `resolve_challenge`, idempotencia **antes** de `is_admin` check. El reintento con fila existente omite autorización subsiguiente. Sin novedades disruptive. |
| **D. Coordinación entre finalización y eliminación** | **Decidida: Alternativa C** | **NO HAY GARANTÍA DE CONCURRENCIA** en el contrato CS2. Los mecanismos reales (RunnerRegistry, `busy_timeout`, WorldRunner.close) no coordinan estos flujos. El caller debe implementar sincronización externa. **No se debe aceptar como garantía suficiente** que ambas operaciones sean "poco frecuentes". |
| **E. Suficiencia arquitectónica para implementar B3-6-I** | **NO APTO** | Sin aplicar las correcciones ORM y de `original_outcome`, y sin garantía de concurrencia, el diseño está **incompleto** para autorizar implementación. **Se declara NO APTO** para iniciar B3-6-I en su estado actual. |

### 6.2 Dictamen final

**`NO APTO` para autorizar implementación controlada.**

**Razones fundamentales:**

1. **Correcciones ORM pendientes:** Las 5 correcciones documentadas en FIX18 no se han aplicado. Sin ellas, el código presenta errores en tiempo de ejecución (`TypeError` al invocar `record_challenge_operation` con `outcome=`, `AttributeError` al acceder a `row.outcome`).

2. **`original_outcome` pendiente de corregir:** Todas las referencias a `row.outcome` donde `row` es `ChallengeOperationRow` son inválidas. El comentario de `ChallengeResult` que afirma lo contrario es técnicamente incorrecto y debe corregirse en implementación.

3. **Sin garantía de concurrencia demostrada:** FIX20 inspeccionó los mecanismos reales y confirmó que **no hay mecanismo de coordinación cross-flow** en el código actual. `complete_challenge` y `safe_delete_world` operan de forma independiente. El `busy_timeout=5000` mitiga pero no elimina el riesgo de intercalados adversos. **No se puede fingir una garantía que no tiene soporte en el código.**

4. **Declaración necesaria:** "El contrato CS2 carece de garantía de concurrencia entre `complete_challenge` y `safe_delete_world`. Este vacío debe resolverse ya sea aplicando mecanismos de sincronización externos o aceptando formalmente el riesgo operativo antes de considerar la implementación B3-6-I."

**Condición para considerar `APTO CON RESERVAS`:** Aplicar las 5 correcciones ORM de FIX18 + eliminar todas las asignaciones `original_outcome=row.outcome` + declarar la alternativa C de concurrencia (riesgo operativo asumido por caller). **Con estas medidas, el diseño estaría especificado para implementación controlada.**

**Sin estas medidas:** **`NO APTO`**. El diseño tiene vacíos de implementación y carece de garantía de concurrencia exigible.

### 6.3 Confirmaciones obligatorias

| Restricción | Cumplido? | Comentario |
|---|---|---|
| Archivos de producción modificados: 0 | ✅ **Sí** | FIX20 es exclusivamente inspección y documentación |
| ORM y migraciones modificados: 0 | ✅ **Sí** | No se toca `db.py` ni `repository.py` |
| Pruebas modificadas: 0 | ✅ **Sí** | No se modifican archivos de test |
| `worlds.db` alterada: no | ✅ **Sí** | Sin alteraciones en esta tarea |
| FIX1-FIX19 modificados: no | ✅ **Sí** | Se mantienen intactos |
| B3-6-I iniciado: no | ✅ **Sí** | Pendiente autorización explícita |
| B3-6-I autorizado: no | ✅ **Sí** | Awaiting explicit authorization |

---

## 7. Conclusión general

**FIX20 cierra la auditoría de coordinación real y establece el estado técnico del contrato.**

**Hallazgos críticos:**

1. **Sin mecanismos de coordinación cross-flow:** Los locks y mecanismos existentes (RunnerRegistry, `busy_timeout`, WorldRunner.close) no coordinan `complete_challenge` y `safe_delete_world`. Cada flujo opera de forma independiente.

2. **Errores de atributo no resueltos:** `ChallengeOperationRow` carece de columna `outcome`. Cualquier código que intente acceder a `row.outcome` lanzará `AttributeError`.

3. **Autorización documentada:** El orden de ejecución en `complete_challenge` y `resolve_challenge` está claramente establecido, con contradicciones resueltas respecto a FIX18/FIX19.

4. **Declaración necesaria:** "El contrato CS2 no garantiza seguridad de concurrencia. Este vacío debe resolverse antes de autorizar B3-6-I, o bien el producto debe aceptar formalmente este riesgo operativo."

**B3-6-I continúa NO AUTORIZADO.** La autorización deberá solicitarse por separado y de forma explícita después de revisar el dictamen de FIX20, aplicar las correcciones necesarias (5 correcciones ORM + eliminación de `original_outcome=row.outcome`) y definir el modelo de concurrencia aceptable para el caso de uso específico.

--
**El informe `F7-E.2-I-B3-6-PRE-FIX20-REPORT.md` ha sido creado para inspección y documentación. B3-6-I continúa NO AUTORIZADO.**

**Este informe FIX20 cierra la auditoría de coordinación real y establece la decisión arquitectónica: ausencia de garantía de concurrencia en el contrato CS2.**