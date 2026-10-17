# Qué haría en la versión 2

Ordenado por valor y riesgo. Cada punto es independiente.

1. **Propagación de cookies dentro de una ejecución**: aplicar los `Set-Cookie` de un salto a los siguientes
   (jar efímero por ejecución), con opción explícita de "escribir de vuelta al navegador".
   Es la limitación que más afecta a flujos de login reales.
2. **HTTP/2 en el Repeater** (`httpx[http2]`), seleccionable por borrador, para reproducir lo que hace el navegador.
3. **Import de HAR** para reabrir trabajo hecho con otras herramientas.
4. **Segundo origen de captura: proxy** (por ejemplo, mitmproxy como librería) que produzca los mismos `Flow`.
   Así se captura cualquier cliente sin tocar Repeater, store ni UI. Requiere gestionar una CA local de forma segura.
5. **Sistema de extensiones mínimo**: funciones Python registradas para tres puntos concretos
   (analizar un flow, transformar un borrador antes de enviar, y añadir un panel de solo lectura).
   No antes de tener al menos dos extensiones reales que lo justifiquen.
6. **Comparación de autorización** (para pentesting autorizado): reenviar el mismo borrador con las cookies
   de varias sesiones o identidades y comparar las respuestas. Es una extensión natural de los modos de cookies
   y de `diff.py`, con límite de velocidad y lista de hosts en alcance obligatoria.
7. Lista de hosts **en alcance** por proyecto: el Repeater se niega a enviar fuera de ella.
8. Ejecución en un hilo aparte para escrituras grandes en SQLite, y paginación virtual en la tabla de tráfico.
9. Cifrado opcional del directorio del proyecto (o al menos de los `storage_state`).
10. Pruebas en Windows y macOS en CI (GitHub Actions con matriz de sistemas operativos).
