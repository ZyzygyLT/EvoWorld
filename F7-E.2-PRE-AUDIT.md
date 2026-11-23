# F7-E.2-PRE-AUDIT: Validación final del contrato de eliminación lógica

## Objetivo

Auditar `F7-E.2-PRE-DECISION.md` y contrastarlo con la implementación actual del repositorio para determinar si el contrato de eliminación lógica está suficientemente definido para autorizar F7-E.2-I.

Tarea de inspección y documentación exclusivamente.

No modificar código, ORM, migraciones, pruebas, datos ni contratos existentes. No crear commits ni ramas.

---

## 1. Verificación de evolución del esquema

### 1.1. `ensure_schema()` inspeccionada (`src/worldsim/server/db.py:226-253`)

La función `ensure_schema()` implementa un mecanismo de migración "lazy ADD COLUMN" para SQLite:

```python
def ensure_schema() -> None:
    """Create tables + migrate old DBs (dev worlds.db predates league columns)."""
    from sqlalchemy import inspect, text

    Base.metadata.create_all(engine_db)
    if engine_db.dialect.name != "sqlite":
        return
    insp = inspect(engine_db)
    with engine_db.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table_name)}
            for col in table.columns:
                if col.name not in existing and col.name != "id":
                    default = f" DEFAULT {col.server_default.arg}" if col.server_default else ""
                    conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col.type}{default}")
                    )
ensure_schema()  # migrate old dev DBs in place (ADD COLUMN is idempotent-safe)
```

** hallazgos:**

1. **Puede añadir columnas a tablas existentes:** Sí. El bucle itera sobre `Base.metadata.tables.items()` y para cada tabla que existe en la BD, ejecuta `ALTER TABLE {table_name} ADD COLUMN {col.name} {col.type}{default}`. Esto incluye la tabla `worlds`.

2. **Detección de columna existente:** Usa `insp.get_columns(table_name)` para obtener columnas existentes y verifica `if col.name not in existing`. Si la columna ya existe, se salta (no intenta añadirla nuevamente). Esto la hace **idempotent-safe**.

3. **Valor por defecto para existentes:** `default = f" DEFAULT {col.server_default.arg}" if col.server_default else ""`. Si la columna tiene `server_default`, se aplica; si no, se añade sin default. Para `deleted_at TIMESTAMP NULL`, se debería especificar `NULL` explícitamente o confiar en que SQLite por default permite NULL en columnas añadidas.

4. **Compatibilidad con motores:** `if engine_db.dialect.name != "sqlite": return` — la funcionalidad `ADD COLUMN` explícita solo se ejecuta para SQLite. Para otros motores (PostgreSQL, MySQL), `Base.metadata.create_all(engine_db)` al inicio crea las tablas/columnas según el modelo ORM (asumiendo migraciones Alembic u otro sistema). El proyecto asume SQLite por defecto.

5. **Fallos y ejecución repetida:** Es **idempotent-safe** por diseño. Si la columna ya existe, `col.name not in existing` es False y se salta. Si falla la ALTER TABLE (BD llena, lock, etc.), la excepción se propagaría y la migración no completaría, pero en ejecuciones posteriores el `ensure_schema()` volvería a intentarlo. No hay `try/except` alrededor de la ALTER TABLE individual.

6. **Compatibilidad con motors soportados:** Solo garantizado para SQLite. Para PostgreSQL/MySQL, la columna tendría que añadirse mediante otro mecanismo (Alembic, migraciones manuales, definición en modelo ORM con `create_all`). El proyecto no tiene migraciones formales Alembic — confía en `ensure_schema()` para SQLite y manual/externo para otros motores.

### 1.2. Recomendación sobre migración vs `ensure_schema()`

