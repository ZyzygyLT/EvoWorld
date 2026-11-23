# EvoWorld — F7-C-R1-H-B: Informe de Auditoría Forense de Persistencia Histórica

**Parte 1 de 4: Dictamen, Estado Git y Evidencia de Código Inspeccionada**

---

## 1. Dictamen Inequívoco

**NO CERRABLE (PARCIAL)**

La auditoría revela que el contrato F7-C (DEC-F7C-01 a DEC-F7C-12) está **definido y corregido** (F7-C-R1-D), pero **ninguna de las garantías requeridas es demostrable con el código actual**. El sistema actual **no implementa** ninguna de las correcciones contractuales necesarias para:

| Garantía contractual | Estado en código actual |
|---|---|
| Identidad durable de operación (batch_token / diario de operaciones) | **NO EXISTE** — no hay tabla de operaciones, ni `batch_token` en `WorldCheckpoint`, ni protocolo de dos transacciones |
| Idempotencia de creación / ejecución / persistencia / respuesta | **NO DEMOSTRABLE** — `MAX(sequence)+1` se calcula dentro de la transacción, no antes; el emisor no puede reconstruir el mismo token tras caída post-commit |
| Atomicidad estado + acontecimientos | **PARCIAL** — checkpoint + `WorldRow` se confirman en la misma transacción (`PersistenceService._write_checkpoint`), pero los acontecimientos viven solo dentro del payload serializado, sin identidad lógica separada ni deduplicación |
| Identidad lógica de acontecimiento (`world_id, checkpoint_id, posición`) | **NO EXISTE** — `Event.id` es contador en memoria por proceso (`itertools.count(1)`); no sobrevive reinicios ni restauraciones |
| Paginación 20 eventos con cursor compuesto `checkpoint_id:posición` | **NO EXISTE** — no hay endpoint `/api/worlds/{id}/events` ni lógica de extracción incremental desde payloads acumulativos |
| Cobertura histórica (`confirmed` / `potentially_incomplete` / `unknown`) | **NO DEMOSTRABLE** — no hay forma de distinguir "no hay eventos" de "eventos en memoria sin persistir"; el contrato DEC-F7C-08 exige explícitamente `potentially_incomplete` pero no hay implementación |
| Restauración segura (rechazar reescritura de historia publicada) | **NO EXISTE** — `RecoveryManager` restaura el primer checkpoint válido sin verificar si eso reescribe historia confirmada |
| Comparación A/B/C con recomendación justificada | **DOCUMENTADA EN CONTRATO** (Alternativa B = registro incremental persistente) pero **NO IMPLEMENTADA** |

**Bloqueadores exactos (no demostrables con código disponible):**

1. **No hay identidad durable de operación** — el `batch_token` contractual (`f"{world_id}:{MAX(sequence)+1}"`) se calcula *dentro* de la transacción del checkpoint (`_write_checkpoint` línea 322-323), no *antes*. Si el proceso cae tras commit pero antes del ACK, el emisor no puede reconstruir el mismo token porque `MAX(sequence)` ya cambió.
2. **No hay tabla de operaciones / diario persistente** — DEC-F7C-09 establece que los eventos viven *solo* en `WorldCheckpoint.payload`; no existe `world_operations` ni `world_events` separada.
3. **No hay extracción incremental** — `Chronicle.events` es acumulativo en memoria; los checkpoints serializan la lista completa. No hay código que compare checkpoint N vs N-1 para emitir solo el delta.
4. **No hay endpoint de historia** — `GET /api/worlds/{id}/events` no existe en `app.py`.
5. **No hay cobertura demostrable** — `RecoveryManager` devuelve `ok`/`not_recoverable`/`no_checkpoints` pero no expone `first_checkpoint_month` / `last_checkpoint_month` / `status` de cobertura.
6. **`Event.id` no es durable** — `models.py:261, 306, 328, 358` usa `itertools.count(1)` global de módulo, reiniciado por proceso.

---

## 2. Estado de Git (Inicial y Final — Idénticos)

