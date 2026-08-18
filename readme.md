# timeproxy

## Cómo usarlo

1. Copia `main.py`, `config.json` y `403.html` a la Raspberry Pi (misma carpeta).
2. Edita `config.json` con los dominios y el límite en **segundos** que quieras permitir al día.
3. Ejecuta: `python3 main.py`
4. Configura el proxy en el dispositivo que quieras controlar:
   apuntando a `http://IP_DE_LA_RASPBERRY:8119` como proxy HTTP **y** HTTPS
   (en la mayoría de sistemas/routers se configura en un único campo "Proxy HTTP",
   y ese mismo proxy se usa también para túneles HTTPS vía `CONNECT`).

`usage.json` se genera solo y se reinicia automáticamente al cambiar de día.

## Importante: por qué HTTPS no puede mostrar tu página 403

Con HTTPS, el proxy solo abre un **túnel cifrado** (`CONNECT`) entre el
cliente y el sitio; no puede leer ni modificar el contenido porque va cifrado
de extremo a extremo. Por eso, cuando un sitio HTTPS está bloqueado, lo único
que puede hacer el proxy es **cortar la conexión** — el navegador mostrará su
propio error de conexión, no tu `403.html`.

Si quieres que también aparezca tu página personalizada en HTTPS, la única
forma es hacer **MITM (man-in-the-middle)**: el proxy genera certificados al
vuelo y el dispositivo cliente debe instalar y confiar en un certificado raíz
propio. Esto es bastante más complejo (librerías como `mitmproxy` lo resuelven)
y añade una superficie de riesgo si algún día compartes el dispositivo o la
red con terceros, así que no lo he incluido por defecto. Dímelo si quieres que
lo montemos.

## Otras notas

- El bloqueo de reintento funciona por coincidencia de dominio o subdominio
  (`www.youtube.com` cuenta como `youtube.com`).
- El contador de HTTPS se actualiza cada `CHECK_INTERVAL` segundos (5 por
  defecto) mientras la conexión sigue abierta, así que el corte no es
  instantáneo al llegar al límite, sino con ese margen.
- Para que arranque solo al iniciar la Raspberry Pi, te recomiendo crear un
  servicio `systemd` (`/etc/systemd/system/timeproxy.service`) que ejecute
  `python3 /usb/timeproxy/main.py`. Dímelo si quieres que te prepare el
  archivo del servicio.
