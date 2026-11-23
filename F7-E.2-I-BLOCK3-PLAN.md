# F7-E.2-I-BLOCK3-PLAN — Plan de implementación de exclusión de mundos eliminados

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK3-PLAN — **planificación exclusiva**.
**Archivos modificados durante esta tarea:** **ninguno** (único archivo nuevo: este plan).
**Ningún subbloque implementado. Nada de este plan se ejecuta sin revisión y autorización expresa.**

---

## 0. Alcance y principios del plan

- **Base factual:** `F7-E.2-I-BLOCK3-PRE-REPORT.md` (inventario de 23 puntos de
  acceso, 8 callsites de `get_existing`, ventanas W1–W4).
- **Nunca se toca:** `runner.py` (incluidos `safe_delete_world`,
  `_delete_world_row`, `RunnerRegistry`, `get_existing`), ORM/`db.py`
  (la columna `deleted_at` ya existe desde Bloque 1 — **cero migraciones
  nuevas**), migraciones, DEC19, F7-D-I1, tests preexistentes, documentos
  anteriores.
- **Regla 8 en todas partes:** toda exclusión es
  `WorldRow.deleted_at.is_(None)` (→ `deleted_at IS NULL` en SQL). Prohibido
  comparar timestamps (SQLite devuelve `deleted_at` naive al releer).
- **Separación obligatoria (§2 del encargo):** los cambios de **lectura
  ordinaria** (B3-1…B3-5) y la **excepción histórica CS2** (B3-6) nunca van
  en la misma modificación.
- **Excepciones permitidas, taxadas (allowlist final, §5.4):** solo tres
  cargas de `WorldRow` sin filtro en todo `src/`:
  1. `runner._delete_world_row` (RN1 — es quien marca; DEC-PRE-3);
  2. `app.delete_world` read (A5 — decisión 1, idempotencia DEC-PRE-3);
  3. `challenge_service._load_world_for_resolution` (CS2 — decisión 11).

### 0.1 Mapeo decisiones 1–13 → subbloque

| Decisión | Aplicación | Subbloque |
|---|---|---|
| 1 (ordinarias excluyen) | R1–R5, A1, A2, A4, A6, L1–L3 | B3-1, B3-3, B3-4 |
| 2 (startup excluye) | RC1 | B3-2 |
| 3 (GET → 404) | A2 (choke point de 16 usos) | B3-3 |
| 4 (listados excluyen) | A1 (+ destacados: no existen hoy) | B3-3 |
| 5 (transiciones no modifican) | R3/R4/R5 (T1/T2 heredan), A4, L3 | B3-1, B3-3, B3-4 |
| 6 (histórico explícito restringido) | CS2 vía helper privado único | B3-6 |
| 7 (sin restauración) | verificación estática (grep cero `deleted_at = None`) | B3-7 |
| 8 (`.is_(None)`) | todas las expresiones + auditoría AST | B3-1…B3-6, B3-7 |
| 9 (DELETE repetido sin filtro) | **A5 SIN CAMBIO** — verificado en B3-3 y B3-7 | (ausencia) |
| 10 (CS1 excluye al crear) | guardia ordinaria en `prepare_worlds` | B3-5 |
| 11 (CS2 histórico restringido, sin reactivar ni modificar estado ordinario) | helper privado + guardia de escritura | B3-6 |
| 12 (`get_existing` intacto) | cero cambios; verificación estática de los 8 callsites | B3-7 |
| 13 (reserva 2c.7 explícita) | §4 de este plan + informe final | B3-7 |

---

## 1. Secuencia de subbloques recomendada

Orden: **ordinarias primero (B3-1…B3-5) → excepción histórica (B3-6) →
verificación global (B3-7)**. Cada subbloque autoriza exactamente **un archivo
de producción + un archivo de test nuevo** (los tests jamás se añaden a archivos
de test preexistentes).

---

### B3-1 — Filtros ordinarios en la capa de datos (`repository.py`)

- **Archivos autorizados:** `src/worldsim/server/repository.py`,
  `tests/test_exclusion_repository.py` (nuevo).