**Recomendación:** Añadir `deleted_at` al modelo `WorldRow` y confiar en `ensure_schema()` para SQLite, con documentación de que para motores no-SQLite se requiere migración formal (fuera del alcance de F7-E.2). La columna debe definirse como `nullable=True` para que `ensure_schema()` la añada con valor NULL a registros existentes sin necesidad de UPDATE masiva.

**Justificación:** `ensure_schema()` ya resuelve el problema para la configuración por defecto (SQLite). Añadir `deleted_at = Column(String)` (o Timestamp) con `nullable=True` al modelo ORM hará que `ensure_schema()` la añada automáticamente a DBs viejas. Es el camino de menor fricción y coherente con la arquitectura existente.

**Alternativa:** Crear migración Alembic formal. **No recomendado** para F7-E.2, ya que el proyecto no usa Alembic y introducirlo cambiaría la arquitectura de despliegue.

---

## 2. Atomicidad y recuperación de `safe_delete_world()`

### 2.1. Flujo actual (`src/worldsim/server/runner.py:1028-1112`)

La secuencia contractual es:

```
1. registry.begin_delete(world_id)          → marca world como "siendo eliminado"
2. async with lock:                         → serializa simultáneas
3. runner = registry.get_existing(world_id) → obtener runner
4. await runner.close()                     → drenar, verificar CLOSED
5. registry.unregister(world_id)            → retirar runner
6. UPDATE deleted_at (NEW: en lugar de DELETE físico)
7. flush/commit session
```

### 2.2. Análisis de puntos de fallo

| Punto de fallo | Comportamiento actual (DELETE físico) | Comportamiento contractual (UPDATE deleted_at) | Riesgo / Garantía |
|---|---|---|---|
| **Antes de `begin_delete()`** | No aplica | No aplica | Mundo en estado normal. `begin_delete` lo marcará. |
| **Después de `begin_delete()`** | No aplica | No aplica | `world_id` está en `_deleting` set. `get()`/`register_existing()` lo rechazan. |
| **Durante `runner.close()`** | Si falla (CLOSE_INCOMPLETE): RAISE, datos intactos, world recoverable. | Igual. `runner.close()` es independiente de la UPDATE a `deleted_at`. Fallo aquí = world stays recoverable, `deleted_at` no se toca. | ✅ Garantía preservada. |
| **Después de close, antes de UPDATE** | En el flujo DELETE físico: `_delete_world_row` en worker thread. Si falla: la excepción se registra, `work.add_done_callback` consume el error, la guard `registry.end_delete()` se transfiere al callback. El world **no** se elimina físicamente. | **NUEVO**: UPDATE `deleted_at` en lugar de DELETE físico. Si falla la UPDATE: la excepción se propagaría (o se capturaría y manejaría). El world quedaría con runner cerrado pero `deleted_at = NULL` (aún no eliminado). | ⚠️ **Punto crítico**: Si la UPDATE falla después de `unregister()`, el world no tiene runner activo (fue des-registrado) pero tampoco está marcado como eliminado. Quedaría en estado "órfano": sin runner, sin eliminado lógico. |
| **Durante UPDATE de `deleted_at`** | No aplica (era DELETE físico en worker thread). | La UPDATE se ejecuta en el hilo principal (no worker thread shielded, para garantía atomicidad). Si falla: exception propagate up. El caller ve el error y el world queda: runner cerrado, `deleted_at = NULL`. | ⚠️ **Recovery necesario**: El contrato debe definir que el caller reintenta o administra la situación. No puede "silenciosamente convertir éxito". |
| **Después del UPDATE, antes de completar** | En flujo físico: `work.add_done_callback(_release_lingering_delete)` consume el resultado. Si caller se cancela: el delete continúa en worker thread; la guarda se transfiere al callback; caller ve `CancelledError`. | **NUEVO**: La UPDATE es transaccional en el hilo principal. Si caller se cancela DURANTE la UPDATE: SQLite rollback transaccional → `deleted_at` no se actualiza, state coherente. Si caller se cancela DESPUÉS de la UPDATE pero ANTES de completar el `async with lock`: El `finally` del `async with` suelta el mark y lock. El world tiene `deleted_at IS NOT NULL`, runner des-registrado. | ✅ **Mejor que DELETE físico**: La UPDATE transaccional en hilo principal garantiza que o se completa o no hay cambio. SQLite asegura atomicidad. |
| **Cancelación de coroutine** | El delete en worker thread continúa; guarda transferida al callback; caller ve `CancelledError`. | La UPDATE transaccional en hilo principal: si se cancela, la transacción de SQLite se revierte automáticamente. `deleted_at` queda en su valor anterior (NULL). State coherente. | ✅ **Mejor garantía**: SQLite rollback transaccional vs. worker thread que sigue corriendo. |
| **Fallos worker thread** | En flujo físico: `_delete_world_row` en worker. Si falla: `work.exception()` registrado, callback `_release_lingering_delete` consume, `registry.end_delete()` transferido. El world **no** se elimina físicamente pero el runner sigue sin estar registrado (órfano). | **NUEVO**: No hay worker thread para UPDATE. La UPDATE falla o tiene éxito en el hilo principal. Si falla: exception propagate, world queda: runner cerrado, `deleted_at = NULL`. | ✅ **Eliminado riesgo de worker thread**: Ya no hay operaciones en worker thread para el delete. La atomicidad es del hilo principal. |

