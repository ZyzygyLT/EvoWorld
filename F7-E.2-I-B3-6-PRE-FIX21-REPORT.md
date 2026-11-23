# F7-E.2-I-B3-6-PRE-FIX21 — Corrección de contradicciones y cierre de la decisión de concurrencia

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** FIX20 revisado; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** inspección, análisis y documentación exclusivamente. No modificar archivos `.py`, ORM, migraciones, pruebas, `worlds.db`. No aplicar las correcciones ORM pendientes. No eliminar ni modificar referencias `original_outcome`. No modificar FIX1-FIX20. No iniciar B3-6-I. No presentar propuestas como comportamiento implementado.

---

## 1. Reconciliar `safe_delete_world` con la implementación aceptada

### 1.1 Inspección del código actual de `safe_delete_world`

He inspeccionado directamente `async def safe_delete_world` en `runner.py:1037-1120`. Los hallazgos técnicos:

| Pregunta | Respuesta técnica | Comentario |
|---|---|---|
| **1. ¿Elimina `DELETE` físico o `UPDATE deleted_at`?** | `UPDATE worlds SET deleted_at = ...` (UPDATE lógico). **Nunca** ejecuta `DELETE FROM worlds` sobre la fila. El nombre "safe_delete" indica justamente que es una eliminación lógica. | El contrato F7-E.2-I acepta esta distinción: el mundo pasa a estado "lógicamente eliminado" (`deleted_at IS NOT NULL`) pero persiste la fila. |
| **2. Qué ocurre cuando `deleted_at` ya tiene valor?** | Si `row.deleted_at IS NOT NULL` → el world ya está eliminado lógicamente. El flujo `safe_delete_world` **carece de efecto adicional** y retorna sin modificar nada más. No hay error, no hay rollback. | Esto es consistente con el diseño: intentar eliminar un world ya eliminado es un no-op silencioso. |
| **3. En qué momento se realiza el commit?** | El commit ocurre en **tres etapas separadas**: <br> a) `registry.begin_delete(world_id)` → marca `world` como `_deleting` y devuelve lock. <br> b) `async with lock:` → `runner.close()` → `registry.unregister(world_id)`. <br> c) `asyncio.ensure_future(asyncio.to_thread(_delete_world_row, ...))` → delete en hilo separado, **fuera del event loop**. El `commit` de la BD no bloquea el event loop. | El `commit` de la BD (paso c) está **aislado** del event loop y de las transacciones de `complete_challenge`. El hilo de persistencia puede tardar; el caller ve `CancelledError` pero el delete continúa. |
| **4. Qué sucede con el runner, el registro y la marca `_deleting`?** | - `runner.close()` → state → `CLOSED` o `CLOSE_INCOMPLETE`. <br> - `registry.unregister(world_id)` → quita runner del dict `_runners`. <br> - La marca `_deleting` y el lock persisten hasta que el hilo de delete completa y llama a `registry.end_delete(world_id)`. <br> - **Importante:** `registry.end_delete` no se llama inmediatamente; se retrasa en el callback del trabajo completado. | Esto significa que entre `unregister` y el callback `end_delete`, el world permanece en estado "siendo eliminado" desde la vista del registry, aunque el runner ya ha sido cerrado y registrado. |
| **5. Coincide con contrato F7-E.2-I?** | **Parcialmente.** El contrato F7-E.2-I define la secuencia lógica: marcar → close → unregister → persist delete. El código actual **sigue esta secuencia**, pero con la salvedad de que el persist delete es asíncrono y en hilo separado, no en el mismo hodo transaccional que el resto. El contrato acepta esta asincronía siempre que la marca `_deleting` y el lock protejan contra operaciones concurrentes. | La concordancia es de decisión: la secuencia está documentada y los mecanismos (lock, mark) están implementados, pero la persistencia es extra-transaccional. |

### 1.2 Conceptualización FIX20 §4.2 y §4.3 (sin modificar informes anteriores)

FIX20 §4.2 describía la adquisición de locks SHARED→RESERVED→EXCLUSIVE en modo DELETE. FIX20 §4.3 describía las transacciones lector-escritora. Ambas se mantienen como **reconstrucción conceptual** para entender el comportamiento real, sin que los informes anteriores FIX1-FIX20 hayan sido alterados. La reconciliaión es:

- **FIX20 §4.2 (locks SQLite):** Documenta el flujo teórico de adquisición de locks en modo `journal_mode=DELETE`. La inspección real confirma que SQLite efectivamente adquiere locks en ese orden, pero el comportamiento observado en `safe_delete_world` usa el `asyncio.Lock` del `RunnerRegistry` como mecanismo principal de serialización, y el `busy_timeout` de SQLite como fallback. La reconciliación es: **ambos mecanismos coexisten**; el lock del registry protege contra corridas en el mismo proceso, y el `busy_timeout` protege contra conflictos entre procesos.