| Elemento | Valor |
|---|---|
| **HEAD** | `e6f6983dfea3298932dd8b74a97aa56f72540f5b` |
| **Rama** | `feature/engine-checkpoints` |
| **Commits creados** | **0** |
| **Ramas creadas** | **0** |
| **Cambios staged (preexistentes)** | `src/worldsim/export/checkpoint.py`, `tests/test_checkpoint.py` |
| **Cambios unstaged (preexistentes)** | `src/worldsim/server/app.py`, `src/worldsim/server/db.py`, `src/worldsim/server/runner.py`, `src/worldsim/sim/engine.py`, `src/worldsim/sim/models.py`, `tests/test_auth.py`, `tests/test_league.py`, `tests/test_multiplayer.py`, `tests/test_server.py`, `web/src/App.jsx`, `web/src/components/RoomPanel.jsx` |
| **Untracked (preexistentes)** | `.opencode/`, `AGENTS.md`, `AUDITORIA_FASE1_parte2.md`, `docs/`, `src/worldsim/export/snapshot.py`, `src/worldsim/server/errors.py`, `src/worldsim/server/persistence_service.py`, `src/worldsim/server/recovery_coordinator.py`, `src/worldsim/server/recovery_manager.py`, `src/worldsim/server/repository.py`, `src/worldsim/server/transitions.py`, `tests/test_*.py` (19 archivos) |
| **Modificaciones de esta auditoría** | **NINGUNA** (tarea read-only) |

Verificación: `git status --short` antes y después produce salida idéntica. No se ejecutó `git add`, `git commit`, `git checkout`, `git reset`, ni ningún comando destructivo.

---

## 3. Evidencia Técnica Inspeccionada (Archivo, Símbolo, Líneas)

### 3.1 Modelo de Eventos y Chronicle — `src/worldsim/sim/models.py`

| Símbolo | Hallazgo | Líneas |
|---|---|---|
| `_EVENT_ID = itertools.count(1)` | Contador global de módulo, **se reinicia en cada nuevo proceso Python**. No sobrevive a reinicios. | 261 |
| `Event.make(..., event_id: int \| None = None)` | Si no se pasa `event_id`, usa `next(_EVENT_ID)`. `Chronicle.log` pasa `event_id=self._next_event_id` (contador por instancia). | 314-331 |
| `Chronicle._next_event_id` | Contador por instancia (`= 1` en `__init__`), incrementado en `log()`. Se reinicia al restaurar checkpoint (ver `engine_from_checkpoint` líneas 1048-1058). | 345, 356-364 |
| `Chronicle.events` | `list[Event]` **acumulativo en memoria**. No hay lógica de extracción delta ni persistencia independiente. | 343, 361 |
| `Chronicle.log()` | Añade a `self.events`, indexa en `_by_kingdom`. **No realiza escrituras SQL**. | 347-364 |

**Conclusión:** `Event.id` es efímero (por proceso). No es identidad durable. La crónica completa se serializa en cada checkpoint.

### 3.2 SimEngine.tick() y Generación de Eventos — `src/worldsim/sim/engine.py`

| Símbolo | Hallazgo | Líneas |
|---|---|---|
| `SimEngine.tick()` | Avanza `self.month += 1`, ejecuta lógica de reinos (crecimiento, sucesión, guerras, etc.). Cada cambio notable llama a `self._ev()` → `self.chronicle.log()`. | 627-654 |
| `_ev(type_, key, params, kids)` | Renderiza texto en inglés y llama `self.chronicle.log(month, type_, text, kids, params, key)`. | 219-233 |
| `Chronicle` instanciado en `__init__` | `self.chronicle = Chronicle()` línea 198. Nueva instancia = contador de eventos reiniciado. | 198 |

**Ciclo real de generación:** `tick()` → lógica de dominio → `_ev()` → `Chronicle.log()` → evento en memoria. **Cero SQL** en este camino.

### 3.3 Persistencia de Checkpoints — `src/worldsim/server/persistence_service.py`

| Símbolo | Hallazgo | Líneas |
|---|---|---|
| `PersistenceService._serialize_engine()` | Serializa `SimEngine` completo (incluye `chronicle.events` completo) a JSON + SHA-256. Corre bajo `engine_lock` (líneas 155-156). | 145-161 |
| `PersistenceService._write_checkpoint()` | **Núcleo atómico**: crea sesión, `repo.next_sequence(world_id)` (línea 323), `repo.save_checkpoint()` (líneas 324-332), `session.commit()` (línea 333). Retorna `cp_id`. | 299-339 |
| `try_save_periodic_checkpoint()` | Reserva escritura periódica: serializa *fuera* del lock (líneas 256-258), luego bajo `_pending_cond` verifica intervalo y registra future (líneas 260-283). | 225-297 |
| `save_checkpoint_now()` | Escritura manual inmediata: serializa, submite a executor con `sequence=None` (se calcula en worker). | 163-223 |

