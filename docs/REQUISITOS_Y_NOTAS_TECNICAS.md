# Requisitos y notas técnicas — Instalador MLR Odoo

## Por qué una app por API y no una acción de servidor

El instalador correcto es una **app externa por la API de Odoo (XML-RPC)**, no una acción de
servidor planificada, porque:

1. El sandbox `safe_eval` de las acciones de servidor **no permite `def` ni `try/except`
   limpios**; sin `try/except` real no se puede "capturar el error, seguir y reportar al final".
2. La creación por código de **campos calculados** y de **`base.automation`** depende mucho de
   la versión (en 19.2 cambió la estructura de automatizaciones). Aquí, con Python completo,
   hay `try/except`, logging, idempotencia y captura de cada error.
3. XML-RPC **funciona en Odoo Online/SaaS**, que es la restricción del entorno.

## Componentes (todos gratuitos)

- `app.py` — Flask + SQLite: conexiones, listado de lotes, instalar uno o todos en orden.
- `odoo_client.py` — cliente XML-RPC (search/create/write/ref/model_id + Parámetros del sistema).
- `installer.py` — motor idempotente por etapas con persistencia de IDs e informe.
- `developments/*.json` — los 4 lotes (versión final desplegada).

## Etapas del motor (ciclos)

`cargar IDs previos → params → models → accesses → fields → server_actions → automations →
views → rules`. Cada etapa obtiene y **persiste los IDs** (`ir.config_parameter`) para que las
siguientes los referencien (`{{PARAM:...}}`, `{{ACTION:...}}`).

## Marcadores admitidos

- `{{PARAM:CLAVE}}` — id resuelto del param (ubicación, cuenta, diario).
- `{{ACTION:CLAVE}}` — id de la acción de servidor creada (para botones en vistas).
- `{{GROUP}}` — xmlid del grupo custom del entorno.
- `{{GROUPS}}` — `base.group_system,<grupo custom>` (para `groups=` en el arch de vistas).

## Automatizaciones "de estado no hecho a estado hecho"

Las automatizaciones usan `filter_pre_domain` (estado **antes**, p. ej. `state != 'done'`) y
`filter_domain` (estado **después**, `state = 'done'`) sobre el campo de disparo `state`, para
capturar exactamente la transición y no re-disparar. Si el valor de `trigger` no existe en la
versión, el motor reintenta con `on_create_or_write` y lo deja anotado en el informe.

## Puntos frágiles conocidos (por eso el informe)

- **`base.automation`** (AA1/AA2/AA3): su estructura cambia entre versiones; el motor captura el
  error y sigue. Verifica en el informe que las 3 quedaron creadas.
- **Vistas heredadas**: validan el arch al crearse. La vista *ocultar "Crear factura"* va como
  **vista opcional aparte** (`optional`): si el ancla `//button[@id='create_invoice']` no existe
  en esa versión, se reporta como AVISO y el resto de la vista de botones sí queda.
- **Vista de lista de ventas**: se intenta heredar de
  `sale.view_quotation_tree_with_onboarding` y, si no, de `sale.view_order_tree`.

## Mapa de params → IDs reales (referencia del despliegue en producción)

| Param | Producción |
|---|---|
| Ubicación cliente dedicada `UBIC` | id 42 |
| Diario "Efectivo sin Factura" `DIARIO` | id 80 |
| Cuenta efectivo `EFECTIVO` (101.01.02) | id 441 |
| Cuenta 9996 (cobro de orden) `C9996` | id 442 |
| Cuenta 9997 (Costo de Venta – Orden) `C9997` | id 443 |
| Acciones A1,A2,B1,B2,C,D | 917–922 |
| Automatizaciones AA1,AA2,AA3 | 923–925 |

(Se muestran solo como referencia; el instalador los resuelve por nombre/código en cada base.)

## Checklist de QA (tras instalar en pruebas)

1. Venta con el booleano → entrega enrutada a la ubicación cliente dedicada.
2. Al validar la entrega: asiento provisional **Dr 9997 / Cr valuación** del producto, enlazado.
3. "Aplicar pago efectivo" → pago a efectivo contra 9996, **sin** tocar cuentas por cobrar.
4. Factura global agrupadora → COGS real 501; automatización de rebalanceo (cancelación exacta
   y parcial con etiqueta de historial).
5. Con un usuario **no** privilegiado: no ve diario 80, cuentas 441/442/9997, sus apuntes, ni
   las órdenes/pagos/movimientos sin factura.

## Salvedades (del runbook)

- Si un usuario sin permisos de contabilidad valida entregas que disparan AA2/AA3, el asiento se
  crea con SUS permisos; por eso el código usa `.sudo()`.
- La 9997 debe ser on-balance; si `button_cancel` en AA3 se bloquea, habilita "Permitir cancelar
  asientos" en el diario de valuación.
- El prefijo `[MLR]` va solo en nombres técnicos (acciones/reglas), **nunca** en textos visibles
  de las vistas (labels, `string` de botones).
