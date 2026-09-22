# chollometro-alerts

Consulta búsquedas públicas de Chollometro y envía por Telegram los nuevos chollos de leche y cerveza.

## Natural-language alerts

Las alertas pueden expresarse en lenguaje natural desde Telegram, por ejemplo:

```text
Avísame de cerveza Mahou por debajo de 1,20 €/litro
Quiero ofertas de leche entera por debajo de 0,90 €/litro
```

El LLM solo transforma la petición en una `AlertRule` estructurada y validada. La evaluación posterior de ofertas es determinista y no vuelve a llamar al LLM.

También se puede inspeccionar o guardar una regla directamente:

```bash
chollometro-alerts alert parse "Avísame de cerveza Mahou por debajo de 1,20 €/litro"
chollometro-alerts alert add "Avísame de cerveza Mahou por debajo de 1,20 €/litro"
chollometro-alerts alert list
```

El flujo es: lenguaje natural → parsing LLM una vez → regla estructurada persistida → evaluación determinista. `ProductExtractor` puede usar el LLM por separado para extraer atributos de ofertas nuevas.

`AlertRule.constraints` admite `max_price`, `max_price_per_liter`,
`max_price_per_unit`, `min_quantity`, `min_volume_l` y `min_temperature`, y todos
los límites presentes deben cumplirse. El precio por unidad lo calcula
`PricingEngine` (precio total dividido entre las unidades extraídas); si la
cantidad no es fiable, la oferta se rechaza con `REJECTED_UNKNOWN_QUANTITY` en
lugar de asumir una unidad. Los límites `max_price*` son exclusivos: el precio
debe ser estrictamente menor.

`run-rules --dry-run` recorre las reglas activas con el mismo camino que
`run-rules` (`repository.rule_from_row()` → `AlertRule` → `InterestRule` →
`PricingEngine` → `InterestEngine`) y solo cambia los efectos: no envía Telegram
ni escribe observaciones, baseline, extracciones o `notified_at`. Cada línea del
informe incluye `price_unit` y `price_per_unit`.

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

## Extracción de producto y DeepSeek

El parser HTML solo obtiene la identidad, los datos publicados y el texto de la
oferta. `AlertService` consulta `repository.exists(deal_id)` antes de permitir
cualquier llamada LLM. Reutiliza la extracción persistida cuando existe; para
ofertas conocidas sin extracción guardada solo usa extracción determinista.

Para ofertas nuevas, la extracción determinista es suficiente cuando obtiene un
volumen total con confianza >= 0.95. Si faltan datos, el servicio puede completarlos
mediante un `ProductExtractor` (`llm/base.py`). La extracción final, incluido el
fallback, se guarda en SQLite antes de ejecutar `PricingEngine`, `InterestEngine`
y Telegram. También se conserva si la oferta es rechazada. Un fallo de Telegram
permite reintentar el envío sin repetir la llamada al LLM.

`DeepSeekProductExtractor` usa `https://api.deepseek.com/responses` con salida
estructurada JSON Schema y validación local de `ProductExtraction`. Solo extrae
hechos: no calcula precios ni decide si la oferta es un chollo. Los timeouts,
errores de conexión y HTTP transitorios tienen reintentos limitados; las respuestas
inválidas o incompletas y los errores definitivos conservan los datos deterministas.
La caché se gestiona en el servicio, no en el provider ni en el parser.

La aplicación carga el archivo `.env` de la raíz del proyecto mediante
`python-dotenv`, incluso si se ejecuta desde otro directorio, sin sobrescribir
variables del entorno. Copia `.env.example` a `.env` (ignorado por Git) y configura:

```dotenv
LLM_ENABLED=false
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TIMEOUT_SECONDS=20
DEEPSEEK_MAX_RETRIES=2
```

Para enviar avisos, añade `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` a `.env`
o al entorno. Si faltan o están vacías, `check` termina con un error claro de
configuración (código 2), sin mostrar sus valores. `check --dry-run` y `baseline`
no requieren credenciales de Telegram.

`DEEPSEEK_MAX_RETRIES` cuenta reintentos adicionales a la petición inicial.
Con `LLM_ENABLED=false` no hace falta una API key. Con `true`, la ausencia de
`DEEPSEEK_API_KEY` produce un error de configuración antes de consultar ofertas.
El modelo elegido debe soportar JSON Schema en la API de Responses.
Para añadir otro provider, implementa `ProductExtractor` y regístralo en
`llm.create_extractor`; `AlertService` también permite inyectar un extractor.

Puedes validar conectividad y credenciales de DeepSeek sin hacer scraping ni
enviar Telegram con una única petición:

```powershell
chollometro-alerts test-llm "Pack de cerveza Mahou 6x330ml"
```

El comando muestra el `ProductExtraction`, estado, métricas y latencia. Fuerza
cero reintentos para que el diagnóstico corresponda a una sola llamada HTTP.
Durante un `check`, el progreso de cada petición registra únicamente `deal_id`,
intento, duración, estado y tokens; nunca registra la API key ni el cuerpo de la
respuesta.

`check` termina con un resumen `NOMBRE=valor`: `FOUND`, `NEW`,
`DETERMINISTIC_COUNT`, `LLM_COUNT`, `HYBRID_COUNT`, `LLM_CALLS`,
`LLM_CACHE_HITS`, `LLM_FAILURES`, `LLM_TOKENS` y `TELEGRAM_SENT`.
Los valores corresponden a esa ejecución. Los contadores por origen incluyen
ofertas rechazadas y extracciones recuperadas de caché; `LLM_CACHE_HITS` cuenta
las extracciones reutilizadas, también las deterministas. `NEW` cuenta las ofertas
aceptadas que se insertan por primera vez, y `LLM_CALLS` incluye los reintentos HTTP.

Con `check --dry-run`, cada oferta mostrada incluye `EXTRACTION_SOURCE`,
`CONFIDENCE`, `TOTAL_VOLUME_L` y `PRICE_PER_LITER` (`N/D` si falta el dato).
`TELEGRAM_SENT` es cero en este modo porque no se envían mensajes reales.

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```
