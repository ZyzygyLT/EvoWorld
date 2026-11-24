# F7-E.4 — Contrato de integración de destacados en la galería pública
Fecha: 2026-11-24
Rama: feature/evoworld-review

## Estado

Contrato aprobado; implementación y verificación pendientes. Este documento registra las decisiones confirmadas para F7-E.4. No constituye evidencia de código implementado ni de pruebas ejecutadas.

## Decisiones aprobadas

1. Añadir exclusivamente el booleano derivado is_highlighted a las proyecciones públicas. No persistirlo en WorldRow ni exponer ID del destacado, fechas de concesión, identidad del administrador u otros metadatos.
2. Calcularlo desde la existencia de un destacado activo y vigente.
3. Consultar en la galería pública solo mundos públicos y no eliminados lógicamente.
4. Ordenar por is_highlighted DESC, id DESC.
5. Usar cursor opaco y versionado cuyo contenido lógico es (is_highlighted, id); el servidor valida su estructura y compatibilidad con los filtros.
6. Rechazar con HTTP 400 cursores inválidos o incompatibles.
7. Reiniciar paginación cuando cambie mine, search o mode.
8. Evaluar vigencia en cada página. No se promete snapshot entre páginas ni ausencia de duplicados/saltos si cambia el estado de destacados durante la navegación.
9. Mantener privacidad DEC20/DEC21, permisos HTTP/WS, reglas de elegibilidad/autoridad F7-E.3, historial F7-E.1 y eliminación lógica F7-E.2-I.
10. Un destacado no hace público un mundo privado ni concede permisos adicionales.
11. No incorporar rankings globales.

## Compatibilidad HTTP y WebSocket

El booleano se limita a las proyecciones públicas de galería y ficha pública equivalente, incluida la ficha pública enviada por WS. Esto no modifica las reglas de autorización ni habilita suscripciones de visitantes al runner. Las respuestas privadas de mine=1 conservan su forma vigente y no incorporan el campo por esta decisión.

## Criterios de aceptación

- La galería pública excluye mundos privados y eliminados.
- is_highlighted siempre es booleano y no revela metadatos.
- Destacados retirados o vencidos dejan de marcarse y priorizarse.
- El orden compuesto y la continuación del cursor son correctos.
- Cursores inválidos/incompatibles se rechazan con 400.
- Cambiar filtros obliga a reiniciar paginación.
- Retirada/expiración conserva historial.
- No cambian permisos HTTP/WS ni se revela estado vivo adicional.
- Se preservan autoridad y elegibilidad F7-E.3.

## Fuera de alcance

Implementación, migraciones, cambios de esquema, pruebas, UI, rankings globales y cualquier modificación de F7-E.1, F7-E.2-I o F7-E.3.

## Referencias

- DECISIONS.md: DEC-F7E-01.
- CONTRACTS.md: §18.
- DEC20/DEC21 y F7-E.1/F7-E.2-I/F7-E.3.
