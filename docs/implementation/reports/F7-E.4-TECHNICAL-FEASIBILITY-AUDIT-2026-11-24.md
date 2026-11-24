# F7-E.4 — Auditoría de viabilidad técnica
Fecha: 2026-11-24
Rama: feature/evoworld-review
HEAD inspeccionado: 00e96afc9c1180056c8ad68e3cabedb9f55588cf

## 1. Dictamen ejecutivo

**Viable con trabajo nuevo acotado; no está implementado.** La arquitectura existente permite integrar el indicador derivado y el cursor compuesto sin reemplazar la galería ni alterar el motor de simulación. Sin embargo, no existe actualmente infraestructura de destacados en el código inspeccionado: no se encontró modelo/tabla WorldHighlight, repositorio, servicio, endpoints administrativos ni lógica de consulta del indicador.

La implementación debe tratarse como una extensión coordinada de F7-E.1/F7-E.3 y F7-B, no como un cambio aislado de ordenación.

## 2. Evidencia inspeccionada

- src/worldsim/server/app.py:
  - GET /api/worlds usa cursor entero, orden id DESC y consulta SQL con limit+1.
  - La consulta pública filtra visibility='public' y deleted_at IS NULL.
  - La proyección selecciona columnas explícitas y no carga state_json.
  - _public_ficha construye la allowlist campo por campo.
  - _public_detail reutiliza _public_ficha para el detalle público HTTP.
  - El WebSocket de visitante público envía un único mensaje derivado de _public_detail y no suscribe al visitante al runner.
  - No se encontró ruta de administración de destacados ni referencia funcional a highlights.
- src/worldsim/server/db.py:
  - WorldRow ya tiene visibility y deleted_at.
  - WorldCheckpoint contiene is_valid, checksum, payload, sequence y checkpoint_version.
  - No se encontró modelo WorldHighlight ni tabla equivalente declarada.
  - ensure_schema ejecuta Base.metadata.create_all dentro de transacciones SQLite/PostgreSQL y luego aplica migraciones explícitas para columnas existentes.
- src/worldsim/server/repository.py:
  - CheckpointRepository.get_latest_valid_checkpoint verifica SHA-256, pero marca checkpoints corruptos con is_valid=0 y persiste esa invalidación antes de devolver.
  - Por tanto, este método NO es apto para una evaluación de elegibilidad no destructiva.
  - Existe acceso de solo lectura a los checkpoints y puede implementarse una verificación independiente que consulte is_valid y compare SHA-256 sin modificar filas ni invocar recuperación.
- src/worldsim/export/checkpoint.py:
  - Existe serialización/restauración versionada (CHECKPOINT_VERSION=2), separada del flujo de galería.
- tests:
  - Existen suites de privacidad, checkpoints y eliminación lógica.
  - No se encontró una suite específica de destacados ni de cursor compuesto F7-E.4.

## 3. Encaje técnico recomendado

### 3.1 Persistencia de destacados

Crear el modelo SQLAlchemy y la tabla conforme al contrato aceptado de F7-E.1, sin modificar WorldRow con un booleano persistido.

La tabla debe conservar el historial y proteger la referencia al mundo con la política de retención acordada (ON DELETE RESTRICT). El índice único parcial por mundo para el registro activo debe declararse de forma compatible con SQLite y PostgreSQL.

La creación de una tabla nueva encaja con Base.metadata.create_all; aun así, deben añadirse pruebas de esquema/idempotencia para ambos dialectos y verificar explícitamente el DDL real del índice parcial.

### 3.2 Cálculo de is_highlighted

Implementarlo como una expresión SQL derivada de EXISTS sobre el registro de destacado activo y vigente, proyectada junto con las columnas de la galería. Reutilizar esa expresión tanto para proyectar el booleano como para ordenar, evitando divergencias entre etiqueta y prioridad.

No hacer una consulta por mundo (N+1), no cargar state_json y no calcular el indicador mediante serialización del modelo.

Incluir el campo solo en las proyecciones públicas: galería pública y ficha pública HTTP/WS. La consulta privada mine=1 debe conservar su forma actual. El detalle del propietario que devuelve el estado completo no debe alterarse.

### 3.3 Orden y cursor

Sustituir el punto de corte entero por un cursor opaco/versionado con la posición lógica (is_highlighted, id), manteniendo limit+1 y aplicando filtros/visibilidad en SQL antes de paginar.

El predicado de continuación debe respetar exactamente el orden is_highlighted DESC, id DESC. El cursor debe validarse antes de ejecutar la consulta y vincularse a la consulta normalizada para rechazar su reutilización con filtros incompatibles.

La consulta debe aplicar siempre visibility='public' y deleted_at IS NULL, independientemente del contenido del cursor. El cursor nunca es una autorización.

### 3.4 WebSocket y privacidad

El camino existente para visitantes públicos ya reutiliza _public_detail y no crea ni suscribe un runner. Añadir el campo derivado a esa proyección pública puede mantener la misma frontera de privacidad.

No modificar el camino del propietario, la suscripción, los mensajes de tick, estadísticas, control del runner ni las reglas 4404/4409. Las pruebas deben demostrar que el visitante recibe una sola ficha pública y nunca datos vivos adicionales.