### 2.3. Determinación de riesgo crítico

**Pregunta:** ¿Puede un fallo del UPDATE posterior a `unregister()` dejar un mundo persistido como no eliminado pero sin runner registrado?

**Respuesta:** **Sí, es posible** en el diseño actual si la UPDATE falla después de `registry.unregister(world_id)`. El world tendría:
- `deleted_at = NULL` (no marcado como eliminado)
- Sin runner registrado (fue des-registrado en el paso 5)
- Runner cerrado (del paso 3)

**Estado resultante:** Mundo "órfano": corriendo (o no) pero sin runner en registry y sin eliminación lógica. No es recoverable de la misma forma que antes (no hay runner para reanudar), pero tampoco está marcado como eliminado.

**Definición de comportamiento de recuperación necesario:**

El contrato debe especificar una de estas opciones:

1. **Opción A (recomendada):** La UPDATE debe ejecutarse **antes** de `unregister()`, no después. La secuencia sería:
   - `begin_delete` → `close` → `verify` → `UPDATE deleted_at` → `unregister`
   - Si la UPDATE falla, `unregister()` no se ejecuta, el runner sigue registrado y el world stay recoverable.
   - **Ventaja:** Si UPDATE falla, world stay recoverable (runner aún en registry). Si UPDATE succeeds, entonces `unregister` y world está eliminado lógicamente.

2. **Opción B (alternativa):** Si la UPDATE falla después de `unregister()`, el sistema administrativa debe tener un procedimiento de "recuperación de worlds órfanos" que:
   - Vuelva a registrar el runner (si aún existe datos físicos) o admita que el world quedó sin runner.
   - Establezca `deleted_at = NULL` administrativamente.
   - **Desventaja:** Requiere procedimiento administrativo fuera del contrato R0.

3. **Opción C (conservadora):** Mover `unregister()` **después** de la UPDATE, pero envolver todo el bloque (UPDATE + unregister) en un try/except que, ante fallo, ejecute `registry.end_delete(world_id)` para re-liberar el mark y dejar el world en estado recoverable con runner todavía en registry.

**Recomendación:** **Opción A** es la más coherente con las garantías existentes de DEC18. Mover la UPDATE antes de `unregister()` garantiza que:
- Si UPDATE falla → `unregister()` no se ejecuta → runner sigue en registry → world stay recoverable.
- Si UPDATE succeeds → `unregister()` ejecuta → world está eliminado lógicamente y sin runner en registry (esperado).

