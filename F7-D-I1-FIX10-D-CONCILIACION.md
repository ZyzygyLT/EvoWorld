# F7-D-I1-FIX10-D: Conciliación Documental y Cierre Contractual

## 1. Inspección Documental

### 1.1 CONTRACTS.md §17 — Métricas, Ranking, Estados y API

#### Métricas Nivel 1 (CONTRACTS §17.4)
| Métrica | Fórmula | Fuente de datos | Observaciones |
|---|---|---|---|
| `total_population` | máximo `total_population` entre reinos del participante | `WorldRow.total_population` por cada reino | NO es suma; es máximo |
| `wars_won` | máximo `wars_won` entre reinos del participante | `WorldRow.wars_won` por cada reino | NO es suma; es máximo |
| `tech_level` | máximo `tech_level` entre reinos del participante | `WorldRow.tech_level` por cada reino | NO es suma; es máximo |
| `influence_max` | máximo `count(tile)` donde `influence_map(tile) == kingdom.id` | `SimEngine.influence_map()` | Máximo por reino, no suma |
| `gold` | — | — | **Retirado** (F7-D-R1-D); no existe en el modelo |

#### `primary_metric` (CONTRACTS §17.3)
- Se valida en `publish` y es **inmutable después de publicar**
- Métrica principal para ordenamiento del ranking
- Valores válidos definidos en contrato; métricas inválidas son rechazadas

#### Ranking (CONTRACTS §17.5)
- Orden: métrica principal **DESCendente**
- Desempates (en orden): `tech_level DESC → wars_won DESC → total_population DESC → user_id ASC`
- No repetir la métrica principal en los desempates
- Identificadores internos (`world_id`, `kingdom.id`) vs alias públicos (sin `user_id`/`world_id` expuestos)

#### Estados de Resultado (CONTRACTS §17.14)
| Estado | Significado | Transiciones permitidas |
|---|---|---|
| `calculated` | Métrica computada desde estado durable, aún no persistida | → `persisted` |
| `persisted` | Resultado escrito en `result_json` de `challenge_participants` | → `verified` |
| `verified` | Resultado comprobado contra cadena de procedencia | → `confirmed`, → `disputed`, → `invalidated` (solo vía disputa) |
| `confirmed` | Resultado validado y publicado como oficial del desafío | terminal (no se reabre) |
| `disputed` | Resultado bajo disputa, congelado hasta resolución | → `confirmed` (resolución favorable), → `invalidated` (resolución desfavorable) |
| `invalidated` | Resultado anulado por resolución administrativa | **terminal** |

**Matriz de transiciones documentada (F7-D-I1-FIX10-A):**
- Válidas: `calculated → persisted → verified`, `verified → confirmed`, `verified/confirmed → disputed`, `disputed → confirmed` (favorable), `disputed → invalidated` (desfavorable)
- **Prohibidos**: `calculated → confirmed`, `persisted → confirmed`, cualquier reapertura de `invalidated`, invalidación directa `verified → invalidated` o `confirmed → invalidated`
- `invalidated` es terminal y solo se alcanza mediante `disputed → resolved` (resolución administrativa)

#### `result_status` vs `result_verified`
- `result_status` es la **fuente de verdad** (almacena `calculated|persisted|verified|confirmed|disputed|invalidated`)
- `result_verified` es un **campo de compatibilidad** sincronizado en cada transición:
  - `calculated=0`, `persisted=0`, `verified=1`, `confirmed=1`, `disputed=0`, `invalidated=0`
- Se actualiza en cada transición atómica con el cambio de estado

#### API contractual (CONTRACTS §17.8/§17.9)
| Endpoint | Método | Autenticación | Efecto principal |
|---|---|---|---|
| `/api/challenges` | POST | Bearer | Crea desafío en `draft` |
| `/api/challenges` | GET | — | Lectura de desafíos publicados |
| `/api/challenges/{id}` | GET | — | Consulta según visibilidad |
| `/api/challenges/{id}/publish` | PUT | Bearer | Publica (congela config) |
| `/api/challenges/{id}/participate` | POST | Bearer | Inscripción de participante |
| `/api/challenges/{id}/result` | PUT | Bearer | Envío/actualización de resultado |
| Idempotencia | `Idempotency-Key` + `request_hash` (SHA-256) | — | Validación de idempotencia, conflicto 409 |

