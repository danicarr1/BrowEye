# Arquitectura y decisiones

## Objetivo de diseño

Un núcleo pequeño y verificable para el flujo **capturar → inspeccionar → editar → reenviar → comparar**.
Todo lo demás (plugins, escáneres, automatización) puede crecer alrededor sin tocar ese núcleo.

## Módulos

| Módulo | Responsabilidad | Depende de |
|---|---|---|
| `models.py` | `RequestSpec`, `ResponseSpec`, `Flow`, `RequestDraft`, `Execution`, `Hop` | nada |
| `capture.py` | Sesiones de navegador (una `BrowserContext` por identidad) y registro de tráfico | Playwright, store |
| `store.py` | Persistencia SQLite (WAL) y consultas | models |
| `repeater.py` | Ejecuta un borrador y registra lo enviado | httpx, models |
| `diff.py` | Comparación de respuestas | models |
| `analysis.py` | Chequeos pasivos sobre tráfico ya capturado | models |
| `interop.py` | cURL (importar y exportar) y HAR (exportar) | models |
| `app.py` | Servicio que orquesta todo; lo usan la API y las pruebas | todos los anteriores |
| `server.py` | API HTTP local, SSE y archivos estáticos | app, aiohttp |
| `static/` | UI (HTML, CSS y JS sin framework ni build) | API |

`repeater`, `diff`, `analysis` e `interop` no dependen del navegador ni de la UI. Se prueban de forma aislada
y se pueden reutilizar desde scripts.

## El modelo de peticiones

Hay tres formas distintas de "petición" y nunca se mezclan:

- **`RequestSpec` (inmutable)**: lo capturado o lo enviado. Es `frozen`, así que no se puede editar por accidente.
- **`RequestDraft` (mutable)**: lo que el usuario edita. `from_flow()` **copia** los headers y el body;
  modificar el borrador no puede alterar la captura (lo verifica `test_draft_from_flow_is_a_copy`).
  Al crearlo se descartan `Host`, `Content-Length` y `Transfer-Encoding`, porque se derivan de la URL y del body al enviar.
  Dejar una copia obsoleta es la causa clásica de "edité el body y no se envió".
- **`Execution`**: una instantánea del borrador **en el momento del envío** más una lista de `Hop`.
  Cada hop guarda la petición que salió (con los headers reales que puso httpx) y la respuesta.

Así la UI puede mostrar sin ambigüedad: original (flow), versión editada (draft) y enviada (hop.request).

Los headers son listas ordenadas de pares, no diccionarios. HTTP permite duplicados y el orden puede importar.

## Decisiones principales y alternativas descartadas

### Captura: instrumentación del navegador (Playwright) en lugar de proxy MITM

- **Elegido**: Chromium lanzado por Playwright. Cada request y response llega como evento, HTTPS incluido.
  No hay que instalar un certificado CA ni configurar un proxy del sistema.
- **Descartado: proxy MITM (mitmproxy o propio)**. Ve tráfico de cualquier aplicación, pero exige instalar
  y confiar en una CA. Es una fricción de instalación y un riesgo si la CA se filtra. Además añade un componente grande.
  Vale la pena como **segundo origen de captura** en el futuro (ver ROADMAP), detrás de la misma interfaz de `Flow`.
- **Descartado: extensión de navegador**. No permite leer bodies de respuesta de forma fiable sin el protocolo de depuración,
  y obliga a mantener el empaquetado por navegador.
- **Descartado: Selenium**. No da acceso nativo a bodies de red; Playwright sí.

Problema encontrado durante el desarrollo: si el body se pide cuando se procesa `requestfinished`,
una navegación rápida provoca que Chromium descarte el documento anterior y el body se pierde
("No resource with given identifier found"). Ahora el body se pide en el callback del evento `response`.
Lo cubre `test_browser_capture_of_compressed_responses`, que falló antes de la corrección.

### Ejecución: httpx con peticiones construidas a mano

- `httpx.Request(...)` + `client.send()` en lugar de `client.get()`, para **no** añadir headers por defecto
  (`User-Agent: python-httpx`, `Accept`…). El servidor recibe lo que el usuario ve más lo que HTTP exige
  (`Host`, `Content-Length`). Lo verifica `test_new_request_without_capture`.