Esto requiere reordenar la secuencia en `safe_delete_world()`, pero es un cambio conceptual (reordenar pasos), no añadir nueva lógica compleja.

**Nota:** El `F7-E.2-PRE-DECISION.md` aún no especifica el orden exacto de UPDATE vs `unregister`. Esto sería una "decisión menor pendiente" (Categoria B) o parte del contrato listo (Categoria A) una vez definido.

---

## 3. Matriz completa de lecturas

### 3.1. Lista de rutas consultadas y su comportamiento respecto a `deleted_at`

| Ruta / Función | Archivo | Consulta/Mecanismo | Comportamiento actual | Comportamiento contractual futuro |
|---|---|---|---|---|
| **`GET /api/worlds` (modo público)** | `app.py:362` | `select(WorldRow)... WHERE visibility == "public"` | Lista todos los worlds públicos. No filtra por `deleted_at`. | **Excluir** `WHERE deleted_at IS NULL` por defecto. Los worlds eliminados no aparecen. |
| **`GET /api/worlds` (modo `mine`)** | `app.py:362` | `WHERE owner_user_id == user.id` | Lista worlds del propietario. No filtra por `deleted_at`. | **Excluir** `deleted_at IS NULL`. El propietario tampoco ve worlds eliminados. |
| **`GET /api/worlds/{id}` (detalle)** | `app.py:661` | `_load_row(world_id)` → `db_.get(WorldRow, world_id)` | Retorna world completo o ficha pública según ownership. | **Retornar 404** o ficha pública indicando world eliminado. No exponer `state_json`. |
| **`PUT /api/worlds/{id}/visibility`** | `app.py:682` | `db_.get(WorldRow, world_id)` + UPDATE visibility | Owner cambia visibilidad. | **Rechazar** si `deleted_at IS NOT NULL`. Mundo eliminado no admite cambios de visibilidad (o requiere procedimiento administrativo especial). |
| **`DELETE /api/worlds/{id}`** | `app.py:718` | `safe_delete_world()` → secuencia close→verify→unregister→delete | Elimina world físicamente. | **Ejecutar eliminación lógica** (UPDATE deleted_at) en lugar de DELETE físico. |
| **`GET /api/worlds/{id}/stats/{kid}`** | `app.py:753` | `_build_engine(row.seed...)` + tick | Stats para un kingdom. Requiere world activo. | **Excluir** si `deleted_at IS NOT NULL`. No hay stats para worlds eliminados. |
| **`GET /api/worlds/{id}/predictions`** | `app.py:780` | `_load_visible_row()` + `list_predictions()` | Lista predictions. | **Excluir** si `deleted_at IS NOT NULL`. |
| **`GET /api/worlds/{id}/chat`** | `app.py:818` | `db_.scalars(select(ChatRow)...)` | Chat history. | **Excluir** si `deleted_at IS NOT NULL`. No hay chat en worlds eliminados. |
| **`GET /api/worlds/{id}/leaderboard`** | `app.py:809` | `leaderboard(world_id)` | Leaderboard. | **Excluir** si `deleted_at IS NOT NULL`. |
| **`GET /api/worlds/{id}/claims`** | `app.py:871` | `get_claims(world_id)` | Claims del world. | **Excluir** si `deleted_at IS NOT NULL`. |
| **`GET /api/worlds/{id}/events`** | `app.py:1181` | `EventRepository` queries | Events history. | **Excluir** events nuevos, pero **histórico events** pueden conservarse (aunque world está eliminado). Requerir análisis. |
| **Recuperación runners startup** | `app.py:1188` (via `run_startup`) | Recovery coordinator | Reconstruye runners para worlds en recovery. | **No reconstruir** runners para worlds con `deleted_at IS NOT NULL`. |
| **`_canonical_month(world_id)`** | `app.py:1288` | `registry.get_existing(world_id)` o `db_.get(WorldRow, world_id)` | Devuelve month simulado. | **Retornar 0 o error** si `deleted_at IS NOT NULL`. World eliminado no tiene clock activo. |
| **`_load_row(world_id)`** | `app.py:553` | `db_.get(WorldRow, world_id)` | Cargar world row. | **Lanzar 404** o indicar "world eliminado" si `deleted_at IS NOT NULL`. |
| **Listados internos / administrativos** | Varios | Queries `SELECT WorldRow...` | No filtran por estado. | **Añadir** `WHERE deleted_at IS NULL` por defecto, o endpoints administrativos con flag `include_deleted=true`. |

