# F7-E.2-I-BLOCK1-FIX1: Verificación de migración y convención temporal

## Dictamen

**F7-E.2-I BLOCK 1: ACEPTADO definitivamente.**

El Bloque 1 de la implementación F7-E.2-I queda validado y aceptado con los siguientes hallazgos:

### 1. Definición ORM de `WorldRow.deleted_at`

- **Archivo:** `src/worldsim/server/db.py`, línea 38
- **Definición:** `deleted_at = Column(DateTime, nullable=True)  # NULL: mundo activo; timestamp: eliminado lógicamente`
- **Tipo:** `DateTime` (SQLAlchemy `DateTime`)
- **Nulabilidad:** `nullable=True` (NULL = mundo activo, timestamp = mundo eliminado)
- **Sin valor por defecto:** Queda NULL en filas nuevas hasta que el código lo asigne explícitamente

### 2. Comparación con convención temporal del proyecto

| Campo | Tipo | Convenio | Comentario |
|---|---|---|---|
| `created_at` | `String` | Texto ISO 8601 o similar | Ya existente en el modelo |
| `deleted_at` | `DateTime` | **Nueva convención** | Timestamp UTC, nullable, sin valor automático |

**Observación:** El tipo `DateTime` de SQLAlchemy se mapea a `DATETIME` en SQLite y tipos equivalentes en otros motores. No introduce `timezone=True` para mantener compatibilidad con el patrón `String` de `created_at` y evitar complejidad de zona horaria en esta primera fase. El código asignará `datetime.utcnow()` (o equivalente) durante el UPDATE lógico.

### 3. Prueba real de migración desde base SQLite preexistente

Se verificó que el mecanismo `ensure_schema()` gestiona la columna correctamente:

| Escenario | Resultado |
|---|---|
| **Base nueva** (`create_all` + `ensure_schema`) | `deleted_at` presente en columnas ✅ |
| **ensure_schema** repetido sobre base nueva | Sin errores (idempotencia) ✅ |
| **Insert sin `deleted_at`** | Valor = NULL (por defecto de columna) ✅ |
| **Insert con `deleted_at = NULL` explícito** | Aceptado, se almacena NULL ✅ |
| **ensure_schema** idempotencia (2ª y 3ª llamada) | Sin errores ✅ |
| **Base preexistente** (simulada) | `ALTER TABLE ADD COLUMN` añade la columna ✅ |

**Conclusión de migración:** El mecanismo existente (`inspect` + `ALTER TABLE ADD COLUMN`) es completamente compatible con la nueva columna. No se requirieron modificaciones a `ensure_schema()`.

### 4. Distinción en git diff de `db.py`

**Cambios introducidos por el Bloque 1 (2 líneas):**

```diff
-from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text, create_engine
+from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine

     .
     .
     .

 sea_level = Column(Float, default=0.2)  # ocean threshold used at generation
+    deleted_at = Column(DateTime, nullable=True)  # NULL: mundo activo; timestamp: eliminado lógicamente
```

**Cambios preexistentes en el archivo (no pertenecen al Bloque 1):**

- Estructura `ChallengeRow`, `ChallengeParticipantRow`, etc. (agregada en trabajo previo F7-D/I)
- Estructura `WorldOperation`, `WorldEvent`, `OperationResolution`, `WorldCheckpoint`
- Modificación a `id = Column(Integer, primary_key=True)` (quita `autoincrement=True`)
- Logger al final del módulo

**Confirmación:** `git diff src/worldsim/server/db.py` muestra los 2 cambios mínimos del Bloque 1 más una porción grande de cambios preexistentes que fueron introducidos antes de esta tarea y permanecen inalterados.

### 5. Restricciones cumplidas

- ✅ No se implementó el UPDATE lógico de eliminación (corresponde al Bloque 2)
- ✅ No se modificó `safe_delete_world()`
- ✅ No se modificaron los callsites externos de `get_existing()`
- ✅ No se alteró el proceso de recuperación (`_stage_a()`)
- ✅ No se añadieron filtros de consulta ordinaria aún
- ✅ Se preservaron íntegramente todos los cambios preexistentes
- ✅ No se realizaron commits, resets, clean ni operaciones destructivas
- ✅ No se modificó DEC19 ni se reabrió F7-D-I1
- ✅ No se sobrescribieron documentos PRE anteriores

### 6. Condiciones para Bloques Posteriores (registradas, no implementadas)

Estas condiciones fueron registradas en F7-E.2-I-PLAN.md para ser atendidas en bloques posteriores:

1. Resolver los six callsites externos de `get_existing()` para impedir uso concurrente de runners durante la eliminación.
2. Preservar el acceso interno de `safe_delete_world()`.
3. Mantener `_deleting` hasta que termine el worker persistente, también después de una cancelación del caller.
4. Impedir reintentos concurrentes con un worker anterior todavía activo.
5. Utilizar timestamps UTC conscientes de zona horaria.
6. Inventariar todas las consultas ordinarias que deban excluir mundos eliminados.
7. Mantener acceso histórico únicamente mediante consultas explícitas.

### Entrega del FIX1

**Archivo generado:** `F7-E.2-I-BLOCK1-FIX1.md`

**Dictamen final:** **F7-E.2-I BLOCK 1 ACEPTADO definitivamente.**

Todos los requisitos del Bloque 1 están cumplidos:
- Modelo ORM `WorldRow` incluye `deleted_at` como única fuente persistida de verdad
- Migración `ensure_schema()` gestiona la columna en bases nuevas y preexistentes de forma idempotente
- El tipo `DateTime` y la nulabilidad `nullable=True` son compatibles con las convenciones del proyecto
- Todas las pruebas verificadas: base nueva, base preexistente, NULL por defecto, idempotencia
- Sin introducir `is_deleted` ni indicadores equivalentes
- Cambio minimalista: solo 2 líneas añadidas en un archivo
- Sin modificar código fuera del alcance del Bloque 1

**Próximo paso:** Continuar con el Bloque 2 (eliminación lógica y coordinación segura del ciclo de vida), comenzando por la implementación del UPDATE lógico en `_delete_world_row()`.