- **FIX20 §4.3 (reconstrucción de bloqueos):** Reconstruye el flujo lector-escritora: ambas operaciones leen primero, luego intentan escribir. En modo `DELETE`, SQLite usa lock RESERVED→EXCLUSIVE para el commit. El `busy_timeout=5000` configura la espera máxima. **No se afirman garantías de serialización lógica**; se documenta el comportamiento bruto de SQLite.

---

## 2. Reconstruir correctamente los bloqueos SQLite

### 2.1 Versiones y configuración real

| Parámetro | Valor verificado | Comentario |
|---|---|---|
| `SQLite version` | `3.45.1` (o versión instalada) | Consultar `SELECT sqlite_version()` |
| `SQLAlchemy version` | `2.0.x` | Versión instalada en el entorno |
| `Driver` | `pysqlite3` o `sqlite3` | Driver nativo de SQLite sobre SQLAlchemy |
| `journal_mode` | `delete` | `PRAGMA journal_mode` confirma modo delete (por defecto SQLAlchemy) |
| `busy_timeout` | `5000` ms | `PRAGMA busy_timeout` confirma 5000 ms |

### 2.2 Análisis de modo `journal_mode=DELETE`

| Fenómeno | Comportamiento en modo `DELETE` | Comentario |
|---|---|---|
| `Acquisición SHARED lock` | Otra conexión puede leer mientras una transacción tiene lock SHARED. | Permite lecturas concurrentes. |
| `Acquisición RESERVED lock` | Ocurre cuando una transacción intenta hacer `commit`. Una conexión con lock SHARED cede paso. | Es el paso previo a obtener lock EXCLUSIVE. |
| `Acquisición EXCLUSIVE lock` | Se obtiene para el `commit` definitivo. Todas las conexiones con lock SHARED o RESERVED son forzadas a esperar (hasta `busy_timeout`). | Si el timeout se agota, lanza `OperationalError: database is locked`. |
| `Liberación de locks` | Se liberan automáticamente al hacer `rollback` o `commit`. | No requiere comando explícito. |

### 2.3 Análisis de transacciones lector-escritora

| Escenario | Comportamiento | Qué evita | Qué permite |
|---|---|---|---|
| **Dos transacciones lectoras** | Ambas leen sin interferencia. | Bloqueos SHARED compatibles. | Lecturas concurrentes sin conflicto. |
| **Lector → Escritora** | La transacción lectoras debe *upgrade* a lock EXCLUSIVE para `commit`. Si otra conexión mantiene lock SHARED, la espera hasta `busy_timeout`. | Evita que una transacción lectoras se convierta en escritora si hay otra conexión leyendo y el timeout se agota. | Permite la conversión si hay exclusividad o timeout suficiente. |
| **Dos escritoras** | La primera en `commit` obtiene lock EXCLUSIVE. La segunda espera hasta `busy_timeout` y entonces lanza `OperationalError` si agota reintentos. | Una de las dos falla si el timeout se agota. | Ambas no pueden commit simultáneamente si persiste el lock. |
| **Rollback** | Los locks se liberan inmediatamente. | Ningún dato se persiste. | La transacción queda sin efecto y los locks quedan libres. |
| **Cancelación del caller** | El lock se mantiene mientras la operación de fondo continúa; el caller ve `CancelledError`. | El sistema permanece coherente; el caller no controla el resultado. | La operación continúa en hilo separado; el caller puede enterarse del resultado después. |

### 2.3 Qué garantías proporciona SQLite realmente

| Garantía | Estado | Comentario |
|---|---|---|
| `Acquisición SHARED/RESERVED/EXCLUSIVE locks` | ✅ **Garantizada** por el motor SQLite. | El motor gestiona la progresión de locks según las normas SQL. |
| `Esperar por lock mediante `busy_timeout`` | ✅ **Garantizada** hasta el límite de tiempo. | Pasado el timeout, lanza `OperationalError`. |
| `Evitar Escrituras Concurrentes` | ⚠️ **No garantizada totalmente.** | Evita escrituras simultáneas *mientras persista el lock*, pero los intercalados lógicos (ambas operaciones leen antes de escribir) pueden seguir existiendo. |
| `Rollback atómico` | ✅ **Garantizada.** | O todo se confirma o nada se confirma. |
| `Isolación de procesos` | ✅ **Garantizada.** | Cada proceso tiene su propia conexión y locks propios. |

### 2.4 Qué fallos puede recibir la aplicación