- Las redirecciones se siguen **manualmente**, para registrar cada salto y decidir explícitamente qué credenciales viajan.
  Al cambiar de origen se eliminan `Cookie`, `Authorization` y la autenticación configurada.
- Se descartó `requests` (no es asíncrono) y `aiohttp` como cliente: normaliza y añade headers con menos control.
- Extras `brotli` y `zstd`: el navegador anuncia `Accept-Encoding: br, zstd`. Sin esos decodificadores,
  reenviar una petición capturada de un sitio real mostraría basura comprimida.

### Persistencia: SQLite

Viene en Python, el proyecto es un solo archivo, funciona en todas las plataformas y permite búsqueda en bodies
(se guardan como BLOB, no en base64). Usa modo WAL. Todo el acceso ocurre en el hilo del event loop:
las operaciones son cortas y así se evita un pool de conexiones.

Los secretos de autenticación del Repeater **no se guardan** en SQLite; viven en memoria
(hay que volver a escribirlos tras reiniciar). Las cookies capturadas sí se guardan porque forman parte del tráfico.

### UI: web local, JS sin framework

- **Elegido**: una página servida por la propia app. Funciona en Linux, Windows y macOS sin empaquetado nativo.
  Tiene unas 540 líneas de JS sin build: cualquiera del equipo puede leerla y modificarla.
- **Descartado: TUI (Textual)**. Ver y editar bodies grandes, comparar lado a lado y renderizar tablas largas
  es mucho peor en terminal.
- **Descartado: Electron o Tauri**. Añaden empaquetado, actualizaciones y un segundo runtime sin resolver un problema actual.
- **Descartado (por ahora): React o similar**. El estado de la UI es simple. Si crece, se puede migrar
  porque la API es la frontera.

Todo dato capturado se inserta con `textContent`. La UI tiene CSP `default-src 'self'` (sin inline, sin eval),
así que una respuesta maliciosa no puede ejecutar código en la herramienta.

### Seguridad de la API local

La API puede manejar un navegador y enviar peticiones arbitrarias, así que una web cualquiera no debe poder usarla:

- escucha solo en `127.0.0.1`;
- token aleatorio por ejecución en cada llamada (`X-BrowEye-Token`), comparado en tiempo constante;
- el header `Host` debe ser el loopback propio, lo que bloquea DNS rebinding (`test_api_rejects_foreign_host_header`).

### Actualizaciones en vivo sin bloquear

Los eventos SSE solo dicen "hay algo nuevo". La UI pide `GET /api/flows?after_seq=N` en lotes.
Las ráfagas se agrupan, un cliente lento pierde notificaciones pero nunca datos, y la tabla mantiene
como máximo 5000 filas en el DOM.

## Cómo están diseñadas las pruebas

1. **El servidor de laboratorio es el oráculo**: registra método, path, query, headers crudos y body.
   Las aserciones de "se envió X" se hacen contra ese registro.
2. **Navegador real** en las pruebas de aceptación (Chromium headless), sin mocks de Playwright.
3. **`drain()`** espera a que las capturas en vuelo se escriban, sin `sleep`, para evitar pruebas intermitentes.
   La suite se ejecutó 3 veces seguidas: 31/31 en cada corrida.
4. Las reglas de seguridad (credenciales entre orígenes, token, Host) tienen pruebas propias.

## Diferencias frente a herramientas existentes (honestas)

- **Frente a Burp y ZAP**: sin CA ni proxy que configurar; las sesiones son contextos de navegador aislados de primera clase;
  los modos de cookies del Repeater están explícitos; las credenciales en redirecciones se manejan de forma visible;
  es open source, ligero y basado en Python. **Les falta mucho**: Intruder, scanner, extensiones maduras,
  HTTP/2 en el Repeater, captura de cualquier cliente.
- **Frente a mitmproxy**: UI orientada al flujo de edición y comparación, y sesiones de navegador integradas.
  mitmproxy captura cualquier cliente y es mucho más maduro como proxy.
- **Frente a Chrome DevTools**: historial persistente y consultable, Repeater real con historial,
  comparación estructural y sesiones múltiples. DevTools tiene mejor integración con JS, rendimiento y WebSockets.
- **Frente a Postman o Insomnia**: las peticiones nacen del tráfico real del navegador, con sus cookies e identidad.
