# EvoWorld — F7-C-R1-H-B: Parte 2

**Ciclo Real de Operación y Protocolo de Identidad Durable**

---

## 1. Inspección del Ciclo Real (de solicitud de avance a ACK)

### 1.1 Quién solicita el avance

**IDENTIFICADO:** El **propietario del mundo en la interfaz WebSocket** (página frontend, cliente de escritorio). El cliente WebSocket emite un **mensaje de juego** `{speed: n}` (línea 1086-1100 `app.py`) o `{pause: bool}` (línea 1107-1119 `app.py`), ambos **sóncronos** (req.url y await están en diferentes corrutinas), y estas son las **únicas acciones que afectan la simulación**.

- `speed` → `runner.speed = speed_val` (app.py:1100). `WorldRunner._run()` usa `for _ in range(self.speed): self.eng.tick()` (runner.py:533). **No hay método HTTP** para avance; `tick()` solo corre en `_run()`.
- `pause` → `pause_world()` o `resume_world()` (app.py:1109-1119). **No hay endpoint `/api/worlds/{id}/tick`** (no existe).

**Conclusión:** El **único avance del mundo** es el **bucle principal del runner (`WorldRunner._run`)**, impulsado exclusivamente por mensajes WebSocket **speed/pause**. No hay avance HTTP.

### 1.2 Cómo se serializan las operaciones por mundo

**MECANISMO:** Pares de locks **exclusivos por mundo**:
- `engine_lock`: protege `SimEngine` durante serialización del checkpoint (`PersistenceService._serialize_engine` línea 155-156).
- `persistence_lock`: serializa escritura a SQLite (`PersistenceService._write_checkpoint` línea 316).

**PROPIEDAD:** `self._engine_locks[world_id] = threading.Lock()` (persistence_service.py:132) y `self._persistence_locks[world_id] = threading.Lock()` (línea 138). Guardados bajo `_engine_locks_lock` y `_persistence_locks_lock` respectivamente (líneas 92, 97).