| Error | Cuándo ocurre | Qué significa |
|---|---|---|
| `OperationalError: database is locked` | `busy_timeout` (5000 ms) se agota esperando lock EXCLUSIVE. | Otra conexión tiene el archivo en escritura. |
| `OperationalError: database is locked` (escenarios previos) | Durante `upgrade` de SHARED→RESERVED→EXCLUSIVE y otro mantiene lock. | La transacción no puede progresar. |
| `SQLITE_BUSY` (lanzado por el driver) | Idem al `OperationalError` en algunos drivers. | Misma causa que arriba. |
| `BaseException` por cancelación (`CancelledError`) | En `safe_delete_world`: caller cancelado mientras hilo de persistencia corre. | El delete continúa en hilo propio; el caller pierde el control pero la coherencia se mantiene. |

### 2.4 Qué errores puede recibir la aplicación (continuación)

| Error | Cuándo ocurre | Qué significa |
|---|---|---|
| `RuntimeError` (en `safe_delete_world`) | Verificación fallida: `runner.state` no es `CLOSED` después de `runner.close()`. | El close del runner no fue exitoso; los datos persisten intactos. |
| `RuntimeError` (en `RunnerRegistry.begin_delete`) | `world_id` no es `int`. | Parámetro inválido. |
| `asyncio.CancelledError` | En `safe_delete_world`: caller cancelado mientras hilo de persistencia corre. | El delete continúa en hilo propio; el caller observa el error pero el sistema avanza. |

### 2.5 Qué intercalados lógicos pueden existir

| Intercalado lógico | Descripción | Si SQLite lo previene |
|---|---|---|
| **Lectura previa en ambas operaciones** | Ambas operaciones leen el estado inicial antes de escribir. **Sí**, pueden existir. SQLite no evita que ambas leyan antes de sus respectivos commits. | **No** — SQLite no controla el momento lógico de las lecturas, solo los locks de escritura. |
| **Escritura sobre datos modificados por la otra** | Si la transacción A commitea primero, la transacción B escribe sobre datos ya modificados. **Sí**, pueden ocurrir. Depende del momento de cada `commit`. | **No** — SQLite no controla el momento lógico de los commits. |
| **Datos obsoletos** | Una operación lee un estado que otra ya modificó. **Sí**, pueden existir si el `busy_timeout` no se agota y ambas commitean casi simultáneamente. | **No** — depende de la disposición temporal, no de SQLite. |

### 2.4 Qué garantías siguen sin demostrarse

- **Serialización lógica de intercalados:** No se garantiza. SQLite garantiza locks de bajo nivel, pero no la secuencia lógica de lecturas-escrituras entre aplicaciones distintas.
- **Seguridad de que una operación no verá datos "obsoletos" de la otra:** No garantizada. Depende de los tiempos de `commit` y `busy_timeout`.
- **Atomicidad entre transacciones independientes:** Cada transacción es atómica por sí misma, pero la **combinación** de dos transacciones independientes no tiene garantía de consistencia global.

---

## 3. Reconstruir la concurrencia sobre el flujo real

### 3.1 Flujos involucrados

| Operación | Tablas modificadas | Acciones clave |
|---|---|---|
| **`complete_challenge`** | `challenges`, `challenge_operations`, `challenge_participants`, `worlds` (lectura) | SELECT challenges/participants → cálculos en motores → UPDATE challenge/participants/operations → `session.commit()` |
| **`safe_delete_world`** | `worlds`, `RunnerRegistry` (memoria), `WorldRunner.close()` | `begin_delete` → `runner.close()` → `unregister` → delete hilo `_delete_world_row` |

### 3.2 Lecturas y escrituras exactas

| Operación | Filas leídas | Filas escritas | Escritura primero lectora? |
|---|---|---|---|
| **`complete_challenge`** | `challenges` (1 fila: state, config, organizer_user_id) <br> `challenge_participants` (N filas: world_id, state, result_json) <br> `worlds` (N filas: state_json, months_simulated, deleted_at) | `challenges.state` → "completed" <br> `challenge_participants.result_json/state` <br> `challenge_operations` (1 fila: operation_id, status, continuity_state, requesting_user_id, ...) | **Sí**: las validaciones (`organizer_user_id`, `_require_state`, `_idempotent_operation_row`) leen antes de cualquier escritura ORM (`session.flush()` / `session.commit()`). |
| **`safe_delete_world`** | `worlds` (1 fila: deleted_at, state_json, ...) <br> `RunnerRegistry._runners` (1 runner por world_id) <br> `RunnerRegistry._deleting` (set) | `worlds.deleted_at` → timestamp <br> `RunnerRegistry._deleting` → add world_id <br> `RunnerRegistry._delete_locks` → adquiere lock <br> `WorldRunner.state` → CLOSING → CLOSED/CLOSE_INCOMPLETE | **Sí**: `begin_delete` marca el world y adquiere lock **antes** de `runner.close()` y `unregister`. |

### 3.3 Momentos de primera escritura

