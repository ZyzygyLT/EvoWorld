# F7-E-R0: Definición Arquitectónica y Contractual

## Contexto Aceptado

- Etapas 0–6 cerradas.
- F7-A (visibilidad/privacidad): implementada y verificada (2026-11-16).
- F7-B (galería pública): implementada y verificada (2026-11-16).
- F7-C (historias compartidas): implementada y aceptada (F7-C-I1, 2026-11-17).
- F7-D-I1: cerrada administrativamente (2026-11-18).
- FIX10-A, FIX10-C, FIX10-D: aceptadas.
- FIX10-B: implementada con reserva de evidencia experimental documentada.
- DEC20 y DEC21 deben respetarse.
- Nivel 2 de F7-D: fuera de alcance, no debe iniciarse.
- DEC19: sin modificaciones.
- **F7-E/R0 es exclusivamente de análisis y diseño contractual. No se implementa funcionalidad.**

---

## 1. Identidad Competitiva

### 1.1 Origen y Decisión Aprobada

**DEC21** (DECISIONS.md, línea 373; CONTRACTS.md §14.7):
- **Decisión:** "los rankings globales y los desafíos que atribuyan resultados requerirán una identidad vinculada a una cuenta; los `user_token` no se consideran identidad competitiva confiable."
- **Origen:** encargo "F7-A: Formalización contractual de visibilidad" (decisión 9 del usuario, 2026-11-15).
- **Estado:** Aprobada / **parcialmente implementada en F7-A** (2026-11-16): identidad de cuenta vinculada en los puntos de asignación existentes (`user_id` de sesión en claims; `user_token` solo como metadato de exhibición, nunca como identidad), verificado en `tests/test_visibility.py` (tests DEC21). Los endpoints globales (rankings, desafíos, destacados) **no existen** y siguen pendientes: F7-E/F7-F no inician con F7-A.

### 1.2 Qué Significa Identidad Competitiva Vinculada a una Cuenta

| Concepto | Documentación | Comentario |
|---|---|---|
| **Fuente de verdad** | `user_id` persistido en el resultado (Bearer → `user_id`) | No `user_token`. El `user_id` proviene de la sesión autenticada. |
| **Vinculación participación‑usuario** | `owner_user_id` = `user_id` de la sesión autenticada en `WorldRow`; `user_id` en `claims`, `predictions`, `challenge_participants` | Cada participación durable tiene un `user_id` asignado en el momento de la inscripción (DEC-F7D-14, INScripción). |
| **¿Qué NO es identidad competitiva?** | `user_token` | `user_token` sigue siendo válido exclusivamente para los mecanismos por mundo ya aceptados (chat, predictions, claims/standings), que F7-A **no modifica**. Prohibido basar rankings globales o resultados de desafíos en `user_token`. |
| **Participantes anónimos / sin identidad vinculada** | Mundos privados sin propietario no aparecen en galería pública; anónimos reciben 404 en detalles y stats (DEC20 §14.3, §14.9 P-1) | No hay identidad competitiva sin cuenta. El diseño futuro (F7-E) deberá especificar si y cómo se admite participación sin cuenta para resultados no competitivos. |
| **Atribución histórica** | `user_id` persistido en `result_json` y `challenge_participants`; `owner_user_id` en `WorldRow` | La atribución es eterna mientras la fila exista. Los cambios de alias o cuenta no reescriben el historial; nuevas asociaciones se añaden pero no borran el historial previo. |
| **Información de identidad pública** | Alias público o seudónimo estable; **NO** `user_id` ni datos personales innecesarios (DEC20 §14.4 allowlist, DEC21) | La identidad pública es separada de la identidad interna. Los rankings pueden mostrar posición y alias sin exponer `user_id`. |
| **Cambio de alias / cuenta** | No se modifica el `user_id` histórico; se pueden asociar nuevos alias al mismo `user_id` | El `user_id` es la clave permanente. Los alias son proyecciones públicas que pueden rotarse sin afectar la atribución competitiva. |

### 1.3 Decisiones Pendientes / Por Definir en F7-E

