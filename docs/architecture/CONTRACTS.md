# CONTRACTS.md — Contratos técnicos vinculantes de EvoWorld

Contratos operativos que cualquier implementación debe respetar. Este archivo **no duplica** DECISIONS.md (allí vive el "porqué" y el estado de aprobación); aquí vive el "cómo" verificable. Cada sección apunta al código que lo implementa para que la revisión se haga contra el código real, no contra este texto.

---

## 1. Ciclo de vida de RecoveryCoordinator (DEC01)

- Estados: `NOT_STARTED → IN_PROGRESS → COMPLETED | FAILED`; `COMPLETED → SHUTTING_DOWN → CLOSED | CLOSE_INCOMPLETE`; `FAILED → NOT_STARTED` **solo** tras rollback limpio con recursos originales vivos y verificados (`_can_retry()`).
- `CLOSED` y `CLOSE_INCOMPLETE` son **terminales**.
- Las transiciones solo ocurren por los métodos del coordinador; cualquier otra secuencia es un defecto.
- Un guard al inicio de `startup()` rechaza una segunda ejecución sobre la misma instancia.
- El coordinador solo cierra `PersistenceService` cuando `owns_persistence=True`.
- Código: `src/worldsim/server/recovery_coordinator.py` (`CoordinatorState`, `_TRANSITIONS`).

## 2. Startup en dos etapas (DEC06)

- **Etapa A (secuencial, nada se inicia):** por cada mundo persistido → `RecoveryManager.recover(world_id)` → si `ok`, `WorldRunner(engine=<instancia exacta>)` → `RunnerRegistry.register_existing()` → **sin** `start()`. Resultados `not_recoverable`/`no_checkpoints` → registro en memoria, sin runner ni motor sustituto (DEC04).
- Fallos individuales se **aislan** y el startup continúa; una excepción de infraestructura **aborta** y dispara rollback.
- **Etapa B (solo si A terminó por completo):** iniciar la tarea de **todos** los runners recuperados (`running` y `paused`). Verificar: tareas creadas vivas, runners esperados registrados, ningún runner pausado sin su pausa. Solo entonces → `COMPLETED`.
- Rollback (ante fallo **o** cancelación) de A o B: cancelar y **esperar** tareas principales, drenar auxiliares, desregistrar, cerrar solo recursos de propiedad exclusiva (`asyncio.to_thread`, referencia retenida hasta el fin real), acumular errores de cleanup, terminar en `FAILED` re-propagando la **excepción original**. Cleanup en tarea propia con `asyncio.shield`.
- `asyncio.CancelledError` durante el startup ejecuta el rollback COMPLETO **antes** de propagarse; nunca se convierte en éxito ni se absorbe como error ordinario.
- Código: `recovery_coordinator.py` (docstring Etapa A/B, `_rollback_guarded`).

## 3. Recuperación de mundos y runners pausados (DEC06/DEC12/DEC04)

- El motor recuperado se inyecta **exactamente** (`engine=result.engine`), nunca se reconstruye (DEC12).
- Un runner pausado queda **registrado**, con su **tarea principal viva** y `runner.paused=True`; `_run` comprueba la pausa en cada iteración y hace `continue` SIN tick.
- `RecoveryManager.recover()` es el responsable de reconstruir el motor y determinar el resultado; el coordinador solo orquesta.
- Importar `worldsim.server.db` no crea ni modifica el esquema; `db.ensure_schema(engine)` es explícito al inicio del startup (DEC02).
- Consulta HTTP 409/503 sobre `not_recoverable`: **Etapa 5, aún no implementada**.

## 4. Contrato de RunnerRegistry (DEC11/DEC10)

- Singleton `world_id → WorldRunner` por proceso; métodos **síncronos = atómicos** en el event loop único (sin locks propios del registry).
- `register_existing(world_id, runner)`: valida tipos y asociación, **rechaza duplicados** (nunca sustituye en silencio), **no inicia** el runner, **rechaza** mundos en eliminación (`RuntimeError`).
- `get_existing(world_id)`: **nunca crea**; lecturas permitidas durante eliminación.
- `get(world_id)`: conserva creación perezosa (misma instancia por mundo) y **rechaza** mundos en eliminación.
- `unregister(world_id)`: solo remueve; **nunca cierra** recursos implícitamente.
- Coordinación de eliminación (DEC10): `begin_delete()` fija la marca **síncronamente** (incluso para un caller aún esperando el lock) y toma un `asyncio.Lock` **por mundo** (FIFO, sin lock global); `end_delete()` cuenta participantes (la marca sobrevive hasta el último); la liberación ocurre en `finally` o, si el caller es cancelado con escritura persistida en curso, se transfiere al callback de finalización de esa tarea.
- Código: `src/worldsim/server/runner.py` (`RunnerRegistry`).

## 5. Contrato de WorldRunner y close() (DEC18/DEC12)

- Estados: `NOT_STARTED → RUNNING → CLOSING → CLOSED | CLOSE_INCOMPLETE`; `CLOSING` se fija **síncronamente** antes del primer `await` de `close()`.
- `CLOSED` y `CLOSE_INCOMPLETE` son **terminales**: nunca ejecutan ticks ni se reinician.
- `close()` es **definitivo, idempotente y seguro ante concurrencia**; drena de verdad tarea principal y auxiliares.
- Timeout por grupo de tareas: `CLOSE_TIMEOUT = 5.0` s; excederlo → `CLOSE_INCOMPLETE` (nunca reportar como `CLOSED`), conservando referencias y evidencia.
- El motor se asigna en construcción; **no se sustituye** una vez comenzada la ejecución (asignarlo antes de `start()` está permitido).
- `start()` es la única creadora de la tarea principal; la reanudación de DEC15 **nunca** llama a `start()` ni crea tareas.
- Código: `runner.py` (`RunnerState`, `close()`).

## 6. Eliminación segura de mundos (DEC10)

- Secuencia obligatoria: **close → verificar close → unregister → borrar datos persistidos** (`safe_delete_world`). Prohibido borrar datos antes de verificar el cierre.
- Durante la sección crítica, `get()` y `register_existing()` rechazan el mundo; no existe lock global, solo el lock por mundo.
- `app.py` (DELETE): cierra el runner, verifica el cierre, lo desregistra y solo entonces borra.
- Protocolo de resultados verificable por tests (Etapa 2 §11, 1-10); la ruta HTTP que los traduce existe en `app.py`.
- Código: `runner.py:808+`, `app.py:388`.

## 7. Cancelación y reconciliación de operaciones (Etapa 4 / DEC14)

- Cada operación recibe una **generación monotónica por mundo** al entrar (`begin`); antes de aplicar cambios en memoria se verifica la vigencia: una operación antigua **nunca** sobrescribe el estado de una más reciente.
- La escritura persistida corre en **worker thread** (`asyncio.to_thread`) sobre el contrato público de `WorldRepository`; el task conserva referencia fuerte (`_writes`) hasta su finalización real.
- **Cancelación:** `asyncio.shield` protege la espera; si el llamador se cancela, la operación espera la finalización **real** de la escritura, determina su resultado efectivo, reconcilia, libera el lock y **solo entonces** propaga la `CancelledError` original. `Future.cancel()` no significa fin del trabajo del executor: el task se observa hasta `done()`.
- **Reconciliación:** (1) escritura confirmada + aceptada → estado operativo se actualiza; (2) escritura confirmada + rechazada (eliminación alcanzada, `close()` iniciado u operación obsoleta) → **restaurar** el valor previo de `WorldRow.status` antes de liberar el lock; si la fila ya no existe, no hay nada que reconciliar; si la restauración falla, el error se conserva en `reconcile_errors` + log (nunca se oculta); (3) escritura falló → nada se tocó, el error original llega al llamador.
- **Eliminación y cierre:** validaciones + arranque de escritura en un bloque **sin `await` intermedio** (atómico en el event loop); la puerta de aplicado se re-evalúa síncronamente tras cada suspensión.
- Un error **no** deja locks retenidos ni estados contradictorios silenciosos.
- Código: `src/worldsim/server/transitions.py` (docstring §4.1-§4.6).

## 8. Contrato de snapshots (DEC13)

- **Independencia:** la congelación reconstruye cada contenedor; sin referencias mutables al motor ni de origen, ni expuestas al consumidor.
- **Inmutabilidad profunda:** `dict → MappingProxyType` (sobre dict exclusivo), `list/tuple → tuple`, escalares ya inmutables.
- **Serialización canónica:** byte a byte idéntica a `checkpoint_to_json` para el mismo estado (UTF-8, `ensure_ascii=False`, `indent=2`, `separadores=(",", ": ")`, `allow_nan=False`, orden de inserción de `checkpoint_to_dict`); claves solo `str`; datos inválidos → `SnapshotError` con la **ruta exacta**, nunca descarte ni conversión silenciosa.
- **Consistencia:** copia en una **única llamada síncrona** a `checkpoint_to_dict` en el hilo propietario (o con lock proporcionado por el llamador, sostenido SOLO durante la copia); sin mutación intercalada, sin bloqueo nuevo.
- El módulo **no** importa SQLAlchemy ni `worldsim.server`; es puro cómputo en memoria.
- Código: `src/worldsim/export/snapshot.py`.

## 9. Pausa y reanudación por mundo (DEC08/DEC14/DEC15)

- Tres estados que deben mantenerse coherentes: `WorldRow.status` (**autoridad persistida**), `WorldRunner.paused` (control operativo), `SimEngine.paused` (**espejo**, nunca autoridad).
- Valores persistidos: `WorldRow.status ∈ {"paused", "running"}`.
- **Exclusión por mundo:** `asyncio.Lock` FIFO por mundo; sin lock global; adquisición cancel-safe; liberación en `finally`; el lock de transición es **hoja** (nunca adquiere el lock de eliminación, solo lee `is_deleting`) → sin interbloqueo con `safe_delete_world`.
- **Escritura:** en worker thread sobre `WorldRepository`; referencia fuerte hasta el fin real.
- **Eliminación/cierre:** si `begin_delete` alcanzó al mundo o el runner está en `CLOSING/CLOSED/CLOSE_INCOMPLETE`, la transición **no** modifica el estado operativo (y revierte la fila si escribió).
- **Sin segunda ruta de ticks:** la pausa solo fija `WorldRunner.paused`; `_run` sigue siendo el único camino de ticks. La reanudación no llama a `start()` ni crea tareas.
- `TransitionCoordinator.shutdown()` impide nuevas transiciones y espera a las en curso (una ya identificada con `begin()` termina), sin abandonar escrituras.
- Orden exacto: `begin(generación) → lock por mundo → validaciones (atómicas) → escritura en worker thread → [si caller cancelado: espera real] → resultado efectivo → puerta de aplicado → reconciliación si rechazó → liberar lock → resultado o error`.
- **No se tocan:** formato de checkpoint, DEC13, DEC19, semántica de recuperación, DEC06, DEC18, DEC10.
- Código: `transitions.py`.

## 10. Shutdown (DEC17 — Etapa 6, pendiente)

- El shutdown global **drena y cierra** todos los runners de forma verificada → `CLOSED` (o `CLOSE_INCOMPLETE`).
- El coordinador ya expone `SHUTTING_DOWN → CLOSED|CLOSE_INCOMPLETE` y sus métodos de transición; en las etapas actuales **no se cierra ningún recurso en un shutdown normal**.
- El lifespan de `app.py` ya drena las transiciones de pausa de la Etapa 4 (rechaza nuevas, espera las activas); el drain/close global **sigue perteneciendo a la Etapa 6**.
- Código: `recovery_coordinator.py:46-51,683-698`; `app.py:91-102`.

## 11. Compatibilidad de checkpoints

- Formato JSON versionado: `_checkpoint_version: 1`, `_checkpoint_type: "simengine"`, con codificación explícita de arrays NumPy, RNG, sets, tuples, enums y dataclasses.
- `checkpoint_to_dict` es el **contrato público** usado por snapshots (DEC13) y por la persistencia (`PersistenceService`); `engine_to_dict` se conserva inalterado para la vista del cliente y **no** es el contrato de restauración.
- Restauración exacta del motor: `checkpoint.py` es el módulo exclusivamente para restauración exacta.
- La Etapa 4 no altera el formato; cualquier cambio de versión es un evento de compatibilidad que debe considerarse aparte.
- Código: `src/worldsim/export/checkpoint.py`.

## 12. Política de errores HTTP y WebSocket (DEC16)

- Clasificación explícita (nunca 404/409/500 indiscriminados):
  - mundo inexistente → **404** (WS: cierre **4404**).
  - mundo existente con resultado `not_recoverable` del `RecoveryCoordinator` → **409** (WS: cierre **4409**).
  - parámetros inválidos → **400** (WS: cierre **4400**).
  - fallo temporal de infraestructura → **503** (WS: cierre **4503**).
  - Traducción WS: `4400 + (status - 400)`.
- `RequestValidationError` → **400 JSON** (reemplaza el 422 por defecto).
- `OperationalError` / `InterfaceError` → **503**, con el error registrado vía `logger.error` (regla 8: nunca se oculta ni se presenta como éxito).
- `WorldRow.status` sigue siendo la fuente persistente del estado del mundo. `not_recoverable` es un resultado operativo del coordinador en memoria, **nunca** un valor de `WorldRow.status` (DEC08/DEC16); no se persiste ni se cambia la lógica de recuperación 1A/1B.
- **DEC09 — WS sin ticks:** `ws_world` nunca avanza la simulación. La única ruta ordinaria de ticks es `WorldRunner._run()`. Se eliminó el fast-forward (`for _ in range(row.months_simulated): runner.eng.tick()`) y cualquier `runner.engine.tick()` del handler WS. `runner = None` se inicializa antes del `try` (evita `NameError` en cancelación/desconexión). Los mensajes WS inválidos cierran con 4400. Se preservan los mensajes válidos del protocolo (`speed`, `pause`, `selected`), suscripción/notificación, pausa/reanudación vía `pause_world`/`resume_world` (Etapa 4 / DEC14) y limpieza de suscriptores ante `WebSocketDisconnect`/`CancelledError`.
- **§5 — sin runners lazy para no recuperables:** antes de tocar la fila o crear runner, `ws_world`, `post_prediction`, `post_chat` y `post_claim` aplican `_require_recoverable(world_id)` (409 si hay registro `not_recoverable`). DELETE está exento del 409 (DEC10: la eliminación segura debe funcionar para mundos no recuperables) y descarta el registro tras borrado confirmado (`_forget_recoverable`) para no heredar un 409 stale al reusar `world_id`.
- Errores de dominio existentes se propagan tal cual (close fallido / persistencia fallida = error existente, `app.py`).
- En la cancelación durante una transición, la `CancelledError` original se propaga al llamador (regla 5 / Etapa 4 §4.6).
- **Nota:** la ruta HTTP/WS completa no es testeable en este entorno sin el extra `server` (DEC07 / fastapi ausente); los tests de integración usan `pytest.importorskip` y SKIP en su lugar de error. Verificación disponible: `py_compile`, tests estáticos de `app.py`/`errors.py` y suites que no requieren FastAPI.
- Código: `src/worldsim/server/app.py`, `src/worldsim/server/errors.py`.

## 13. Shutdown global y cierre de ciclo de vida (DEC17 / DEC18 / DEC05)

- **Flujo efectivo (lifespan, `app.py`):** al terminar el cuerpo de la aplicación (incluso si terminó con excepción):
  1. `transition_coordinator.shutdown()` — drena transiciones de pausa/reanudación Etapa 4: rechaza nuevas, espera las en curso, no abandona escrituras.
  2. `RecoveryCoordinator.shutdown()` — cierre global ordenado (DEC17):
     a. Cierra todos los runners registrados vía `WorldRunner.close()` (DEC18): asíncrono, idempotente, definitivo, no reiniciable, espera terminación real de tareas principal y auxiliares. Un fallo individual no abandona los demás cierres; los errores se acumulan.
     b. Drena writes pendientes de persistencia (`PersistenceService.wait_pending_writes`) — **sin** generar checkpoint final automático (DEC05).
     c. Si el coordinator es dueño exclusivo de la persistencia (`owns_persistence=True`), cierra `PersistenceService` (drena writes + cierra el `ThreadPoolExecutor`).
  3. Transición terminal: `SHUTTING_DOWN → CLOSED` si todo cerró limpio; `→ CLOSE_INCOMPLETE` si hubo cualquier error o timeout. Nunca presentado como éxito.

- **Estados del coordinator (`RecoveryCoordinator.state`):** `NOT_STARTED → IN_PROGRESS → COMPLETED → SHUTTING_DOWN → CLOSED | CLOSE_INCOMPLETE`. `CLOSED` y `CLOSE_INCOMPLETE` son terminales.

- **Estados del runner (`RunnerState`):** `NOT_STARTED → RUNNING → CLOSING → CLOSED | CLOSE_INCOMPLETE`. `CLOSING` se fija síncronamente antes del primer `await`. Un runner cerrado nunca vuelve a ejecutar ticks ni se reinicia.

- **Cancelación durante shutdown:** una cancelación no abandona tareas de cierre en curso; cada operación conserva su referencia fuerte y se espera su terminación real. La cancelación del llamador nunca se presenta como éxito.

- **Propiedad:** cada recurso es cerrado por su propietario. No se cierra dos veces el mismo recurso mediante rutas independientes. No se crean runners ni se inicia recuperación durante shutdown.

- **Código:** `src/worldsim/server/app.py` (`lifespan`), `src/worldsim/server/recovery_coordinator.py` (`shutdown`, `begin_shutdown`, `finish_shutdown`), `src/worldsim/server/runner.py` (`RunnerRegistry.all_runners`, `WorldRunner.close`).

## 14. Visibilidad de mundos (DEC20 / DEC21 — F7-A)

> **Estado:** **implementada y verificada** (F7-A implementación, 2026-11-16). Columna `visibility` con CHECK y migración en `src/worldsim/server/db.py`; rutas, compuerta de autorización y representación acotada en `src/worldsim/server/app.py`; suite `tests/test_visibility.py` (30 pruebas, checklist §14.8 en verde) más las suites Etapa 5/auth/server/multiplayer/league adaptadas a la creación autenticada.

### 14.1 Atributo y valores

- Cada mundo tiene un atributo persistido `visibility` (columna sobre `WorldRow` en `src/worldsim/server/db.py`; **implementada en F7-A**) con exactamente dos valores válidos: `private` y `public`. Todo otro valor o su ausencia es inválido (§14.2.3).
- `visibility` es independiente de `WorldRow.status` (autoridad de estado/pausa, DEC14): nunca se almacena en `status` y no afecta al runner, a los checkpoints, a la recuperación ni al shutdown (DEC05/DEC06/DEC17 intactos).
- **Predeterminados:** los mundos nuevos son `private`; su **creación exige autenticación** y establece como propietario al `user_id` de la sesión autenticada (**no** se crean mundos privados sin propietario). Los mundos preexistentes quedan `public` vía migración explícita (compatibilidad transitoria de enlaces y comportamientos existentes, retirable por su propietario).

### 14.2 Migración de los registros existentes (implementada en F7-A — `ensure_schema` en `db.py`)