### 3.5 Elegibilidad F7-E.3

La comprobación de elegibilidad debe ser de solo lectura y no llamar RecoveryManager.recover() ni CheckpointRepository.get_latest_valid_checkpoint(), porque este último invalida y persiste checkpoints corruptos.

Se necesita una consulta read-only de candidatos con is_valid=1 y una comparación SHA-256 sobre los bytes almacenados. Debe definirse qué candidato cuenta si el checkpoint más reciente está corrupto y existe uno anterior íntegro; la opción compatible con el repositorio actual es recorrerlos por sequence DESC y aceptar el primero que pase checksum, sin mutar filas.

La concesión administrativa debe revalidar en la misma transacción la visibilidad pública, owner_user_id, deleted_at, status y el checkpoint elegido. Deben considerarse las carreras entre elegibilidad y concesión; el mecanismo de bloqueo/transacción debe cubrir SQLite y PostgreSQL sin recuperar ni avanzar el mundo.

## 4. Hallazgos que requieren resolución antes de implementar

### H1 — El cursor debe incluir también limit

CONTRACTS.md §15.2 establece expresamente que cambiar cualquiera de mine, search, mode o limit obliga a reiniciar la paginación. DEC-F7E-01 y la nueva §18 mencionan mine, search y mode, pero omiten limit en la vinculación/compatibilidad del cursor.

**Acción requerida:** corregir DEC-F7E-01 y CONTRACTS §18 para incluir limit en el contexto del cursor. No relajar §15.2.

### H2 — Destacado vencido y unicidad parcial

El contrato F7-E.1 define un índice único parcial para el registro activo por mundo y una expiración consultada mediante expires_at. Un registro vencido que conserve is_active=true puede dejar de mostrarse como destacado, pero seguir ocupando el índice único e impedir conceder un nuevo destacado al mismo mundo.

**Acción requerida:** definir en el flujo administrativo de concesión una transición atómica que retire/desactive el registro vencido anterior, conservándolo como historial, antes de crear el nuevo. La galería debe seguir siendo read-only y no limpiar expiraciones durante un GET. No modificar ni borrar registros históricos.

### H3 — Semántica exacta del checkpoint elegible

El método existente get_latest_valid_checkpoint no es no destructivo. La decisión F7-E.3 exige evaluación no destructiva.

**Acción requerida:** documentar explícitamente si la elegibilidad acepta el checkpoint íntegro más reciente entre los registros is_valid=1, y comprobarlo con una función read-only independiente. No reutilizar el método mutante ni RecoveryManager.recover().

## 5. Matriz de impacto

| Área | Estado actual | Trabajo necesario |
|---|---|---|
| Modelo/tabla de destacados | No encontrado | Modelo, tabla, FK e índice parcial |
| Servicio administrativo | No encontrado | Conceder/retirar con autorización real is_admin |
| Elegibilidad | Sin servicio específico encontrado | Verificación read-only y transacción de concesión |
| Galería pública | Implementada, id DESC | EXISTS, proyección booleana y orden compuesto |
| Cursor | Entero por ID | Cursor opaco/versionado y validación de filtros |
| Detalle público HTTP | Reutiliza _public_ficha | Campo derivado público |
| WS de visitante | Una ficha pública, sin runner | Campo derivado sin cambiar el flujo |
| Eliminación lógica | deleted_at filtrado en galería | Mantener filtro; retención FK RESTRICT |
| Privacidad | Allowlist explícita DEC20 | Ampliación estricta con un booleano público |
| Pruebas | Suites base presentes | Nuevas pruebas F7-E.4 y regresión |

## 6. Plan de implementación sugerido

1. Resolver H1–H3 y actualizar el contrato antes de tocar código.
2. Implementar modelo, migración/creación de tabla e índices de destacados con pruebas SQLite y PostgreSQL.
3. Implementar servicio de elegibilidad read-only y operaciones administrativas transaccionales.
4. Implementar proyección SQL del booleano y cursor compuesto en galería pública.
5. Integrar el mismo booleano en ficha pública HTTP/WS sin alterar permisos.
6. Añadir pruebas unitarias, integración, concurrencia, privacidad y regresión F7-B/F7-E.2-I/F7-E.3.
7. Ejecutar regresión apropiada y solicitar auditoría final/aceptación separada.

## 7. Límites de esta auditoría

- Se inspeccionó el contenido de la rama GitHub indicada; no se modificó código ni esquema.
- No se ejecutaron pruebas ni se conectó a una base de datos.
- No se inspeccionaron bases locales ni worktrees protegidos.
- No se creó migración, modelo, endpoint, servicio ni cambio de frontend.
- Esta auditoría no autoriza implementación.

## 8. Dictamen final

**F7-E.4 es técnicamente viable**, reutilizando la consulta SQL explícita de la galería, la allowlist pública y el camino público existente de HTTP/WS. No requiere modificar el motor de simulación.

**No debe comenzar la implementación todavía** hasta resolver H1 (cursor y limit), H2 (reconcesión tras expiración conservando historial) y H3 (selección no destructiva del checkpoint elegible). Estas son precisiones acotadas y no exigen reabrir F7-E.1, F7-E.2-I ni F7-E.3; preservan sus contratos y aclaran cómo implementarlos conjuntamente.
