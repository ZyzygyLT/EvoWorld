# Uso local del archivo histórico EvoWorld

Este procedimiento no restaura mundos ni modifica bases de datos. Trabaja con el snapshot histórico preservado fuera del repositorio y genera un JSON local que el navegador lee únicamente cuando el usuario lo selecciona.

## 1. Actualizar la rama

Desde la carpeta del repositorio:

```powershell
git fetch origin
git switch feature/historical-archive-local
git pull --ff-only origin feature/historical-archive-local
```

## 2. Exportar el snapshot histórico más completo

Usar el snapshot más reciente con datos, que contiene los ocho registros. No pasar los snapshots anteriores en la misma ejecución: son versiones históricas previas de la base y podrían repetir IDs/estados de mundos.

```powershell
py tools/export_historical_archive.py `
  --snapshot "$env:USERPROFILE\Documents\evoworld-recovery-review\worlds-a86fced2ee.db" `
  --out "$env:USERPROFILE\Documents\evoworld-historical-archive.json"
```

El exportador abre el SQLite con `mode=ro`, ejecuta `integrity_check`, valida la estructura de los ocho estados y calcula SHA-256. Los mapas legacy sin `world.elevation` son compatibles: el visor usa su representación neutral, sin generar datos de elevación. Si cualquier fila no cumple el formato, cancela toda la exportación.

El JSON resultante contiene datos históricos. Mantenerlo fuera del repositorio y no subirlo a GitHub ni compartirlo públicamente.

## 3. Ejecutar las pruebas

```powershell
py -m pytest -q tests/test_historical_archive_export.py
cd web
npm run test:archive
npm run build
```

## 4. Abrir el archivo en la interfaz

Iniciar FastAPI y Vite como de costumbre. En la pantalla inicial, elegir **Archivo local**, seleccionar `evoworld-historical-archive.json` y abrir uno de los mundos.

El fichero se procesa localmente en el navegador. No se realiza una petición API con sus datos. Al cerrar o recargar la pestaña, habrá que volver a seleccionar el archivo.

## 5. Verificación de seguridad

Antes de confirmar el resultado:

- comprobar que el SHA-256 del snapshot sigue siendo el mismo;
- comprobar que `worlds.db` no cambió;
- comprobar que el JSON y los SQLite siguen fuera del repositorio;
- revisar `git status --short` y confirmar que no aparecen bases de datos ni paquetes de archivo;
- no hacer commit del JSON generado.
