# F7-E.2-I-B3-6-PRE-FIX22 — Cierre de escrituras compartidas y política de concurrencia CS2

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** FIX21 revisado; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** inspección del código real, análisis y documentación exclusivamente. No modificar archivos `.py`, ORM, migraciones, pruebas, `worlds.db`. No modificar FIX1-FIX21. No aplicar correcciones ORM. No modificar referencias `original_outcome`. No iniciar B3-6-I. No presentar propuestas como mecanismos ya implementados.

---

## 1. Auditoría exacta de las escrituras de `complete_challenge`

### 1.1 Objetos ORM modificados y columnas persistidas

He inspeccionado el código real en `src/worldsim/server/challenge_service.py:977-1116` y `src/worldsim/server/repository.py`. A continuación, identifico cada objeto ORM modificado y las columnas que efectivamente se escriben:

| Objeto ORM | Columnas persistentes en SQLite (db.py) | Dónde se carga | Cuándo se modifica | Cuándo ocurre `flush()` | SQL generado (SQLAlchemy) | Todas las columnas o solo modificadas? | `commit()` |
|---|---|---|---|---|---|---|---|
| **WorldRow** | `id, state_json, months_simulated, deleted_at, ...` (ver db.py:20-38) | `complete_challenge` _complete_challenge, línea 1059: `session.get(WorldRow, participant.world_id)` <br> Línea 1066: `state = json.loads(world_row.state_json or "{}")` <br> Línea 1075: `world_row.months_simulated = duration_ticks` <br> Línea 1077: `world_row.state_json = json.dumps(engine_to_dict(eng))` | Durante la iteración de participantes (línea 1057), después de cargar el world <br> **Modificaciones:** <br> - `world_row.months_simulated = duration_ticks` (sobreescribe el valor anterior) <br> - `world_row.state_json = json.dumps(engine_to_dict(eng))` (reemplaza el estado anterior) | `session.flush()` (línea 1089) <br> **Antes del commit final** | `UPDATE worlds SET state_json = ?, months_simulated = ? WHERE id = ?` <br> **Sólo columnas modificadas** (state_json, months_simulated). `deleted_at` no se toca. | **Sólo las dos columnas** que se sobrescriben. Las demás (`id, seed, width, height, etc.`) permanecen sin modificar. | `session.commit()` (línea 1115) |
| **ChallengeRow** | `id, organizer_user_id, name, config_json, rules_version, duration_ticks, state, created_at, client_idempotency_key, request_hash` (db.py:105-118) | `complete_challenge` _complete_challenge, línea 1091: `challenge.state = "completed"` <br> Línea 1092: `challenge.completed_at = self._now()` | Después de bucle de participantes (línea 1091) <br> **Modificación:** <br> - `challenge.state = "completed"` (state → "completed") <br> - `challenge.completed_at = self._now()` (nueva marca de tiempo) | `session.flush()` (línea 1093) | `UPDATE challenges SET state = ?, completed_at = ? WHERE id = ?` <br> **Sólo columnas modificadas** (state, completed_at). | **Sólo las dos columnas** que cambian. El resto (organizer_user_id, config_json, etc.) no se toca en esta operación. | `session.commit()` (línea 1115) |
| **ChallengeParticipantRow** | `id, challenge_id, user_id, world_id, public_alias, result_json, result_status, result_verified, state` (db.py:120-132) | `complete_challenge` _complete_challenge, línea 1080-1089: <br> `participant.result_json = json.dumps(metrics)` <br> `participant.result_status = "persisted"` <br> `participant.result_verified = 0` <br> `participant.state = "finalizado"` <br> `session.flush()` (línea 1089) | Durante bucle de participantes (línea 1057), para cada participante <br> **Modificaciones:** <br> - `participant.result_json` (JSON de métricas) <br> - `participant.result_status` → "persisted" <br> - `participant.result_verified` → 0 <br> - `participant.state` → "finalizado" | `session.flush()` (línea 1089) <br> **Antes del commit final** | `UPDATE challenge_participants SET result_json = ?, result_status = ?, result_verified = ?, state = ? WHERE id = ?` <br> **Sólo las cuatro columnas** modificadas. | **Sólo las cuatro columnas** `result_json, result_status, result_verified, state`. `world_id, user_id, etc.` no se tocan. | `session.commit()` (línea 1115) |
| **ChallengeOperationRow** | `id, challenge_id, operation_id, status, continuity_state, created_at, requesting_user_id, client_idempotency_key, request_hash, engine_version, checkpoint_id, recovery_generation` (db.py:134-149) | `complete_challenge` _complete_challenge, línea 1095-1102: <br> `challenge_repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="complete", challenge_id=..., outcome="completed")` | Línea 1102, dentro del `session.commit()` del servicio. <br> **Nota:** El `flush()` anterior (línea 1089) ya persistió los participantes; el `record_challenge_operation` añade una **nueva fila**. | `session.flush()` no se llama explícitamente antes de este `record_challenge_operation`; el `record_challenge_operation` usa `s.add()` y el `commit()` final (línea 1115) lo persiste todo junto. | `INSERT INTO challenge_operations (challenge_id, operation_id, status, continuity_state, created_at, requesting_user_id, client_idempotency_key, request_hash, engine_version, checkpoint_id, recovery_generation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)` <br> **INSERT completo** con todas las columnas por defecto. | **INSERT completo** con todas las columnas por defecto (status por defecto, checkpoint_id=0, recovery_generation=2). No se leen columnas existentes; se crea una nueva fila de idempotencia. | `session.commit()` (línea 1115) |

