# F7-E.5 — Auditoría final y cierre documental de F7-E

**Fecha:** 2026-11-24  
**Rama auditada:** `feature/f7-e4-implementation`  
**HEAD base auditado:** `a0133cd76e7f3e3d7bbdadae54000a3426785826`  
**Alcance autorizado:** auditoría y documentación exclusivamente. Sin cambios de código, pruebas, migraciones, esquema ni bases de datos.

## 1. Dictamen ejecutivo

F7-E.1 a F7-E.4 cuentan con contratos definidos y evidencia de implementación en la rama auditada. F7-E.2-I y F7-E.4 tienen aceptación explícita registrada en el historial de trabajo; F7-E.3 cuenta con contrato aceptado y evidencia de servicio, rutas y pruebas. No se encontró una acta independiente de aceptación final de F7-E.3.

**Conclusión:** no se identifica una funcionalidad adicional de destacados pendiente que justifique una F7-E.5 funcional. F7-E.5 queda definida como cierre documental. La aceptación global de F7-E debe conservar la distinción entre implementación demostrada, pruebas focalizadas y regresión global.

## 2. Matriz de subetapas

| Subetapa | Contrato | Evidencia en rama | Verificación disponible | Dictamen |
|---|---|---|---|---|
| F7-E.1 — ciclo de vida e historial | Aceptado | `WorldHighlight`, esquema, índice único parcial, servicio de concesión/retiro y consulta administrativa | `tests/test_world_highlight_service.py`, `tests/test_world_highlights_schema.py`, `tests/test_world_highlight_api.py` | Implementada; preservar historial |
| F7-E.2-I — eliminación lógica | Aceptado | `deleted_at`, exclusión de consultas normales, protección de retención mediante FK RESTRICT | Acta independiente: 93 passed, 1 xpassed, 1 warning, 0 failed | Implementada y aceptada; no reabrir |
| F7-E.3 — autoridad y elegibilidad | Aceptado | Endpoints admin, verificación de `is_admin`, propietario distinto, visibilidad pública, estado, no eliminado y checksum read-only | Incluida en pruebas de API/servicio; 19 pruebas focalizadas conjuntas pasaron | Implementación evidenciada; falta acta independiente de aceptación |
| F7-E.4 — galería destacada | Aceptado | Booleano derivado, orden compuesto, cursor opaco/versionado, allowlist pública y ausencia de filtro `highlighted=1` | Ejecución local informada: 19 passed, 64 deselected, 0 failed, 1 warning | Aceptada definitivamente por el usuario |

## 3. Hallazgos técnicos

### 3.1 Persistencia y retención

- El destacado es un registro histórico en `world_highlights`, no una columna booleana persistida en `WorldRow`.
- El índice único parcial limita a un registro activo por mundo.
- La relación con el mundo conserva la política de retención `ON DELETE RESTRICT`.
- Retirar o expirar cierra el registro activo sin borrar el historial.
- La concesión posterior a una expiración desactiva el registro vencido anterior y crea el nuevo dentro de la misma transacción.

### 3.2 Autoridad y elegibilidad

La ruta administrativa verifica la cuenta administradora. El servicio vuelve a comprobar la elegibilidad al conceder:

- mundo público, no eliminado y con propietario;
- estado `running` o `paused`;
- propietario existente y distinto del administrador;
- checkpoint íntegro entre los candidatos `is_valid=true`, recorridos por `sequence DESC`.

La verificación de checksum es de solo lectura. No debe sustituirse por `CheckpointRepository.get_latest_valid_checkpoint()` ni por `RecoveryManager.recover()`.

### 3.3 Galería y privacidad

- La galería pública consulta únicamente mundos públicos y no eliminados.
- `is_highlighted` es derivado y solo añade un booleano a proyecciones públicas.
- Orden: `is_highlighted DESC, id DESC`.
- El cursor es opaco, versionado y ligado a `mine`, `search`, `mode` y `limit`.
- No existe filtro `highlighted=1`; los mundos no destacados siguen apareciendo después de los destacados.
- La ficha pública HTTP/WS mantiene la frontera de privacidad; no habilita suscripciones de visitantes ni expone estado vivo adicional.
- La vista privada `mine=1` no recibe el booleano por este contrato.

## 4. Discrepancias documentales corregidas en F7-E.5

1. Se actualiza el estado de DEC-F7E-01 para reflejar aceptación y evidencia focalizada.
2. Se aclara que la sección histórica §15 de F7-B es la línea base y que §18 gobierna la extensión de destacados.
3. Se corrige la descripción del estado de DEC21 para no afirmar que los mecanismos de destacados aún no existen.
4. Se conserva `F7-E-R0-DEF.IN.md` como antecedente histórico. Sus propuestas preliminares —por ejemplo, columna `highlighted`, filtro `highlighted=1` o independencia de visibilidad— no prevalecen sobre las decisiones posteriores.

## 5. Evidencia de pruebas y límites

La verificación focalizada informada por el usuario en el worktree local fue:

```
19 passed, 64 deselected, 1 warning
```

La advertencia corresponde a la deprecación de la integración httpx/Starlette TestClient; no fue un fallo.

Esta auditoría no ejecutó las pruebas en GitHub ni accedió a worktrees o bases de datos locales. La regresión global previa de la rama reportó fallos en otras áreas (incluidos F7-D/B35 y contratos históricos); no se atribuyen a F7-E sin evidencia específica. Por ello, el resultado focalizado no debe presentarse como una regresión global limpia.

No se hizo una nueva verificación de PostgreSQL en esta auditoría. La existencia de código compatible no sustituye evidencia de ejecución contra ese dialecto.

## 6. Elementos que permanecen fuera de alcance

- Rankings globales y F7-D Nivel 2.
- Filtros nuevos de destacados, incluido `highlighted=1`.
- Cambios de frontend no definidos en el contrato.
- Restauración de mundos eliminados o purga del historial.
- Cambios en DEC19, motor de simulación, runners, checkpoints o recuperación.
- Merge, PR o cambios en ramas distintas de `feature/f7-e4-implementation`.

## 7. Resultado de F7-E.5

**F7-E.5 — cierre documental: COMPLETADO.**

No se detecta una nueva funcionalidad de F7-E pendiente. La única reserva administrativa es que F7-E.3 no tiene un acta independiente de aceptación final identificada en los documentos inspeccionados. La evidencia de implementación y las pruebas focalizadas sí existen.

Este informe no sustituye la aceptación explícita del usuario sobre el cierre global de F7-E. No se reabren F7-E.1, F7-E.2-I ni F7-E.4.
