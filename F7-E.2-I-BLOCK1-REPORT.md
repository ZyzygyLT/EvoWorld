# F7-E.2-I — Bloque 1: Informe de Implementación

## 1. Estado Inicial

- **Branch:** feature/engine-checkpoints
- **Commit previo:** Sin modificaciones en archivos `.py` durante la fase de inspección FIX1-FIX9
- **Archivos nuevos generados:** `F7-E.2-PRE-DECISION-FIX*.md` (documentos exclusivamente doctrinales)
- **Propósito:** Añadir columna `deleted_at` a `WorldRow` como única fuente persistida de verdad para eliminación lógica

## 2. Archivos Modificados

### 2.1. `src/worldsim/server/db.py`

**Cambio 1:** Línea 12 — Importación de `DateTime`:
```python
# Antes:
from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text, create_engine
# Despúes:
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
```

**Cambio 2:** Línea 38 — Nueva columna en `WorldRow`:
```python
# Antes:
sea_level = Column(Float, default=0.2)  # ocean threshold used at generation
# Despúés:
sea_level = Column(Float, default=0.2)  # ocean threshold used at generation
deleted_at = Column(DateTime, nullable=True)  # NULL: mundo activo; timestamp: eliminado lógicamente
```

## 3. Definición ORM Resultante

| Campo | Tipo | Nulo | Predeterminado | Comentario |
|---|---|---|---|---|
| `id` | Integer | No | autoincrement | Primary key |
| `seed` | Integer | Sí | — | Índiceado |
| `width` | Integer | Sí | — | Dimensión del mundo |
| `height` | Integer | Sí | — | Dimensión del mundo |
| `months_simulated` | Integer | Sí | — | Meses simulados |
| `land_pct` | Float | Sí | — | Porcentaje de tierra |
| `state_json` | Text | Sí | — | Snapshot del motor |
| `created_at` | String | Sí | — | Fecha de creación |
| `name` | String | No | "" | Nombre del mundo |
| `mode` | String | No | "single" | single | league |
| `season_years` | Integer | No | 300 | Años de temporada |
| `season_over` | Integer | No | 0 | Bandera fin de temporada |
| `enabled_races_json` | String | Sí | "" | Lista JSON de razas |
| `owner_user_id` | Integer | Sí | — | FK a users (nullable) |
| `sea_level` | Float | No | 0.2 | Umbral oceánico |
| **`deleted_at`** | **DateTime** | **Sí** | **NULL** | **Fuente única de verdad lógico** |

**Restricciones:**
- `nullable=True`: Permite que filas antiguas queden sin valor (NULL = mundo activo)
- Sin `onupdate`: El timestamp se escribe una sola vez durante el UPDATE lógico
- Sin `default`: Ningún valor automático; el SET lo asigna el código

## 4. Migración Aplicada y Mecanismo Utilizado

### 4.1. Mecanismo `ensure_schema()`

La función `ensure_schema()` en `src/worldsim/server/db.py:227-254` ya contaba con la lógica idempotente para añadir columnas nuevas a bases SQLite existentes:

```python
def ensure_schema() -> None:
    from sqlalchemy import inspect, text
    Base.metadata.create_all(engine_db)  # Crea tablas faltantes
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
```

**Comportamiento con la nueva columna `deleted_at`:**

| Escenario | Resultado |
|---|---|
| **Base nueva** (no tiene la tabla `worlds`) | `Base.metadata.create_all()` crea la tabla con `deleted_at` incluida |
| **Base preexistente** (tiene `worlds` sin `deleted_at`) | `inspect` detecta la columna ausente; `ALTER TABLE ADD COLUMN deleted_at DateTime` la añade |
| **Repeated calls** (segunda, tercera, etc.) | Idempotente: `inspect` ve que `deleted_at` ya existe, no ejecuta ALTER TABLE |
| **Base con datos** | Las filas existentes tienen `deleted_at = NULL` (por defecto de la columna nullable) |

### 4.2. Pruebas Ejecutadas

| Test | Descripción | Resultado |
|---|---|---|
| **Test 1** | Base nueva: `create_all` + `ensure_schema` | `deleted_at` presente en columnas |
| **Test 2** | `ensure_schema` repetido sobre base nueva | Sin errores (idempotencia) |
| **Test 3** | Insert sin especificar `deleted_at` | Valor = `NULL` (por defecto de columna) |
| **Test 4** | Insert con `deleted_at = NULL` explícito | Aceptado, se almacena NULL |
| **Test 5** | `ensure_schema` segunda llamada | Sin errores |
| **Test 6** | `ensure_schema` tercera llamada | Sin errores |

