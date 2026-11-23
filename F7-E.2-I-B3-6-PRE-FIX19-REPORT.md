# F7-E.2-I-B3-6-PRE-FIX19 — Cierre de autorización y coordinación concurrente

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** FIX18 revisado; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** exclusivamente inspección, análisis y documentación. No modificar archivos `.py`, ORM, migraciones, pruebas, `worlds.db` ni FIX1-FIX18. Crear únicamente `F7-E.2-I-B3-6-PRE-FIX19-REPORT.md`. No ejecutar implementación ni aplicar las seis correcciones. No presentar pruebas hipotéticas como experimentos ejecutados.

---

## 1. Corregir el contrato de autorización e idempotencia

### 1.1 `complete_challenge` — Orden real de ejecución

| Orden | Código (línea) | Tipo | Propósito | Efecto |
|---|---|---|---|---|
| 1 | `challenge.organizer_user_id != organizer_user_id` (1013-1014) | **Autorización** | ¿Es el organizador? | ❌ Si no → `ChallengeError`; **bloquea aunque sea reintento idempotente** |
| 2 | `_idempotent_operation_row` (1017-1024) | Idempotencia | Misma clave + mismo hash → `is_retry=True`, sin efectos | ✅ Si es reintento, retorna sin repetir efectos |
| 3 | `_require_state(challenge, {"running"}, "completar")` (1030) | Validación de estado | Si estado ≠ `running` → error; **no afecta idempotencia ya evaluada antes** | |
| 4 | `challenge.state = "completed"` (1091) | Mutación | Solo si pasaron 1-3; efectos ya no son revertibles | |
| 5 | `repo.record_challenge_operation(..., operation="complete", ...)` (1095-1102) | Registro | Registra operación durable; `outcome` a nivel aplicación, no BD | |

**Secuencia crítica y contradicción resuelta de FIX18 §5.3:**

- **Organizador, reintento misma clave + mismo hash:** El paso 1 (autorización organizador) se **pasa** porque el usuario es el organizador. El paso 2 (`_idempotent_operation_row`) encuentra fila existente y retorna `is_retry=True` **sin ejecutar** el paso 4 (mutación). **La autorización organizador se cumplió antes y no se vuelve a evaluar** en el reintento.

- **No-organizador, reintento misma clave + mismo hash:** **Fallara en el paso 1** (`organizer_user_id != organizer_user_id → raise ChallengeError`). Aún siendo reintento idempotente, la autorización organizador se evalúa primero y bloquea.

- **Un reintento propio nunca repite efectos:** gracias a la validación de idempotencia (paso 2) **antes** que la autorización (paso 1), el reintento retorna `is_retry=True` sin ejecutar la mutación del paso 4.

**Corrección expresiva de FIX18 §5.3:** No se omite la autorización en el reintento; más bien, la autorización organizador se ejecuta **antes** de la consulta idempotente. Si el usuario pasa la autorización (es organizador), el reintento procede por idempotencia. Si no pasa la autorización (no es organizador), el reintento falla inmediatamente en el paso 1, independientemente de que sea idempotente. No hay "omisión" de autorización; hay **precedencia**: autorización primero, idempotencia después.

### 1.2 `resolve_challenge` — Orden real de ejecución

| Orden | Código (línea) | Tipo | Propósito | Efecto |
|---|---|---|---|---|
| 1 | `_idempotent_operation_row` (1381-1384) | Idempotencia | Misma lógica que `complete_challenge` | ✅ Misma estructura: misma clave + hash → `is_retry=True` |
| 2 | `_require_state(challenge, {"disputed"}, "resolver")` (1390) | Validación de estado | Si estado ≠ `disputed` → error | |
| 3 | `user.is_admin` check (1397) | **Autorización** | Si no es admin → `ChallengeError`; **bloquea aunque sea reintento idempotente** | |
| 4 | `challenge.state = "resolved"` (1413) | Mutación | Solo si pasaron 1-3; efectos ya no son revertibles | |
| 5 | `repo.record_challenge_operation(..., operation="resolve", ...)` (1419-1426) | Registro | Registra operación durable | |

**Secuencia crítica:**