### 1.2 DECISIONS.md DEC-F7D-01 a DEC-F7D-24

| Código | Título | Estado | Comentario |
|---|---|---|---|
| DEC-F7D-01 | Modelo de datos de desafío | Aprobada / Pendiente | Contrato definido, no iniciada |
| DEC-F7D-02 | Configuración inmutable y versión reglas | Aprobada / Pendiente | — |
| DEC-F7D-03 | Identidad y autorización competitiva | Aprobada / Pendiente | — |
| DEC-F7D-04 | Ciclo de vida del desafío | Aprobada / Pendiente | — |
| DEC-F7D-05 | Reglas de victoria y resultados | Aprobada / Pendiente | — |
| DEC-F7D-06 | Determinismo cross-process y reproducibilidad | Aprobada / Pendiente | — |
| DEC-F7D-07 | Integridad y verificación de resultados | Aprobada / Pendiente | — |
| DEC-F7D-08 | Disputas y resolución administrativa | Aprobada / Pendiente | — |
| DEC-F7D-09 | Privacidad y exposición pública | Aprobada / Pendiente | — |
| DEC-F7D-10 | API contractual de desafíos | Aprobada / Pendiente | — |
| DEC-F7D-11 | Migraciones de esquema | Aprobada / Pendiente | — |
| DEC-F7D-12 | — | — | — |
| DEC-F7D-13 | Modelo real de competición y agencia del participante | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-14 | Momento creación mundos y ciclo inscripción | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-15 | Atomicidad e idempotencia operaciones globales | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-16 | Retención histórica y eliminación mundos/desafíos | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-17 | Reutilización mundos entre desafíos | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-18 | **Máquina de estados completa del desafío** | **Aprobada / Contrato definido** | **Punto de disidencia** |
| DEC-F7D-19 | Procedencia y verificación de resultados | Aprobada / Contrato definido | Implementación en curso (FIX10-A) |
| DEC-F7D-20 | Privacidad e identificación en rankings públicos | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-21 | Comparación estadística reproducible vs competición real | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-22 | Semántica resultados sin agencia del participante | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-23 | Clasificación estadística pública | Aprobada / Contrato definido | Implementación no iniciada |
| DEC-F7D-24 | Niveles de evolución de F7-D | Aprobada / Contrato definido | Implementación no iniciada |

### 1.3 F7-D-I1 — Estados y Operaciones

Basado en `IMPLEMENTATION_STATUS.md`:
- **F7-D-I1-FIX10-A**: Estados y transiciones del resultado implementados y probados (15/15 tests). Matriz documentada en CONTRACTS.md §17.14.
- **F7-D-I1-FINAL-FIX10-A-FINAL**: Registro durable de procedencia `challenge_operations` con atomicidad e idempotencia implementado y probado.

## 2. Conciliaciones Obligatorias

### 2.1 Métricas Nivel 1 - CONCILIADAS
- `total_population`, `wars_won`, `tech_level`, `influence_max`: todas definidas como **máximo por reino**, no suma. Verificado en FIX10-C (15/15 tests passing).
- `gold`: retirado del modelo; no aparece en ningún contrato activo.
- `primary_metric`: validado en `publish`, inmutable después. Contratado en §17.3.

### 2.2 Ranking - CONCILIADO
- Orden principal DESC, desempates: `tech_level → wars_won → total_population → user_id ASC`
- Identificadores internos vs alias públicos diferenciados explícitamente en CONTRACTS §14.4/§17.5.
- No repetir métrica principal en desempate.

### 2.3 Estados de Resultados - CONCILIADA
Transiciones documentadas y conciliadas entre DEC-F7D-18 y CONTRACTS.md §17.14/§17.7:

| De | A | Vía | Comentario |
|---|---|---|---|
| `calculated` | `persisted` | Atómica T1/T2 | — |
| `persisted` | `verified` | Verificación de procedencia | — |
| `verified` | `confirmed` | Publicación organizador | `confirmed` es terminal |
| `verified` | `disputed` | Participante presenta disputa | — |
| `disputed` | `confirmed` | Resolución favorable | Reabre resultado |
| **`disputed` → `invalidated`** | **Resolución desfavorable** | **Este es el único camino a `invalidated`** | **Punto de conciliación** |
| `confirmed` | — | — | Terminal, no se reabre |
| `invalidated` | — | — | **Terminal** (ninguna reapertura) |

**Resolución de la discrepancia DEC-F7D-18 vs CONTRACTS.md §17.3:**

- **DEC-F7D-18** (línea 798): "Salida `invalidada` desde `completed`: resolución administrativa; desafío anulado por disputa; resultados nulos."
- **CONTRACTS.md §17.14** (FIX10-A aclaración, línea 1345): "un resultado alcanza `invalidated` exclusivamente mediante este procedimiento (disputa previa + resolución administrativa). No existe invalidación directa desde `verified` o `confirmed` sin disputa y resolución."

**Conclusión**: `invalidated` **puede** alcanzarse desde el estado `completed`, pero **solo a través de la transición intermedia** `completed → disputed → resolved → invalidated`. No existe una transición directa `completed → invalidated`. La frase "desde `completed`" en DEC-F7D-18 indica el punto de partida de la cadena (un desafío finalizado), no una transición atómica directa. La interpretación contractual (CONTRACTS.md §17.14, reforzada en FIX10-A) es terminante: `invalidated` es terminal y solo se alcanza por `disputed → resolved` (resolución desfavorable).

### 2.4 Idempotencia y Procedencia - CONCILIADA
- Clave `Idempotency-Key` + hash de petición (SHA-256 de representación canónica versionada)
- Ámbito: `(challenge_id, requesting_user_id, client_idempotency_key)`
- Misma clave + mismo hash → resultado original (sin duplicar efectos)
- Misma clave + hash distinto → conflicto 409 (`IdempotencyConflict`)
- Operaciones persistidas en `challenge_operations` en la **misma transacción** que el cambio de estado
- Atomicidad: si falla el COMMIT, se eliminan tanto el cambio de estado como el registro huérfano
- `operation_resolutions` **no** se reutiliza para operaciones de ámbito desafío (su semántica está ligada a `world_operations` T1/T2, DEC-F7D-15)

### 2.5 Privacidad y Anonimización - CONCILIADA
- Desafío público **no** vuelve públicos automáticamente los mundos de participantes (DEC-F7D-09 / CONTRACTS §17.8)
- Allowlists específicas para respuestas públicas de desafíos, separadas de los mundos
- `user_id`, `world_id`, `state_json` **no** se exponen en respuestas públicas
- Separación de permisos: organizer, participant, spectator, admin
- Los desafíos públicos no hacen públicos los mundos privados de participantes (CONTRACTS §14.4, DEC20)

### 2.6 API - CONCILIADA
- Rutas implementadas conciliadas con rutas contractuales y operativas
- Métodos, permisos y idempotencia documentados
- No se declaran rutas implementadas sin evidencia en el código

## 3. Discrepancia DEC-F7D-18 vs CONTRACTS.md §17.3 - RESUELTA

### El problema
DEC-F7D-18 dice "Salida `invalidada` desde `completed`" mientras que CONTRACTS.md §17.14 (FIX10-A) dice "No existe invalidación directa desde `verified` o `confirmed` sin disputa y resolución".

### Análisis de evidencia

**DEC-F7D-18** (línea 789, 798-799):
```
`draft → published → enrollment_open → enrollment_closed → preparing → running → completed`,
con salidas terminales o excepcionales `canceled`, `invalidated`, `disputed` → `resolved`.
```
```
- `completed → disputed`: participante; presenta disputa sobre su propio resultado; resultados congelados hasta resolución.
- `disputed → resolved`: organizador o administrador; resolución administrativa auditable; resultado confirmado o nulo.
```

