# F7-E.2-I-BLOCK2-2D-REPORT — Conciliación de regresión contra línea base

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK2-2D — verificación exclusiva.
**Archivos modificados durante esta tarea:** **ninguno** (único archivo nuevo: este informe).
**Resultado global: NO hay fallos nuevos atribuibles a 2a–2c.**

---

## 1. Comandos exactos ejecutados

Todos con `python -m pytest …` desde la raíz del proyecto, replicando los
comandos de la línea base cuando están documentados:

| # | Comando |
|---|---|
| 1 | `python -m pytest tests/test_world_deletion.py -q --tb=no` |
| 2 | `python -m pytest tests/test_world_deletion.py tests/test_runner_close.py tests/test_shutdown.py tests/test_runner_foundation.py tests/test_pause_transitions.py -q --tb=no` |
| 3 | `python -m pytest tests/test_repository.py -q --tb=no` |
| 4 | `python -m pytest tests/test_world_deletion.py tests/test_runner_close.py tests/test_shutdown.py tests/test_runner_foundation.py tests/test_repository.py -q --tb=no` *(combo exacta que originó la cifra documentada "34f/60p")* |
| 5 | `python -m pytest tests/test_recovery_coordinator.py -q --tb=line` |
| 6 | `python -m pytest tests/test_f7d_state_machine.py tests/test_schema_migrations.py -q --tb=no` (y `--tb=line` para causas; y por archivo para nombres) |
| 7 | `python -m pytest "tests/test_server.py::test_server_rest" -x -q` *(node documentado de la línea base)* |
| 8 | `python -m pytest tests/test_server.py -q --tb=no` |

Comparaciones de nombres de fallos se hicieron extrayendo el identificador de
nodo de cada línea `FAILED`/`ERROR` y diff-ing contra los históricos
`state_machine_out.txt`, `schema_mig_out.txt`, `server_out.txt` y contra los
nombres de la línea base registrados en el PRE §7.2/§7.2 (corridas previas).

---

## 2. Resultados completos por suite

| # | Suite | passed | failed | errors | notas |
|---|---|---|---|---|---|
| 1 | `test_world_deletion.py` | **17** | 0 | 0 | sin skipped |
| 2 | Combinada (eliminación + close + shutdown + foundation + pause) | **60** | **1** | **20** | 1 warning preexistente (Starlette deprecation) |
| 3 | `test_repository.py` (solo) | **7** | **33** | 0 | 40 tests recolectados |
| 4 | Combo exacta de línea base (eliminación + close + shutdown + foundation + repository) | **67** | **34** | 0 | |
| 5 | `test_recovery_coordinator.py` | **2** | **35** | 0 | |
| 6 | `test_f7d_state_machine.py` + `test_schema_migrations.py` | **0** | **46** (26 + 20) | 0 | |
| 7a | `tests/test_server_rest.py` | — | — | — | **archivo no existe** (§6) |
| 7b | `tests/test_server.py::test_server_rest` (`-x -q`) | 0 | **1** | 0 | se detiene tras el primer fallo, como la línea base |
| 8 | `test_server.py` (completo) | **1** | **4** | 0 | |

---

## 3. Comparación explícita contra la línea base

| Suite | Línea base documentada | Actual | Delta | Veredicto |
|---|---|---|---|---|
| `test_world_deletion.py` | 10 passed | 17 passed | **+7 passed** | Crecimiento **planificado** (los 7 tests de 2c); cero fallos |
| Combinada del+close+shutdown+foundation+pause | 1 failed, 53 passed, 20 errors | 1 failed, 60 passed, 20 errors | **+7 passed**; failed/errors **iguales en cantidad e identidad** | El fallo es el mismo `test_shutdown.py::test_lifespan_startup_then_shutdown_closed`; los 20 errors son los mismos 20 de `test_pause_transitions.py` |
| Cifra documentada "repository: 34 failed, 60 passed" | 34f/60p (procedente de la corrida combinada — ver nota) | combo idéntica: **34f/67p** | failed **+0**, passed **+7** | Mismo número y conjunto de fallos; +7 = tests 2c |
| `test_repository.py` (solo) | derivada: **33f/7p** (aritmética exacta, §3.1) | **33f/7p** | **0** | Idéntico |
| `test_recovery_coordinator.py` | 2 passed, 35 failed | **2p/35f** | **0** | Idéntico en cantidad y causas |
| `test_f7d_state_machine.py` + `test_schema_migrations.py` | 46 failed | **46 failed** (26 + 20) | **0** | Idéntico |
| `test_server.py::test_server_rest` | fallos por `WorldRow`/`visibility` | 1 failed `TypeError: 'visibility'…` | **0** | Idéntico |
| `test_server.py` (histórico `server_out.txt`: 4 failed) | 4 failed | **4 failed, 1 passed** | **0** | Los **4 nombres idénticos** |

