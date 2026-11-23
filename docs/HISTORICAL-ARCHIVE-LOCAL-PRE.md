# Archivo histórico local — PRE

Estado: DISEÑO PRELIMINAR  
Rama: `feature/historical-archive-local`  
Base: `feature/evoworld-review`  
Alcance: visualización privada, local y de solo lectura de snapshots históricos.

## 1. Objetivo

Permitir abrir los ocho mundos preservados en snapshots SQLite históricos y consultar sus datos originales mediante una vista de archivo. La solución no debe reanimar ni reconstruir el motor de simulación.

## 2. Límites no negociables

- No escribir, migrar, renombrar ni reemplazar ningún snapshot histórico.
- No leer ni escribir la base operativa `src/worldsim/server/worlds.db` como parte del archivo.
- No insertar mundos históricos en `worlds`, `world_checkpoints` ni otras tablas actuales.
- No añadir los archivos SQLite históricos, sus JSON exportados ni datos de mundos al repositorio Git.
- No crear endpoints HTTP públicos/privados para entregar los datos históricos.
- No conectar el archivo a WebSocket, runners, claims, standings, chat, predicciones ni controles de simulación.
- No reconstruir `SimEngine`, no ejecutar `generate_world` para simular una restauración y no fabricar campos ausentes.
- No presentar métricas derivadas como si fueran datos históricos originales.
- La importación debe ser explícita por el usuario desde un archivo local. Los datos solo viven en memoria del navegador durante la sesión; no se guardan en localStorage ni IndexedDB.

## 3. Arquitectura propuesta

### 3.1 Exportador local de solo lectura

Añadir una herramienta CLI independiente que reciba una ruta explícita a uno o varios snapshots SQLite y produzca un único paquete JSON portable en una ruta de salida elegida por el usuario.

Requisitos:
- Abrir SQLite con URI `mode=ro` y verificar que el archivo fuente no cambia.
- Aceptar únicamente el esquema histórico inspeccionado y columnas esperadas; no asumir que el esquema actual es compatible.
- Leer únicamente `worlds` y sus campos históricos necesarios: `id`, `seed`, `months_simulated`, `land_pct`, `created_at`, `state_json` (y `name` solo si existe).
- Validar cada `state_json`: JSON objeto, dimensiones, matrices, longitudes y tipos antes de exportar.
- Conservar el estado histórico original; incluir metadatos de procedencia (hash SHA-256 del snapshot, identificador del commit fuente, ID de fila) sin modificar el payload.
- Versionar el formato del paquete, por ejemplo `format = "evoworld-historical-archive"` y `format_version = 1`.
- No incluir credenciales, usuarios ni tablas ajenas a `worlds`.
- Fallar de forma cerrada ante esquema o estructura no reconocidos; no omitir filas silenciosamente.

### 3.2 Importación en el navegador

Añadir una entrada independiente desde la pantalla inicial para seleccionar el paquete JSON local. Validar versión, estructura y límites antes de aceptarlo. No hacer `fetch`, llamadas API ni carga automática desde el servidor.

La UI debe:
- Mostrar los mundos incluidos y su procedencia.
- Abrir un mundo solo tras selección explícita.
- Permitir volver a la lista del archivo.
- Etiquetar claramente que es un snapshot histórico estático y no una simulación recuperada.
- Mantener el paquete únicamente en estado React/memoria; al recargar o cerrar la pestaña, se vuelve a seleccionar el archivo.

### 3.3 Adaptador de presentación

Crear un adaptador puro y probado que traduzca solo campos existentes al contrato mínimo que necesita la visualización.

- Reutilizar `WorldMap` solo para el mapa base y overlays que puedan sustentarse con datos originales.
- El mapa base puede usar `world.biome`, `world.rivers` y `world.elevation` tras validar dimensiones y orientación.
- Los overlays de influencia, batallas y marcadores políticos se habilitan únicamente cuando su estructura histórica se valida; si no, se deshabilitan.
- Normalizar reinos/sitios para el renderizado sin inventar atributos ausentes.
- Reutilizar `ChroniclePanel` únicamente si los eventos cumplen el contrato `month/type/text`; el texto histórico original debe ser fallback.
- No reutilizar `Sidebar` ni `StatsPanel` para rankings o estadísticas que requieren campos modernos ausentes.
- No ofrecer `Timeline`: los snapshots disponibles son puntos estáticos, no una serie temporal.
- Deshabilitar selección interactiva, claims, seguimiento, foco de guerra y acciones de juego en el modo archivo.

## 4. Privacidad y exposición

El origen histórico no contiene información suficiente para demostrar titularidad actual de cada mundo. Por ello:
- El paquete no se publica ni se incorpora a la galería.
- El paquete no se despliega ni se sirve desde FastAPI.
- El archivo solo se abre desde un fichero que el usuario selecciona localmente.
- No se crea enlace compartible ni mecanismo de exportación desde la UI en esta primera versión.

## 5. Archivos previstos para implementación

- `tools/export_historical_archive.py`: exportador SQLite read-only.
- `tests/test_historical_archive_export.py`: pruebas con SQLite temporal y datos sintéticos.
- `web/src/components/HistoricalArchive.jsx`: selector, lista y visor local.
- `web/src/historicalArchive.js`: validación de paquete y adaptador puro.
- `web/src/App.jsx`: integración mínima de entrada/salida del modo archivo, sin alterar `openStream` ni el flujo normal.
- Pruebas frontend si el repositorio ya dispone de infraestructura adecuada; si no, pruebas unitarias del adaptador con el runner ya instalado o validación de build.

No se añadirá ningún snapshot ni JSON real a estos archivos.

## 6. Pruebas y criterios de aceptación

1. Exportar desde una copia de snapshot en modo read-only; verificar SHA-256 antes/después idéntico.
2. Rechazar DB corrupta, esquema inesperado, JSON inválido, payload no objeto, matrices truncadas y dimensiones inconsistentes.
3. Exportar los ocho registros sin pérdida de filas ni cambios en el payload.
4. Abrir un paquete válido desde el selector local y navegar entre sus mundos.
5. Rechazar paquete con versión desconocida o estructura manipulada.
6. Verificar por pruebas/código que el modo archivo no llama a `/api`, no abre WebSocket y no ejecuta acciones de escritura.
7. Verificar que el flujo normal de galería, `openStream`, autenticación y simulación sigue intacto.
8. Ejecutar suite backend afectada y `npm run build`.
9. Confirmar que `worlds.db`, snapshots y datos personales no aparecen en el diff ni en Git.

## 7. Riesgos y decisiones abiertas

- Antes del exportador final, confirmar el esquema exacto de cada snapshot y las claves/longitudes reales de los ocho payloads.
- Las estructuras históricas de reinos, guerras, bestias e influencia no son equivalentes al estado moderno. El adaptador debe ser conservador y mostrar solo información comprobada.
- El mapa histórico puede no disponer de todos los overlays modernos; degradar a mapa de biomas en lugar de sintetizar datos.
- La UI debe dejar explícito que no existe reproducción mensual ni restauración de la simulación.

## 8. Fuera de alcance

- Restaurar mundos en el motor actual.
- Importar datos a `worlds.db`.
- Recuperar checkpoints exactos, RNG, estado interno o historial mensual.
- Hacer públicos los mundos o asignarles propietarios.
- Rankings, comparativas oficiales o estadísticas recalculadas.
- Cambios a DEC19, F7-C, F7-D, F7-E.1 o contratos de borrado lógico F7-E.2.