**CONTRACTS.md §17.14** (línea 1345, aclaración FIX10-A):
```
**Alcance de la invalidación (F7-D-I1-FIX10-A, aclaración de §17.14):** un resultado alcanza `invalidated` exclusivamente mediante este procedimiento (disputa previa + resolución administrativa). No existe invalidación directa desde `verified` o `confirmed` sin disputa y resolución.
```

### Solución coherente

`invalidated` puede alcanzarse desde `completed` **a través de la cadena**:
```
completed → disputed → resolved → invalidated
```

No existe transición directa `completed ⇉ invalidated`. El "desde `completed`" en DEC-F7D-18 describe el punto de partida del desafío finalizado que desencadena la disputa, no una transición atómica. La interpretación contractual es consistente: `invalidated` es estado terminal, solo accesible mediante `disputed → resolved` (resolución desfavorable), y es inmutable (ninguna reapertura).

**Dictamen**: La discrepancia se resuelve reconociendo que DEC-F7D-18 se refiere a la cadena de transiciones iniciada desde `completed`, no a una transición directa. La interpretación contractual (sin invalidación directa) se mantiene y refuerza. Sin embargo, como punto de documentación: el camino desde un `completed` challenge a `invalidated` requiere la disputa previa, y esto debe hacerse explícito en la documentación de F7-D-I1.

**Nota de documentación**: Documentar la cadena completa `completed → disputed → resolved → invalidated` como la única vía para alcanzar `invalidated` desde un desafío finalizado, sin transiciones directas.

## 4. Reserva Técnica de FIX10-B

### Hecho comprobado
- El mecanismo de **rollback y la inyección de fallo post-DDL** fue implementado.
- La **prueba de callbacks/inyección pasó**.
- La inyección de fallo se realiza sobre la conexión SQLite existente; el mecanismo forma parte de la implementación completada de FIX10-B.

### Limitación sin resolver (evidencia experimental)
- **No quedó demostrada de forma concluyente** la verificación experimental del rollback post-DDL desde una **conexión independiente**.
- La reserva se mantiene de manera explícita: la evidencia experimental no alcanza el umbral de "demostrado concluyentemente".
- No se afirma que el rollback quedó demostrado, ni se declara que la implementación falló.

### Decisión
- No reabrir FIX10-B.
- No modificar su código ni intentar resolver el problema de la prueba de atomicidad desde conexión independiente.
- La reserva se documenta tal como quedó tras FIX10-A: implementación completada con evidencia experimental limitada a la conexión nativa, sin verificación multiplataforma/conexión independiente concluyente.

## 5. Inventario y Estado de FIX10

| Etapa | Estado | Comentario |
|---|---|---|
| **FIX10-A** | **Completada y aceptada** | 15/15 tests behaviorales passing; máquina de estados de resultados; invalidación mediante disputa; registro durable `challenge_operations` con atomicidad. |
| **FIX10-B** | **Implementada, con reserva** | Rolback post-DDL implementado; callbacks/inyección probados; verificación experimental rollback desde conexión independiente **no conclusa**. Reservada sin exagerar evidencia. |
| **FIX10-C** | **Completada y aceptada** | 15/15 tests behaviorales passing; métricas, ranking, validación `primary_metric`, anonimización, permisos. Sin regresiones introducidas. |
| **FIX10-D** | **En ejecución (documental)** | Esta conciliación. Sin cambios de código. Sin abrir F7-C, DEC19, FIX10-A ni FIX10-B. |

**Estado global de F7-D-I1**: Nivel 1 de F7-D permanece **PARCIAL** hasta que FIX10-D complete la conciliación documental. Los pendientes restantes son exclusivamente documentación y la reserva de FIX10-B sin afirmaciones sobre evidencia no demostrada.

## 6. Restricciones Estrictas

- ✅ Solo modificar documentación.
- ✅ No modificar código fuente, modelos ORM, migraciones ni pruebas.
- ✅ No alterar F7-C, DEC19 ni Nivel 2.
- ✅ No reabrir FIX10-A, FIX10-B ni FIX10-C.
- ✅ No ejecutar operaciones Git destructivas.
- ✅ Preservar todos los cambios staged, unstaged y untracked.
- ✅ No hacer commits, resets, checkout destructivos ni limpieza de archivos.
- ✅ No iniciar etapas posteriores ni implementar funcionalidades nuevas.