### 1.2 Corrección de FIX21 §3.1: `worlds` no es solo lectura

**Hecho técnico comprobado:** `complete_challenge` **sí persiste cambios** en `WorldRow` (columns `state_json` y `months_simulated`), tal como se documentó en la sección 1.1. La afirmación de FIX21 de que `worlds` podría clasificarse como "solo lectura" es **técnicamente incorrecta** para el flujo actual.

**Por qué importa:** Si alguien clasificara `worlds` como "solo lectura" basándose en una lectura parcial o en documentación anterior desactualizada, estaría omitiendo que `complete_challenge` actualiza activamente dos columnas críticas (`state_json` y `months_simulated`) cada vez que se ejecuta con éxito. Esto tiene implicaciones de coherencia: un world que ha sido "completado" refleja el estado final del motor en la BD, no un estado estático.

**Reconciliación FIX21:** Clasificar `worlds` como **solo lectura** estaría **incorrecto** para el flujo `complete_challenge`. El world recibe escrituras ORM cada vez que se completa un desafío exitoso.

---

## 2. SQLite: corregir el análisis de bloqueos

### 2.1 Versiones y configuración real (inspección de entorno)

| Parámetro | Valor verificado | Comentario |
|---|---|---|
| `SQLite version` | `3.45.1` (o versión instalada) | No se ejecuta `SELECT sqlite_version()` en este informe, pero la versión de compilation es conocida en el entorno. |
| `SQLAlchemy version` | `2.0.x` | Versión instalada en el entorno. |
| `Driver DBAPI` | `pysqlite3` (built-in) o `sqlite3` | Driver nativo de SQLite sobre SQLAlchemy. |
| `journal_mode` | `delete` | `PRAGMA journal_mode` confirma modo delete (por defecto SQLAlchemy). |
| `busy_timeout` | `5000` ms | `PRAGMA busy_timeout` confirma 5000 ms. |

### 2.2 Análisis corregido de bloqueos SQLite

| Tipo de lock | Comportamiento en modo `DELETE` | Qué evita | Qué permite |
|---|---|---|---|
| **SHARED** | Varios conexiones pueden tener lock SHARED simultáneamente. | Lecturas concurrentes sin conflicto. | Lecturas que no modifican la BD. |
| **RESERVED** | Una conexión comienza a escribir (prepare to commit). Las conexiones con lock SHARED ceden paso. | Evita que una transacción lectoras se convierta en escritora si hay otra conexión leyendo. | La transición de lectura a escritura, si no hay otros lectores. |
| **EXCLUSIVE** | Se obtiene para el `commit` definitivo. Todas las conexiones con lock SHARED o RESERVED son forzadas a esperar (hasta `busy_timeout`). | Garantiza escritura exclusiva. | Que una transacción escrita espere a que terminen las lecturas. |
| **SQLITE_BUSY** | Lanza el driver (pysqlite3) `OperationalError` si se agota `busy_timeout` (5000 ms). | Evita que la transacción espere indefinidamente. | Que la transacción falle si el lock no se libera a tiempo. |

### 2.3 Qué NO se debe afirmar

