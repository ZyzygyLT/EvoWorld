# DECISIONS.md — Decisiones arquitectónicas aprobadas de EvoWorld

Consolidación de DEC01–DEC21 y DEC-F7B-01…DEC-F7C-19 (F7-C-R1-I, 2026-11-16: arquitectura objetivo B — registro incremental persistente + diario durable de operaciones + checkpoints para recuperación). Cada entrada registra: identificador, decisión aprobada, contrato resultante, restricciones de implementación, estado y origen.

**Origen documental:** los textos originales de las decisiones no están en este repositorio (no existe un archivo de decisiones previo). Lo que se registra aquí se ha reconstruido a partir de las referencias vivas en el código y las pruebas (`grep DECxx`). Cuando un detalle no es recuperable se marca **pendiente de reconstrucción documental**; no se inventan detalles. **Excepción:** DEC20–DEC21 provienen directamente del encargo aprobado de F7-A (instrucciones del usuario, 2026-11-15), no de reconstrucción.

**Convención de estado:**
- `Aprobada / Implementada` — decisión aprobada y con implementación en el código.
- `Aprobada / Pendiente de implementación` — decisión aprobada, sin implementación aún.
- `Revisada` — la decisión fue modificada formalmente (ver DEC19).

---

## DEC01 — Ciclo de vida de RecoveryCoordinator
- **Decisión:** `RecoveryCoordinator` es el dueño del proceso de recuperación y del ciclo de vida del startup (no de la reconstrucción de motores: eso es `RecoveryManager`, ni de la persistencia: eso es `PersistenceService`).
- **Contrato:** máquina de estados `NOT_STARTED → IN_PROGRESS → COMPLETED|FAILED`, `COMPLETED → SHUTTING_DOWN → CLOSED|CLOSE_INCOMPLETE`; `FAILED → NOT_STARTED` solo tras rollback limpio con recursos originales verificados. `CLOSED` y `CLOSE_INCOMPLETE` son terminales. Transiciones solo vía métodos del propio coordinador (§4).
- **Restricciones:** rollback ante fallo o cancelación de las etapas A/B: cancelar y esperar tareas, drenar auxiliares, desregistrar, cerrar solo recursos de propiedad exclusiva (`owns_persistence=True`), acumular errores de cleanup, terminar en `FAILED` re-propagando la excepción original; cleanup protegido con `asyncio.shield`. Sin locks: startup corre en el event loop único; guard de estado rechaza segunda ejecución.
- **Estado:** Aprobada / Implementada.
- **Origen:** `src/worldsim/server/recovery_coordinator.py` (docstring, `CoordinatorState`, `_TRANSITIONS`, `_rollback_guarded`); tests `tests/test_recovery_coordinator.py`.

## DEC02 — Startup sin efectos secundarios de importación
- **Decisión:** importar módulos del servidor no crea ni modifica el esquema de base de datos; `db.ensure_schema()` se llama de forma explícita durante el startup, antes que cualquier consulta o recuperación.
- **Contrato:** `import worldsim.server.db` (y el resto de módulos) es libre de efectos sobre SQLite; el esquema se garantiza explícitamente en el arranque.
- **Restricciones:** prohibido crear/migrar esquema en import-time; toda inicialización ocurre en el lifespan/startup.
- **Estado:** Aprobada / Implementada.
- **Origen:** `src/worldsim/server/db.py:164,218`; `recovery_coordinator.py` (startup); `app.py` lifespan; test en `test_recovery_coordinator.py:1615`.

## DEC03 — (sin referencias en el repositorio)
- **Decisión:** pendiente de reconstrucción documental.
- **Contrato:** pendiente de reconstrucción documental.
- **Restricciones:** pendiente de reconstrucción documental.
- **Estado:** Aprobada (origen documental no disponible) / Detalle pendiente.
- **Origen:** ninguna referencia `DEC03` en el código o tests.

## DEC04 — Resultados `not_recoverable` solo en memoria
- **Decisión:** los mundos con resultado `not_recoverable`/`no_checkpoints` se registran como resultado operativo en memoria, sin crear runner ni motor sustituto.
- **Contrato:** `WorldRow.status` no adquiere valores nuevos por este motivo; los resultados viven solo en `RecoveryCoordinator.not_recoverable` (por instancia, sin persistir); una instancia nueva vuelve a evaluar la recuperación en su startup. La consulta HTTP 409/503 sobre estos resultados corresponde a la Etapa 5.
- **Restricciones:** prohibido persistir el resultado, prohibido crear runner sustituto, prohibido inventar motor.
- **Estado:** Aprobada / Implementada (la consulta HTTP 409/503 es Etapa 5, pendiente).
- **Origen:** `recovery_coordinator.py:15,40-44,143`.

## DEC05 — (sin referencias en el repositorio)
- **Decisión:** pendiente de reconstrucción documental.
- **Contrato:** pendiente de reconstrucción documental.
- **Restricciones:** pendiente de reconstrucción documental.
- **Estado:** Aprobada (origen documental no disponible) / Detalle pendiente.
- **Origen:** ninguna referencia `DEC05` en el código o tests.

## DEC06 — Startup en dos etapas y runners pausados
- **Decisión:** el startup ocurre en dos etapas secuenciales: Etapa A (recuperar y registrar, sin iniciar nada) y Etapa B (activar: iniciar tareas, solo si A terminó por completo). Los runners pausados se recuperan, registran e inician su tarea con `runner.paused=True`.
- **Contrato:** Etapa A: por cada mundo → `RecoveryManager.recover()` → si `ok`, `WorldRunner(engine=EXACTO)` → `register_existing()` → sin `start()`. Fallos individuales se aislan; una excepción de infraestructura aborta y dispara rollback. Etapa B: iniciar TODOS los runners (`running` y `paused`); verificar tareas vivas, runners registrados y ningún pausado sin pausa → `COMPLETED`. Un runner pausado tiene tarea principal viva y `_run` hace `continue` SIN tick. `_run` sigue siendo la única ruta de ticks y comprueba la bandera antes de cada tanda.
- **Restricciones:** prohibido crear una segunda ruta de ticks; prohibido iniciar nada antes de completar la Etapa A; la reanudación nunca llama a `start()` ni crea tareas.
- **Estado:** Aprobada / Implementada (Etapa 1B + corrección 1B-R1).
- **Origen:** `recovery_coordinator.py:9-28,284,470,716`; `test_recovery_coordinator.py:690,1046`.

## DEC07 — FastAPI como extra opcional
- **Decisión:** el servidor FastAPI (y `httpx` para `TestClient`) es un extra opcional; el código y los tests del dominio no dependen de él para ejecutarse.
- **Contrato:** los módulos de dominio (runner, recovery, transiciones, eliminación, snapshots) son importables y testeables sin FastAPI; los tests de integración usan `pytest.importorskip("fastapi"/"httpx")`.
- **Restricciones:** prohibido introducir dependencias de FastAPI en módulos de dominio; `app.py` no es importable sin el extra.
- **Estado:** Aprobada / Implementada.
- **Origen:** `test_runner_foundation.py:6`; `test_runner_close.py:7`; `test_world_deletion.py:7`; `test_pause_transitions.py:10`; `test_recovery_coordinator.py:1640-1641`.

## DEC08 — (Etapa 4, sin texto original recuperable)
- **Decisión:** referenciada junto a DEC14/DEC15 como base de las transiciones de pausa/reanudación de la Etapa 4. El texto original no está disponible: **pendiente de reconstrucción documental**.
- **Contrato:** los contratos operativos verificables de la Etapa 4 están descritos en DEC14/DEC15 y en CONTRACTS.md (sección Pausa y reanudación). No se atribuyen a DEC08 detalles no recuperables.
- **Restricciones:** pendiente de reconstrucción documental.
- **Estado:** Aprobada / Implementada (Etapa 4) / Detalle pendiente.
- **Origen:** `transitions.py:1`; `test_pause_transitions.py:1`.

## DEC09 — WebSocket sin ejecución de ticks
- **Decisión:** el handler WebSocket (`ws_world` en `app.py`) NUNCA avanza la simulación. La única ruta ordinaria de ticks es `WorldRunner._run()`. Se eliminan las llamadas directas a `runner.engine.tick()` y el fast-forward (`for _ in range(row.months_simulated): runner.eng.tick()`) del handler WS. No se sustituye el fast-forward por ningún otro mecanismo de ticks iniciado desde el handler.
- **Contrato:** el WS conserva los mensajes válidos del protocolo (`speed`, `pause`, `selected`), la suscripción/notificación, la pausa y reanudación vía `pause_world`/`resume_world` de Etapa 4 (DEC14), y la limpieza de suscriptores ante desconexiones y cancelaciones (`CancelledError`/`WebSocketDisconnect` → `unsubscribe`). Antes de tocar la fila o crear runner, aplica DEC16: mundo inexistente → 404 (cierra WS 4404); mundo con registro `not_recoverable` → 409 (cierra WS 4409) y NO se crea runner lazy. Se inicializa `runner = None` antes del `try` para evitar `NameError` en las ramas de cancelación/desconexión. Los mensajes WS inválidos (no-JSON, no-dict, `speed`/`selected` no enteros) cierran con 4400; fallo de infraestructura cierra con 4503 y se registra.
- **Restricciones:** ninguna ruta HTTP/WS ejecuta ticks fuera de `WorldRunner._run()`. `create_world` (POST) y `get_kingdom_stats` (GET stats) conservan `eng.tick()` como rutas HTTP de reconstrucción/consulta, fuera del alcance de DEC09. No se crea runner sustituto para mundos no recuperables.
- **Estado:** Aprobada / Implementada (Etapa 5).
- **Origen:** `app.py` (`ws_world`, handlers de excepción, `_require_recoverable`); `src/worldsim/server/errors.py`; `tests/test_etapa5_static.py`, `tests/test_etapa5_http.py`.

## DEC10 — Eliminación segura de mundos
- **Decisión:** la eliminación de un mundo cierra su runner, verifica el cierre, lo retira del registro y SOLO DESPUÉS borra los datos persistidos.
- **Contrato:** secuencia `close → verificar → unregister → borrar datos` en `safe_delete_world()` (§8). Coordinación explícita por mundo en `RunnerRegistry`: `begin_delete()` (marca síncrona + lock FIFO por mundo) / `end_delete()` / `is_deleting()`. Durante la sección crítica, `get()` y `register_existing()` rechazan el mundo. La marca se libera en `finally` o, si el caller es cancelado con la escritura persistida aún en el worker thread, se transfiere al callback de finalización de esa tarea (§9). `unregister` nunca cierra implícitamente.
- **Restricciones:** prohibido borrar datos antes de verificar el cierre; prohibido lock global (solo por mundo); el registry no es dueño implícito de `close()`.
- **Estado:** Aprobada / Implementada (Etapa 2).
- **Origen:** `runner.py:808-859,617-660`; `app.py:388`; `test_world_deletion.py`.

## DEC11 — Contrato de RunnerRegistry
- **Decisión:** `RunnerRegistry` es el singleton `world_id → WorldRunner` por proceso, con API explícita y sin creación implícita peligrosa.
- **Contrato:** `register_existing()` registra un runner ya construido, rechaza duplicados (nunca sustituye silenciosamente), valida asociación `world_id`/runner, no lo inicia y rechaza mundos en eliminación. `unregister()` solo remueve. `get_existing()` nunca crea. `get()` conserva la creación perezosa para flujos que la necesitan y rechaza mundos en eliminación. Métodos síncronos = atómicos en el event loop único (sin locks propios).
- **Restricciones:** prohibido iniciar runners desde el registro; prohibido cerrar recursos desde `unregister`.
- **Estado:** Aprobada / Implementada (Etapa 1A, ampliada con DEC10 en Etapa 2).
- **Origen:** `runner.py:617-729`; `test_runner_foundation.py` (tests 1-9).

## DEC12 — Identidad del motor en WorldRunner
- **Decisión:** cada `WorldRunner` posee LA instancia única de `SimEngine` de su mundo; los espectadores se suscriben a su canal de broadcast.
- **Contrato:** el motor se asigna en construcción, antes de cualquier tarea; una vez comenzada la ejecución (`start()`), el motor NO se sustituye (asignarlo antes de `start()` sigue permitido — flujo POST de `app.py`). En la recuperación se inyecta EXACTAMENTE la instancia recuperada, nunca reconstruida.
- **Restricciones:** prohibido reconstruir o sustituir el motor en ejecución; prohibido crear motores sustitutos en recuperación.
- **Estado:** Aprobada / Implementada (Etapa 1A).
- **Origen:** `runner.py:73-136`; `recovery_coordinator.py:12,441`; `test_runner_foundation.py:246,266`; `test_recovery_coordinator.py:666`.

## DEC13 — Contrato de snapshots
- **Decisión:** los snapshots son independientes, profundamente inmutables y canónicos.
- **Contrato:** copia vía `checkpoint_to_dict` (contrato público de checkpoints) y congelación en estructuras inmutables: independencia (sin referencias mutables ni de origen ni expuestas), inmutabilidad profunda (`MappingProxyType`, tuples), serialización canónica byte a byte idéntica a `checkpoint_to_json` (UTF-8, `ensure_ascii=False`, `indent=2`, separadores exactos, `allow_nan=False`, orden de inserción), errores explícitos `SnapshotError` con la ruta del dato (nunca descartes silenciosos), consistencia por captura síncrona única en el hilo propietario (opcionalmente con lock proporcionado por el llamador, sostenido SOLO durante la copia). No importa SQLAlchemy ni `worldsim.server`.
- **Restricciones:** prohibido mutar el motor durante la captura sin lock; prohibida sincronización nueva; el módulo es puro cómputo en memoria.
- **Estado:** Aprobada / Implementada (Etapa 3).
- **Origen:** `src/worldsim/export/snapshot.py` (docstring §3.1-§3.4); `tests/test_snapshot.py`.

## DEC14 — Estado persistido de pausa: `WorldRow.status` es la autoridad
- **Decisión:** en una pausa/reanudación por mundo, `WorldRow.status` es el estado persistido de referencia; `WorldRunner.paused` es el control operativo; `SimEngine.paused` es SOLO un espejo, nunca autoridad independiente.
- **Contrato:** la transición valida y escribe en un worker thread sobre el contrato público de `WorldRepository`; el espejo del engine se actualiza solo para coincidir (`runner.eng.paused = value # espejo, nunca autoridad`); el estado persistido distinto no se toca fuera de la transición (la recuperación no escribe filas).
- **Restricciones:** prohibido usar `SimEngine.paused` como autoridad; prohibido escribir `WorldRow.status` fuera de la transición (incluida la recuperación); el formato de checkpoint, DEC13, DEC19, DEC06, DEC18 y DEC10 no se tocan.
- **Estado:** Aprobada / Implementada (Etapa 4).
- **Origen:** `transitions.py:6,379,582`; `recovery_coordinator.py:444`; `app.py:686`.