1. **Identificación de preexistentes:** la migración agrega la columna `visibility` (NULL en todas las filas existentes) y considera preexistentes **todas las filas con `visibility IS NULL`** al ejecutarse. Su valor resultante es **`public`** (asignación explícita y auditable; decisión 3).
2. **Mundos posteriores:** desde la habilitación, toda creación escribe **`private`** de forma explícita, con `DEFAULT 'private'` en esquema como defensa adicional; ninguna creación depende de la ausencia de valor.
3. **Ausente o inválido → nunca público (`fail-closed`):** en ejecución, `NULL` o un valor fuera de `{private, public}` se interpreta como `private` y se registra `logger.error` (regla 8). Se exige una restricción CHECK (o validación equivalente) que impida persistir valores fuera del dominio.
4. **Preservación de datos:** la migración solo escribe la columna `visibility`; no modifica ni elimina ninguna otra columna ni fila.
5. **Interrupción y repetición:** idempotente (actúa solo sobre `visibility IS NULL`); re-ejecutarla no altera filas ya migradas; una interrupción deja filas NULL que la re-ejecución completa, sin producir valores parciales inválidos.
6. **Verificación previa a habilitar:** las rutas con enforcement y el default `private` en creación se habilitan **solo después** de comprobar: (a) 0 filas NULL y 0 valores inválidos; (b) muestra de IDs conocidos responden `public`; (c) el listado sin filtro muestra los mismos mundos que antes de la migración (compatibilidad de enlaces existentes); (d) la suite de pruebas en verde. Hasta entonces el comportamiento observado es el actual (todo legible), evitando tanto la publicación accidental como el ocultamiento accidental.
7. **Mundos preexistentes sin propietario** (creados anónimamente): migran a `public` como cualquier otro preexistente; al no tener propietario **no pueden retirarse de publicación** (solo eliminación). Consecuencia conocida del alcance actual.

### 14.3 Matriz de autorización

Operación de publicación/retirada: `PUT /api/worlds/{world_id}/visibility` con cuerpo `{"visibility": "public" | "private"}` (operación que requiere identidad; PUT idempotente).

| Situación | Resultado |
|---|---|
| Sin autenticación en una operación que requiere identidad | **401** |
| Autenticado, mundo inexistente | **404** |
| Autenticado, no propietario, mundo **público** | **403** |
| Autenticado, no propietario, mundo **privado** | **404** (sin revelar existencia; resolución conjunta de las decisiones 6 y 7 leídas con la regla §6 de no exposición indirecta) |
| Autenticado y propietario | **200** con el valor nuevo |
| Cuerpo con valor fuera de `{private, public}` | **400** |
| DELETE anónimo, mundo con propietario | **401** |
| DELETE autenticado, no propietario, mundo **público** | **403** |
| DELETE autenticado, no propietario, mundo **privado** | **404** (sin revelar existencia) |
| DELETE autenticado y propietario | reglas de eliminación existentes (DEC10): `close → verificar → unregister → borrar` |
| Mundo sin propietario (`owner_user_id IS NULL`) | nadie puede cambiar su visibilidad ni borrarla por cuenta ajena (**403** autenticado); solo la creación/migración fija su valor; borrado de mundo sin propietario conserva el comportamiento actual mientras exista el caso P-1 |

- **Consulta de un mundo privado por un tercero** (con o sin autenticación): **404**, indistinguible de un mundo inexistente, en **todas** las rutas del mundo (§14.5).
- **Eliminación (`DELETE`)**: anónima → **401**; autenticado no propietario de mundo **público** → **403**; autenticado no propietario de mundo **privado** → **404** (no-revelación); propietario → DEC10 sin cambios. Esto aplica a mundos con propietario; la creación anónima queda resuelta por P-1 (§14.9).
- **Prueba de propiedad:** exclusivamente la sesión del propietario (Bearer → `user_id` coincide con `WorldRow.owner_user_id`). Los `user_token` y cualquier identificador auto-declarado **no** valen como prueba de propiedad (DEC21).
- **Autenticación ≠ autorización:** 401 = no identificado; 403 = identificado sin permiso (propietario distinto en mundo público); 404 = inexistente o privado oculto al solicitante. Autenticarse nunca sustituye a comprobar la propiedad.
- **Orden de comprobación en toda ruta de mundo:** (1) existencia + visibilidad → (2) guarda DEC16 `_require_recoverable` (409) → (3) lógica de negocio (400/409/503). La visibilidad precede al 409 para que un tercero **nunca** reciba 409 (u otro código que revele existencia) por un mundo privado. Se conserva el propósito de §12 §5 (sin runner lazy, sin motor sustituto): la comprobación de visibilidad solo lee la fila y no crea runners. Para el propietario, DEC16 se aplica sin cambios.

### 14.4 Representación pública acotada

- **Ficha pública** (lista cerrada de campos permitidos en listado y metadatos de detalle): `id`, `name`, `mode`, `seed`, `months_simulated`, `land_pct`, `created_at`, `season_years`, `season_over`, `visibility`, y `mine` (solo cuando el solicitante es el propietario).
- **Detalle público:** la ficha + bloque de render del mapa base `world: {width, height, biome, elevation, rivers}` (lo necesario para visualizar el mapa). Los datos vivos (reinos, influencia, guerras, eventos, bestias, cronología) llegan por el WS en vivo solo para el **propietario** conectado (contrato de Etapa 5/DEC09); un tercero en un mundo público recibe únicamente un mensaje único de representación acotada y **nunca es suscrito** al runner, por tanto no recibe datos vivos (prohibición crítica de DEC20/P-2).
- **Prohibido en datos públicos:** `state_json` completo y cualquier campo no listado explícitamente arriba — en particular: `kingdoms`, `chronicle`, `wars`, `beasts`, `relations`, `fallen`, `influence`, `battle_heat`, `month`/`year` del snapshot de creación, `status`, `owner_user_id`, `username` del propietario, `enabled_races_json`, `sea_level`, `state_json`.
- **Detalle completo (`state_json` actual):** reservado al **propietario autenticado** de su propio mundo (el comportamiento actual del dueño no cambia).
- **Ampliar la lista cerrada exige una DEC nueva** (regla transversal 3). Si la implementación demuestra que el cliente necesita un campo adicional para ver mundos públicos, se solicita dicha DEC en lugar de exponerlo por conveniencia.
- **Alcance:** la representación acotada aplica a las **rutas HTTP y a WebSocket** por igual (política de privacidad uniforme). Un mundo público solo expone información incluida en la allowlist de DEC20; el **snapshot completo no se transmite a terceros por el mero hecho de que el mundo sea público**. El propietario, autenticado y autorizado, accede a los datos completos que le correspondan. No se debe conservar un payload anterior si contiene campos que DEC20 prohíbe exponer.
- **WebSocket:** una conexión de terceros a un mundo privado se cierra con código **4404** (no revela existencia); un mundo público acepta la conexión bajo el contrato de representación acotada: la conexión recibe **un único mensaje de ficha pública acotada**, es **nunca suscrita** al runner y por tanto no recibe datos vivos (ni por el handshake ni por ningún mensaje posterior); el WS no sirve `state_json` y su payload representa solo lo permitido en §14.4. El propietario autenticado recibe el stream completo de Etapa 5.

### 14.5 Rutas cubiertas por la compuerta de visibilidad

- **Lecturas de mundo para terceros** (no propietario): `GET /api/worlds` (lista **solo** `public`), `GET /api/worlds/{id}`, `.../stats/{kid}`, `.../predictions`, `.../leaderboard`, `.../chat`, `.../claims`, `.../standings` y la ruta WS `.../ws/{id}` (cierre **4404**). Mundo privado ajeno → **404/4404**; mundo público → comportamiento actual.
- **Escrituras de mundo por terceros** (chat, predictions, claims) en un mundo privado ajeno → **404** (también evitan revelar existencia). Para el propietario: sin cambios.
- **Creación (`POST /api/worlds`):** operación que requiere autenticación; el propietario es el `user_id` de la sesión autenticada y el mundo nace `private` por defecto (DEC20, resolución P-1).
- **No se modifican:** `DELETE` (regla de propiedad actualizada en §14.3), `/api/auth/*`, `/api/meta`, `/api/identity` y las operaciones internas de transición/pausa.
- `?mine=1` sigue exigiendo autenticación (401) y lista solo mundos del propietario, `private` o `public`: el propietario siempre ve los suyos.

### 14.6 Concurrencia

- La visibilidad se lee **por solicitud** desde la fila persistida; no se cachean decisiones de visibilidad entre solicitudes.
- Publicar/retirar es una **escritura atómica de una fila**: toda solicitud que comienza después de la escritura confirmada observa el valor nuevo; las solicitudes en curso al momento del cambio pueden completar con el valor anterior (no hay revocación retroactiva ni estados mixtos visibles).
- No se introducen locks nuevos ni se toman los locks por mundo de pausa (Etapa 4) o eliminación (DEC10); la única interacción es la existencia de la fila (si desaparece durante el cambio → 404).
- Un cambio de visibilidad no escribe `WorldRow.status`, no toca checkpoints ni crea cierra runners (DEC05/DEC14/DEC18 intactos).

### 14.7 Identidad competitiva (DEC21)

- Los rankings globales y los desafíos que atribuyan resultados exigen identidad **vinculada a cuenta** (Bearer → `user_id` persistido en el resultado). `user_token` no es identidad competitiva confiable.
- Los mecanismos por mundo existentes (chat, predictions, claims/standings) **no se modifican** en F7-A: sus contratos de etapas aceptadas siguen vigentes.
- Aplica a futuras subetapas (destacados, desafíos); su implementación queda fuera de F7-A.

### 14.8 Criterios de aceptación de la futura implementación

1. Crear un mundo autenticado lo deja `private`: no aparece en el listado anónimo y un tercero recibe 404 en detalle, stats, chat, claims, standings, predictions, leaderboard y WS (4404).
2. Tras la migración: 0 filas NULL, 0 valores inválidos; los IDs de muestra preexistentes responden `public` y sus enlaces siguen funcionando.
3. `PUT .../visibility` produce 401/403/404/200/400 exactamente según §14.3, incluida la no-revelación (404) ante mundos privados ajenos.
4. Ninguna respuesta pública contiene `state_json` completo ni campos fuera de la lista cerrada de §14.4 (aserción sobre el cuerpo de la respuesta).
5. Un tercero nunca recibe 409/400/503 que revele la existencia de un mundo privado (orden de §14.3).
6. Valores ausentes/inválidos se tratan como `private` + `logger.error` (`fail-closed`), verificado inyectando una fila inválida.
7. La migración es idempotente (re-ejecución sin cambios) y no toca otras columnas (comprobación por diff de filas).
8. El retiro es inmediato para solicitudes posteriores (sin caché) y no altera `status`, runner ni checkpoints.
9. Sin sesión → 401; con sesión ajena → 403/404 según §14.3; un `user_token` nunca concede permiso.
10. Regresión: DEC01–DEC19 intactos y suite global sin fallos nuevos (baseline de referencia: 443 passed / 1 skipped / 2 fallos preexistentes conocidos).
11. Cuestiones P-1…P-4 resueltas (§14.9); si alguna resolución exige adaptación de pruebas existentes, la adaptación se hará en la etapa de implementación con autorización explícita.
12. Sin dependencias nuevas, sin cambios en lockfiles ni configuración, sin commits no autorizados.

### 14.9 Cuestiones P-1…P-4 — resueltas (precisión contractual F7-A-R1)

Estas cuestiones quedaron pendientes en F7-A y fueron resueltas por el encargo F7-A-R1 (2026-11-15). Se incorporan como precisiones de DEC20 y DEC21, sin crear DEC22: no son decisiones arquitectónicas independientes.

- **P-1 — Creación autenticada (resuelta).** La creación de mundos nuevos exige autenticación. Todo mundo nuevo tendrá propietario identificado mediante la sesión autenticada (`owner_user_id` = `user_id` del propietario) y nacerá `private` por defecto. No se crearán mundos privados sin propietario. La implementación futura deberá adaptar los flujos y pruebas que dependan de creación anónima; no se eliminará funcionalidad existente sin identificar y documentar su impacto. Afecta a DEC20 y a `POST /api/worlds` (§14.5).
- **P-2 — WebSocket y representación pública (resuelta).** La política de privacidad es uniforme entre HTTP y WebSocket. Los mundos privados no son accesibles por terceros; una conexión de terceros a un mundo privado podrá cerrarse con código **4404**. Los mundos públicos solo exponen información incluida en la allowlist de DEC20 (nunca el snapshot completo por el mero hecho de ser públicos). El propietario, autenticado y autorizado, accede a los datos completos que le correspondan. El contrato WebSocket especifica qué representación recibe cada tipo de usuario; no se conserva un payload anterior si contiene campos que DEC20 prohíbe exponer. Afecta a §14.4 y §14.5.
- **P-3 — Precedencia de autorización (resuelta).** Orden de comprobación en toda ruta de mundo: (1) existencia + visibilidad → (2) guarda DEC16 `_require_recoverable` (409) → (3) lógica de negocio. Resultado explícito: mundo inexistente **404**; mundo privado consultado por un tercero **404**; mundo público consultado por un tercero en operación exclusiva del propietario **403**; propietario autenticado, acceso conforme a la operación y al estado del mundo. La visibilidad precede al 409 para evitar filtraciones indirectas sobre mundos privados. Afecta a §14.3.
- **P-4 — DELETE anónimo (resuelta).** Solicitud anónima de eliminación: **401**. Usuario autenticado no propietario de mundo **público**: **403**. Usuario autenticado no propietario de mundo **privado**: **404**. Propietario autenticado: reglas de eliminación existentes (DEC10). Afecta a §14.3.

- **Código:** implementado en F7-A (2026-11-16). Rutas afectadas: `src/worldsim/server/app.py` (compuerta de visibilidad, PUT visibility, DELETE matriz, WS reescrito con compuerta y representación acotada); atributo: `src/worldsim/server/db.py` (`WorldRow.visibility`, migración `ensure_schema` + CHECK + lógica fail-closed). Pruebas: `tests/test_visibility.py` (30, checklist §14.8); suites adaptadas: `test_etapa5_static`, `test_etapa5_http`, `test_auth`, `test_server`, `test_multiplayer`, `test_league`.

---

## 15. Galería pública de mundos (DEC-F7B-01 a DEC-F7B-10 — F7-B)

> **Estado:** contrato definido (F7-B-R1, 2026-11-16). Implementación **no iniciada**. Ningún código de galería existe aún; esta sección especifica el contrato futuro sobre la implementación de F7-A.

### 15.1 Alcance

- `GET /api/worlds` define dos modos de consulta independientes: **modo público** (predeterminado, sin `mine=1`) y **modo propietario** (`mine=1`).
- Modo público: devuelve exclusivamente mundos con `visibility = public`. Disponible para visitantes anónimos y usuarios autenticados. Nunca incluye mundos privados, incluso si pertenecen al usuario autenticado.
- Modo propietario (`mine=1`): requiere sesión autenticada. Devuelve exclusivamente mundos cuyo `owner_user_id` coincide con el `user_id` de la sesión. Puede incluir mundos públicos y privados propios. Nunca devuelve mundos pertenecientes a terceros. `user_token` no es prueba de propiedad.
- La galería pública y la consulta de mundos propios pueden compartir el formato de respuesta paginado, pero mantienen consultas y condiciones de acceso separadas.
- F7-B **no** incorpora mundos destacados, rankings globales, desafíos, sistemas de recomendación, votaciones, likes, comentarios ni reputación. Estas funcionalidades pertenecen a etapas posteriores (F7-C a F7-F).

### 15.2 Parámetros de consulta de `GET /api/worlds`

| Parámetro | Tipo | Predeterminado | Valores válidos | Comportamiento |
|---|---|---|---|---|
| `mine` | entero opcional | — | `1` | `mine=1` selecciona el modo propietario (requiere sesión). La ausencia selecciona el modo público. Otros valores no se reinterpretan como autenticación. |
| `limit` | entero opcional | 20 | 1..100 | Tamaño de página. Fuera de rango → 400. |
| `cursor` | entero opcional | — | cualquier entero | Punto de corte en el orden `id DESC`. Filtra `id < cursor`. No requiere que el registro del cursor exista ni sea público. Inválido (no entero) → 400. |
| `search` | cadena opcional | — | ≤ 64 caracteres | Búsqueda por `name` con `LOWER(name) LIKE LOWER(:search)`, escapando `%` y `_`. Vacío/solo espacios → sin búsqueda. |
| `mode` | cadena opcional | — | `single` \| `league` | Filtra por modo. Otro valor → 400. |

**Parámetros compartidos entre ambos modos:**
- `limit`: valor predeterminado 20, rango 1–100, aplicado en persistencia.
- `cursor`: punto de corte entero, sin requerir existencia del registro, aplicado junto con todos los filtros y condiciones de visibilidad/propiedad.
- `search`: búsqueda por nombre, con los límites y reglas de escape definidos en §15.6. Se combina con `visibility = public` (modo público) o con la condición de propiedad (modo propietario).
- `mode`: filtro opcional por `single` o `league`. Se combina con los demás filtros sin debilitar las condiciones de privacidad.

**Cambio de filtros entre páginas:** el cliente debe reiniciar la paginación (no enviar `cursor`) cuando cambie cualquiera de estos parámetros: `mine`, `search`, `mode`, `limit`. El cursor de una consulta anterior no se reutiliza como si perteneciera a una consulta diferente. El backend aplica todos los filtros de la solicitud actual; el cursor no puede eludirlos.

### 15.3 Respuesta paginada

Ambos modos utilizan el mismo formato de respuesta paginado, no una lista simple:

```json
{
  "worlds": [
    {
      "id": int,
      "name": str,
      "mode": str,
      "seed": int,
      "months_simulated": int,
      "land_pct": float,
      "created_at": str,
      "season_years": int,
      "season_over": bool,
      "visibility": "public" | "private",
      "mine": bool
    }
  ],
  "next_cursor": int | null
}
```

- `worlds` se construye con `_public_ficha` (allowlist explícita de DEC20 §14.4). Nunca serialización del modelo con eliminación de campos.
- `mine` es `true` solo cuando el solicitante es el propietario autenticado del mundo. En modo público anónimo siempre es `false`. En modo propietario (`mine=1`) puede ser `true` para mundos propios y `false` no aplica (todos son del propietario).
- `visibility` es `"public"` en modo público. En modo propietario puede ser `"public"` o `"private"` según el valor persistido de cada mundo propio.
- `next_cursor` se calcula con el algoritmo `limit + 1` (§15.3.1).
- No se devuelve un total global de mundos.
- El filtro de visibilidad o propiedad se aplica en la consulta SQL, no en Python.

#### 15.3.1 Algoritmo de determinación de página siguiente (`limit + 1`)

1. Aplicar la condición de visibilidad (`visibility = public`, modo público) o de propiedad (`owner_user_id = user_id` de la sesión, modo propietario).
2. Aplicar los filtros de búsqueda (`search`) y modo (`mode`) solicitados.
3. Aplicar el punto de corte del cursor (`id < cursor`), si se proporciona.
4. Ordenar por `id DESC`.
5. Solicitar `limit + 1` registros en SQL.
6. Si se obtienen más de `limit` registros, existe otra página.
7. Devolver exclusivamente los primeros `limit` registros.
8. Si existe otra página, establecer `next_cursor` con el `id` del último registro efectivamente entregado.
9. Si no existe otra página, establecer `next_cursor = null`.

- No se devuelve el registro adicional utilizado para detectar la página siguiente.
- No se ejecuta una consulta de conteo global.
- El límite se aplica en persistencia, no después de recuperar todos los resultados.

### 15.4 Orden

- `ORDER BY id DESC` (mundos más recientes primero). `id` es clave primaria monotónica y única, sirve como desempate estable.
- `created_at` no se usa como criterio de orden (campo `String` sin garantía de precisión o formato canónico).
- No se exponen órdenes alternativos configurables por el cliente en F7-B.

### 15.5 Privacidad y autorización

- Visitante anónimo: ve exclusivamente mundos públicos (200).
- Usuario autenticado no propietario: ve los mismos mundos públicos (`mine=false`).
- Propietario: ve sus propios mundos en `?mine=1` (públicos o privados); en galía pública anónima ve solo los públicos.
- Mundo privado de tercero: no aparece en el listado; `GET /api/worlds/{id}` → 404 (indistinguible de inexistente).
- `state_json` nunca aparece en respuestas de galería.
- El WS no se usa en la galería; las reglas de DEC20 para WebSocket permanecen intactas.

### 15.6 Búsqueda por nombre

- Parámetro `search`, longitud máxima 64. Más largo → 400.
- Insensible a mayúsculas/minúsculas (`LOWER(name) LIKE LOWER(:search)`).
- Caracteres comodín SQL (`%`, `_`) y carácter de escape se escapan antes de construir el patrón.
- `search` vacío o solo espacios se trata como sin búsqueda.
- Solo se busca por `name`; no por descripción, etiquetas, propietario, seed ni otros campos.

