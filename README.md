# Instalador MLR · Odoo

Plataforma web (Flask) para desplegar **códigos/personalizaciones de Odoo por API externa
(XML-RPC)** en cualquier base **Odoo Online/SaaS**, con identidad de marca **MLR Consultores**.

Tú alimentas un **catálogo de apps con historial de versiones** subiendo *bundles* (`.zip` con
varios `.json`) o códigos sueltos (`.json`); eliges la base destino y la versión, y la app crea
en orden e idempotente: modelos, campos (incl. calculados), reglas de acceso, acciones de
servidor, acciones contextuales, automatizaciones, vistas y reglas de registro. Guarda los IDs
en Parámetros del sistema de Odoo para referenciarlos entre etapas y re-ejecuciones.

## Características

- **Login** con contraseña maestra; **API keys cifradas** (Fernet + PBKDF2). En la base solo
  se guarda el texto cifrado.
- **Catálogo con versiones**: cada subida es una versión; avisa antes de sobrescribir; permite
  instalar versiones anteriores.
- **SaaS**: todo por XML-RPC, sin módulos ni acceso al filesystem de Odoo.
- **100% libre**: Flask + SQLite + cryptography + gunicorn.

## Ejecutar en local

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

## Producción (nube)

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 180
```

Variables de entorno: `SECRET_KEY` (aleatoria y larga) y `DATA_DIR` (carpeta persistente).
Guía completa en `docs/README_DESPLIEGUE.md` y uso en `docs/GUIA_DE_USO.md`.

## Estructura

```
app.py              # rutas Flask, auth, catálogo, instalación
installer.py        # motor idempotente por etapas (con persistencia de IDs)
odoo_client.py      # cliente XML-RPC (SaaS)
security.py         # login + cifrado de API keys
brand.py            # activos de marca MLR (data URI)
templates/          # UI con identidad MLR
docs/               # guía de uso, despliegue y notas técnicas
data/               # (generada) catálogo de apps + SQLite  [no versionar]
```

---
MM&LR Consultores Fiscales · "Contadores que SÍ le entienden a Odoo" · uso interno · Confidencial
