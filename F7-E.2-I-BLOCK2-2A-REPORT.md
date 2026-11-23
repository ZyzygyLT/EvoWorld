# F7-E.2-I-BLOCK2-2A-REPORT — Implementación del subbloque 2a: eliminación lógica en `_delete_world_row`

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-BLOCK2-2A (subbloque 2a del PRE aprobado `F7-E.2-I-BLOCK2-PRE.md`).
**Alcance ejecutado:** un único archivo, una única función. Nada más fue modificado.

---

## 1. Diff exacto de la función

Generado con `difflib` extrayendo la versión nueva directamente del archivo real
(`ast.get_source_segment` sobre `src/worldsim/server/runner.py`), para que sea
byte-exacto:

```diff
--- src/worldsim/server/runner.py (pre-2a)
+++ src/worldsim/server/runner.py (post-2a)
@@ -1,10 +1,19 @@
 def _delete_world_row(session_factory, world_id: int) -> None:
-    """Blocking row delete; runs in a WORKER THREAD (never on the loop)."""
+    """Blocking LOGICAL row delete; runs in a WORKER THREAD (never on the loop).
+
+    The row is NEVER physically removed: ``deleted_at`` is the single
+    persisted source of truth (NULL: active; timestamp: logically deleted).
+    Idempotent (DEC-PRE-3): an already-marked row keeps its exact original
+    value — the assignment happens only when ``deleted_at IS NULL``.
+    """
+    from datetime import datetime, timezone
+
     from worldsim.server.db import WorldRow

     with session_factory() as session:
         row = session.get(WorldRow, world_id)
         if row is None:
             raise WorldNotFoundError(world_id)
-        session.delete(row)
+        if row.deleted_at is None:
+            row.deleted_at = datetime.now(timezone.utc)
         session.commit()
```

Resultado funcional (cuerpo, tras la línea base de 10 líneas → 19 líneas):

```python
def _delete_world_row(session_factory, world_id: int) -> None:
    ...
    from datetime import datetime, timezone

    from worldsim.server.db import WorldRow

    with session_factory() as session:
        row = session.get(WorldRow, world_id)
        if row is None:
            raise WorldNotFoundError(world_id)
        if row.deleted_at is None:
            row.deleted_at = datetime.now(timezone.utc)
        session.commit()
```

Decisiones de forma:
- **Import local** dentro de la función (junto al import local ya existente de
  `WorldRow`): produce el diff más pequeño y coherente; **no** se crea ningún
  helper UTC nuevo; **no** se añaden imports de nivel módulo que afecten a
  otros símbolos de `runner.py`.
- Docstring actualizado dentro de la misma función (el texto "row delete" habría
  quedado falso); ningún código fuera de la función aparece en el diff.

---

## 2. Archivos modificados

| Archivo | Cambio propio en 2a | Estado |
|---|---|---|
| `src/worldsim/server/runner.py` | **Único** — solo la función `_delete_world_row` | Modificado (preexistente + 2a) |
| Cualquier otro `.py` (src/, tests/) | **Ninguno** | Intacto |
| ORM / migraciones (`db.py`) | Ninguno (`deleted_at` ya existía desde Bloque 1) | Intacto |
| Pruebas | Ninguna | Intactas |
| Documentos anteriores | Ninguno (solo se crea este informe) | Intactos |

Evidencia de unicidad: filtro de mtimes sobre `src/` y `tests/` con corte en la
entrega del PRE (19/11 01:54:43, `F7-E.2-I-BLOCK2-PRE.md`) ⇒ **solo
`runner.py`** (19/11 10:02) aparece.

---

## 3. Verificaciones ejecutadas y resultados