### 15.7 Filtro por modo

- Parámetro `mode`, debe ser `single` o `league`. Otro valor → 400.
- Ausencia del filtro devuelve todos los modos públicos.
- Se combina con `search`, `cursor`, `limit`.

### 15.8 Proyección SQL

- La consulta de galería proyecta solo las columnas necesarias para la ficha pública (`id`, `name`, `mode`, `seed`, `months_simulated`, `land_pct`, `created_at`, `season_years`, `season_over`, `visibility`, `owner_user_id` para derivar `mine`).
- **No se carga `state_json`** en la consulta de listado.
- No existe N+1: una sola consulta SQL serializada en Python mediante allowlist explícita.

### 15.9 Índices

- No se agregan índices por anticipación.
- Se documenta la necesidad de medir antes de introducir un índice en `visibility` o compuesto (`visibility`, `id`), considerando volumen esperado y coste de escritura.

### 15.10 Errores HTTP

| Situación | Código |
|---|---|
| `limit` fuera de 1..100 | 400 |
| `cursor` no entero | 400 |
| `mode` fuera de `{single, league}` | 400 |
| `search` > 64 caracteres | 400 |
| `mine` con valor distinto de `1` | 400 |
| `mine=1` sin sesión autenticada | 401 |
| Mundo privado/inexistente en detalle | 404 |
| Operación sin sesión cuando se requiere | 401 |

---

## Reglas transversales

1. Los contratos de esta lista son **vinculantes**: una implementación que los vulnere es un defecto, aunque pase sus propios tests.
2. Al revisar, contrastar **siempre contra el código real** (`grep` de los símbolos citados), no solo contra este texto.
3. Ampliar un contrato requiere decisión aprobada (DEC); corregir este archivo por conveniencia está prohibido por AGENTS.md.

---

## 16. Historias compartidas de evolución (DEC-F7C-01 a DEC-F7C-12 — F7-C)

> **Estado:** contrato corregido y resuelto arquitectónicamente (F7-C-R1-I, 2026-11-16, Arquitectura B: registro incremental persistente de acontecimientos + diario durable de operaciones + checkpoints para recuperación del motor). Implementación **no iniciada** (F7-C-I1 no autorizada). Ningún código de galería de historias existe aún; esta sección especifica el contrato futuro sobre la implementación de F7-A y F7-B.

### 16.1 Alcance

- `GET /api/worlds/{world_id}/events` es el endpoint de consulta histórica de acontecimientos. Devuelve exclusivamente acontecimientos de mundos a los que el solicitante tiene acceso.
- F7-C **no** incorpora mundos destacados, rankings globales, desafíos, sistemas de recomendación, votaciones, likes, comentarios ni reputación. Estas funcionalidades pertenecen a etapas posteriores (F7-D a F7-F).
- Las historias de mundos privados solo son accesibles a su propietario autenticado. Los mundos privados de terceros responden 404, indistinguibles de un mundo inexistente.

### 16.2 Modelo de persistencia (contrato conceptual — Arquitectura B)

La arquitectura objetivo (Arquitectura B, F7-C-R1-I) distingue tres entidades: **Operación**, **Acontecimiento** y **Checkpoint**.

- **Operación:** unidad durable de trabajo que hace avanzar el motor; persiste en tabla `world_operations` con `operation_id`, `operation_sequence`, `status` (`pending`/`executing`/`confirmed`/`blocked`/`failed`/`canceled`), `created_at`, `confirmed_at`, `checkpoint_id`, `payload_ref`, `client_idempotency_key`, `request_hash`, `engine_version`, `rng_state_ref`, `recovery_state`, `initial_checkpoint_id`, `recovery_attempt_id`.
- **Acontecimiento:** hecho lógico generado durante una operación; persiste en tabla `world_events` con `world_id`, `operation_id`, `operation_sequence`, `position`, `month`, `event_type`, `event_text`, `kingdom_ids`, `event_params`, `msg_key`, `created_at`.
- **Checkpoint:** fotografía del estado completo del motor para recuperación; persiste en `WorldCheckpoint` (`id`, `world_id`, `sequence`, `month`, `checkpoint_type`, `payload`, `checksum`, `created_at`, `is_valid`, `checkpoint_version`); es complementario al registro de eventos, no la fuente histórica.
- El registro histórico consultable se construye con `world_events`; el checkpoint contiene el estado completo del motor (DEC13) para recuperación, no como fuente histórica.
- Los acontecimientos no se persisten solo en el payload del checkpoint; se insertan en `world_events` junto con la transición de la operación en una única transacción (T1/T2, DEC-F7C-15).

**Asignador durable de secuencias por mundo (compatible con SQLite):**
- La tabla `worlds` incluye una columna `next_operation_sequence` (INTEGER, NOT NULL, DEFAULT 1) que almacena el próximo número de secuencia disponible para ese mundo.
- La reserva de `operation_sequence` ocurre **dentro de T1** mediante una transacción de escritura serializada (`BEGIN IMMEDIATE`):
  1. Iniciar `BEGIN IMMEDIATE`.
  2. Consultar la operación existente por `(world_id, requesting_user_id, client_idempotency_key)`.
  3. Si existe, verificar `request_hash` y devolver la operación existente; **no** reservar una secuencia nueva.
  4. Si no existe, leer `worlds.next_operation_sequence`.
  5. Reservar ese valor como `operation_sequence`.
  6. Incrementar `next_operation_sequence`.
  7. Insertar `world_operations` con su identidad, hash y estado inicial.
  8. **Commit T1** (reserva de secuencia, incremento de contador, inserción de operación — todo atómico).
- Si T1 hace rollback, la reserva y el incremento se revierten juntos; no se consume una secuencia.
- La restricción `UNIQUE (world_id, operation_sequence)` actúa como defensa adicional contra colisiones.
- **No** se utiliza `MAX(operation_sequence)+1` como mecanismo de asignación (ni en código ni en ejemplos).
- **No** se utiliza `FOR UPDATE`; el bloqueo se logra mediante `BEGIN IMMEDIATE` y la transacción serializada.

**Manejo de contención y reintentos en T1:**
- Errores reintentables: `SQLITE_BUSY`, `SQLITE_LOCKED` (contención de base de datos).
- Errores terminales: restricción UNIQUE violada por clave externa distinta, errores de esquema, corrupción.
- Un reintento de T1 con la **misma** clave externa no genera una nueva operación (la comprobación de idempotencia está dentro de la transacción).
- Política de backoff configurable con límite máximo de reintentos; no bucles ilimitados.
- Un error de contención **no** se convierte en respuesta de éxito.
- Si solo falló la adquisición de la transacción T1, **no** se reintenta la operación de motor.

La tabla `world_operations` almacena:
| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Identificador interno |
| `world_id` | INTEGER, FK a `worlds.id`, índice | Mundo al que pertenece la operación |
| `operation_id` | VARCHAR, UNIQUE por mundo | Identidad interna durable (p. ej., UUID) |
| `operation_sequence` | INTEGER, UNIQUE por mundo | Posición serializada dentro del mundo (monotonía) |
| `status` | VARCHAR | `pending` \| `executing` \| `confirmed` \| `blocked` \| `failed` \| `canceled` |
| `continuity_state` | VARCHAR | `active` \| `blocked` \| `released` — estado de continuidad operativa, separado del estado histórico |
| `created_at` | String | ISO 8601 UTC timestamp |
| `confirmed_at` | String | ISO 8601 UTC timestamp (NULL si no confirmada) |
| `checkpoint_id` | INTEGER, FK a `WorldCheckpoint.id` | Checkpoint resultante de T2 (NULL si pendiente) |
| `payload_ref` | TEXT | Referencia opcional al payload del checkpoint |
| `client_idempotency_key` | VARCHAR | Clave de idempotencia aportada por el cliente |
| `request_hash` | VARCHAR | Huella canónica de la solicitud (SHA-256) |
| `engine_version` | VARCHAR | Versión del motor/reglas para reproducibilidad |
| `rng_state_ref` | TEXT | Referencia al estado RNG inicial durable |
| `recovery_state` | TEXT | Estado de recuperación (JSON) |
| `initial_checkpoint_id` | INTEGER, FK a `WorldCheckpoint.id` | Checkpoint desde el que se reconstruye el motor |
| `recovery_attempt_id` | VARCHAR | Identificador durable del intento de recuperación (UUID) |
| `recovery_generation` | INTEGER, NOT NULL, DEFAULT 0 | Generación monotónica de recuperación para fencing |

La tabla `WorldCheckpoint` almacena:
| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Identificador del checkpoint, monotonía dentro del mundo |
| `world_id` | INTEGER, FK a `worlds.id`, índice | Mundo al que pertenece el checkpoint |
| `sequence` | INTEGER | Secuencia del checkpoint dentro del mundo, monotonía durable por mundo |
| `month` | INTEGER | Mes simulado en que ocurrió el checkpoint |
| `checkpoint_type` | VARCHAR | Tipo de checkpoint (manual, periodic, pre_tick, recovery, shutdown) |
| `payload` | LargeBinary | Serialización completa del SimEngine (incluye `chronicle.events`) |
| `checksum` | String(64) | SHA-256 hex del payload |
| `created_at` | String | ISO 8601 UTC timestamp |
| `is_valid` | INTEGER | 1 = válido, 0 = corrupto |
| `checkpoint_version` | INTEGER | Versión de serialización |

**Tabla `operation_resolutions` (registro administrativo de resolución de operaciones fallidas):**
| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Identificador de resolución |
| `world_id` | INTEGER, FK a `worlds.id`, índice | Mundo al que pertenece la operación |
| `operation_id` | VARCHAR, FK a `world_operations.operation_id` | Identidad interna de la operación resuelta |
| `operation_sequence` | INTEGER | Secuencia de la operación resuelta |
| `admin_user_id` | INTEGER, FK a `users.id` | Principal administrativo autenticado |
| `resolution_type` | VARCHAR | `confirmed_no_effects` \| `continue_with_gap` \| `remain_blocked` |
| `reason` | TEXT | Motivo y justificación de la resolución |
| `evidence_refs` | TEXT | Referencias de auditoría (JSON array) |
| `resolution_version` | INTEGER | Versión del formato de resolución |
| `created_at` | String | ISO 8601 UTC timestamp |
| `idempotency_key` | VARCHAR | Clave de idempotencia de la resolución administrativa |

**Restricciones de `operation_resolutions`:**
- `UNIQUE (world_id, operation_id, idempotency_key)` — idempotencia de la resolución.
- `CHECK (resolution_type IN ('confirmed_no_effects','continue_with_gap','remain_blocked'))`.
- `ON DELETE CASCADE` en `operation_resolutions.world_id` y `operation_resolutions.operation_id`.
- Una resolución no puede modificarse tras confirmarse; una corrección crea un nuevo registro que referencia al anterior.

**Restricciones de unicidad e integridad:**
- `UNIQUE (world_id, operation_id)` — identidad interna por mundo.
- `UNIQUE (world_id, operation_sequence)` — serialización estricta.
- `UNIQUE (world_id, requesting_user_id, client_idempotency_key)` — idempotencia externa por mundo y principal autenticado.
- `CHECK (status IN ('pending','executing','confirmed','blocked','failed','canceled'))`.
- `CHECK (continuity_state IN ('active','blocked','released'))`.
- `CHECK ( (status IN ('confirmed') AND continuity_state = 'active') OR (status IN ('pending','executing','blocked') AND continuity_state = 'active') OR (status IN ('failed','canceled') AND continuity_state IN ('blocked','released')) )` — coherencia entre estado histórico y continuidad.
- `ON DELETE CASCADE` en `world_operations.world_id`, `world_events.world_id`, `world_events.operation_id`, `WorldCheckpoint.world_id`.

**Semántica de `continuity_state` (estado de continuidad operativa):**
- **`active`**: la operación no tiene una liberación terminal por discontinuidad. No implica autorización para admitir otra operación. `active` significa que la operación está en curso (`pending`, `executing`, `blocked`) o confirmada (`confirmed`), y que no se ha producido una liberación terminal por discontinuidad. No implica que la cobertura histórica sea continua ni que T1 permita avanzar automáticamente. La cobertura histórica se determina mediante los registros de eventos, confirmaciones y discontinuidades establecidos en §16.
- **`blocked`**: la operación terminó en `failed` o `canceled`, pero todavía no existe una liberación operativa confirmada. El mundo no puede avanzar.
- **`released`**: la operación terminó en `failed` o `canceled`, y la discontinuidad y liberación operativa fueron confirmadas atómicamente. El mundo puede admitir una operación posterior.

La continuidad histórica y la admisión operativa son conceptos distintos. `active` no significa que la cobertura histórica sea continua ni que T1 permita avanzar automáticamente. La cobertura histórica se determina mediante los registros de eventos, confirmaciones y discontinuidades establecidos en §16.

El estado histórico (`status`) **nunca** cambia tras ser terminal. La continuidad operativa (`continuity_state`) es un campo separado que refleja si la discontinuidad ha sido registrada y el mundo liberado. Una resolución administrativa o cancelación segura que libera el mundo actualiza `continuity_state` a `released` en la **misma transacción** que registra la discontinuidad; el `status` histórico permanece inalterado (`failed` o `canceled`).

**Combinaciones válidas e inválidas de `operation_status` y `continuity_state`:**

| operation_status | continuity_state | Validez | Admite nueva operación (T1) | Notas |
|---|---|---|---|---|
| pending | active | Válida | No | Operación en curso |
| executing | active | Válida | No | Operación en curso |
| blocked | active | Válida | No | Operación no resuelta |
| confirmed | active | Válida | Sí | Secuencia liberada |
| failed | blocked | Válida | No | Terminal sin liberación |
| failed | released | Válida | Sí | Resolución liberadora confirmada |
| canceled | blocked | Válida | No | Terminal sin liberación |
| canceled | released | Válida | Sí | Cancelación segura confirmada |

**Combinaciones inválidas (prohibidas por CHECK constraint):**

| operation_status | continuity_state | Razón |
|---|---|---|
| pending | released | Operación no terminal no puede estar liberada |
| executing | released | Operación no terminal no puede estar liberada |
| blocked | released | Operación no terminal no puede estar liberada |
| confirmed | blocked | Operación confirmada nunca bloquea |
| confirmed | released | Operación confirmada no se libera por discontinuidad |

Las combinaciones inválidas están prohibidas por la restricción CHECK en §16.2. No se permite que una operación terminal `failed` o `canceled` vuelva a un estado ejecutable.

- Un acontecimiento se considera **confirmado** solo cuando su operación (`world_operations`) quedó `confirmed` en T2, junto con el checkpoint asociado, en una única transacción.
- Si la transacción T2 falla (rollback de SQLite), ningún acontecimiento de ese lote se considera persistido; ningún checkpoint queda `is_valid=1` ni operación `confirmed` sin sus eventos.
- Los acontecimientos entre operaciones no persistidos no deben contarse como confirmados.
- Borrado en cascada al eliminar un mundo (`ON DELETE CASCADE` en `WorldCheckpoint.world_id` y en `world_operations`/`world_events`).
- No se exponen `state_json` ni campos de propiedad en consultas de historia; el payload del checkpoint no se expone salvo para recuperación interna.

### 16.3 Parámetros de consulta de `GET /api/worlds/{world_id}/events`

| Parámetro | Tipo | Predeterminado | Valores válidos | Comportamiento |
|---|---|---|---|---|
| `limit` | entero opcional | 20 | 1..100 | Tamaño de página. Fuera de rango → 400. |
| `cursor` | cursor opaco opcional | — | par (`operation_sequence`, `posición`) serializado (p. ej. `operation_sequence:posición`) | Punto de corte en el orden total (`operation_sequence` ASC, `posición` ASC). Filtra eventos estrictamente posteriores al punto de corte. No requiere que el registro exista. Inválido → 400. |
| `snapshot_token` | token opaco opcional | — | token firmado por el servidor | Identifica la navegación histórica iniciada y su `snapshot_high_watermark`. Ausente en la primera consulta. Si se proporciona, debe coincidir con el `snapshot_token` de la navegación original. |
| `month` | entero opcional | — | entero ≥ 0 | Filtra por mes simulado. |
| `event_type` | cadena opcional | — | tipo de evento válido | Filtra por tipo de acontecimiento. |
| `kingdom_id` | entero opcional | — | ID de reino válido | Filtra por civilización involucrada. |

**Reglas de navegación histórica:**
- La primera consulta **no** incluye `snapshot_token`; el servidor fija el `snapshot_high_watermark` como el último acontecimiento confirmado visible y genera un `snapshot_token` opaco (firmado, versionado, vinculado a `world_id` y a la huella de los filtros `month`, `event_type`, `kingdom_id`, `limit`).
- Consultas posteriores **deben** incluir el mismo `snapshot_token`; el servidor valida su integridad, expiración y coincidencia de filtros. Si falla la validación → 400.
- El `snapshot_high_watermark` es inmutable durante toda la navegación; nuevos acontecimientos confirmados **no** se incluyen.
- Los filtros (`month`, `event_type`, `kingdom_id`, `limit`) **no** pueden cambiar entre páginas de una misma navegación. Un cambio de filtros inicia una navegación nueva (sin `snapshot_token`).
- El cursor (`next_cursor`) representa solo la posición de continuación; el `snapshot_token` representa el universo fijo de la navegación.

### 16.4 Respuesta paginada

La respuesta es un objeto paginado:

```json
{
  "events": [
    {
      "event_id": int,
      "month": int,
      "event_type": str,
      "event_text": str,
      "kingdom_ids": [int],
      "event_params": dict,
      "msg_key": str
    }
  ],
  "next_cursor": "operation_sequence:posición"|null,
  "snapshot_token": "string"|null,
  "snapshot_high_watermark": "operation_sequence:posición"|null,
  "coverage": {
    "status": "confirmed|potentially_incomplete|unknown",
    "first_checkpoint_month": int | null,
    "last_checkpoint_month": int | null
  }
}
```

- `events` se construye con allowlist explícita. Nunca serialización del modelo completo.
- **Proyección pública** (anónimo y tercero en mundo público): solo `event_id`, `month`, `event_type`, `event_text`, `msg_key`. Los campos `kingdom_ids` y `event_params` se omiten.
- **Proyección del propietario** (propietario autenticado de su mundo): todos los campos de la allowlist, incluidos `kingdom_ids` y `event_params`.
- `next_cursor` es el par (`operation_sequence`, `posición`) del último acontecimiento entregado si hay más resultados dentro del `snapshot_high_watermark`, o `null` si se alcanzó el final de la navegación.
- `snapshot_token` es un token opaco firmado por el servidor que identifica la navegación histórica y su `snapshot_high_watermark`. Presente en **todas** las respuestas de una navegación (incluida la primera). Ausente solo si la navegación no puede iniciarse (p. ej., sin acontecimientos confirmados).
- `snapshot_high_watermark` es la posición (`operation_sequence:posición`) del último acontecimiento confirmado incluido en el universo de la navegación. Inmutable durante toda la navegación.
- No se devuelve un total global de acontecimientos.
- El algoritmo `limit + 1` se aplica para detectar página siguiente **dentro** del `snapshot_high_watermark`.
- Expiración del `snapshot_token`: configurable (p. ej., 5 min); tras expiración, una nueva consulta inicia una navegación nueva.

### 16.5 Orden

- `ORDER BY operation_sequence ASC, posición ASC` dentro del mundo (orden cronológico de la crónica). `operation_sequence` es monotonía creciente por mundo, asignada en T1; `posición` es el índice dentro del delta de la operación.
- El mes simulado (`month`) es un atributo consultable, pero no la clave de orden principal.
- No existe orden trans-mundo global.
- El orden es estable: restaurar un checkpoint no reutiliza identificadores ya confirmados, porque la operación tiene secuencia durable distinta.
- En una navegación histórica con `snapshot_token`, el universo de consulta es `(operation_sequence, posición) <= snapshot_high_watermark`; el `next_cursor` avanza dentro de ese universo fijo.