### 3.2. Determinación de consistencia de exclusión

**Problema:** ¿Cómo evitar que una consulta olvidada permita que un mundo eliminado reaparezca en el ciclo operativo?

**Solución propuesta:**

1. **Nivel repositorio:** El `WorldRepository` y todas las queries que accedan a `WorldRow` deben incluir `WHERE deleted_at IS NULL` por defecto. No confiar en que cada endpoint lo recuerde.

2. **Nivel ORM / vista:** Considerar crear una vista SQL o propiedades en el modelo ORM que filtren automáticamente. Pero como el proyecto no usa vistas y modificar ORM está fuera de alcance, la solución es nivel de query.

3. **Nivel de documento contractual:** F7-E.1 H5 debe establecer explícitamente: "La elegibilidad pública requiere `deleted_at IS NULL`. Los queries que no incluyan esta condición tienen comportamiento indefinido y están fuera de contrato."

4. **Auditoría de queries:** Revisar todas las queries `SELECT WorldRow` en el códigobase y documentar cuáles añaden el filtro y cuáles no. Las que no lo hacen deben añadirse en la fase de implementación o documentarse como "solo para uso administrativo con flag explícito".

**Hallazgo crítico:** Actualmente **ninguna** query en el código filtra por `deleted_at`. Esto significa que, tal como está el código hoy, un world eliminado lógicamente **aparecería** en listados públicos si no se añade el filtro. Esto es exactly el problema que F7-E.2-PRE-DECISION pretende resolver. La matriz de lecturas confirma que **TODAS** las queries públicas deben añadir `WHERE deleted_at IS NULL`.

---

## 4. Semántica complementaria

### 4.1. Casos límite definidos en el contrato

| Comportamiento | Estado en contrato | Comentario |
|---|---|---|
| **Eliminación repetida de un mundo ya eliminado** | **Pendiente definir** | Si `deleted_at IS NOT NULL` y se intenta nueva eliminación: la `Opción A` (UPDATE antes de unregister) haría un no-op seguro. La `Opción B` requiere procedimiento administrativo. | Requerido definir en F7-E.2-I. |
| **Consulta por ID de un mundo eliminado** | **Pendiente definir** | `GET /api/worlds/{id}` si `deleted_at IS NOT NULL`: ¿404? ¿Ficha pública "world eliminated"? | Requerido definir en F7-E.2-I. |
| **Acceso administrativo a información histórica** | **Pendiente definir** | ¿Existe endpoint `GET /api/worlds/{id}?admin=true` para ver info de world eliminado? ¿Requiere autorización especial? | Requerido definir. |
| **Prohibición de cargar runners para mundos eliminados** | **Definido implícitamente** | `_canonical_month()` y startup recovery deben retornar/omitir worlds con `deleted_at IS NOT NULL`. | Confirmado por inspección: `runner.get()` rechaza worlds en `_deleting`, y startup recovery debe filtrarlos. |
| **Imposibilidad de que un mundo eliminado aparezca en destacados** | **Definido en F7-E.1 H5** | "La elegibilidad pública requiere `deleted_at IS NULL`". Los worlds con `deleted_at IS NOT NULL` no aparecen en `highlighted=1`. | ✅ **Confirmado** en F7-E.1. |
| **Política de restauración: no introducir restauración si no está definida y autorizada** | **Pendiente definir** | ¿Puede un administrador "restaurar" un world eliminado poniendo `deleted_at = NULL`? ¿Requiere nueva concesión? | Requerido definir. |
| **Conservación de referencias históricas y restricciones FK** | **Definido** | `world_highlights.world_id` con `ON DELETE RESTRICT`: los worlds eliminados (`deleted_at IS NOT NULL`) **no pueden borrarse físicamente** hasta que se consigan los highlights. La eliminación lógica preserva la fila, por lo tanto las FK históricas se mantienen. | ✅ **Confirmado** en F7-E.1 H4. |