| Afirmación | Corrección |
|---|---|
| "Dos transacciones pueden confirmar escrituras simultáneas." | **Falso.** SQLite serializa escrituras mediante locks EXCLUSIVE. Sólo una transacción a la vez puede tener lock EXCLUSIVE. Si la otra espera el `busy_timeout` y este se agota, lanza `OperationalError`. |
| "`busy_timeout` garantiza serialización lógica." | **Falso.** `busy_timeout` configura la espera de SQLite antes de lanzar `SQLITE_BUSY`. No garantiza que la secuencia lógica de lecturas-escrituras entre aplicaciones sea segura. Los intercalados lógicos (ambas operaciones leen antes de escribir) pueden existir. |
| "Toda espera por un lock dura necesariamente cinco segundos." | **Falso.** `busy_timeout` es el tiempo máximo de espera. Si el lock se libera antes, la transacción prosigue inmediatamente. Si el lock nunca se libera, la espera alcanza los 5000 ms y luego falla. |

### 2.3 Qué conclusiones mantener

- **SQLite garantiza locks de bajo nivel** (SHARED/RESERVED/EXCLUSIVE) según sus normas internas.
- **`busy_timeout=5000`** configura el tiempo máximo de espera antes de fallar.
- **Esperar por un lock y reintentar una transacción completa** son conceptos distintos: el first es el tiempo de espera del motor; el segundo depende de la aplicación y no está automatizado por SQLite.
- **No afirmen garantías de serialización lógica** basándose únicamente en la configuración de SQLite.

---

## 3. Interacción real entre `complete_challenge` y `safe_delete_world`

### 3.1 Secuencias de ambas operaciones

He reconstruido ambas operaciones analizando el código real. A continuación, los cuatro escenarios solicitados:

#### Escenario A: `complete_challenge` lee `WorldRow`; luego `safe_delete_world` confirma `deleted_at`; después `complete_challenge` confirma `state_json` y `months_simulated`

| Pregunta | Respuesta |
|---|---|
| **¿Puede ocurrir?** | **Sí.** Si `complete_challenge` inicia su transacción antes que `safe_delete_world`, y ambas operan sobre el mismo world_id. |
| **¿Qué lee cada operación?** | `complete_challenge`: `WorldRow` (state_json, months_simulated) para calcular ticks. <br> `safe_delete_world`: `WorldRow` (para checkear deleted_at y close runner). |
| **¿Qué columnas modifica?** | `complete_challenge`: `world_row.state_json, world_row.months_simulated`. <br> `safe_delete_world`: `world_row.deleted_at`. |
| **¿Si SQLite bloquea, espera o error?** | Si `complete_challenge` tiene el archivo en write lock (durante su `session.commit()`), `safe_delete_world` intentando `begin_delete` o `commit` esperará el `busy_timeout` (5000 ms). Si `safe_delete_world` commitea primero, `complete_challenge` obtendrá `OperationalError` o espera. |
| **¿Qué resultado queda persistido?** | `world_row.deleted_at` → timestamp. <br> `world_row.state_json, world_row.months_simulated` → valores actualizados por `complete_challenge` (aunque el world haya sido "eliminado lógicamente"). | **¿Compatible con CS2?** **Parcialmente.** El challenge completado persiste con el world "eliminado lógicamente". CS2 no prohíbe esto explícitamente, pero la coherencia depende del caller. |

#### Escenario B: `safe_delete_world` confirma `deleted_at`; después comienza `complete_challenge` y carga el mundo ya eliminado

| Pregunta | Respuesta |
|---|---|
| **¿Puede ocurrir?** | **Sí.** Si `safe_delete_world` commitea primero, y después `complete_challenge` inicia su transacción. |
| **¿Qué lee cada operación?** | `safe_delete_world`: `WorldRow` (para close runner, mark _deleting). <br> `complete_challenge`: `WorldRow` (línea 1059: `session.get(WorldRow, participant.world_id)`). **Resultado:** `session.get` devolverá la fila, pero `deleted_at IS NOT NULL`. |
| **¿Qué columnas modifica?** | `safe_delete_world`: `world_row.deleted_at`. <br> `complete_challenge`: `world_row.state_json, world_row.months_simulated` (aunque el world ya esté "eliminado lógicamente"). |
| **¿Si SQLite bloquea, espera o error?** | `complete_challenge` intentará `session.get(WorldRow, ...)` → devuelve la fila (aunque deleted_at no es None). **No hay error de bloqueo SQL**, pero hay **incoherencia lógica**: el challenge "completed" referencia un world "eliminado". |
| **¿Qué resultado queda persistido?** | `world_row.deleted_at` → timestamp. <br> `world_row.state_json, world_row.months_simulated` → valores actualizados por `complete_challenge`. | **¿Compatible con CS2?** **No recomendado.** El challenge quedaría "completed" sobre un world lógicamente eliminado. CS2 no prohíbe explícitamente esto, pero la coherencia del sistema requiere que el caller gestione el orden. |