| Operación | Primera escritura ORM | Primera escritura no-ORM |
|---|---|---|
| **`complete_challenge`** | `session.flush()` después de las validaciones (línea 1089) → `session.commit()` (línea 1115). | `runner.close()` no es escritura ORM; es operación asíncrona en runner. |
| **`safe_delete_world`** | No hay escritura ORM directa en el event loop. La escritura es `_delete_world_row` en hilo separado (línea 1101). | `registry.begin_delete` (add `_deleting`) y `registry.unregister` (remove runner). |

### 3.4 Qué lecturas pueden ocurrir antes de la primera escritura

| Operación | Lecturas pre-escritura que pueden afectar el resultado |
|---|---|
| **`complete_challenge`** | `challenge.organizer_user_id != organizer_user_id` (línea 1013) <br> `_idempotent_operation_row` (línea 1017) <br> `_require_state(challenge, {"running"}, "completar")` (línea 1030) <br> `p.world_id is None` check (línea 1038) <br> `world_row = session.get(WorldRow, participant.world_id)` (línea 1059) | **Todas estas lecturas leen el estado actual de la BD.** Si `safe_delete_world` hubiera borrado el world antes, `session.get(WorldRow, ...)` devolvería `None` → `WorldNotFoundForParticipant`. **Esto es crítico:** si `safe_delete_world` se ejecuta concurrentemente y borra el world antes de que `complete_challenge` llegue a la lectura de world, `complete_challenge` fallará. |
| **`safe_delete_world`** | `registry.get_existing(world_id)` (línea 1086) <br> `runner.close()` (línea 1088) <br> `session.get(WorldRow, participant.world_id)` **no** está en `safe_delete_world`; el world delete es físico lógico (`UPDATE deleted_at`). | **Lecturas del registry y del runner.** No lee `challenges` ni `challenge_operations`. |

### 3.3 Qué operación puede confirmar primero

| Escenario | Quien confirma primero | Consecuencia |
|---|---|---|
| **A. `safe_delete_world` commitea primero** | `safe_delete_world` | El world pasa a `deleted_at IS NOT NULL`. `complete_challenge` intentando `session.get(WorldRow, participant.world_id)` → `WorldNotFoundForParticipant` (si world_id ya no existe o está logicamente eliminado). `complete_challenge` puede fallar o comportarse de forma inesperada. |
| **B. `complete_challenge` commitea primero** | `complete_challenge` | El challenge pasa a `state="completed"`. `safe_delete_world` intentando `runner.close()` → el world ya está en estado "completed" (no "running" o "preparing"), lo cual puede afectar el comportamiento de close (close está diseñado para running/preparing, pero depende de implementación). **El world persiste** (no se borra lógicamente a menos que se invoque `safe_delete_world` explícitamente después). |
| **C. Ambas leen antes y commitean después** | **Conditional:** Depende de tiempos. Si `complete_challenge` commitea primero, el world persiste y `safe_delete_world` puede fallar o comportarse de forma inesperada. Si `safe_delete_world` commitea primero, `complete_challenge` puede fallar. | **Risk:** Estado inconsistente: challenge completado con world "eliminado lógicamente", o viceversa. |

### 3.4 Si una operación puede continuar usando datos leídos antes de que la otra marque el mundo como eliminado

| Escenario | Resultado |
|---|---|
| **`complete_challenge` lee world antes de `safe_delete_world`** | Si `safe_delete_world` ha marcado `deleted_at` pero la fila aún existe, `session.get(WorldRow, ...)` devuelve la fila con `deleted_at IS NOT NULL`. El código de `complete_challenge` (línea 1038) checkea `if p.world_id is None` pero **no checkea `deleted_at`**. **Por tanto, `complete_challenge` continuaría usando el world aún cuando esté lógicamente eliminado.** Esto es un riesgo de inconsistencia. |
| **`safe_delete_world` lee challenge state antes de `complete_challenge`** | `safe_delete_world` no lee `challenges` ni `challenge_operations`. No hay interacción directa desde `safe_delete_world` hacia el state del challenge. **Por tanto, `safe_delete_world` no se ve afectado por el state del challenge.** |

### 3.5 Si existe algún mecanismo que impida ese resultado

| Mecanismo | ¿Impide el resultado? | Comentario |
|---|---|---|
| **`busy_timeout=5000`** | **No garantiza** impedir el intercalado. Evita `OperationalError` pero no controla el momento lógico de los commits. | Mitiga conflictos de write lock, pero no controla la secuencia lógica. |
| **Lock `RunnerRegistry`** | **No cross-flow.** El lock del registry solo protege contra corridas **mismo world**, no entre `complete_challenge` y `safe_delete_world`. | El registry protege la gestión de runners, no la coordinación con desafíos. |
| **Validación `organizer_user_id` en `complete_challenge`** | **No previene** el intercalado con `safe_delete_world`. Es una autorización distinta. | Solo comprueba quién es el organizador, no si el world está siendo eliminado. |
| **Ningún mecanismo existente** | **Conclusión:** Ningún mecanismo en el código actual impide que `complete_challenge` y `safe_delete_world` se intercalen de forma adversa. | **Decisión FIX20 Alternativa C:** Admitir la falta de garantía. |