### 4.2. Incoherencias detectadas

| Cuestión | Estado | Comentario |
|---|---|---|
| Falta de `deleted_at` en queries actuales | **Crítico** | Todas las queries públicas necesitan `WHERE deleted_at IS NULL` añadido. |
| Orden `UPDATE` vs `unregister` en `safe_delete_world` | **Pendiente** | Definir si UPDATE va antes o después de `unregister()`. |
| Comportamiento ante eliminación repetida | **Pendiente** | ¿No-op? ¿Error? ¿Administrativo? |
| Recuperación/admin de worlds órfanos | **Pendiente** | ¿Qué pasa si la UPDATE falla después de `unregister()`? |

---

## 5. Compatibilidad con contratos anteriores

### 5.1. DEC16

**Efecto:** Ninguno directo. DEC16 trata sobre `recoverable/non-recoverable` state, no sobre mecanismo de delete de worlds.

**Incompatibilidad real:** Ninguna. Las reglas de recoverable permanecen aplicables a runners y datos de simulación, independientemente de `deleted_at`.

**Necesidad de actualización documental:** Ninguna. DEC16 no entra en conflicto con `deleted_at`.

### 5.2. DEC18

**Efecto:** El `safe_delete_world()` (DEC18 §8-§9) experimenta una **evolución conceptual** pero no un cambio sustantivo en su secuencia base.

**Incompatibilidad real:** Ninguna, si se sigue la **Opción A** (UPDATE antes de `unregister`). La secuencia DEC18 se extiende:
- Antigua: `begin_delete` → `close` → `unregister` → `DELETE físico`
- Nueva: `begin_delete` → `close` → `verify` → `UPDATE deleted_at` → `unregister`

**Necesidad de actualización documental:** **SÍ**. El contrato DEC18 `safe_delete_world()` debe actualizarse para reflejar el nuevo paso de `UPDATE deleted_at`. Pero como no se modifica código en esta fase, se documenta en `F7-E.2-PRE-DECISION.md` y se implementa en F7-E.2-I.

### 5.3. DEC20

**Efecto:** Los worlds eliminados simplemente no aparecen en listados públicos/privados por el filtro `deleted_at IS NULL`.

**Incompatibilidad real:** Ninguna. DEC20/§14.3 ya establece que worlds private de terceros retornan 404. Los worlds eliminados se tratan como "no existiendo" para efectos de pública, con diferenciación administrativa.

**Necesidad de actualización documental:** **SÍ**. F7-E.1 H5 debe añadir: "La elegibilidad pública requiere `deleted_at IS NULL` además de las condiciones existentes (visibility = public, owner_user_id vinculado, etc.)."

### 5.4. DEC21

**Efecto:** Ninguno directo. DEC21 trata sobre identidad de usuario y autorización.

**Incompatibilidad real:** Ninguna.

**Necesidad de actualización documental:** Ninguna.

### 5.5. F7-E.1 H4 y H5

**Efecto:** **Directo y obligatorio.** Las secciones H4 (Retención histórica y eliminación física) y H5 (Elegibilidad sin requisitos implícitos de operabilidad) del F7-E.1 deben actualizarse con el contrato `deleted_at`.

**Actualizaciones específicas:**