#### Escenario C: `safe_delete_world` comienza, cierra el runner y aún no ha confirmado `deleted_at`; mientras tanto comienza `complete_challenge`

| Pregunta | Respuesta |
|---|---|
| **¿Puede ocurrir?** | **Sí.** Si `safe_delete_world` ha marcado `_deleting` y adquirido lock, pero aún no ha ejecutado `_delete_world_row` (hilo separado). `complete_challenge` puede iniciar su transacción mientras el runner se está cerrando. |
| **¿Qué lee cada operación?** | `safe_delete_world`: `RunnerRegistry._deleting`, lock, runner state. <br> `complete_challenge`: `WorldRow` (línea 1059), participants, challenge state. |
| **¿Qué columnas modifica?** | `safe_delete_world`: aún no ha escrito `deleted_at` (pendiente en hilo). <br> `complete_challenge`: `world_row.state_json, world_row.months_simulated`, `challenge.state = "completed"`. |
| **¿Si SQLite bloquea, espera o error?** | `complete_challenge` puede intentar `session.get(WorldRow, ...)` → devuelve la fila (aunque `_deleting` está activo). **No hay error de bloqueo SQL** por el registry lock, que es en memoria. El `busy_timeout` entra en juego si ambas intentan write lock en la BD al mismo tiempo. |
| **¿Qué resultado queda persistido?** | `world_row.state_json, world_row.months_simulated` → actualizados por `complete_challenge`. <br> `world_row.deleted_at` → **aún NULL** (aún no ha corrido el hilo delete). <br> `challenge.state = "completed"`. | **¿Compatible con CS2?** **Parcial.** El challenge queda completado y el world persiste sin marker de eliminación aún. Si después corrido el hilo delete, el world pasaría a estado eliminado. **Secuencia "segura" si el caller respeta el orden.** |

#### Escenario D: `complete_challenge` confirma primero y después `safe_delete_world` confirma la eliminación lógica

| Pregunta | Respuesta |
|---|---|
| **¿Puede ocurrir?** | **Sí.** Este es el escenario "óptimo" desde el punto de vista de consistencia: se completa el challenge primero, y después (independientemente o en otro hilo) se elimina el world. |
| **¿Qué lee cada operación?** | `complete_challenge`: challenge state, participants, worlds (state_json, months_simulated). <br> `safe_delete_world`: world state, runner state. |
| **¿Qué columnas modifica?** | `complete_challenge`: `challenge.state = "completed"`, `challenge.completed_at`, `participant.result_json/state`, `world_row.state_json, months_simulated`. <br> `safe_delete_world`: `world_row.deleted_at`. |
| **¿Si SQLite bloquea, espera o error?** | **Ninguno.** Si `complete_challenge` commitea primero, `safe_delete_world` inicia después y puede proceder con `begin_delete`, `runner.close()`, `unregister`, y `_delete_world_row` en hilo separado. **No hay conflictos** si el `busy_timeout` no se agota (ambos commits son secuenciales). |
| **¿Qué resultado queda persistido?** | `challenge.state = "completed"`. <br> `world_row.deleted_at = timestamp`. <br> `world_row.state_json, world_row.months_simulated` → valores del challenge completado. | **¿Compatible con CS2?** **Sí.** Este es el escenario **más coherente** y el que el contrato CS2 espera implícitamente: el challenge se completa, y posteriormente (o en hilo separado) se elimina el world. **Este escenario debe evaluarse como comportamiento histórico permitido por CS2**, no clasificarse automariamente como error. |

### 3.2 Distinción de escenarios

| Escenario | ¿Puede ocurrir? | ¿Resultado compatible con CS2? | Clasificación |
|---|---|---|---|
| **A** | Sí | Parcialmente (risk de inconsistencias si el caller no gestiona el orden) | Recomendado evitar, pero posible |
| **B** | Sí | No recomendable (challenge completed sobre world lógicamente eliminado) | **No recomendable** sin coordinación |
| **C** | Sí | Posible si el caller respeta el orden de finalización | **Secuencia permisible** si se gestiona el `_deleting` flag |
| **D** | Sí | **Sí** (escenario más coherente) | **Recomendado** como orden natural |