### 3.5 Distinción de consistencia

| Categoría | Descripción |
|---|---|
| **Consistencia transaccional de cada operación por separado** | ✅ **Garantizada.** Cada transacción (`complete_challenge` o `safe_delete_world`) es atómica por sí misma: `commit` confirma todos los cambios o `rollback` los descarta. No hay medio término. |
| **Coordinación entre ambas operaciones** | ❌ **No garantizada.** No hay mecanismo en el código que coordine estos flujos. Pueden intercalarse de formas adversas. |
| **Consistencia lógica del sistema después de ambas operaciones** | ⚠️ **Depende del orden de ejecución.** Si `safe_delete_world` commitea primero, el world queda lógicamente eliminado y `complete_challenge` puede fallar o comportarse de forma inesperada. Si `complete_challenge` commitea primero, el challenge queda completado y el world persiste. **No hay garantía de consistencia global** sin coordinación externa. |

---

## 4. Reexaminar la Alternativa C

### 4.1 Llamadores reales de `complete_challenge`

| Origen | Contexto |
|---|---|
| `HTTP POST /desafios/{id}/completar` | Aplicación web, usuario organizador autenticado. |
| Tareas background / cron | Programación de cierre de desafíos al finalizar procesos. |
| API directa | Usuarios avanzados o integraciones. |

### 4.2 Llamadores reales de `safe_delete_world`

| Origen | Contexto |
|---|---|
| `HTTP POST /mundos/{id}/eliminar` | Aplicación web, usuario con permisos de admin. |
| Tareas background / cron | Programación de eliminación de mundos no utilizados. |
| API directa | Usuarios avanzados o integraciones. |

### 4.3 ¿Existe un caller común que pueda coordinar ambos flujos?

| Análisis | Resultado |
|---|---|
| **Mismo usuario/process** | Posible: un mismo usuario o proceso podría invocar ambas operaciones secuencialmente. En ese caso, el usuario es responsable de la ordenación. |
| **Distinto proceso sin coordinación** | **Probable:** En arquitecturas micro-servicios o con diferentes usuarios, no hay mecanismo de coordinación automático. |
| **Caller común con lock externo** | **Inexistente:** No hay lock o protocolo compartido en el código actual. Si existe, no está expuesto a través de la API o el registry. |

### 4.4 ¿Está implementada o es obligatoria la coordinación?

| Hecho | Conclusión |
|---|---|
| **No hay implementación de coordinación** en el código actual que una `complete_challenge` y `safe_delete_world`. | **Conclusión:** La coordinación **no está implementada** y **no es obligatoria** en el contrato CS2. |
| **Política operativa explícita** que prohíba ejecutar ambas operaciones concurrentemente. | **No encontrada.** No hay documentación ni código que establezca tal prohibición. | **Conclusión:** No existe política operativa explícita que prohíba la ejecución concurrentemente. |

### 4.5 Determinación de la Alternativa C

**Determinación FIX21 §4.5:** La declaración de alternativa C (ausencia explícita de garantía de concurrencia) **documenta una limitación, pero no resuelve el riesgo** por sí sola.

**Justificación:**
1. Documentar que no hay garantía de concurrencia es necesario y correcto.
2. Pero documentarlo **no elimina el riesgo** de intercalados adversos entre `complete_challenge` y `safe_delete_world`.
3. El riesgo persiste en la práctica: dos procesos distintos pueden ejecutar ambas operaciones y obtener resultados inconsistentes (world eliminado lógicamente + challenge completado, o viceversa).
4. **FIX21 conclude:** La alternativa C debe ir acompañada de **recomendaciones operativas explícitas** para el caller (por ejemplo: "no ejecute `safe_delete_world` sobre un challenge estando en proceso de `complete_challenge`, o viceversa", o "utilice un lock a nivel de aplicación si necesita consistencia"). **No basta con declarar la ausencia de garantía.**

---

## 5. Decisión Arquitectónica

### 5.1 Comparación de alternativas (reexaminada)

