# Limitaciones conocidas (v0.1)

## Qué tráfico se observa

- **Solo el Chromium que lanza BrowEye.** No se ve el tráfico de otros navegadores, apps móviles ni otros programas
  (para eso haría falta un proxy; ver ROADMAP).
- **WebSockets**: no se capturan los frames. El handshake puede aparecer o no según el navegador.
- **Respuestas en streaming** (SSE, descargas largas): el body puede no estar disponible o quedar incompleto.
- **Bodies de petición no expuestos**: en subidas de archivos (`multipart` con archivos) o bodies en streaming,
  Chromium puede no entregar el body. La UI lo indica con una nota ("request body not exposed by the browser").
- **Bodies de respuestas de redirección (3xx)**: el navegador no los entrega.
- **Recursos servidos desde caché o service workers** pueden aparecer con información parcial.
- Límites de tamaño: 10 MB por body capturado y 20 MB por respuesta en el Repeater. Si se truncan, se indica con una nota.
- `data:` y `blob:` se ignoran.

## Diferencias entre navegador y Repeater

- **Protocolo**: el navegador puede usar HTTP/2 o HTTP/3; el Repeater usa **HTTP/1.1**. Los servidores
  que se comportan distinto por protocolo darán resultados distintos.
- **Huella TLS**: diferente a la de Chromium. Algunos sistemas antibots responden distinto. No hay ni habrá
  funciones para imitar huellas: no es un objetivo del proyecto.
- **Orden y mayúsculas de headers**: el Repeater respeta el orden del borrador, pero `Host` va primero.
  En HTTP/2 el navegador envía los nombres en minúsculas.
- **Cookies en modo `session`**: se *leen* del jar del navegador. Los `Set-Cookie` que reciba el Repeater
  **no** actualizan el navegador, y dentro de una cadena de redirecciones tampoco se aplican a los saltos siguientes.
  Es una decisión explícita (el Repeater no altera la sesión del navegador), pero rompe flujos de login
  que dependen de cookies puestas en un 302.
- La autenticación Basic/Bearer configurada en el borrador vive en memoria: se pierde al reiniciar.

## Persistencia y seguridad

- La base de datos guarda cookies, tokens y bodies **en texto plano**. Los archivos `sessions/*.storage.json`
  (sesiones reutilizables) también. Protege el directorio del proyecto.
- La exportación cURL redacta `Cookie` y `Authorization` por defecto. **El HAR no las redacta**,
  porque es una exportación fiel; revisa el archivo antes de compartirlo.
- Los chequeos pasivos son pistas para un humano, no un escáner; hay falsos positivos y negativos.

## Plataforma y escala

- Probado en Linux (headless, Chromium 141, Python 3.13). **Windows y macOS no se han probado**.
  No hay código específico de plataforma salvo las señales (manejadas).
- `--headed` requiere un entorno gráfico.
- Un solo proceso y SQLite en el hilo del event loop: escribir bodies muy grandes puede pausar la UI unos milisegundos.
  Probado con 150 peticiones simultáneas del navegador más 30 envíos concurrentes; no se han medido volúmenes mayores.
- La tabla de tráfico mantiene como máximo 5000 filas en el DOM (los filtros consultan la base completa).
- No hay import de HAR, proxy upstream, ni certificados de cliente.