- **Funciones y cambio exacto:**
  - **R1 `get_world` (119–124):** `select(WorldRow).where(WorldRow.id == world_id, WorldRow.deleted_at.is_(None))`.
  - **R2 `get_worlds_by_status` (126–131):** añadir `WorldRow.deleted_at.is_(None)`.
  - **R3 `update_status` (143):** sustituir `s.get(WorldRow, world_id)` por la
    misma carga filtrada; `row is None` → `return False` (ya no distingue
    "ausente" de "eliminado": para lo ordinario son lo mismo). **No tocar**
    `row.status = status` (defecto preexistente fuera de alcance).
  - **R4 `update_checkpoint_metadata` (171):** mismo patrón que R3.
  - **R5 `save_checkpoint` (275):** check de existencia con la misma carga
    filtrada; mensaje de `ValueError` **idéntico** al actual
    (`f"World {world_id} does not exist"` — indistinguible).
- **Efecto en callers (sin cambio en ellos):** `_write_status` sobre mundo
  eliminado → `row is None` → `WorldNotFoundError` (regla 5, semántica ya
  documentada en su docstring); `_revert_status` → `False` → rama "row
  vanished" ya existente (transitions.py sin tocar); `recovery_manager`
  hereda (doble barrera con B3-2); checkpoints nuevos rechazados en mundo
  eliminado.
- **Riesgos:**
  - R2 es **inyexecutable en verde hoy** (`WorldRow.status` no existe como
    columna → `OperationalError` preexistente) → test estático (AST) + nota.
  - R3 sobre mundo vivo: `row.status = status` sigue sin persistir (defecto
    preexistente) → los tests **no** asertan el valor de `status`, solo
    retorno y que la fila `deleted_at` no cambia.
  - Ventana W2 (marca puesta, commit pendiente): el filtro **no** afecta
    (la fila sigue `NULL` hasta el commit) — sin cambio de garantías (§4).
- **Pruebas nuevas** (`tests/test_exclusion_repository.py`, motor propio
  `Base.metadata.create_all` en SQLite temporal, **sin** `ensure_schema`):
  ≥7 — `get_world` vivo/devuelto, eliminado/`None`, ausente/`None`;
  `update_status` eliminado→`False` y fila intacta, vivo→`True`;
  `update_checkpoint_metadata` eliminado→`False`; `save_checkpoint`
  eliminado→`ValueError` con mensaje exacto, vivo→crea checkpoint.
  R2: test AST (ambos predicados presentes) documentando el defecto.
- **Pruebas existentes afectadas:** `test_repository.py` (33 fallos
  preexistentes — comparar **identidad**, sin fallos nuevos); combos de 2d.
- **Criterios de aceptación:** ≥8 tests nuevos verdes; 17/17 de
  `test_world_deletion.py`; identidad de fallos de repository vs línea base;
  grep de diff confinado a los 2 archivos autorizados.
- **Dependencias:** ninguna (puede ser el primero).

---

### B3-2 — Exclusión en startup (`recovery_coordinator.py`)

- **Archivos autorizados:** `src/worldsim/server/recovery_coordinator.py`,
  `tests/test_exclusion_startup.py` (nuevo).
- **Cambio exacto:** `_list_world_rows` (463):
  `select(WorldRow).where(WorldRow.deleted_at.is_(None)).order_by(WorldRow.id)`.
  Nada más (RC2/`get_existing` intacto; `_stage_a`/`_stage_b` intactos).
- **Riesgos:** un mundo eliminado deja de aparecer en `recovery_results` /
  `not_recoverable` (correcto: no se recupera ni registra — regla 2 +
  DEC-PRE-2). Con los datos actuales (`worlds.db` sin eliminados) **cero
  cambio observable**. Si `_list_world_rows` se llamara en caliente, sería
  defensa adicional — hoy solo corre en startup.
- **Pruebas nuevas:** ≥3, llamando al método **sin instancia**
  (`RecoveryCoordinator._list_world_rows(stub)` con `stub.session_factory`
  → SQLite temporal): vivo+eliminado → solo vivo; solo eliminados → `[]`;
  sin filtro el test fallaría (control negativo documentado).
- **Pruebas existentes afectadas:** `test_recovery_coordinator.py` (35f/2p
  preexistentes — identidad; sus fixtures construyen `WorldRow(status=…)` y
  **no** se tocan).