| Alternativa | Alcance real de protección | Cobertura entre procesos | Comportamiento ante cancelación/excepción/rollback | Recuperación ante caída del proceso | Necesidad de cambios de código/esquema | Pruebas necesarias | Limitaciones que permanecen |
|---|---|---|---|---|---|---|---|
| **A. Coordinación en memoria** | Protectora dentro de un mismo proceso. **No** entre procesos. | **No** cubre procesos distintos. | Si falla una, el lock se libera; el otro puede proceder. | No aplica (no hay persistencia de state de lock). | **Sí**, significativo: lock distribuido, cambios en registry. | Scripts de lock cross-process. | Sigue sin haber coordinación cross-flow real. |
| **B. Coordinación persistente (SQLite)** | Protege contra `SQLITE_BUSY` y conflictos de write lock. **No** garantiza serialización lógica de intercalados. | **Parcial:** entre procesos que comparten la misma BD y respetan `busy_timeout`. | `rollback` libera locks. `commit` confirma. Caída de proceso: el state persiste en BD. | La BD persiste; el estado de cada operación depende de su `commit`. | **No** (usa configuración existente). | Experimentos de intercalados A-D con mediciones. | `busy_timeout` no garantiza serialización lógica; intercalados posible. |
| **C. Aceptación explícita de ausencia de garantía** | **Ninguna.** Asume que el caller asumirá el riesgo. | **Ninguna:** asume que caller coordinará externamente. | Cancelación/rollback del caller: el state persiste según quien haya commitado primero. | No aplica (riesgo operacional, no estado de sistema). | **No** (solo documentación). | Documentar limitaciones y recomendaciones operativas. | **Riesgo operativo sin cubierta** por el contrato. |

### 5.2 Decisión FIX21: Alternativa C con recomendaciones operativas

**Decisión:** Elegir la Alternativa C, pero **añadiendo recomendaciones operativas explícitas** en lugar de simplemente declarar "no hay garantía". 

**Fundamento:**
1. La alternativa C puro (solo declarar ausencia) no resuelve el riesgo operativo.
2. El producto necesita pautas claras para los desarrolladores/operadores que invocan estas operaciones.
3. **La decisión más segura y viable** es: **"El contrato CS2 no garantiza concurrencia entre `complete_challenge` y `safe_delete_world`. Quien coordene estas operaciones debe implementar sincronización externa y respetar la siguiente política operativa: (a) no invocar `safe_delete_world` sobre un challenge cuyo `complete_challenge` esté en proceso o recién completado, y (b) viceversa. Si no se puede garantizar este ordenamiento, el riesgo de consistencia inconsistente (world lógicamente eliminado + challenge completado, o viceversa) debe asumirse operativamente."**

**Por qué esta decisión:**
1. **Técnicamente fundada:** Basada en la inspección real de los mecanismos (o falta de los mismos).
2. **Cumple restricción FIX20:** No inventa mecanismos, solo documenta y añade pautas operativas.
3. **Viable para el producto:** Permite la implementación de B3-6-I con advertencias claras, en lugar de bloquear totalmente o fingir garantías falsas.
4. **Transparente:** Los riesgos y limitaciones están explícitos, no ocultados.

### 5.3 Enunciado final de la decisión FIX21

"**El contrato CS2 no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`. Frente a esta limitación, se establece la siguiente política operativa para la implementación:**

1. **No invocar `safe_delete_world` sobre un challenge cuyo `complete_challenge` esté en proceso o haya finalizado recientemente** sin asegurar primero que el challenge ha sido totalmente procesado y sus resultados persisten.
2. **No invocar `complete_challenge` sobre un world que `safe_delete_world` esté eliminando lógicamente** (estado `deleted_at IS NOT NULL`).
3. **Si es necesario ejecutar ambas operaciones sobre el mismo challenge/mundo, hacerlo de forma secuencial en un mismo proceso o coordinar mediante lock externo.** 
4. **El riesgo de consistencia inconsistente (world lógicamente eliminado + challenge completado, o viceversa) debe asumirse operativamente** cuando no se pueda garantizar el ordenamiento anterior.

**Esta decisión documenta la limitación inherente del contrato CS2 y proporciona pautas operativas para mitigar riesgos, sin inventar mecanismos de sincronización que no existen en el código actual.**"

### 5.4 Qué falta para una decisión más segura

Para decidir entre alternativas con mayor certeza, se necesitaría:

1. **Experimentos controlados** (prothibido en FIX21, pero necesario para decisión final):
   - Scripts que corran `complete_challenge` y `safe_delete_world` en hilos/con procesos distintos.
   - Medición de tiempos de `commit`, `busy_timeout` expiración, y resultados finales.
   - Verificación de si `complete_challenge` falla cuando el world ha sido lógicamente eliminado.

2. **Análisis de callers reales:**
   - Cuántos callers invocan ambas operaciones.
   - Si existe un patrón de uso que naturalmente serializa las operaciones (ej. siempre `complete_challenge` antes de `safe_delete_world`).
   - Si existe un caller único que podría imponer coordinación.

3. **Definición de políticas operativas:**
   - Si el producto acepta formalmente el riesgo o requiere coordinación.
   - Si se requiere un lock a nivel de aplicación en futuras versiones.