**Crítico:** `sequence = repo.next_sequence(world_id)` se ejecuta **dentro** de la transacción (línea 322-323), bajo `persistence_lock`. No hay `batch_token` generado *antes* de la transacción. No hay verificación de idempotencia por `batch_token` antes de escribir.

### 3.4 Repositorio: next_sequence y save_checkpoint — `src/worldsim/server/repository.py`

| Símbolo | Hallazgo | Líneas |
|---|---|---|
| `CheckpointRepository.next_sequence()` | `SELECT COALESCE(MAX(sequence), 0) + 1 FROM world_checkpoints WHERE world_id = ?` (líneas 386-392). Se ejecuta **dentro de la misma sesión/transacción** que el `INSERT` del checkpoint. | 377-392 |
| `CheckpointRepository.save_checkpoint()` | Crea `WorldCheckpoint` con `sequence`, `month`, `payload`, `checksum`, `is_valid=1`, etc. `session.flush()` asigna `checkpoint.id`. Actualiza `WorldRow` (`last_checkpoint_at`, `months_simulated`, `checkpoint_count`) en **misma transacción** (líneas 283-298). `commit()` al final (línea 299). | 239-300 |

**Estructura `WorldCheckpoint` (db.py:137-166):** `id` (PK AUTOINCREMENT), `world_id`, `sequence`, `month`, `checkpoint_type`, `payload` (LargeBinary, serialización completa), `checksum`, `created_at`, `is_valid`, `checkpoint_version`. **No hay columna `batch_token`**. Único: `(world_id, sequence)`.

### 3.5 Recuperación — `src/worldsim/server/recovery_manager.py`

| Símbolo | Hallazgo | Líneas |
|---|---|---|
| `RecoveryManager.recover(world_id)` | Lee `WorldRow.status`; si no `running`/`paused` → `not_recoverable`. Obtiene checkpoints válidos (`is_valid=1`) ordenados por `sequence DESC`. Evalúa cada uno: checksum, versión, reconstrucción semántica (`engine_from_checkpoint`). El primero que reconstruya OK devuelve `RecoveryResult(status="ok", engine=..., checkpoint_sequence, checkpoint_month, ...)`. Si todos fallan → `not_recoverable`. | 85-193 |
| `_evaluate_checkpoint(cp)` | Valida checksum, decodifica JSON, verifica versión, llama `engine_from_checkpoint(state)`. En fallo de corrupción → `_RecoverableCheckpointError` → `mark_checkpoint_invalid(cp.id)` y prueba el anterior. | 199-256 |
| `engine_from_checkpoint()` | Reconstruye `SimEngine` completo desde JSON. Restaura `chronicle.events` completo, `_next_event_id` = `max(event.id)+1` si el guardado es inconsistente. | checkpoint.py:918-1067 |

**Conclusión:** La recuperación restaura el **estado completo** del motor desde un checkpoint válido. No hay extracción incremental de eventos ni verificación de cobertura histórica.

### 3.6 Endpoint de Mundos — `src/worldsim/server/app.py`

| Símbolo | Hallazgo | Líneas |
|---|---|---|
| `GET /api/worlds` | Devuelve `{worlds: [...], next_cursor: int}` con paginación keyset por `WorldRow.id DESC`. **No hay endpoint `/events`**. | 323-435 |
| `WebSocket /api/ws/{world_id}` | Owner: stream completo (`_tick_payload` incluye `eng.chronicle.recent(40)`). Terceros en mundo público: **una sola respuesta** con `_public_detail` (sin events). No hay consulta histórica paginada. | 998-1186 |

---

## Resumen de Evidencia

| Capa | Qué existe | Qué falta (según contrato) |
|---|---|---|
| **Modelo** | `Chronicle` acumulativo en memoria, `Event.id` efímero | Identidad persistente `(world_id, checkpoint_id, posición)`; extracción delta |
| **Motor** | `tick()` genera eventos → `Chronicle.log()` | Separación explícita frontera captura/persistencia (DEC-F7C-02) |
| **Persistencia** | Checkpoint atómico (engine completo + WorldRow) en una transacción | `batch_token` generado *antes* de transacción; verificación idempotencia; tabla operaciones |
| **Recuperación** | Restaura primer checkpoint válido completo | Cobertura histórica (`confirmed`/`potentially_incomplete`/`unknown`); verificación no-reescritura |
| **API** | `GET /api/worlds` paginado (mundos), WS stream (owner) | `GET /api/worlds/{id}/events` con cursor compuesto, filtros, proyección allowlist, cobertura |

---

*Fin de Parte 1. Continúa en Parte 2: Ciclo Real de Operación y Protocolo de Identidad Durable.*