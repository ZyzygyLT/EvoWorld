# F7-E.5-PRE — Reconciliación del alcance R0 y dictamen previo

**Fecha:** 2026-11-24  
**Rama:** `feature/f7-e4-implementation`  
**Tipo:** reconciliación contractual/documental exclusivamente.  
**Autorización:** F7-E.5-PRE autorizada por el usuario.  
**Límites:** sin cambios de código, pruebas, migraciones, esquema, bases de datos, configuración, PR ni merge.

## 1. Dictamen ejecutivo

La propuesta R0 original está localizada en la raíz del repositorio: `F7-E-R0-DEF.IN.md`. Su §7 proponía una secuencia preliminar F7-E.1–F7-E.6. Esa numeración no coincide con la descomposición que posteriormente se definió, implementó y aceptó.

La revisión de R0 contra los contratos vigentes confirma que **el alcance funcional que R0 asignaba a F7-E.5 (endpoints y filtros de destacados) ya está resuelto por F7-E.1 y F7-E.4**. La integración de la galería fue definida en DEC-F7E-01 / CONTRACTS.md §18 y aceptada definitivamente el 2026-11-24.

Por tanto, no existe una nueva funcionalidad de endpoints/filtros que deba implementarse como F7-E.5. El nombre F7-E.5 se conserva para el cierre documental de F7-E, como ya concluye el informe `docs/implementation/reports/F7-E.5-FINAL-AUDIT-2026-11-24.md`.

## 2. Fuentes contrastadas

- `F7-E-R0-DEF.IN.md`, raíz del repositorio, especialmente §§1–7.
- `docs/architecture/CONTRACTS.md`, §18 (galería pública con destacados).
- `docs/architecture/DECISIONS.md`, DEC-F7E-01 y DEC21.
- `docs/implementation/reports/F7-E.3-CONTRACT-DRAFT-2026-11-24.md` (contrato consolidado de autoridad/elegibilidad).
- `docs/implementation/reports/F7-E.4-CONTRACT-2026-11-24.md`.
- `docs/implementation/reports/F7-E.5-FINAL-AUDIT-2026-11-24.md`.

R0 se conserva como antecedente histórico. Sus propuestas preliminares no prevalecen sobre los contratos y decisiones posteriores.

## 3. Mapeo de la descomposición R0 al alcance vigente

| Propuesta preliminar R0 (§7) | Resolución vigente | Estado |
|---|---|---|
| F7-E.1 — identidad competitiva | DEC21 sigue gobernando identidad vinculada a cuenta y excluye `user_token` como identidad competitiva. La administración de destacados usa la cuenta autenticada y `is_admin`; no se crean rankings globales. | Resuelto en contratos existentes; no requiere subetapa nueva de destacados. |
| F7-E.2 — definición de `highlighted` | F7-E.1 define `world_highlights`, ciclo de vida, vigencia e historial. El estado público no es una columna booleana persistida en `WorldRow`. | Implementada y aceptada; preservar historial. |
| F7-E.3 — autoridad y elegibilidad | F7-E.3 define autoridad administrativa, elegibilidad pública/no eliminada/con propietario, estado permitido y checkpoint íntegro verificado de forma no destructiva. | Implementada y aceptada por el usuario el 2026-11-24. |
| F7-E.4 — privacidad y galería | F7-E.4 integra el booleano derivado `is_highlighted` en proyecciones públicas, preservando DEC20/DEC21 y los límites HTTP/WS. | Implementada y aceptada definitivamente. |
| F7-E.5 — endpoints y filtros | Los endpoints administrativos pertenecen al ciclo de vida F7-E.1/F7-E.3. La galería pública prioriza destacados automáticamente mediante DEC-F7E-01. No se añade `highlighted=1`. | Resuelto; no queda trabajo funcional independiente. |
| F7-E.6 — criterios finales | La auditoría de cierre F7-E.5 consolida evidencia, límites y estado de las subetapas. | Cierre documental existente; sujeto a la reserva de evidencia indicada en §5. |