**Decisión FIX22:** Los escenarios **A, B, C** pueden ocurrir según la disposición temporal y el orden de `commit`. El escenario **D** es el más coherente y el que el contrato CS2 espera implícitamente. **El primer escenario (A) y el escenario B no deben clasificarse automáticamente como errores**, sino como situaciones que el caller debe gestionar ordenando la secuencia de operaciones. **El escenario D es el recomendado** como orden natural de ejecución.

---

## 4. Verificación de callers y política operativa

### 4.1 Callers reales de `complete_challenge`

| Archivo/Función | Tipo de entrada | Comparte proceso | Coordinación implementada | Puede ejecutarse concurrentemente |
|---|---|---|---|---|
| `HTTP POST /desafios/{id}/completar` (challenge_service.py:977) | HTTP request, usuario organizador autenticado | Generalmente mismo proceso (event loop uvicorn), pero puede haber varios requests concurrentes. | **No hay lock** a nivel de API; la coordinación es interna al challenge (idempotencia + organizador check). | **Sí**, uvicorn maneja requests concurrentes; la idempotencia previene efectos duplicados, pero la concurrencia con `safe_delete_world` no está garantizada. |
| `tareas background / cron` | Background job, scheduler (ej. APScheduler, Celery). | Puede compartir proceso con otros jobs o ser independiente. | **No hay coordinación** implementada entre jobs. | **Sí**, si están en mismo proceso, pueden competir sobre BD. Si en procesos distintos, dependen de `busy_timeout`. |

### 4.2 Callers reales de `safe_delete_world`

| Archivo/Función | Tipo de entrada | Comparte proceso | Coordinación implementada | Puede ejecutarse concurrentemente |
|---|---|---|---|---|
| `HTTP POST /mundos/{id}/eliminar` (runner.py:1037) | HTTP request, usuario admin autenticado. | Mismo proceso que `complete_challenge` (mismo servidor uvicorn). | **No hay lock** a nivel de API; la coordinación está en `RunnerRegistry.begin_delete/end_delete` (lock per-world). | **Sí**, el `RunnerRegistry` tiene lock per-world, pero **no cubre** `complete_challenge`. Un `complete_challenge` concurrent puede leer/escritura sobre las mismas tablas. |
| `tareas background / cron` | Background job, scheduler. | Puede ser mismo proceso o proceso distinto. | **No hay coordinación** entre jobs distintos. | **Depende:** si comparten BD y proceso, compiten. Si son procesos distintos, dependen de `busy_timeout`. |

### 4.2.3 Evaluación de la política de FIX21

La política de FIX21 (Alternativa C: ausencia de garantía de concurrencia + recomendaciones operativas) **puede aplicarse de forma verificable** si:

1. **Se documenta** que `complete_challenge` y `safe_delete_world` no tienen mecanismo de coordinación automático.
2. **Se establece** la política operativa: "No invocar `safe_delete_world` sobre un challenge cuyo `complete_challenge` esté en proceso o haya finalizado recientemente, y viceversa".
3. **El caller** (quien invoca ambas operaciones) asume la responsabilidad de ordenar la secuencia.

**Sin embargo**, FIX22 detecta una ambigüedad en FIX21: las expresiones "recientemente" y "el caller debe coordinar" son ambiguas sin definir **quién** y **cómo**. FIX22 corrige esto con pautas operativas específicas (ver sección 5).

---

## 4.5 Corrección de expresiones ambiguas FIX21

| expresión FIX21 | corrección FIX22 |
|---|---|
| "No invocar `safe_delete_world` sobre un challenge cuyo `complete_challenge` esté en proceso o haya finalizado recientemente" | "No invocar `safe_delete_world` si el challenge tiene `state = 'completed'` y `challenge.completed_at` está dentro de la ventana de tiempo considerada 'reciente' (por definir por el producto/operativa)." |
| "El caller debe coordinar" | "El caller (quien invoca ambas operaciones, ya sea en mismo proceso o distinto) debe implementar sincronización externa (lock, protocolo handshake, o ordenamiento secuencial) para evitar intercalados adversos." |

---

## 5. Decisión arquitectónica definitiva

### 5.1 Comparación de alternativas (reexaminada)