### 16.6 Privacidad y autorización

| Situación | Resultado |
|---|---|
| Anónimo, mundo público | 200 con acontecimientos públicos (proyección pública) |
| Autenticado no propietario, mundo público | 200 con acontecimientos públicos (proyección pública) |
| Propietario autenticado, mundo público o privado | 200 con acontecimientos del mundo (proyección del propietario) |
| Tercero (autenticado o anónimo), mundo privado | 404 (indistinguible de inexistente) |
| Mundo inexistente | 404 |
| Mundo no recuperable | 409 (guarda DEC16 aplicada antes de consultar) |
| Sin sesión, consulta de mundo propio | 404 (sin sesión no se puede probar propiedad; se trata como mundo privado de tercero) |

- `state_json` nunca aparece en respuestas de historia.
- Los campos `kingdom_ids` y `event_params` se filtran mediante allowlist pública; no se exponen campos internos de propiedad.
- `user_token` nunca es prueba de identidad.
- Sin una sesión autenticada, el servidor no puede identificar al solicitante como propietario. Por tanto, una solicitud anónima sobre un mundo privado se trata exactamente como una solicitud sobre un mundo inexistente: 404.

### 16.7 Historia anterior a F7-C

- Los acontecimientos generados antes de la implementación de F7-C **no se importan**.
- Solo se registran acontecimientos ocurridos después de habilitar F7-C.
- Los acontecimientos anteriores carecen de identificador persistente y no son consultables.
- La historia comienza en la fecha de habilitación de F7-C; no se asume completitud.

### 16.8 Restauración y protección de historia publicada

- **Recuperación automática tras crash:** el proceso restaura el motor desde el último checkpoint válido (DEC13/DEC18); las operaciones pendientes (`world_operations.status=pending`) se recuperan desde el último estado durable confirmado y se ejecutan conforme al protocolo T1/T2; las operaciones `confirmed` no se reejecutan.
- **Restauración deliberada a punto histórico anterior:** la política mínima segura es crear un mundo nuevo (`world_id` nuevo) con su propio espacio de operaciones y acontecimientos; no se reutilizan identidades de operaciones ni acontecimientos del mundo original; los cursores del mundo original no se trasladan al nuevo mundo.
- **Prohibido:** truncar o reescribir historia publicada confirmada como efecto secundario de recuperación ordinaria; inferir cobertura `confirmed` solo por la existencia de checkpoints válidos.
- Si el último checkpoint está corrupto y se recupera uno anterior, el sistema debe registrar la divergencia entre estado recuperado e historia confirmada posterior; no se oculta la divergencia.
- Si la arquitectura no permite una restauración segura sin ramas explícitas, el comportamiento mínimo seguro es rechazar la restauración.

### 16.9 Eliminación de mundo

- Al eliminar un mundo, se eliminan en cascada sus `world_operations`, `world_events` y `WorldCheckpoint` asociados.
- `DELETE /api/worlds/{id}` elimina el mundo, sus operaciones, sus acontecimientos y sus checkpoints en una transacción única; si el mundo no puede borrarse, tampoco se borran sus operaciones, acontecimientos ni checkpoints.
- La eliminación es atómica con el mundo; no quedan huérfanos.

### 16.10 Estados de operación y continuidad de secuencia

**Estados válidos y transiciones:**

| Estado | Significado | Transiciones permitidas |
|---|---|---|
| `pending` | T1 confirmado; ejecución no iniciada | `executing` (inicio ejecución), `canceled` (cancelación segura), `blocked` (no reproducible) |
| `executing` | Ejecución en curso | `confirmed` (T2 exitoso), `blocked` (fallo reproducible), `failed` (fallo terminal) |
| `confirmed` | T2 confirmó checkpoint + eventos + operación | **ninguna** (irreversible) |
| `blocked` | No puede recuperarse automáticamente con garantías | `executing` (tras intervención manual durable), `failed` (resolución terminal) |
| `failed` | Fallo terminal documentado; resultado incierto o sin evidencia durable de efectos | **ninguna** (terminal, no se reejecuta) |
| `canceled` | Cancelación segura: T1 confirmado, ejecución no iniciada, sin mutaciones ni acontecimientos | **ninguna** (terminal, irreversible) |

**Reglas de serialización y continuidad por mundo:**
1. Un mundo solo puede tener **una** operación en estado `pending` o `executing` a la vez.
2. `operation_sequence` se reserva en T1 de forma atómica mediante el asignador durable (§16.2) y es monotonía estricta por mundo (`UNIQUE (world_id, operation_sequence)`).
3. Una operación con `operation_sequence = N+1` **no puede** entrar en `executing` mientras exista una operación con `operation_sequence = N` en estado `pending`, `executing`, `blocked`, `failed` o `canceled`.
4. Una operación `confirmed` libera la siguiente posición de secuencia para ejecución.
5. Una operación `blocked` **bloquea el avance** del mundo hasta resolución explícita y durable (intervención manual que cambia el estado a `executing` o `failed`).
6. Una operación `failed` **no se salta**; el mundo permanece bloqueado en esa secuencia hasta una resolución administrativa explícita, durable y auditable (§16.11.3).
7. **Cancelación segura (`pending → canceled`):** solo permitida cuando se demuestra **durablemente** que:
   - T1 confirmó la reserva.
   - La ejecución del motor **no comenzó**.
   - No hubo mutaciones del motor atribuibles a la operación.
   - No se generaron acontecimientos persistidos para esa operación.
   - La operación no tiene un resultado T2 confirmado.
   - La cancelación se realiza bajo exclusión del mundo y de la operación.
   - La decisión, la discontinuidad correspondiente (`discontinuity_sequence`, `discontinuity_reason='canceled'`, `last_confirmed_sequence`) y `continuity_state = 'released'` quedan registradas en la **misma transacción durable**.
   Si existe incertidumbre sobre el inicio de ejecución o sus efectos, la operación pasa a `blocked`, **no** a `canceled`.
8. **Liberación del avance tras cancelación:** una operación `canceled` con `continuity_state = 'released'` permite reservar o ejecutar la siguiente secuencia. La siguiente operación conserva su secuencia normal; **no** se reutiliza el número cancelado. Si `continuity_state = 'blocked'` (cancelación sin liberación operativa), el mundo permanece bloqueado.
9. **No** se reutilizan secuencias. **No** se borran operaciones para ocultar huecos.
10. **Cobertura y discontinuidad:** un intervalo con operaciones no resueltas (`pending`/`executing`/`blocked`/`failed`/`canceled`) impide declarar `coverage.status = confirmed` más allá de la última operación `confirmed` contigua desde el inicio. La discontinuidad se expone en:
    - `discontinuity_sequence`: `operation_sequence` de la primera operación no resuelta.
    - `discontinuity_reason`: `pending` | `executing` | `blocked` | `failed` | `canceled`.
    - `last_confirmed_sequence`: última `operation_sequence` confirmada contigua desde el inicio.
    - No se usa `discontinuity_at` con posición de acontecimiento ficticia.
    - La existencia de operaciones posteriores confirmadas **no** repara retrospectivamente la discontinuidad. La cobertura continua desde el origen termina en `last_confirmed_sequence`; las operaciones posteriores pertenecen a un tramo posterior a la discontinuidad.

### 16.11 Protocolo T1/T2 con idempotencia externa, fencing de recuperación y resolución administrativa

**T1 — Reserva durable de operación con clave externa (compatible con SQLite):**
1. El cliente genera `client_idempotency_key` (opaca, p. ej., UUID v4) y la envía en la solicitud junto con `request_hash` (SHA-256 de la representación canónica de la solicitud).
2. **Canonicalización de `request_hash`:** el hash se calcula sobre una representación determinista y versionada que incluye: `world_id`, tipo de operación, todos los parámetros semánticos, versión de ejecución solicitada. **No** incluye timestamps de recepción, IDs de conexión, ni metadatos de transporte. El esquema de canonicalización lleva versión (p. ej., `v1`) para evitar que cambios futuros alteren hashes históricos.
3. El servidor ejecuta T1 en una transacción de escritura serializada (`BEGIN IMMEDIATE`):
   - Inicia `BEGIN IMMEDIATE`.
   - Busca en `world_operations` por `(world_id, requesting_user_id, client_idempotency_key)` donde `requesting_user_id` es el `user_id` de la sesión autenticada (Bearer). Las operaciones que requieren identidad durable **exigen** sesión autenticada; sin sesión → 401.
   - **Existe y `request_hash` coincide:** recupera la operación existente y devuelve su identidad histórica sin crear una nueva operación, sin consumir otra secuencia, y sin reejecutar la operación. La respuesta incluye la representación contractual de la operación según su estado:
     - `pending`, `executing`, `blocked`: devuelve la identidad y el estado actual; no reserva nueva secuencia; no ejecuta el motor.
     - `confirmed`: devuelve la identidad y el resultado confirmado (reintento post-ACK).
     - `canceled`, `failed`: devuelve la identidad histórica y el estado terminal; no reabre la operación; no crea una nueva operación.
   - **Existe y `request_hash` NO coincide:** rechaza con **409 Conflict** (clave reutilizada con payload semántico distinto).
   - **No existe:** antes de reservar secuencia, **comprueba la condición operativa del mundo** consultando la operación con la `operation_sequence` más alta para ese `world_id`:
     - Si no existe ninguna operación previa → procede a reservar.
     - Si la operación más alta tiene `status = 'confirmed'` → procede a reservar (secuencia liberada).
     - Si la operación más alta tiene `continuity_state = 'released'` → procede a reservar (discontinuidad registrada y mundo liberado operativamente).
     - En cualquier otro caso (`pending`, `executing`, `blocked`, o terminal con `continuity_state = 'blocked'`) → **rechaza la reserva con 409** (el mundo tiene una operación incompatible no resuelta o una terminal sin liberación operativa). El cliente no recibe una nueva secuencia.
   - Si la comprobación pasa: lee `worlds.next_operation_sequence`, reserva ese valor como `operation_sequence`, incrementa `next_operation_sequence`, crea operación con `operation_id` (UUID), `status = 'pending'`, `continuity_state = 'active'`, `recovery_generation = 0`, `recovery_attempt_id = NULL`, guarda `client_idempotency_key`, `request_hash`, `engine_version`, `rng_state_ref`, `initial_checkpoint_id`. **Commit T1** (reserva de secuencia, incremento de contador, inserción de operación — todo atómico).
   - La restricción `UNIQUE (world_id, requesting_user_id, client_idempotency_key)` garantiza exclusividad: una solicitud concurrente con la misma tripleta espera o recibe conflicto de unicidad, tras lo cual consulta la operación ganadora y aplica la misma lógica.
4. Solo tras commit de T1 se permite iniciar la ejecución (transición a `executing`).

**Tabla: Estados y condición de continuidad que determinan admisión T1**

| Estado histórico (`status`) | Condición de continuidad (`continuity_state`) | Admisión T1 | Razón |
|---|---|---|---|
| `pending` | `active` (siempre) | **Rechazar** | Operación reservada sin ejecutar |
| `executing` | `active` (siempre) | **Rechazar** | Operación en curso |
| `blocked` | `active` (siempre) | **Rechazar** | Operación no resuelta; requiere intervención |
| `failed` | `blocked` | **Rechazar** | Operación terminal sin liberación operativa; requiere resolución administrativa (§16.11.3) |
| `failed` | `released` | **Permitir** | Resolución administrativa liberadora (`confirmed_no_effects` o `continue_with_gap`) con discontinuidad registrada atómicamente |
| `canceled` | `blocked` | **Rechazar** | Cancelación sin liberación operativa (no se demostró ausencia de efectos) |
| `canceled` | `released` | **Permitir** | Cancelación segura con discontinuidad y liberación operativa registradas atómicamente (§16.10.7) |
| `confirmed` | `active` (siempre) | **Permitir** | Operación completada; secuencia liberada |

**Tabla: Comportamiento de T1 ante operación existente (misma clave de idempotencia — regla única de idempotencia):**

| Estado existente (`status`) | Continuidad (`continuity_state`) | Resultado T1 (respuesta idempotente) |
|---|---|---|
| `confirmed` | `active` | Devuelve la identidad histórica y el resultado confirmado (reintento post-ACK) |
| `pending` | `active` | Devuelve la identidad histórica y el estado `pending`; no reserva nueva secuencia; no ejecuta el motor |
| `executing` | `active` | Devuelve la identidad histórica y el estado `executing`; no reserva nueva secuencia; no ejecuta el motor |
| `blocked` | `active` | Devuelve la identidad histórica y el estado `blocked`; no reserva nueva secuencia; no ejecuta el motor |
| `canceled` | `blocked` | Devuelve la identidad histórica y el estado terminal `canceled`; no reabre la operación; no crea una nueva operación |
| `canceled` | `released` | Devuelve la identidad histórica y el estado terminal `canceled`; no reabre la operación; no crea una nueva operación |
| `failed` | `blocked` | Devuelve la identidad histórica y el estado terminal `failed`; no reabre la operación; no crea una nueva operación |
| `failed` | `released` | Devuelve la identidad histórica y el estado terminal `failed`; no reabre la operación; no crea una nueva operación |

**Dos solicitudes concurrentes con claves de idempotencia distintas:**
- Ambas entran en `BEGIN IMMEDIATE` serializadas por SQLite.
- La primera que committee reserva la secuencia si el mundo está libre.
- La segunda, al consultar la operación más alta, encuentra `pending`/`executing`/`blocked`/`failed`/`canceled` y **rechaza 409**.
- Ninguna reserva una secuencia mientras exista una operación incompatible.

**Separación entre respuesta idempotente y admisión T1:**
Existen dos decisiones independientes dentro de T1:
1. **Resolver una clave idempotente existente:** si la `client_idempotency_key` ya existe para el `(world_id, requesting_user_id)`, se comprueba el `request_hash`. Si coincide, se devuelve la operación original (identidad histórica, estado o resultado); no se reserva nueva secuencia; no se ejecuta el motor; no se modifica el estado histórico ni `continuity_state`.
2. **Admitir una operación nueva con clave inexistente:** si la clave no existe, se aplica la regla T1 de admisión según la última operación del mundo (comprobando la combinación `operation_status + continuity_state` de la operación de mayor secuencia); se reserva una secuencia solo si está permitido; se inserta la nueva operación.

La liberación de continuidad de una operación `failed` o `canceled` (con `continuity_state = 'released'`) permite una nueva operación con clave **diferente**, pero **nunca** reabre la operación histórica ni altera su estado terminal.

**Respuesta HTTP para reintento idempotente:**
La respuesta de un reintento idempotente representa el resultado de la operación original, no la admisión de una nueva operación. La estructura de respuesta incluye la representación contractual de la operación según su estado:

| Estado | Código HTTP | Cuerpo de respuesta |
|---|---|---|
| `confirmed` | 200 | Representación completa del resultado confirmado (`operation_id`, `operation_sequence`, `status`, `checkpoint_id`, `created_at`, `confirmed_at`, eventos delta) |
| `pending` | 200 | Representación de la operación pendiente (`operation_id`, `operation_sequence`, `status='pending'`, `created_at`) |
| `executing` | 200 | Representación de la operación en ejecución (`operation_id`, `operation_sequence`, `status='executing'`, `created_at`) |
| `blocked` | 200 | Representación de la operación bloqueada (`operation_id`, `operation_sequence`, `status='blocked'`, `created_at`, `recovery_state` si aplica) |
| `canceled` | 200 | Representación de la operación cancelada (`operation_id`, `operation_sequence`, `status='canceled'`, `created_at`, `continuity_state`) |
| `failed` | 200 | Representación de la operación fallida (`operation_id`, `operation_sequence`, `status='failed'`, `created_at`, `continuity_state`, `recovery_state` si aplica) |
| Hash no coincide (mismismo `client_idempotency_key`) | 409 | `{"error": "idempotency_conflict", "message": "La clave de idempotencia ya existe con un payload diferente"}` |
| Clave nueva pero mundo bloqueado (admisión T1 denegada) | 409 | `{"error": "world_blocked", "message": "El mundo tiene una operación no resuelta o terminal sin liberación"}` |

El cliente puede distinguir un reintento idempotente (200 con representación histórica) de un rechazo de admisión T1 (409 con `world_blocked`) por el código y el contenido de la respuesta.

**Reintento idempotente mientras el mundo está bloqueado:**
- Cliente reintenta con misma `client_idempotency_key`.
- T1 encuentra la operación existente, verifica `request_hash`.
- Devuelve la operación existente; **no** reserva nueva secuencia.
- No elude las restricciones de ejecución del mundo: si la operación está `pending`/`executing`/`blocked`, se aplica recuperación (§16.14); si `failed`/`canceled`, 409 (la liberación operativa `continuity_state = 'released'` permite nuevas operaciones con claves **nuevas**, no reabre la operación histórica).

**Regla T1 definitiva (decisión de admisión):**

La decisión de admisión se basa en la combinación completa de `status` (histórico) y `continuity_state` (continuidad operativa) de la operación de mayor `operation_sequence` para el `world_id`. La regla se ejecuta dentro de `BEGIN IMMEDIATE`, junto con:

1. Comprobación de idempotencia (`UNIQUE (world_id, requesting_user_id, client_idempotency_key)`).
2. Consulta de la última operación (`MAX(operation_sequence)` para el `world_id`).
3. Verificación de la combinación `(status, continuity_state)` de esa operación.
4. Lectura e incremento de `next_operation_sequence`.
5. Inserción de la nueva operación con `status = 'pending'`, `continuity_state = 'active'`.
6. Commit T1.

**Tabla de decisión T1 (combinación completa):**

| `status` (histórico) | `continuity_state` | Decisión T1 |
|---|---|---|
| `pending` | `active` | Rechazar |
| `executing` | `active` | Rechazar |
| `blocked` | `active` | Rechazar |
| `confirmed` | `active` | Permitir |
| `failed` | `blocked` | Rechazar |
| `failed` | `released` | Permitir |
| `canceled` | `blocked` | Rechazar |
| `canceled` | `released` | Permitir |

La nueva operación siempre comienza con `continuity_state = 'active'`. `continuity_state` nunca se usa de forma aislada como autorización de admisión; la decisión siempre considera la combinación completa `(status, continuity_state)`.

**Invariantes de secuencia y escritura:**

1. La operación de mayor `operation_sequence` es el último punto de decisión operativa del mundo.
2. No puede admitirse una nueva operación mientras la última permanezca en un estado que impida avanzar.
3. Una operación posterior solo puede admitirse después de `confirmed + active` o de `failed/canceled + released`.
3. La cancelación y la resolución administrativa actualizan la continuidad operativa en la misma transacción que registra la discontinuidad.
4. T1 y las transiciones que liberan continuidad utilizan el mismo protocolo de serialización SQLite (`BEGIN IMMEDIATE`).
5. Ninguna ruta alternativa de escritura puede reservar secuencias o modificar la autorización operativa fuera de estas reglas.
6. La secuencia histórica nunca se reutiliza.
7. La liberación operativa no altera la cobertura histórica ni el estado terminal original.

**Manejo de contención y reintentos en T1:**
- Errores reintentables: `SQLITE_BUSY`, `SQLITE_LOCKED` (contención de base de datos).
- Errores terminales: restricción UNIQUE violada por clave externa distinta, errores de esquema, corrupción.
- Un reintento de T1 con la **misma** clave externa no genera una nueva operación (la comprobación de idempotencia está dentro de la transacción).
- Política de backoff configurable con límite máximo de reintentos; no bucles ilimitados.
- Un error de contención **no** se convierte en respuesta de éxito.
- Si solo falló la adquisición de la transacción T1, **no** se reintenta la operación de motor.

**Adquisición de recuperación (exclusión durable y fencing):**
Antes de iniciar una recuperación, el proceso debe adquirir la generación vigente. Solo las operaciones en estados **explicitamente recuperables** pueden adquirir una generación.

