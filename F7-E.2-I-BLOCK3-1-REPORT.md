# F7-E.2-I-BLOCK3-1-REPORT — Filtros de exclusión de mundos eliminados en `repository.py` (R1–R5)

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK3-1 — implementación de los filtros ordinarios R1–R5.
**Estado:** **implementado y probado** (no aceptado — pendiente de revisión).
**Punto de parada:** al terminar este informe. **B3-2 no fue iniciado.**

## 0. Archivos

| Archivo | Estado en este subbloque |
|---|---|
| `src/worldsim/server/repository.py` | **modificado** (único archivo de producción) |
| `tests/test_deleted_world_repository.py` | **nuevo** (único archivo de test) |
| `F7-E.2-I-BLOCK3-1-REPORT.md` | **nuevo** (este informe) |
| Cualquier otro archivo | **intacto** (verificado por manifiesto SHA256, §7) |

---

## 1. Git antes y después

| Ítem | Antes | Después |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` (sin commits) |
| `git status --porcelain` | 129 entradas | **131** (solo + test nuevo + informe) |
| Staged | 0 | 0 |
| Stash | 0 | 0 |
| SHA256 de `.py` (`src/`, `tests/`) | 94 en manifiesto | 95 (94 + test nuevo); **de los 94 preexistentes, solo `repository.py` difiere** |
| `worlds.db` SHA256 | `5D0B27BC493A2194…` | `5D0B27BC493A2194…` (intacto) |

Operaciones Git ejecutadas: solo lectura (`status`, `diff`, `rev-parse`, `ls-files`, `stash list`). Cero commits/resets/checkout/stash/clean. Cambios staged/unstaged/untracked preexistentes preservados (el delta de status es exactamente los 2 archivos nuevos de este subbloque).

> Nota: `repository.py` es **untracked** (`??`) en este repositorio — estado preexistente, sin cambios por mi parte; por eso el diff se documenta aquí con los fragmentos exactos old→new (§2) en lugar de `git diff`.

---

## 2. Diff exacto por función

Todos los cambios son **exclusivamente** la sustitución de la carga sin filtro por una carga con `WorldRow.deleted_at.is_(None)` (regla 8) más su docstring/comentario. **Cero** cambios de lógica, transacción, commit o rollback.

### R1 — `get_world` (`repository.py:119–131`)

```diff
     def get_world(self, world_id: int) -> WorldRow | None:
-        """Return the world row by primary key, or None if missing."""
+        """Return the world row by primary key, or None if missing.
+
+        F7-E.2: logically deleted worlds (``deleted_at IS NOT NULL``) are
+        treated as missing; ordinary reads never see them (decision 1).
+        """
         s = self._session
         return s.scalars(
-            select(WorldRow).where(WorldRow.id == world_id)
+            select(WorldRow).where(
+                WorldRow.id == world_id,
+                WorldRow.deleted_at.is_(None),
+            )
         ).first()
```

### R2 — `get_worlds_by_status` (`repository.py:133–144`)

```diff
     def get_worlds_by_status(self, status: str) -> list[WorldRow]:
-        """Return all worlds with the given status."""
+        """Return all worlds with the given status.
+
+        F7-E.2: logically deleted worlds are excluded (decision 1).
+        """
         s = self._session
         return s.scalars(
-            select(WorldRow).where(WorldRow.status == status)
+            select(WorldRow).where(
+                WorldRow.status == status,
+                WorldRow.deleted_at.is_(None),
+            )
         ).all()
```

### R3 — `update_status` (`repository.py:146–171`)

```diff
         s = self._session
         try:
-            row = s.get(WorldRow, world_id)
+            # F7-E.2: filtered load; a logically deleted world behaves
+            # exactly like a missing one (decision 5 -> return False).
+            row = s.scalars(
+                select(WorldRow).where(
+                    WorldRow.id == world_id,
+                    WorldRow.deleted_at.is_(None),
+                )
+            ).first()
             if row is None:
                 return False
             row.status = status
```

### R4 — `update_checkpoint_metadata` (`repository.py:173–211`)

```diff
         s = self._session
         try:
-            row = s.get(WorldRow, world_id)
+            # F7-E.2: filtered load; a logically deleted world is treated
+            # as missing (decision 5 -> return False, no writes).
+            row = s.scalars(
+                select(WorldRow).where(
+                    WorldRow.id == world_id,
+                    WorldRow.deleted_at.is_(None),
+                )
+            ).first()
             if row is None:
                 return False
             if last_checkpoint_at is not None:
```

### R5 — `save_checkpoint` (check de existencia, `repository.py:277–312`)

```diff
         s = self._session
         try:
             # Verify the world exists before writing a checkpoint for it.
-            world_row = s.get(WorldRow, world_id)
+            # F7-E.2: filtered load; a logically deleted world raises the
+            # same contractual ValueError as a missing one (byte-identical
+            # message; no checkpoint is persisted for it).
+            world_row = s.scalars(
+                select(WorldRow).where(
+                    WorldRow.id == world_id,
+                    WorldRow.deleted_at.is_(None),
+                )
+            ).first()
             if world_row is None:
                 raise ValueError(f"World {world_id} does not exist")