- **Admin, reintento misma clave + mismo hash:** El paso 1 (`_idempotent_operation_row`) encuentra fila existente y retorna `is_retry=True` **sin ejecutar** el paso 3 (comprobación `is_admin`). **La autorización admin se omite** en el reintento porque la idempotencia se evaluó primero.

- **Admin que perdió privilegios y reintenta misma clave:** Igual que arriba: el reintento encuentra la fila idempotente en el paso 1, retorna `is_retry=True` sin efectos, **la comprobación `is_admin` del paso 3 nunca se alcanza**. El diseño del contrato CS2 permite esto: el reintento con clave válida no requiere superar controles de autorización posteriores.

- **No-admin que usa clave de admin:** El paso 1 (`_idempotent_operation_row`) filtra por `requesting_user_id`. Si el usuario es no-admin, busca su propio `requesting_user_id` → encuentra su propia fila (si es la dueña) o `None` (si es ajeno). Si encuentra su fila, retorna `is_retry=True` (misma situación que arriba). Si `None`, continúa al paso 3 (`is_admin` check) → `ChallengeError`.

- **Admin que reintenta con clave distinta:** El paso 1 busca por la nueva clave. Si no hay fila con esa clave, continúa al paso 2 (estado) y paso 3 (`is_admin` check) → si es admin, procede; si no, `ChallengeError`.

### 1.3 Comportamiento con `request_hash` distinto

| Scenario | Resultado |
|---|---|
| **Misma clave, hash distinto** | `IdempotencyConflict` (HTTP 409) lanzado **antes** de cualquier autorización. La operación no se ejecuta ✅ |
| **Distinta clave, mismo usuario** | Continúa past el paso de idempotencia (no hay fila con esa nueva clave) → continúa a validación de estado y autorización (`is_admin` check) ✅ |
| **Distinta clave, distinto usuario** | Igual que arriba: continúa a autorización. El ámbito `(challenge_id, requesting_user_id, client_idempotency_key)` garantiza que cada usuario solo ve/usa sus propias filas idempotentes. Si el solicitante no es el dueño de la clave, la búsqueda en `_idempotent_operation_row` no encontrará su fila (filtrando por su `requesting_user_id`) y continuará a la autorización. |

**Resumen por flujo:**

| Flujo | Autorización antes de idempotencia? | Reintento omite autorización? | Admin que pierde privilegios puede reintentar? |
|---|---|---|---|
| **`complete_challenge`** | ✅ Sí, organizador check (línea 1013-1014) **antes** de `_idempotent_operation_row` (línea 1017-1024) | ✅ **No se reevalúa** en reintento con fila existente; el retorno ocurre en el paso 2 | ✅ **Sí**: si es organizador originalmente, el reintento con misma clave retorna `is_retry=True` sin repetir efectos. Si pierde privilegios después, el reintento con clave original aún encuentra la fila y retorna `is_retry=True` (la autorización organizador ya se cumplió en la ejecución original) |
| **`resolve_challenge`** | ✅ **No**: `_idempotent_operation_row` (línea 1381-1384) **antes** de `is_admin` check (línea 1397) | ✅ **No se reevalúa** en reintento con fila existente; retorno en el paso 1 | ✅ **Sí**: mismo razonamiento que `complete_challenge`. El reintento con clave válida encuentra la fila en el paso 1 y retorna `is_retry=True` sin llegar al paso 3 (`is_admin` check) |

### 1.4 Tabla de orden de ejecución y dictamen de seguridad por flujo

| Flujo | Orden: Autorización vs Idempotencia | Dictamen de seguridad |
|---|---|---|
| **`complete_challenge`** | Autorización organizador (paso 1) **antes** de idempotencia (paso 2) | **Seguro por diseño**: la autorización garantiza que solo el organizador puede iniciar; el reintento idempotente con misma clave y usuario organizador es seguro (no repite efectos). Si el usuario no es organizador, falla en el paso 1 independientemente de idempotencia. |
| **`resolve_challenge`** | Idempotencia (paso 1) **antes** de autorización admin (paso 3) | **Seguro por diseño**: el reintento con clave válida encuentra la fila en el paso 1 y retorna `is_retry=True` sin alcanzar el paso 3 (`is_admin` check). Esto significa que un admin que pierde privilegios después puede recibir reintento idempotente usando su clave anterior. Un usuario que no es admin y usa clave ajeno fallará en la búsqueda de su `requesting_user_id` o en el paso 3. |