**Lista positiva de estados recuperables (admisión de generación):**

| Estado | Admite recuperación | Condición |
|---|---|---|
| `pending` | Sí | T1 confirmado; se cumplen condiciones §16.14 |
| `executing` | Sí | Solo mediante protocolo de recuperación y fencing (§16.14, §16.15) |
| `blocked` | No (no automáticamente) | Requiere condición explícita de desbloqueo contractual; no recuperable automáticamente |
| `failed` | **No** | Terminal; requiere resolución administrativa (§16.11.3) |
| `canceled` | **No** | Terminal; discontinuidad registrada |
| `confirmed` | **No** | Terminal; irreversible |

`recovery_generation` es un mecanismo de **fencing**, no una autorización de transición de estado. Una operación no recuperable no puede incrementar su generación ni adquirir un `recovery_attempt_id`.

**Procedimiento de adquisición (para estados recuperables):**
1. Iniciar transacción serializada (`BEGIN IMMEDIATE`).
2. Consultar la operación objetivo.
3. Verificar que `status ∈ {'pending','executing'}` (lista positiva).
4. Verificar que se cumplen las condiciones de reproducibilidad (§16.14) — para `pending` antes de ejecutar; para `executing` tras reconstrucción.
5. Leer `recovery_generation` actual.
6. Incrementar `recovery_generation` (nueva generación = actual + 1).
7. Generar nuevo `recovery_attempt_id` (UUID).
8. Persistir: `recovery_generation`, `recovery_attempt_id`, `recovery_state = 'acquired'`, propietario de recuperación.
9. **Commit** de la adquisición.
Un proceso **no puede** iniciar la reconstrucción ni la reejecución sin haber adquirido la generación vigente. Dos procesos no pueden compartir simultáneamente una generación activa.

**T2 — Confirmación atómica con fencing:**
Una única transacción confirma:
- Checkpoint del estado resultante (`WorldCheckpoint` con `is_valid=1`).
- Acontecimientos nuevos en `world_events` (delta de la operación).
- Verificación de fencing: si la ejecución procede de recuperación, comprobar que `world_operations.recovery_generation` sigue siendo la generación vigente **dentro de esta misma transacción**.
- Transición `world_operations.status: 'executing' → 'confirmed'`, `confirmed_at = now()`, `checkpoint_id = <nuevo checkpoint>`.
- Metadatos de continuidad (referencia al checkpoint anterior, hash de estado final).
Si falla cualquier escritura (incluida la verificación de fencing) → **rollback completo**; la operación queda en `executing` (o `pending` si no llegó a transición) y se aplicará recuperación en el siguiente arranque.
Un recuperador antigua cuya generación fue reemplazada **no puede** confirmar T2 aunque siga ejecutándose en memoria.

**Reintento post-commit/pre-ACK:**
Si el cliente reintenta con la misma `client_idempotency_key` (y mismo `requesting_user_id`) tras confirmación, el servidor localiza la operación `confirmed` y devuelve el resultado persistido **sin** reejecutar el tick, sin insertar eventos duplicados, sin crear checkpoint de segunda ejecución.

**Resolución administrativa de operaciones `failed`:**
Una operación `failed` solo puede resolverse mediante una intervención administrativa explícita, durable y auditable. La resolución **no** cambia el estado histórico de la operación (que permanece `failed`), sino que registra una decisión que permite o no el avance del mundo.
1. **Autoridad:** solo un principal con permiso administrativo explícito (`admin_user_id`) puede registrar una resolución. No se equipara al propietario ordinario.
2. **Idempotencia:** cada resolución lleva su propia `idempotency_key`; una resolución no puede modificarse tras confirmarse; una corrección crea un nuevo registro que referencia al anterior.
3. **Tipos de resolución:**
   - **`confirmed_no_effects`**: solo si existe evidencia suficiente de que la operación no produjo mutaciones persistentes, no confirmó T2 y no generó acontecimientos. Resultado: la operación original **permanece `failed`**; se registra una discontinuidad (`discontinuity_sequence`, `discontinuity_reason='failed'`, `last_confirmed_sequence`) y `continuity_state = 'released'`; el mundo puede continuar **después del commit de la resolución y la discontinuidad**. No se transforma en `pending`, `executing` ni `confirmed`.
   - **`continue_with_gap`**: cuando el resultado no puede reconstruirse con garantías, pero un administrador autorizado acepta explícitamente continuar con una discontinuidad histórica. Resultado: la operación original **permanece `failed`**; se registra la incertidumbre y la evidencia disponible, y `continuity_state = 'released'`; el mundo puede continuar **únicamente después del commit de la resolución y la discontinuidad**. No se transforma en `pending`, `executing` ni `confirmed`.
   - **`remain_blocked`**: cuando no existe base suficiente para resolver. Resultado: la operación **sigue `failed`**; `continuity_state` permanece `blocked`; el mundo **permanece bloqueado**; no se reserva ni ejecuta la siguiente operación.
4. **Atomicidad de la liberación:** la resolución, la discontinuidad, la actualización de `continuity_state` y la liberación del mundo son atómicas (misma transacción):
   - Comprobar estado actual de la operación (`failed`).
   - Comprobar autoridad del principal.
   - Comprobar que no existe resolución incompatible.
   - Registrar la resolución en `operation_resolutions`.
   - Registrar la discontinuidad (actualizar metadatos de cobertura: `discontinuity_sequence`, `discontinuity_reason='failed'`, `last_confirmed_sequence`).
   - Actualizar `continuity_state = 'released'` en `world_operations` (para `confirmed_no_effects` y `continue_with_gap`).
   - **Commit** conjunto.
   Si el commit falla, el mundo permanece bloqueado.

**Resumen de comportamiento post-resolución:**

| Tipo de resolución | Estado histórico (`status`) | Continuidad (`continuity_state`) | Mundo puede avanzar |
|---|---|---|---|
| `confirmed_no_effects` | `failed` (inalterado) | `released` | Sí, tras commit atómico |
| `continue_with_gap` | `failed` (inalterado) | `released` | Sí, tras commit atómico |
| `remain_blocked` | `failed` (inalterado) | `blocked` (inalterado) | No |

Ninguna resolución administrativa transforma `failed` en `pending`, `executing` o `confirmed`. La discontinuidad queda expuesta en cobertura (`discontinuity_sequence`, `discontinuity_reason='failed'`).

**Autenticación, ámbito y privacidad de la clave externa:**
- Ámbito definitivo de unicidad: `(world_id, requesting_user_id, client_idempotency_key)`.
- `requesting_user_id` = `user_id` de la sesión Bearer autenticada. No se usa `user_token` ni identificadores auto-declarados.
- Un principal diferente no puede consultar estado, resultado, hash ni contenido de una operación ajena. La búsqueda por clave aplica autorización: si el principal no coincide, la respuesta no revela si la clave existe para otro usuario (404 o 403 según política de no exposición).
- Sin sesión autenticada no se aceptan solicitudes que requieran identidad durable de operación (401).

### 16.12 Cobertura histórica

- Los acontecimientos generados **antes** de la implementación de F7-C **no se importan**. Solo se registran acontecimientos ocurridos después de habilitar F7-C.
- Los acontecimientos anteriores carecen de identificador persistente y no son consultables.
- La historia comienza en la fecha de habilitación de F7-C; no se asume completitud.
- La cobertura se define con tres estados explícitos (`confirmed`, `potentially_incomplete`, `unknown`) y se devuelve en la respuesta (`coverage.status`):
  - `confirmed`: existe evidencia durable de punto de inicio de habilitación, secuencia continua de operaciones confirmadas desde el inicio hasta `last_checkpoint_month`, todos los acontecimientos de cada operación confirmada persistidos, ausencia de operaciones `pending`/`executing`/`blocked`/`failed`/`canceled` en el intervalo, y commit atómico de persistencia + checkpoint.
  - `potentially_incomplete`: hay datos históricos persistidos, pero no puede probarse ausencia de huecos o operaciones no confirmadas (incluye caso de operación `blocked`/`failed`/`canceled` en el intervalo).
  - `unknown`: no hay evidencia suficiente para caracterizar el intervalo.
- **Metadatos adicionales de cobertura:** cuando `status ≠ confirmed`, la respuesta incluye:
  - `discontinuity_sequence`: `operation_sequence` de la primera operación no resuelta.
  - `discontinuity_reason`: `pending` | `executing` | `blocked` | `failed` | `canceled`.
  - `last_confirmed_sequence`: última `operation_sequence` confirmada contigua desde el inicio.
  - No se usa `discontinuity_at` con posición de acontecimiento (`operation_sequence:posición`); la discontinuidad se refiere a la secuencia de operación.
- No se declara cobertura `confirmed` solo porque un checkpoint pase checksum y pueda restaurarse; no se realiza backfill ficticio de eventos anteriores a la habilitación.

### 16.13 Paginación estable con snapshot (ampliación)

**Semántica de `snapshot_high_watermark` y `next_cursor`:**
- `snapshot_high_watermark`: posición del último acontecimiento **confirmado** visible al iniciar la navegación. Es la cota superior inmutable del universo de consulta.
- `next_cursor`: posición de continuación para la página siguiente **dentro** del universo fijado por `snapshot_high_watermark`.
- Ambos se expresan como par `operation_sequence:posición` (codificado en el `snapshot_token` firmado). Las posiciones son **basadas en cero** (`position = 0` para el primer acontecimiento de una operación).
- En la primera consulta, el servidor:
  1. Determina el último acontecimiento confirmado visible (respetando autorización §16.6).
  2. Fija `snapshot_high_watermark = (operation_sequence, posición)` de ese acontecimiento.
  3. Devuelve los primeros `limit` acontecimientos con `(operation_sequence, posición) <= snapshot_high_watermark`.
  4. Genera `snapshot_token` (firmado, versionado, con expiración, vinculado a `world_id` y huella de filtros).
  5. Devuelve `next_cursor` para la página siguiente.
- En consultas posteriores con el mismo `snapshot_token`:
  1. Valida firma, expiración, `world_id`, huella de filtros (`month`, `event_type`, `kingdom_id`, `limit`).
  2. Aplica `(operation_sequence, posición) > next_cursor` **Y** `(operation_sequence, posición) <= snapshot_high_watermark`.
  3. Devuelve página, nuevo `next_cursor`, mismo `snapshot_token`, mismo `snapshot_high_watermark`.
- Si `snapshot_token` expira o falla validación → 400; el cliente debe iniciar navegación nueva (sin token).

**Ejemplo obligatorio (20 acontecimientos, `limit=10`, posiciones 0-based):**
- OP1 (seq=21): E1–E7 → positions (21,0) … (21,6)
- OP2 (seq=22): E8–E13 → positions (22,0) … (22,5)
- OP3 (seq=23): E14–E20 → positions (23,0) … (23,6)

Primera consulta (`limit=10`, sin token):
- Último confirmado visible: E20 → `snapshot_high_watermark = (23,6)`.
- Devuelve: E1–E10 = (21,0)…(21,6), (22,0)…(22,2).
- `next_cursor = "22:2"` (E10).
- `snapshot_token = <token_firmado>`, `snapshot_high_watermark = "23:6"`.

Segunda consulta (`limit=10`, `snapshot_token=<token>`, `cursor="22:2"`):
- Universo fijado: ≤ (23,6).
- Filtro: `(op_seq, pos) > (22,2)` Y `≤ (23,6)`.
- Devuelve: E11–E20 = (22,3)…(22,5), (23,0)…(23,6).
- `next_cursor = null`.
- `snapshot_token` y `snapshot_high_watermark` idénticos.

Inserción concurrente: entre consultas se confirma OP4 (seq=24) con E21–E22 → positions (24,0)…(24,1).
- Segunda página **sigue devolviendo E11–E20** (fuera del high-water mark original).
- Nueva consulta (sin token) establece nuevo high-water mark = (24,1) y puede incluir E21–E22.

**Convención de `position`:**
- Primer acontecimiento de una operación: `position = 0`.
- N-ésimo acontecimiento: `position = N-1`.
- `position` es entero no negativo; no se reutiliza dentro de una operación.
- Una operación confirmada sin acontecimientos tiene resultado vacío (sin fabricar acontecimiento ficticio en `position = 0`).
- Las discontinuidades se refieren a `operation_sequence` (`discontinuity_sequence`), no a posiciones de acontecimiento.

### 16.14 Recuperación determinista de operaciones pendientes

**Condiciones de reproducibilidad (todas obligatorias):**
Para que una operación `pending`/`executing`/`blocked` pueda reejecutarse tras crash, **todas** deben cumplirse:
1. **Estado inicial confirmado:** `initial_checkpoint_id` referencia un checkpoint válido (`is_valid=1`) que será la base de reconstrucción.
2. **Parámetros canónicos:** la solicitud original se reconstruye a partir de `request_hash` y metadatos guardados; no hay ambigüedad.
3. **Estado RNG reproducible:** `rng_state_ref` permite restaurar el estado exacto del generador aleatorio antes de la operación (el checkpoint incluye el estado RNG del motor; la referencia apunta a ese estado durable).
4. **Versión de motor/reglas:** `engine_version` coincide con la versión actual del motor; si difiere, la operación pasa a `blocked` (requiere intervención para decidir compatibilidad).
5. **Orden de iteración determinista:** el motor itera estructuras en orden estable (claves ordenadas, sin dependencia de hash map iteration order no determinista).
6. **Sin fuentes temporales externas:** la operación no depende de `time.time()`, `random` no semillado, APIs externas, ni entradas de usuario no persistidas.
7. **Identidad de operación:** `operation_id` y `operation_sequence` se conservan; la reejecución produce los mismos `position` para cada acontecimiento.
8. **Versión de esquema de acontecimientos:** compatible con la versión actual de `world_events`.

Si **cualquiera** falla → la operación transiciona a `blocked` (no `failed`); el mundo no avanza; se requiere intervención explícita, auditable y durable.

**Datos mínimos en `world_operations` para recuperación:**
- `initial_checkpoint_id` (FK a `WorldCheckpoint`): base de reconstrucción.
- `request_hash` + metadatos de parámetros: reconstrucción de la solicitud.
- `engine_version`: comprobación de compatibilidad.
- `rng_state_ref`: referencia al estado RNG en el checkpoint inicial.
- `operation_id`, `operation_sequence`: identidad y posición.
- `recovery_state` (JSON): `{"attempts": n, "last_error": "...", "deterministic": true/false, "blocked_reason": "..."}`.
- `status`: estado actual.
- **No** se duplica el payload completo del motor.

**Política obligatoria:**
1. Operación `confirmed` → **nunca** se reejecuta.
2. Operación pendiente → **nunca** continúa desde motor volátil previo al crash; reconstrucción desde `initial_checkpoint_id`.
3. Solo reejecución si **todas** las condiciones de reproducibilidad se cumplen.
4. Si no se cumple → `blocked`; mundo detenido en esa secuencia; intervención manual durable.
5. No inventar acontecimientos para completar lote.
6. No eliminar operación pendiente para ocultar incertidumbre.
7. ACK perdido de operación `confirmed` → reintento con `client_idempotency_key` devuelve resultado persistido (no reejecuta).

### 16.15 Tabla de escenarios de recuperación (A–J)

| Escenario | Estado `world_operations` | Último checkpoint confirmado | Motor en memoria | Registro histórico | Acción RecoveryCoordinator | ¿Reejecuta? | ¿Mundo avanza? | Cobertura |
|---|---|---|---|---|---|---|---|---|
| **A. Caída antes de T1** | (no existe) | Checkpoint N | Sin cambios | Sin cambios | Ninguna; operación no existía | No | Sí (siguiente solicitud crea T1) | `confirmed` hasta N |
| **B. Caída tras T1, antes de ejecución** | `pending` (seq=S) | Checkpoint N | Sin cambios | Sin cambios | Recupera motor desde checkpoint N; operación `pending` → `executing`; ejecuta tick | Sí (determinista: estado inicial = checkpoint N, params guardados, RNG desde N) | Sí, tras confirmar S | `confirmed` hasta S-1; S pendiente |
| **C. Caída tras modificar motor en memoria** | `executing` (seq=S) | Checkpoint N | Mutado (volátil) | Sin cambios | Descarta mutaciones; reconstruye motor desde checkpoint N; operación `executing` → reejecuta si determinista | Solo si determinista (§16.14); si no → `blocked` | Solo si reejecuta y confirma | `confirmed` hasta S-1; S según resultado |
| **D. Caída durante T2 antes de commit** | `executing` (seq=S) | Checkpoint N | Mutado (volátil) | Sin cambios (rollback) | Igual que C: reconstruye desde N; reejecuta si determinista | Solo si determinista; si no → `blocked` | Solo si reejecuta y confirma | `confirmed` hasta S-1 |
| **E. Caída tras commit T2, antes de ACK** | `confirmed` (seq=S) | Checkpoint N+1 | Confirmado | Eventos de S persistidos | Operación ya `confirmed`; reintento con `client_idempotency_key` devuelve resultado | **No** (irreversible) | Sí (siguiente secuencia libre) | `confirmed` hasta S |
| **F. Caída tras ACK** | `confirmed` (seq=S) | Checkpoint N+1 | Confirmado | Eventos de S persistidos | Idem E | **No** | Sí | `confirmed` hasta S |
| **G1. Caída tras registrar `recovery_attempt_id`/`recovery_generation`, antes de reconstruir** | `pending`/`executing` (seq=S), `recovery_attempt_id = RA1`, `recovery_generation = G` | Checkpoint M | Sin cambios / volátil | Según ops confirmadas | Al reiniciar: lee op con `recovery_attempt_id = RA1` y `recovery_generation = G`; si `status ∈ {'pending','executing'}`, descarta estado volátil, reconstruye desde `initial_checkpoint_id`, reintenta recuperación adquiriendo **nueva** generación `G+1` (mismo `recovery_attempt_id` o nuevo). Si `status = 'blocked'` → **no recuperable automáticamente**; permanece `blocked` hasta intervención. | Según determinismo (§16.14) | Según resultado | `confirmed` hasta M; S según resultado |
| **G2. Caída durante reconstrucción del motor** | `pending`/`executing` (seq=S), `recovery_generation = G` | Checkpoint M | Parcialmente reconstruido | Según ops confirmadas | Idem G1: reconstrucción idempotente desde `initial_checkpoint_id`; adquiere generación `G+1`. Si `status = 'blocked'` → **no recuperable automáticamente**. | Según determinismo | Según resultado | `confirmed` hasta M; S según resultado |
| **G3. Caída tras reconstruir, antes de reejecutar** | `pending`/`executing` (seq=S), `recovery_generation = G` | Checkpoint M | Reconstruido (estado inicial) | Según ops confirmadas | Motor ya en estado inicial confirmado; procede a reejecutar tick si determinista (con generación `G`). Si `status = 'blocked'` → **no recuperable automáticamente**. | Según determinismo | Según resultado | `confirmed` hasta M; S según resultado |
| **G4. Caída durante reejecución, antes de T2** | `executing` (seq=S), `recovery_generation = G` | Checkpoint M | Mutado (volátil) | Según ops confirmadas | Descarta mutaciones; reconstruye desde `initial_checkpoint_id` (idempotente); reintenta adquiriendo generación `G+1`. | Según determinismo | Según resultado | `confirmed` hasta M; S según resultado |
| **G5. Caída durante T2 antes de commit** | `executing` (seq=S), `recovery_generation = G` | Checkpoint M | Mutado (volátil) | Sin cambios (rollback T2) | Igual que G4: rollback completo, reconstruye desde `initial_checkpoint_id`, reintenta con generación `G+1`. | Según determinismo | Según resultado | `confirmed` hasta M; S según resultado |
| **G6. Caída tras commit T2, antes de registrar fin de recuperación** | `confirmed` (seq=S), `recovery_generation = G` | Checkpoint M+1 | Confirmado | Eventos de S persistidos | Operación ya `confirmed`; al reiniciar se detecta `status = confirmed` → no reejecuta; marca recuperación completada | **No** (irreversible) | Sí (siguiente libre) | `confirmed` hasta S |
| **G7. Caída tras completar recuperación** | `confirmed` (seq=S) | Checkpoint M+1 | Confirmado | Eventos de S persistidos | Idem G6/F | **No** (irreversible) | Sí | `confirmed` hasta S |
| **H. Dos solicitudes concurrentes, misma clave externa** | Primera: `pending`/`executing`/`confirmed` | Según caso | Según caso | Según caso | Segunda solicitud encuentra operación existente por `(world_id, requesting_user_id, client_idempotency_key)`; aplica política según estado (§16.11) | No crea segunda operación | Según operación existente | Según operación existente |
| **I. Dos operaciones diferentes, mismo mundo** | OP-A (seq=S), OP-B (seq=S+1) | Según OP-A | Según OP-A | Según OP-A | OP-B **no inicia** mientras OP-A ∈ `{pending,executing,blocked,failed,canceled}`; serialización estricta | OP-B espera | OP-B espera | Bloqueada en S hasta resolver OP-A |
| **J. Operación pendiente no reproducible** | `pending`/`executing` (seq=S) | Checkpoint N | Mutado/irrelevante | Sin cambios | Verifica condiciones §16.14 → falla → transiciona a `blocked` con `blocked_reason` | **No** | **No** (mundo bloqueado en S) | `confirmed` hasta S-1; `discontinuity_sequence = S`, `discontinuity_reason = blocked` |

