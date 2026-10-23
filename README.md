# Buscador de marcas

Este servicio consulta el formulario público del acervo del IMPI. No es una API oficial del instituto y sus resultados no garantizan el registro de una marca.

## Qué cambió en la versión 8.1

La búsqueda envía el mismo formulario público con las cookies de sesión y el ViewState que entrega el IMPI. No ejecuta JavaScript, no abre Chromium para buscar y no intenta saltar verificaciones de acceso. Esto reduce la carga del servidor y evita esperar a que el navegador dibuje cientos de logotipos.

Una búsqueda tiene un presupuesto total de 65 segundos. Si el IMPI no responde, cambia el formulario o no entrega una tabla válida, el servicio responde con un error. Nunca convierte esos casos en cero coincidencias.

El cliente debe comprobar `consulta.estado === "completada"` y `consulta.completa === true`. Solamente entonces puede utilizar los grupos de resultados. Incluso una búsqueda correctamente completada sin coincidencias no equivale a una autorización de registro.

Los registros se leen por los encabezados de las columnas; no por posiciones fijas. Las coincidencias de similitud baja se conservan. Si el portal empieza a entregar varias páginas, el servicio rechaza una respuesta parcial en lugar de presentarla como completa.

## Iniciar en local

Probado con Python 3.12. En PowerShell, desde esta carpeta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Chromium solamente se utiliza para obtener expedientes PDF; no hace falta para buscar ni generar Excel. No se necesita una clave del IMPI ni una clave de Supabase para este servicio.

## Probar

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Estas pruebas usan datos simulados y una base SQLite en memoria. No consultan al IMPI. Las pruebas reales dependen de la disponibilidad del portal y deben hacerse por separado.

`GET /health` debe responder `status: ok`, `version: 8.1` y `search_contract: 2`. Esto confirma que el bot está encendido, no que el IMPI esté disponible.

Para comprobar una consulta real, envía a `POST /api/buscar`:

```json
{"denominacion":"INGENICCA","clase":"41"}
```

La respuesta válida contiene `consulta`, `metricas` y `resultados`. Si recibes 502 o 504, no debe mostrarse disponibilidad de la marca ni generarse una conclusión de ausencia de antecedentes.

## Publicar en Render y conectar la app

1. Publica primero este repositorio. Render debe reconstruir la imagen Docker; no basta con reiniciar la imagen anterior. El Dockerfile ya instala `requirements.txt` e incluye Chromium.
2. Comprueba `/health`: debe mostrar la versión 8.1 y el contrato 2.
3. En el backend de IngeniariCCa, conserva `IMPI_API_URL=https://impi-bot.onrender.com` y configura `IMPI_TIMEOUT_MS=120000`. Este margen incluye el arranque de la instancia gratuita. No aumenta los 65 segundos de trabajo del bot.
4. Publica después el conector y los clientes actualizados. El cliente web tiene un límite mayor al del backend.
5. Haz una consulta real desde la plataforma. Revisa tanto un resultado correcto como un error; ninguno debe inventar disponibilidad.

La variable opcional `IMPI_SEARCH_TIMEOUT_SECONDS` acepta de 10 a 70 segundos; por defecto son 65. No la aumentes para ocultar un fallo.

En Render, los registros incluyen `request_id`, paso y tiempo transcurrido. Los pasos son `open_portal`, `submit_search`, `read_results`, `complete` y `failed`. No registran la marca, las cookies ni el ViewState.

## Descargas y límites

- Se atiende una búsqueda o descarga de PDF a la vez. Si está ocupado, devuelve 503 y `Retry-After: 15`.
- Los PDF se piden manualmente, hasta diez expedientes por solicitud. El lote tiene un presupuesto compartido y avisa si sólo obtuvo parte de los archivos. Se verifica la cabecera PDF antes de publicar el archivo.
- Excel admite hasta 500 registros. No ejecuta fórmulas recibidas como texto ni descarga imágenes de direcciones arbitrarias. Los logotipos son opcionales; si fallan, se conservan los datos del reporte.
- Los archivos y SQLite son locales al servicio. En una instancia sin disco persistente pueden perderse al reiniciar o desplegar. El historial principal de IngeniariCCa sigue estando en Supabase; esta corrección no cambia esa base de datos.

La consulta del IMPI puede ser intermitente. Un plan de Render sin suspensión elimina el tiempo de arranque, pero no garantiza que el portal del IMPI responda.
