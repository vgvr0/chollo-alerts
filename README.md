# chollometro-alerts

Consulta búsquedas públicas de Chollometro y envía por Telegram los nuevos chollos de leche y cerveza.

## Fuente investigada

La navegación con Chrome DevTools/CDP mostró que la ruta pública del frontend es `GET https://www.chollometro.com/search?q=<término>&page=<n>`. La respuesta contiene HTML server-side: cada oferta es `article#thread_<id>` con `data-t-d={"id":...}`. El HTML incluye título (`a.thread-title`), URL, precio (`.thread-price`), tienda (`[data-t="merchantLink"]`), temperatura (botón con `...°`) y antigüedad (`Publicado hace ...`). No se necesitó cookie ni sesión para esta ruta. No se encontró una API JSON pública necesaria para la extracción; por estabilidad y simplicidad se usa este HTML server-side.

La paginación se controla con `page=N`. La implementación consulta una página por término por defecto para evitar peticiones innecesarias; se puede aumentar con `--pages`.

## Uso

```powershell
python -m pip install -e .
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
chollometro-alerts --db deals.sqlite3 --pages 1
```

SQLite hace la operación idempotente mediante `deal_id` como clave primaria. Solo se marca `notified_at` después de un envío Telegram correcto.

Para activar Telegram sin recibir avisos de las ofertas ya existentes, inicializa primero la base de datos:

```powershell
chollometro-alerts baseline
chollometro-alerts check
```

Puedes inspeccionar el baseline sin modificar SQLite con `chollometro-alerts baseline --dry-run`.

```powershell
python -m pytest -q
```