### 16.16 Integración con WorldRunner (contrato futuro)

- La integración con `WorldRunner._run()` debe garantizar que cada tick se ejecute como operación durable: T1 reserva la operación antes del tick (incluyendo `client_idempotency_key` y `request_hash` del cliente), la ejecución ocurre una sola vez por operación, y T2 confirma checkpoint + acontecimientos + estado de operación atómicamente.
- No se permite que un tick quede confirmado únicamente en memoria; la mutación en memoria no confirmada se descarta si el proceso cae antes de T2.
- Durante pausa, reanudación y shutdown, las operaciones pendientes se gestionan según el protocolo T1/T2; en shutdown, las operaciones en curso se drenan o se dejan `pending` para recuperación posterior.
- `RecoveryCoordinator` recupera operaciones pendientes tras reinicio desde el último estado durable confirmado y las ejecuta conforme al protocolo; no reejecuta operaciones `confirmed`.
- Esta sección define el contrato futuro; no describe mecanismos implementados.

### 16.17 Consistencia transversal (bloqueos 1–7) y ejemplos integrados

**Recorrido A — confirmed + active → nueva operación admitida:**
1. OP20 (seq=20) `confirmed`, `continuity_state = 'active'` con eventos E1–E5.
2. T1 para nueva solicitud (clave nueva): consulta operación más alta = OP20 (`confirmed`, `continuity_state='active'`) → **admite** OP21 (seq=21).
3. OP21 se ejecuta y confirma con eventos E6–E9.
4. Verificación: combinación `confirmed + active` → T1 permite.

**Recorrido B — pending + active → nueva operación rechazada:**
1. OP50 (seq=50) `pending`, `continuity_state = 'active'` en curso.
2. T1 para nueva solicitud (clave nueva): consulta operación más alta = OP50 (`pending`, `continuity_state='active'`) → **rechaza 409**.
3. Verificación: combinación `pending + active` → T1 rechaza.

**Recorrido C — canceled + blocked → nueva operación rechazada:**
1. OP60 (seq=60) `canceled`, `continuity_state = 'blocked'` (cancelación sin liberación).
2. T1 para nueva solicitud (clave nueva): consulta operación más alta = OP60 (`canceled`, `continuity_state='blocked'`) → **rechaza 409**.
3. Verificación: combinación `canceled + blocked` → T1 rechaza.

**Recorrido D — canceled + released → nueva operación admitida:**
1. OP20 (seq=20) `confirmed`, `continuity_state = 'active'`.
2. OP21 (seq=21) `pending`, `continuity_state = 'active'`; ejecución no comenzó.
4. Cancelación segura: en una transacción, `status='canceled'`, `continuity_state='released'`, discontinuidad registrada.
5. T1 para nueva solicitud (clave nueva): consulta operación más alta = OP21 (`canceled`, `continuity_state='released'`) → **admite** OP22 (seq=22).
5. OP22 se ejecuta y confirma.
6. Cobertura: continua termina en 20; 21 discontinuidad con `continuity_state='released'`; 22 tramo posterior.

**Recorrido E — failed + blocked → nueva operación rechazada:**
1. OP60 (seq=60) `failed`, `continuity_state = 'blocked'` (fallo terminal sin liberación).
2. T1 para nueva solicitud (clave nueva): consulta operación más alta = OP60 (`failed`, `continuity_state='blocked'`) → **rechaza 409**.
3. Verificación: combinación `failed + blocked` → T1 rechaza.

**Recorrido F — failed + released → nueva operación admitida:**
1. OP30 (seq=30) `failed`, `continuity_state = 'blocked'` tras caída en ejecución.
2. OP31 (seq=31) bloqueada: T1 ve OP30 `failed` + `continuity_state='blocked'` → rechaza 409.
3. Administrador evalúa y registra `continue_with_gap`: en transacción atómica, registra resolución en `operation_resolutions`, discontinuidad, y **actualiza OP30 a `continuity_state = 'released'`**.
4. T1 para nueva solicitud: consulta OP30 `failed` + `continuity_state='released'` → **admite** OP31.
5. OP31 se ejecuta y confirma.
6. Historia: OP30 permanece `failed` (inalterado), `continuity_state='released'`; discontinuidad en cobertura.

**Recorrido G — Reintento idempotente de operación terminal liberada:**
1. OP21 (`canceled`, `continuity_state='released'`) tras cancelación segura.
2. Cliente reintenta con `client_idempotency_key` de OP21.
3. T1 encuentra OP21, verifica `request_hash` → devuelve OP21 existente (histórico).
4. **No** crea OP22, **no** consume nueva secuencia.
5. Nueva operación con clave **nueva** → T1 ve OP21 `canceled` + `continuity_state='released'` → admite OP22.

**Recorrido H — Nueva clave después de liberación confirmada:**
1. OP80 (`canceled`, `continuity_state='released'`) tras cancelación segura (commit confirmado).
2. T1 para nueva solicitud (clave nueva): consulta operación más alta = OP80 (`canceled`, `continuity_state='released'`) → **admite** OP81.
3. OP81 se ejecuta y confirma.
4. Verificación: combinación `canceled + released` → T1 permite.

**Ejemplo A — Cancelación segura y continuidad (con `continuity_state`):**
1. OP20 (seq=20) está `confirmed` (`continuity_state = 'active'`) con eventos E1–E5.
2. OP21 (seq=21) tiene T1 confirmado (`pending`, `continuity_state = 'active'`); su ejecución **no comenzó**.
3. Se demuestra durablemente ausencia de efectos (sin mutaciones, sin acontecimientos).
4. En una transacción durable: se registra `status = 'canceled'`, `continuity_state = 'released'`, y la discontinuidad (`discontinuity_sequence=21`, `discontinuity_reason='canceled'`, `last_confirmed_sequence=20`).
5. El mundo queda habilitado para OP22 (seq=22).
6. OP22 se ejecuta y confirma con eventos E6–E9.
7. Cobertura: continua desde origen termina en seq=20. Seq=21 queda como discontinuidad permanente (`status='canceled'`, `continuity_state='released'`). Seq=22 pertenece a tramo posterior; no se declara cobertura continua a través de la cancelación.

**Ejemplo B — Fallo incierto y resolución administrativa (con `continuity_state`):**
1. OP30 (seq=30) entra en `executing` (`continuity_state = 'active'`).
2. El proceso cae; no se puede determinar si hubo efectos durables.
3. OP30 queda `blocked` (incertidumbre) o `failed` (evidencia de fallo terminal sin efectos confirmados), `continuity_state = 'blocked'`. **Nunca** se presume `canceled`.
4. OP31 (seq=31) **no puede** comenzar (bloqueada en seq=30).
5. Un administrador autorizado evalúa la evidencia durable y registra una resolución en `operation_resolutions`:
   - Si `confirmed_no_effects`: evidencia suficiente de ausencia de efectos → en la misma transacción: registra resolución, discontinuidad, `continuity_state = 'released'` en OP30; mundo puede continuar.
   - Si `continue_with_gap`: acepta discontinuidad explícita → en la misma transacción: registra resolución, discontinuidad, `continuity_state = 'released'` en OP30; mundo puede continuar.
   - Si `remain_blocked`: sin base suficiente → `continuity_state` permanece `blocked`; mundo permanece bloqueado.
6. La resolución, la discontinuidad y la actualización de `continuity_state` son **atómicas** (misma transacción).
7. Historia: OP30 permanece `failed` (inalterado); `continuity_state = 'released'` (si resolución liberadora) o `blocked`; discontinuidad en seq=30 expuesta en cobertura; OP31+ son tramo posterior.

**Ejemplo C — Dos recuperadores con fencing:**
1. OP40 (seq=40) queda `pending` (`continuity_state = 'active'`) tras caída antes de ejecución.
2. Recuperador R1 inicia: adquiere `recovery_generation = 5`, `recovery_attempt_id = RA1` (transacción `BEGIN IMMEDIATE`).
3. R1 reconstruye motor desde último checkpoint confirmado.
4. R1 pierde lease / queda obsoleto (no renueva, o proceso muere).
5. Recuperador R2 inicia: adquiere `recovery_generation = 6`, `recovery_attempt_id = RA2` (nueva transacción, incrementa generación).
6. R1 intenta confirmar T2 con generación 5.
7. T2 verifica fencing **dentro de su transacción**: `recovery_generation` actual (6) ≠ generación de R1 (5) → **rollback completo**; R1 no puede confirmar.
8. R2 reconstruye desde último checkpoint confirmado, ejecuta tick, confirma T2 con generación 6.
9. Reintento posterior del cliente observa `status = 'confirmed'` y devuelve resultado persistido sin reejecutar.
10. Resultado: un solo lote de acontecimientos, un solo checkpoint, una sola confirmación. Sin duplicación ni publicación parcial.

**Ejemplo D — Cancelación segura y continuación: `OP20 confirmed → OP21 canceled con discontinuidad confirmada → OP22 admitida`**
1. OP20 (seq=20) `confirmed`, `continuity_state = 'active'`.
2. OP21 (seq=21) `pending`, `continuity_state = 'active'`; ejecución no comenzó.
3. Cancelación segura: en una transacción, `status='canceled'`, `continuity_state='released'`, discontinuidad registrada.
2. T1 para nueva solicitud (clave nueva): consulta operación más alta = OP21 (`canceled`, `continuity_state='released'`) → **admite** OP22 (seq=22).
3. OP22 se ejecuta y confirma.
4. Cobertura: continua termina en 20; 21 discontinuidad con `continuity_state='released'`; 22 tramo posterior.

**Ejemplo E — Resolución liberadora: `OP30 failed → continue_with_gap confirmado → OP31 admitida`**
1. OP30 (seq=30) `failed`, `continuity_state = 'blocked'` tras caída en ejecución.
2. OP31 (seq=31) bloqueada: T1 ve OP30 `failed` + `continuity_state='blocked'` → rechaza 409.
3. Administrador evalúa y registra `continue_with_gap`: en transacción atómica, registra resolución en `operation_resolutions`, discontinuidad, y **actualiza OP30 a `continuity_state = 'released'`**.
4. T1 para nueva solicitud: consulta OP30 `failed` + `continuity_state='released'` → **admite** OP31.
5. OP31 se ejecuta y confirma.
6. Historia: OP30 permanece `failed` (inalterado), `continuity_state='released'`; discontinuidad en cobertura.

**Ejemplo F — Resolución no liberadora: `OP40 failed → remain_blocked → OP41 rechazada`**
1. OP40 (seq=40) `failed`, `continuity_state = 'blocked'`.
2. Administrador registra `remain_blocked`: `continuity_state` permanece `blocked`.
3. T1 para nueva solicitud: consulta OP40 `failed` + `continuity_state='blocked'` → **rechaza 409**.
4. Mundo permanece bloqueado; OP41 no se crea.
5. Historia: OP40 `failed`, `continuity_state='blocked'`; discontinuidad en cobertura.

**Ejemplo G — Concurrencia antes/después de commit de cancelación:**
1. OP80 (`pending`) en curso.
2. Cancelación segura de OP80: transacción T1 actualiza `status='canceled'`, `continuity_state='released'`, registra discontinuidad → **commit**.
3. **Solicitud concurrente A (llega ANTES del commit)**: T1 ve OP80 `pending` → rechaza 409 (no observa liberación parcial).
4. **Solicitud concurrente B (llega DESPUÉS del commit)**: T1 ve OP80 `canceled` + `continuity_state='released'` → **admite** OP81.
5. No hay ventana de liberación parcial.

**Ejemplo H — Reintento idempotente tras cancelación: `OP21 canceled → reintento con misma clave`**
1. OP21 (`canceled`, `continuity_state='released'`) tras cancelación segura.
2. Cliente reintenta con `client_idempotency_key` de OP21.
3. T1 encuentra OP21, verifica `request_hash` → devuelve OP21 existente (histórico).
4. **No** crea OP22, **no** consume nueva secuencia.
5. Nueva operación con clave **nueva** → T1 ve OP21 `canceled` + `continuity_state='released'` → admite OP22.

**Recorrido I — Reintento idempotente en estado pending:**
1. OP80 (`pending`, `continuity_state='active'`) en curso.
2. Cliente reintenta con `client_idempotency_key` de OP80.
3. T1 encuentra OP80, verifica `request_hash` → devuelve OP80 existente con estado `pending`.
4. **No** crea OP81, **no** consume nueva secuencia, **no** ejecuta el motor.
5. Estado devuelto permite al cliente saber que la operación está pendiente.

**Recorrido J — Reintento idempotente en estado executing:**
1. OP81 (`executing`, `continuity_state='active'`) en ejecución.
2. Cliente reintenta con `client_idempotency_key` de OP81.
3. T1 encuentra OP81, verifica `request_hash` → devuelve OP81 existente con estado `executing`.
4. **No** crea OP82, **no** consume nueva secuencia, **no** ejecuta un segundo tick.
5. Estado devuelto permite al cliente saber que la operación está en ejecución.

**Recorrido K — Reintento idempotente en estado confirmed:**
1. OP82 (`confirmed`, `continuity_state='active'`) ya confirmada.
2. Cliente reintenta con `client_idempotency_key` de OP82.
3. T1 encuentra OP82, verifica `request_hash` → devuelve OP82 con resultado confirmado (reintento post-ACK).
4. **No** crea OP83, **no** consume nueva secuencia, **no** reejecuta el tick, **no** crea segundo checkpoint.

**Recorrido L — Reintento idempotente en canceled + released:**
1. OP83 (`canceled`, `continuity_state='released'`) tras cancelación segura.
2. Cliente reintenta con `client_idempotency_key` de OP83.
3. T1 encuentra OP83, verifica `request_hash` → devuelve OP83 histórico con estado terminal `canceled`.
4. **No** crea OP84, **no** consume nueva secuencia.
5. Clave nueva → T1 ve OP83 `canceled` + `continuity_state='released'` → admite OP84.

**Recorrido M — Reintento idempotente en failed + released:**
1. OP84 (`failed`, `continuity_state='released'`) tras resolución administrativa `continue_with_gap`.
2. Cliente reintenta con `client_idempotency_key` de OP84.
3. T1 encuentra OP84, verifica `request_hash` → devuelve OP84 histórico con estado terminal `failed`.
4. **No** crea OP85, **no** consume nueva secuencia, **no** reabre la operación.
5. Clave nueva → T1 ve OP84 `failed` + `continuity_state='released'` → admite OP85.

**Recorrido N — Reintento idempotente en failed + blocked:**
1. OP85 (`failed`, `continuity_state='blocked'`) sin resolución liberadora.
2. Cliente reintenta con `client_idempotency_key` de OP85.
3. T1 encuentra OP85, verifica `request_hash` → devuelve OP85 histórico con estado terminal `failed`.
4. **No** crea OP86, **no** consume nueva secuencia, **no** reabre la operación.
5. Clave nueva → T1 ve OP85 `failed` + `continuity_state='blocked'` → **rechaza 409** (mundo bloqueado).

**Recorrido O — Misma clave y hash diferente:**
1. OP86 (`pending`, `continuity_state='active'`) creada con `client_idempotency_key=K1` y `request_hash=H1`.
3. Cliente envía nueva solicitud con `client_idempotency_key=K1` pero `request_hash=H2` (payload distinto).
4. T1 encuentra OP86, verifica hash → **rechaza 409 Conflict** (clave reutilizada con payload semántico distinto).
4. **No** crea OP87, **no** consume nueva secuencia.

**Recorrido P — Dos solicitudes concurrentes con la misma clave:**
1. OP87 no existe.
3. Dos solicitudes concurrentes con `client_idempotency_key=K2` y mismo `request_hash=H3`.
5. Ambas entran en `BEGIN IMMEDIATE` serializadas por SQLite.
6. Primera que hace commit reserva la secuencia (ej. OP88), inserta la operación.
7. Segunda solicitud encuentra OP88 existente, verifica `request_hash` coincide → devuelve OP88.
8. **Una sola identidad, una sola secuencia**, sin duplicación.

**Recorrido transversal de las ocho correcciones (R1-I a R1-N):**
1. **Cliente** (autenticado, `requesting_user_id=U1`) genera `client_idempotency_key = "req-abc"` y `request_hash = SHA-256(canonical(payload))`, envía `POST /api/worlds/42/advance`.
2. **Servidor** T1: inicia `BEGIN IMMEDIATE`, lee `next_operation_sequence = 17`. Busca `(world_id=42, requesting_user_id=U1, client_idempotency_key="req-abc")` → no existe. **Comprueba operación más alta**: ninguna o `confirmed` o `continuity_state='released'` → procede. Reserva secuencia 17, incrementa `next_operation_sequence = 18`. Crea `operation_id=OP-17`, `operation_sequence=17`, `status=pending`, `continuity_state='active'`, `recovery_generation=0`, guarda clave, hash, `engine_version`, `rng_state_ref` (desde checkpoint 5), `initial_checkpoint_id=5`, `recovery_attempt_id=NULL`. **Commit T1** (reserva + incremento + inserción atómicos).
3. **Servidor cae** antes de responder ACK.
4. **Cliente** reintenta con mismo `client_idempotency_key="req-abc"` y mismo payload canónico.
5. **Servidor** (tras reinicio): RecoveryCoordinator recupera motor desde checkpoint 5 (último durable). Encuentra OP-17 `pending` (seq=17, `continuity_state='active'`, `recovery_attempt_id=NULL`, `recovery_generation=0`). Verifica reproducibilidad: estado inicial = checkpoint 5, params = hash guardado, RNG = estado en checkpoint 5, versión = actual, orden determinista, sin fuentes externas → **todas se cumplen**. Adquiere recuperación: `recovery_generation = 1`, `recovery_attempt_id = RA1` (lista positiva: `pending`).
6. OP-17 transiciona a `executing`, ejecuta tick (una sola vez).
7. **T2 atómico con fencing:** verifica `recovery_generation = 1` vigente; confirma checkpoint 6, eventos E1–E7 (delta OP-17, positions 0–6), OP-17 `status=confirmed`, `continuity_state='active'`, `checkpoint_id=6`, `recovery_attempt_id=RA1`, `recovery_generation=1`. **Commit T2**.
8. **Cliente** recibe resultado de OP-17 (eventos, nuevo estado).
9. **Consulta histórica:** `GET /api/worlds/42/events?limit=10`. Primera página: servidor fija `snapshot_high_watermark = (17,6)` (último evento confirmado = E7, position 6), devuelve E1–E7, `next_cursor=null`, `snapshot_token=TK1`, `snapshot_high_watermark="17:6"`.
10. **Entretanto**, cliente avanza mundo otra vez → OP-18 (seq=18) confirmada con E8–E13 (positions 0–5), checkpoint 7.
11. **Cliente** pide página 2 con `cursor=(17,6)`, `snapshot_token=TK1`.
12. Servidor valida TK1: firma, expiración, `world_id=42`, huella filtros. Universo fijado: `≤ (17,6)`. Filtro: `> (17,6)` Y `≤ (17,6)` → **vacío** (no hay más eventos en OP-17). `next_cursor=null`. Navegación original **estable**.
13. **Nueva consulta** (sin token): nuevo high-water mark = `(18,5)` (E13). Devuelve E8–E13 (y siguientes). OP-18 visible en nueva navegación.