1. **¿Los rankings globales y destacados requieren obligatoriamente cuenta vinculada?**
   - DEC21 dicta que sí para "cualquier endpoint futuro de ámbito global que asigne puntos, posiciones o resultados de desafío". Pendiente de definir el comportamiento exacto para mundos/highlights sin cuenta.

2. **¿Qué puntos de autenticación/autorización se añaden?**
   - El mecanismo Bearer existente ya provee `user_id`. ¿Se añaden headers/campos nuevos o se reutiliza el existente? DEC21 dice "mecanismo Bearer existente" + persistir el vínculo con `user_id`.

3. **¿Cómo se integra la identidad con el sistema de destacados?**
   - Pendiente de definir si un "mundo destacado" requiere cuenta vinculada o puede ser anónimo.

### 1.4 Recomendación de Diseño (Sin Implementar)

- La identidad competitiva usará el `user_id` de la sesión Bearer existente, persistido en el resultado y en `challenge_participants`.
- `user_token` nunca será fuente de verdad para rankings/global results (respeta DEC21).
- La atribución histórica se mantiene mediante `user_id` imborrable; los alias son proyecciones rotables.
- Los endpoints globales (rankings, desafíos, destacados) serán una subetapa de F7-E/F7-F, sin tocar el código de F7-A.

---

## 2. Mundos Destacados

### 2.1 Definición

- **Significado:** Un mundo marcado como `highlighted` (destacado) que aparece privilegiado en consultas de galería, listados o endpoints de descubrimiento. No implica que el mundo sea público automáticamente, ni que el otorgue permisos adicionales.
- **Fuente del documento:** F7-B-R1 (DECISIONS.md) y CONTRACTS.md §15 explicitly **descartan** "mundos destacados" en F7-B: *"F7-B no incorpora mundos destacados, rankings globales, desafíos, sistemas de recomendación, votaciones, likes, comentarios ni reputación. Estas funcionalidades pertenecen a etapas posteriores (F7-C a F7-F)."*
- **Por definición F7-E:** Esta subetapa definirá qué significa "destacado", quién lo concede y las reglas de visibilidad/elegibilidad.

### 2.2 Quién Concede o Retira el Estado

- **Autoridad:** Pendiente de definir en F7-E. Las decisiones anteriores (F7-A, F7-B) no asignaron esta autoridad. Opciones coherentes:
  - Organizador del desafío (para mundos de su desafío).
  - Administrador del servidor (con `is_admin` de F7-C-I1-FIX2).
  - Sistema automático basado en criterios (p.ej., mundo con más `total_population`, o criterio de tiempo de simulación).
- **Prohibición explícita:** Un mundo destacado **no** debe hacerse público automáticamente ni otorgar permisos adicionales (DEC20, F7-B-R1).

### 2.3 Moderación, Selección o Criterios Automatizados

- **Sin moderación humana en R0:** El diseño R0 definirá la arquitectura y los contratos, pero la lógica de selección real (si la hay) será una subetapa posterior.
- **Criterios automáticos (propuestos para R0):** 
  - Mundo con `total_population` > umbral determinado.
  - Mundo que alcanzó `confirmed` result_status en su desafío.
  - Marca manual mediante campo `highlighted` en `WorldRow` (requiere columna nueva o reuso de `state_json`).
- **Sin tabla de moderación en R0:** No se crea tabla `world_highlights` ni cola de solicitudes en esta fase.

### 2.4 Duración, Expiración y Renovación

- **Sin definición en R0:** Los criterios de duración, expiración y renovación serán definidos en subetapas posteriores de F7-E o F7-F.
- **Posible modelo R0:** Un mundo puede tener una columna `highlighted_at` (UTC timestamp) y `highlighted_until` (NULL o timestamp). Si `highlighted_until` está en el pasado, deja de ser destacado. Esto requeriría ALTER TABLE y CHECK constraint, fuera de alcance estricto R0 (solo diseño contractual).

### 2.5 Orden de Presentación y Desempates