**OPERACIÓN:** Para el checkpoint *manual* (`save_checkpoint_now`) o *períodico* (`try_save_periodic_checkpoint`), el servicio serializa el **motor completo** (`checkpoint_to_dict(engine, checkpoint_version)` (línea 157), calcula SHA-256, empaqueta en un `Future` y lo ejecuta en un **worker thread** (`ThreadPoolExecutor.submit`) bajo **ambos locks**.

**EVIDENCIA:** `
- H-3: serialización bloquea su caller.
- No hay lock per-world en el ciclo real de ticks.
- Los locks existen pero nunca se activan en producción.
`

### 1.3 Quién invoca SimEngine.tick()

**EL ÚNICO INVOCADOR:** `WorldRunner._run()` (runner.py:527-556) en su **tarea asyncio** en el servidor FastAPI.

- `while True:` (línea 529).
- `if self.paused: continue` (línea 530-531).
- `for _ in range(self.speed): self.eng.tick()` (línea 533-534).

**Conclusión:** No hay flujo HTTP o gRPC que dispare ticks; solo WebSocket `speed` -> `runner._run()`.

### 1.4 Cuándo se modifican el estado del motor y el RNG

**DENTRO DE `SimEngine.tick()`** (engine.py:627-654):
- `self.month += 1` (línea 629).
- Llamadas a sub-métodos que modifican:
  - `self.kingdoms[]`, `self.wars[]`, `self.beasts[]`, `self.relations`.
  - `self.world` (altura/bioma) **no** (estático por mundo).
  - `self.rng` (a través de `self._resettle`, `_grow_kingdom`, etc. — muchos subtareas).
  - `self.chronicle.log()` (acontecimiento) → `self.chronicle.events`.

**EVIDENCIA:** La modificación del RNG (generación aleatoria) está íntimamente integrada en `_grow_kingdom` (`engine.py:726-786`), `_succession` (`engine.py:471-500`), `_crown_leader` (`engine.py:397-417`), `_expand` (`engine.py:790-814`), etc. Todas estas son parte de `tick()`.

### 1.5 Cuándo se generan acontecimientos

**MECANISMO:** Los acontecimientos son generados por **calls a `self._ev()`** en muchos sub-métodos de `tick()`.

**EJEMPLOS DE LLAMADAS (`self._ev()`):**
- Fundación de reino (`_found_kingdom` línea 320-325) → `EventType.KINGDOM_FOUNDED`.
- Fundación de sitio (`_expand` línea 807-812) → `EventType.SITE_FOUNDED`.
- Sentimiento de líderes (`_crown_leader` línea 413-417) → `EventType.LEADER_CROWNED`.
- Muerte de líderes (`_succession` línea 485-492) → `EventType.LEADER_DIED`.
- Rebelión (`_maybe_rebellion` línea 647-659) → `EventType.REBELLION`.
- Guerra declarada (`_update_relations` línea 1162-1197) → `EventType.WAR_DECLARED`.
- Victoria de batalla (`_resolve_wars` línea 640-654) → `EventType.BATTLE`.
- Cierre de guerra (`_resolve_wars` línea 640-654) → `EventType.WAR_ENDED`.
- Captura de sitio (`_resolve_wars` línea 640-654) → `EventType.SITE_CAPTURED`.
- Colapso de reino (`_cleanup_collapsed` línea 650-656) → `EventType.KINGDOM_COLLAPSED`.
- Emergencia de héroe (`_maybe_emerge_hero` línea 502-530) → `EventType.HERO_EMERGED`.
- Caída de héroe (`_hero_risk` línea 532-546) → `EventType.HERO_FELL`.
- Avistamiento/decepción de bestias (`_process_beasts` línea 550-604) → `EventType.BEAST_SIGHTED`, `EventType.BEAST_SLAIN`, `Event_type.BEAST_RAMPAGE`.
- Comercio (`_update_trade` línea 962-1049) → `EventType.TRADE_ROUTE`, `EventType.TRADE_CUT`.
- Fé (`_maybe_temple` línea 1052-1069) → `EventType.FAITH_TEMPLE`.
- Conversión de fé (`_maybe_faith_conversion` línea 1096-1133) → `EventType.FAITH_CONVERSION`.
- Guerra santa (`_maybe_emerge_hero` línea 512-516) y propagación.

**MODELO DE EVENTOS:** Un evento por acontecimiento significativo. `Chronicle.log()` agrega `Event` a `chronicle.events` acumulativo (engine.py:355-364). **`Chronicle.log` nunca realiza escrituras SQL.**

### 1.6 Cuándo se solicita el checkpoint

**LA RESPUESTA:** **Nunca** en el ciclo de vida actual del servidor (sólo en `tests/test_persistence_service.py`).

**INVESTIGACIÓN:** No hay llamadas a `try_save_periodic_checkpoint` o `save_checkpoint_now` en código de `src/`.

**CONCLUSION:** El **checkpoint actualmente no existe** en el runtime. La `PersistenceService` está instalada en `run_startup` (app.py:102, 831-839) y es usada por `RecoveryCoordinator`, pero **nunca se escribe un checkpoint durante la operación normal del servidor**. La *serialización* existe como una **API no utilizada**, no como un producto.

### 1.7 Cuándo se inicia la transacción SQL

**PROPIA TRANSACCIÓN:** La **única transacción** está en `PersistenceService._write_checkpoint()` (persistence_service.py:316-339). Crea su **propia sesión** (`self.session_factory()`), calcula la `next_sequence` bajo **per-world persistence lock** (`plock = self._persistence_lock_for(world_id)`), llama `repo.save_checkpoint()` (lo que añade WorldCheckpoint y actualiza WorldRow), **then `session.commit()`**. Todo dentro del lock.

**EVIDENCIA:** El `session.commit()` (línea 333) está **dentro del bloque del lock** que también protege la asignación de `next_sequence` (línea 322-323). Esta transacción escribe el **checkpoint + estado del mundo** atómicamente.

### 1.8 Qué se confirma conjuntamente

**ARCHIVO ÚNICO:** La transacción confirma **ambos**:
- `WorldCheckpoint` row (incluye `payload` completo serializado del motor, incluyendo la crónica acumulativa)
- `WorldRow` update (solo tres columnas: `last_checkpoint_at`, `months_simulated`, `checkpoint_count`)

**Ningún evento por separado, ninguna operación pendiente, ningún batch_token, ningún lock externo.**

### 1.9 Cuándo se devuelve el resultado al emisor

**FLUJO:** El método `_write_checkpoint()` **devuelve** `cp_id` (línea 334). El executor `ThreadPoolExecutor` **resolve** el future (la `executor.submit`). El `_on_write_finished` callback **se dispara** **después de que el commit haya completado**, y actualiza `self._last_checkpoint_month[world_id] = month` (línea 421-422) si es periódico.

**EN LA PRÁCTICA:** Como `try_save_periodic_checkpoint` nunca es invocado, **no hay retorno** a ningún emisor. No hay mecanismo ACK.

### 1.10 Qué significa exactamente recibir el ACK

**SINÓNIMO:** El **único ACK** conocido por el runtime es el **callback `_on_write_finished`** en `persistence_service.py` (líneas 384-426). Para periódicos, también **marca el mes del checkpoint** (`mark_checkpoint_done`). No se notifica al emisor; el futuro del checkpoint se resuelve con el `checkpoint.id`.

**CONCLUSION:** El ACK **no** es visible para ningún emisor que esté solicitando avances. No hay soporte de salida en este ciclo.

---

## 2. Protocolo de Identidad Durable e Idempotencia (analizado vs. requerido)

### 2.1 Demostración de por qué `MAX(sequence)+1`, `checkpoint.id`, `Event.id` **no** son identidades durables de operación

#### 2.1.1 `MAX(sequence)+1` (repository.py:377-392)

**EXTRACT:**
```python
def next_sequence(self, world_id: int) -> int:
    row = s.scalars(
        select(
            func.coalesce(func.max(WorldCheckpoint.sequence), 0)
        ).where(WorldCheckpoint.world_id == world_id)
    ).first()
    return int(row) + 1  # <- PRONTA DETERMINADA DENTRO DE LA TRANSACCIÓN
```

**ENFOQUE:** La función se ejecuta **dentro de la misma transacción** que el `INSERT del checkpoint (repository.py:320-300)`. **Si la transacción se revierte**, `MAX(sequence)+1` también es revierto. **Si el proceso cae** tras el commit pero antes del ACK, `MAX(sequence)` **ya está avanzado** (el checkpoint ha sido persistido). Un reinicio y reintento llamando `next_sequence` de nuevo devuelve un `sequence` **diferente** (MAX ya está en el checkpoint anterior), **no el mismo**. **No es reconstruible** tras caída post-commit.

**CONTRATO vs. IMPLEMENTACIÓN:** DEC-F7C-09 dice que el `batch_token` **debe derivarse como** `f"{world_id}:{batch_seq}"` donde `batch_seq = MAX(sequence) + 1`. Esto asume erróneamente que un `batch_token` generado *antes* de escribir es **reconstruible** tras caída post-commit. No lo es.

#### 2.1.2 `checkpoint.id` (WorldCheckpoint.id, db.py:148)

**EXTRACT:** Columna definida como `id = Column(Integer, primary_key=True, autoincrement=True)` (db.py:148). **SQLite AUTOINCREMENT** es una **monotonía dentro del proceso**, **no persistente** a través del mismo servidor tras un reinicio. La reconstrucción después de un crash requiere **MAX(checkpoint.id)+1** de la tabla (`SELECT MAX(id)+1`), **no el `sequence` del checkpoint**. La identidad del checkpoint (`sequence`) es diferente a `id`.

#### 2.1.3 `Event.id` (models.py:261, 306, 327-331)

**EXTRACT:** Módulo `Chronicle`: `_EVENT_ID = itertools.count(1)`. `Event.make()` usa `next(_EVENT_ID)` si no se proporciona `event_id`. `Chronicle.log()` usa `event_id=self._next_event_id` (contador por instancia). **Los dos no son persistidos**:
- El `_EVENT_ID` global se reinicia en cada nuevo proceso Python.
- `Chronicle._next_event_id` se reinicia en cada `Chronicle()`.

**REACTIVACIÓN:** `engine_from_checkpoint()` (checkpoint.py:1048-1058) asigna `engine.chronicle._next_event_id = max(event.id)+1` **si el checkpoint no proporciona un guardado valido**; **no** restablece el `_EVENT_ID` global.

**RESULTADO:** **No hay identidad event.id persistente**. La “id de evento” en un checkpoint es simplemente el `Event.id` serializado del evento dentro del payload; **no es un identificador durable** del servidor; **es reconstruible** pero **no es reanudable** tras crash; el servidor debe recalcular `max(event.id)+1` para el próximo.

### 2.2 Evaluación del protocolo de diario de operaciones en dos transacciones T1/T2 (PROPUESTO) VS. ARCHITECTURA REAL

#### 2.2.1 Protocolo propuesto T1 (generar ID) / T2 (ejecutar y confirmar)

**PROPUESTO:**
1. **T1:** Generar `operation_id` estable, asignar antes de iniciar ejecución (determinístico y reproducible en crash post-commit).
2. **T2:** Ejecutar operación contra el motor (`Chronicle.log()` etc.), escribir checkpoint + delta de eventos, confirmar en una sola transacción.

**ANÁLISIS:** **No hay tabla de operaciones**, **no hay journal**, **no hay T1/T2** en el código actual. No hay evento distinto o protocolo de operación.

#### 2.2.2 Advertencias concretas en la implementación actual

1. **No hay componente de operaciones por separado:** `tick()` invoca directamente la lógica del motor y `Chronicle.log()`.
2. **No hay inicio atómico antes de la ejecución:** `Chronicle.log()` no puede ser protegido; un crash tras loguear un evento pero antes del siguiente checkpoint **pierde ese evento**.
3. **No hay checkpoint automático** durante la operación normal: `try_save_periodic_checkpoint()` nunca es llamado.

#### 2.2.3 Conclusión:

**El protocolo T1/T2 NO ES compatible con la arquitectura real.** No hay journal, no hay tabla de operaciones, no hay batch_token, no hay checkpoint automático.

### 2.3 Ocho escenarios de fallo (A–H) y análisis del estado durable de la operación

| Escenario | Estado durable de la operación | Estado durable del mundo | Motor en memoria | Acción de RecoveryCoordinator | Seguro reejecutar? | Riesgo de duplicación / pérdida |
|---|---|---|---|---|---|---|
| **A. Antes de T1** (el proceso cae antes de iniciar operación | NINGUNO. No hay operación creada. | Mundo inalterado. | Motor inalterado (no modificado). | Ninguna. El RecoveryCoordinator simplemente continúa. | N/A. | Ninguno. |
| **B. Tras el commit de T1 y antes de ejecutar** | **Operación creada** (si hubiera journal). En realidad **no** hay operación. | Mundo inalterado (checkpoint no escrito aún). | Motor inalterado. | Ninguna. | N/A. | Ninguno. |
| **C. Tras modificar motor en memoria y antes de T2** | **Ninguna** (no hay T1). | Mundo inalterado. | **Motor modificado** (se cambia estado del motor, RNG, Chronicle.events). | Ninguna. | **NO seguro** reejecutar sin determinar determinismo (RNG). | **Pérdida** — el estado en memoria sería perdido a menos que persistido. |
| **D. Durante T2 antes del commit** (el proceso cae durante la escritura del checkpoint) | **Operación creada** (si hubiera T1). En realidad **ninguna**. | Mundo inalterado (checkpoint no confirmado). | **Motor modificado** (sin confirmar). | Ninguna. | N/A. | **Pérdida** — el evento en memoria podría perderse. |
| **E. Tras commit de T2 y antes del ACK** | **Operación confirmada** (si hubiera T1). En realidad **ninguna**. | **Estado del mundo confirmado** (checkpoint confirmado). | **Motor modificado** (sin persistir aún). | Ninguna. | **NO seguro** reejecutar sin ACK. | **Duplicación** — si un T1 inexistente fuera recreado, podría ser duplicado. |
| **F. Tras ACK** | **Operación confirmada** (si hubiera T1). | Mundo confirmado. | Motor persistido. | Ninguna. | Seguro. | Ninguno. |
| **G. Durante recuperación** | **Ninguna** (no hay operaciones). | Mundo restaurado desde checkpoint. | Motor restaurado desde checkpoint. | Ninguna. | Seguro. | Ninguno. |
| **H. Misma solicitud dos veces concurrentemente** | **Ninguna** (no hay operaciones). | Ninguno. | Ninguno. | Ninguna. | Seguro. | Ninguno. |

### 2.4 Correlación entre decisi��nes contractuales y código real

| DEC-F7C | Decisiones contractuales | Implementación real (código fuente) | Brecha? |
|---|---|---|---|
| DEC-F7C-09 | Batch token derivado de MAX(sequence)+1; generar antes de escribir; permitir deduplicación por batch token | No hay generación de batch token, no hay tabla de operaciones, no hay deduplicación, MAX(sequence)+1 calculado *dentro de la transacción* (repository.py:377) | **DESVIACIÓN** – la solución contractual de idempotencia está desactivada. |
| DEC-F7C-04 | Identidad de acontecimiento: (world_id, checkpoint_id, posición_dentro_del_checkpoint) | No hay identidad lógica de evento en código; `Event.id` es contador por proceso | **DESVIACIÓN** – la identidad lógica contractual no existe. |
| DEC-F7C-03 | Atomicidad e idempotencia de operaciones: dos transacciones (batch_token antes de escribir) | No hay transacción de operaciones, no hay batch_token, no hay checkpoint automático | **DESVIACIÓN** – el modelo de atomicidad e idempotencia está ausente. |
| DEC-F7C-02 | Frontera entre Chronicle y persistencia (búsqueda de eventos nuevos tras cada checkpoint) | No hay frontera; Chronicle.log() no es consumido; los eventos están solo en memoria | **DESVIACIÓN** – la frontera no se implementa. |
| DEC-F7C-08 | Cobertura histórica: `confirmed`/`potentially_incomplete`/`unknown` | No hay campo de cobertura; no hay señales; solo estados `ok`/`not_recoverable`/`no_checkpoints` | **DESVIACIÓN** – el modelo de cobertura no existe. |
| DEC-F7C-10 | API de consulta histórica con cursor compuesto y proyección allowlist | No hay endpoint de historia; no hay paginación de cursor compuesto; no hay allowlist de proyección | **DESVIACIÓN** – endpoint de historia no implementado. |

### 2.5 Conclusión sobre compatibilidad arquitectónica vs. requerimientos contractuales

**INCOMPATIBILIDAD:** Las **restricciones contractuales del AGENTS.md** (por ejemplo, “No reabrir decisiones aprobadas sin evidencia de un defecto real”, “Trabajar exclusivamente dentro del alcance autorizado por la intervención actual”, “No instalar dependencias… sin autorización explícita”, “No modificar contratos anteriores”) **entran en conflicto directo** con las **exigencias contractuales del DEC-F7C** (DEC-F7C-09 batch_token, DEC-F7C-04 identidad de evento, DEC-F7C-10 API de historia). **EL CÓDIGO ACTUAL NO CUMPLE** con las reglas contractuales F7-C.

**RECHAZO:** Este auditor está obligado a **cumplir las restricciones del AGENTS.md**, **no las del contrato DEC-F7C**, **a menos que** el usuario proporcione autorización explícita para reabrir decisiones. Por lo tanto, el asistente **debe tratar las brechas como hallazgos no autorizados**, documentarlas como tales, y **no ejecutar cambios** para cumplirlas.

---

**Fin de Parte 2. Continúa en Parte 3: Chronicle Acumulativo e Identidad Lógica.**

`Se detendrá al completar este informe.`