**Sin contradicciones:** asignador durable de secuencias (`next_operation_sequence` + `BEGIN IMMEDIATE`) elimina `MAX+1` y `FOR UPDATE`; T1 admite nueva operación si la más alta tiene `continuity_state='released'` o `status='confirmed'`; rechaza si `continuity_state='blocked'` o estados activos; reintentos idempotentes no consumen secuencias; `canceled`/`failed` liberan avance solo con `continuity_state='released'` atómico; `failed` requiere resolución administrativa con auditoría, `status` inalterable; `blocked` no recuperable automáticamente; `recovery_generation` monotónico fencing impide confirmación de recuperador obsoleto; T2 verifica fencing dentro de su transacción; operación `confirmed` nunca se reejecuta; lista positiva de estados recuperables (`pending`/`executing`) excluye `blocked`/`failed`/`canceled`/`confirmed`; `continuity_state` separa estado histórico de continuidad operativa; `active` no implica autorización de admisión; combinaciones inválidas prohibidas por CHECK; liberación operativa no altera cobertura histórica ni estado terminal.

---

## Reglas transversales (originales)

## 17. Desafíos con semillas comunes (DEC-F7D-01 a DEC-F7D-12 — F7-D)

> **Estado:** contrato definido (F7-D-R1, 2026-11-17). Implementación **no iniciada**. Esta sección especifica el contrato futuro sobre F7-A, F7-B y F7-C; no declara capacidad implementada.

### 17.1 Alcance y arquitectura general

F7-D implementa **desafíos con semillas comunes**: un desafío congela una configuración competitiva común (seed, dimensiones, versión del motor, reglas de victoria, duración en ticks) y varios participantes compiten en mundos independientes, uno por cuenta inscrita, construidos desde la misma configuración.

- No se modifica la tabla `worlds` ni se le añade columna `challenge_id` (DEC-F7D-01).
- La vinculación desafío → participante → mundo se modela con dos tablas nuevas: `challenges` y `challenge_participants` (DEC-F7D-01).
- Se reutiliza íntegramente la infraestructura de F7-C: protocolo T1/T2, idempotencia externa, secuencia durable, eventos, checkpoints, recovery generation y fencing (DEC-F7D-07). No se duplica el servicio de operaciones ni se crea un protocolo alternativo.
- La identidad competitiva deriva exclusivamente de la cuenta autenticada mediante sesión Bearer (`user_id`); `user_token` nunca es identidad competitiva (DEC-F7D-03, aplicando DEC20 y DEC21).
- La reproducibilidad entre procesos es un requisito contractual verificable (DEC-F7D-06; evidencia en F7-D-R1).

### 17.2 Modelo de datos

#### Tabla `challenges`

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Identificador del desafío |
| `organizer_user_id` | INTEGER, FK a `users.id`, no nulo | Cuenta organizadora (Bearer `user_id`) |
| `name` | VARCHAR | Nombre del desafío |
| `config_json` | JSON | Configuración inmutable y versionada (seed, dimensiones, motor, reglas, duración) |
| `rules_version` | VARCHAR | Versión de reglas competitivas aplicables |
| `duration_ticks` | INTEGER, no nulo | Cantidad exacta de ticks competitivos |
| `state` | VARCHAR | `draft`, `published`, `enrollment_open`, `enrollment_closed`, `preparing`, `running`, `completed`, `canceled`, `invalidated`, `disputed`, `resolved` |
| `created_at` | String, ISO 8601 UTC | Momento de creación |
| `closed_at` | String, ISO 8601 UTC, nulo | Cierre de inscripciones |
| `completed_at` | String, ISO 8601 UTC, nulo | Finalización del desafío |

#### Tabla `challenge_participants`

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Identificador de participación |
| `challenge_id` | INTEGER, FK a `challenges.id`, no nulo | Desafío al que pertenece |
| `user_id` | INTEGER, FK a `users.id`, no nulo | Cuenta participante (identidad competitiva) |
| `world_id` | INTEGER, FK a `worlds.id`, no nulo | Mundo independiente del participante; único globalmente entre desafíos activos |
| `state` | VARCHAR | `inscrito`, `preparando`, `ejecutando`, `finalizado`, `abandonado` |
| `joined_at` | String, ISO 8601 UTC | Momento de inscripción |
| `result_json` | JSON, nulo | Resultado calculado por el servidor (solo servidor escribe) |
| `result_verified` | INTEGER (0/1), default 0 | Resultado verificado |

#### Tabla `challenge_operations` (F7-D-I1-FIX10-A-FINAL)

Registro durable de operaciones globales de desafío (procedencia exigida por §17.7 y §17.13). Tabla propia y mínima: no es una arquitectura general de auditoría y no reutiliza identidad ni secuencia T1/T2 de operaciones por mundo.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | Identificador del registro de procedencia |
| `challenge_id` | INTEGER, FK a `challenges.id`, no nulo | Desafío de la operación |
| `operation` | VARCHAR(32), no nulo | `create`, `publish`, `open_enrollment`, `enroll`, `close_enrollment`, `prepare`, `start`, `cancel`, `complete`, `confirm`, `dispute`, `resolve` |
| `actor_user_id` | INTEGER, FK a `users.id`, no nulo | Actor autenticado que ejecutó la operación (`requesting_user_id` del ámbito de §17.13: organizador, participante o administrador `is_admin` verificado en servidor) |
| `client_idempotency_key` | VARCHAR(64), no nulo | Identidad de la petición idempotente |
| `request_hash` | VARCHAR(64), no nulo, default `''` | Hash SHA-256 del contenido canónico (vacío cuando el llamador no aporta hash) |
| `outcome` | VARCHAR(32), nulo | Resultado de la operación (estado alcanzado, `enrolled`, o la resolución `confirm_all`/`invalidate_all`) |
| `created_at` | String, timestamp UTC | Momento durable de ejecución |

- `UNIQUE (challenge_id, actor_user_id, client_idempotency_key)` (`uq_challenge_operation_idem`) — materializa el ámbito de idempotencia de §17.13.
- Sin `ON DELETE CASCADE`: la procedencia no se elimina en cascada; ningún código del proyecto borra desafíos o usuarios.
- No almacena datos sensibles: sin credenciales, sin payloads de mundo ni resultados autoritativos.

#### Restricciones e índices

- `UNIQUE (challenge_id, user_id)` — impide inscripciones duplicadas de una misma cuenta en un desafío.
- `UNIQUE (world_id)` — un mundo pertenece a un único desafío competitivo durante todo su ciclo de vida (corrige la protección parcial de `UNIQUE(challenge_id, world_id)` de F7-D-R1; véase DEC-F7D-17).
- `UNIQUE (user_id)` en `users` ya existe (F7-A).
- `ON DELETE CASCADE` de `challenge_participants.challenge_id` hacia `challenges` y de `challenge_participants.user_id` hacia `users.id`. La FK `challenge_participants.world_id` hacia `worlds.id` **no** usa `ON DELETE CASCADE` destructiva: eliminar un mundo no borra la participación ni su resultado histórico (véase DEC-F7D-16).
- La eliminación de un desafío no borra resultados confirmados; solo se permite eliminación lógica o borrado físico tras archivar la evidencia (DEC-F7D-16).
- La eliminación de un mundo de participante marca su participación como `abandonado` y conserva `world_id` como referencia histórica; no borra la inscripción ni el resultado.

### 17.3 Ciclo de vida del desafío

| Estado | Significado | Transiciones salientes permitidas | Actor |
|---|---|---|---|
| `draft` | Borrador, solo organizador, no público | `published` | Organizador |
| `published` | Configuración congelada, inscripciones abiertas | `enrollment_open`, `canceled` | Organizador |
| `enrollment_open` | Inscripciones abiertas (transición operativa desde `published`) | `enrollment_closed`, `canceled` | Organizador |
| `enrollment_closed` | Lista de participantes congelada | `preparing`, `canceled`, `invalidated` | Organizador |
| `preparing` | Mundos derivados creados y asociados | `running`, `canceled` | Sistema (post-inscripción) |
| `running` | Simulación en curso | `completed`, `canceled`, `disputed` | Sistema |
| `completed` | Resultados calculados y verificados | `disputed`, `resolved` | Sistema / disputa |
| `canceled` | Anulado antes de `running` | (terminal) | Organizador |
| `invalidated` | Anulado tras `completed` por disputa | (terminal) | Resolución administrativa |
| `disputed` | Disputa abierta, resultados congelados | `resolved` | Sistema |
| `resolved` | Disputa resuelta | (terminal) | Resolución administrativa |

- **Publicación** (`draft → published`) congela `config_json`, `rules_version` y `duration_ticks` (DEC-F7D-02).
- **Apertura de inscripciones** (`published → enrollment_open`) es una transición explícita y separada de la publicación (DEC-F7D-18).
- **Cierre de inscripciones** (`enrollment_open → enrollment_closed`) congela la lista de participantes (DEC-F7D-18).
- **Creación de mundos** ocurre exclusivamente en la fase `preparing` (`enrollment_closed → preparing`), a partir de la `config_json` inmutable del desafío, uno por participante; **no** se crea el mundo en el momento de inscripción (DEC-F7D-14).
- **Inicio** (`preparing → running`) solo puede producirse cuando **todos** los participantes admitidos tienen su mundo correctamente preparado y asociado; congela definitivamente la lista de participantes y las reglas aplicables (DEC-F7D-14/18).
- Inscripción tardía, retirada y reingreso tras `enrollment_closed`: **no permitidas** (DEC-F7D-18).
- Inscripción: registra durablemente la participación y su identidad (`user_id`) en `challenge_participants`; no crea mundo aún (DEC-F7D-14). Es idempotente por `(challenge_id, user_id, client_idempotency_key)` (DEC-F7D-04, DEC-F7D-07, DEC-F7D-15).

### 17.4 Reglas de victoria y resultados

- **Política inicial:** estado final tras `duration_ticks` ticks, comparado mediante métricas agregadas con orden de prioridad y desempates deterministas (DEC-F7D-05).
- **Métricas admitidas Nivel 1 (orden de prioridad):** `total_population`, `wars_won`, `tech_level`, `influence_max`.
- **Métrica principal:** cada desafío declara `primary_metric` en `config_json` inmutable (valores permitidos: `total_population`, `wars_won`, `tech_level`, `influence_max`). Se congela al publicar; no es modificable posteriormente.
- **Definiciones formales:**
  - `total_population`: máximo `kingdom.total_population` entre reinos del participante.
  - `wars_won`: máximo `kingdom.wars_won` entre reinos del participante.
  - `tech_level`: máximo `kingdom.tech_level` entre reinos del participante.
  - `influence_max`: máximo sobre reinos del participante de `count(tile) donde influence_map(tile) == kingdom.id`. Fuente: `engine.influence_map()`. Determinista y reproducible.
- **Métricas retiradas:** `gold` retirado de Nivel 1 por ausencia de economía simulada durable en modelo. No se introduce campo artificial.
- **Clasificación:** lexicográfica con `primary_metric` DESC, luego desempates: `tech_level` DESC, `wars_won` DESC, `total_population` DESC, `user_id` ASC interno. Si `primary_metric` coincide con una métrica de desempate, se omite la repetición. `influence_max` puede ser métrica principal pero no se usa como desempate.
- **Desempates (orden):** `tech_level` desc, `wars_won` desc, `total_population` desc, `user_id` asc (determinista).
- **Abandono:** participación marcada `abandonado`; no ocupa posición competitiva.
- **Participantes que no completan:** no aparecen en clasificación.
- **Mundos irrecuperables (409 DEC16):** participante excluido; el desafío continúa si hay al menos un completado.
- **Resultados inválidos:** nunca se aceptan puntuaciones enviadas por clientes; solo cálculo en servidor desde estado durable (DEC-F7D-05, DEC-F7D-07).
- **Empates reales:** desempate por `user_id` asc; si persiste, empate compartido.
- **No se usa tiempo de pared como criterio competitivo** (DEC-F7D-05).

### 17.5 Determinismo cross-process

- Misma `config_json` y misma `seed` producen estados iniciales y finales equivalentes, independientemente de `PYTHONHASHSEED` (DEC-F7D-06).
- El motor (`SimEngine`) consume RNG exclusivamente a través de `np.random.default_rng(seed)` en orden fijo; no usa `time`, `uuid`, `secrets` ni `os.environ` en el camino de `tick()`.
- `checkpoint_to_dict` / `engine_from_checkpoint` codifican y restauran el estado RNG (PCG64 validado).
- **Evidencia F7-D-R1:** dos procesos independientes con `PYTHONHASHSEED=1` y `PYTHONHASHSEED=98765`, misma `seed=12345` y 50 ticks, producen digest SHA-256 idéntico del estado canónico (`ff7593098a9f81e84f6584b2f0ba0de59f26796c2a0e78376d2d1987c91956d4`).
- Cualquier cambio al motor debe re-verificar esta propiedad antes de habilitar competición (DEC-F7D-06).

### 17.6 Integridad y verificación de resultados

- Cada envío de resultado de desafío es una operación T1/T2 de F7-C asociada al `world_id` del participante (DEC-F7D-07).
- Repetición con idéntica clave e idéntico contenido devuelve la operación original (idempotencia F7-C).
- Una caída entre cálculo, persistencia y ACK no genera duplicados ni resultados contradictorios (T2 atómico).
- Distinción contractual: resultado calculado / persistido / verificado / confirmado / disputado / invalidado.
- El cliente **no** puede enviar una puntuación para que sea aceptada como verdad; solo el servidor calcula desde estado durable (DEC-F7D-05, DEC-F7D-07).

### 17.7 Disputas y resolución administrativa

- Un participante puede presentar disputa sobre su propio resultado (`disputed`) (DEC-F7D-08).
- El organizador o administrador resuelve la disputa con una resolución administrativa auditable (actor, motivo, evidencia, fecha, identidad de operación, resultado, idempotencia).
- La identidad administrativa se verifica contra `is_admin` del servidor; no se confía en `admin_user_id` suministrado por el cliente.
- Los resultados confirmados no se sobrescriben silenciosamente; una corrección crea un nuevo registro que referencia al anterior.
- **Alcance de la invalidación (F7-D-I1-FIX10-A, aclaración de §17.14):** un resultado alcanza `invalidated` exclusivamente mediante este procedimiento (disputa previa + resolución administrativa). No existe invalidación directa desde `verified` o `confirmed` sin disputa y resolución.
- Se reutiliza `operation_resolutions` de F7-C cuando su semántica corresponda, sin convertirla en un sistema genérico de moderación.
- **Mecanismo de auditoría (F7-D-I1-FIX10-A-FINAL):** los campos exigidos (actor, fecha, identidad de operación, resultado, idempotencia) se materializan en la tabla propia `challenge_operations` (§17.2), insertada en la MISMA transacción de la operación: un fallo de COMMIT elimina a la vez el cambio de estado y el registro. `operation_resolutions` NO se reutiliza para esta procedencia: su semántica está ligada a `world_operations` (`world_id` + `operation_id` + `operation_sequence`, T1/T2), no corresponde a una operación de ámbito desafío, y reutilizarla implicaría reutilizar indebidamente la identidad o secuencia T1/T2 por mundo (DEC-F7D-15). La resolución `resolve` registra actor (`actor_user_id` administrador u organizador), timestamp (`created_at`), identidad (`client_idempotency_key` + `request_hash`), operación (`resolve`) y resultado (`outcome` = `confirm_all`/`invalidate_all`), con UNIQUE sobre el ámbito de §17.13.

### 17.8 Privacidad y exposición pública

- Un desafío público **no** vuelve públicos automáticamente los mundos de sus participantes (DEC-F7D-09).
- No se amplía `PUBLIC_WORLD_FIELDS` ni se expone `state_json` de terceros (DEC20 §14.4).
- Allowlists específicas para respuestas públicas de desafíos, separadas de los mundos.
- Identificadores internos de usuario no se exponen por defecto.
- Visitante: ve desafíos publicados y clasificación anónima (sin `user_id`).
- Participante inscrito: ve su propio mundo y resultado.
- Organizador: ve todos los mundos y resultados de su desafío (dentro de los límites DEC20 para mundos privados de terceros).
- Administrador autorizado: ve todo para resolución de disputas.
- **Clasificación estadística pública:** ordena resultados por métricas deterministas y públicamente documentadas. No utiliza etiquetas como "campeón", "ganador", "derrotado", "mejor jugador" o "habilidad superior"; utiliza etiquetas descriptivas como "posición estadística", "métrica comparada" o "resultado de simulación". Solo aparecen resultados `confirmed` (resultados `disputed` o `invalidated` no se incluyen en la clasificación confirmada).

### 17.9 API contractual

| Endpoint | Método | Autenticación | Autorización | Efecto persistente principal |
|---|---|---|---|---|
| `/api/challenges` | POST | Sí (Bearer) | Cualquier cuenta autenticada | Crea desafío en `draft` |
| `/api/challenges` | GET | No | Cualquiera | Lectura de desafíos publicados |
| `/api/challenges/{id}` | GET | No | Según visibilidad | Consulta desafío |
| `/api/challenges/{id}/publish` | PUT | Sí | Organizador | Publica (congela configuración) |
| `/api/challenges/{id}/open-enrollment` | PUT | Sí | Organizador | Abre inscripción (`draft`/`published` → `enrollment_open`, DEC-F7D-18) |
| `/api/challenges/{id}/enroll` | POST | Sí | Cualquier cuenta autenticada | Inscribe (T1 F7-C, idempotente) |
| `/api/challenges/{id}/participants` | GET | Sí | Según visibilidad | Lista participantes |
| `/api/challenges/{id}/close-enrollment` | PUT | Sí | Organizador | Cierra inscripciones |
| `/api/challenges/{id}/start` | PUT | Sí | Organizador | Inicia (congela participantes y reglas) |
| `/api/challenges/{id}/results` | GET | Sí | Según visibilidad | Consulta resultados |
| `/api/challenges/{id}/complete` | PUT | Sí | Organizador / sistema | Completa ejecución y calcula métricas (`running` → `completed`, DEC-F7D-18) |
| `/api/challenges/{id}/confirm` | PUT | Sí | Organizador / administrador (`is_admin` del servidor) | Confirmación formal de resultados `verified` → `confirmed` (idempotente, desafío `completed`; §17.14) |
| `/api/challenges/{id}/dispute` | POST | Sí | Participante inscrito | Presenta disputa |
| `/api/challenges/{id}/resolve` | POST | Sí | Organizador / administrador | Resuelve disputa |
| `/api/challenges/{id}/cancel` | PUT | Sí | Organizador | Cancela / invalida |

- **Endpoints operativos complementarios:** `open-enrollment` y `complete` no aparecen en la tabla original pero son necesarios para materializar transiciones de la máquina de estados aprobada (DEC-F7D-18: `published` → `enrollment_open`, `running` → `completed`). No son extensiones no autorizadas. `confirm` materializa la confirmación formal del resultado prevista en §17.14 y §17.13 (`confirmar/invalidar resultados` como operación de ámbito desafío), con la misma autorización organizador/administrador que `resolve` (§17.7, DEC-F7D-20).
- No se permiten endpoints que dejen al cliente establecer estados internos o resultados autoritativos (DEC-F7D-10).
- Errores HTTP siguen las convenciones de F7-A: 401 sin sesión, 403 sin permiso, 404 no revelación de mundo privado, 409 conflicto de idempotencia / mundo bloqueado, 503 infraestructura (DEC16).