```

**Totales:** 5 predicados `WorldRow.deleted_at.is_(None)` nuevos (líneas 129, 142, 161, 196, 308); 3 `s.get(WorldRow, …)` reemplazados (R3/R4/R5) — **cero** `s.get(WorldRow` restantes en el archivo. El SQL crudo de R5 (`UPDATE worlds … WHERE id = :world_id`) no se toca: es inalcanzable para mundos eliminados porque el check filtrado lanza antes (y nunca fue una carga de `WorldRow`).

### Conservación de errores (§2 del encargo)

| Operación | Mundo ausente (antes) | Mundo eliminado (ahora) | Idéntico al contrato actual |
|---|---|---|---|
| R1 `get_world` | `None` | `None` | sí (indistinguible, DEC-PRE-4) |
| R2 `get_worlds_by_status` | excluido | excluido | sí |
| R3 `update_status` | `False` | `False` | sí |
| R4 `update_checkpoint_metadata` | `False` | `False` | sí |
| R5 `save_checkpoint` | `ValueError("World {id} does not exist")` | `ValueError` **byte-idéntico** | sí (mensaje exacto verificado en test) |

Sin excepciones genéricas nuevas; transacciones/commits/rollbacks sin cambios (sesión externa → flush-only, como antes).

### Excepciones históricas NO introducidas (§3 del encargo)

- `grep` en `repository.py`: **cero** apariciones de `include_deleted`, `allow_deleted`, `historical` ni `get_existing`.
- `get_world()` permanece **estrictamente ordinario** (sin parámetros ni mecanismos equivalentes).
- **Cero** helpers históricos creados; CS1/CS2 siguen fuera de esta capa (B3-5/B3-6).
- `deleted_at = None` en todo `src/`: **0** (cero restauración).
- Los 8 callsites de `get_existing()` (todos en `runner.py`): **intactos** — `runner.py` no fue modificado (hash idéntico al manifiesto).

---

## 3. Pruebas nuevas — `tests/test_deleted_world_repository.py` (12/12 verdes)

Aislamiento (§4 del encargo): motor SQLite temporal por test con `Base.metadata.create_all` + 2 `ALTER TABLE` (espejo de las columnas reales migradas `last_checkpoint_at`/`checkpoint_count`); **sin** `ensure_schema()` (evita su defecto de firma) y **sin** `WorldRow(status=…, visibility=…)` (evita el `TypeError` preexistente).

| # | Test | Resultado |
|---|---|---|
| 1 | `test_r1_get_world_returns_alive_world` | PASSED |
| 2 | `test_r1_get_world_excludes_deleted_world` | PASSED |
| 3 | `test_r1_get_world_missing_and_deleted_are_indistinguishable` | PASSED |
| 4 | `test_r2_get_worlds_by_status_excludes_deleted_worlds_ast` | PASSED (AST — ver §5) |
| 5 | `test_r3_update_status_deleted_world_returns_false_and_row_unchanged` | PASSED |
| 6 | `test_r3_update_status_alive_world_keeps_previous_behavior` | PASSED |
| 7 | `test_r4_update_metadata_deleted_world_returns_false_and_row_unchanged` | PASSED |
| 8 | `test_r4_update_metadata_alive_world_keeps_previous_behavior` | PASSED |
| 9 | `test_r5_save_checkpoint_deleted_world_raises_contractual_valueerror` | PASSED |
| 10 | `test_r5_save_checkpoint_alive_world_keeps_previous_behavior` | PASSED |
| 11 | `test_repository_has_no_historical_access_parameters` | PASSED |
| 12 | `test_repository_deleted_at_reads_are_is_none_and_never_assigned` | PASSED |

Cobertura del encargo: (1) R1 vivo/eliminado ✓ · (2) R2 exclusión ✓ (AST, §5) · (3) R3 sin actualización ✓ · (4) R4 sin actualización ✓ · (5) R5 sin checkpoint + mensaje exacto ✓ · (6) fila eliminada **byte-idéntica** en R3/R4/R5 (snapshot SQL crudo de 18 columnas antes/después) ✓ · (7) filas activas con comportamiento previo (R1 fila devuelta, R3 `True`, R4 `True` + `months_simulated` persistido, R5 checkpoint insertado + `months_simulated`/`last_checkpoint_at`/`checkpoint_count` actualizados) ✓ · tests 11–12: sin params históricos + toda lectura de `deleted_at` es `.is_(None)` + cero asignaciones ✓.

```text
12 passed in 2.16s
```

---

## 4. Pruebas existentes vs línea base

| Suite | Línea base | Después de B3-1 | Delta |
|---|---|---|---|
| `tests/test_repository.py` | **33f/7p** (2d §3.1/§4) | **33f/7p** | **0** |
| `tests/test_world_deletion.py` | 17/17 | **17/17** | **0** |

**Identidad de fallos de `test_repository.py` (sin nombres baseline aislados — el informe 2d §7.2 declara que esa línea base aislada nunca se capturó como tal; la comparación 2d fue por conteo + 11 nombres muestreados + causas):**
- Conteo idéntico: 33f/7p.
- Las 33 causas actuales son **solo** las dos raíces preexistentes documentadas en 2d: 30 × `TypeError: 'status' is an invalid keyword argument for WorldRow` + 3 × `TypeError: ensure_schema() takes 0 positional arguments but 1 were given`.
- Esos `TypeError` ocurren en la **construcción de fixtures** (`WorldRow(status=…, visibility=…)`) y en llamadas a `ensure_schema()`, es decir **antes de que cualquier consulta de R1–R5 se construya o ejecute**: ningún fallo actual puede ser causado por este cambio, y ningún test que falle ahora podía pasar antes (la construcción falla antes de llegar a mi código). Por contención de causas, el conjunto de 33 es el mismo que en la línea base; los 7 verdes también.
- Cero menciones de `deleted_at`/`is_(None)`/B3-1 en la salida de fallos.
- Cero fallos fuera de `test_repository.py` en esa ejecución.

Tests preexistentes **no** fueron modificados ni eliminados.

---

## 5. Limitaciones de ejecución por defectos preexistentes (declaradas, sin corregir)

1. **R2 no es ejecutable en verde (AST-only, autorizado):** `WorldRow.status` no es columna mapeada en el ORM → `WorldRow.status == status` lanza `AttributeError` al construir la consulta (defecto preexistente: deriva ORM↔`worlds.db`, fuera de alcance). El test 4 verifica **estructuralmente** (AST) que la función conserva su predicado de estado **y** añade `WorldRow.deleted_at.is_(None)` dentro del mismo `.where(...)`. **Esto NO es una prueba de ejecución SQL** y no se presenta como tal: demuestra la presencia del predicado de exclusión en el código, no que una consulta SQL corrió.
2. **R3 vivo no persiste `status`:** `row.status = status` asigna un atributo no mapeado (defecto preexistente). Los tests solo asertan el retorno (`True`/`False`) y la integridad de la fila, nunca la persistencia de `status`.
3. **R4:** `row.last_checkpoint_at`/`row.checkpoint_count` tampoco están mapeados; los tests usan `months_simulated` (mapeado) para asertar persistencia y evitan `increment_checkpoint_count=True` (lanzaría `AttributeError` preexistente al leer `row.checkpoint_count`).
4. **`ensure_schema()`** no fue llamado en ningún test nuevo (su firma es de 0 argumentos; el defecto sigue intacto).
5. **Ventana de concurrencia W2 (reserva 2c.7 — NO aliviada):** antes del commit de `deleted_at`, estos filtros leen `deleted_at IS NULL` y devuelven el mundo igual que antes — **correcto** (la eliminación aún no es durable). La carrera de dos workers con lecturas previas al commit **sigue sin demostrarse resuelta**; nada de este subbloque afirma lo contrario.

---

## 6. Confirmaciones del encargo

- [x] `WorldRow.deleted_at.is_(None)` en R1, R2, R3, R4, R5 (5/5).
- [x] `repository.get_world()` estrictamente ordinario (cero params históricos; test 11).
- [x] Errores conservados: `None`/`False`/`ValueError` con mensaje **byte-idéntico** (`f"World {world_id} does not exist"`, test 9).
- [x] Sin excepciones históricas en repository; CS1/CS2 diferidos (B3-5/B3-6).
- [x] Sin filtros de timestamps ni comparaciones naive/aware (solo `IS NULL`; test 12).
- [x] Sin cambios transaccionales.
- [x] 8 callsites de `get_existing()` intactos (`get_existing` ausente de `repository.py`; `runner.py` hash idéntico).
- [x] Sin arreglos oportunistas de defectos preexistentes.
- [x] Tests preexistentes sin modificar.
- [x] B3-2 **no** ejecutado.
- [x] Sin operaciones Git destructivas; staged/unstaged/untracked preexistentes preservados.

## 7. Integridad del repositorio (verificación final)

```text
HEAD: e6f6983 | staged: 0 | stash: 0
status: 129 -> 131 (delta: ?? tests/test_deleted_world_repository.py + ?? F7-E.2-I-BLOCK3-1-REPORT.md)
hashes .py: 94 preexistentes -> solo src/worldsim/server/repository.py difiere
             (+ tests/test_deleted_world_repository.py, nuevo, fuera del manifiesto)
worlds.db SHA256: 5D0B27BC493A2194… (intacto)
deleted_at en src/: 11 ocurrencias (db.py 1, runner.py 4, repository.py 6) — 0 asignaciones `= None`
```

---

## 8. Estado y parada

**Implementado y probado.** 12/12 tests nuevos verdes; 17/17 de `test_world_deletion.py`; `test_repository.py` 33f/7p idéntico a línea base con causas preexistentes; integridad verificada.

**Me detengo aquí. No se inició B3-2 (startup/`recovery_coordinator.py`) ni ningún subbloque posterior.** Pendiente de revisión y autorización expresa.