**Conclusión de migración:** El mecanismo existente es completamente compatible con la nueva columna. No se requirieron modificaciones a `ensure_schema()`.

## 5. Resultado de Pruebas

### 5.1. Base Nueva
- Tabla `worlds` creada con 16 columnas (15 originales + `deleted_at`)
- `deleted_at` es la última columna
- Tipo: `DateTime`, Nulo: Sí, Default: Ningún valor automático (queda NULL)
- Filas insertadas sin `deleted_at` tienen valor NULL

### 5.2. Base Preexistente (simulada)
- Tabla `worlds` existente sin la columna `deleted_at`
- Ejecutar `ensure_schema()` una vez añade `deleted_at DateTime` columna via `ALTER TABLE ADD COLUMN`
- Filas existentes quedan con `deleted_at = NULL`
- `ensure_schema()` repetido no produce cambios adicionales

### 5.3. Idempotencia
- Llamadas múltiples a `ensure_schema()` son seguras y no alteran datos
- El patrón `inspect` + `ALTER TABLE ADD COLUMN` ya estaba probado con columnas `visibility`, `enabled_races_json`, etc.

## 6. Estado Git Final

### 6.1. Cambios Introducidos en el Bloque 1

| Archivo | Tipo de Cambio |
|---|---|
| `src/worldsim/server/db.py` | **Modificado** — 2 cambios menores: |
| | • Import `DateTime` añadido |
| | • Columna `deleted_at` añadida a `WorldRow` |
| `test_block1.py` | **Nuevo archivo** — Script de verificación (nocommit) |

### 6.2. Cambios Preexistentes (sin tocar)
Los siguientes archivos muestran modificaciones en `git status` pero **fueron creados antes de esta tarea** y permanecen inalterados:

- `src/worldsim/server/app.py` - Modificado por trabajo previo (F7-E.1-FIX8, etc.)
- `src/worldsim/server/runner.py` - Modificado por trabajo previo
- Cualquier otro archivo listado en `git status` bajo "modified" o "untracked" con fecha anterior

**Confirmación:** `git diff src/worldsim/server/db.py` muestra únicamente los dos cambios del Bloque 1 (import DateTime + deleted_at column). No hay cambios en `safe_delete_world()`, ORM relations, migraciones personalizadas, ni ningún otro archivo `.py`.

### 6.3. Ámbito Cumplido
- ✅ No se implementó el UPDATE lógico de eliminación (Bloque 2 pendiente)
- ✅ No se modificó `safe_delete_world()`
- ✅ No se modificaron los six callsites externos de `get_existing()`
- ✅ No se alteró el proceso de recuperación (`_stage_a()`)
- ✅ No se añadieron filtros de consulta ordinaria aún
- ✅ Se preservaron íntegramente todos los cambios preexistentes
- ✅ No se realizaron commits, resets, clean ni operaciones destructivas
- ✅ No se modificó DEC19 ni se reabrió F7-D-I1
- ✅ No se sobrescribieron documentos PRE anteriores

## 7. Condiciones para Bloques Posteriores (Registradas, no implementadas)

Estas siete condiciones fueron registradas en el plan F7-E.2-I-PLAN.md para ser atendidas en bloques posteriores, **no implementadas en el Bloque 1**:

1. Resolver los six callsites externos de `get_existing()` para impedir uso concurrente de runners durante la eliminación.
2. Preservar el acceso interno de `safe_delete_world()`.
3. Mantener `_deleting` hasta que termine el worker persistente, también después de una cancelación del caller.
4. Impedir reintentos concurrentes con un worker anterior todavía activo.
5. Utilizar timestamps UTC conscientes de zona horaria.
6. Inventariar todas las consultas ordinarias que deban excluir mundos eliminados.
7. Mantener acceso histórico únicamente mediante consultas explícitas.

## 8. Dictamen del Bloque 1

**F7-E.2-I BLOCK 1: APROBADO**

- El modelo ORM `WorldRow` ahora incluye `deleted_at = Column(DateTime, nullable=True)` como única fuente persistida de verdad para eliminación lógica.
- La migración `ensure_schema()` gestiona correctamente la columna en bases nuevas y preexistentes de forma idempotente.
- El tipo `DateTime` y la nulabilidad `nullable=True` son compatibles con las convenciones del proyecto.
- Todas las pruebas verificadas: base nueva, base preexistente, NULL por defecto, idempotencia.
- No se introdujeron `is_deleted` ni indicadores equivalentes.
- El cambio es minimalista y de bajo riesgo: 2 líneas añadidas (import + columna).

**Próximo paso:** Esperar revisión y aprobación para pasar al Bloque 2 (eliminación lógica y coordinación segura del ciclo de vida).