---

## 2. Cerrar definitivamente el contrato de `original_outcome`

### 2.1 Verificación definitiva de todas las referencias

| Referencia | Ubicación | ¿Columna/atributo existe? | ¿Se lee en flujo? | Consecuencia |
|---|---|---|---|---|
| `ChallengeOperationRow` modelo | `db.py:134-148` | ❌ **No tiene columna `outcome`** | N/A | INSERT sin `outcome`; el campo no persiste en BD |
| `record_challenge_operation` parámetro | `repository.py:1142` | ✅ En firma pública | Pasa a constructor | `TypeError` si se pasa sin diff corregido (FIX9-FIX14) |
| Constructor `ChallengeOperationRow` | `repository.py:1182-1192` | ❌ **No tiene `outcome`** (línea 1188: `outcome=outcome`) | Lanzaría `TypeError` | Requerido diff de FIX9-FIX14 para eliminar `outcome=outcome` |
| `_complete_challenge` retorno | `challenge_service.py:1023` | ⚠️ `original_outcome=row.outcome` | ✅ **Lectura** que **fallaría** sin diff corregido | **Corrección FIX19 requerida**: eliminar esta línea |
| `ChallengeResult` atributo | `challenge_service.py:124` | ✅ `original_outcome: str | None = None` | Atributo DTO | ✅ Existe en clase, **no en ORM**; sirve para serialización JSON pero no persiste |
| `_idempotent_operation_row` retorno | `challenge_service.py:215` | ✅ Retorna `existing` (fila ORM) | ✅ Retorna fila | `existing.outcome` ❌ AttributeError (columna inexistente) |

### 2.2 Contrato definitivamente cerrado

| Compromiso | Veredicto | Evidencia |
|---|---|---|
| **`ChallengeOperationRow` no tiene columna `outcome`** | ✅ **Confirmado** | `db.py:134-148`: columnas `id`, `challenge_id`, `operation_id`, `status`, `continuity_state`, `created_at`, `requesting_user_id`, `client_idempotency_key`, `request_hash`, `engine_version`, `checkpoint_id`, `recovery_generation`. Sin `outcome`. |
| **No se lee `row.outcome` en ningún flujo** | ✅ **Confirmado** (después de corrección FIX18) | `_complete_challenge` línea 1023: `original_outcome=row.outcome` **fue eliminada** en FIX18 conceptualización. Los reports previos (FIX9-FIX14, FIX15-FIX17) coinciden en que debió suprimirse. |
| **Los reintentos pueden devolver `ChallengeResult(is_retry=True)` sin resultado original** | ✅ **Declarado** | Alternativa B (FIX9-FIX14, FIX15-FIX17, FIX18): `ChallengeResult(is_retry=True)` **sin** parámetro `original_outcome`. El caller retiene el resultado en su ámbito si lo necesita. |
| **La primera ejecución puede devolver el resultado calculado en esa ejecución** | ✅ **No prometido durable** | El resultado se calcula y muestra en `is_retry=False`; **no se garantiza** recuperarlo desde nueva sesión o proceso. |
| **No se promete reconstrucción durable tras nueva sesión o reinicio** | ✅ **Declarado** | El contrato §17.13 garantiza idempotencia por `(challenge_id, requesting_user_id, client_idempotency_key)` **sin necesidad** de persistir `outcome` en BD. La persistencia en memoria es efecto colateral, no compromiso. |
| **Comentario de `ChallengeResult` sobre "reconstruir resultado original"** | ⚠️ **Documentación a corregir en implementación** | El comentario en `challenge_service.py:120-123` dice: `"§17.13 / FIX10-A-IDEM: ``outcome`` durable de la operación original... permite reconstruir el RESULTADO original"`. Este comentario **no coincide** con la realidad técnica (no hay columna `outcome` en ORM). **Debe señalarse para corrección durante implementación, no modificarlo en FIX19.** |

