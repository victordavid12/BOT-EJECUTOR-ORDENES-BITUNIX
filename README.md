# BOT-EJECUTOR-ORDENES-BITUNIX
Bot de trading en Python para Bitunix Futures: recibe alertas de TradingView por webhook y ejecuta LONG/SHORT con SL/TP, breakeven y trailing; configuración por símbolo en SQLite y GUI.

Cómo usar el bot (Bitunix Futures + TradingView Webhook)
1) Qué es y cómo funciona

Este repo levanta un servidor Flask con un endpoint /webhook que recibe alertas de TradingView (en JSON o texto).
Cada alerta se encola en una cola FIFO por símbolo (para que no se pisen señales del mismo par) y se ejecuta la lógica:

Señal LONG / SHORT: abre posición si no hay, o gestiona la existente (ignora / resetea órdenes / hace flip).

Señal BUY_TP / SELL_TP: cierra la posición correspondiente sin abrir una nueva.

Gestión de riesgo opcional: SL, TP por niveles, breakeven y trailing (moviendo el SL).

2) Requisitos

Python 3.x

Librerías que usa el proyecto:

Flask

requests

python-dotenv

PySide6 (solo si vas a usar la GUI)

3) Configuración inicial (API keys + entorno)

El bot lee un archivo llamado bitunix.env en la misma carpeta del proyecto.

Variables que soporta:

BITUNIX_API_KEY: tu API key de Bitunix (Futures).

BITUNIX_SECRET_KEY: tu secret.

BOT_DB_PATH: ruta del SQLite de configuración (si no lo pones, usa bot_config.db en la carpeta).

FLASK_HOST: por defecto 0.0.0.0 (escucha en todas las interfaces).

FLASK_PORT: por defecto 5001.

Notas:

Si cambias la base de datos o sus valores, el bot no recarga “en caliente”: hay que reiniciar el proceso para que vuelva a leer el SQLite.

4) Base de datos de configuración (SQLite)

El comportamiento por símbolo se define en bot_config.db (o el archivo que indiques con BOT_DB_PATH).

Tabla pairs_config (1 fila por símbolo)

Campos importantes:

symbol: por ejemplo ETCUSDT (el bot también maneja entrada con .P y la “mapea” si corresponde).

is_enabled: 1/0 para habilitar o deshabilitar ese símbolo.

margin_mode: ISOLATION o CROSS.

leverage: apalancamiento.

order_size_type:

MARGIN_USDT: interpreta order_size_value como margen (USDT) y lo multiplica por el leverage para calcular notional.

NOTIONAL_USDT: interpreta order_size_value como notional directo (USDT).

PCT_BALANCE: usa un % del balance disponible (en decimal) como margen.

order_size_value: el valor asociado al tipo de tamaño.

sl_enabled: 1/0

sl_pct: el SL en decimal. Ejemplo: 0.02 = 2%.

tp_enabled: 1/0

breakeven_enabled: 1/0

breakeven_trigger_pct: decimal. Movimiento mínimo desde entrada para activar BE. Ej: 0.01 = 1%.

breakeven_offset_pct: decimal. Offset respecto a la entrada al poner BE. Ej: 0.002 = 0.2%.

trailing_enabled: 1/0

trailing_trigger_pct: decimal. Movimiento mínimo desde entrada para activar trailing.

trailing_step_pct: decimal. Cada cuánto “escalona” el trailing.

trailing_distance_pct: decimal. Distancia del SL respecto al mejor precio.

trailing_move_immediately: 1/0. Si está activo, al activar trailing puede mover el SL inmediatamente.

same_side_policy:

IGNORE: si llega otra señal del mismo lado y ya estás en esa dirección, no hace nada.

RESET_ORDERS: si llega otra señal del mismo lado, cancela TPs pendientes y recalcula SL/TPs.

Importante sobre porcentajes: en la DB son decimales, no “porcentaje humano”.
Ejemplo: 0.01 = 1%, 0.02 = 2%.

Tabla tp_levels (N filas por símbolo)

Define TPs parciales por niveles:

level: 1,2,3…