- **Criterios de aceptación:** tests nuevos verdes; identidad 35f/2p;
  diff confinado.
- **Dependencias:** ninguna.

---

### B3-3 — Lecturas ordinarias HTTP (`app.py`) — **A5 sin cambio**

- **Archivos autorizados:** `src/worldsim/server/app.py`,
  `tests/test_exclusion_http.py` (nuevo).
- **Funciones y cambio exacto:**
  - **A2 `_load_row` (553–558):** carga filtrada
    (`select(WorldRow).where(WorldRow.id == world_id, WorldRow.deleted_at.is_(None))`
    → `scalar_one_or_none()`); **mensaje 404 byte-idéntico**
    (`f"world {world_id} not found"`). Un solo punto → 16 usos en 404
    (DEC-PRE-4, reglas 1/3). Orden de comprobación intacto (404 antes que
    auth/visibility ⇒ idéntico para owner y tercero).
  - **A1 `list_worlds` (≈445):** `q = q.where(WorldRow.deleted_at.is_(None))`
    tras montar la proyección, antes de `limit` (regla 4; público y `mine`).
  - **A4 `put_world_visibility` (710):** relectura `live` con la misma carga
    filtrada (cierra el TOCTOU 698→710); mensaje 404 idéntico.
  - **A6 `_canonical_month` (1294):** fallback con carga filtrada
    (`row.months_simulated if row else 0` ya tolera `None`).
  - **A5 `delete_world` (729): SIN CAMBIO — decisión 9** (DEC-PRE-3 paso 8:
    repetir DELETE → éxito HTTP). Se deja constancia en comentario de prueba,
    no en producción (o si se comenta, una línea aclaratoria sin lógica).