| # | Verificación exigida | Método | Resultado |
|---|---|---|---|
| 1 | Inspeccionar diff inicial y preservar cambios preexistentes | `git diff --stat HEAD -- runner.py` antes/después + `git status` | **Antes:** 935+/16− (951 líneas). **Después:** 944+/16− (960). Delta **+9 inserciones, 0 borrados** — exactamente 19−10 líneas de la función. Ninguna línea preexistente eliminada ni alterada; los cambios preexistentes de `runner.py` y del resto del worktree quedan intactos |
| 2 | Implementar únicamente la función autorizada | AST: número de `FunctionDef` con ese nombre; diff de la región | `_delete_world_row` encontrada **exactamente 1**; el diff (§1) contiene **solo** esa función. Única escritura ejecutada: una operación de edición con 1 reemplazo |
| 3 | Sin `session.delete(row)` ni DELETE en la función | AST (`'session.delete' in segment`), recuento en archivo completo, grep SQL | Función: **False**. Archivo completo: **0 ocurrencias** de `session.delete`. Sin `DELETE` (SQL) en la función |
| 4 | Asignación solo cuando `deleted_at is None` | Presencia del guard + inspección de flujo | `if row.deleted_at is None:` presente; la asignación es su única rama. Si la fila ya está marcada: **cero escrituras de valor**, solo `commit()` (≡ `SET deleted_at = deleted_at`, DEC-PRE-3) |
| 5 | `datetime.now(timezone.utc)`; prohibido `utcnow` | Búsqueda en función y en **todo** el archivo | `datetime.now(timezone.utc)`: **presente**. `utcnow` en todo `runner.py`: **0** |
| 6 | `WorldNotFoundError` y `session.commit()` conservados | AST + inspección | Ambos presentes: `raise WorldNotFoundError(world_id)` ante `row is None` (ausencia física → 404 legacy); `session.commit()` como última sentencia |
| 7 | Comprobaciones estáticas/sintaxis sin modificar otros archivos | `ast.parse`, `py_compile`, import del módulo | `ast.parse`: **OK** · `py_compile`: **OK** · `import worldsim.server.runner`: **OK** (`_delete_world_row` y `safe_delete_world` callable, `registry` = `RunnerRegistry`; sin `session.delete` ni `utcnow` en el código fuente de la función) |
| 8 | No declarar `test_world_deletion.py` superado | Política de reporting | **No se ejecutó ni se declara superado.** El harness (`_FakeRow` sin `deleted_at`, asserts `h.store == {}` / op `"delete"`) asume borrado físico y fallaría con `AttributeError` — adaptación en **2b**, como prevé el PRE §8.1 |
| 9 | Revisar `git diff`/`git status` distinguiendo lo propio de lo preexistente | §4 abajo | Hecho y documentado |

Comprobación adicional (límite Bloque 3): grep de `deleted_at` en todo `src/`
⇒ **solo 5 apariciones**: `db.py:38` (columna, Bloque 1) y las 4 líneas de la
función nueva (docstring ×2, guard, asignación). **Ningún filtro de lectura**
introducido (sin `.where(WorldRow.deleted_at…)` ni equivalentes).

---

## 4. Estado Git

| Ítem | Antes de 2a | Después de 2a |
|---|---|---|
| Branch / HEAD | `feature/engine-checkpoints` / `e6f6983` | **sin cambios** (`e6f6983`) |
| Entradas en `git status --porcelain` | 123 | **123** (ninguna entrada nueva/eliminada) |
| `runner.py` vs HEAD | 935+/16− | 944+/16− (**delta +9/0 = la función**) |
| Índice (staged) | vacío | **vacío** (`git diff --cached --name-only` sin resultados; mis cambios en `runner.py` están solo en worktree, ` M`) |
| Stash | vacío | **vacío** |
| Commits / reset / checkout / add / stash ejecutados por mí | — | **Ninguno** (solo comandos de solo lectura: `status`, `diff`, `show`, `log`, `ls-files`, `rev-parse`, `branch`, `stash list`) |

Nota sobre dos entradas con código ` A` (`src/worldsim/export/checkpoint.py`,
`tests/test_checkpoint.py`): verificadas como **intent-to-add preexistentes**
(`git ls-files` las lista, `git diff --cached` vacío, no existen en HEAD, mtime
anterior a esta intervención). **No fueron creadas ni alteradas por este
subbloque**; se preservan como modificación preexistente (regla 4 AGENTS.md).

---

## 5. `safe_delete_world()` y sus garantías: intactas

El diff (§1) no contiene ninguna línea de `safe_delete_world`. Verificación por
recorrido de la secuencia en el archivo resultante (líneas 1037–1121, desplazadas
+9 por el crecimiento de la función; **mismo orden absoluto** que la línea base
del PRE §2.1):