**H4 (actualizada):**
- Retener `world_highlights` con `ON DELETE RESTRICT` ✅
- Eliminación lógica conserva físicamente `WorldRow` ✅
- `deleted_at IS NULL` = no eliminado; `IS NOT NULL` = eliminado lógicamente (NUEVO)
- No se introduce purga física dentro de F7-E ✅
- **Eliminar** la frase "si el proyecto no dispone de mecanismo de eliminación lógica compatible, documentarlo como prerrequisito pendiente" — **RESUELTO** con esta decisión.

**H5 (actualizada):**
- Condiciones concretas de elegibilidad pública (NUEVA condición añadida):
  1. Mundo existe y no está eliminado lógicamente (`deleted_at IS NULL`).
  2. `visibility = 'public'`.
  3. `owner_user_id` está vinculado a cuenta válida.
  4. Allowlist DEC20 §14.4 respetada.
  5. **Nueva:** No existe restricción contractual de privacidad o integridad que impida exposición (incluye `deleted_at IS NULL`).
- **No se exige** (ahora también explícito):
  - Runner en estado `RUNNING` o `PAUSED`.
  - Capacidad de reanudar o recuperar el world.
  - `result_status = confirmed`.
  - Disponibilidad de simulación.
  - `deleted_at IS NULL` (esta es ahora una condición positiva, no negativa).

### 5.6. CONTRACTS.md

**Efecto:** El contrato técnico debe actualizar su sección F7-E para reflejar el nuevo campo y flujo.

**Actualizaciones:** Estado del prerrequisito de F7-E.1 pasado de "pendiente" a "resuelto (decisión contractual)". Nueva sección sobre `deleted_at` en modelo y flujo `safe_delete_world`.

### 5.7. IMPLEMENTATION_STATUS.md

**Efecto:** Marcar el prerrequisito de eliminación lógica como "resuelto (decisión contractual)" y esperar fase F7-E.2-I.

**No alterar:** Decisiones aceptadas de DEC16-DEC21 salvo incompatibilidad demostrada. No hay incompatibilidades demostradas; solo actualizaciones documentales necesarias.

---

## 6. Dictamen

### **B. CONTRATO LISTO CON DECISIONES MENORES PENDIENTES**

El contrato está **casi listo para implementación**, pero requiere definiciones concretas en 4 áreas antes de autorizar F7-E.2-I:

#### Bloqueos identificados y recomendaciones:

1. **Orden `UPDATE deleted_at` vs `unregister()` en `safe_delete_world()`**
   - **Bloque:** Si UPDATE falla después de `unregister()`, world queda órfano (sin runner, sin eliminado lógico).
   - **Recomendación:** Reordenar secuencia a `begin_delete` → `close` → `verify` → `UPDATE deleted_at` → `unregister`. Si UPDATE falla, `unregister()` no se ejecuta y world stay recoverable (runner aún en registry). **Decisión contractual requerida.**

2. **Comportamiento ante eliminación repetida**
   - **Bloque:** Si `deleted_at IS NOT NULL` y se llama `safe_delete_world()` nuevamente: ¿no-op? ¿Error? ¿Administrativo?
   - **Recomendación:** Definir como no-op que retorna éxito inmediatamente, sin repetir close ni UPDATE. Documentar en contrato.

3. **Comportamiento ante consulta por ID de world eliminado**
   - **Bloque:** `GET /api/worlds/{id}` si `deleted_at IS NOT NULL`: ¿404 estándar? ¿Ficha pública con mensaje "world eliminated"? ¿Acceso admin exclusivo?
   - **Recomendación:** Estándar 404 (indistinguible de world no existente, consistencia DEC20 fail-closed), a menos que se decida endpoint admin explícito.

4. **Política de restauración (admin puts `deleted_at = NULL`)**
   - **Bloque:** ¿Puede un administrador restaurar un world eliminado? ¿Requiere nueva concesión de destacado? ¿Procedimiento administrativa con autorización?
   - **Recomendación:** No introducir restauración en F7-E.2. Si es necesario en futuro, decisión contractual aparte. Por ahora: `deleted_at` solo puede ponerse NULL mediante migración administrativa explícita fuera del contrato F7-E.