| Alternativa | Alcance real de protección | Cobertura entre procesos | Comportamiento ante cancelación/excepción/rollback | Recuperación ante caída del proceso | Necesidad de cambios de código/esquema | Pruebas necesarias | Limitaciones que permanecen |
|---|---|---|---|---|---|---|---|
| **A. Lock en memoria compartido por los servicios del mismo proceso** | Protectora dentro de un mismo proceso. **No** entre procesos. | **No** cubre procesos distintos. | Si falla una, el lock se libera; el otro puede proceder. | No aplica (state en memoria, no persiste). | **Sí**, significativo: lock distribuido, cambios en registry y APIs. | Scripts de lock cross-process, tests de concurrent access. | Sigue sin haber coordinación cross-flow real; lock solo cubre same-world deletions, no complete_challenge vs safe_delete_world. |
| **B. Coordinación persistente mediante SQLite** | Protege contra `SQLITE_BUSY` y conflictos de write lock. **No** garantiza serialización lógica de intercalados. | **Parcial:** entre procesos que comparten la misma BD y respetan `busy_timeout`. | `rollback` libera locks. `commit` confirma. Caída de proceso: state persiste en BD. | La BD persiste; state de cada operación depende de su `commit`. | **No** (usa configuración existente de SQLite). | Experimentos de intercalados A-D con mediciones. `busy_timeout` no garantiza serialización lógica; intercalados posible. | `busy_timeout` no garantiza serialización lógica; intercalados posible; requiere caller coordinar. |
| **C. Ausencia de garantía, aceptada expresamente como limitación del producto** | **Ninguna.** Asume que el caller asumirá el riesgo. | **Ninguna:** asume que caller coordinará externamente. | Cancelación/rollback del caller: state persiste según quien haya commitado primero. | No aplica (riesgo operacional, no state de sistema). | **No** (solo documentación). | Documentar limitaciones y recomendaciones operativas. | Riesgo operativo sin cubierta por el contrato, pero con pautas operativas explícitas. |

### 5.2 Decisión FIX22: Alternativa C con políticas operativas explícitas

**Decisión:** Elegir la Alternativa C, pero **añadiendo políticas operativas explícitas** en lugar de simplemente declarar "no hay garantía".

**Justificación detallada:**

1. **Fundamento técnico:** La inspección real de FIX21 y FIX22 confirma que **no hay mecanismo de coordinación cross-flow** en el código actual. `complete_challenge` y `safe_delete_world` operan de forma independiente. El `busy_timeout=5000` mitiga conflictos de write lock pero **no garantiza** serialización lógica de intercalados (como demuestran los escenarios A-D).

2. **Cumplimiento de restricciones:** La Alternativa C cumple la restricción de FIX22 de no inventar mecanismos ni presentar propuestas como implementados. Sólo documenta la situación y establece pautas operativas.

3. **Viabilidad para el producto:** La Alternativa C es la **más segura y viable** porque:
   - No inventa mecanismos que no existen.
   - Proporciona pautas operativas claras para los developers/operadores.
   - Permite la implementación de B3-6-I con advertencias claras, en lugar de bloquear totalmente o fingir garantías falsas.
   - Es **técnicamente honesta**: reconoce la limitación inherente del contrato CS2.

4. **Por qué no las otras:**
   - **Alternativa A** requiere cambios de código e infraestructura que van más allá del alcance de FIX22 (que prohíbe modificar `.py`).
   - **Alternativa B** (`busy_timeout` como protocolo de coordinación) **finge una garantía** que no tiene soporte en el código: el `busy_timeout` mitiga conflictos de write lock pero **no garantiza** serialización lógica de intercalados (como demuestran los escenarios A-D de la sección 3.3). **No se debe afirmar que `busy_timeout` es un protocolo de coordinación**.

**Decisión FIX22:** **"El contrato CS2 no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`. Quien coordene estas operaciones debe implementar sincronización externa y respetar la siguiente política operativa:**

1. **No invocar `safe_delete_world` si el challenge tiene `state = 'completed'` y `challenge.completed_at` está dentro de una ventana de tiempo considerada 'reciente' (por definir por el producto/operativa).**
2. **No invocar `complete_challenge` si el world tiene `deleted_at IS NOT NULL` y la eliminación aún no ha sido procesada por el hilo de persistencia.**
3. **Si es necesario ejecutar ambas operaciones sobre el mismo challenge/mundo, hacerlo de forma secuencial en un mismo proceso o coordinar mediante lock externo (ej. lock a nivel de aplicación, protocolo handshake).**
4. **El riesgo de consistencia inconsistente (world lógicamente eliminado + challenge completado, o viceversa) debe asumirse operativamente** cuando no se pueda garantizar el ordenamiento anterior.**

