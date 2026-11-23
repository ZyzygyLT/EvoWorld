# F7-E.2-I-BLOCK3-PRE-REPORT — Auditoría de exclusión de mundos eliminados

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK3-PRE — auditoría exclusivamente de lectura.
**Archivos modificados durante esta tarea:** **ninguno** (único archivo nuevo: este informe).
**Ningún filtro implementado. Bloque 3 de implementación NO iniciado.**

---

## 1. Inventario exhaustivo de consultas a `WorldRow`

Búsqueda global `WorldRow` en `src/` (64 coincidencias, 11 archivos) + búsquedas
`select(WorldRow…)`, `.get(WorldRow…)`, `session.get(WorldRow…)` y
`deleted_at`. Cobertura verificada: **no existe ninguna otra consulta a
mundos en el código de producción**. `export/checkpoint.py` (intent-to-add,
ajeno) no referencia `WorldRow`. Hoy `deleted_at` aparece **solo** en
`db.py:38` (columna) y `runner.py:1019–1033` (`_delete_world_row`): **cero
filtros de lectura en todo el árbol** (estado coherente con "Bloque 3 no
iniciado").

### 1.1 `src/worldsim/server/app.py`

| # | Ubicación | Función / endpoint | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| A1 | `app.py:431–466` | `list_worlds` — `GET /api/worlds` (público y `mine`, con search/mode/cursor) | listado | **No** | **Sí** | Regla 4; PRE §3 filas "listado público" y "listado `mine`"; DEC20 (prohibido exponer mundos privados → aquí además eliminados) |
| A2 | `app.py:553–558` (`db_.get`) | `_load_row` — **choke point** de carga individual | carga individual | **No** | **Sí** | **DEC-PRE-4**: 404 indistinguible incl. owner. Alimenta: `get_world` (668), `put_world_visibility` (698), `delete` gate… `events` (1200), WS (2103) y vía `_load_visible_row` (615): stats (760), predictions GET/POST (785/797), leaderboard (814), chat GET/POST (823/848), claims GET/POST (878/890), standings (915) — **16 usos** |
| A3 | `app.py:521–548` | `create_world` — `POST /api/worlds` (construcción + `registry.get`) | creación | n/a | — | Nace `deleted_at = NULL`; sin filtro posible ni necesario |
| A4 | `app.py:710` (`live = db_.get`) | `put_world_visibility` — relectura para escritura | escritura (visibility) | **No** | **Sí** (vía A2 + defensa) | Regla 5: no modificar mundos eliminados. Gate A2 en 698 cubre; persiste TOCTOU 698→710 (mundo eliminado en medio) → filtro aquí lo cierra |
| A5 | `app.py:729` (`db_.get`) | `delete_world` — identificación + auth para `DELETE` | identificación de borrado | **No** | **No (decisión documentada, §3.4)** | DEC-PRE-3 paso 8: la eliminación repetida "**retorna éxito al caller HTTP**" → el read del endpoint **no** se filtra (mantener 200 idempotente) |
| A6 | `app.py:1290–1295` (`get_existing` + `db_.get` fallback) | `_canonical_month` | lectura de mes canónico | **No** | Defensa (vía A2) | Sus 3 callers (800, 851, 895) pasan antes por `_load_visible_row`; fallback `row else 0` ya tolera `None` ⇒ con A2+A… filtrado devuelve 0. Filtro explícito opcional (defensa en profundidad) |

### 1.2 `src/worldsim/server/repository.py`

| # | Ubicación | Método | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| R1 | `repository.py:119–124` | `get_world` | carga individual | **No** | **Sí** | **"Implementación recomendada" del PRE §3** (filtro en capa de datos, no por endpoint). Caller: `transitions._write_status:167` y `recovery_manager.recover:95` |
| R2 | `repository.py:126–131` | `get_worlds_by_status` | listado por estado | **No** | **Sí** | Regla 4 + PRE §3 (default `WHERE deleted_at IS NULL`). **Sin callers en producción** (grep verificado) |
| R3 | `repository.py:143–148` | `update_status` | escritura de transición | **No** | **Sí** | Regla 5: las transiciones ordinarias no modifican mundos eliminados. Caller: `_write_status:173` y `_revert_status:195` |
| R4 | `repository.py:171–181` | `update_checkpoint_metadata` | escritura de metadata | **No** | **Sí** | Regla 5. **Sin callers en producción** (grep verificado) |
| R5 | `repository.py:275–277` (+ update ~294) | `CheckpointRepository.save_checkpoint` — check de existencia + update de metadata | persistencia de checkpoint | **No** | **Sí** | Regla 5 + PRE §3 fila "recolección de checkpoints" ("persisten pero no son operativos" → **no escribir NUEVOS** checkpoints sobre mundos eliminados). Callers: `persistence_service.py:324`, `operation_service.py:564/648`. **Nota de carrera:** ventana runner-en-cierre (§4) |

### 1.3 `src/worldsim/server/transitions.py`

| # | Ubicación | Función | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| T1 | `transitions.py:165–176` (`_write_status`, worker thread) | transición persistida (pausa/reanudación) vía R1+R3 | transición | **No** (hereda R1/R3) | **Sí vía R1/R3** — **sin cambio en este archivo** | Regla 5. Hoy: con la marca ya liberada, una transición tardía **escribiría `status` sobre una fila eliminada** (viola regla 5); con R1/R3 filtrados → `WorldNotFoundError` (semántica idéntica al borrado físico previo, ya documentada en su docstring) |
| T2 | `transitions.py:193–198` (`_revert_status`) | reconciliación de escritura confirmada | transición | No (hereda R3) | Sí vía R3 | Con R3 filtrado: fila eliminada → `update_status` → `False` → "row vanished; nothing to restore" — **ya es la rama manejada** (563–568) |

### 1.4 `src/worldsim/server/recovery_coordinator.py`

| # | Ubicación | Función | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| RC1 | `recovery_coordinator.py:461–465` (`_list_world_rows`) | enumeración de **Etapa A del startup** → `recover()` → `runner_factory` → `register_existing` | recuperación/registro en startup | **No** | **Sí — obligatorio** | **Regla 2** + DEC-PRE-2 (filas 38–39: "El startup recovery debe excluir worlds con `deleted_at IS NOT NULL`") + PRE §3 fila "Recuperación de runners startup" |
| RC2 | `recovery_coordinator.py:490` (`get_existing`) | verificación Etapa B (runner sigue registrado) | verificación de startup | n/a | Ninguna | Ver §2, callsite 4 |

### 1.5 `src/worldsim/server/recovery_manager.py`

| # | Ubicación | Función | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| RM1 | `recovery_manager.py:93–102` → `get_world` (95) | `RecoveryManager.recover` — lectura de elegibilidad + `status` | recuperación | **No** (hereda R1) | **Sí vía R1** — sin cambio propio | Regla 2 en capa de datos; RC1 ya filtra antes (defensa en profundidad, doble barrera). **Único caller:** `recovery_coordinator._stage_a:412` |

### 1.6 `src/worldsim/server/league.py`

| # | Ubicación | Función | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| L1 | `league.py:79–85` | `claim_kingdom` — lectura de `mode` + escritura de `ClaimRow` | transición/escritura | **No** | Cubierto por gate (app 890 `_load_visible_row` → A2) | Regla 5/1: el POST de claims pasa por A2 ⇒ mundo eliminado → 404 antes de llegar aquí. Filtro explícito opcional (defensa) |
| L2 | `league.py:124–136` | `claim_kingdom` — relectura de `state_json` para el nombre | lectura derivada | No | Cubierto por L1/gate | Mismo sesión/ventana que L1 |
| L3 | `league.py:274–281` | `maybe_end_season` — **escritura** `season_over` | transición de temporada (desde tick del runner) | **No** | **Sí (defensa, opcional)** | Regla 5. Hoy inalcanzable para mundos eliminados (el runner se cierra antes del mark; sin runner no hay ticks) — filtro defensivo de bajo riesgo: `row is None` ya está manejado |

### 1.7 `src/worldsim/server/challenge_service.py`

| # | Ubicación | Función | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| CS1 | `challenge_service.py:752–772` | `prepare` — construcción de `WorldRow` por participante | creación | n/a | — | Nace `deleted_at = NULL` |
| CS2 | `challenge_service.py:1039–1057` | `complete`/resolución — `session.get(WorldRow, …)` + **escritura** de `months_simulated`/`state_json` | resolución de desafío (escritura) | **No** | **DECISIÓN PENDIENTE (§3.5)** | Regla 5 (no modificar) vs regla 6 (operación de completitud con intención explícita). Hoy: fila ausente → `WorldNotFoundForParticipant`. Filtrar cambiaría el resultado de un desafío en curso si un participante borró su mundo |

### 1.8 `src/worldsim/server/runner.py`

| # | Ubicación | Función | Tipo | Filtra hoy | Debe excluir | Justificación |
|---|---|---|---|---|---|---|
| RN1 | `runner.py:1016–1034` (`_delete_world_row`, worker) | **la propia eliminación lógica**: `row is None → WorldNotFoundError`; `if row.deleted_at is None: mark` | escritura de borrado | **No — y NO debe filtrar** | **Excepción deliberada (única)** | Es la ruta que **establece** la marca; un filtro aquí impediría eliminar (y rompería DEC-PRE-3, que exige leer la fila marcada para el no-op). Documentar como excepción explícita en Bloque 3 |
| RN2 | `runner.py:1086` (`get_existing`) | verificación dentro de `safe_delete_world` | interno del borrado | — | Ver §2, callsite 1 | **No tocar** (FIX9) |

### 1.9 Otros

- `operation_service.py:54`: `import WorldRow` **sin uso** (import muerto; grep sin referencias posteriores) — sin query.
- `errors.py`: solo comentarios. `persistence_service.py`: escribe vía R5.
- **Destacados (`world_highlights`):** no existe **ninguna** tabla, query ni endpoint de destacados en `src/` (grep `highlight` → solo un docstring de `engine.py:657`). Regla 4 "destacados": **nada que filtrar hoy**; cualquier implementación futura nacerá con `is_(None)` (DEC-PRE-5 prohíbe reactivación en F7-E.2).

---

## 2. Auditoría de `get_existing()` — reconciliación de callsites

**8 callsites de producción** (los 7 de FIX9 + `transitions.py:473` que FIX9
reportó como faltante). Contrato vigente (FIX9 + `runner.py:792–798`):
`get_existing` **no** chequea `_deleting` — "reads stay allowed during a
deletion" — y `safe_delete_world` **depende** de ese comportamiento para
cerrar el runner registrado. **No se globaliza ningún cambio.**

| # | Callsite | Contexto | ¿Runner durante `_deleting`? | ¿Opera sobre mundo eliminado? | Veredicto |
|---|---|---|---|---|---|
| 1 | `runner.py:1086` (`safe_delete_world`) | dentro del lock por mundo, marca activa | **Sí — es el diseño** (obtener el runner a cerrar) | Sí (no-op idempotente, DEC-PRE-3) | **Ninguna modificación.** Cambiar `get_existing` aquí o globalmente **rompe DEC10/FIX9** (el flujo nunca encontraría el runner a cerrar) |
| 2 | `transitions.py:473` (`_critical_section`) | bajo lock del coordinador; `is_deleting` verificado en 468 **sin `await` entre** → atómico en el loop | **No** (rechazado antes: `TransitionError`) | Post-commit + marca liberada: `runner is None` → PAUSED intentaría `_write_status` | **Ninguna en `get_existing`.** La corrección es vía **R1/R3** (fila filtrada → `WorldNotFoundError`); hoy esa secuencia **escribiría status sobre fila eliminada** (viola regla 5) |
| 3 | `transitions.py:367` (`_apply_in_memory`) | tras escritura confirmada; `is_deleting` en 360 **sin `await` entre** → atómico | No (se omite el apply y se reconcilia) | Mismo caso que 2 | **Ninguna.** Igual que 2: corrección vía R1/R3 |
| 4 | `recovery_coordinator.py:490` (Etapa B) | verificación de startup | No (startup termina antes de servir; `delete_world` solo tras lifespan) | No (RC1 filtra antes) | **Ninguna** |
| 5 | `app.py:850` (`post_chat` broadcast) | tras A2 + guard DEC16 | Puede devolver el runner **en cierre** → `_broadcast_system` → `_send_all` es **best-effort** (no lanza; descarta suscriptores muertos, `runner.py:768–776`) | **No** (gate A2 → 404 antes) | **Ninguna** |
| 6 | `app.py:900` (`post_claim` broadcast) | ídem 5 | Ídem 5 | No (gate A2) | **Ninguna** |
| 7 | `app.py:1290` (`_canonical_month`) | tras gates de sus 3 callers | Sí → devuelve `eng.month` (correcto: aún sin commit de eliminación) | Runner `None` + fallback `db_.get` → `row.months_simulated if row else 0` **ya tolera `None`** | **Ninguna** (A2 cubre el fallback; filtro explícito opcional) |
| 8 | `app.py:2163` (WS `speed`, tercero) | socket abierto desde el handshake (gate 2103) | Puede devolver runner en cierre → `target.speed = val` (asignación de atributo; **no** arranca tareas; `start()` tiene guards propios) | No nuevo: el socket ya pasó gates; velocidad sobre runner cerrado = inofensivo | **Ninguna** |

**Conclusión §2:** `get_existing()` **no se modifica**. La exclusión de
mundos eliminados se logra en la capa de datos (R1/R3 y A2), preservando el
uso interno de `safe_delete_world()`.

---

## 3. Contrato de exclusión — verificación y contradicciones

### 3.1 Reglas 1–8 vs código actual

| Regla | ¿Cumple hoy? | Evidencia |
|---|---|---|
| 1. Consultas ordinarias excluyen `deleted_at IS NOT NULL` | **No** | Cero filtros en el árbol (grep §1); A1, A2, R1–R5, RC1, RM1, L1–L3, CS2 sin filtrar |
| 2. Startup no recupera/registra eliminados | **No** | `RC1` sin filtro → `_stage_a` recuperaría y **registraría runners** de mundos eliminados (viola DEC-PRE-2) |
| 3. GET por ID → 404 indistinguible (incl. owner) | **No** (pendiente por diseño desde Bloque 2) | `get_world:668` → `_load_row` sin filtro → **owner recibe 200 + `state_json` completo** de un mundo eliminado |
| 4. Listados público/privado/destacados excluyen | **No** (A1); destacados: inexistente | A1 sin filtro; no hay endpoint de destacados (§1.9) |
| 5. Transiciones ordinarias no modifican eliminados | **No** | `update_status`/`_write_status` sin filtro → escritura posible sobre fila eliminada (T1); `put_visibility` (A4) y `save_checkpoint` (R5) idem |
| 6. Acceso histórico/administrativo explícito conservable | **Sí, por ausencia** | No se elimina ningún camino histórico: challenges (CS2), checkpoints (R5 existente), `WorldNotFoundForParticipant` siguen disponibles para decisión explícita |
| 7. Sin restauración en F7-E.2 | **Sí** | grep `deleted_at` en `src/`: solo `db.py:38` y `runner.py` (marcado); **ningún** `deleted_at = NULL` ni endpoint de restore |
| 8. Filtros con `is_(None)` (sin mezcla naive/aware) | **No aplica aún** (no hay filtros); **requisito para Bloque 3** | SQLite devuelve `deleted_at` naive al releer (probado en BLOCK2-PRE) → solo `is_(None)` / `IS NOT NULL`; **prohibido** comparaciones de rango aware-vs-naive |

### 3.2 Contradicciones con documentos — resolución

1. **DEC-PRE-4 vs matriz PRE §3** (`F7-E.2-PRE-DECISION.md:95` "404 **o
   representación pública vacía**" y `:102` owner "⚠️ Conditional… dependiendo
   de política administrativa"): **resuelta por DEC-PRE-4**
   (`F7-E.2-PRE-DECISION-FIX.md:75–87`): 404 indistinguible **incluso para el
   owner**, sin campo `deleted`. DEC-PRE-4 es posterior y específica →
   gobierna. La fila condicional queda **derogada**.
2. **DEC-PRE-5 vs PRE-DECISION.md:79** ("recuperación administrativa puede
   limpiar `deleted_at`"): **resuelta por DEC-PRE-5** (`FIX.md:89–104`):
   restauración **fuera de F7-E.2**; no se implementa nada que ponga
   `deleted_at = NULL`.
3. **Regla 3 vs DEC-PRE-3** (¿`DELETE` repetido → 404 o 200?): **no es
   contradicción** — DEC-PRE-4 rige **GET**; DEC-PRE-3 paso 8 exige "**éxito
   al caller HTTP**" en repetición de DELETE. Coherente con el comportamiento
   ya aceptado en 2b (del_06 → no-op con éxito). Ver §3.4.
4. **Regla 5 vs `_write_status` actual** (§1.3): contradicción **real** que
   el Bloque 3 resuelve filtrando R1/R3 — sin tocar `transitions.py`.
5. **Regla 6 vs CS2**: pendiente de decisión (§3.5).

### 3.3 Excepción deliberada (documentar en Bloque 3)

`_delete_world_row` (`runner.py:1016–1034`) es la **única** ruta que debe
leer/operar sobre filas con `deleted_at IS NOT NULL` (marcar, y no-op
idempotente en DEC-PRE-3). Cualquier filtro "global por defecto" del PRE §3
debe **excluir explícitamente** esta función.

### 3.4 Decisión: read del endpoint `DELETE` (A5)

- **Opción recomendada: NO filtrar A5** — DEC-PRE-3 paso 8 ("éxito al caller
  HTTP") y la semántica aceptada en 2b/del_06: repetir DELETE → `200
  {"deleted": id}` con `safe_delete_world` no-op. La auth/visibilidad del
  endpoint sigue operando sobre la fila (que persiste).
- Opción alternativa: filtrar A5 → 404 en repetición (más parecido al
  borrado físico histórico) pero **contradice** DEC-PRE-3 paso 8.
- **Requiere autorización explícita** antes de implementar cualquiera.

### 3.5 Decisión: resolución de challenge sobre mundo eliminado (CS2)

- **Opción A (regla 5 estricta):** filtrar → el `session.get` no ve la fila →
  `WorldNotFoundForParticipant` → un participante que borró su mundo **falla
  la completitud** del desafío.
- **Opción B (regla 6):** acceso explícito de completitud (la resolución es
  una operación del sistema con intención declarada) → mantener CS2 sin
  filtro, documentado como excepción.
- **Requiere decisión de autorización.** No se elige en esta auditoría.

---

## 4. Callers y carreras — secuencias temporales

### 4.1 Ventanas de `safe_delete_world()` (evidencia: `runner.py:1082–1121`)

`begin_delete` (marca síncrona) → lock → `get_existing` → `close` →
verificar `CLOSED` → `unregister` → worker `UPDATE deleted_at` (`shield`) →
`end_delete` (en `finally` o callback transferido).

| Ventana | Estado de fila | Estado de marca | Exposición hoy (sin filtros) | Tras Bloque 3 |
|---|---|---|---|---|
| **W1:** pre-inicio | `deleted_at NULL` | inactiva | GET 200 (correcto) | igual |
| **W2:** `[begin_delete … commit]` | aún `NULL` (no commit-eada) | **activa** | GET **200** (correcto: la eliminación **aún no está persistida**; `get()`/`register_existing()` ya rechazan) | GET 200 (mismo: aún no eliminado) — **ventana residual documentada, no evitable sin transacción atómica registry+BD** |
| **W3:** `[commit … end_delete]` | **`NOT NULL`** | activa (hasta que el work termine) | **GET 200 → VIOLACIÓN de reglas 1/3** (el PRE §7.2 ya la llamó "residual conocido → Bloque 3") | **404/listado excluido inmediatamente al caer el commit** (filtros sobre la fila), mientras la guarda sigue protegiendo `get()` |
| **W4:** post-`end_delete` | `NOT NULL` | inactiva | GET 200 (violación) | 404 (DEC-PRE-4) |

### 4.2 Escenarios pedidos

1. **Caller obtiene runner justo antes de `begin_delete`:** `get()` devolvió
   el runner; la eliminación lo cierra (W2). El runner posee guards propios
   (`start()`/`_spawn` no reinician un cerrado); un WS con referencia puede
   seguir mandando `speed` (atributo, inofensivo — callsite 8) y sus
   `pause`/`resume` son rechazados por `is_deleting` → `TransitionError`
   (manejado en `app.py:2186`). **Sin modificación necesaria.**
2. **`get_existing()` con mundo en `_deleting`:** permitido por contrato
   (§2). Los dos llamadores de transición tienen la comprensión de marca
   **atómica** (sin `await` entre el chequeo y `get_existing`); los de app
   están tras gates o son best-effort. **Sin modificación necesaria.**
3. **Transición después de `unregister()` pero antes del commit de
   `deleted_at`:** la **marca sigue activa** (`end_delete` solo tras
   completar el work: 1117–1121 o el callback 1112–1114) →
   `_critical_section:468` lanza `TransitionError` → **ninguna escritura**.
   La ventana está cubierta por la guarda, **no** por un filtro de fila.
   Tras el commit y la liberación (W4): gates A2 → 404 en HTTP; en WS,
   `_write_status` con **R1/R3 filtrados** → `WorldNotFoundError` → log en
   `app.py:2193`. **Corrección vía repositorio, sin tocar `transitions.py`.**
4. **Worker activo tras cancelar el caller:** refcount transferido (2c.7
   verificado en harness): la guarda permanece hasta que el work termine →
   W3 con guarda activa (tabla §4.1). Tras terminar, callback consume el
   resultado y libera (`_release_lingering_delete` + `end_delete`).
5. **Segunda eliminación mientras el worker anterior está activo:**
   serializada por lock+refcount por mundo (2c.7: refcount `1→2→{}`, segunda
   no completa ni altera la primera marca).

### 4.3 **RESERVA EXPLÍCITA (2c.7 §5) — no declarar resuelta**

La **carrera de dos escrituras con lecturas previas al commit ajeno** (p. ej.
dos sesiones SQLAlchemy concurrentes que leen `deleted_at = NULL` antes del
commit de la otra) **NO está demostrada ni resuelta**. La guarda
serializa llamadas a `safe_delete_world()` para **el mismo mundo**, pero eso
**no** demuestra que `_write_status`/`save_checkpoint`/`claim_kingdom` (otras
sesiones) no puedan leer estado pre-commit y escribir sobre una fila que
acaba de ser marcada. **No se afirma lo contrario.** Eliminarla exigiría
mecanismos atómicos en producción (`UPDATE … WHERE deleted_at IS NULL`,
checks condicionales) — **fuera del Bloque 3 salvo decisión expresa**.

---

## 5. Compatibilidad y fallos preexistentes (sin corregir)

Defectos §7.1 del PRE, **sin tocar** y con su efecto sobre la validación del
Bloque 3:

| Defecto preexistente | Suites afectadas | Condicionamiento del Bloque 3 |
|---|---|---|
| `WorldRow` sin `status`/`visibility` → `TypeError: … invalid keyword argument` | repository (33f), pause (20e), recovery (35f), server (4f), combo (1f) | **R2/R3 no pueden validarse en verde** hoy (`get_worlds_by_status`/`update_status` ya fallan antes del filtro). Validación posible: identidad de fallos vs línea base 2d (**sin fallos nuevos**) + tests unitarios sobre harness que no construyan `WorldRow(status=…)` |
| `ensure_schema(engine)` arity → `TypeError` | f7d (26f), schema (20f), conftest | **La app no arranca** → los tests end-to-end de DEC-PRE-4 (GET→404 vía `test_server`/`test_visibility`) **no son ejecutables en verde** hasta que se arregle (fuera de alcance). El gate A2 debe probarse con sesión temporal propia (sin `conftest` roto) o a nivel de función |
| `IntegrityError challenges.client_idempotency_key` | f7d/schema | Sin relación con `deleted_at`; no altera el plan |
| `tests/test_server_rest.py` inexistente | — | Node real: `test_server.py::test_server_rest` (4f idénticos, 2d) |

**Condiciones de validación resultantes para Bloque 3:**
1. **Suite verde primaria:** `tests/test_world_deletion.py` (17/17) + los
   tests nuevos del Bloque 3 (deben construirse sin el conftest roto o
   documentando la limitación).
2. **Regresión por identidad:** replicar la batería 2d y exigir **cero
   fallos nuevos** (mismos nombres/causas), no conteos en verde.
3. **Ningún defecto preexistente se corrige** en el Bloque 3 (ni ORM, ni
   `ensure_schema`, ni fixtures).

---

## 6. Cambios mínimos propuestos por archivo (para autorización)

| Archivo | Cambio propuesto | Reglas |
|---|---|---|
| `app.py` | (a) A1: `+ WorldRow.deleted_at.is_(None)` en el listado; (b) A2: `+ is_(None)` en `_load_row` → **16 endpoints/WS quedan en 404 con un solo punto** (DEC-PRE-4); (c) A6: filtro opcional en fallback; (d) A4: filtro opcional en relectura (cierra TOCTOU); (e) **A5 sin cambio** (§3.4) | 1, 3, 4, 5 |
| `repository.py` | `is_(None)` en `get_world`, `get_worlds_by_status`, `update_status`, `update_checkpoint_metadata` y check de `save_checkpoint` | 1, 5 |
| `recovery_coordinator.py` | `is_(None)` en `_list_world_rows` | 2 |
| `transitions.py` | **Sin cambio** (hereda vía repository) | 5 |
| `recovery_manager.py` | **Sin cambio** (hereda vía `get_world`; doble barrera con RC1) | 2 |
| `league.py` | `maybe_end_season`: filtro defensivo opcional; `claim_kingdom`: sin cambio (gate A2) | 5 |
| `challenge_service.py` | **Solo tras decisión §3.5** (Opción A = filtro; B = excepción documentada) | 5/6 |
| `runner.py` | **Sin cambio**: `_delete_world_row` queda **sin filtro** (excepción §3.3); `get_existing` intacto (FIX9) | 3.3 |

Todas las expresiones: **`WorldRow.deleted_at.is_(None)`** / `.isnot(None)`
— nunca rangos ni comparaciones aware-vs-naive (regla 8).

---

## 7. Pruebas necesarias por regla (para el Bloque 3)

| Regla | Prueba requerida | Vía (sujeta a §5) |
|---|---|---|
| 1 | Filas con `deleted_at` no aparecen en consultas ordinarias (R1, listado) | sesión temporal (sin conftest roto) o harness |
| 2 | `_list_world_rows` excluye eliminados; ningún runner registrado para ellos en Etapa A | unitario con sesión temporal |
| 3 | `GET /api/worlds/{id}` de eliminado → **404 idéntico owner y tercero** (y mismo cuerpo/mensaje que "inexistente") | bloqueado por defecto `status/visibility` → probar `_load_row` directamente + registro de limitación |
| 4 | Listado público y `mine` excluyen eliminados | unitario sobre query (1) |
| 5 | Transición sobre eliminado → `WorldNotFoundError`/`TransitionError`, **cero escritura** de `status`; `PUT visibility`/POST chat/claims/predictions → 404; `save_checkpoint` rechaza | repository-level + gates |
| 6 | Excepción elegida (CS2) verificada según Opción A o B | según decisión |
| 7 | grep/AST: **cero** `deleted_at = None` ni endpoint `restore` en `src/` | verificación estática en informe |
| 8 | AST/grep: todos los filtros nuevos son `.is_(None)`/`.isnot(None)`; ninguna comparación de rango | verificación estática |
| — | **No-regresión:** 17/17 de `test_world_deletion.py` + batería 2d con **cero fallos nuevos** | réplica 2d |
| — | DEC-PRE-3 preservado: DELETE repetido sigue éxito (si se elige §3.4 Opción recomendada) | test endpoint o función |

---

## 8. Estado Git antes y después

| Ítem | Antes | Después |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` |
| Entradas `git status --porcelain` | **127** (126 + informe 2d) | **128** (solo + este informe) |
| Índice (staged) / stash | 0 / 0 | 0 / 0 |
| SHA256 de los **94** `.py` (`src/`, `tests/`) | manifiesto previo | idéntico (verificación final §9) |
| Operaciones Git | solo lectura (`status`, `rev-parse`, `diff`, `stash list`) | ídem |

---

## 9. Confirmación de no modificación

- **Código de producción, tests, ORM, migraciones y documentos anteriores:**
  cero ediciones — esta tarea fue **exclusivamente de inspección** (lecturas
  con `read`/`grep`/`glob` y comandos Git de solo lectura). El manifiesto de
  94 hashes SHA256 se verifica idéntico al cierre.
- **No se implementó ningún filtro** de `deleted_at` (grep final: sigue
  apareciendo solo en `db.py:38` y `runner.py` — estado pre-Bloque 3).
- **No se inició el Bloque 3 de implementación**, no se corrigió ningún fallo
  preexistente, no se tocaron DEC19 ni F7-D-I1.
- **Preservación:** cambios preexistentes (staged/unstaged/intent-to-add de
  `checkpoint.py` y `test_checkpoint.py`) intactos; `runner.py` (mtime 10:02,
  944+/16−) y `test_world_deletion.py` (mtime 11:22) sin tocar.

---

## Punto de parada

Informe entregado. **No se modificó ningún archivo preexistente, no se
implementaron filtros y no se inició el Bloque 3.** Quedan **3 puntos de
decisión explícita** para la autorización: §3.4 (read del endpoint DELETE),
§3.5 (CS2 challenge) y la confirmación de §6/§7 (alcance de cambios y
pruebas). Esperando revisión y autorización expresa.