## DEC15 — (Etapa 4, sin texto original recuperable)
- **Decisión:** referenciada junto a DEC08/DEC14 como base de las transiciones de pausa/reanudación. El texto original no está disponible: **pendiente de reconstrucción documental**. Los aspectos verificables de la Etapa 4 (exclusión por mundo, generación/identidad, escritura persistida, cancelación y reconciliación, eliminación y cierre, sin segunda ruta de ticks) están descritos en CONTRACTS.md y en el docstring de `transitions.py`.
- **Contrato:** ver CONTRACTS.md (Pausa y reanudación). No se atribuyen a DEC15 detalles no recuperables.
- **Restricciones:** pendiente de reconstrucción documental.
- **Estado:** Aprobada / Implementada (Etapa 4) / Detalle pendiente.
- **Origen:** `transitions.py:1`; `test_pause_transitions.py:1`.

## DEC16 — Clasificación explícita de errores HTTP
- **Decisión:** los errores se clasifican explícitamente en lugar de convertirse indiscriminadamente en 404/409/500: mundo inexistente → 404; mundo existente con resultado `not_recoverable` del `RecoveryCoordinator` → 409; parámetros inválidos → 400; fallo temporal de infraestructura → 503. En WebSocket los códigos se traducen a cierre (`4400 + status - 400`: 404→4404, 409→4409, 400→4400, 503→4503).
- **Contrato:** `RequestValidationError` → 400 JSON (reemplaza el 422 por defecto; ningún test asiente 422). `OperationalError`/`InterfaceError` → 503 con el error registrado mediante `logger.error` (nunca ocultado, regla 8). `WorldRow.status` sigue siendo la fuente persistente de estado; `not_recoverable` es un resultado operativo del coordinador en memoria por instancia de proceso, NUNCA un valor de `WorldRow.status` (DEC08). DELETE está exento del 409 (DEC10: la eliminación segura debe funcionar para mundos no recuperables) y descarta el registro tras borrado confirmado (`_forget_recoverable`) para no heredar un 409 stale al reusar `world_id`.
- **Restricciones:** no persistir `not_recoverable` en `WorldRow.status`; no cambiar la lógica de recuperación 1A/1B; no crear runners sustitutos ni rutas HTTP/WS que evadan el resultado de recuperación; los errores de infraestructura se registran y no se presentan como éxito. Las respuestas/mensajes existentes se preservan cuando son compatibles con DEC16.
- **Estado:** Aprobada / Implementada (Etapa 5).
- **Origen:** `src/worldsim/server/errors.py` (módulo puro sin FastAPI, DEC07-testable: `is_infra_error`, `ws_close_code`, `not_recoverable_reason`, `forget_not_recoverable`); `app.py` (handlers de excepción y guardas `_require_recoverable`/`_forget_recoverable` en `post_prediction`/`post_chat`/`post_claim`/`delete_world`/`ws_world`); `tests/test_etapa5_static.py`, `tests/test_etapa5_http.py`.

## DEC17 — Shutdown global (drenaje y cierre verificados)
- **Decisión:** el shutdown global drena y cierra todos los runners de forma verificada, terminando en `CLOSED`.
- **Contrato:** el coordinador define la máquina de estados y los métodos de transición (`SHUTTING_DOWN → CLOSED|CLOSE_INCOMPLETE`). La Etapa 6 implementa la secuencia real de cierre en el `lifespan` de FastAPI: `transition_coordinator.shutdown()` (drenaje de transiciones Etapa 4) seguido de `RecoveryCoordinator.shutdown()` (cierre de todos los runners vía `WorldRunner.close()` DEC18, drenaje de writes de persistencia sin checkpoint final automático DEC05, cierre de `PersistenceService` si es dueño exclusivo). `SHUTTING_DOWN → CLOSED` solo si todo cerró limpio; `→ CLOSE_INCOMPLETE` si hubo cualquier error o timeout, nunca presentado como éxito. Los errores se acumulan y nunca se silencian. Una cancelación no abandona tareas de cierre en curso.
- **Restricciones:** prohibido implementar el drain/close global fuera de la Etapa 6; prohibido abandonar escrituras en curso; no generar checkpoint final automático (DEC05); no reiniciar runners terminales (DEC18).
- **Estado:** Aprobada / Implementada (Etapa 6).
- **Origen:** `recovery_coordinator.py` (`CoordinatorState`, `begin_shutdown`, `finish_shutdown`, `shutdown`); `runner.py` (`RunnerRegistry.all_runners`, `WorldRunner.close`); `app.py` (`lifespan` finally); `tests/test_shutdown.py`.

## DEC18 — Contrato de `WorldRunner.close()`
- **Decisión:** `close()` es definitivo, idempotente y seguro ante concurrencia; drena de verdad las tareas principal y auxiliares.
- **Contrato:** máquina de estados `NOT_STARTED → RUNNING → CLOSING → CLOSED|CLOSE_INCOMPLETE`. `CLOSING` se fija de forma síncrona antes del primer `await`. `CLOSED` y `CLOSE_INCOMPLETE` son terminales: un runner en esos estados nunca vuelve a ejecutar ticks ni se reinicia. Timeout de drenaje por grupo de tareas (`CLOSE_TIMEOUT = 5.0`); un close que lo excede es `CLOSE_INCOMPLETE` (nunca se presenta como `CLOSED`) y conserva referencias y evidencia. En eliminación, `safe_delete_world` es responsable del close verificado.
- **Restricciones:** prohibido reiniciar un runner terminal; prohibido presentar `CLOSE_INCOMPLETE` como `CLOSED`.
- **Estado:** Aprobada / Implementada (Etapa 2).
- **Origen:** `runner.py:9-60,279-308`; `test_runner_close.py`.

## DEC19 — Presupuesto de rendimiento de captura de snapshot (REVISADA)
- **Decisión (revisión aprobada):**
  - **Mediana** de captura completa (`capture_snapshot()`: copia + congelación) **≤10 ms: obligatoria.**
  - **P95** de captura completa **≤10 ms: obligatorio.**
  - **Máximo observado: métrica obligatoria de diagnóstico, sin límite absoluto.**
  - El **requisito original de máximo absoluto ≤10 ms fue retirado expresamente.**
- **Contrato:** el presupuesto se evalúa sobre `capture_snapshot()` COMPLETO en un escenario comparable al original; determinismo byte a byte como única aserción adicional. El máximo se registra siempre en el output de diagnóstico.
- **Restricciones:** prohibido usar el máximo como criterio de aceptación; prohibido omitir la medición del máximo.
- **Estado:** Revisada / Implementada con **divergencia pendiente**: `tests/test_snapshot_perf.py:264` aún aserciona `capture_max <= 10.0` ms (más estricto que la revisión aprobada). Corregir el test queda **pendiente** y requiere autorización de etapa; no se modificó en esta intervención.
- **Origen:** revisión aprobada por el usuario (intervención de memoria arquitectónica); estado actual del test: `tests/test_snapshot_perf.py:50,257-268`.

## DEC20 — Visibilidad de mundos `private`/`public` (F7-A)
- **Decisión:** cada mundo tendrá un atributo de visibilidad con exactamente dos valores, `private` y `public`. Los mundos nuevos son `private` por defecto y su creación **exige autenticación**; todo mundo nuevo tendrá un propietario identificado mediante la sesión autenticada (`owner_user_id` = `user_id` del propietario), por lo que **no se crearán mundos privados sin propietario**. Los mundos existentes conservan inicialmente su disponibilidad pública para no romper comportamientos y enlaces existentes; esa compatibilidad es transitoria y sus propietarios deberán poder retirarlos de publicación. Publicar o retirar un mundo es una operación exclusiva de su propietario autenticado. La implementación futura deberá adaptar los flujos y pruebas que dependan de creación anónima; no se eliminará funcionalidad existente sin identificar y documentar su impacto.
- **Contrato:** toda operación que requiera identidad responde **401** sin autenticación y **403** con autenticación de no propietario; la consulta de un mundo privado por un tercero responde **404** sin revelar su existencia, en todas las rutas del mundo (incluidas rutas alternativas y WebSocket). Los datos públicos usan una representación acotada con campos explícitamente permitidos y **nunca** sirven `state_json` completo. La migración asigna `public` explícito a los registros preexistentes; un valor ausente o inválido se trata como `private` (**fail-closed**: nunca publicación accidental) y toda creación posterior escribe `private`. En concurrencia, la visibilidad se lee por solicitud desde la fila persistida (sin caché de decisiones) y el retiro es una escritura atómica. Regla completa verificable: `CONTRACTS.md` §14.
- **Restricciones:** prohibido cambiar visibilidad sin comprobar propiedad; prohibido exponer mundos privados mediante listados, búsquedas o endpoints alternativos; prohibido exponer `state_json` completo en datos públicos; prohibido confundir autenticación con autorización; prohibido usar `user_token` u otros identificadores no confiables como prueba de propiedad; prohibido publicación accidental por valores predeterminado o migración incompleta. Los criterios de selección de mundos destacados quedan **fuera de F7-A** y requerirán decisión posterior. No se reabren las Etapas 0–6: la visibilidad no modifica `WorldRow.status` (DEC14), la eliminación segura (DEC10), los snapshots (DEC13), el shutdown (DEC17) ni la recuperación; la clasificación de errores (DEC16) se conserva salvo la precedencia de no-revelación 404 documentada en `CONTRACTS.md` §14.3.
- **Estado:** Aprobada / **Implementada y verificada** (F7-A implementación, 2026-11-16). Columna `visibility` + CHECK + migración en `src/worldsim/server/db.py` (`ensure_schema`); rutas y compuerta de visibilidad en `src/worldsim/server/app.py`; pruebas `tests/test_visibility.py` (30) y suites Etapa 5/auth/server/multiplayer/league adaptadas. Regresión global: 473 passed / 1 skipped / 2 fallos preexistentes (sin fallos nuevos).
- **Origen:** encargo "F7-A: Formalización contractual de visibilidad" (decisiones 1–8 y 10 del usuario, 2026-11-15); precisión P-1 incorporada en el encargo F7-A-R1 (2026-11-15); contrato completo en `docs/architecture/CONTRACTS.md` §14; estado en `docs/implementation/IMPLEMENTATION_STATUS.md`.

## DEC21 — Identidad confiable para resultados competitivos (F7)
- **Decisión:** los rankings globales y los desafíos que atribuyan resultados requerirán una identidad vinculada a una cuenta; los `user_token` no se consideran identidad competitiva confiable.
- **Contrato:** cualquier endpoint futuro de ámbito global que asigne puntos, posiciones o resultados de desafío debe autenticar al usuario con cuenta (mecanismo Bearer existente) y persistir el vínculo con `user_id`. Los `user_token` siguen siendo válidos exclusivamente para los mecanismos por mundo ya aceptados (chat, predictions, claims/standings), que F7-A no modifica.
- **Restricciones:** prohibido basar rankings globales o resultados de desafíos en `user_token`; prohibido cambiar los mecanismos por mundo existentes (contratos de etapas aceptadas) sin evidencia de defecto; su implementación pertenece a futuras subetapas (F7-E/F7-F), **no** a F7-A.
- **Estado:** Aprobada / **parcialmente implementada en F7-A** (2026-11-16): identidad de cuenta vinculada en los puntos de atribución existentes (`user_id` de sesión en claims; `user_token` solo como metadato de exhibición, nunca como identidad), verificado en `tests/test_visibility.py` (tests DEC21). La integración administrativa y pública de mundos destacados se implementa en F7-E.1–F7-E.4. Los rankings globales y otras funciones competitivas globales permanecen fuera de ese alcance y requieren sus subetapas correspondientes.
- **Origen:** encargo "F7-A: Formalización contractual de visibilidad" (decisión 9 del usuario, 2026-11-15); detalle en `CONTRACTS.md` §14.7.

---

## DEC-F7B-01 — Separación entre galería pública y mundos propios (F7-B)

- **Decisión:** `GET /api/worlds` define dos modos de consulta independientes: modo público (predeterminado, sin `mine=1`) y modo propietario (`mine=1`). El modo público devuelve exclusivamente mundos con `visibility = public`, disponible para visitantes anónimos y usuarios autenticados. El modo propietario requiere sesión autenticada y devuelve exclusivamente mundos cuyo `owner_user_id` coincide con el `user_id` de la sesión, pudiendo incluir mundos públicos y privados propios. Un mundo propio público puede aparecer tanto en la galería pública como en el listado de mundos propios, sin duplicar su identidad ni crear registros adicionales.
- **Contrato:** `GET /api/worlds` (modo público) sin autenticación devuelve solo mundos paginados con `visibility = public`. `GET /api/worlds?mine=1` (modo propietario) requiere sesión (401 sin ella) y devuelve solo mundos del propietario, paginados, pudiendo ser `private` o `public`. Ningún modo expone mundos privados de terceros. Ambos modos comparten el formato de respuesta paginado, pero mantienen consultas y condiciones de acceso separadas.
- **Restricciones:** la galería no modifica `WorldRow.visibility` ni `WorldRow.status`; no crea registros adicionales; no altera el comportamiento de `?mine=1`; `user_token` nunca es prueba de propiedad.
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1-A corrección de compatibilidad de `?mine=1` (2026-11-16).

## DEC-F7B-02 — Orden predeterminado del listado de galería (F7-B)

- **Decisión:** el orden predeterminado es mundos más recientes primero, usando `id DESC` como criterio estable (desempate implícito por `id`, que es monotónico y único). `created_at` no se usa como criterio principal porque es un campo `String` sin garantía de precisión, unicidad ni formato canónico en todas las filas preexistentes.
- **Contrato:** `GET /api/worlds` aplica `ORDER BY id DESC` en la consulta de persistencia. No se exponen órdenes alternativos configurables por el cliente en F7-B.
- **Restricciones:** no se añaden parámetros de ordenamiento al cliente; el orden es fijo y determinista.
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7B-03 — Paginación por cursor/keyset en la galería (F7-B)