- **Riesgos:**
  - `_load_row` es parcheado por tests de infra ("infra probe tests patch
    this"): firma y contrato (row | HTTPException 404) **inalterados**.
  - W2 (commit pendiente): GET seguirá 200 — **correcto y exigido por §4**
    (la eliminación aún no es durable); no se afirma lo contrario.
  - Los tests de endpoint vivo chocan con el defecto preexistente
    (`_require_visible` → `row.visibility` inexistente) → los controles
    vivos se hacen a nivel `_load_row`/`list_worlds` (§5).
- **Pruebas nuevas** (`tests/test_exclusion_http.py`, patrón `_fresh_app`
  copiado de `test_server.py` — ver §5.2): ≥6 —
  1. GET eliminado → 404 **con detalle idéntico** al mundo inexistente;
  2. GET eliminado **con token de owner real** → mismo 404 (DEC-PRE-4);
  3. GET eliminado con y sin cabecera → mismos tres resultados idénticos;
  4. `_load_row` vivo → devuelve la fila (control vivo, sin `visibility`);
  5. `list_worlds(mine=1)` con vivo+eliminado → excluye el eliminado
     (`_public_ficha` parcheado **solo** para sortear el defecto
     preexistente de `WorldRow.visibility`, documentado en el test);
  6. `put_world_visibility` sobre eliminado → 404; `_canonical_month` de
     eliminado sin runner → 0.
- **Pruebas existentes afectadas:** `test_server.py` (4f — identidad),
  `test_visibility.py` (**52f/4p — línea base capturada en este plan**),
  `test_etapa5_http.py` (**12f/1p — línea base capturada**), suites de 2d.
- **Criterios de aceptación:** ≥6 tests nuevos verdes; 17/17; identidad de
  fallos en server/visibility/etapa5; diff confinado.
- **Dependencias:** ninguna (recomendado después de B3-1 para revisión
  incremental).

---

### B3-4 — Filtros defensivos en liga (`league.py`)

- **Archivos autorizados:** `src/worldsim/server/league.py`,
  `tests/test_exclusion_league.py` (nuevo).
- **Cambio exacto:**
  - **L1 `claim_kingdom` (80):** `select(WorldRow).where(WorldRow.id == world_id, WorldRow.deleted_at.is_(None))`.
  - **L2 (125):** mismo filtro en la relectura de `state_json`.
  - **L3 `maybe_end_season` (274):** mismo filtro (`row is None → None`
    ya manejado; mundo eliminado → sin escritura de `season_over`, regla 5).
- **Riesgos:** **cero observable** en HTTP (A2 ya 404a antes; L1 inalcanzable
  para eliminados) — es defensa en profundidad de la capa de datos.
  `maybe_end_season` es inalcanzable para eliminados (runner cerrado antes
  del commit); el filtro es barrera adicional.
- **Pruebas nuevas:** ≥3 — claim sobre eliminado → `"world not found"`
  (mensaje existente) y **cero** `ClaimRow` insertado; claim sobre liga viva
  → funciona (control); `maybe_end_season` sobre eliminado → `None` y fila
  intacta (stub `eng.month` con `SimpleNamespace`).
- **Pruebas existentes afectadas:** ninguna suite de liga propia; las rutas
  de claims viven en visibility/etapa5 (identidad, §B3-3).
- **Criterios de aceptación:** tests nuevos verdes; diff confinado.
- **Dependencias:** ninguna.

---

### B3-5 — CS1: exclusión en el flujo de creación de desafíos (`challenge_service.py`)

- **Archivos autorizados:** `src/worldsim/server/challenge_service.py`,
  `tests/test_exclusion_challenge_create.py` (nuevo).
- **Contexto verificado (decisión 10):** `create_challenge` (245),
  `enroll_participant` (521) y `prepare_worlds` (680) **no leen mundos
  existentes hoy** (grep: único toque = construcción de fila nueva en 752,
  que nace `deleted_at NULL`). El único punto por donde un mundo existente
  puede entrar a la creación es el skip de idempotencia de
  `prepare_worlds` (749): `if participant.world_id is not None: continue`
  — **sin verificar que ese mundo siga activo**.
- **Cambio exacto (opción recomendada — requiere aprobación explícita de la
  opción en la revisión):**
  1. Nuevo predicado privado `_assigned_world_active(session, world_id) -> bool`
     con carga **ordinaria filtrada** (`.is_(None)`).
  2. En el punto 749: si `world_id` está asignado **y** el mundo está
     activo → `continue` (como hoy). Si está asignado y **está eliminado o
     ausente** → **no cuenta como preparado**: registrar
     `logger.warning` explícito (regla 8) y proseder a crear la fila nueva
     para el participante (la exclusión ocurre *dentro* de la creación).
  - **Opción alternativa (más conservadora):** lanzar `ChallengeError`
    explícito en lugar de recrear. **Ambas opciones se someten a
    autorización; no se implementa ninguna sin elegir.**
- **Riesgos:** la opción recomendada **cambia comportamiento** (un
  participante con mundo derivado eliminado recibe uno nuevo en vez de
  saltarse); es el único subbloque con elección de comportamiento funcional.
  La opción alternativa falla el `prepare` completo (más ruidoso).
  Ninguna toca la fila eliminada (modifica solo `ChallengeParticipantRow` —
  registro histórico del desafío, regla 6).
- **Pruebas nuevas:** ≥3 — predicado con vivo/eliminado/ausente; guardia en
  el skip (mundo eliminado **no** se considera preparado — test sobre el
  predicado + aserción AST de que el skip lo usa); fila del mundo eliminado
  **byte-idéntica** tras el flujo. Integración completa de `prepare_worlds`
  solo si es viable sin fixtures rotos; si no, se documenta la limitación
  (§5.3) sin debilitar la afirmación del predicado.
- **Pruebas existentes afectadas:** suites de challenges
  (`test_f7d_state_machine` 26f y schema 20f preexistentes — identidad).
- **Criterios de aceptación:** tests nuevos verdes; identidad de suites de
  challenges; **opción elegida registrada en el informe**; diff confinado.
- **Dependencias:** ninguna (pero debe ir **antes** que B3-6 para no mezclar
  lo ordinario con la excepción histórica en el mismo archivo).

---

### B3-6 — CS2: excepción histórica restringida de resolución (`challenge_service.py`)

- **Archivos autorizados:** `src/worldsim/server/challenge_service.py`,
  `tests/test_exclusion_challenge_resolution.py` (nuevo).
- **Diseño de la excepción (contención de propagación — §3):**
  1. **`repository.get_world()` NO se toca aquí** (sigue 100% ordinario) y
     **no se añade ningún parámetro `include_deleted`** en ningún repositorio
     (nada que mal-usar).
  2. Nueva función **privada, de un solo callsite**, en el propio módulo:
     `_load_world_for_resolution(session, world_id)` → `session.get(WorldRow, world_id)`
     **sin filtro, deliberado**, con docstring que marca la excepción
     (decisión 11: solo para resolver un desafío YA existente; prohibido
     reactrunner, crear runners o usarla en cualquier otro flujo). Sustituye
     a `session.get(WorldRow, participant.world_id)` en **1039**.
  3. Nueva función privada `_store_resolution_progress(world_row, months, state_json) -> bool`
     que envuelve las asignaciones de **1055/1057**:
     `if world_row.deleted_at is not None: return False` (sin tocar la fila);
     si no, persiste y `return True`. Así la **fila eliminada queda
     byte-idéntica** tras la resolución (regla 5 + decisión 11: sin modificar
     estado ordinario), mientras el engine sigue corriendo **en memoria** para
     calcular las métricas y el resultado se persiste en las tablas del
     desafío (participant/challenge — histórico, regla 6).
  4. `WorldNotFoundForParticipant` solo se lanza ahora para filas
     **realmente ausentes** (la excepción lee sin filtrar).
- **Riesgos:** una resolución reintentada sobre mundo eliminado **recalcula**
  en memoria (determinista: mismo `state_json` → mismas métricas) en vez de
  reanudar desde `months_simulated` persistido — documentar. La única escritura
  que se salta es el progreso del mundo derivado; `participant.result_json` /
  `challenge.state` **siguen persistiendo** (completitud F7-D intacta).
- **Pruebas nuevas:** ≥4 — helper devuelve la fila eliminada; helper devuelve
  `None` si falta; `_store_resolution_progress` con eliminado → `False` +
  atributos intactos (objeto `SimpleNamespace`), con vivo → `True` +
  valores escritos; **aserción de que las llamadas al helper son exactamente
  las del flujo de resolución** (grep/AST).
- **Pruebas existentes afectadas:** suites de challenges (identidad).
- **Criterios de aceptación:** tests nuevos verdes; fila eliminada
  byte-idéntica demostrada; diff confinado; allowlist de §5.4 sigue exacta.
- **Dependencias:** **B3-1 y B3-3 primero** (para que la allowlist tenga
  sentido: el resto del código debe estar ya filtrado) y **B3-5 antes**
  (separación ordinario/excepción exigida por el encargo).

---

### B3-7 — Verificación final e informe (sin cambios de producción)

- **Archivos autorizados:** únicamente el informe nuevo
  `F7-E.2-I-BLOCK3-REPORT.md`.
- **Contenido:**
  1. **Batería completa** con identidad de fallos vs líneas base:
     `test_world_deletion.py` 17/17 · repository (33f/7p) · recovery (35f/2p)
     · f7d+schema (46f) · pause+combo (1f/60p/20e) · **server (4f/1p)** ·
     **visibility (52f/4p — baseline de este plan)** · **etapa5 (12f/1p —
     baseline de este plan)** · todos los tests nuevos de B3-1…B3-6 en verde.
     **Cero fallos nuevos** = aceptación; cualquier delta se investiga y
     reporta sin ocultar (regla 8).
  2. **Auditoría estática allowlist (§5.4):** AST/grep — cargas de `WorldRow`
     sin filtro **solo** en los 3 sitios taxados; todos los filtros nuevos son
     `.is_(None)`; cero `deleted_at = None` ni endpoints de restauración
     (reglas 7/8); A5 sin cambio (regla 9); los 8 callsites de `get_existing`
     idénticos (regla 12); `runner.py` hash idéntico al cierre de 2a.
  3. **Reserva 2c.7 reafirmada sin aliviar** (§4 abajo).
  4. Estado Git y confirmación de no modificación de preexistentes.
- **Dependencias:** todos los subbloques anteriores.

---

## 2. Matriz de consultas (23 filas; "22" = 23 − A3, que no admite filtro)

| Punto | Regla | Filtro / excepción | Ordinario o histórico | Contención de la excepción |
|---|---|---|---|---|
| A1 listado | 4 | `.is_(None)` | Ordinario | n/a |
| A2 `_load_row` (16 usos) | 1, 3 | `.is_(None)` + 404 idéntico | Ordinario | n/a |
| A3 `create_world` | — | ninguno (nace NULL) | n/a | n/a |
| A4 relectura visibility | 5 | `.is_(None)` | Ordinario | n/a |
| A5 read de `DELETE` | 9 | **SIN FILTRO (deliberado)** | Excepción DEC-PRE-3 | Taxado en allowlist §5.4; commentado en test |
| A6 `_canonical_month` | 1 | `.is_(None)` en fallback | Ordinario | n/a |
| R1 `get_world` | 1 | `.is_(None)` | Ordinario | — |
| R2 `get_worlds_by_status` | 1 | `.is_(None)` | Ordinario | — |
| R3 `update_status` | 5 | carga filtrada → `False` | Ordinario | — |
| R4 `update_checkpoint_metadata` | 5 | carga filtrada → `False` | Ordinario | — |
| R5 `save_checkpoint` | 1, 5 | check filtrado → `ValueError` (mensaje exacto) | Ordinario | — |
| T1 `_write_status` | 5 | vía R1/R3 — **sin cambio en transitions.py** | Ordinario | — |
| T2 `_revert_status` | 5 | vía R3 — rama existente | Ordinario | — |
| RC1 `_list_world_rows` | 2 | `.is_(None)` | Ordinario (startup) | — |
| RC2 `get_existing` (Etapa B) | 12 | ninguno | n/a | callsite intacto |
| RM1 `recover` | 2 | vía R1 + RC1 (doble barrera) | Ordinario | — |
| L1 `claim_kingdom` read | 1 | `.is_(None)` | Ordinario | — |
| L2 relectura claim | 1 | `.is_(None)` | Ordinario | — |
| L3 `maybe_end_season` | 5 | `.is_(None)` | Ordinario | — |
| CS1 `prepare_worlds` skip | 10 | predicado ordinario filtrado (opción aprobada) | Ordinario | — |
| CS2 resolución | 6, 11 | **excepción: helper privado sin filtro + guardia de escritura** | **Histórico restringido** | Helper privado `_`, un callsite, sin API de repositorio con `include_deleted`, allowlist final |
| RN1 `_delete_world_row` | 3.3 PRE | **SIN FILTRO (quien marca; DEC-PRE-3)** | Excepción taxada | Allowlist §5.4 |
| RN2 `get_existing` en `safe_delete_world` | 12 | ninguno | n/a | callsite intacto |

**Propagación de la excepción (mecanismos acumulativos):** (a) helper privado
de un solo callsite, nunca público; (b) repositorios sin kwarg
`include_deleted` (no existe vía general — `get_world` queda irremediablemente
ordinario); (c) allowlist estática final de exactamente 3 sitios; (d) docstrings
de excepción en los 3; (e) `get_existing` global intacto (regla 12).

---

## 3. Concurrencia — garantías preservadas (regla 13 + §4 del encargo)

| Garantía | Mecanismo | Estado tras el plan |
|---|---|---|
| Durante `_deleting`, las transiciones siguen protegidas | marca síncrona de `begin_delete` + chequeos atómicos (`transitions:360/468`, sin `await` entre chequeo y `get_existing`) | **Intacto — `runner.py` y `transitions.py` sin cambios** |
| Antes del commit, la lectura ordinaria **puede seguir viendo el mundo**; **no** es durable aún | filtros que leen la fila: en W2 `deleted_at` sigue `NULL` → A2/A1 la devuelven | **Correcto por diseño**: el plan **no** afirma eliminación durable hasta el commit; GET 200 en W2 es el comportamiento esperado |
| Después del commit, las consultas ordinarias lo excluyen | todos los filtros actúan en cuanto el commit cae (A2/A1/R1/RC1…) | Garantía nueva del Bloque 3 |
| Cancelación del caller no libera la guarda prematuramente con el worker activo | refcount transferido al callback (2c.7 verificado) | **Intacto — mecanismo no tocado** |
| Una segunda eliminación espera al worker anterior | lock por mundo + refcount (`1 → 2 → {}`) | **Intacto** |
| **Reserva 2c.7 (no aliviar)** | — | **PERMANECE NO DEMOSTRADA:** la carrera de dos sesiones con lecturas previas al commit ajeno **no está resuelta** por este plan; los filtros leen `NULL` antes del commit igual que antes. Serializar `safe_delete_world` **no** demuestra la ausencia de carreras de `_write_status`/`save_checkpoint`/`claim_kingdom` contra el commit. **No se afirma lo contrario en ningún test ni informe** |
| `get_existing` durante `_deleting` | contrato FIX9 (lecturas permitidas) | Los 8 callsites sin cambios (regla 12); los gates de transición siguen siendo la protección |
| Ventana `unregister → commit` | la **marca** sigue activa hasta que el work termina (`end_delete` en finally/callback) → transiciones rechazadas sin escribir | Intacto; el filtro añade la barrera post-commit para lo ordinario |

---

## 4. Validación independiente de los defectos preexistentes (regla §5)

### 4.1 Principios
- **Cero arreglos oportunistas:** no se tocan `WorldRow.status/visibility`,
  `ensure_schema()`, fixtures rotos ni los fallos de repository/recovery.
- **Cero dependencia de ellos:** todos los tests nuevos crean su esquema con
  `Base.metadata.create_all(motor_temporal)` — **sin** `ensure_schema`
  (evita el defecto de firma) y **sin** construir `WorldRow(status=…,
  visibility=…)` (evita el `TypeError`).
- **Punto de parcheo único ya probado:** `dbmod.SessionLocal =
  sessionmaker(bind=test_engine)` (patrón `_fresh_app` de `test_server.py`,
  que hoy logra 1 test verde) — lo replican los tests nuevos como helper
  propio (los archivos de test preexistentes **no** se modifican).

### 4.2 Tests HTTP sin lifespan
`TestClient` usado **sin** `with` no ejecuta startup → no corre recovery ni
`ensure_schema`. Los tests de B3-3 usan el patrón completo `_fresh_app`
(copia local) + inserción directa de `WorldRow` con columnas válidas (sin
`visibility`).

### 4.3 Controles vivos y limitaciones declaradas
- `_load_row` vivo → **sí** testeable (no toca `visibility`).
- GET/listado **con** fila viva → hoy crashea en `_require_visible`/
  `_public_ficha` (`row.visibility` inexistente, defecto preexistente) → los
  tests de B3-3 parchean `_public_ficha` **solo en el test** (documentado)
  o afirman solo el camino 404. **Limitación declarada en el informe, sin
  debilitar** la afirmación "eliminado → 404 idéntico a inexistente".
- R2 (`get_worlds_by_status`): test AST + limitación (columna `status`
  ausente impide cualquier ejecución en verde).
- Suites en rojo preexistente: la regresión se mide por **identidad de
  nombres/causas** contra las líneas base (2d + baselines de visibility y
  etapa5 capturados en este plan: 52f/4p y 12f/1p), nunca por "parecido".

### 4.4 Auditoría estática final (allowlist)
Test AST en B3-7: (1) cargas de `WorldRow` sin `.is_(None)` → exactamente
{`runner._delete_world_row`, `app.delete_world`, `challenge_service._load_world_for_resolution`}
+ el read de A5 (mismo sitio `app.delete_world`); (2) todo filtro nuevo usa
`.is_(None)`/`.isnot(None)`; (3) cero `deleted_at = None` en `src/`;
(4) los 8 callsites de `get_existing` sin diff; (5) `runner.py` hash idéntico.

---

## 5. Integridad del repositorio

| Ítem | Antes de planificar | Tras este plan |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` |
| `git status --porcelain` | 128 | **129** (solo + este plan) |
| Staged / stash | 0 / 0 | 0 / 0 |
| SHA256 de 94 `.py` (`src/`, `tests/`) | manifiesto previo | idéntico (verificación al cierre) |
| Operaciones Git | solo lectura | solo lectura |

**Confirmación:** este encargo no modificó código, tests, ORM, migraciones ni
documentos anteriores; no se ejecutó ningún subbloque; no se corrigió ningún
defecto preexistente.

---

## 6. Punto de parada

Plan entregado. **No se implementa ningún subbloque (B3-1…B3-7) sin revisión
y autorización expresa.** Puntos que requieren decisión junto con la
autorización: **opción de comportamiento de CS1** (B3-5: recrear vs fallar)
y confirmación del alcance/orden de los subbloques.