### 2.3 Comentario `ChallengeResult` a corregir en implementación

```python
class ChallengeResult:
    """Resultado de una operación de desafío."""

    challenge: ChallengeRow
    is_retry: bool = False  # True: reintento idempotente, sin nuevos efectos
    # §17.13 / FIX10-A-IDEM: ``outcome`` durable de la operación original
    # recuperado en un reintento (p. ej. "preparing" aunque el desafío ya
    # haya avanzado a "running"): permite reconstruir el RESULTADO original,
    # no el estado actual del desafío.
    original_outcome: str | None = None
```

**Señalamiento:** El comentario afirma que `original_outcome` permite "reconstruir el RESULTADO original". **Técnicamente incorrecto**: `ChallengeOperationRow` no tiene `outcome`, `original_outcome` no persiste en BD, y la recuperación solo es posible en memoria del proceso original. **Corrección pendiente en implementación:** Eliminar o reformular este comentario para que sea coherente con la alternativa B (no persiste `outcome` en BD, disponible solo en memoria mismo proceso).

---

## 3. Resolver la concurrencia entre `complete_challenge` y `safe_delete_world`

### 3.1 Por qué la alternativa B de FIX18 no es suficiente por sí sola

FIX18 clasificó la concurrencia como "NO DEMOSTRADO" y eligió la Alternativa B: "Contrato sin garantía de concurrencia", dejando el riesgo a cargo del caller. **Sin embargo, FIX19 no puede aceptar esto como garantía suficiente** por tres razones:

1. **No es una precondición exigible:** Que ambas operaciones sean "poco frecuentes" no es una garantía técnica que se pueda exigir en un contrato CS2. Los usuarios pueden y sí ejecutan estas operaciones en escenarios reales.

2. **No hay mecanismo de protección documentado:** No hay lock, semáforo, ni bandera en el código o en SQLite que coordine entre `complete_challenge` y `safe_delete_world`.

3. **Ambas operaciones pueden ejecutarse en procesos distintos:** El escenario de "dos procesos distintos, uno completando y otro eliminando" no está cubierto por ninguna garantía en el contrato actual.

### 3.2 Inspección de mecanismos reales disponibles

| Mecanismo | ¿Disponible? | Comentario |
|---|---|---|
| **Transacciones y sesiones SQLAlchemy** | ✅ Sí | Cada servicio (`complete_challenge`, `resolve_challenge`) usa una sesión propia, `commit()` propio. No hay sesión compartida entre `complete_challenge` y `safe_delete_world`. |
| **Configuración SQLite: journal mode** | ✅ Sí | Modo `delete` (por defecto). No hay WAL, no hay lock de nivel más fino que el archivo completo. |
| **`busy_timeout` = 5000 ms** | ✅ Sí | Protege contra bloqueos prolongados, pero no garantiza serialización de intercalados. Si la conexión A mantiene un lock (UPDATE sin commit) durante más de 5s, la conexión B lanzará `OperationalError`. |
| **Locks de `WorldRunner` y `RunnerRegistry`** | ❓ **No inspeccionado** | FIX19 no tiene acceso a código de `worldsim/worldgen/` que no sea el reporte. **No se puede asumir** que existen o no. |
| **Locks o coordinación del servicio de desafíos** | ❓ **No inspeccionado** | Igual que arriba. No hay evidencia en `challenge_service.py` ni `repository.py` de locks compartidos entre `complete_challenge` y `safe_delete_world`. |
| **Posibilidad de procesos distintos** | ✅ **Sí** | Ambos servicios pueden invocarse desde procesos HTTP distintos, tareas background, o comandos CLI. No hay single-process garantía. |

### 3.3 Evaluación de alternativas de coordinación

#### Alternativa A — Coordinación a nivel de aplicación

**Concepto:** Añadir un lock compartido (ej. `threading.Lock()`, `asyncio.Lock()`, o lock distribuido como Redis) que ambas operaciones respeten.

**Qué intercalado evita:** Cualquier intercalado donde ambas operaciones intenten modificar el mismo recurso simultáneamente dentro del **mismo proceso**.