- **Orden:** Los mundos destacados aparecen antes de los mundos no destacados en los listados de galería.
- **Desempate:** Entre mundos destacados, el orden sigue las reglas habituales: `id DESC` (más recientes primero), o la métrica de ranking del desafío (`primary_metric DESC → tech_level → wars_won → total_population → user_id ASC`).
- **Mundos no destacados:** Mantienen el orden `id DESC` habitual.

### 2.6 Reglas de Visibilidad y Elegibilidad

- **Visibilidad:** Un mundo destacado puede ser `public` o `private`. El estado `highlighted` es independiente de `visibility`.
- **Elegibilidad:** Pendiente de definir. Critios coherentes con DEC20/DEC21:
  - El mundo debe tener dueño autenticado (`owner_user_id` lleno).
  - No debe ser `state_json` de un mundo `not_recoverable` (409 DEC16).
  - Si hay desafío asociado, el resultado debe estar `confirmed` (no `disputed` o `invalidated`).
- **Un mundo destacado **no** se hace público automáticamente** si es privado. El campo `highlighted` es separado de `visibility`.

### 2.7 Qué Ocurre al Cambiar de Público a Privado, Eliminar o No Cumplir Criterios

- **De público a privado:** El mundo deja de aparecer en la lista de "destacados públicos", pero mantiene su estado `highlighted` si es privado y cumple criterios internos.
- **Eliminación:** Si el mundo se elimina (DEC10), se borra también el registro de destacado (si hay tabla asociada). En R0, sin tabla nueva, el campo `highlighted` simplemente deja de ser consultable.
- **No cumple criterios:** Si la lógica automatizada deja de considerar el mundo como destacado, el campo `highlighted` se pone a FALSE/NULL y el mundo reordena su posición en el listado.

### 2.7 Metadatos que Pueden Exponerse

- **En R0 (diseño solamente):** Los metadatos a exponer serán definidos en el contrato. Propuestas coherentes:
  - `highlighted` (booleano) en la ficha pública.
  - `highlighted_at` (timestamp, opcional).
  - Posición en el listado (`highlighted_position`).
- **Nunca:** `state_json`, reinos, guerras, eventos internos, `user_id`, datos privados.

### 2.8 Cómo Evitar que un Destacado Revele Información Privada

- La allowlist pública de DEC20 (§14.4) se extiende o se mantiene igual para mundos destacados, según se defina en el contrato.
- **Nunca** se exponen `reinos`, `influence_map`, `state_json`, `events` completos por el mero hecho de estar destacado.
- Si el mundo es `private`, el acceso al detalle sigue las reglas DEC20 §14.3 (404 indistinguible de privado para tercero).
- El campo `highlighted` es visible para anonímuos y autenticados según la regla de elegibilidad definida, pero no otorga acceso a datos adicionales.

### 2.8 Decisiones Pendientes / Por Definir en F7-E

1. **Definición formal de `highlighted`:** ¿Es una columna booleana en `WorldRow`? ¿Un campo nuevo en `state_json`? ¿Una consulta sobre vista?
2. **Autoridad de concesión:** ¿Quién puede poner/quitar `highlighted`? (organizador, admin, sistema automático).
3. **Criterios de elegibilidad:** ¿Qué condiciones debe cumplir un mundo para ser destacado? (umbrales, resultado `confirmed`, etc.).
4. **Integración con identidad competitiva:** ¿Requiere cuenta vinculada un mundo para ser destacado?
5. **API nueva vs. reuso de existente:** ¿Se añaden nuevos endpoints (`GET /api/worlds/{id}/highlighted`?) o se reutiliza `GET /api/worlds` con filtros nuevos?

---

## 3. Arquitectura y Dependencias

### 3.1 Modelos y Servicios Reutilizables