- **Decisión:** `GET /api/worlds` adopta paginación por cursor basada en `id DESC`. Los parámetros son `limit` (tamaño de página) y `cursor` (punto de corte en el orden `id DESC`). La respuesta es un objeto paginado, no una lista simple.
- **Contrato:**
  - `limit`: entero opcional, predeterminado 20, máximo 100. Valores fuera de rango → 400.
  - `cursor`: entero opcional, representa exclusivamente un punto de corte en el orden `id DESC`. Si se proporciona, la consulta filtra `id < cursor` (para orden `id DESC`) junto con todos los demás filtros y la condición obligatoria de visibilidad. El cursor no requiere que el registro correspondiente exista, ni que sea público, ni que sea visible para el solicitante. Un cursor entero válido se acepta aunque el registro que originó ese valor haya sido eliminado o cambiado a privado. La consulta nunca busca primero el registro del cursor para comprobar su existencia o visibilidad.
  - Respuesta: `{"worlds": [...], "next_cursor": <int|null>}`. `next_cursor` es el `id` del último mundo entregado en la página actual si hay más resultados, o `null` si se alcanzó el final.
  - El cursor filtra en la consulta SQL, por lo que nunca expone mundos privados ni saltea condiciones de visibilidad.
  - No se devuelve un total global de mundos (evita consultas de conteo innecesarias).
