# Guía de uso — Plataforma Instalador MLR Odoo (en línea)

Plataforma web con identidad MLR para **desplegar códigos de Odoo por API (XML-RPC)** en
cualquier base Odoo Online/SaaS. Tú alimentas un **catálogo de apps con historial de
versiones** subiendo bundles (.zip con varios .json) o códigos sueltos (.json), eliges la base
y la versión, y la app instala todo en orden. Login + API keys cifradas. 100% libre.

## 1. Primer arranque (setup)

Al entrar por primera vez pide crear **usuario** y **contraseña maestra**. Esa contraseña:
- es tu login, y
- es la llave con la que se **cifran/descifran las API keys** (no se guarda en ningún lado).

Guárdala bien: si se pierde, no se pueden descifrar las keys (habría que recapturarlas).

## 2. Conexiones (bases)

En *Conexiones* agrega cada base: nombre, URL (p. ej. `https://pruebaconsultores.odoo.com`),
base de datos, usuario (login), **API key** (se guarda cifrada) y el **XML ID del grupo custom**
del entorno (pruebas `__export__.res_groups_84_5b1291db`, producción `__export__.res_groups_84_23e50353`).
Usa *Probar* para validar.

## 3. Tu base de códigos (catálogo + versiones)

- **Subir**: elige el archivo (`.zip` con varios `.json`, o un `.json` suelto), pon el
  **Código de app** (identificador estable, p. ej. `acretex_venta_sin_factura`), el **Nombre**
  y la **Versión** (si la dejas vacía se pone fecha/hora). Cada subida es una **versión** nueva.
- Si subes una **versión que ya existe**, la app **avisa** y solo la reemplaza si marcas
  *Sobrescribir*.
- El catálogo agrupa por app y conserva **todas las versiones**; puedes instalar la última o
  **una versión anterior**.
- Tus `.zip` locales son tu **base de códigos maestra**: si el host se reinicia, vuelves a
  subirlos.

## 4. Instalar

En cada versión del catálogo eliges la **base destino** y pulsas *Instalar esta versión*.
Instala en orden todos los códigos de esa versión (por su campo `order`), idempotente, y
muestra el informe (`[OK]`/`[AVISO]`/`[ERROR]`). Opción de enviarlo por correo desde Odoo.

## 5. Antes de instalar en cada base

Edita el `value` de los `params` de cada código para que coincidan con esa base (ubicación,
cuentas, diario). Los objetos base (cuenta 9996/9997, diario de efectivo, ubicación cliente
dedicada) deben existir antes; el instalador los localiza y referencia, no los crea.

## 6. Formato de un código (.json)

Metadatos recomendados en cada `.json`: `app_code`, `app`, `name`, `order`, y las secciones
`params`, `models`, `accesses`, `fields`, `server_actions`, `automations`, `views`, `rules`.
Ver `REQUISITOS_Y_NOTAS_TECNICAS.md` para el detalle del motor y los marcadores
`{{PARAM:...}}`, `{{ACTION:...}}`, `{{GROUP}}`, `{{GROUPS}}`.