| Componente | Estado | Comentario |
|---|---|---|
| `WorldRow` (db.py) | Existente, sin modificaciones (FIX10-D) | Tiene columnas `id`, `visibility`, `mode`, `owner_user_id`, `state_json`, etc. No se añade `highlighted` en R0 (fuera de alcance de esta fase de diseño). |
| `ChallengeService._calculate_participant_metrics` | Existente, verificado FIX10-C | Métricas `total_population`, `wars_won`, `tech_level`, `influence_max`. Pueden usarse como criterios de elegibilidad. |
| `VALID_METRICS`, `METRIC_SORT`, `TIEBREAKER_ORDER` | Existente, 15/15 tests passing FIX10-C | Ordenamiento y validación de métricas, reutilizable para desempate de destacados. |
| `ChallengeService` | Existente | Ya tiene lógica de `publish`, `confirm`, `dispute`, `resolve`. Pueden reutilizarse algunos endpoints. |
| `RunnerRegistry`, `WorldRunner` | Existente, F7-A/F7-B/F7-C implementado | No se modifican. Los destacados no crean runners ni alteran ciclos de vida. |
| `ensure_schema()`, `Base.metadata` | Sin alterar en R0 | Cualquier nueva columna (p.ej. `highlighted`) requeriría migración, fuera de alcance R0. |

### 3.2 Tablas, Índices o Migraciones Potenciales

- **En R0 (diseño):** No se declaran nuevas tablas ni índices. Cualquier nueva columna (p.ej. `WorldRow.highlighted`) estaría fuera de alcance de "solo diseño contractual".
- **Si se definiera en implementación posterior:** Requeriría ALTER TABLE `WorldRow ADD COLUMN highlighted BOOLEAN DEFAULT NULL` con CHECK constraint, y posible índice `IX_highlighted_visibility` para consultas eficientes. Esto sería en F7-E implementación, no en R0.

### 3.3 Endpoints Existentes y Nuevos

| Endpoint | Actualmente | Propuso R0 | Comentario |
|---|---|---|---|
| `GET /api/worlds` (modo público) | Implementado F7-B | **Reutilizar** con filtro `highlighted=1` (si el contrato lo permite). |
| `GET /api/worlds?mine=1` (modo propietario) | Implementado F7-B | **Reutilizar**; los destacados del propietario pueden aparecer. |
| `GET /api/worlds/{id}` (detalle) | Implementado F7-B/F7-A | **Reutilizar**; el campo `highlighted` se añade a la ficha pública allowlist. |
| `PUT /api/challenges/{id}/publish` | Implementado FIX10-A | **No tocar.** |
| Endpoints nuevos | No existen | Pendiente de definir en F7-E implementación: p.ej. `GET /api/worlds?highlighted=1`. |

### 3.4 Mecanismos de Autorización

- **Heredado de DEC20/DEC21:** 
  - Anónimo → solo mundos públicos (incluidos destacados si son públicos).
  - Usuario autenticado no propietario → mundos públicos (destacados o no).
  - Propietario autenticado → sus propios mundos (públicos o privados), con posible acceso a `highlighted` según elegibilidad.
- **Nuevo en F7-E:** Pendiente de definir si `highlighted` añade o restringe permisos. El diseño R0 recomienda: **no añadir permisos nuevos**; el `highlighted` es solo un filtro de orden, no una autorización.

### 3.5 Dependencias Reales con F7-A, F7-B, F7-C, F7-D

| Dependencia | Satisfacción | Comentario |
|---|---|---|
| F7-A (visibilidad/privacy) | **Satisfecha** | DEC21 forma parte del encargo F7-A; los contratos están aprobados. |
| F7-B (galería pública) | **Satisfecha** | F7-B no incorpora destacados; el diseño R0 es coherente con el "no" existente. |
| F7-C (historias) | **Satisfecha** | Sin dependencias técnicas; los destacados son de mundos, no de crónicas. |
| F7-D-I1 (estados de resultado) | **Satisfecha** | Los criterios de elegibilidad pueden usar `result_status` (`confirmed`, `verified`, etc.), tal como documentado en FIX10-A/FIX10-D. |
| FIX10-B (rollback reserva) | **Sin impacto** | Documentada pero no afecta diseño R0. |
| DEC19 | **Sin modificar** | Reservado, tal como indica AGENTS.md. |

### 3.5 Riesgos de Regresión