- **Restricciones:** el cursor es un entero simple, no un token opaco ni firmado. Un cursor inválido (no entero) → 400. Un cursor alterado que siga siendo un entero válido no constituye una vulneración de seguridad: solo cambia el punto de corte. La privacidad se garantiza mediante los filtros SQL, no mediante la confidencialidad del cursor. No reutilizar un cursor de una consulta con parámetros distintos (`mine`, `search`, `mode`, `limit`).
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py`.
- **Origen:** F7-B-R1-A corrección de semántica del cursor (2026-11-16).

## DEC-F7B-04 — Búsqueda por nombre en la galería (F7-B)

- **Decisión:** la galería acepta búsqueda textual sencilla por nombre mediante el parámetro `search`.
- **Contrato:**
  - `search`: cadena opcional, longitud máxima 64 caracteres. Valores más largos → 400.
  - La búsqueda es insensible a mayúsculas/minúsculas usando `LOWER(name) LIKE LOWER(:search)` con `%` envolviendo el término.
  - Caracteres comodín SQL (`%`, `_`) y el carácter de escape se escapan correctamente antes de construir el patrón.
  - `search` vacío o solo espacios se trata como sin búsqueda.
  - Se combina con `visibility = public`, `cursor`, `limit` y `mode`.
- **Restricciones:** no se busca por descripción, etiquetas, propietario, seed ni otros campos. No se permiten patrones arbitrarios sin escapado.
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7B-05 — Filtro por modo en la galería (F7-B)

- **Decisión:** la galería acepta filtro opcional por modo mediante el parámetro `mode`.
- **Contrato:**
  - `mode`: cadena opcional, debe ser `single` o `league` (valores del dominio existente en `WorldRow.mode`). Cualquier otro valor → 400.
  - La ausencia del filtro devuelve todos los modos públicos.
  - Se combina con `search`, `visibility = public`, `cursor` y `limit`.
- **Restricciones:** no se crean nuevos modos ni se modifican semánticas de modos existentes.
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7B-06 — Información visible en la tarjeta de galería (F7-B)

- **Decisión:** la tarjeta de galería consume exclusivamente la allowlist pública cerrada de DEC20 (§14.4): `id`, `name`, `mode`, `seed`, `months_simulated`, `land_pct`, `created_at`, `season_years`, `season_over`, `visibility`, `mine`. Los campos `mine` y `visibility` se derivan de la identidad del solicitante y del valor persistido, respectivamente.
- **Contrato:** `GET /api/worlds` devuelve una lista de fichas públicas construidas con `_public_ficha` (allowlist explícita, nunca serialización del modelo con eliminación de campos). `mine` es `true` solo cuando el solicitante es el propietario autenticado; en galería pública anónima siempre es `false`.
- **Restricciones:** no se amplía la exposición pública por conveniencia del frontend. No se muestran datos personales del propietario (`owner_user_id`, `username`) sin contrato explícito que los autorice. No se expone `state_json` ni campos de infraestructura.
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7B-07 — Ficha de detalle público en la galería (F7-B)

- **Decisión:** la galería enlaza con el detalle público existente (`GET /api/worlds/{id}`) que devuelve la ficha pública más el bloque de render del mapa base `world: {width, height, biome, elevation, rivers}`.
- **Contrato:** el detalle público conserva la representación acotada de DEC20. Nunca usa endpoints auxiliares (stats, chat, claims, standings, predictions, leaderboard) como mecanismo indirecto para obtener información completa. El WS no se usa para obtener datos de mundos ajenos.
- **Restricciones:** no se modifica el contrato de detalle público salvo contradicción documental real. Los datos vivos (reinos, influencia, guerras, eventos, bestias, cronología) solo llegan por WS al propietario conectado (Etapa 5/DEC09).
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7B-08 — Acceso anónimo y autenticado a la galería (F7-B)

- **Decisión:** la galería pública puede consultarse sin iniciar sesión. El visitante anónimo recibe solo mundos públicos. El usuario autenticado que no es propietario recibe los mismos mundos públicos (más `mine=false`). El propietario recibe sus propios mundos en `?mine=1` (públicos o privados).
- **Contrato:** `GET /api/worlds` sin autenticación → 200 con mundos públicos. `GET /api/worlds/{id}` sin autenticación → 200 si es público, 404 si es privado o inexistente (indistinguible). Las operaciones reservadas al propietario mantienen sus controles de autorización (401/403/404 según DEC20 §14.3). La identidad se obtiene de la sesión Bearer, nunca de `user_token`.
- **Restricciones:** ningún acceso de galería permite obtener estado completo de un mundo privado. La galería no necesita autenticación para descubrir mundos públicos.
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7B-09 — WebSocket en la galería (F7-B)

- **Decisión:** la galería no se suscribe a simulaciones de otros usuarios. El descubrimiento y la ficha pública se realizan exclusivamente mediante HTTP. No se abre un stream completo de simulación para visitantes. No se crean runners ni se alteran ciclos de vida para satisfacer solicitudes de galería.
- **Contrato:** las reglas de DEC20 para WebSocket permanecen intactas: tercero en mundo privado → 4404; tercero en mundo público → un único mensaje `world` acotado, nunca suscrito al runner.
- **Restricciones:** no se modifica el contrato WebSocket existente.
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7B-10 — Experiencia de interfaz de la galería (F7-B)

- **Decisión:** la galería se accede desde la interfaz existente (pantalla inicial/lobby) como una vista separada de los mundos propios. Muestra tarjetas de mundos públicos con indicador de que son públicos, navegación entre páginas, acceso al detalle público, estados de carga, vacío (sin mundos públicos), vacío (sin resultados de búsqueda) y error de comunicación con posibilidad de reintento.
- **Contrato:** la propuesta aprovecha `StartScreen.jsx` y `App.jsx` existentes sin reestructuración amplia. No se implementan componentes ni se modifican estilos en R1.
- **Restricciones:** no se incorporan mundos destacados, rankings globales, desafíos, sistemas de recomendación, votaciones, likes, comentarios ni reputación. Estas funcionalidades pertenecen a etapas posteriores (F7-C a F7-F).
- **Estado:** Aprobada / Implementada y verificada (F7-B-I1/I2); implementación en `app.py` y frontend.
- **Origen:** F7-B-R1 definición contractual (2026-11-16).

## DEC-F7C-04 — Identidad de acontecimientos (F7-C) — **DESCARTADA** (F7-C-R1-I; suplantada por DEC-F7C-13)

> **Nota de trazabilidad (F7-C-R1-I, 2026-11-16):** esta DEC se conserva como registro histórico pero queda **descartada** por la resolución arquitectónica de F7-C-R1-I. La propuesta `(world_id, checkpoint_id, posición_dentro_del_checkpoint)` como identidad lógica de acontecimiento se descarta: identifica una aparición dentro de un checkpoint, no un acontecimiento lógico, no admite el orden total por operación y no sobrevive a la creación de un mundo divergente tras restauración. Decisión vigente: DEC-F7C-13.

- **Decisión (histórica, descartada):** cada acontecimiento persistido tiene un **identificador estable y único dentro de su mundo**, compuesto por la tupla (`world_id`, `checkpoint_id`, `posición_dentro_del_checkpoint`). `checkpoint_id` es la clave primaria de `WorldCheckpoint` (asignada por SQLite AUTOINCREMENT al escribir el checkpoint); `posición_dentro_del_checkpoint` es el índice secuencial del acontecimiento dentro de `Chronicle.events` serializado en ese checkpoint (0-based, orden de inserción). No se reutiliza `Event.id` de la crónica en memoria, porque ese ID es secuencial por instancia de `Chronicle`, se reinicia en cada nuevo proceso y no sobrevive restauraciones de checkpoint.
- **Contrato (histórico, descartado):** el identificador persistente se deriva en el momento de escribir el checkpoint: `event_id` persistente = (`world_id`, `checkpoint_id`, `índice_dentro_del_checkpoint`). Es monotonía dentro del mundo (los checkpoints tienen `sequence` creciente y los eventos dentro de cada checkpoint mantienen su orden de crónica). Sirve como clave de orden y deduplicación, y nunca cambia aunque el mundo se restaure desde checkpoint. Al restaurar un checkpoint, la frontera de captura lee `MAX(sequence)` de `world_checkpoints` para ese mundo y reinicia el contador local de captura desde ese valor + 1, evitando colisiones con acontecimientos ya persistidos.
- **Restricciones (históricas, descartadas):** no hay identidad trans-mundo global para acontecimientos. El orden estable se basa en (`world_id`, `checkpoint_id`, `índice_dentro_del_checkpoint`). Los acontecimientos anteriores a F7-C no tienen identificador persistente y se tratan como no importados. `Event.id` de la crónica en memoria es efímero y nunca se usa como clave persistente.
- **Estado:** Descartada / suplantada por DEC-F7C-13 (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-B corrección de identidad (2026-11-16); descartada por F7-C-R1-I (2026-11-16).

## DEC-F7C-05 — Orden temporal de acontecimientos (F7-C) — **DESCARTADA** (F7-C-R1-I; suplantada por DEC-F7C-14)

> **Nota de trazabilidad (F7-C-R1-I, 2026-11-16):** esta DEC se conserva como registro histórico pero queda **descartada** por la resolución arquitectónica de F7-C-R1-I. La propuesta de ordenar por (`checkpoint_id`, `posición_dentro_del_checkpoint`) como clave principal se descarta: la clave lógica debe ser (`operation_sequence`, `posición`) dentro de un checkpoint, no (`checkpoint_id`, `posición`). El orden total debe basarse en (`operation_sequence`, `posición`), no en `checkpoint_id`, para garantizar continuidad de operaciones tras restauración.
- **Decisión (histórica, descartada):** los acontecimientos se ordenan por (`checkpoint_id`, `posición_dentro_del_checkpoint`) como clave principal de orden estable y determinista. `checkpoint_id` refleja el orden de persistencia del checkpoint (monotonía creciente con `WorldCheckpoint.sequence`); `posición_dentro_del_checkpoint` refleja el orden de inserción del acontecimiento dentro de `Chronicle.events` en ese checkpoint. El mes simulado (`month`) es un atributo consultable, pero no la clave de orden principal.
- **Contrato (histórico, descartado):** la consulta de historia devuelve acontecimientos ordenados por `checkpoint_id` ASC y, dentro de cada checkpoint, por `posición_dentro_del_checkpoint` ASC. Varios acontecimientos del mismo mes y mismo tick mantienen su orden de inserción en la crónica. El cursor de paginación es un `checkpoint_id` válido (o tupla `checkpoint_id`,`posición`) y representa exclusivamente un punto de corte en este orden.
- **Restricciones (históricas, descartadas):** no se utiliza `WorldRow.created_at` como fecha histórica de acontecimientos. No existe orden trans-mundo global. No se asume que `event_id` de la crónica en memoria refleje el orden de ticks exacto (se reinicia por proceso). El orden es estable y no repetido: restaurar un checkpoint no reutiliza identificadores ya confirmados, porque el `checkpoint_id` del checkpoint restaurado ya fue consumido y los nuevos checkpoints obtienen `sequence` mayor.
- **Estado:** Descartada / suplantada por DEC-F7C-14 (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-B corrección de orden (2026-11-16); descartada por F7-C-R1-I (2026-11-16).

## DEC-F7C-06 — Tipos de acontecimientos compartibles (F7-C)

- **Decisión:** los 37 tipos de `EventType` no se exponen automáticamente. F7-C define una política explícita de publicación: los tipos compartibles públicamente se definen mediante allowlist; los demás son consultables solo por el propietario o excluidos.
- **Contrato:** la representación pública de un acontecimiento construye explícitamente los campos permitidos (tipo, mes, texto, IDs de civilizaciones involucradas, parámetros permitidos, clave de mensaje). Nunca se serializa el modelo completo y se eliminan campos después. Los tipos nuevos agregados en el futuro no se exponen por defecto.
- **Restricciones:** los campos `text`, `params` y `kingdom_ids` pueden contener información interna y se revisan antes de incluirse en la allowlist pública. No se amplía la allowlist sin una DEC nueva.
- **Estado:** Aprobada / Contrato definido (F7-C-R1); implementación no iniciada.
- **Origen:** F7-C-R1 definición contractual (2026-11-16).

## DEC-F7C-07 — Autorización de historia (F7-C)

- **Decisión:** la consulta de historia respeta la matriz de autorización de DEC20. La identidad se obtiene exclusivamente de la sesión autenticada (Bearer → `user_id` coincide con `WorldRow.owner_user_id`). Sin una sesión autenticada, el servidor no puede saber si el solicitante es el propietario, por lo que una solicitud anónima sobre un mundo privado se trata exactamente como una solicitud sobre un mundo inexistente: 404.
- **Contrato:**
  - Anónimo + mundo público: 200 con acontecimientos públicos (proyección pública).
  - Autenticado no propietario + mundo público: 200 con acontecimientos públicos (proyección pública).
  - Propietario autenticado + mundo público o privado: 200 con acontecimientos del mundo (proyección del propietario).
  - Tercero (autenticado o anónimo) + mundo privado: 404 (indistinguible de inexistente).
  - Mundo inexistente: 404.
  - Mundo no recuperable: la guarda DEC16 se aplica antes de consultar historia (409).
- **Restricciones:** ningún acceso de historia permite obtener `state_json` ni datos internos de un mundo privado. `user_token` nunca es prueba de identidad ni de propiedad. No se introduce publicación selectiva de historias sin definir sus implicaciones y justificar si pertenece realmente a F7-C.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-A); implementación no iniciada.
- **Origen:** F7-C-R1-A corrección de autorización (2026-11-16).

## DEC-F7C-08 — Historia anterior a F7-C (F7-C)

- **Decisión:** los acontecimientos generados antes de la implementación de F7-C **no se importan**. Solo se registran acontecimientos ocurridos después de habilitar F7-C.
- **Contrato:** no hay backfill de crónicas existentes desde checkpoints. Los acontecimientos anteriores carecen de identificador persistente y no son consultables a través del endpoint de historia.
- **Restricciones:** no se asume que la historia sea completa. La documentación debe indicar explícitamente que la historia comienza en la fecha de habilitación de F7-C y que la cobertura está limitada a los checkpoints confirmados después de esa fecha. Los acontecimientos entre el último checkpoint confirmado y el momento actual viven solo en memoria y pueden perderse si el proceso cae.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-B); implementación no iniciada.
- **Origen:** F7-C-R1-B corrección de cobertura histórica (2026-11-16).

## DEC-F7C-09 — Modelo de persistencia de acontecimientos (F7-C) — **DESCARTADA** (F7-C-R1-I; suplantada por DEC-F7C-16)

> **Nota de trazabilidad (F7-C-R1-I, 2026-11-16):** esta DEC se conserva como registro histórico pero queda **descartada** por la resolución arquitectónica de F7-C-R1-I. La propuesta de persistir acontecimientos **únicamente dentro del payload serializado del checkpoint** (sin tabla `world_events`) se descarta: no proporciona delta histórico incremental ni aislamiento de operaciones; el checkpoint se redefine como mecanismo de recuperación y el registro histórico consultable se redefine como `world_events` (DEC-F7C-16).
- **Decisión (histórica, descartada):** los acontecimientos se persisten **únicamente dentro del payload serializado del checkpoint** (`WorldCheckpoint.payload`, producido por `checkpoint_to_dict`), que `PersistenceService._write_checkpoint()` escribe atómicamente en una transacción SQLite junto con el estado del mundo (`WorldRow.last_checkpoint_at`, `months_simulated`, `checkpoint_count`). No existe tabla `world_events` separada; los acontecimientos no se persisten de forma independiente del checkpoint.
- **Contrato (histórico, descartado):** un acontecimiento se considera **confirmado** solo cuando el checkpoint que lo contiene está commitado en SQLite (`world_checkpoints` fila con `is_valid=1` y checksum válido). Si la transacción del checkpoint falla (rollback de SQLite), ningún acontecimiento de ese lote se considera persistido. La recuperación detecta automáticamente checkpoints corruptos (checksum no coincide) y los marca `is_valid=0`, excluyéndolos de la historia consultable.
- **Idempotencia (histórica, descartada):** el `batch_token` se genera antes de serializar el motor y se incluye en el payload del checkpoint. El `batch_token` se deriva como `f"{world_id}:{batch_seq}"` donde `batch_seq = MAX(sequence) + 1` para ese mundo (consultado en `WorldCheckpoint` antes de escribir). Si un lote ya fue confirmado (existe un checkpoint con ese `batch_token` para el mismo mundo), la escritura se rechaza y se devuelve el `checkpoint_id` existente, sin crear eventos duplicados. El `batch_token` se genera antes de serializar el motor, por lo que sobrevive a una caída del proceso tras el commit: el emisor puede reintentar con el mismo `batch_token` y el servidor devuelve el `checkpoint_id` existente sin duplicar eventos.
- **Restricciones (históricas, descartadas):** no se asume atomicidad implícita entre operaciones separadas fuera de la transacción de checkpoint. `Chronicle.log()` no realiza escrituras SQL; los acontecimientos no se persisten de forma independiente del checkpoint. No se crean tablas separadas; los acontecimientos viven únicamente dentro del payload del checkpoint.
- **Estado:** Descartada / suplantada por DEC-F7C-16 (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-D corrección de idempotencia ante caída después del commit (2026-11-16); descartada por F7-C-R1-I (2026-11-16).

## DEC-F7C-10 — API de consulta histórica (F7-C) — **CORREGIDA** (F7-C-R1-I; suplantada por DEC-F7C-17)

> **Nota de trazabilidad (F7-C-R1-I, 2026-11-16):** esta DEC se conserva como registro histórico pero queda **corregida** por la resolución arquitectónica de F7-C-R1-I. La propuesta de paginación con cursor compuesto `checkpoint_id:posición_dentro_del_checkpoint` y orden por checkpoints se descarta; se redefine el orden total por (`operation_sequence`, `posición`) y el cursor pasa a ser opaco sobre ese par. Decisión vigente: DEC-F7C-17.
- **Decisión (histórica, corregida):** se define el endpoint `GET /api/worlds/{world_id}/events` para consulta paginada de acontecimientos.
- **Contrato (histórico, corregido):**
  - Parámetros: `limit` (1-100, default 20), `cursor` (cursor compuesto opcional `checkpoint_id:posición_dentro_del_checkpoint`, punto de corte en `checkpoint_id DESC`), `month` (mes simulado, opcional), `event_type` (tipo, opcional), `kingdom_id` (ID de civilización, opcional).
  - Respuesta: `{"events": [...], "next_cursor": "checkpoint_id:posición_dentro_del_checkpoint"|null, "coverage": {...}}` con allowlist de campos (`event_id` dentro del checkpoint, `month`, `event_type`, `event_text`, `kingdom_ids`, `event_params`, `msg_key`) para propietarios; proyección pública reducida (`event_id`, `month`, `event_type`, `event_text`, `msg_key`) para terceros en mundos públicos.
  - Orden: `checkpoint_id` ASC y, dentro de cada checkpoint, `posición_dentro_del_checkpoint` ASC (orden cronológico de la crónica).
  - Autenticación: opcional para mundos públicos, requerida para mundos propios. Sin sesión, solo mundos públicos.
  - Errores: 400 (parámetros inválidos), 401 (sesión requerida para mundo propio), 404 (mundo inexistente o privado para tercero), 409 (mundo no recuperable).
  - Sin conteo global. Sin `state_json`. Sin efectos secundarios. Consultar historia no inicia runners, no ejecuta ticks, no restaura motores completos.
- **Restricciones (históricas, corregidas):** los filtros no deben revelar existencia de eventos de un mundo privado. La paginación usa cursor compuesto `(checkpoint_id, posición_dentro_del_checkpoint)`. El cursor es un punto de corte, no una autorización. Un cursor inválido (formato no válido, `checkpoint_id` no entero o > 2^63-1, `posición_dentro_del_checkpoint` no entero o < 0) → 400. Un cursor válido se acepta aunque el checkpoint al que apunta ya no exista (punto de corte, no autorización).
- **Estado:** Corregida / suplantada por DEC-F7C-17 (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I corrección de API y paginación compuesta (2026-11-16); corregida por F7-C-R1-C (2026-11-16).

## DEC-F7C-11 — Eliminación de mundo y eventos (F7-C) — **CORREGIDA** (F7-C-R1-I; suplantada por DEC-F7C-18)

> **Nota de trazabilidad (F7-C-R1-I, 2026-11-16):** esta DEC se conserva como registro histórico pero queda **corregida** por la resolución arquitectónica de F7-C-R1-I. La eliminación en cascada se redefine sobre `world_operations`, `world_events` y `WorldCheckpoint` (no sobre acontecimientos embebidos en el payload). Decisión vigente: DEC-F7C-18.
- **Decisión (histórica, corregida):** al eliminar un mundo (`DELETE /api/worlds/{id}`), sus checkpoints asociados (y con ellos los acontecimientos contenidos en ellos) se eliminan en cascada. La eliminación del mundo y sus eventos es atómica: si el mundo no puede borrarse, tampoco se borran sus eventos.
- **Contrato (histórico, corregido):** un consumidor de eventos que intente leer acontecimientos de un mundo en proceso de eliminación encuentra el mundo ya inexistente (404) o los acontecimientos ya borrados (página vacía), nunca eventos huérfanos. Los lotes de eventos pendientes de persistencia para un mundo en eliminación se abortan: el checkpoint del mundo no se confirma, por lo tanto sus eventos tampoco se confirman.
- **Restricciones (históricas, corregidas):** no se conservan huellas de acontecimientos de mundos eliminados. La recuperación de un checkpoint de un mundo eliminado no resucita sus acontecimientos, porque el mundo ya no existe en `worlds` y los checkpoints asociados se eliminaron con él.
- **Estado:** Corregida / suplantada por DEC-F7C-18 (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I corrección de eliminación (2026-11-16); corregida por F7-C-R1-A (2026-11-16).

## DEC-F7C-12 — Frontend de historia (F7-C)

- **Decisión:** la historia se accede desde el detalle del mundo o la galería pública como una vista separada. Muestra tarjetas de acontecimientos ordenadas cronológicamente, con estados de carga, vacío y error. No se suscribe a simulaciones de terceros ni crea runners.
- **Contrato:** el frontend consume `GET /api/worlds/{id}/events` para obtener la historia. La vista es independiente del listado de mundos propios. La proyección mostrada depende de la identidad del solicitante: proyección pública para anónimos y terceros, proyección del propietario para el dueño autenticado.
- **Restricciones:** no se implementan componentes ni modifican estilos en R1. No se incorporan funcionalidades de F7-D a F7-F.
- **Estado:** Vigente (F7-C-R1-I no la modifica); implementación no iniciada.
- **Origen:** F7-C-R1-A corrección de frontend (2026-11-16).

| DEC | Tema | Estado |
|---|---|---|
| DEC01 | Ciclo de vida RecoveryCoordinator | Implementada |
| DEC02 | Startup sin efectos de importación | Implementada |
| DEC03 | — | Pendiente de reconstrucción documental |
| DEC04 | `not_recoverable` solo en memoria | Implementada |
| DEC05 | — | Pendiente de reconstrucción documental |
| DEC06 | Startup dos etapas / pausados | Implementada |
| DEC07 | FastAPI como extra opcional | Implementada |
| DEC08 | Base Etapa 4 (texto no disponible) | Implementada / detalle pendiente |
| DEC09 | WebSocket sin ticks/fast-forward | Implementada (Etapa 5) |
| DEC10 | Eliminación segura | Implementada |
| DEC11 | RunnerRegistry | Implementada |
| DEC12 | Identidad del motor | Implementada |
| DEC13 | Snapshots | Implementada |
| DEC14 | `WorldRow.status` autoridad de pausa | Implementada |
| DEC15 | Base Etapa 4 (texto no disponible) | Implementada / detalle pendiente |
| DEC16 | Clasificación explícita de errores HTTP | Implementada (Etapa 5) |
| DEC17 | Shutdown global | Implementada (Etapa 6) |
| DEC18 | `WorldRunner.close()` | Implementada |
| DEC19 | Presupuesto de captura (revisada) | Revisada / divergencia pendiente en test |
| DEC20 | Visibilidad de mundos (F7-A) | Implementada y verificada (F7-A) |
| DEC21 | Identidad competitiva confiable (F7-A) | Implementada y verificada (F7-A) |
| DEC-F7B-01 | Separación galería pública / mundos propios | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-02 | Orden predeterminado de galería por `id DESC` | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-03 | Paginación por cursor por `id DESC` | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-04 | Búsqueda por nombre | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-05 | Filtro por modo | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-06 | Tarjeta de galería / allowlist pública | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-07 | Detalle público de mundo | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-08 | Acceso anónimo y autenticado | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-09 | WebSocket acotado en galería | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7B-10 | Interfaz de galería | Implementada y verificada (F7-B-I1/I2) |
| DEC-F7C-01 | Persistencia de acontecimientos (F7-C) | Corregida (F7-C-R1-I): registro incremental + diario durable; implementación no iniciada |
| DEC-F7C-02 | Frontera entre Chronicle y persistencia | Corregida (F7-C-R1-I); implementación no iniciada |
| DEC-F7C-03 | Atomicidad e idempotencia | Corregida (F7-C-R1-I): protocolo T1/T2; implementación no iniciada |
| DEC-F7C-04 | Identidad de acontecimientos | **Descartada** (F7-C-R1-I); suplantada por DEC-F7C-13 |
| DEC-F7C-05 | Orden total de acontecimientos | **Descartada** (F7-C-R1-I); suplantada por DEC-F7C-14 |
| DEC-F7C-06 | Tipos de acontecimientos compartibles | Vigente (F7-C-R1-I no la modifica); implementación no iniciada |
| DEC-F7C-07 | Autorización de historia | Vigente (F7-C-R1-I no la modifica); implementación no iniciada |
| DEC-F7C-08 | Historia anterior a F7-C | Vigente (F7-C-R1-I no la modifica); implementación no iniciada |
| DEC-F7C-09 | Modelo de persistencia de acontecimientos | **Descartada** (F7-C-R1-I); suplantada por DEC-F7C-16 |
| DEC-F7C-10 | API de consulta histórica | Corregida (F7-C-R1-I); suplantada por DEC-F7C-17 |
| DEC-F7C-11 | Eliminación de mundo y eventos | Corregida (F7-C-R1-I); suplantada por DEC-F7C-18 |
| DEC-F7C-12 | Frontend de historia | Vigente (F7-C-R1-I no la modifica); implementación no iniciada |
| DEC-F7C-13 | Identidad lógica de operación y acontecimiento (F7-C) | Aprobada / Contrato definido (F7-C-R1-I); implementación no iniciada |
| DEC-F7C-14 | Orden total de acontecimientos (F7-C) | Aprobada / Contrato definido (F7-C-R1-I); implementación no iniciada |
| DEC-F7C-15 | Diario durable de operaciones y protocolo T1/T2 (F7-C) | Aprobada / Contrato definido (F7-C-R1-I); implementación no iniciada |
| DEC-F7C-16 | Modelo de persistencia `world_operations`/`world_events` (F7-C) | Aprobada / Contrato definido (F7-C-R1-I); implementación no iniciada |
| DEC-F7C-17 | Paginación histórica estable y cobertura (F7-C) | Aprobada / Contrato definido (F7-C-R1-I); implementación no iniciada |
| DEC-F7C-18 | Restauración, protección de historia publicada y eliminación (F7-C) | Aprobada / Contrato definido (F7-C-R1-I); implementación no iniciada |
| DEC-F7C-19 | Arquitectura objetivo A/B/C (F7-C) | Aprobada / Arquitectura B adoptada (F7-C-R1-I); implementación no iniciada |

## DEC-F7C-01 — Persistencia de acontecimientos (F7-C)

- **Decisión:** F7-C implementa un **registro persistente e incremental de acontecimientos basado en un diario durable de operaciones + checkpoints para recuperación** (Arquitectura B de F7-C-R1-I), no derivación bajo demanda comparando checkpoints ni persistencia embebida exclusivamente en el payload del checkpoint. Los acontecimientos generados por `Chronicle.log()` se capturan en una frontera explícita y se persisten en `world_events` (junto con la transición de la operación asociada) en una única transacción T2; el checkpoint contiene el estado completo del motor para recuperación y es complementario al registro histórico.
- **Contrato:** los acontecimientos se insertan en `world_events` dentro de T2 junto con el checkpoint del estado resultante y la transición de la operación a `confirmed`. La consulta histórica es HTTP y no inicia runners ni avanza la simulación.
- **Restricciones:** `Chronicle.log()` no realiza consultas ni escrituras SQL. La frontera de captura es explícita y separada de la producción de acontecimientos. No se expone `state_json` en respuestas públicas. No se reinserta la crónica completa acumulativa como delta.
- **Estado:** Corregida / Arquitectura B (F7-C-R1-I); implementación no iniciada.
- **Origen:** F7-C-R1 definición contractual (2026-11-16); corregida por F7-C-R1-I (2026-11-16).

## DEC-F7C-02 — Frontera entre Chronicle y persistencia (F7-C)

- **Decisión:** la captura de acontecimientos ocurre en una frontera explícita entre `Chronicle` (productor) y la infraestructura persistente (consumidor). `Chronicle.log()` sigue generando acontecimientos en memoria; una capa externa de captura detecta el delta de los eventos nuevos producidos por la operación actual y los transfiere a `world_events` en T2, no al payload del checkpoint como fuente histórica.
- **Contrato:** la frontera detecta eventos nuevos por su secuencia dentro del mundo (identificador persistente del acontecimiento), no los escanea completos en cada operación. La transferencia ocurre después de que el estado del mundo se confirma (dentro de T2), para evitar persistir acontecimientos de un mundo que no se guardó.
- **Restricciones:** `Chronicle` no importa SQLAlchemy ni servicios de persistencia. La frontera no ejecuta ticks ni inicia runners. No se compara ciegamente snapshots completos para calcular el delta; la captura se hace en el origen (dentro del avance del motor) o inmediatamente después, marcando los eventos nuevos.
- **Estado:** Corregida / Arquitectura B (F7-C-R1-I); implementación no iniciada.
- **Origen:** F7-C-R1 definición contractual (2026-11-16); corregida por F7-C-R1-I (2026-11-16).

## DEC-F7C-03 — Atomicidad e idempotencia de acontecimientos (F7-C) — **CORREGIDA** (F7-C-R1-I; suplantada por DEC-F7C-15)

> **Nota de trazabilidad (F7-C-R1-I, 2026-11-16):** esta DEC se conserva como registro histórico pero queda **corregida** por la resolución arquitectónica de F7-C-R1-I. La propuesta de `batch_token = f"{world_id}:{batch_seq}"` con `batch_seq = MAX(sequence) + 1` como identidad de lote se descarta: `MAX(sequence)+1` no es identidad durable de operación, se calcula después del commit y no sobrevive a la caída si el emisor no conserva el token. La atomicidad se redefine como protocolo T1/T2 (DEC-F7C-15). Decisión vigente: DEC-F7C-15.

- **Decisión (histórica, corregida):** los acontecimientos y el estado del mundo se confirman en la **misma transacción de base de datos** que el checkpoint correspondiente, ejecutada por `PersistenceService._write_checkpoint()` en un worker thread bajo el lock de persistencia por mundo. Cada lote de acontecimientos recibe un `batch_token` generado **antes** de serializar el motor, derivado como `f"{world_id}:{batch_seq}"` donde `batch_seq = MAX(sequence) + 1` para ese mundo (consultado en `WorldCheckpoint` antes de escribir). Antes de persistir, se comprueba si ya existe un checkpoint con ese `batch_token` para el mismo mundo; si existe, se devuelve el `checkpoint_id` existente sin escribir de nuevo.
- **Contrato (histórico, corregido):** un acontecimiento se considera **confirmado** solo cuando el checkpoint que lo contiene está commitado en SQLite (`world_checkpoints` fila con `is_valid=1` y checksum válido). Si la transacción del checkpoint falla (rollback de SQLite), ningún acontecimiento de ese lote se considera persistido. La recuperación detecta automáticamente checkpoints corruptos (checksum no coincide) y los marca `is_valid=0`, excluyéndolos de la historia consultable.
- **Idempotencia (histórica, corregida):** el `batch_token` se deriva de `f"{world_id}:{batch_seq}"` donde `batch_seq = MAX(sequence) + 1` para ese mundo (consultado en `WorldCheckpoint` antes de escribir). Si el proceso cae después del commit del checkpoint pero antes de que el emisor reciba la confirmación, el emisor puede reintentar con el mismo `batch_token` y el servidor devuelve el `checkpoint_id` existente sin crear eventos duplicados.
- **Restricciones (históricas, corregidas):** no se asume atomicidad implícita entre operaciones separadas fuera de la transacción de checkpoint. `Chronicle.log()` no realiza escrituras SQL; los acontecimientos no se persisten de forma independiente del checkpoint. No se crean tablas separadas; los acontecimientos viven únicamente dentro del payload del checkpoint.
- **Estado:** Corregida / suplantada por DEC-F7C-15 (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-D corrección de idempotencia ante caída después del commit (2026-11-16); corregida por F7-C-R1-I (2026-11-16).

## DEC-F7C-13 — Identidad lógica de operación y acontecimiento (F7-C)

- **Decisión:** la identidad lógica de una operación es la tupla (`world_id`, `operation_id`, `posición`) donde `operation_id` es una identidad durable generada en T1, `posición` es el índice secuencial dentro del delta de la operación, y `world_id` identifica el mundo. El acontecimiento persiste con la misma `world_id`, `operation_id` y `posición`, y su orden se basa en (`operation_sequence`, `posición`) dentro de cada `world_id`. `Event.id` de la crónica en memoria es efímero y nunca forma parte de la identidad lógica.
- **Contrato:** la identidad de operación es única dentro de su mundo, se genera en T1, y se asocia al delta de la operación. El acontecimiento mantiene la misma identidad durante su vida; la restauración desde checkpoint reutiliza `operation_id` y `posición` pero no reutiliza `operation_sequence` ni `event_id`.
- **Restricciones:** la identidad no es trans-mundo; no se permite reutilizar `operation_id` entre mundos; `position` es única dentro del delta de la operación.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I definición de identidad lógica de operación (2026-11-16).

## DEC-F7C-14 — Orden total de acontecimientos (F7-C)

- **Decisión:** los acontecimientos se ordenan por (`operation_sequence`, `posición`) como clave total y determinista. `operation_sequence` es monotonía creciente por mundo, asignada en T1; `posición` es el índice dentro del delta de la operación. El orden es estable, no repetido y no depende de `checkpoint_id` ni de `WorldCheckpoint.sequence`.
- **Contrato:** la consulta de historia devuelve acontecimientos ordenados por `operation_sequence` ASC y, dentro de cada operación, por `posición` ASC. El cursor de paginación es un par (`operation_sequence`, `posición`) que representa un punto de corte en este orden total.
- **Restricciones:** no se utiliza `WorldRow.created_at` ni `WorldCheckpoint.sequence` como clave principal; el orden es independiente del checkpoint.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I definición de orden total (2026-11-16).

## DEC-F7C-15 — Diario durable de operaciones y protocolo T1/T2 (F7-C)

- **Decisión:** se define un diario durable de operaciones que registra cada operación pendiente antes de su ejecución, y un protocolo de dos transacciones (T1/T2) que garantiza la atomicidad de la confirmación del checkpoint, los acontecimientos y el estado de la operación.
- **T1 — Reserva durable de operación:** antes de iniciar la ejecución del motor, se genera una `operation_id` durable (p. ej., UUID o secuencia atómica) y se registra la operación pendiente en el diario de operaciones con estado `pending`. El emisor conserva la `operation_id` antes de enviar la solicitud.
- **T2 — Confirmación atómica:** una única transacción confirma el checkpoint del estado resultante del motor, los acontecimientos nuevos generados por la operación, la transición del registro de operación a `confirmed`, la referencia al checkpoint resultante y la secuencia durable de operación. Si cualquiera de esas escrituras falla, T2 hace rollback completo.
- **Idempotencia:** si el mismo `operation_id` se envía dos veces, el servidor detecta que la operación ya está confirmada (existe `world_operations` con `status=confirmed`) y devuelve el resultado ya persistido sin volver a ejecutar el tick ni crear eventos duplicados.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I definición del protocolo T1/T2 (2026-11-16).

## DEC-F7C-16 — Modelo de persistencia `world_operations`/`world_events` (F7-C)

- **`world_operations`** (tabla):
  - `id` (PK, AUTOINCREMENT)
  - `world_id` (FK a `worlds.id`, índice)
  - `operation_id` (identidad durable de operación, único por mundo)
  - `operation_sequence` (monotonía creciente por mundo, asignada en T1)
  - `status` (enum: `pending`, `confirmed`, `failed`, `canceled`)
  - `created_at` (ISO 8601 UTC)
  - `confirmed_at` (ISO 8601 UTC, nulo si no confirmada)
  - `checkpoint_id` (FK a `WorldCheckpoint.id`, nulo si pendiente)
  - `payload_ref` (texto opcional con referencia al payload del checkpoint)
- **`world_events`** (tabla):
  - `id` (PK, AUTOINCREMENT)
  - `world_id` (FK a `worlds.id`, índice)
  - `operation_id` (FK a `world_operations.operation_id`)
  - `operation_sequence` (monotonía dentro del delta de la operación)
  - `position` (índice secuencial dentro del delta de la operación)
  - `month` (mes simulado)
  - `event_type` (tipo de acontecimiento)
  - `event_text` (texto descriptivo)
  - `kingdom_ids` (lista de IDs de civilizaciones)
  - `event_params` (JSON o BLOB con parámetros)
  - `msg_key` (clave de mensaje)
  - `created_at` (ISO 8601 UTC)
- **PK/UNIQUE:** `world_operations` tiene PK (`id`) y UNIQUE (`world_id`, `operation_id`); `world_events` tiene PK (`id`) y UNIQUE (`world_id`, `operation_id`, `operation_sequence`, `position`).
- **FK & cascadas:** `world_operations.world_id` → `worlds.id`; `world_events.world_id` → `worlds.id`; `world_events.operation_id` → `world_operations.operation_id`; `WorldCheckpoint.world_id` → `worlds.id`. Al borrar un mundo, se cascada a `world_operations`, `world_events` y `WorldCheckpoint` (ON DELETE CASCADE).
- **Índices:** índices en `world_id`, `operation_id`, `operation_sequence`, `position` para consultas por operación y orden total.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I modelo de persistencia (2026-11-16).

## DEC-F7C-17 — Paginación histórica estable y cobertura (F7-C)

- **Endpoint:** `GET /api/worlds/{world_id}/events`
- **Parámetros:**
  - `limit` (1‑100, default 20)
  - `cursor` (opaco o serializado `operation_sequence:posición`, punto de corte en orden total)
  - `month` (opcional)
  - `event_type` (opcional)
  - `kingdom_id` (opcional)
- **Respuesta:** `{"events": [...], "next_cursor": "operation_sequence:posición"|null, "coverage": {"status": "confirmed|potentially_incomplete|unknown", "first_checkpoint_month": int|null, "last_checkpoint_month": int|null}}`
- **Proyección:** propietario ve todos los campos permitidos; terceros en mundo público ven solo `event_id`, `month`, `event_type`, `event_text`, `msg_key`.
- **Cobertura:** `confirmed` = al menos un checkpoint válido y todos los acontecimientos entre `first_checkpoint_month` y `last_checkpoint_month` presentes; `potentially_incomplete` = al menos un checkpoint válido pero posible pérdida entre checkpoints; `unknown` = sin checkpoints válidos.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I definición de API y cobertura (2026-11-16).

## DEC-F7C-18 — Restauración, protección de historia publicada y eliminación (F7-C)

- **Restauración:** al recuperar un checkpoint, el motor inyecta la instancia exacta de `SimEngine` y reconstruye `world_events` solo a partir del delta de la operación asociada al checkpoint, no reinserta la crónica completa. Los acontecimientos anteriores a la habilitación de F7-C no son consultables.
- **Protección de historia publicada:** una restauración nunca reescribe silenciosamente acontecimientos ya confirmados; para producir una historia divergente se crea un nuevo `world_id`, manteniendo la integridad del mundo original.
- **Eliminación:** al ejecutar `DELETE /api/worlds/{id}`, se elimina el mundo, sus `world_operations`, `world_events`, y los `WorldCheckpoint` asociados en una transacción única. Los acontecimientos huérfanos no pueden existir; la eliminación es atómica con el mundo.
- **Estado:** Aprobada / Contrato definido (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** F7-C-R1-I diseño de restauración y eliminación (2026-11-16).

## DEC-F7C-19 — Arquitectura objetivo A/B/C (F7-C)

- **A — Historia derivada de checkpoints:** evalúa pérdida de eventos entre checkpoints, duplicación por crónica acumulativa, falta de identidad lógica, paginación inestable y coste de consulta. No resuelve los defectos de R1-H-B.
- **B — Registro incremental persistente + diario durable + checkpoints:** evalúa que B resuelve los fallos observados (identidad no durable, ausencia de diario, pérdida de cobertura, ausencia de orden total, ausencia de restauración segura). Adoptada como arquitectura obligatoria.
- **C — Híbrida:** evalúa que C añade complejidad sin resolver los defectos fundamentales y dificulta la recuperación.
- **Decisión obligatoria:** adoptar B como registro histórico permanente, manteniendo checkpoints como mecanismo de recuperación. No se recomienda A como fuente histórica ni C por su complejidad innecesaria.
- **Estado:** Aprobada / Arquitectura B adoptada (F7-C-R1-I, Arquitectura B); implementación no iniciada.
- **Origen:** Evaluación y decisión arquitectónica (2026-11-16).

| DEC-F7D-01 | Modelo de datos de desafío | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-02 | Configuración común inmutable y versión de reglas | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-03 | Identidad y autorización competitiva | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-04 | Ciclo de vida del desafío | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-05 | Reglas de victoria y resultados | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-06 | Determinismo cross-process y reproducibilidad | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-07 | Integridad y verificación de resultados | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-08 | Disputas y resolución administrativa | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-09 | Privacidad y exposición pública | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-10 | API contractual de desafíos | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-11 | Migraciones y compatibilidad | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |
| DEC-F7D-12 | Frontend y alcance de interfaz | Aprobada / Contrato definido (F7-D-R1); implementación no iniciada |

## DEC-F7D-01 — Modelo de datos de desafío (F7-D)

- **Decisión:** F7-D implementa desafíos con semillas comunes mediante dos tablas nuevas, `challenges` y `challenge_participants`, **sin modificar la tabla `worlds`** ni añadirle columna `challenge_id`. Cada desafío congela su configuración competitiva (seed, dimensiones, versión del motor, reglas de victoria, duración en ticks) y se asocia a participantes a través de mundos independientes, uno por cuenta inscrita.
- **Contrato:**
  - `challenges`: PK `id`; `organizer_user_id` (FK a `users.id`, no nulo); `name`; `config_json` (configuración inmutable, versionada); `rules_version`; `duration_ticks`; `state` (máquina de estados); `created_at`; `closed_at`; `completed_at`.
  - `challenge_participants`: PK `id`; `challenge_id` (FK a `challenges.id`, ON DELETE CASCADE); `user_id` (FK a `users.id`, no nulo); `world_id` (FK a `worlds.id`, ON DELETE CASCADE, único por challenge); `state` (inscrito/preparando/ejecutando/finalizado/abandonado); `joined_at`; `result_json` (resultado calculado, solo servidor); `result_verified` (booleano).
  - UNIQUE (`challenge_id`, `user_id`) impide inscripciones duplicadas.
  - UNIQUE (`challenge_id`, `world_id`) impide asociar un mismo mundo a dos participantes de un desafío.
  - La eliminación de un desafío borra sus participaciones (historia competitiva preservada vía `result_json` antes del borrado si el organizador lo decide; véase DEC-F7D-08).
  - La eliminación de un mundo de participante marca su participación como abandonada, no borra la inscripción.
- **Restricciones:** no se añade `challenge_id` a `worlds`; no se crea segunda identidad de operación ni protocolo de idempotencia paralelo a F7-C; toda escritura de resultado usa el protocolo T1/T2 existente.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17).

## DEC-F7D-02 — Configuración común inmutable y versión de reglas (F7-D)

- **Decisión:** la configuración de un desafío es un objeto inmutable versionado (`config_json`) que contiene, como mínimo: `seed`, `width`, `height`, `sea_level`, `n_civilized`, `n_tribal`, `n_beasts`, `enabled_races`, `kingdom_races`, `mode`, `season_years`, `engine_version`, `rules_version` y `duration_ticks`. Todos los mundos participantes se construyen desde el mismo `config_json`.
- **Contrato:**
  - `seed` por sí sola **no** garantiza igualdad de condiciones iniciales; se requiere todo el `config_json`.
  - La publicación del desafío congela `config_json`, `rules_version` y `duration_ticks`; no pueden modificarse después.
  - La versión del motor (`engine_version`) se registra en cada operación de resultado (T1/T2 de F7-C).
- **Restricciones:** no se asume que `seed` por sí sola sea suficiente; `config_json` es la fuente de verdad. No se permiten dos versiones de reglas activas simultáneamente en un mismo desafío.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17).

## DEC-F7D-03 — Identidad y autorización competitiva (F7-D)

- **Decisión:** la identidad competitiva deriva exclusivamente de la cuenta autenticada mediante sesión Bearer (`user_id`), aplicando DEC20 y DEC21 sin excepciones. `user_token` nunca se usa como identidad, autorización ni prueba de propiedad en desafíos.
- **Contrato:**
  - Organizador: cuenta autenticada que crea el desafío; es `organizer_user_id` en `challenges`.
  - Participante: cuenta autenticada inscrita; es `user_id` en `challenge_participants`.
  - Espectador: cualquier visitante (mundo público) o solo participantes/organizador (mundo privado), según DEC20.
  - Administrador del sistema: cuenta con `is_admin=true` (F7-C-I1-FIX2); puede resolver disputas.
- **Restricciones:** un participante no puede inscribir a otra cuenta, alterar reglas congeladas, reemplazar su mundo por uno ajeno, declarar arbitrariamente haber completado el desafío, ni cambiar la identidad que recibe el resultado. El organizador no puede alterar retroactivamente resultados confirmados sin una resolución administrativa auditable.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17), aplicando DEC20 y DEC21.

## DEC-F7D-04 — Ciclo de vida del desafío (F7-D)

- **Decisión:** máquina de estados simplificada: `draft → published → enrollment_open → enrollment_closed → preparing → running → completed`, con salidas terminales `canceled` e `invalidated` desde `published`, `enrollment_open` o `enrollment_closed`, y `disputed` → `resolved` desde `completed`.
- **Contrato:**
  - `draft`: borrador, solo organizador, no público.
  - `published`: configuración congelada, inscripciones abiertas.
  - `enrollment_open`: sinónimo operativo de `published` para inscripciones.
  - `enrollment_closed`: lista de participantes congelada; no se aceptan más inscripciones.
  - `preparing`: mundos derivados creados y asociados; aún no se ejecuta simulación.
  - `running`: simulación en curso en todos los mundos participantes.
  - `completed`: resultados calculados y verificados; desafío terminado.
  - `canceled`: desafío anulado antes de `running`; no hay resultados competitivos.
  - `invalidated`: desafío anulado tras `completed` por disputa; resultados nulos.
  - `disputed`: desafío con disputa abierta; resultados congelados hasta resolución.
- **Transiciones atómicas:** publicación, cierre de inscripciones, inicio, finalización y resolución de disputa son transacciones únicas. La inscripción usa T1 de F7-C (idempotente por `client_idempotency_key`).
- **Inscripción tardía/retirada/reingreso:** no permitidas tras `enrollment_closed`; retirada antes del cierre marca la participación como abandonada sin borrar la inscripción.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17).

## DEC-F7D-05 — Reglas de victoria y resultados (F7-D)

- **Decisión:** política inicial simple y técnicamente verificable: **estado final tras `duration_ticks` ticks**, comparado mediante métricas agregadas con orden de prioridad y desempates deterministas.
- **Contrato:**
  - Duración exacta: `duration_ticks` definidos en la configuración congelada.
  - Condición de finalización: todos los mundos participantes han ejecutado `duration_ticks` ticks (o están irrecuperables/cancelados).
  - Métricas admitidas Nivel 1 (orden de prioridad): `total_population`, `wars_won`, `tech_level`, `influence_max`.
  - **Definiciones formales:**
    - `total_population`: máximo `kingdom.total_population` entre reinos del participante.
    - `wars_won`: máximo `kingdom.wars_won` entre reinos del participante.
    - `tech_level`: máximo `kingdom.tech_level` entre reinos del participante.
    - `influence_max`: máximo sobre reinos del participante de `count(tile) donde influence_map(tile) == kingdom.id`. Determinista vía `engine.influence_map()`.
  - **Métricas retiradas Nivel 1:** `gold` retirado por ausencia de economía simulada durable en modelo. Requiere decisión arquitectónica independiente para futura incorporación.
  - **Métrica principal:** cada desafío declara `primary_metric` en `config_json` inmutable (valores permitidos: `total_population`, `wars_won`, `tech_level`, `influence_max`). Se congela al publicar; no es modificable posteriormente.
  - **Clasificación:** lexicográfica con `primary_metric` DESC, luego desempates: `tech_level` DESC, `wars_won` DESC, `total_population` DESC, `user_id` ASC interno. Si `primary_metric` coincide con una métrica de desempate, se omite la repetición. `influence_max` puede ser métrica principal pero no se usa como desempate.
  - Desempates: en orden, `tech_level` desc, `wars_won` desc, `total_population` desc, `user_id` asc (determinista).
  - Abandono: participación marcada `abandonado`; no ocupa posición competitiva.
  - Participantes que no completan: no aparecen en clasificación.
  - Mundos irrecuperables (409 DEC16): participante excluido, desafío continúa si hay al menos un completado.
  - Resultados inválidos: nunca se aceptan puntuaciones enviadas por clientes; solo cálculo en servidor desde estado durable.
  - Empates reales: desempate por `user_id` asc; si persiste, empate compartido.
- **Restricciones:** no se usa tiempo de pared como criterio competitivo. Los resultados se calculan en servidor usando estado durable (`worlds.state_json`, `world_operations`, `world_events`).
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17).

## DEC-F7D-06 — Determinismo cross-process y reproducibilidad (F7-D)

- **Decisión:** la reproducibilidad entre procesos es un requisito contractual verificable: misma `config_json` y mismo `seed` deben producir estados iniciales y finales equivalentes, independientemente de `PYTHONHASHSEED`.
- **Contrato:**
  - El motor de simulación (`SimEngine`) consume RNG exclusivamente a través de `np.random.default_rng(seed)` en orden fijo; no usa `time`, `uuid`, `secrets` ni `os.environ` en el camino de `tick()`.
  - `checkpoint_to_dict` / `engine_from_checkpoint` codifican y restauran el estado RNG (PCG64 validado).
  - Representación canónica para comparación: `checkpoint_to_dict` serializada con `json.dumps(sort_keys=True, ensure_ascii=False)`, excluyendo únicamente metadatos de persistencia no competitivos (timestamps de creación, IDs de fila).
- **Evidencia (F7-D-R1):** dos procesos independientes con `PYTHONHASHSEED=1` y `PYTHONHASHSEED=98765`, misma `seed=12345` y 50 ticks, producen digest SHA-256 idéntico (`ff7593098a9f81e84f6584b2f0ba0de59f26796c2a0e78376d2d1987c91956d4`) del estado canónico.
- **Restricciones:** no se declara reproducibilidad cross-process garantizada sin evidencia; cualquier cambio al motor debe re-verificar esta propiedad antes de habilitar competición.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 experimento de determinismo (2026-11-17).

## DEC-F7D-07 — Integridad y verificación de resultados (F7-D)

- **Decisión:** se reutilizan íntegramente las garantías de F7-C (protocolo T1/T2, identidad de operación, idempotencia, secuencia durable, eventos, checkpoints, recovery generation, fencing, coverage) sin duplicar el servicio de operaciones ni crear un protocolo alternativo.
- **Contrato:**
  - Cada envío de resultado de desafío es una operación T1/T2 de F7-C asociada al `world_id` del participante.
  - La repetición de una misma operación con idéntica clave e idéntico contenido devuelve la operación original (idempotencia F7-C).
  - Una caída entre cálculo, persistencia y ACK no genera duplicados ni resultados contradictorios (T2 atómico).
  - Distinción contractual: resultado calculado / persistido / verificado / confirmado / disputado / invalidado.
- **Restricciones:** el cliente no puede enviar una puntuación para que sea aceptada como verdad; solo el servidor calcula desde estado durable.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17), aplicando CONTRACTS.md §16 (F7-C).

## DEC-F7D-08 — Disputas y resolución administrativa (F7-D)

- **Decisión:** mecanismo acotado de disputa y resolución. La identidad administrativa se verifica contra la autorización real del servidor (`is_admin` de F7-C-I1-FIX2); no se confía en un `admin_user_id` suministrado por el cliente.
- **Contrato:**
  - Un participante puede presentar disputa sobre su propio resultado (`disputed`).
  - El organizador o administrador resuelve la disputa con una resolución administrativa auditable (actor, motivo, evidencia, fecha, identidad de operación, resultado, idempotencia).
  - Los resultados confirmados no se sobrescriben silenciosamente; una corrección crea un nuevo registro que referencia al anterior.
  - Se reutiliza `operation_resolutions` de F7-C cuando su semántica corresponda, sin convertirla en un sistema genérico de moderación.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17), aplicando CONTRACTS.md §16 (F7-C).

## DEC-F7D-09 — Privacidad y exposición pública (F7-D)

- **Decisión:** política conservadora compatible con DEC20. Un desafío público no vuelve públicos automáticamente los mundos de sus participantes. No se amplía `PUBLIC_WORLD_FIELDS` ni se expone `state_json` de terceros.
- **Contrato:**
  - Allowlists específicas para respuestas públicas de desafíos (separadas de los mundos).
  - Identificadores internos de usuario no se exponen por defecto.
  - Visitante: ve desafíos públicos y clasificación anónima (sin `user_id`).
  - Participante inscrito: ve su propio mundo y resultado.
  - Organizador: ve todos los mundos y resultados de su desafío (dentro de los límites DEC20 para mundos privados de terceros).
  - Administrador autorizado: ve todo para resolución de disputas.
- **Restricciones:** la visibilidad de resultados respeta el contrato; mundos privados siguen protegidos (404 para terceros).
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17), aplicando DEC20.

## DEC-F7D-10 — API contractual de desafíos (F7-D)

- **Decisión:** endpoints necesarios, sin asumir que todos requieren endpoints independientes. Cada endpoint define método, ruta, autenticación, autorización, request, response, validaciones, errores HTTP, idempotencia, efectos persistentes y campos públicos/privados.
- **Contrato (endpoints):**
  - `POST /api/challenges` (auth): crear desafío (borrador).
  - `GET /api/challenges` (público): listar desafíos publicados.
  - `GET /api/challenges/{id}` (público/según visibilidad): consultar desafío.
  - `PUT /api/challenges/{id}/publish` (auth, organizador): publicar (congela configuración).
  - `POST /api/challenges/{id}/enroll` (auth): inscribirse (idempotente, T1 F7-C).
  - `GET /api/challenges/{id}/participants` (según visibilidad): consultar participantes.
  - `PUT /api/challenges/{id}/close-enrollment` (auth, organizador): cerrar inscripciones.
  - `PUT /api/challenges/{id}/start` (auth, organizador): iniciar (congela participantes y reglas).
  - `GET /api/challenges/{id}/results` (según visibilidad): consultar resultados.
  - `POST /api/challenges/{id}/dispute` (auth, participante): presentar disputa.
  - `POST /api/challenges/{id}/resolve` (auth, organizador/admin): resolver disputa.
  - `PUT /api/challenges/{id}/cancel` (auth, organizador): cancelar/invalidar.
- **Restricciones:** no se permiten endpoints que dejen al cliente establecer estados internos o resultados autoritativos.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17).

## DEC-F7D-11 — Migraciones y compatibilidad (F7-D)

- **Decisión:** esquema propuesto sin aplicarse durante R1. Se respetan las tablas y garantías existentes; `worlds` no se modifica salvo que una nueva decisión justificada lo demuestre (alternativa preferida: no hacerlo).
- **Contrato:**
  - Creación de tablas `challenges` y `challenge_participants` con PK, FK, UNIQUE, CHECK e índices.
  - Integridad referencial: ON DELETE CASCADE de `challenge_participants.challenge_id` y `challenge_participants.world_id`; ON DELETE CASCADE de `challenge_participants.user_id` a `users.id`.
  - Migraciones futuras idempotentes y con rollback seguro (tabla-rebuild pattern de FIX3 si fuera necesario).
  - Atomicidad: inscripción y creación de mundo como transacción coordinada sin segunda identidad de operación.
- **Restricciones:** no se ejecutan migraciones durante R1; no se rompe compatibilidad con F7-A, F7-B, F7-C.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17).

## DEC-F7D-12 — Frontend y alcance de interfaz (F7-D)

- **Decisión:** alcance mínimo de interfaz, reutilizando componentes existentes cuando sea razonable. No se exige WebSocket si HTTP con polling resulta suficiente.
- **Contrato:**
  - Descubrimiento de desafíos, detalle, inscripción, estado de participación, progreso, resultados, disputas.
  - No se reestructura el lobby ni la navegación general sin necesidad.
- **Restricciones:** no se implementan componentes durante R1.
- **Estado:** Aprobada / Contrato definido (F7-D-R1); implementación no iniciada.
- **Origen:** F7-D-R1 diseño arquitectónico (2026-11-17).

| DEC-F7D-13 | Modelo real de competición y agencia del participante | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-14 | Momento de creación de mundos y consistencia del ciclo de inscripción | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-15 | Atomicidad e idempotencia de operaciones globales de desafío | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-16 | Retención histórica y eliminación de mundos y desafíos | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-17 | Reutilización de mundos entre desafíos | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-18 | Máquina de estados completa del desafío | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-19 | Procedencia y verificación de resultados | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-20 | Privacidad e identificación en rankings públicos | Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada |
| DEC-F7D-21 | Comparación estadística reproducible vs competición real | Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada |
| DEC-F7D-22 | Semántica de resultados sin agencia del participante | Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada |
| DEC-F7D-23 | Clasificación estadística pública | Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada |
| DEC-F7D-24 | Niveles de evolución de F7-D | Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada |

## DEC-F7D-13 — Modelo real de competición y agencia del participante (F7-D)

- **Decisión:** **Alternativa C — Desafío de simulación sin agencia competitiva del participante.** La arquitectura actual **no dispone de ningún mecanismo de intervención del usuario sobre la simulación en curso**. `WorldRunner` solo expone `subscribe`, `unsubscribe`, `set_selected` (selección de reino para stats) y `start`/`close`; `SimEngine` consume RNG exclusivamente a través de `np.random.default_rng(seed)` en orden fijo y **no usa `time`, `uuid`, `secrets` ni `os.environ` en el camino de `tick()`**. `OperationParams` de F7-C solo define `operation_type="advance"` y `months` — no existe operación de decisión o acción del participante.
- **Consecuencia técnica:** dado el mismo `config_json` (seed, dimensiones, razas, modo, etc.), todos los participantes obtienen **estados idénticos**. Eso no constituye una competición significativa entre participantes, sino una comparación de simulaciones reproducibles.
- **Contrato:**
  - No se declaran rankings como resultados competitivos de participantes.
  - No se aceptan puntuaciones ni resultados autoritativos enviados por el cliente.
  - El servidor calcula cualquier resultado a partir del estado durable del mundo; el cliente nunca puede afirmar un resultado.
  - La participación competitiva se reduce a inscribirse en un desafío y que su mundo asociado ejecute `duration_ticks` ticks bajo las mismas reglas que los demás.
- **Restricciones:** no se inventan endpoints, comandos o capacidades de intervención que no existan en el código. Si en el futuro se desea una competición real con agencia del participante (Alternativa A o B), requiere una decisión arquitectónica nueva y una implementación de mecanismo de decisiones del usuario, que queda fuera de F7-D.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17), fundamentado en inspección de `src/worldsim/server/runner.py`, `src/worldsim/sim/engine.py` y `src/worldsim/server/operation_service.py`.

## DEC-F7D-14 — Momento de creación de mundos y consistencia del ciclo de inscripción (F7-D)

- **Decisión:** se adopta un único momento contractual de creación de mundos, consistente en todo el ciclo:
  1. **Inscripción:** registra durablemente la participación y su identidad (`user_id`) en `challenge_participants`; **no** crea mundo aún.
  2. **Cierre de inscripciones (`enrollment_closed`):** congela el conjunto de participantes; no se admiten más inscripciones.
  3. **Preparación (`preparing`):** crea los mundos derivados a partir de la `config_json` inmutable del desafío, uno por participante, y los asocia a su participación.
  4. **Inicio (`running`):** solo puede producirse cuando **todos** los participantes admitidos tienen su mundo correctamente preparado y asociado.
- **Contrato:**
  - Una inscripción parcialmente fallida no deja mundos huérfanos: la transacción de inscripción es atómica (registro de participación + reserva de secuencia en T1 de F7-C si aplica).
  - La preparación es reanudable e idempotente: reintentar la preparación no genera mundos duplicados ni asociaciones duplicadas (`UNIQUE(challenge_id, world_id)` y `UNIQUE(challenge_id, user_id)` lo impiden).
  - Si la preparación no puede completarse para algún participante, el desafío no pasa a `running`; se aplica reintento controlado, bloqueo o cancelación según corresponda (DEC-F7D-15/18).
  - No se inicia la ejecución competitiva con un conjunto de mundos incompleto o parcialmente preparado.
- **Restricciones:** no se crea el mundo en el momento de inscripción; la creación ocurre en `preparing`, después del cierre de inscripciones. Esto elimina la contradicción de F7-D-R1 entre "inscripción T1 crea el mundo" y "`preparing` crea los mundos".
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17).

## DEC-F7D-15 — Atomicidad e idempotencia de operaciones globales de desafío (F7-D)

- **Decisión:** las operaciones de ámbito desafío (publicar, abrir/cerrar inscripciones, inscribir, iniciar preparación, iniciar ejecución, cancelar, registrar disputa, resolver disputa, confirmar/invalidar resultados) usan **transacciones SQL atómicas sobre la entidad `challenges` y las entidades relacionadas**, con identidad de petición idempotente. No se duplica el servicio T1/T2 de F7-C ni se crea un motor paralelo de ejecución.
- **Contrato:**
  - Cada operación global lleva una clave de petición idempotente (`client_idempotency_key`) y un hash del contenido canónico (`request_hash`, SHA-256 de la representación canónica versionada).
  - Ámbito de idempotencia: `(challenge_id, requesting_user_id, client_idempotency_key)`.
  - Misma clave y mismo contenido → devuelve el resultado de la operación original (sin duplicar efectos).
  - Misma clave y contenido diferente → conflicto explícito (409).
  - El cambio de estado y sus efectos relacionales persisten atómicamente en una misma transacción SQL.
  - La autorización se comprueba en el servidor usando la identidad autenticada (Bearer `user_id`); nunca se confía en un `admin_user_id` ni identidad de autoridad aportados por el cliente.
  - Una operación ya confirmada no se ejecuta otra vez; una operación en estado incierto no produce efectos duplicados.
- **Restricciones:** no se introduce un registro general de solicitudes globales salvo lo estrictamente necesario para operaciones de desafío; no se replica todo el ciclo T1/T2 de F7-C para operaciones de desafío.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17), aplicando CONTRACTS.md §16 (F7-C) de forma proporcional.

## DEC-F7D-16 — Retención histórica y eliminación de mundos y desafíos (F7-D)

- **Decisión:** política de retención histórica explícita. **No se permite que la eliminación física de un mundo participante destruya el registro histórico del desafío**, ni que eliminar un desafío publicado borre resultados confirmados.
- **Contrato:**
  - La asociación participante→mundo se conserva como referencia histórica incluso si el mundo deja de estar disponible operativamente. La FK `challenge_participants.world_id` no usa `ON DELETE CASCADE` destructiva del historial; el mundo puede marcarse como eliminado lógicamente y la participación conserva su `state` (p. ej., `abandonado`) y su `result_json`.
  - `result_json` (resultado calculado por el servidor) y `result_verified` se conservan de forma inmutable tras la confirmación del desafío.
  - Eliminar un desafío **publicado o finalizado** no borra resultados confirmados; solo se permite eliminación lógica o borrado físico tras archivar la evidencia.
  - Se separa eliminación lógica (marcar estado) de eliminación física (borrar filas); la anonimización de datos personales, si aplica, se define por separado.
- **Restricciones:** no se deja una FK obligatoria que impida la política elegida; no se usan cascadas que destruyan el historial. El resultado confirmado es inmutable salvo un procedimiento explícito, autorizado y auditable de corrección o invalidación (DEC-F7D-08/19).
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17), resolviendo la incompatibilidad de F7-D-R1 entre `ON DELETE CASCADE` y marcar participación como `abandoned`.

## DEC-F7D-17 — Reutilización de mundos entre desafíos (F7-D)

- **Decisión:** un mundo participante pertenece a un **único desafío competitivo durante todo su ciclo de vida**. La restricción es global sobre `world_id`, no solamente sobre la pareja `(challenge_id, world_id)`.
- **Contrato:**
  - Un mundo no puede asociarse a dos desafíos distintos simultáneamente.
  - Si un participante desea participar en otro desafío, se crea un **mundo derivado nuevo** vinculado al nuevo desafío; no se reutiliza el mundo original.
  - Justificación: aislamiento entre desafíos, reproducibilidad (mismo mundo en dos desafíos contaminaría la comparación), atribución de resultados y prevención de reutilización de estados.
  - Mundos abandonados, invalidados o pertenecientes a desafíos cancelados conservan su asociación histórica; no se reutilizan para nuevos desafíos sin crear un mundo derivado.
- **Restricciones:** `UNIQUE(world_id)` a nivel de participaciones activas (no solo `UNIQUE(challenge_id, world_id)`). Esto corrige la incompleteness de F7-D-R1 que solo impedía repetir un mundo dentro del mismo desafío.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17).

## DEC-F7D-18 — Máquina de estados completa del desafío (F7-D)

- **Decisión:** máquina de estados completa, sin transiciones implícitas ni omitidas:
  `draft → published → enrollment_open → enrollment_closed → preparing → running → completed`,
  con salidas terminales o excepcionales `canceled`, `invalidated`, `disputed` → `resolved`.
- **Contrato (transición → origen, destino, actor, precondición, efecto atómico):**
  - `draft → published`: organizador; desafío en `draft`; congela `config_json`, `rules_version`, `duration_ticks`.
  - `published → enrollment_open`: organizador; desafío en `published`; abre inscripciones (son sinónimo operativo durante este estado).
  - `enrollment_open → enrollment_closed`: organizador; desafío en `enrollment_open`; congela lista de participantes; cierra inscripciones.
  - `enrollment_closed → preparing`: organizador/sistema; desafío en `enrollment_closed`; crea mundos derivados (DEC-F7D-14); idempotente.
  - `preparing → running`: sistema; todos los participantes admitidos tienen mundo preparado y asociado; inicia simulación en todos los mundos.
  - `running → completed`: sistema; todos los mundos participantes ejecutaron `duration_ticks` ticks (o están irrecuperables/cancelados); calcula y verifica resultados.
  - Salidas `canceled` desde `published`, `enrollment_open` o `enrollment_closed`: organizador; desafío anulado antes de `running`; no hay resultados competitivos.
  - Salida `invalidated` desde `completed`: resolución administrativa; desafío anulado por disputa; resultados nulos.
  - `completed → disputed`: participante; presenta disputa sobre su propio resultado; resultados congelados hasta resolución.
  - `disputed → resolved`: organizador o administrador; resolución administrativa auditable; resultado confirmado o nulo.
- **Restricciones:** no se permiten transiciones arbitrarias mediante actualizaciones directas del estado; cada transición valida precondiciones y actor dentro de una transacción atómica. No se asume que todos los estados sean equivalentes ni que cualquier actor pueda activarlos.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17), completando la transición `published → enrollment_open` omitida en F7-D-R1.

## DEC-F7D-19 — Procedencia y verificación de resultados (F7-D)

- **Decisión:** cadena verificable entre configuración inmutable, identidad del participante, mundo asociado, operaciones competitivas aceptadas, secuencia durable, eventos confirmados, checkpoints, estado final, métricas calculadas y resultado persistido.
- **Contrato:**
  - El cliente **nunca** puede suministrar puntuaciones autoritativas; el resultado deriva exclusivamente del estado durable del servidor.
  - Las operaciones competitivas deben estar **confirmadas** (`status=confirmed`) y no quedar en estado incierto (`pending`, `executing`, `blocked`, `failed`, `canceled` no cuentan como acciones competitivas válidas).
  - La secuencia debe respetar el orden durable de `operation_sequence`.
  - La duración se mide en `duration_ticks` (unidad determinista), **no** en tiempo de pared.
  - Condición exacta de finalización: todos los mundos participantes han ejecutado `duration_ticks` ticks (o están irrecuperables/cancelados).
  - Todos los participantes son evaluados bajo las mismas reglas (misma `config_json`, misma `duration_ticks`, misma `engine_version`).
  - Un participante que no termina no aparece en clasificación; un mundo irrecuperable (409 DEC16) excluye al participante sin invalidar el desafío si hay al menos un completado.
  - La confirmación del resultado es idempotente e inmutable; una disputa no sobrescribe silenciosamente el resultado original (una corrección crea un nuevo registro que referencia al anterior).
- **Distinción de estados del resultado:**
  - `calculated`: métrica computada por el servidor desde estado durable, aún no persistida.
  - `persisted`: resultado escrito en `result_json` de `challenge_participants`.
  - `verified`: resultado comprobado contra la cadena de procedencia (operaciones confirmadas, secuencia durable, checkpoints válidos).
  - `confirmed`: resultado validado y publicado como oficial del desafío.
  - `disputed`: resultado bajo disputa, congelado hasta resolución.
  - `invalidated`: resultado anulado por resolución administrativa.
- **Matriz de transiciones del resultado (F7-D-I1-FIX10-A):** válidas `calculated → persisted → verified`, `verified → confirmed`, `verified/confirmed → disputed`, `disputed → confirmed` (resolución favorable) y `disputed → invalidated` (resolución desfavorable); `invalidated` es terminal. Prohibidos los saltos `calculated → confirmed` y `persisted → confirmed`, la confirmación ordinaria de `disputed`, cualquier reapertura de `invalidated` y la invalidación directa `verified → invalidated` o `confirmed → invalidated`: el contrato solo alcanza `invalidated` mediante disputa previa y resolución administrativa (DEC-F7D-18 `disputed → resolved`; CONTRACTS §17.7/§17.14). La secuencia `calculated → persisted → verified` se materializa dentro de la transacción atómica de completitud (`running → completed`), con el COMMIT como punto de durabilidad, sin transiciones artificiales fuera de ese flujo. `result_status` es la fuente de verdad; `result_verified` se sincroniza en cada transición (`calculated=0`, `persisted=0`, `verified=1`, `confirmed=1`, `disputed=0`, `invalidated=0`).
- **Registro durable de procedencia (F7-D-I1-FIX10-A-FINAL):** las operaciones globales de desafío dejan un registro propio en la tabla `challenge_operations` con los campos que exige el contrato (desafío, operación, actor autenticado, identidad de petición `client_idempotency_key` + `request_hash`, resultado `outcome` y timestamp durable), insertado en la MISMA transacción que el cambio de estado: si el COMMIT falla no queda ni estado parcial ni registro huérfano. El ámbito de idempotencia es `(challenge_id, actor, client_idempotency_key)` (CONTRACTS §17.13): misma clave con hash distinto se rechaza con `IdempotencyConflict` (409) antes de cualquier retorno temprano por estado, y misma clave con el mismo hash no duplica registros. NO se reutiliza `operation_resolutions` ni la identidad/secuencia T1/T2 por mundo (DEC-F7D-15): la semántica de `operation_resolutions` está ligada a `world_operations`, no a operaciones de ámbito desafío. No constituye una arquitectura general de auditoría ajena a F7-D.
- **Restricciones:** un hash o checkpoint por sí solos **no** prueban la validez de una ejecución; verifican integridad del estado, no la corrección competitiva. Si alguna fase de la cadena no puede verificarse con la infraestructura actual, se documenta como capacidad pendiente, no como garantía implementada.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación en curso (F7-D-I1-FIX10-A: estados y transiciones del resultado implementados y probados, matriz documentada en CONTRACTS.md §17.14; F7-D-I1-FIX10-A-FINAL: registro durable de procedencia `challenge_operations` con atomicidad e idempotencia implementado y probado en `tests/test_f7d_provenance.py`).
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17).

## DEC-F7D-20 — Privacidad e identificación en rankings públicos (F7-D)

- **Decisión:** política explícita y conservadora compatible con DEC20. Un desafío público **no** vuelve públicos automáticamente los mundos de sus participantes. No se amplía `PUBLIC_WORLD_FIELDS` ni se expone `state_json` de terceros.
- **Contrato (qué puede consultar cada perfil):**
  - **Visitante anónimo:** información pública del desafío, estado general, posición y métricas autorizadas, resultados agregados o identificadores públicos no reversibles. **No** `world_id`, `state_json`, eventos internos completos, datos privados del participante, tokens de autenticación, ni datos de mundos ajenos fuera de la allowlist.
  - **Participante autenticado:** su propia inscripción, su propio resultado, su propio mundo conforme a permisos existentes. **No** acceso automático a mundos privados de otros participantes.
  - **Organizador:** información necesaria para administrar su desafío y verificar resultados, dentro de los límites DEC20 para mundos privados de terceros. La condición de organizador **no** es autorización general para leer cualquier mundo privado.
  - **Administrador:** autorización real del servidor (`is_admin` de F7-C-I1-FIX2), no información de rol suministrada por el cliente.
- **Identidad pública:** el ranking muestra alias público o identificador seudónimo estable, o posición sin identidad. **No** se expone `user_id` ni datos personales innecesarios.
- **Restricciones:** compatible con DEC20; no se amplía `PUBLIC_WORLD_FIELDS` por conveniencia; allowlists específicas para respuestas públicas de desafíos, separadas de los mundos.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17), aplicando DEC20.

## DEC-F7D-21 — Comparación estadística reproducible vs competición real (F7-D)

- **Decisión:** se distinguen dos conceptos conceptualmente diferentes:
  1. **Comparación estadística reproducible:** presentación de métricas obtenidas de simulaciones reproducibles. Puede incluir resultados calculados por el servidor, métricas comparables, ordenaciones estadísticas, agrupaciones, filtros y visualizaciones de diferencias entre resultados. Su finalidad es comparar ejecuciones, no demostrar habilidad de participantes. Puede existir aunque los participantes no tengan capacidad de intervenir en la simulación.
  2. **Competición real:** evaluación de participantes cuyas decisiones o acciones influyen de forma verificable en los resultados. Requiere, como mínimo, agencia efectiva del participante, acciones autenticadas y atribuibles, validación del servidor, persistencia durable, reglas competitivas definidas, resultados calculados a partir de esas acciones y procedimiento verificable de evaluación. La arquitectura actual **no satisface estos requisitos**.
- **Modalidades:** la comparación estadística y la competición real son modalidades conceptualmente diferentes, pero **no son necesariamente excluyentes en el conjunto del producto EvoWorld**. Un desafío concreto debe declarar explícitamente su modalidad y aplicar exclusivamente las reglas correspondientes. La arquitectura actual de F7-D **Nivel 1 solo permite comparación estadística de simulaciones reproducibles**. Mientras no exista agencia efectiva y verificable, no se puede atribuir mérito competitivo a participantes. La incorporación futura de competición real requerirá una decisión arquitectónica adicional y un contrato específico. La coexistencia futura de ambas modalidades no autoriza a mezclar métricas estadísticas con resultados competitivos ni a cambiar retrospectivamente la modalidad de un desafío.
- **Contrato:**
  - Se mantiene DEC-F7D-13: no existe actualmente un mecanismo de intervención del usuario que permita una competición significativa.
  - Se permite que F7-D se implemente como comparación estadística de simulaciones reproducibles, sin atribuir mérito competitivo.
  - Está prohibido denominar "ranking competitivo" a una ordenación estadística.
  - Está prohibido presentar una posición como evidencia de habilidad.
  - Está prohibido afirmar que un participante superó a otro mediante decisiones propias cuando no existe agencia.
  - Está prohibido utilizar expresiones de victoria, derrota o superioridad competitiva sin reglas y acciones que las fundamenten.
  - Si la interfaz utiliza términos como "clasificación", "posición" o "resultado", debe aclarar que son métricas comparativas de simulaciones.
- **Restricciones:** no se crea un sistema de agencia en esta intervención. Si en el futuro se desea una competición real con agencia del participante, requiere una decisión arquitectónica nueva y una implementación de mecanismo de decisiones del usuario, que queda fuera de F7-D.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada.
- **Origen:** F7-D-R1-B corrección contractual (2026-11-17).

## DEC-F7D-22 — Semántica de resultados sin agencia del participante (F7-D)

- **Decisión:** el resultado de un participante representa exclusivamente las métricas calculadas a partir de su simulación asociada y de las reglas estadísticas comunes.
- **Contrato:**
  - El servidor calcula las métricas.
  - El cliente no suministra resultados autoritativos.
  - La configuración común permite comparar ejecuciones bajo condiciones conocidas.
  - La semilla y la configuración idénticas pueden producir resultados idénticos.
  - La existencia de resultados distintos no demuestra por sí sola mérito o habilidad.
  - Las diferencias deben interpretarse según las condiciones efectivas de cada ejecución.
- **Terminología de estados de resultado:**
  - `calculated`: métricas calculadas desde el estado durable.
  - `persisted`: métricas almacenadas durablemente.
  - `verified`: se verificó la procedencia e integridad de los datos conforme al contrato. Acredita verificaciones técnicas; no acredita habilidad.
  - `confirmed`: el resultado estadístico quedó confirmado e inmutable. Confirma el resultado estadístico; no certifica una victoria competitiva.
  - `disputed`: existe una controversia pendiente sobre el resultado o su procedencia.
  - `invalidated`: el resultado dejó de ser válido según un procedimiento autorizado.
- **Cadena de procedencia:** prueba la relación entre configuración, operaciones, eventos, checkpoints y métricas, pero no prueba mérito competitivo. Una disputa no debe permitir sustituir silenciosamente un resultado confirmado.
- **Regla permanente:** ninguno de los estados `calculated`, `persisted`, `verified`, `confirmed`, `disputed` o `invalidated` demuestra por sí mismo habilidad, mérito, superioridad o victoria competitiva. Incluso en una futura modalidad competitiva, `verified` acreditará exclusivamente verificaciones técnicas y `confirmed` acreditará exclusivamente la confirmación formal del resultado. El mérito competitivo requerirá reglas de evaluación explícitas, acciones atribuibles al participante, evidencia durable y cálculo conforme a esas reglas; la evaluación competitiva será una propiedad del resultado y de sus evidencias, nunca una inferencia derivada del estado `verified` o `confirmed`.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada.
- **Origen:** F7-D-R1-B corrección contractual (2026-11-17).

## DEC-F7D-23 — Clasificación estadística pública (F7-D)

- **Decisión:** se permite una clasificación estadística pública con límites claros. La clasificación ordena resultados por métricas deterministas y públicamente documentadas.
- **Contrato:**
  - Métricas utilizadas, dirección de ordenación y tratamiento de empates se documentan públicamente.
  - Reglas de inclusión y exclusión definidas.
  - Estados de resultado que pueden aparecer: solo `confirmed` (y provisionales claramente identificados si se usa una vista informativa separada, nunca mezclados con confirmados).
  - Resultados disputados (`disputed`) o invalidados (`invalidated`) no aparecen en la clasificación confirmada.
  - Identidad pública mediante alias o seudónimo estable. No se expone `user_id`, `world_id`, `state_json` ni datos privados.
  - Etiquetas prohibidas: "campeón", "ganador", "derrotado", "mejor jugador", "habilidad superior".
  - Etiquetas permitidas: "posición estadística", "métrica comparada", "resultado de simulación", "diferencia observada", "ejecución comparable".
- **Restricciones:** compatibles con DEC20; no se amplía `PUBLIC_WORLD_FIELDS`; la clasificación no implica superioridad competitiva.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada.
- **Origen:** F7-D-R1-B corrección contractual (2026-11-17).

## DEC-F7D-24 — Niveles de evolución de F7-D (F7-D)

- **Decisión:** F7-D define dos niveles de evolución:
  - **Nivel 1 — Comparación estadística reproducible:** es el alcance implementable de F7-D bajo la arquitectura actual. Incluye entidades, inscripción, preparación, ejecución, resultados, persistencia, privacidad y clasificación estadística conforme a los contratos. No representa una competición basada en decisiones de participantes.
  - **Nivel 2 — Competición con agencia:** queda expresamente fuera del alcance actual. Requiere una nueva decisión arquitectónica que defina modelo de acciones de participantes, validación y autorización, aplicación durable y determinista, atribución de acciones, reglas competitivas y evaluación/resolución de resultados.
- **Contrato:**
  - El Nivel 2 no es una dependencia artificial para implementar el Nivel 1.
  - El Nivel 1 puede implementarse sin el Nivel 2.
  - No se implementa ni diseña en detalle el Nivel 2 durante F7-D-R1-B.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada.
- **Origen:** F7-D-R1-B corrección contractual (2026-11-17).

 
## DEC-F7E-01 — Integración de mundos destacados en la galería pública (F7-E.4)

- **Decisión:** la galería pública identifica y prioriza los mundos que tienen un destacado activo y vigente. La representación pública incorpora exclusivamente el booleano derivado is_highlighted; no se persiste como atributo de WorldRow.
- **Contrato de consulta y representación:**
  - La consulta de galería pública incluye únicamente mundos públicos y no eliminados lógicamente.
  - is_highlighted es siempre un booleano calculado a partir de la existencia de un destacado activo cuya vigencia no haya expirado. No se exponen el ID del registro destacado, fechas de concesión, identidad del administrador ni otros metadatos internos.
  - La marca se incorpora a la proyección pública de la galería y a la ficha pública equivalente servida por HTTP/WS. No cambia la autorización, el alcance de suscripción ni los datos privados disponibles por HTTP o WS. Las proyecciones privadas de mine=1 conservan su contrato vigente y no reciben este campo por esta decisión.
  - Un destacado retirado o vencido deja de marcarse y de recibir prioridad. La retirada o expiración no elimina el historial.
- **Orden y paginación:** orden total por is_highlighted DESC, id DESC. El cursor es opaco, versionado y compuesto por el valor booleano y el ID de la última fila; el servidor valida su estructura y su compatibilidad con el contexto normalizado de consulta. Un cursor inválido o incompatible se rechaza con HTTP 400. La continuación respeta primero el resto de mundos destacados por ID descendente y luego los no destacados por ID descendente.
- **Filtros y contexto del cursor:** mine, search, mode y limit forman parte del contexto normalizado vinculado al cursor. Cambiar cualquiera de ellos obliga a iniciar una paginación nueva sin reutilizar el cursor anterior. Se conservan los límites y validaciones vigentes de F7-B.
- **Alcance del filtro:** no existe un parámetro `highlighted=1` para filtrar la galería. `is_highlighted` es informativo y la priorización se aplica automáticamente, sin excluir los mundos no destacados.
- **Consistencia entre páginas:** cada página evalúa el estado vigente de los destacados al momento de su consulta. No existe snapshot transaccional entre páginas; si un destacado se concede, retira o expira durante la paginación, el orden puede cambiar y no se garantiza ausencia de duplicados o saltos entre páginas. El cliente debe reiniciar la consulta para obtener una vista ordenada coherente.
- **Privacidad y autoridad:** destacar un mundo no lo hace público ni concede permisos adicionales. Se preservan DEC20/DEC21, la allowlist pública y los contratos F7-E.1, F7-E.2-I y F7-E.3. La elegibilidad y la autoridad administrativa siguen gobernadas por F7-E.3.
- **Restricciones:** no exponer metadatos internos de destacados; no persistir un booleano redundante en WorldRow; no cambiar la visibilidad ni las autorizaciones; no borrar historial; no modificar el contrato de mundos privados; no introducir rankings globales.
- **Resolución H2 — expiración y unicidad:** al conceder un destacado, el servicio administrativo debe, dentro de la misma transacción que revalida elegibilidad y crea el nuevo registro, localizar el destacado anterior que siga `is_active=true` pero cuyo `expires_at` ya haya vencido y cambiarlo a `is_active=false` antes de insertar el nuevo. La fila anterior se conserva como historial. La galería y cualquier GET permanecen de solo lectura: no realizan esta limpieza. Un destacado todavía vigente no se desactiva implícitamente para crear otro.
- **Resolución H3 — checkpoint elegible:** la elegibilidad selecciona el checkpoint íntegro más reciente entre los registros con `is_valid=true`: consultar candidatos en orden `sequence DESC`, comparar el SHA-256 del payload almacenado sin modificar filas y aceptar el primero cuyo checksum coincida. Si ninguno coincide, el mundo no es elegible. Un candidato corrupto no se marca como inválido ni se persiste ninguna corrección durante esta evaluación; no se llama a `get_latest_valid_checkpoint()` ni a `RecoveryManager.recover()`.
- **Estado:** Aprobada / implementada y aceptada definitivamente (2026-11-24). Validación focalizada informada: 19 passed, 64 deselected, 1 warning, 0 failed. La regresión global conserva fallos históricos de otras áreas y no se declara limpia.
- **Origen:** decisiones confirmadas por el usuario para F7-E.4 (2026-11-24).