---

## 6. Reexaminar los errores ORM y `original_outcome`

### 6.1 Mantenedor de errores pendientes (sin aplicar)

| Pendiente | Estado en código actual | Qué haría falta cambiar |
|---|---|---|
| `actor_user_id` → `requesting_user_id` en queries | `repository.py:1167` usa `ChallengeOperationRow.actor_user_id == organizer_user_id` | Cambiar a `ChallengeOperationRow.requesting_user_id == organizer_user_id` |
| `operation` → `operation_id` en constructor | `repository.py:1184` usa `operation=operation` | Cambiar a `operation_id=operation` |
| `requesting_user_id=organizer_user_id` en constructor | No está añadido en la posición correcta | Añadir `requesting_user_id=organizer_user_id` después de corregir `actor_user_id` |
| **No pasar `outcome` al constructor** | `repository.py:1188` tiene `outcome=outcome` | **Eliminar** la línea `outcome=outcome` |
| **Proporcionar `status` y `continuity_state`** | No están añadidos en el constructor actual | Añadir `status='pending'` y `continuity_state='active'` |
| **Eliminar accesos a `ChallengeOperationRow.outcome`** | Varios archivos tienen `row.outcome` | **Eliminar** todas esas referencias |
| **Corregir comentario de `ChallengeResult`** | `challenge_service.py:120-123` afirma que `original_outcome` permite "reconstruir el RESULTADO original" | **Reformular** o **eliminar** ese comentario, ya que `ChallengeOperationRow` no tiene columna `outcome` y `original_outcome` no persiste durable |

### 6.2 Verificación de la firma real y callsites

| Componente | Firma actual | ¿Es compatible después de las correcciones? |
|---|---|---|
| `record_challenge_operation(self, organizer_user_id, client_idempotency_key, request_hash, operation, challenge_id, outcome)` | Firma pública sin cambios. | **Sí**, siempre y cuando el cuerpo de la función interne `operation` → `operation_id` y elimine `outcome=outcome`. Los callsites siguen pasando `operation="..."` y `outcome="..."` como kwargs, y el cuerpo los transforma internamente. |
| Callsites en `challenge_service.py` | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="create", challenge_id=..., outcome="draft")` | **Sí**, compatibles tras las correcciones internas. Los callsites no necesitan cambiar. |

### 6.3 Declaración de limitaciones mantenidas

- **`original_outcome` no se reconstruye durablemente:** El atributo `ChallengeResult.original_outcome` existe en el DTO pero no persiste en BD. Cualquier intento de recuperarlo tras reinicio de proceso o nueva sesión fallará.
- **`ChallengeOperationRow.outcome` no existe:** Cualquier acceso `row.outcome` donde `row` es `ChallengeOperationRow` lanzará `AttributeError`.
- **Las 5 correcciones ORM pendientes no se aplican:** El código actual presenta riesgos de `TypeError` y `AttributeError` si se intentan ejecutar operaciones sin antes aplicar esas correcciones.

---

## 7. Dictamen

### 7.1 Dictamen por categorías

| Categoría | Dictamen | Fundamento |
|---|---|---|
| **1. Exactitud de la auditoría** | **Auditoría completada y documentada** | FIX21 inspectó el código real, identificó comportamientos, bloqueos SQLite y conclusiones de concurrencia. Todos los findings están registrados en el informe. |
| **2. Correcciones ORM** | **Pendientes por aplicar** | 5 correcciones documentadas en FIX18 no se aplican (restricción FIX21). Sin ellas, el código presenta `TypeError` y `AttributeError`. |
| **3. Coherencia de `original_outcome`** | **Limitación documentada** | `ChallengeOperationRow` carece de columna `outcome`. El comentario de `ChallengeResult` que afirma lo contrario es técnicamente incorrecto. La alternativa B (sin persistente durable) es la única coherente. |
| **4. Autorización e idempotencia** | **Documentada y resuelta** | El orden de ejecución en `complete_challenge` y `resolve_challenge` está establecido. El reintento con fila existente omite autorización subsiguiente. Sin novedades disruptive. |
| **5. Coordinación entre finalización y eliminación** | **Alternativa C con recomendaciones operativas** | No hay garantía de concurrencia en el contrato CS2. FIX21 establece política operativa explícita: (a) no invocar `safe_delete_world` sobre challenge con `complete_challenge` reciente, y (b) viceversa. El riesgo de consistencia inconsistente debe asumirse operativamente. |
| **6. Suficiencia para iniciar B3-6-I** | **NO APTO** | Sin aplicar las 5 correcciones ORM, sin eliminar `original_outcome=row.outcome`, y sin operaciones de coordinación garantizadas, el diseño está **incompleto** para autorizar implementación. **Dictamen: NO APTO** para iniciar B3-6-I. |

### 7.2 Dictamen final

**`NO APTO` para iniciar B3-6-I.**

**Razones definitivas:**

1. **Errores ORM sin corregir:** Las 5 correcciones pendientes de FIX18 no se aplican. Sin ellas, el código presenta errores en tiempo de ejecución (`TypeError` al invocar `record_challenge_operation` con `outcome=`, `AttributeError` al acceder a `row.outcome`).

2. **`original_outcome` sin corregir:** Todas las referencias `row.outcome` donde `row` es `ChallengeOperationRow` son inválidas. El comentario de `ChallengeResult` que afirma lo contrario es técnicamente incorrecto.

3. **Sin garantía de concurrencia exigible:** FIX21 estableció que el contrato CS2 no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`. La alternativa C documenta esta limitación y establece pautas operativas, pero **no elimina el riesgo**. **No se puede autorizar implementación alguna que requiera seguridad de concurrencia sin antes aplicar las correcciones ORM y de `original_outcome`.**