### 17.10 Frontend

- Alcance mínimo: descubrimiento, detalle, inscripción, estado de participación, progreso, resultados, disputas (DEC-F7D-12).
- Se reutilizan componentes existentes (`App.jsx`, `StartScreen.jsx`, `RoomPanel.jsx`, `WorldMap.jsx`, `ChroniclePanel.jsx`, `StatsPanel.jsx`, `Comparator.jsx`, `Sidebar.jsx`).
- No se exige WebSocket; HTTP con polling es suficiente para resultados de desafío.
- No se reestructura el lobby ni la navegación general sin necesidad.
- No se implementan componentes durante R1.

### 17.11 Migraciones y compatibilidad

- Se crean las tablas `challenges` y `challenge_participants` con PK, FK, UNIQUE, CHECK e índices (DEC-F7D-11).
- `worlds` no se modifica salvo que una nueva decisión justificada lo demuestre; la alternativa preferida es no hacerlo (DEC-F7D-01).
- Integridad referencial: `ON DELETE CASCADE` de `challenge_participants.challenge_id` hacia `challenges` y de `challenge_participants.user_id` hacia `users.id`. La FK `challenge_participants.world_id` hacia `worlds.id` **no** usa `ON DELETE CASCADE` destructiva: eliminar un mundo no borra la participación ni su resultado histórico (DEC-F7D-16).
- Inscripción y creación de mundo como transacción coordinada sin segunda identidad de operación (DEC-F7D-02/14).
- No se ejecutan migraciones durante R1; no se rompe compatibilidad con F7-A, F7-B, F7-C.

### 17.12 Modelo de competición y agencia del participante (Corrección A)

- **Decisión (bloqueante):** **Alternativa C — Desafío de simulación sin agencia competitiva del participante.** La arquitectura actual **no dispone de ningún mecanismo de intervención del usuario sobre la simulación en curso**. `WorldRunner` solo expone `subscribe`, `unsubscribe`, `set_selected` (selección de reino para stats) y `start`/`close`; `SimEngine` consume RNG exclusivamente a través de `np.random.default_rng(seed)` en orden fijo y **no usa `time`, `uuid`, `secrets` ni `os.environ` en el camino de `tick()`**. `OperationParams` de F7-C solo define `operation_type="advance"` y `months` — no existe operación de decisión o acción del participante.
- **Dos conceptos distintos (DEC-F7D-21):**
  - **Comparación estadística reproducible:** presentación de métricas obtenidas de simulaciones reproducibles. Compara ejecuciones; no demuestra habilidad de participantes. Puede existir aunque los participantes no tengan capacidad de intervenir en la simulación.
  - **Competición real:** evaluación de participantes cuyas decisiones o acciones influyen de forma verificable en los resultados. Requiere agencia efectiva del participante, acciones autenticadas y atribuibles, validación del servidor, persistencia durable, reglas competitivas definidas y procedimiento verificable de evaluación. La arquitectura actual **no satisface estos requisitos**.
- **Modalidades (DEC-F7D-21):** la comparación estadística y la competición real son modalidades conceptualmente diferentes, pero no son necesariamente excluyentes en el conjunto del producto EvoWorld. Un desafío concreto debe declarar explícitamente su modalidad y aplicar exclusivamente las reglas correspondientes. La arquitectura actual de F7-D **Nivel 1 solo permite comparación estadística de simulaciones reproducibles**. Mientras no exista agencia efectiva y verificable, no se puede atribuir mérito competitivo a participantes. La incorporación futura de competición real requerirá una decisión arquitectónica adicional y un contrato específico. La coexistencia futura de ambas modalidades no autoriza a mezclar métricas estadísticas con resultados competitivos ni a cambiar retrospectivamente la modalidad de un desafío.
- **Consecuencia técnica:** dado el mismo `config_json` (seed, dimensiones, razas, modo, etc.), todos los participantes obtienen **estados idénticos**. Eso es una comparación de simulaciones reproducibles, no una competición significativa entre participantes.
- **Contrato:**
  - No se declaran rankings como resultados competitivos de participantes.
  - No se aceptan puntuaciones ni resultados autoritativos enviados por el cliente.
  - El servidor calcula cualquier resultado a partir del estado durable del mundo; el cliente nunca puede afirmar un resultado.
  - La participación competitiva se reduce a inscribirse en un desafío y que su mundo asociado ejecute `duration_ticks` ticks bajo las mismas reglas que los demás.
- **Restricciones:** no se inventan endpoints, comandos o capacidades de intervención que no existan en el código. Si en el futuro se desea una competición real con agencia del participante (Alternativa A o B), requiere una decisión arquitectónica nueva y una implementación de mecanismo de decisiones del usuario, que queda fuera de F7-D.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación no iniciada.
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17), fundamentado en inspección de `src/worldsim/server/runner.py`, `src/worldsim/sim/engine.py` y `src/worldsim/server/operation_service.py`.

### 17.13 Atomicidad e idempotencia de operaciones globales de desafío (Corrección C)

- **Decisión (bloqueante):** las operaciones de ámbito desafío (publicar, abrir/cerrar inscripciones, inscribir, iniciar preparación, iniciar ejecución, cancelar, registrar disputa, resolver disputa, confirmar/invalidar resultados) usan **transacciones SQL atómicas sobre la entidad `challenges` y las entidades relacionadas**, con identidad de petición idempotente. No se duplica el servicio T1/T2 de F7-C ni se crea un motor paralelo de ejecución.
- **Contrato:**
  - Cada operación global lleva una clave de petición idempotente (`client_idempotency_key`) y un hash del contenido canónico (`request_hash`, SHA-256 de la representación canónica versionada).
  - Ámbito de idempotencia: `(challenge_id, requesting_user_id, client_idempotency_key)`.
  - Misma clave y mismo contenido → devuelve el resultado de la operación original (sin duplicar efectos).
  - Misma clave y contenido diferente → conflicto explícito (409).
  - El cambio de estado y sus efectos relacionales persisten atómicamente en una misma transacción SQL.
  - La autorización se comprueba en el servidor usando la identidad autenticada (Bearer `user_id`); nunca se confía en un `admin_user_id` ni identidad de autoridad aportados por el cliente.
  - Una operación ya confirmada no se ejecuta otra vez; una operación en estado incierto no produce efectos duplicados.
- **Restricciones:** no se introduce un registro general de solicitudes globales salvo lo estrictamente necesario para operaciones de desafío; no se replica todo el ciclo T1/T2 de F7-C para operaciones de desafío.
- **Mecanismo implementado (F7-D-I1-FIX10-A-IDEM):** el registro de idempotencia y procedencia de estas operaciones vive en la tabla propia `challenge_operations` (§17.2), con `UNIQUE (challenge_id, requesting_user_id, client_idempotency_key)` que materializa el ámbito de idempotencia. El ámbito se evalúa **antes** de cualquier retorno temprano por estado para todas las operaciones globales: misma clave con `request_hash` distinto → `IdempotencyConflict` (409, §17.9); misma clave con el mismo hash → resultado original sin duplicar efectos, recuperando `original_outcome`. El registro se inserta con `flush()` en la transacción del llamador y se confirma con su COMMIT, junto con el cambio de estado (atómico: fallo previo al COMMIT → ni estado parcial ni registro huérfano). La capa HTTP impone `Idempotency-Key` obligatorio con validación regex, hash canónico por operación vía `compute_operation_request_hash`, y rechaza ausencia/invalidación con 400; no se generan claves de servidor.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación completada (F7-D-I1-FIX10-A-IDEM: idempotencia contractual HTTP con clave de cliente para todas las operaciones globales, 409 por hash distinto, replay con `original_outcome`, pruebas en `tests/test_f7d_idempotency_http.py` y regresiones verificadas).
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17), aplicando CONTRACTS.md §16 (F7-C) de forma proporcional.

### 17.14 Cadena de procedencia y verificación de resultados (Corrección G)

- **Decisión (bloqueante):** cadena verificable entre configuración inmutable, identidad del participante, mundo asociado, operaciones competitivas aceptadas, secuencia durable, eventos confirmados, checkpoints, estado final, métricas calculadas y resultado persistido.
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
- **Matriz de transiciones del resultado (F7-D-I1-FIX10-A): interpretación conforme a las decisiones aprobadas DEC-F7D-18 (máquina `disputed → resolved`), DEC-F7D-19 (estados), DEC-F7D-22 (terminología) y a §17.7 (disputas y resolución administrativa). No crea estados, transiciones ni permisos nuevos:**
  - Transiciones válidas: `calculated → persisted` (escritura del resultado en `result_json`), `persisted → verified` (comprobación de precondiciones y procedencia), `verified → confirmed` (confirmación formal: organizador o administrador `is_admin`, idempotente, solo desde desafío `completed`), `verified → disputed` y `confirmed → disputed` (disputa del participante sobre su propio resultado), `disputed → confirmed` (resolución favorable `confirm_all`) y `disputed → invalidated` (resolución desfavorable `invalidate_all`), ambas por resolución administrativa de organizador o administrador.
  - Transiciones prohibidas: `calculated → confirmed` y `persisted → confirmed` (sin verificar); confirmación ordinaria de `disputed` (solo la resolución favorable puede llevar `disputed → confirmed`); cualquier transición de salida desde `invalidated` (**`invalidated` es terminal**: no se reabre, no se confirma, no se disputa, no se resuelve de nuevo); y la invalidación directa `verified → invalidated` o `confirmed → invalidated` sin disputa previa.
  - **`verified → invalidated` directa no está autorizada por el contrato:** el único procedimiento que alcanza `invalidated` es el de §17.7 (disputa del participante + resolución administrativa). Por tanto la única vía válida es `verified/confirmed → disputed → invalidated`; no se implementa una invalidación directa por conveniencia.
  - Persistencia y verificación: durante la completitud del desafío (`running → completed`) el resultado se escribe en `result_json` (`persisted`) y, en la misma transacción atómica, se comprueban sus precondiciones y se registra la operación de procedencia `complete`; el estado final `verified` se consolida con el COMMIT de esa transacción. No se inventan transiciones artificiales fuera de la completitud ni se omiten las contractuales.
  - `result_status` es la **fuente de verdad**; `result_verified` es un campo de compatibilidad sin autoridad que debe sincronizarse en cada transición: `calculated=0`, `persisted=0`, `verified=1`, `confirmed=1`, `disputed=0`, `invalidated=0`.
  - Pruebas: `tests/test_f7d_state_machine.py` (transiciones válidas y prohibidas, sincronización, idempotencia, autorización organizador/administrador/participante/usuario ajeno, atomicidad ante fallo, terminalidad de `invalidated`).
- **Restricciones:** un hash o checkpoint por sí solos **no** prueban la validez de una ejecución; verifican integridad del estado, no la corrección competitiva. `verified` acredita verificaciones técnicas definidas por el contrato; no acredita habilidad. `confirmed` confirma el resultado estadístico; no certifica una victoria competitiva. Ninguno de estos estados demuestra por sí mismo habilidad, mérito, superioridad o victoria competitiva, incluso en una futura modalidad competitiva. En dicha modalidad futura, `verified` acreditará exclusivamente verificaciones técnicas y `confirmed` acreditará exclusivamente la confirmación formal del resultado; el mérito competitivo requerirá reglas de evaluación explícitas, acciones atribuibles al participante, evidencia durable y cálculo conforme a esas reglas, y será una propiedad del resultado y sus evidencias, nunca una inferencia derivada del estado `verified` o `confirmed`. Si alguna fase de la cadena no puede verificarse con la infraestructura actual, se documenta como capacidad pendiente, no como garantía implementada.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-A); implementación en curso (F7-D-I1-FIX10-A: estados y transiciones del resultado implementados y probados en `tests/test_f7d_state_machine.py`; la verificación completa de las fases de la cadena que la infraestructura actual no soporta sigue documentada como capacidad pendiente).
- **Origen:** F7-D-R1-A corrección contractual (2026-11-17).

### 17.15 Reglas transversales aplicables a F7-D

1. Los contratos de esta sección son **vinculantes** (regla transversal 1 de CONTRACTS.md).
2. Al revisar, contrastar **siempre contra el código real**, no solo contra este texto (regla transversal 2).
3. Ampliar un contrato requiere decisión aprobada (DEC); corregir este archivo por conveniencia está prohibido (regla transversal 3).
4. `DECISIONS.md` contiene el "porqué" y estado de aprobación; `CONTRACTS.md` contiene el "cómo" verificable.

### 17.16 Semántica de resultados sin agencia del participante (Corrección B / DEC-F7D-22)

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
- **Estado:** Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada.
- **Origen:** F7-D-R1-B corrección contractual (2026-11-17).

### 17.17 Clasificación estadística pública (Corrección C / DEC-F7D-23)

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

### 17.18 Niveles de evolución de F7-D (Corrección E / DEC-F7D-24)

- **Decisión:** F7-D define dos niveles de evolución:
  - **Nivel 1 — Comparación estadística reproducible:** es el alcance implementable de F7-D bajo la arquitectura actual. Incluye entidades, inscripción, preparación, ejecución, resultados, persistencia, privacidad y clasificación estadística conforme a los contratos. No representa una competición basada en decisiones de participantes.
  - **Nivel 2 — Competición con agencia:** queda expresamente fuera del alcance actual. Requiere una nueva decisión arquitectónica que defina modelo de acciones de participantes, validación y autorización, aplicación durable y determinista, atribución de acciones, reglas competitivas y evaluación/resolución de resultados.
- **Contrato:**
  - El Nivel 2 no es una dependencia artificial para implementar el Nivel 1.
  - El Nivel 1 puede implementarse sin el Nivel 2.
  - No se implementa ni diseña en detalle el Nivel 2 durante F7-D-R1-B.
- **Estado:** Aprobada / Contrato definido (F7-D-R1-B); implementación no iniciada.
- **Origen:** F7-D-R1-B corrección contractual (2026-11-17).


 
## 18. Galería pública con mundos destacados (F7-E.4 / DEC-F7E-01)

### 18.1 Alcance y proyección pública

- La galería pública solo consulta mundos con visibility=public y deleted_at IS NULL.
- La proyección pública añade is_highlighted, booleano derivado de la existencia de un registro de destacado activo y vigente. No es una columna persistida de WorldRow.
- La ficha pública equivalente servida por HTTP y el mensaje público de ficha por WS pueden incluir el mismo booleano. Esto no amplía permisos, suscripciones ni acceso a estado vivo; se mantienen las reglas de DEC20 para visitantes y propietarios.
- El campo no incluye ni permite inferir mediante la respuesta el ID del destacado, su fecha de concesión, el administrador que lo concedió ni otros metadatos internos.
- Las proyecciones privadas de mine=1 mantienen el contrato F7-B existente y no incorporan is_highlighted por esta decisión.
- El destacado nunca cambia visibility, no convierte un mundo privado en público y no concede permisos adicionales.

### 18.2 Determinación de vigencia

- is_highlighted=true únicamente si existe un destacado activo cuya expires_at sea posterior al instante de evaluación, o no tenga expiración cuando el contrato de F7-E.1 lo permita.
- Un destacado retirado o vencido produce is_highlighted=false y no otorga prioridad.
- La retirada y la expiración conservan el registro histórico. Se preservan las reglas de retención de F7-E.1 y la eliminación lógica de F7-E.2-I.
- La elegibilidad, la validación no destructiva del checkpoint y la autoridad para conceder o retirar siguen siendo las de F7-E.3; este contrato no las redefine.

### 18.3 Orden y cursor compuesto

- Orden total: is_highlighted DESC, id DESC.
- El cursor es opaco y versionado. Su contenido lógico representa la pareja (is_highlighted, id) de la última fila emitida; el cliente no construye ni interpreta esa estructura.
- El servidor valida versión, tipos, integridad/formato y compatibilidad del cursor con los filtros de consulta. Un cursor inválido o incompatible se rechaza con HTTP 400.
- La página siguiente continúa por ID descendente dentro del grupo destacado correspondiente y, agotado ese grupo, continúa por el grupo no destacado en ID descendente.
- Los filtros mine, search y mode forman parte del contexto de consulta. Cambiar cualquiera de ellos exige reiniciar la paginación; no se reutiliza el cursor anterior. Se conservan los límites de página de F7-B.
- Cada página calcula la condición de destacado vigente en el momento de la consulta. No hay snapshot transaccional entre páginas: una concesión, retirada o expiración concurrente puede cambiar el orden y causar duplicados o saltos. El cliente debe reiniciar la consulta para obtener una vista coherente.

### 18.4 Resoluciones técnicas H1–H3

- **H1 — Vinculación del cursor:** el contexto normalizado y versionado del cursor incluye `mine`, `search`, `mode` y `limit`. Cualquier cambio en uno de esos cuatro parámetros invalida el cursor y exige reiniciar la paginación, conforme a §15.2. La validación ocurre antes de ejecutar la consulta. El cursor no concede autorización ni sustituye los filtros SQL obligatorios.
- **H2 — Concesión después de expiración:** la operación administrativa de concesión, dentro de una única transacción, revalida la elegibilidad del mundo y busca el registro anterior que siga `is_active=true` pero tenga `expires_at` vencido. Si existe, lo cambia a `is_active=false` y conserva íntegra esa fila histórica; después crea el nuevo registro. La transición y la inserción son atómicas, de modo que el índice único parcial no queda bloqueado por una fila expirada. La consulta de galería y cualquier GET no modifican registros ni limpian expiraciones. Un destacado aún vigente no se desactiva automáticamente para permitir una segunda concesión.
- **H3 — Checkpoint elegible no destructivo:** consultar únicamente candidatos con `is_valid=true`, ordenados por `sequence DESC`. Verificar el SHA-256 directamente sobre los bytes del payload persistido y elegir el primer candidato cuyo checksum coincida. Si el candidato más reciente está corrupto, continuar con el siguiente candidato anterior; si ninguno pasa, el mundo no es elegible. La evaluación no actualiza `is_valid`, no persiste invalidaciones ni reparaciones, no avanza ni recupera el mundo y no reutiliza `CheckpointRepository.get_latest_valid_checkpoint()` ni `RecoveryManager.recover()`.

### 18.5 Privacidad y compatibilidad

- Ningún mundo privado ni eliminado lógicamente aparece en la galería pública.
- La condición de destacado no modifica la autorización HTTP ni WS, ni crea una suscripción al runner para visitantes.
- Se mantiene la allowlist de DEC20, ampliada únicamente con el booleano is_highlighted para las proyecciones públicas. No se exponen state_json, owner_user_id, identidad del administrador ni datos operativos internos.
- El contrato no introduce rankings globales ni modifica el orden o las respuestas privadas de mine=1.

### 18.6 Criterios de aceptación

1. Ningún mundo privado aparece en la galería pública.
2. Ningún mundo con deleted_at no nulo aparece en la galería pública.
3. is_highlighted es siempre booleano en las proyecciones públicas y nunca contiene metadatos.
4. Los destacados vencidos o retirados dejan de marcarse y de recibir prioridad.
5. El orden es exactamente is_highlighted DESC, id DESC.
6. La paginación atraviesa correctamente ambos grupos sin omitir la continuación lógica del cursor.
7. Los cursores inválidos o incompatibles con `mine`, `search`, `mode` o `limit` se rechazan con HTTP 400.
8. Cambiar cualquiera de esos cuatro parámetros obliga a reiniciar la paginación.
9. La concesión posterior a la expiración desactiva atómicamente el registro vencido anterior, conserva su historial y no muta datos desde GET.
10. La elegibilidad selecciona el checkpoint íntegro más reciente entre candidatos `is_valid=true`, sin mutar ni persistir cambios.
11. La retirada/expiración conserva el historial.
12. La condición de destacado no altera permisos HTTP/WS ni expone estado vivo adicional.
13. Se mantienen autoridad y elegibilidad de F7-E.3.

- **Estado:** contrato aprobado; implementación y pruebas pendientes.