| Paso | Pre-2a | Post-2a | Estado |
|---|---|---|---|
| `begin_delete` (marca síncrona) | 1073 | 1082 | ✅ intacto |
| `get_existing` (sin chequeo `_deleting`; sin cambios permitidos) | 1077 | 1086 | ✅ intacto |
| `await runner.close()` | 1079 | 1088 | ✅ intacto |
| Verify `CLOSED` (solo entonces prosigue) | 1080–1087 | 1089–1096 | ✅ intacto |
| `unregister` (solo el verificado-cerrado) | 1089 | 1098 | ✅ intacto |
| `asyncio.to_thread(_delete_world_row, …)` — worker, orden `unregister → UPDATE` (Alternativa B) | 1091–1092 | 1101 | ✅ intacto (el **solo** cambio es el interior de la función llamada) |
| `add_done_callback(_release_lingering_delete)` — referencia fuerte | 1095 | 1104 | ✅ intacto |
| `asyncio.shield(work)` — cancelación no aborta el trabajo | 1097 | 1106 | ✅ intacto |
| Cancel → `end_delete` vía callback + `guard_transferred` (transferencia de guarda) | 1098–1106 | 1106–1115 | ✅ intacto |
| `finally: end_delete` si no hubo transferencia | 1108–1112 | 1117–1121 | ✅ intacto |

Invariantes preservados: **no** se modificó `safe_delete_world()` ni su orden;
**no** se modificó `RunnerRegistry`, `begin_delete`, `end_delete`, `unregister`
ni `get_existing` (el diff no contiene ninguna de esas definiciones); **no** se
tocaron `to_thread`, `shield`, callbacks ni la transferencia de guarda. El
`WorldNotFoundError` que el worker puede lanzar sigue el mismo canal de
propagación (`shield` → caller) que ya probaba `test_del_05`.

Garantía de atomicidad equivalente a la previa: excepción antes de `commit()`
(p. ej. `commit_error`) ⇒ rollback implícito del contexto de sesión ⇒
`deleted_at` conserva su valor exacto anterior (nunca una marca parcial).

---

## 6. Limitaciones de validación pendientes para 2b

1. **`tests/test_world_deletion.py` NO ejecutado en este subbloque** y sus
   resultados **no declarados**: el harness está escrito para borrado físico
   (`_FakeRow` sin atributo `deleted_at` ⇒ `AttributeError` en el guard;
   aserciones `h.store == {}`, op `("delete", 7)` y órdenes con `"commit"` como
   borrado). La adaptación del harness es el contenido de **2b** (PRE §8.1,
   plan Fase 2 paso 2).
2. **Los 7 tests nuevos del PRE §8.2 no existen todavía** (idempotencia,
   no-sobrescritura, tz-aware, 404 por ausencia física, rollback de marca) —
   son **2c**.
3. **Validación end-to-end imposible por defecto preexistente** (PRE §7.1):
   la app no arranca (`ensure_schema(engine)`), `WorldRow` carece de
   `status`/`visibility` ⇒ ninguna suite de sistema puede atribuirse al 2a.
4. **DEC-PRE-4 (GET → 404) sigue pendiente**: hasta el Bloque 3 no hay filtros
   de lectura; un mundo con `deleted_at` fijado seguirá siendo visible para
   GET/listados. Este subbloque **no lo declara cumplido**.
5. **Estado de "implementado, no probado":** este subbloque deja la eliminación
   lógica **implementada y verificada estáticamente** (AST, compilación,
   import, invariantes de flujo). **No está probada funcionalmente** hasta 2b/2c
   ni aceptada: la distinción implementado / probado / aceptado se mantiene
   (regla 7 AGENTS.md).

---

## Dictamen del subbloque 2a

- **Implementado:** sí — única función autorizada, eliminación lógica con
  guard idempotente y `datetime.now(timezone.utc)`, sin `session.delete`.
- **Alcance respetado:** un archivo, una función; cero cambios en otros `.py`,
  tests, ORM, migraciones, documentos previos, DEC19 o F7-D-I1; cero filtros de
  lectura; cero operaciones Git destructivas; cambios preexistentes preservados
  y cuantificados.
- **Pruebas:** no ejecutadas en 2a por diseño (pendencia formal de 2b).

**Estado:** entregable de 2a completo. **Se detiene aquí — no se inicia 2b ni
ningún otro subbloque.**