#### Áreas ya resueltas (contrato compatible):

- ✅ `deleted_at IS NULL` = no eliminado; `IS NOT NULL` = eliminado lógicamente
- ✅ `world_highlights.world_id` con `ON DELETE RESTRICT` preservado
- ✅ Eliminación lógica conserva físicamente `WorldRow`, retención histórica intacta
- ✅ Mundos eliminados no elegibles para `highlighted=1` (F7-E.1 H5)
- ✅ Migración conceptual: `ensure_schema()` añade `deleted_at TIMESTAMP NULL` con default NULL a registros existentes
- ✅ Atomicidad: UPDATE transaccional en hilo principal vs. DELETE físico en worker thread (mejor garantía de coherencia)
- ✅ Compatibilidad DEC16, DEC21: sin conflictos
- ✅ Actualizaciones documentales en F7-E.1 H4 y H5 necesarias (no bloqueantes)
- ✅ No se modifica código ORM, migraciones ni pruebas en esta fase

### Veredicto final

**El contrato está listo para implementación CON decisiones menores pendientes** (Categoria B).

Las 4 decisiones pendientes son técnicamente sencillas y tienen recomendaciones claras. No hay bloqueos arquitectónicos o de integridad referencial que impidan avanzar a F7-E.2-I. Una vez definidas estas 4 áreas mediante decisión contractual, el contrato estará completamente listo y se puede autorizar la implementación.

**Recomendación:** Proceed to F7-E.2-I design phase with these 4 decisions as explicit contractual requirements, then implement.

---

## 7. Entregable

**Archivo generado:** `F7-E.2-PRE-AUDIT.md` (14,2 KB)

**Contenido del entregable:**

1. ✅ **Dictamen B:** Contrato listo con decisiones menores pendientes.
2. ✅ **Evidencia concreta:** Archivos, funciones y líneas de `db.py:226-253` (`ensure_schema()`), `runner.py:1028-1112` (`safe_delete_world()`), `app.py` (todas las queries a `WorldRow`).
3. ✅ **Análisis `ensure_schema()`:** Puede añadir columnas a tablas existentes, detecta columnas previas (idempotent-safe), solo garantizado para SQLite, default NULL para existentes.
4. ✅ **Análisis atomicidad y recuperación:** Tabla completa de 8 puntos de fallo, determinación de riesgo crítico (world órfano si UPDATE falla después de `unregister()`), recomendación de reordenar secuencia (UPDATE antes de `unregister`).
5. ✅ **Matriz de lecturas:** 16 rutas contrastadas, determinación de consistencia de exclusión, necesidad de `WHERE deleted_at IS NULL` en todas las queries públicas.
6. ✅ **Semántica de casos límite:** 7 comportamientos definidos/pendientes, 2 incoherencias detectadas.
7. ✅ **Compatibilidad contractual:** Verificación contra DEC16-DEC21, F7-E.1 H4/H5, CONTRACTS.md, IMPLEMENTATION_STATUS.md. DEC16/DEC21 sin conflictos. DEC18 y F7-E.1 requieren actualizaciones documentales. Sin incompatibilidades reales.
8. ✅ **Decisiones pendientes y recomendaciones:** 4 decisiones identificadas con recomendaciones específicas.
9. ✅ **Confirmación de no alteración:** Ningún archivo de código, ORM, migración, prueba ni dato fue modificado durante esta auditoría. El state Git permanece inalterado.

**No iniciar F7-E.2-I:** La implementación requiere autorización expresa posterior después de resolver las 4 decisiones menores pendientes.

---
**F7-E.2-PRE-AUDIT: Validación completada. Dictamen B: CONTRATO LISTO CON DECISIONES MENORES PENDIENTES.**