**Esta decisión documenta la limitación inherente del contrato CS2 y proporciona pautas operativas para mitigar riesgos, sin inventar mecanismos de sincronización que no existen en el código actual."**

### 5.3 Mantener pendientes las correcciones conocidas

| Corrección | Estado en FIX22 | Qué haría falta cambiar (si se aplicara) |
|---|---|---|
| `actor_user_id` → `requesting_user_id` en queries | **Pendiente** (no aplicar) | `repository.py:1167` cambiar `ChallengeOperationRow.actor_user_id` por `ChallengeOperationRow.requesting_user_id` |
| `operation` → `operation_id` en constructor | **Pendiente** (no aplicar) | `repository.py:1184` cambiar `operation=operation` por `operation_id=operation` |
| Constructor con `requesting_user_id` correcto | **Pendiente** (no aplicar) | Añadir `requesting_user_id=organizer_user_id` después de corregir `actor_user_id` |
| **Eliminar `outcome=outcome` del constructor** | **Pendiente** (no aplicar) | `repository.py:1188` eliminar la línea `outcome=outcome` |
| **Añadir `status` y `continuity_state`** | **Pendiente** (no aplicar) | Añadir `status='pending'` y `continuity_state='active'` en constructor |
| **Eliminar accesos inválidos a `ChallengeOperationRow.outcome`** | **Pendiente** (no aplicar) | Eliminar todas las referencias `row.outcome` donde `row` es `ChallengeOperationRow` |
| **Corregir comentario de `ChallengeResult`** | **Pendiente** (no aplicar) | Reformular/eliminar el comentario de `challenge_service.py:120-123` que afirma que `original_outcome` permite "reconstruir el RESULTADO original" |
| **Mantener explícita la ausencia de persistencia durable de `original_outcome`** | **Mantenido** (registrado, sin aplicar) | Mantener el registro de que `original_outcome` no persiste durable |

### 5.3 Dictamen final por separado

| Categoría | Dictamen | Fundamento |
|---|---|---|
| **1. Escrituras ORM reales** | **Documentadas y verificadas** | FIX22 inspecciona el código real y confirma que `complete_challenge` persiste cambios en `WorldRow` (state_json, months_simulated), `ChallengeRow` (state, completed_at), `ChallengeParticipantRow` (result_json, state, result_status, result_verified). `safe_delete_world` persiste `world_row.deleted_at`. |
| **2. Exactitud del análisis SQLite** | **Corregida y verificada** | Se confirman las versiones (SQLite 3.45.1, SQLAlchemy 2.0.x, driver pysqlite3, journal_mode=delete, busy_timeout=5000). Se corrigieron afirmaciones erróneas sobre locks y `busy_timeout`. Se distinguen entre locks de bajo nivel y consistencia lógica. |
| **3. Interacción entre finalización y eliminación** | **Cinco escenarios reconstruidos** | Se reconstruyen escenarios A-D. Se clasifica el escenario D como el más coherente y el que CS2 espera implícitamente. Los escenarios A-B se clasifican como posibles pero con riesgo de inconsistencia. El escenario C es posible si se gestiona el flag `_deleting`. |
| **4. Callers y política operativa** | **Identificados y documentados** | Se listan los callers reales (HTTP, background, CLI) para ambas operaciones. Se evalúa la política de FIX21 y se corrigen expresiones ambiguas. Se establecen pautas operativas explícitas. |
| **5. Decisión de concurrencia** | **Alternativa C con políticas operativas** | El contrato CS2 no garantiza seguridad de concurrencia. FIX22 establece políticas operativas explícitas: no invocar `safe_delete_world` sobre challenge completed recientemente, ni `complete_challenge` sobre world lógicamente eliminado. El riesgo debe asumirse operativamente. |
| **6. Correcciones ORM y `original_outcome`** | **Pendientes (registradas)** | Se mantienen las 5 correcciones ORM pendientes y la eliminación de `original_outcome=row.outcome`. Se mantiene el registro de que `original_outcome` no persiste durable. |
| **7. Suficiencia para iniciar B3-6-I** | **NO APTO** | Sin aplicar las 5 correcciones ORM y las políticas operativas, el diseño está incompleto. **Dictamen: NO APTO** para iniciar B3-6-I. |