target_pct: decimal desde el precio de entrada. Ej: 0.015 = 1.5%.

close_frac: fracción a cerrar en ese nivel. Ej: 0.30 = cerrar 30%.

is_enabled: 1/0

El bot coloca TPs parciales y deja como “runner” la cantidad restante que no cubran los niveles.

5) Arranque del bot (modo headless)

El servidor que recibe webhooks es app.py.
Cuando está levantado expone:

GET /health para comprobar que responde.

POST /webhook para recibir señales.

Por defecto escucha en el puerto 5001 (si no lo cambias en bitunix.env).

6) Uso con la GUI (opcional, recomendado para editar la DB)

El archivo main.py es una GUI (PySide6/Qt) que sirve para:

Editar pairs_config y tp_levels (CRUD).

Lanzar el bot (app.py) como subproceso y ver logs en vivo.

Si usas la GUI:

Editas la DB desde la interfaz.

Inicias/detienes el bot desde el panel.

Cada vez que cambies configuración, lo normal es reiniciar el subproceso para aplicar cambios.

7) Configurar TradingView (alertas + webhook)

En TradingView crea una alerta y en el campo Webhook URL apunta al servidor:

http://TU_IP:5001/webhook (ajusta puerto si cambias FLASK_PORT)

El bot acepta 2 formatos de mensaje:

A) JSON (aunque TradingView lo mande como texto)

Campos típicos:

symbol (o ticker)

signal (o action / side)

Señales válidas:

LONG

SHORT

BUY_TP (cierra LONG)

SELL_TP (cierra SHORT)

Además, si mandas BUY o SELL, el bot lo normaliza a:

BUY → LONG

SELL → SHORT

B) Texto plano

Si no envías JSON, el bot intenta extraer:

El símbolo (ejemplos soportados: ETCUSDT, SOLUSDT.P, BINANCE:SOLUSDT.P)

La señal buscando palabras como LONG, SHORT, BUY TP, SELL TP (también detecta “TP alcista/bajista”).

Tip práctico: incluye siempre en el mensaje el símbolo y la palabra exacta LONG / SHORT / BUY TP / SELL TP.

8) Qué hace exactamente cuando llega una señal
LONG / SHORT

Verifica que el símbolo exista en la DB y que esté habilitado.

Ajusta margin_mode y leverage del símbolo (si falla, lo loguea y sigue).

Si NO hay posición abierta:

Abre market.

Si sl_enabled=1, intenta abrir con un SL provisional para proteger desde el primer momento.

Luego coloca el SL de posición con positionId.

Si tp_enabled=1, coloca TPs por niveles usando tp_levels.

Si ya hay posición:

Si es del mismo lado:

IGNORE: no hace nada.

RESET_ORDERS: recalcula SL y vuelve a colocar TPs (cancela TPs anteriores).

Si es del lado contrario:

Cierra la posición actual a mercado.

Abre la nueva en el lado pedido.

BUY_TP / SELL_TP (TP manual)

BUY_TP: cierra una posición LONG si existe.

SELL_TP: cierra una posición SHORT si existe.

Antes de cerrar, intenta cancelar TPs pendientes para evitar órdenes colgadas.

9) Breakeven y trailing (cómo se aplican)

Para que se mueva el SL automáticamente:

sl_enabled debe estar activo, y además:

breakeven_enabled o trailing_enabled deben estar activos.

El bot corre un monitor por símbolo (hilo en segundo plano) que:

Consulta precio y posición.

Si se cumple el trigger de BE, mueve SL a entrada ± offset.

Si trailing está activo, va ajustando el SL en “escalones” según trailing_step_pct y manteniendo distancia trailing_distance_pct.

10) Errores típicos y solución rápida

“symbol sin config”: el símbolo que llega en la alerta no existe en pairs_config.
Solución: crea/edita la fila en la DB (o ajusta el símbolo de TradingView).

HTTP 429 “cola llena”: estás metiendo demasiadas señales del mismo símbolo.
Solución: reduce frecuencia o evita duplicados.

Cambios en DB no se reflejan: el bot carga config al inicio.
Solución: reinicia app.py (o el subproceso desde la GUI).