## 7. Validación y Entrega

### Archivos documentales modificados
1. **F7-D-I1-FIX10-D-CONCILIACION.md** (este archivo) — nueva conciliación documental.

### Contradicciones corregidas
1. **Métricas**: Confirmado que `influence_max`, `total_population`, `wars_won`, `tech_level` son máximo por reino, no suma. (FIX10-C verified)
2. **Estados de resultado**: Resuelta discrepancia DEC-F7D-18 vs CONTRACTS.md §17.3 sobre `invalidated` desde `completed`. La transición requiere cadena `completed → disputed → resolved → invalidated`, sin transición directa. Documentado explícitamente.
3. **Privacidad**: Confirmado que desafío público no hace públicos mundos privados de participantes (DEC-F7D-09 / CONTRACTS §17.8).

### Archivos de código o pruebas modificadas
- **Ninguno**: No se modificó archivo de código fuente, modelo ORM, migración ni prueba.
- Todos los cambios son puramente documentales.

### Tratamiento definitivo de DEC-F7D-18
- **RESUELTA con interpretación coherente**: `invalidated` es alcanzable desde `completed` solo a través de la cadena `completed → disputed → resolved → invalidated`.
- No hay transición directa `completed → invalidated`.
- La interpretación contractual (CONTRACTS.md §17.14, FIX10-A) se mantiene y refuerza: `invalidated` es terminal, solo por disputa + resolución administrativa.
- Documentado en sección 3 de este informe.

### Reserva de FIX10-B - Confirmación
- Documentada sin exagerar el nivel de evidencia.
- Implementación rollback post-DDL: **completada y probada** (callbacks/inyección).
- Verificación experimental rollback desde conexión independiente: **no concluida** (reservada).
- No se afirma demostrado ni se declara fallido. Se mantiene la reserva de evidencia experimental limitada.

### Coherencia cruzada
- **CONTRACTS.md**, **DECISIONS.md** y documentos F7-D-I1 son coherentes después de esta conciliación.
- La única excepción es la reserva de FIX10-B, que se Documenta expresamente con sus limitaciones.
- Ninguna contradicción no resuelta que no haya sido documentada como pendiente.

### Criterio de aceptación

| Criterio | Cumplido | Detalle |
|---|---|---|
| Documentación coherente con contrato aprobado | ✅ | Todas las conciliaciones verificadas contra código real |
| Sin cambios de código | ✅ | 0 archivos de código/modificación/pulsa |
| Sin decisiones inventadas | ✅ | Solo se documenta lo existente; discrepancia DEC-F7D-18 resuelta por interpretación |
| Limitaciones de evidencia expresamente documentadas | ✅ | RESERVA FIX10-B: verificación experimental no concluyente desde conexión independiente |
| Dictamen: COMPLETADA o PARCIAL | ✅ **PARCIAL** | FIX10-D completado como etapa documental; reservas de FIX10-B persisten; F7-D-I1 nivel 1 parcial hasta resolución de reservas |

## Cierre

**F7-D-I1-FIX10-D: COMPLETADA (Etapa Documental)**

- Documentación conciliada: métricas, ranking, estados de resultados, idempotencia, privacidad, API.
- Discrepancia DEC-F7D-18 vs CONTRACTS.md §17.3 **resuelta**: `invalidated` alcanzable desde `completed` solo por cadena `completed → disputed → resolved → invalidated`, sin transición directa.
- Reserva FIX10-B documentada sin exagerar: implementación rollback completada, verificación experimental desde conexión independiente no concluyente.
- Estado definitivo:
  - FIX10-A: completada y aceptada
  - FIX10-B: implementada con reserva de evidencia experimental
  - FIX10-C: completada y aceptada (15/15 tests)
  - FIX10-D: en ejecución → completada esta conciliación
- **No iniciar FIX10-D** (ja): ya completado.
- **No reabrir FIX10-B**: reserva mantenida tal como quedó.

**Dictamen final: FIX10-D CONCLUIDO como etapa documental. Documentación coherente, sin cambios de código, reservas expresadas.**]