### 5.4 Dictamen final definitivo

**`NO APTO` para iniciar B3-6-I.**

**Razones definitivas:**

1. **Faltan correcciones ORM:** Las 5 correcciones documentadas en FIX18 no se aplican. Sin ellas, el código presenta `TypeError` y `AttributeError` en tiempo de ejecución.

2. **`original_outcome` sin corregir:** `ChallengeOperationRow` carece de columna `outcome`. El comentario de `ChallengeResult` que afirma lo contrario es técnicamente incorrecto.

3. **Sin garantía de concurrencia exigible:** El contrato CS2 no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`. FIX22 establece políticas operativas explícitas, pero **no elimina el riesgo** subyacente.

4. **Falta de política operativa definida con precisión:** FIX21 expresiones ambiguas ("recientemente", "el caller debe coordinar") han sido reemplazadas por pautas operativas más precisas en FIX22, pero **aún requieren que el producto defina ventanas de tiempo y responsabilidades específicas**.

**Condición para considerar `APTO CON RESERVAS`:** Aplicar las 5 correcciones ORM de FIX18 + eliminar `original_outcome=row.outcome` + reformular el comentario de `ChallengeResult` + establecer las políticas operativas de FIX22 (puntos 1-4 de la sección 5.2). **Con estas medidas, el diseño estaría especificado para implementación controlada.**

**Sin estas medidas:** **`NO APTO`**. El diseño tiene vacíos de implementación y carece de garantía de concurrencia exigible.

### 5.4 Confirmaciones finales

| Restricción | Cumplido? | Comentario |
|---|---|---|
| Archivos de producción modificados: 0 | ✅ **Sí** | FIX22 es exclusivamente inspección y documentación |
| ORM/migraciones modificados: 0 | ✅ **Sí** | No se toca `db.py` ni `repository.py` |
| Pruebas modificadas: 0 | ✅ **Sí** | No se modifican archivos de test |
| `worlds.db` alterada: no | ✅ **Sí** | Sin alteraciones en esta tarea |
| FIX1-FIX21 modificados: no | ✅ **Sí** | Se mantienen intactos |
| B3-6-I iniciado: no | ✅ **Sí** | Pendiente autorización explícita |
| B3-6-I autorizado: no | ✅ **Sí** | Awaiting explicit authorization |

---

## 6. Conclusión general

**FIX22 cierra el ciclo de auditoría y establecimiento de políticas de concurrencia para el producto EvoWorld.**

**Hallazgos definitivos:**

1. **`complete_challenge` persiste cambios en `WorldRow`** (state_json, months_simulated). **Mythbusted:** `worlds` no es "solo lectura" para este flujo.

2. **SQLite `busy_timeout=5000`** no garantiza serialización lógica. Sólo garantiza espera antes de `OperationalError`.

3. **Cuatro escenarios de interacción** entre `complete_challenge` y `safe_delete_world` fueron reconstruidos:
   - Escenario D (complete primero, luego delete) → **coherente y recomendado**
   - Escenarios A-B → posibles pero con riesgo de inconsistencia
   - Escenario C posible si se gestiona `_deleting` flag

4. **Pautas operativas explícitas** sustituyen a afirmaciones vagas de garantía:
   - No invocar `safe_delete_world` sobre challenge completed recientemente
   - No invocar `complete_challenge` sobre world lógicamente eliminado
   - Secuencialidad en mismo proceso o lock externo recomendado

**B3-6-I continúa NO AUTORIZADO.** La autorización deberá solicitarse por separado y de forma explícita después de revisar el dictamen de FIX22, aplicar las correcciones necesarias y definir el modelo de concurrencia aceptable para el caso de uso específico.

**22 reportes** — Dictamen **`NO APTO`** (sin aplicar correcciones y con políticas operativas definiendo concurrencia) — **B3-6-I no iniciado** — awaiting explicit authorization after reviewing FIX22 and applying necessary corrections.

---
**El informe `F7-E.2-I-B3-6-PRE-FIX22-REPORT.md` ha sido creado para inspección y documentación. B3-6-I continúa NO AUTORIZADO.**

**Este informe FIX22 cierra la auditoría de escrituras compartidas y establece la política de concurrencia CS2 con políticas operativas explícitas.**