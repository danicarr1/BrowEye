# BrowEye (versión independiente de Claude)

Herramienta local para **observar, editar, reenviar y comparar tráfico HTTP/HTTPS** generado por un navegador real.
Sirve para depurar aplicaciones web, entender APIs para scraping permitido y apoyar pruebas de seguridad **autorizadas**.

```
navegador Chromium (Playwright) ──eventos──▶ captura ──▶ SQLite ◀── Repeater (httpx)
                                                   ▲                  ▲
                                         API local 127.0.0.1 + token ─┘
                                                   ▲
                                          UI web (JS sin build)
```

## Instalación

Requisitos: Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium                         # descarga el navegador (una sola vez)
```

Si ya tienes Chromium en otra ruta: `export BROWEYE_CHROMIUM=/ruta/a/chrome`.

## Uso

```bash
broweye lab --port 8081                        # (opcional) servidor de laboratorio local
broweye --headed --start-url http://127.0.0.1:8081/
```

El comando imprime una URL como `http://127.0.0.1:8765/#token=…`. Ábrela en tu navegador habitual.
El token protege la API local, así que no lo compartas.

Flujo típico:

1. **Traffic**: navega desde la ventana de Chromium (`--headed`) o desde la barra de URL de la UI. Las peticiones aparecen en vivo.
   Filtra por URL, método, estado (`200`, `4xx`), host o contenido del body.
2. Selecciona una petición para ver URL, query, headers, body (con formato JSON o form), respuesta y hallazgos pasivos.
3. **Send to Repeater** crea un *borrador* (`RequestDraft`), una copia editable. La captura original nunca cambia.
4. Edita método, URL, headers (uno por línea; se conservan el orden y los duplicados), body (texto o base64/archivo),
   modo de cookies, autenticación, timeout y redirecciones. Pulsa **Send**.
5. El panel derecho muestra **la petición efectivamente enviada** en cada salto de redirección y su respuesta.
   Cada envío queda en el historial del borrador.
6. **Compare with original** o los botones A/B comparan cualquier captura (`flow:`) o ejecución (`exec:`).
7. **+ New** o **Import cURL** crean peticiones desde cero. **Export HAR** y **Copy cURL** exportan.
8. **+ Session** abre otra identidad aislada (cookies y almacenamiento propios), por ejemplo `admin` y `usuario`.

Sin navegador (solo constructor y Repeater): `broweye --no-browser`.

### Modos de cookies del Repeater

| Modo | Qué se envía |
|---|---|
| `as_is` (por defecto) | Exactamente el header `Cookie` del borrador, copiado de la captura y editable |
| `session` | Las cookies **actuales** del jar de la sesión de navegador elegida, filtradas por dominio, path y secure para la URL destino |
| `none` | Ninguna cookie |

En cualquier modo, los headers `Cookie` y `Authorization` y la autenticación configurada **no se envían a otro origen**
cuando una redirección sale del origen inicial. Cada salto lo indica con una nota.

## Pruebas

```bash
pytest -q
```

Hay 31 pruebas. Usan un servidor de laboratorio local (`src/broweye/lab.py`) que **registra lo que recibe**,
así que las aserciones verifican lo que llegó al servidor y no lo que la app dice haber enviado.
Incluyen una prueba de extremo a extremo que maneja la UI con un segundo navegador.

| Escenario de aceptación | Prueba |
|---|---|
| Navegar a un servidor local, el tráfico aparece | `test_acceptance.py::test_browse_is_captured` |
| POST con JSON, el servidor recibe el body correcto | `test_post_json_capture_matches_server` |
| Editar un body capturado, se envía el modificado (con Content-Length correcto) | `test_edited_body_is_sent` |
| Cambiar un header, el servidor recibe el nuevo valor | `test_changed_header_is_sent` |
| Petición nueva sin captura previa (texto y binario, sin headers ocultos) | `test_new_request_without_capture` |
| Cookies según el modo elegido, sesiones aisladas | `test_cookie_modes` |
| Comparar respuestas, diferencias relevantes | `test_compare_responses` |
| 150 peticiones del navegador más 30 envíos concurrentes, historial consistente | `test_many_requests_history_consistent` |
| Flujo completo desde la UI | `test_server.py::test_ui_end_to_end` |

Además: credenciales en redirecciones entre orígenes, semántica 302/303/307, gzip/br/zstd,
token de la API, rechazo de Host ajeno (DNS rebinding), cURL ida y vuelta, HAR y diff JSON.

## Documentación

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): decisiones, alternativas descartadas y modelo de datos.
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md): qué no puede ver o hacer esta versión.
- [docs/ROADMAP.md](docs/ROADMAP.md): qué haría en la versión 2.

## Uso responsable

Usa BrowEye solo contra sistemas propios, laboratorios o sistemas con autorización expresa y alcance definido.
La base de datos del proyecto (`broweye-project/`) contiene cookies y tokens capturados en texto plano:
trátala como un secreto.