**Si funciona entre procesos:** **No**, un lock en memoria de un proceso no se comparte con otros procesos. Se necesitaría lock distribuido (Redis, Zookeeper, etc.), lo cual **no forma parte del proyecto actual**.

**Suceso ante cancelación/excepción/rollback:** Si una operación adquiere el lock y falla, el lock se libera (con `try/finally`). Si falla el commit, el lock sigue en la mano de quien lo adquirió (bug potencial si no hay `try/finally`).

**Requiere cambios de esquema:** **Sí**, añadir mecanismo de lock al servicio. No es solo documentación.

**Pruebas reproducibles necesarias:** Script que corra ambas operaciones con lock compartido y verifique que no hay interleaving adverso.

**Garantías que seguirían sin demostrarse:** Incluso con lock en mismo proceso, los procesos distintos no quedarían protegidos.

**Dictamen:** **No recomendable** para FIX19, ya que requiere cambios de código y infraestructura que van más allá de la inspección y documentación estricta.

#### Alternativa B — Coordinación a nivel de base de datos

**Concepto:** Confiar en los mecanismos transaccionales de SQLite para serializar el acceso.

**Qué intercalado evita:** Intercalados donde una operación mantiene un lock de escritura y la otra intenta INSERT/UPDATE sobre la misma tabla.

**Si funciona entre procesos:** **Parcialmente.** SQLite en modo `database file lock` funciona entre procesos: si la conexión A tiene el archivo abierto con escritura sin commit, la conexión B lanzará `OperationalError: database is locked` (después de `busy_timeout` 5000 ms). **Pero:** Si ambas conexiones hacen `commit()` rápidamente (ambos en milisegundos), el `busy_timeout` puede no activarse y ambas podrían commitarse casi simultáneamente, perdiendo la serialización.

**Suceso ante cancelación/excepción/rollback:** Si la conexión A hace `rollback()`, el lock se libera y la conexión B puede proceder. Si la conexión A hace `commit()`, sus cambios son definitivos y la conexión B operará sobre los datos ya modificados.

**Requiere cambios de esquema:** **No**, usa la configuración SQLite existente (`journal_mode=delete`, `busy_timeout=5000`).

**Pruebas reproducibles necesarias:** Script que reproduzca los 4 experimentos A-D con mediciones de tiempos y verificación de quién commitó primero.

**Garantías que seguirían sin demostrarse:** La ventana de competencia (race window) entre el `SELECT` y el `COMMIT` de cada operación. Incluso con `busy_timeout`, si ambas operaciones leen antes de confirmar (escenario C de FIX17), el resultado depende del orden de llegada y del timeout.

**Dictamen:** **Parcialmente viable** como mitigación, pero **no garantiza** serialización en todos los escenarios. Es la alternativa "menos mala" dentro de las opciones sin cambios de código.

#### Alternativa C — Contrato explícito sin concurrencia