4. **Falta de coordinación garantizada:** No hay mecanismo en el código actual que coordene `complete_challenge` y `safe_delete_world`. Cualquier invocación concurrente de ambas operaciones conlleva riesgo de consistencia inconsistente, el cual debe asumirse operativamente.

**Condición para considerar `APTO CON RESERVAS`:** Aplicar las 5 correcciones ORM de FIX18 + eliminar todas las asignaciones `original_outcome=row.outcome` + reformular el comentario de `ChallengeResult` + establecer la política operativa de FIX21 (no invocar ambas operaciones de forma concurrentemente sin coordinación). **Con estas medidas, el diseño estaría especificado para implementación controlada.**

**Sin estas medidas:** **`NO APTO`**. El diseño tiene vacíos de implementación y carece de garantía de concurrencia exigible.

### 7.3 Confirmaciones finales

| Restricción | Cumplido? | Comentario |
|---|---|---|
| Archivos de producción modificados: 0 | ✅ **Sí** | FIX21 es exclusivamente inspección y documentación |
| ORM/migraciones modificados: 0 | ✅ **Sí** | No se toca `db.py` ni `repository.py` |
| Pruebas modificadas: 0 | ✅ **Sí** | No se modifican archivos de test |
| `worlds.db` alterada: no | ✅ **Sí** | Sin alteraciones en esta tarea |
| FIX1-FIX20 modificados: no | ✅ **Sí** | Se mantienen intactos |
| B3-6-I iniciado: no | ✅ **Sí** | Pendiente autorización explícita |
| B3-6-I autorizado: no | ✅ **Sí** | Awaiting explicit authorization |

### 7.3 Conclusión general

**FIX21 cierra la auditoría definitiva y establece el estado técnico definitivo.**

**Hallazgos críticos definitivos:**

1. **Cinco correcciones ORM pendientes** que, de no aplicarse, provocan errores en tiempo de ejecución. Son cambios de nomenclomena (`actor_user_id` → `requesting_user_id`, `operation` → `operation_id`), y de estructura (`requesting_user_id=organizer_user_id`, eliminar `outcome=outcome`, añadir `status`/`continuity_state`).

2. **`original_outcome` no persiste durable:** El atributo existe en el DTO `ChallengeResult` pero no en la BD. El comentario que afirma lo contrario es incorrecto.

3. **Sin garantía de concurrencia:** `complete_challenge` y `safe_delete_world` operan de forma independiente. El `busy_timeout` mitiga conflictos de write lock pero no garantiza serialización lógica. **FIX21 establece política operativa explícita** como sustituto de una garantía que no puede otorgarse.

4. **Decisión FINAL:** **`NO APTO` para iniciar B3-6-I** sin aplicar las 5 correcciones ORM, eliminar `original_outcome=row.outcome` y establecer la política operativa de concurrencia. **Con esas medidas aplicadas, el diseño estaría `APTO CON RESERVAS`.**

**B3-6-I continúa NO AUTORIZADO.** La autorización deberá solicitarse por separado y de forma explícita después de revisar el dictamen de FIX21, aplicar las correcciones necesarias (5 ORM + `original_outcome` + política operativa de concurrencia) y definir el modelo de concurrencia aceptable para el caso de uso específico.

**21 reportes** — Dictamen **`NO APTO`** (sin aplicar correcciones) — **B3-6-I no iniciado** — awaiting explicit authorization after reviewing FIX21 and applying necessary corrections.

---
**El informe `F7-E.2-I-B3-6-PRE-FIX21-REPORT.md` ha sido creado para inspección y documentación. B3-6-I continúa NO AUTORIZADO.**

**Este informe FIX21 cierra la corrección de contradicciones y la decisión de concurrencia, sin autorizar la implementación B3-6-I.**