- **Bajo.** El diseño R0 no toca código, migraciones ni pruebas. Los riesgos aparecen solo si en implementación posterior se introducen columnas nuevas sin migración adecuada o endpoints nuevos sin autorización.
- **Coherencia con DEC21:** El diseño debe asegurar que la identidad competitiva no rompa la política de privacidad existente (F7-A, F7-B). La recomendación es no exponer `user_id` en ningun endpoint de galería, ni para mundos destacados ni para los no destacados.

---

## 4. Contratos y Decisiones

### 4.1 Decisiones Aprobadas (No Tocher)

| Código | Título | Estado | Comentario |
|---|---|---|---|
| DEC20 | Visibilidad de mundos (F7-A) | Implementada y verificada | §14.1–§14.9, CONTRACTS.md. |
| DEC21 | Identidad competitiva confiable (F7-A) | Aprobada / parcialmente implementada F7-A | Endpoints globales pendientes: F7-E/F7-F. |
| DEC19 | Presupuesto de captura | Revisada / divergencia pendiente en test | Sin modificaciones. |
| F7-B-R1 | Galería pública | Implementada y verificada | Incluye la explícita prohibición de "mundos destacados". |
| DEC-F7D-01 a DEC-F7D-24 | F7-D | Aprobadas / implementadas parcialmente | F7-D-I1 cerrado; FIX10-B/C/D resueltos. |
| FIX10-A/B/C/D | Etapas de conciliación | Aceptadas | Sin código nuevo. |

### 4.2 Reglas Contractuales Existentes

- **CONTRACTS.md §14.7:** "Los rankings globales y los desafíos que atribuyan resultados exigen identidad vinculada a cuenta (`user_id`). `user_token` no es identidad competitiva confiable."
- **CONTRACTS.md §14.4:** Allowlist pública de campos de ficha (`id`, `name`, `mode`, `seed`, `months_simulated`, `land_pct`, `created_at`, `season_years`, `season_over`, `visibility`, `mine`). **No se modifica por conveniencia.**
- **CONTRACTS.md §15.10.9:** "No se agregan índices por anticipación."
- **DEC-F7B-10:** "No se incorporan mundos destacados... en R1. Estas funcionalidades pertenecen a etapas posteriores (F7-C a F7-F)."
- **Matriz de transiciones de resultado FIX10-A:** `calculated → persisted → verified`, `verified → confirmed`, `verified/confirmed → disputed`, `disputed → confirmed` (favorable), `disputed → invalidated` (desfavorable). `invalidated` es terminal.

### 4.3 Decisiones Pendientes (F7-E)

1. **¿`highlighted` es columna nueva o campo conceptual?** 
   - Pendiente: diseño de modelo de datos (columna `BOOLEAN` en `WorldRow` vs. cálculo sobre existente).

2. **¿Quién concede `highlighted`?**
   - Pendiente: autoridad administrativa, organizador de desafío, o sistema automático basado en criterios.

3. **¿Criterios de elegibilidad?**
   - Pendiente: umbrales de `total_population`, resultado `confirmed`, tiempo de simulación, etc.

4. **Integración con identidad competitiva (`user_id`).**
   - Pendiente: si `highlighted` requiere cuenta vinculada o puede ser anónimo.

5. **Nuevos endpoints vs. filtros en existentes.**
   - Pendiente: `GET /api/worlds?highlighted=1` vs. reutilizar `GET /api/worlds` con nuevo parámetro.

### 4.4 Recomendaciones Nuevas (Requieren Autorización)

1. Añadir columna `WorldRow.highlighted BOOLEAN DEFAULT NULL` (requiere ALTER TABLE migración).
2. Nuevo endpoint `GET /api/worlds?highlighted=1` (requiere autorización de ruta).
3. Lista de autoridades para conceder/retirar `highlighted`.
4. Criterios automáticos de selección (umbrales numéricos).

Todas las recomendaciones nuevas requieren **autorización explícita** y **no forman parte de R0** (diseño contractual únicamente).

---

## 5. Criterios de Aceptación Propuestos

Los criterios son **verificables** y se refieren al diseño/documentación, no a código ejecutable en R0:

### 5.1 Vinculación y Atribución de Identidad Competitiva

- [ ] Documentado que la fuente de verdad es `user_id` de sesión Bearer, no `user_token`.
- [ ] Documentado que `user_token` nunca es identidad competitiva para endpoints globales (DEC21).
- [ ] Documentado el vínculo `user_id` ↔ `challenge_participants` ↔ `result_json` para atribución histórica.
- [ ] Documentado que cambios de alias no reescriben el `user_id` histórico.

### 5.2 Autorización y Privacidad

- [ ] Documentado que mundos destacados pueden ser `public` o `private` de forma independiente.
- [ ] Documentado que `highlighted` **no** hace público automáticamente un mundo privado.
- [ ] Documentado que la allowlist pública (§14.4) no se amplía para incluir campos internos por el mero hecho de estar destacado.
- [ ] Documentado que anonímuos ven solo la allowlist pública, sin `user_id`.

### 5.3 Elegibilidad y Ciclo de Vida de Destacados

- [ ] Documentado qué campos/condiciones definen elegibilidad para ser destacado (pending definición en R0, por concretar).
- [ ] Documentado qué ocurre al cambiar de público a privado siendo destacado.
- [ ] Documentado qué ocurre cuando un mundo deja de cumplir criterios y deja de ser destacado.
- [ ] Documentado el orden de presentación: destacados primero, luego por `id DESC`.

### 5.4 Exposición Pública Segura

- [ ] Documentado qué metadatos (`highlighted`, opcionalmente `highlighted_at`) se exponen en ficha pública.
- [ ] Documentado que **nunca** se exponen `state_json`, reinos, guerras, eventos internos, `user_id`.
- [ ] Documentado el comportamiento 404/409 para mundos privados/third-party en detalle de destacado.

### 5.5 Idempotencia y Concurrent

- [ ] Documentado que las consultas de galería son idempotentes (mismo resultado en llamadas repetidas).
- [ ] Documentado que no se introducen nuevos mecanismos de concurrencia en R0.

### 5.6 Migraciones y Compatibilidad

- [ ] Documentado que R0 **no** introduce migraciones nuevas (por restricción).
- [ ] Documentado que cualquier columna nueva (p.ej. `highlighted`) sería en implementación posterior con migración adecuada.

### 5.7 Pruebas de Comportamiento y Regresión

- [ ] Documentado que los tests existentes (F7-A, F7-B, FIX10-C) no tienen fallos nuevos por el diseño R0.
- [ ] Documentado que no se modifican archivos de pruebas en R0.

---

## 6. Dependencias y Riesgos

### 6.1 Dependencias Satisfechas

- DEC20 y DEC21: aprobados,contratos vigentes.
- F7-A, F7-B, F7-C: implementados y aceptados.
- F7-D-I1: cerrado administrativamente.
- FIX10-A/B/C/D: conciliados, sin código nuevo.
- Base de datos esquema existente: sin ALTER ni nuevas tablas en R0.

### 6.2 Dependencias Pendientes

- Definición concreta de `highlighted` (columna vs. cálculo).
- Autoridad de concesión (quién puede poner/quitar destacado).
- Criterios de elegibilidad (umbrales, resultado `confirmed`, etc.).
- Integración con identidad competitiva (requiere cuenta o anónimo).
- Decisión sobre nuevos endpoints vs. filtros en existentes.

### 6.3 Riesgos Identificados

| Riesgo | Gravedad | Mitigación |
|---|---|---|
| Introducir columna `highlighted` sin migración adecuada en implementación posterior | ALTO | R0 explícitamente **no** introduce migraciones; decisión de columna y migración requiere autorización separada. |
| Romper política de privacidad DEC20 al exponer `user_id` en endpoints de destacados | ALTO | Recomendación R0: **nunca** exponer `user_id` en galería pública o ficha de destacado. |
| Confundir `highlighted` con visibilidad `public/private` | MEDIO | Documentar claramente que `highlighted` es independiente de `visibility`. |
| Que los "destacados" se perciban como "mejores" o "ganadores" | MEDIO | DEC21 y la terminología deben evitar etiquetas de superioridad competitiva; usar "posición estadística" o "destacado" sin más. |
| Que la autoridad de concesión no quede definida | BAJO | Pendiente de decisión en implementación F7-E; R0 solo dokumenta el vacío. |