### 3.1 Nota de atribución (corrección transparente, sin ocultar)

El PRE §7.2 registró *"test_repository.py: 34 failed / 60 passed"*. Esa cifra
procedía de la corrida **combinada de 5 archivos** (comando #4 de este
informe), no de la suite aislada. Reconciliación aritmética exacta:

- failed: `34 = 1 (test_shutdown) + 33 (repository)` ✓
- passed: `60 = 10 (test_world_deletion) + 43 (close/shutdown/foundation) + 7 (repository)` ✓

Re-ejecutando el **mismo comando combinado** hoy: **34 failed** (idénticos) y
**67 passed** (`60 + 7` tests nuevos de 2c) ✓. La suite aislada hoy da
**33f/7p**, consistente con la derivación. No se atribuye causalidad más allá
de lo medido: la línea base aislada de repository no se capturó como tal, por
lo que la comparación aislada es de **conteo derivado +11 nombres concretos +
causas** (no de conjunto completo de nombres).

### 3.2 Comparación de causas y nombres concretos (no solo similitud de totales)

| Suite | Nivel de comparación | Resultado |
|---|---|---|
| `test_f7d_state_machine.py` | **nombres completos** vs `state_machine_out.txt` | 26 vs 26 → **0 solo-históricos, 0 solo-actuales** |
| `test_schema_migrations.py` | **nombres completos** vs `schema_mig_out.txt` | 20 vs 20 → **0 diferencias** |
| `test_server.py` | **nombres completos** vs `server_out.txt` | 4 vs 4 → **0 diferencias**; causa idéntica `TypeError: 'visibility'` |
| `test_pause_transitions.py` | errors 20 vs 20 + 7 nombres de la línea base | **0 ausentes**; causa idéntica `TypeError: 'status' is an invalid keyword argument for WorldRow` (en el fixture, durante setup) |
| `test_repository.py` | 11 nombres de la línea base vs 33 actuales | **0 ausentes**; causas actuales: `TypeError: 'status'/'visibility'` y `TypeError: ensure_schema() takes 0 positional arguments…` — las mismas raíces documentadas |
| `test_recovery_coordinator.py` | conteo 35f/2p + nombres muestreados del tail (`test_i_03…`, `test_i_05…`) + causas | coincidentes; causa dominante actual `TypeError: 'status' … WorldRow` (misma familia documentada) |
| Causas de la combinada | identidad del único fallo | `test_lifespan_startup_then_shutdown_closed` (el mismo de la línea base) |

---

## 4. Fallos nuevos introducidos después de 2a–2c

**NINGUNO.** Evidencia:

- Ninguna suite tiene más `failed` o `errors` que su línea base.
- Ningún test pasó de *passed* a *failed/error* (las identidades de fallo
  coinciden con la línea base/históricos en los niveles de nombre comparables).
- Los únicos deltas son **+7 passed** en tres corridas, todos atribuibles a
  `test_del_11`…`test_del_17` (2c): `10→17`, `53→60`, `60→67`.
- `test_world_deletion.py` (la única suite que ejercita directamente el código
  modificado): **17/17**.

---

## 5. Fallos que permanecen iguales a la línea base

- `test_shutdown.py::test_lifespan_startup_then_shutdown_closed` (1 failed de
  la combinada) — idéntico.
- Los **20 errors** de `test_pause_transitions.py` — misma cantidad, mismos
  nombres, misma causa `TypeError: 'status' … WorldRow`.
- `test_repository.py` — 33 fallos, mismos 11 nombres muestreados, mismas
  causas (`status`/`visibility`/`ensure_schema`).
- `test_recovery_coordinator.py` — 35f/2p, mismas causas.
- `test_f7d_state_machine.py` (26) + `test_schema_migrations.py` (20) —
  conjuntos de nombres **idénticos** a los históricos y mismas causas
  (`ensure_schema()` arity + `IntegrityError NOT NULL challenges…`).
- `test_server.py` — los mismos 4 `TypeError: 'visibility'`.

Todas estas causas son las raíces preexistentes documentadas en el PRE §7.1
(columnas `status`/`visibility` ausentes del ORM; `ensure_schema()` sin
parámetros invocada con engine). **Ninguna causa cambió de tipo.**

---

## 6. Suites que no pudieron ejecutarse (o no como se nombraron)

- **`tests/test_server_rest.py`: el archivo NO existe** en el repositorio
  (verificado por glob). **No se contabiliza como pasada.** En su lugar se
  ejecutó el node documentado de la línea base
  (`tests/test_server.py::test_server_rest -x -q` → 1 failed idéntico) y el
  archivo completo `tests/test_server.py` (4 failed, idénticos a
  `server_out.txt`). Ninguna otra suite falló por colección/importación/
  infraestructura: todas recolectaron y corrieron completas.

---

## 7. Estado Git antes y después