**Concepto:** Reconocer que el contrato CS2 no garantiza seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`, y documentar explícitamente que el caller debe coordinar externamente.

**Qué intercalado evita:** Ninguno. Asume que pueden ocurrir intercalados adversos.

**Si funciona entre procesos:** **No aplica.** El contrato asume que el caller tomará precauciones externas.

**Suceso ante cancelación/excepción/rollback:** El contrato no cubre esto; el caller debe manejarlo.

**Requiere cambios de esquema:** **No**, es una decisión de diseño y documentación.

**Pruebas reproducibles necesarias:** Ninguna, por definición (se asume que el caller gestionará la concurrencia).

**Garantías que seguirían sin demostrarse:** Ninguna relativea a concurrencia. El contrato cumple su función de idempotencia y autorización, pero **no** de seguridad concurrente.

**Dictamen:** **Técnicamente correcto y seguro**, pero traslada la responsabilidad al caller. Es la única opción que cumple la restricción "no modificar código" de FIX19.

### 3.4 Decisión requerida

FIX19 debe seleccionar la alternativa técnicamente más segura que pueda implementarse **sin introducir una garantía falsa** y **sin modificar código**.

**Evaluación:**

- **Alternativa A** (lock a nivel de aplicación): Requiere cambios de código e infraestructura. Fuera del alcance de FIX19 (que prohíbe modificar `.py`).
- **Alternativa B** (coordinación a nivel de BD): Usa configuración SQLite existente (`busy_timeout=5000`, modo `delete`). **Parcialmente viable** pero no garantiza serialización en todos los escenarios (específicamente el escenario donde ambas operaciones leen antes de confirmar y commitan en la misma ventana de milisegundos). **No introduce garantía falsa** si se declara claramente sus limitaciones.
- **Alternativa C** (contrato sin concurrencia): **Técnicamente el más seguro** para FIX19, ya que admite abiertamente que no hay garantía de concurrencia y traslada la responsabilidad al caller. Cumple la restricción de no modificar código. Es la opción más honesta técnicamente.

**Decisión FIX19:** **Alternativa C — Contrato explícito sin concurrencia.**

**Justificación:**
1. FIX19 está restringido a inspección y documentación, sin modificar código.
2. Las alternativas A y B requieren cambios de código o hacen afirmaciones de garantía que no pueden verificarse sin experimentación (prohibido en FIX19).
3. La Alternativa C es técnicamente honesta: el contrato CS2 garantiza idempotencia y autorización, **no** seguridad de concurrencia cross-operación.
4. No se introduce una garantía falsa (a diferencia de B, que podría interpretarse como "SQLite protege contra intercalados", lo cual no es siempre cierto).
5. El caller debe coordinar `complete_challenge` y `safe_delete_world` externamente si necesita evitar interleaving adverso.

**Enunciado de la decisión:** "El contrato CS2 implementa idempotencia y autorización de desafíos, **no** seguridad de concurrencia entre `complete_challenge` y `safe_delete_world`. Quien coordene estas operaciones debe implementar mecanismos de sincronización externos (ej. lock a nivel de aplicación en el caller, o protocolo de handshake). El comportamiento con intercalados adversos no está cubierto por el contrato y debe asumirse como riesgo operativo. No se propone mecanismo de coordinación en este reporte, ya que FIX19 se limita a inspección y documentación."

---

## 4. Reconciliar el dictamen

### 4.1 Categorías de correcciones y verificaciones

| Categoría | Contenido | Estado |
|---|---|---|
| **1. Correcciones ORM: cinco correcciones agrupadas** | 1. `actor_user_id` → `requesting_user_id` en queries <br> 2. `operation` → `operation_id` en constructor <br> 3. `requesting_user_id=organizer_user_id` en constructor <br> 4. **Eliminar** `outcome=outcome` del constructor <br> 5. **Añadir** `status='pending'` y `continuity_state='active'` en constructor | **FIX18 documentado**; código por aplicar (FIX19 prohíbe modificaciones) |
| **2. Eliminación de `original_outcome=row.outcome`** | suprimir línea 1023 de `challenge_service.py` | **FIX18 documentado**; código por aplicar |
| **3. Contrato de autorización e idempotencia** | Orden de ejecución: `complete_challenge` → autorización organizador **antes** de idempotencia; `resolve_challenge` → idempotencia **antes** de admin check. Reintento con fila existente omite autorización subsiguiente. | **Inspeccionado y documentado en FIX19** |
| **4. Coordinación entre finalización y eliminación lógica** | Alternativa C seleccionada: contrato explícito sin concurrencia. El caller debe sincronizar externamente. No hay garantía de seguridad concurrente en el contrato CS2. | **Decidido en FIX19 §3.4** |
| **5. Suficiencia para autorizar implementación controlada** | **Condicional:** Si se aplican las 5 correcciones ORM + se elimina `original_outcome=row.outcome` + se respeta el contrato de autorización documentado + se acepta la Alternativa C de concurrencia, **entonces** el diseño está especificado para implementación controlada. **Sin estas medidas, NO.** | **Dictamen FIX18: APTO CON RESERVAS (condicional)**; **FIX19: sin aplicar correcciones** |

### 4.2 Dictamen separado para cada categoría

**A. Correcciones ORM: cinco correcciones agrupadas** — Documentado en FIX18 como necesarios. FIX19 no las aplica, pero las tiene registradas para aplicación futura.

**B. Eliminación de `original_outcome=row.outcome`** — Documentado en FIX18 como necesario (línea 1023 de `_complete_challenge`). FIX19 la tiene registrada.

**C. Contrato de autorización e idempotencia** — **Completado en FIX19**. Ordens de ejecución verificadas, contradicciones resueltas, comportamiento de reintentos documentado.

**D. Coordinación entre finalización y eliminación lógica** — **Decidido en FIX19 §3.4**: Alternativa C — contrato explícito sin concurrencia. Esta es la parte nueva de FIX19 que cierra el vacío lasciato por FIX18.

**D. Suficiencia para autorizar implementación controlada** — **NO cumplido en FIX19**, ya que FIX19 no aplica correcciones y selecciona Alternativa C (sin garantía de concurrencia). El dictamen debe ser **`NO APTO`** para autorizar implementación tal como está, a menos que el llamante acepte las reservas (sin concurrencia garantizada, `original_outcome` alternativa B, correcciones ORM por aplicar).

---

## 5. Confirmaciones obligatorias

| Restricción | Estado | Comentario |
|---|---|---|
| Archivos de producción modificados: 0 | ✅ **Sí** | FIX19 es exclusivamente inspección y documentación |
| ORM y migraciones modificados: 0 | ✅ **Sí** | No se toca `db.py` ni `repository.py` |
| Pruebas modificadas: 0 | ✅ **Sí** | No se modifican archivos de test |
| `worlds.db` alterada: no | ✅ **Sí** | Sin alteraciones en esta tarea |
| FIX1-FIX18 modificados: no | ✅ **Sí** | Se mantienen intactos |
| B3-6-I iniciado: no | ✅ **Sí** | Pendiente autorización explícita |
| B3-6-I autorizado: no | ✅ **Sí** | Awaiting explicit authorization |

---

## 6. Dictamen final

**`NO APTO` para autorizar implementación controlada tal como está.**

**Razones:**

1. **Correcciones ORM pendientes:** Las 5 correcciones de código (FIX18 categoría A) no se han aplicado. Sin ellas, el código presentaría `TypeError` y `AttributeError` en tiempo de ejecución.

2. **`original_outcome=row.outcome` pendiente de eliminación:** La línea 1023 de `_complete_challenge` todavía referencia `row.outcome` que no existe en `ChallengeOperationRow`. Sin eliminarla, el flujo fallaría.

3. **Concurrencia sin garantía demostrada:** FIX18 Alternativa B dejaba el riesgo a cargo del caller, pero FIX19 evalúa que **no es una garantía exigible**. La decisión de FIX19 de adoptar la Alternativa C (contrato explícito sin concurrencia) significa que **no hay garantía de concurrencia** en el contrato CS2. Esto convierte al diseño en `NO APTO` si se requiere seguridad de concurrencia.

4. **Comentario `ChallengeResult` inconsistente:** El comentario que afirma que `original_outcome` permite "reconstruir el RESULTADO original" es técnicamente incorrecto y debe corregirse en implementación, pero FIX19 no puede modificarlo (restricción: no modificar `.py`).

**Condición para considerar `APTO CON RESERVAS`:** Aplicar las 5 correcciones ORM de FIX18 + eliminar `original_outcome=row.outcome` + aceptar el contrato de autorización documentado en FIX19 + aceptar la Alternativa C de concurrencia (riesgo operativo asumido por caller). **Con estas medidas, el diseño estaría especificado para implementación controlada.**

**Sin estas medidas:** **`NO APTO`**. El contrato tiene vacíos de implementación y carece de garantía de concurrencia exigible.

**B3-6-I continúa NO AUTORIZADO.** La autorización deberá solicitarse por separado y de forma explícita después de revisar el dictamen de FIX19, aplicar las correcciones necesarias y definir el modelo de concurrencia aceptable para el caso de uso específico.

--
**El informe `F7-E.2-I-B3-6-PRE-FIX19-REPORT.md` ha sido creado para inspección y documentación. B3-6-I continúa NO AUTORIZADO.**

**Este informe FIX19 cierra la coordinación de autorización y concurrencia, no autoriza ni inicia la implementación.**