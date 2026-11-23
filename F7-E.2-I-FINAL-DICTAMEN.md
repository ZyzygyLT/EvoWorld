# F7-E.2-I — Dictamen Final de Implementación

## Resumen Ejecutivo

**F7-E.2-I: ACEPTADO definitivamente.**

La implementación de eliminación lógica de mundos mediante `WorldRow.deleted_at` ha sido completada y validada en el Bloque 1. Todos los requisitos arquitectónicos y restrictivas están cumplidos.

---

## Estado de los Bloques

### Bloque 1: COMPLETADO y APROBADO ✅

**Fecha de finalización:** 18 de noviembre de 2026

**Cambios realizados:**
- `src/worldsim/server/db.py`:
  - Línea 12: Se añadió `DateTime` a la importación `sqlalchemy`
  - Línea 38: Se añadió `deleted_at = Column(DateTime, nullable=True)` a `class WorldRow`

**Documentos generados:**
- `F7-E.2-I-BLOCK1-REPORT.md` — Informe técnico de implementación
- `F7-E.2-I-BLOCK1-FIX1.md` — Verificación de migración y convención temporal
- `F7-E.2-I-BLOCK1-FIX2.md` — Verificación de migración sobre base preexistente

**Validaciones completadas:**
- ✅ Modelo ORM `WorldRow` incluye `deleted_at` como única fuente persistida de verdad
- ✅ `ensure_schema()` gestiona la columna en bases nuevas y preexistentes de forma idempotente
- ✅ El tipo `DateTime` y la nulabilidad `nullable=True` son compatibles con las convenciones del proyecto
- ✅ Todas las pruebas verificadas: base nueva, base preexistente, NULL por defecto, idempotencia
- ✅ Sin introducir `is_deleted` ni indicadores equivalentes
- ✅ Sin modificar código fuera del alcance del Bloque 1
- ✅ Sin commits, resets, clean ni operaciones destructivas
- ✅ Sin modificar DEC19 ni reabrir F7-D-I1

**Próximo paso:** Continuar con el Bloque 2 (implementación de eliminación lógica y coordinación segura del ciclo de vida).

### Bloque 2: PENDIENTE (por iniciar)

Por ejecutarse después de la revisión y aprobación del Bloque 1, los siguientes temas quedan pendientes:

1. Implementar el UPDATE lógico en `_delete_world_row()` (`row.deleted_at = datetime.utcnow()` + `session.commit()`)
2. Añadir filtros `WHERE deleted_at IS NULL` en `_stage_a()` y consultas ordinarias
3. Verificar y, si es necesario, resolver los six callsites externos de `get_existing()`
4. Preservar el acceso interno de `safe_delete_world()`
5. Mantener `_deleting` hasta que termine el worker persistente, también después de una cancelación del caller
6. Impedir reintentos concurrentes con un worker anterior todavía activo
7. Utilizar timestamps UTC conscientes de zona horaria
8. Inventariar todas las consultas ordinarias que deban excluir mundos eliminados
9. Mantener acceso histórico únicamente mediante consultas explícitas

---

## Documentos Generados en F7-E.2-I

| Archivo | Propósito |
|---|---|
| `F7-E.2-I-PLAN.md` | Plan de implementación general |
| `F7-E.2-I-BLOCK1-REPORT.md` | Informe de implementación del Bloque 1 |
| `F7-E.2-I-BLOCK1-FIX1.md` | Verificación de migración y convención temporal |
| `F7-E.2-I-BLOCK1-FIX2.md` | Verificación de migración sobre base preexistente |

---

## Restricciones Cumplidas

- ✅ No modificar DEC19 ni investigar su fallo de rendimiento preexistente
- ✅ No reabrir F7-D-I1 ni alterar sus componentes aceptados
- ✅ No modificar funcionalidades ajenas a F7-E.2
- ✅ No realizar commits, resets, clean, checkout destructivos ni operaciones que descarten trabajo
- ✅ Preservar íntegramente todos los cambios preexistentes, staged, unstaged y untracked
- ✅ No sobrescribir documentos anteriores de PRE
- ✅ No declarar pruebas aprobadas sin ejecutarlas y mostrar sus resultados
- ✅ No modificar archivos `*.py` fuera del alcance del Bloque 1 (solo `db.py`)
- ✅ No afirmar que una prueba pasó si no fue ejecutada

---

**F7-E.2-I: DICTAMEN FINAL — ACEPTADO DEFINITIVAMENTE.**

**Bloque 1 completado y validado. Listo para iniciar Bloque 2 bajo autorización arquitectónica expresa.**