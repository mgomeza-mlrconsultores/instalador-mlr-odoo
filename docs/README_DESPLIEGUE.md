# Despliegue en la nube (gratis) — Instalador MLR Odoo

La app es un servicio web Flask servido con **gunicorn**. Necesita **salida a internet** para
llegar por XML-RPC a las bases Odoo. Recomendado: **Render (plan free)**.

## Opción A — Render (recomendada, salida a internet OK)

1. Sube esta carpeta a un repositorio Git (GitHub/GitLab).
2. En Render: *New → Web Service* apuntando al repo. Detecta Python.
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 180`
   (o usa el `render.yaml` incluido: *New → Blueprint*).
3. Variables de entorno:
   - `SECRET_KEY` → una cadena larga aleatoria (Render puede generarla).
   - `DATA_DIR` → `/var/data` (donde vive SQLite y el catálogo).
4. Abre la URL, completa el **setup** (usuario + contraseña maestra) y a trabajar.

**Persistencia:** en el plan free el disco es efímero (se borra en cada redeploy/reinicio).
Como tu **base de códigos maestra son tus .zip locales**, si se reinicia solo vuelves a subir
los bundles y recapturar conexiones. Si quieres persistencia real, añade un **disco** montado
en `/var/data` (ver el bloque comentado en `render.yaml`).

## Opción B — Servidor propio de MLR

Cualquier host con Python 3.10+ y salida a internet:
```
pip install -r requirements.txt
export SECRET_KEY="...largo y aleatorio..."
export DATA_DIR="/ruta/persistente/datos"
gunicorn app:app --bind 0.0.0.0:8000 --workers 2 --timeout 180
```
Ponlo detrás de Nginx/Caddy con HTTPS. Ideal si quieren usar `mlrconsultores.com`.

## Opción C — Local tipo web

```
pip install -r requirements.txt
python app.py    # http://127.0.0.1:5000
```

## Nota sobre PythonAnywhere free

Su plan gratuito **restringe la salida de red a una whitelist**, por lo que XML-RPC a
`*.odoo.com` normalmente **no funciona**. Úsalo solo si confirmas que tu dominio Odoo está en
la whitelist; si no, usa Render u otro host con salida libre.

## Seguridad

- La API key se guarda **cifrada** (Fernet, llave derivada de la contraseña maestra con
  PBKDF2). En la base solo hay ciphertext + un verificador.
- Protege con HTTPS y un `SECRET_KEY` fuerte (la sesión firma un token de descifrado).
- Acceso con login; no expongas la URL públicamente sin necesidad.