Este mapeo es una reconciliación de alcance, no una renumeración retroactiva de decisiones ni una reapertura de subetapas aceptadas.

## 4. Propuestas R0 que quedan expresamente sustituidas

Las siguientes ideas de R0 son preliminares y no deben convertirse en requisitos ni reintroducirse:

1. **Columna booleana `WorldRow.highlighted`:** sustituida por el historial `world_highlights` y el booleano público derivado `is_highlighted`.
2. **Filtro `GET /api/worlds?highlighted=1`:** rechazado en F7-E.4. La galería devuelve los mundos públicos que cumplen los filtros vigentes y prioriza automáticamente los destacados.
3. **Destacados independientes de la visibilidad, incluidos mundos privados:** sustituido por la elegibilidad F7-E.3 y la proyección pública F7-E.4. Un mundo privado no puede recibir ni mostrar un destacado público.
4. **Selección automática por población, resultados de desafíos o umbrales:** no forma parte del contrato aceptado de destacados.
5. **Metadatos públicos como `highlighted_at` o `highlighted_position`:** no autorizados. La proyección pública añade únicamente `is_highlighted`.
6. **Eliminar el registro destacado al eliminar el mundo:** incompatible con la retención histórica y `ON DELETE RESTRICT` de F7-E.1/F7-E.2-I.
7. **Desempates basados en métricas competitivas:** sustituidos por el orden contractual `is_highlighted DESC, id DESC`; no introduce rankings globales.
8. **Nuevos permisos por estar destacado:** prohibidos. El destacado no cambia visibilidad, autorización, suscripciones ni acceso al estado vivo.

## 5. Aceptación, evidencia y límites

- F7-E.1: contrato de ciclo de vida e historial aceptado; conservar la retención.
- F7-E.2-I: eliminación lógica implementada y aceptada; no reabrir.
- F7-E.3: contrato e implementación aceptados explícitamente por el usuario el 2026-11-24. La matriz/informe de auditoría registra pruebas focalizadas de servicio/API/esquema y pruebas PostgreSQL P09/P10; esto no equivale a declarar limpia la regresión global.
- F7-E.4: contrato, implementación y aceptación definitiva registrados el 2026-11-24. Validación focalizada informada: 19 passed, 64 deselected, 1 warning, 0 failed.
- F7-E.5: el informe final existente concluye que no hay funcionalidad adicional pendiente y que corresponde el cierre documental.

La reserva del informe F7-E.5-FINAL-AUDIT que decía no haber encontrado un acta independiente de aceptación final de F7-E.3 queda superada por la aceptación explícita posterior del usuario registrada el 2026-11-24. Esto no altera los resultados de pruebas ni convierte la validación focalizada en una regresión global limpia.

## 6. Fuera de alcance y protecciones

- No implementar endpoints, filtros ni nuevos parámetros de destacados.
- No añadir columna `highlighted`, `highlighted_at` ni `highlighted_position`.
- No alterar DEC20/DEC21, F7-E.1, F7-E.2-I, F7-E.3 ni F7-E.4.
- No reabrir F7-D ni implementar su Nivel 2.
- No introducir rankings globales, automatización por métricas o resultados competitivos.
- No tocar DEC19, motor, runners, checkpoints, recuperación ni bases de datos.
- No borrar historial ni debilitar `ON DELETE RESTRICT`.
- No realizar cambios de código, pruebas, migraciones, PR ni merge como parte de este PRE.

## 7. Dictamen PRE y siguiente paso

**F7-E.5-PRE: RECONCILIACIÓN COMPLETADA.**

La F7-E.5 funcional que R0 describía como endpoints/filtros ya está cubierta por las decisiones posteriores y aceptadas. No se justifica una implementación adicional. F7-E.5 debe mantenerse como cierre documental de F7-E, sin reabrir F7-E.1–E.4.

El siguiente paso lógico es revisar y, si corresponde, aceptar el cierre global documental de F7-E. Esta aceptación global es distinta de la aceptación individual ya otorgada a las subetapas y no se presume en este informe.