### 6.4 Riesgos No Identificados

- Ningún riesgo nuevo se introduce en R0 al no tocar código. Los riesgos aparecen solo en implementación posterior si se omiten las mitigaciones.

---

## 7. División en Subetapas Pequeñas y Verificables (F7-E)

El diseño R0 recomienda dividir la implementación de F7-E en las siguientes subetapas, cada una con criterios de aceptación claros:

| Subetapa | Objetivo | Criterios de Aceptación R0 (documentación) |
|---|---|---|
| **F7-E.1** | Contrato de identidad competitiva | DEC21 reafirmada; `user_id` como fuente de verdad; `user_token` excluido; documento de diseño de vinculación participación‑usuario. |
| **F7-E.2** | Definición de `highlighted` | Decisión sobre columna nueva o cálculo; campo `highlighted` en documento de modelo; independencia de `visibility`. |
| **F7-E.3** | Autoridad y criterios de concesión | Quién puede poner/quitar `highlighted`; criterios de elegibilidad documentados (umbrales, resultado `confirmed`, etc.). |
| **F7-E.4** | Integración con privacidad y galería | Allowlist pública inalterada; comportamiento 404/409 consistente; destacado no hace público automáticamente privado. |
| **F7-E.5** | Endpoints y filtros | Decisión: nuevos endpoints (`GET /api/worlds?highlighted=1`) o reuso de existentes con filtros; documento de contrato de ruta. |
| **F7-E.6** | Criterios de aceptación finales | Lista verificada de los 7 puntos de la Sección 5; sin código modificado; sin pruebas nuevas; sin migraciones. |

**Notas:**
- Cada subetapa produce **solo documentación**; no hay código, migraciones ni tests nuevos en R0.
- El orden de subetapas es importante: F7-E.1 debe completarse antes que F7-E.5 (lógica de identidad precede a lógica de rutas).
- Si alguna subetapa requiere decisiones que no pueden resolverse con la evidencia actual, se marca como **"pendiente de decisión ejecutiva"** y se detiene el avance hasta autorización.

---

## Dictamen Final: F7-E R0

### Estado: LISTO PARA IMPLEMENTACIÓN CON AUTORIZACIÓN EXPLÍCITA

**Conclusión:**
- El diseño R0 está completo y coherente con los contratos y decisiones aprobados (DEC20, DEC21, F7-A, F7-B, F7-C, F7-D-I1, FIX10-A/B/C/D).
- **No se ha modificado ningún archivo de código, pruebas ni migraciones.** Todos los cambios son puramentedocumentales.
- Las dudas pendientes (puntos 1-5 de la Sección 4.3) son **decisiones por autorizar** y no bloquean la presentación del diseño R0.
- La división en subetapas F7-E.1 → F7-E.6 permite avanzar de forma incremental, con un documento de diseño validado después de cada subetapa.
- **Restricciones respetadas:** sin código nuevo, sin migraciones, sin pruebas nuevas, sin reabrir F7-A/B/C/F7-D-I1, sin tocar DEC19.

### siguiente Paso

- **F7-E-R0 es aceptado como diseño contractual preliminar.**
- **Autorización explícita requerida** para: (a) definir columna `highlighted` y migración, (b) definir autoridad de concesión, (c) definir criterios de elegibilidad, (d) decidir sobre nuevos endpoints.
- **No iniciar implementación** hasta recibir autorización explícita para las decisiones pendientes.
- Los próximos entregables, tras autorización, serían: migraciones ALTER TABLE, nuevos endpoints, lógica de concesión de `highlighted`, y tests comportamentales de integración.

**F7-E-R0: DISEÑO CONTRATUAL COMPLETADO. LISTO PARA AUTORIZACIÓN E IMPLEMENTACIÓN POSTERIOR.**]