| Ítem | Antes | Después |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` |
| Entradas `git status --porcelain` | 126 | 126 |
| Snapshot completo de status | `2d_status_before.txt` | **idéntico** (Compare-Object: 0 diferencias) |
| Índice (staged) | 0 | **0** |
| Stash | 0 | **0** |
| SHA256 de los **94** `.py` de `src/` y `tests/` | manifiesto previo | **idéntico** (0 diferencias) |
| `src/worldsim/server/worlds.db` | `5D0B27BC…`, len 118784, mtime 18/11 23:45 | **hash idéntico** (las corridas no lo alteraron) |

Confines previos verificados tras la tarea:
- **2a** → `runner.py` (mtime 10:02, diffstat 944+/16−) — **intacto durante 2d**.
- **2b–2c** → `tests/test_world_deletion.py` (mtime 11:22) — **intacto durante 2d**.
- Archivos preexistentes modificados y untracked (incluidas las entradas
  intent-to-add `A` de checkpoint): **preservados**; el snapshot de status no
  varió ni en una línea, de modo que tampoco se creó, eliminó o pasó a tracked
  ningún archivo por la ejecución de la batería (solo artefactos
  `.pytest_cache`/`__pycache__`, ajenos al conteo).

---

## 8. Confirmación de no modificación

- **Código de producción:** cero ediciones (hashes SHA256 idénticos).
- **Tests:** cero ediciones (hashes idénticos; los de alcance 2a/2b-2c siguen
  confinados a sus archivos).
- **ORM / migraciones:** intactos (`db.py` con hash idéntico).
- **Documentos preexistentes:** intactos; solo se creó este informe.
- **Configuraciones:** ninguna alterada (comandos idénticos a la línea base
  cuando documentados; sin flags que oculten fallos — `--tb=no`/`--tb=line`
  solo afectan el formato del reporte, no el conteo).
- **Git:** cero operaciones destructivas (solo `status`, `diff`, `rev-parse`,
  `stash list`, `ls-files`, `cat-file` de solo lectura).
- **DEC19 / F7-D-I1:** no tocados. **Bloque 3:** no iniciado. Correcciones de
  fallos preexistentes: ninguna ejecutada.

---

## 9. Dictamen: ¿introdujeron 2a–2c regresiones demostrables?

**NO.** Con la evidencia anterior:

1. Ningún fallo nuevo en ninguna de las suites ejecutables; identidades de
   fallo y causas **idénticas** a la línea base en todos los niveles de
   comparación disponibles (nombres completos en state_machine, schema y
   server; nombres muestreados en repository y pause; conteo+causa+tail en
   recovery).
2. Los deltas de `passed` (+7) se explican **completa y exactamente** por los
   7 tests añadidos en 2c, sin desplazar ningún resultado previo.
3. La única suite funcional del cambio (`test_world_deletion.py`) pasa
   **17/17**.
4. Los fallos restantes son los **mismos preexistentes** del defecto §7.1 del
   PRE (columnas `status`/`visibility` y arity de `ensure_schema`), con causas
   sin cambio de tipo — atribuidos a esa causa **por traza concreta**, no por
   parecido.

**Salvedades de alcance de este dictamen:**
- La línea base aislada de `test_repository.py` no fue capturada como tal
  (§3.1): su comparación es porcombo exacto + conteo derivado +11 nombres +
  causas.
- Las suites con fallos preexistentes **no arrancan la aplicación** (mismo
  estado que en la línea base), de modo que la regresión aquí medida es a
  nivel de suites, no de sistema end-to-end encendido — condición
  preexistente, no atribuible a 2a–2c.

---

## 10. Reserva explícita sobre la carrera de dos workers (2c.7)

La **carrera de dos workers con lecturas previas al commit ajeno**, documentada
como diferencia fake/real en `F7-E.2-I-BLOCK2-2C-REPORT.md` §5 (ítem 1: dos
`_delete_world_row` concurrentes sobre una misma fila en una base real, donde
el segundo leería `deleted_at = NULL` antes del commit del primero), **NO fue
probada ni verificada en esta tarea ni en ninguna anterior**:

- No existe prueba que ejecute esa condición contra SQLAlchemy/SQLite real.
- El harness de 2c.7 verifica la coordinación (guarda, refcount, shield,
  no-completación, idempotencia secuencial), **no** la escritura doble
  concurrente en base real.
- **No se declara verificada, ni segura, ni inexistente dicha carrera.**
  Queda como condición abierta cuya eliminación exigería un `UPDATE`
  condicional (`… WHERE deleted_at IS NULL`) u otro mecanismo atómico en
  producción — prohibido en los bloques 2a–2d y reservado a una decisión
  futura (posible material de revisión posterior, junto al Bloque 3).

---

## Punto de parada

Informe entregado. **No se inició el Bloque 3 ni se corrigió ningún fallo.**
Esperando revisión y autorización expresa.
