# Sistema de Trading Multi-Broker con Research + Paper + Live
## Documento de Diseño — v1.0

---

## Cómo leer este documento

Este documento tiene tres tipos de contenido marcados explícitamente:

- **[FIRME]** — Decisiones cerradas. Claude Code las implementa tal cual, sin reinventar.
- **[PROVISIONAL]** — Decisión razonable para empezar, pero esperada a revisarse al llegar a esa fase con información nueva. Claude Code la implementa tal cual pero anota en el plan que es provisional.
- **[ABIERTO]** — Decisión que deliberadamente no se cierra ahora. Cuando llegue el momento, se decide con más contexto.

Las fases se implementan **en orden**. No empezar la siguiente hasta que la anterior pase sus criterios de aceptación.

Cada fase termina con una sección **"Criterios de aceptación"** — son la definición dura de "terminado". Si no se cumplen todos, la fase no cerró.

---

## Parte I — Decisiones de diseño globales

### I.1 Principios rectores [FIRME]

Estos principios ordenan todas las decisiones siguientes. Cuando haya tensión entre ellos, ganan en el orden escrito.

1. **Seguridad sobre features.** El sistema nunca pone en riesgo capital por un bug o confusión de modo. Ninguna feature nueva tiene prioridad sobre un guardrail que no está. Si hay duda entre "lo hacemos más simple" y "lo hacemos más seguro", gana seguro.

2. **Mismo código en los tres modos.** El código de estrategia que corre en backtest debe ser exactamente el mismo que corre en paper y en live. Nada de flags `if mode == "live"`. La diferencia de modo es inyectada desde afuera vía configuración y adapters, no vía `if` dentro de la estrategia.

3. **Reproducibilidad absoluta.** Todo backtest debe poder reproducirse bit a bit. Esto implica: semillas fijadas, versiones de dependencias pinneadas, datos inmutables en DB con hash, código versionado con commit hash embebido en el reporte.

4. **Separación honesta de research y producción.** El LLM y los notebooks viven en el lado de research. Nunca tocan ejecución. Es una pared de diseño, no una convención.

5. **Observabilidad desde el día uno.** Logs estructurados, métricas, trazas. No "lo agregamos después". Después es nunca.

6. **Pequeño y funcionando siempre.** Cada fase termina con algo corriendo end-to-end, aunque sea mínimo. Nunca hay una fase donde "falta integrar todo al final".

### I.2 Stack tecnológico [FIRME]

**Lenguaje principal:** Python 3.11+. Razón: Nautilus, Pandas, librerías de análisis. No hay alternativa real para este caso.

**Motor de trading:** NautilusTrader (última estable). Razón: discutida en la conversación previa.

**Base de datos operacional:** PostgreSQL 16 + extensión TimescaleDB. Razón: series temporales como ciudadanos de primera, hypertables, retention policies, compresión nativa. Postgres puro sería aceptable pero desperdicia la naturaleza time-series del dominio.

**Cache / pub-sub:** Redis 7. Razón: para canales de eventos internos entre procesos (ejemplo: strategy_engine publica `trade_executed`, telegram_bot lo recibe y notifica).

**API backend:** FastAPI. Razón: async nativo, OpenAPI automático, Pydantic para validación, ecosistema Python consistente.

**Dashboard operacional:** Grafana, conectado directo a Postgres. Razón: construir un frontend custom para paneles tipo "PnL, posiciones, health" es reinventar Grafana peor. El dashboard custom viene solo para lo que Grafana no cubre (lanzar backtests, explorar reportes — Fase 5+).

**Contenedorización:** Docker + Docker Compose. No Kubernetes. Razón: es un sistema de una persona en un VPS. K8s es overhead sin retorno.

**Reverse proxy + HTTPS:** Caddy. Razón: HTTPS automático con Let's Encrypt, configuración más simple que Nginx/Traefik para este caso.

**CI/tests:** GitHub Actions (asumiendo repo privado en GitHub). Tests unitarios obligatorios, integración opcional.

**LLM provider:** OpenRouter. Razón: un endpoint, múltiples modelos, billing unificado, fácil de intercambiar.

**Notificaciones:** Telegram Bot API. WhatsApp excluido del v1.

**Logging:** `structlog` (JSON estructurado) + `loguru` para dev-experience. Envío a stdout del contenedor, recolectado por Docker. Para v1 no hay stack centralizado de logs; se agrega si se justifica.

**Secretos:** variables de entorno cargadas desde `.env` local y desde `/etc/trading-system/secrets.env` en el VPS con permisos 600. No en el repo. Para v2 se puede migrar a HashiCorp Vault o Doppler.

### I.3 Topología de procesos [FIRME]

El sistema corre como **ocho contenedores Docker** orquestados por un único `docker-compose.yml`. La separación en contenedores no es por microservicios-por-moda, es por aislamiento de fallas y ciclos de vida distintos.

1. `postgres` — Postgres + TimescaleDB. Volumen persistente. Único consumidor: los servicios internos.
2. `redis` — Redis para pub/sub. Sin persistencia (los mensajes son efímeros).
3. `ingestor` — proceso que baja datos históricos y mantiene WebSockets de tiempo real. Un adapter por broker, todos en el mismo proceso (no vale la pena separar).
4. `trading-engine` — el `TradingNode` de Nautilus. Puede correr en modo `backtest`, `paper`, o `live` según config. Solo una instancia activa por vez.
5. `api` — FastAPI. Expone REST para el dashboard y Telegram. No tiene lógica de negocio; es una fachada sobre la DB y Redis.
6. `grafana` — Grafana server. Paneles pre-provisionados desde el repo.
7. `caddy` — reverse proxy con HTTPS.
8. `telegram-bot` — bot de Telegram. Corre como proceso independiente. Se suscribe a Redis para eventos, habla con el `api` para comandos.

El LLM-copilot no es un contenedor propio. Es un endpoint dentro de `api` que habla con OpenRouter. No necesita proceso separado.

### I.4 Estructura del repositorio [FIRME]

```
trading-system/
├── README.md
├── docker-compose.yml
├── docker-compose.override.yml.example   # para dev local
├── Caddyfile
├── .env.example
├── .github/
│   └── workflows/
│       ├── tests.yml
│       └── lint.yml
├── docs/
│   ├── design.md                         # este doc
│   ├── runbook.md                        # qué hacer cuando algo falla
│   └── decisions/                        # ADRs (architecture decision records)
├── infra/
│   ├── postgres/
│   │   ├── Dockerfile
│   │   └── init/                         # schemas iniciales
│   ├── grafana/
│   │   ├── dashboards/                   # JSON de dashboards
│   │   └── provisioning/                 # datasources, etc.
│   └── scripts/
│       ├── backup_db.sh
│       └── restore_db.sh
├── src/
│   ├── trading/
│   │   ├── __init__.py
│   │   ├── common/                       # tipos, configs, utils compartidos
│   │   ├── ingest/                       # adapters de datos por broker
│   │   │   ├── binance/
│   │   │   ├── bybit/
│   │   │   └── polymarket/
│   │   ├── engine/                       # integración con Nautilus
│   │   │   ├── node.py                   # TradingNode factory
│   │   │   ├── backtest.py               # runner de backtest
│   │   │   └── live.py                   # runner paper/live
│   │   ├── strategies/                   # estrategias (una carpeta cada una)
│   │   │   ├── base.py                   # helpers comunes
│   │   │   ├── polymarket_btc5m/
│   │   │   └── ...
│   │   ├── research/                     # notebooks, reportes, análisis
│   │   │   ├── notebooks/
│   │   │   └── reports/
│   │   ├── api/                          # FastAPI app
│   │   ├── bots/
│   │   │   └── telegram/
│   │   └── llm/                          # integración con OpenRouter
│   └── cli/                              # scripts CLI (backtest, ingest, etc)
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/                         # datos sintéticos para tests
└── pyproject.toml                        # deps, tool configs
```

Convención: todo lo ejecutable vive en `src/trading/` como paquete. Los scripts CLI son thin wrappers en `src/cli/` que llaman al paquete. Esto permite usar las mismas funciones desde notebooks.

### I.5 Modelo de datos [FIRME para Fase 1, extensible después]

TimescaleDB con las siguientes tablas principales en schema `market_data`:

```sql
-- OHLCV de cripto (Binance, Bybit)
CREATE TABLE market_data.crypto_ohlcv (
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    interval        TEXT NOT NULL,              -- '1m', '5m', '1h', '1d'
    ts              TIMESTAMPTZ NOT NULL,
    open            NUMERIC(20,8) NOT NULL,
    high            NUMERIC(20,8) NOT NULL,
    low             NUMERIC(20,8) NOT NULL,
    close           NUMERIC(20,8) NOT NULL,
    volume          NUMERIC(28,8) NOT NULL,
    PRIMARY KEY (exchange, symbol, interval, ts)
);
SELECT create_hypertable('market_data.crypto_ohlcv', 'ts', chunk_time_interval => INTERVAL '7 days');

-- Trades tick de cripto (para reconstruir microestructura)
CREATE TABLE market_data.crypto_trades (
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    trade_id        TEXT NOT NULL,
    price           NUMERIC(20,8) NOT NULL,
    qty             NUMERIC(28,8) NOT NULL,
    side            TEXT NOT NULL,              -- 'buy' / 'sell' (taker side)
    PRIMARY KEY (exchange, symbol, trade_id)
);
SELECT create_hypertable('market_data.crypto_trades', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Polymarket: eventos de precio a nivel de token YES/NO
CREATE TABLE market_data.polymarket_prices (
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,              -- YES o NO
    ts              TIMESTAMPTZ NOT NULL,
    price           NUMERIC(10,6) NOT NULL,
    PRIMARY KEY (condition_id, token_id, ts)
);
SELECT create_hypertable('market_data.polymarket_prices', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Polymarket: trades individuales
CREATE TABLE market_data.polymarket_trades (
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    tx_hash         TEXT NOT NULL,
    price           NUMERIC(10,6) NOT NULL,
    size            NUMERIC(28,8) NOT NULL,
    side            TEXT NOT NULL,
    PRIMARY KEY (condition_id, tx_hash)
);
SELECT create_hypertable('market_data.polymarket_trades', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Polymarket: metadatos de mercados (para no re-fetchearlos)
CREATE TABLE market_data.polymarket_markets (
    condition_id    TEXT PRIMARY KEY,
    slug            TEXT NOT NULL,
    question        TEXT NOT NULL,
    window_ts       BIGINT,                     -- para mercados btc-updown-5m
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    outcome         TEXT,
    open_time       TIMESTAMPTZ,
    close_time      TIMESTAMPTZ,
    resolve_time    TIMESTAMPTZ,
    metadata        JSONB
);
```

Schema `trading` para datos del sistema (órdenes, posiciones, PnL, ejecuciones). Este schema se diseña en Fase 2 porque hay que alinearlo con los tipos de Nautilus para no duplicar representación. **[PROVISIONAL]** — se cierra cuando arranque la Fase 2.

Schema `research` para reportes de backtest, resultados históricos, y comparaciones.

```sql
CREATE TABLE research.backtests (
    id              UUID PRIMARY KEY,
    strategy_name   TEXT NOT NULL,
    strategy_commit TEXT NOT NULL,              -- git hash del código
    params_hash     TEXT NOT NULL,              -- hash de los parámetros
    params          JSONB NOT NULL,
    dataset_from    TIMESTAMPTZ NOT NULL,
    dataset_to      TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL,              -- 'running', 'completed', 'failed'
    metrics         JSONB,                      -- sharpe, sortino, dd, etc.
    report_path     TEXT                        -- path al HTML generado
);

CREATE TABLE research.backtest_trades (
    backtest_id     UUID NOT NULL REFERENCES research.backtests(id),
    trade_idx       INTEGER NOT NULL,
    instrument      TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             NUMERIC(28,8) NOT NULL,
    entry_ts        TIMESTAMPTZ NOT NULL,
    entry_price     NUMERIC(20,8) NOT NULL,
    exit_ts         TIMESTAMPTZ,
    exit_price      NUMERIC(20,8),
    pnl             NUMERIC(20,8),
    fees            NUMERIC(20,8),
    metadata        JSONB,
    PRIMARY KEY (backtest_id, trade_idx)
);
```

### I.6 Configuración [FIRME]

Todas las configs son archivos TOML versionados en `config/`. No se editan a mano en producción; se cambian en repo, se revisan en PR, se despliegan.

```
config/
├── base.toml                # defaults
├── environments/
│   ├── dev.toml             # laptop local
│   ├── staging.toml         # VPS en modo paper
│   └── production.toml      # VPS en modo live (solo cuando corresponda)
├── strategies/
│   ├── polymarket_btc5m_delta_naive.toml
│   └── ...
└── brokers/
    ├── binance.toml         # endpoints, rate limits
    ├── bybit.toml
    └── polymarket.toml
```

El deploy elige el `environments/X.toml` vía variable de entorno `TRADING_ENV`. Los secretos (API keys) NUNCA están en los TOML — se inyectan por env vars.

### I.7 Guardrails transversales [FIRME]

Estos guardrails existen en TODAS las fases desde la Fase 0. No son opcionales.

1. **Modo por default = paper.** Si el sistema arranca sin `TRADING_ENV` explícito, arranca en paper. Live requiere setting explícito Y un archivo `I_UNDERSTAND_THIS_IS_REAL_MONEY` en el VPS.

2. **Kill switch físico.** Archivo `/etc/trading-system/KILL_SWITCH` en el VPS. Si existe, el `trading-engine` se niega a enviar órdenes. Revisado en cada tick y al arranque.

3. **Position limits por estrategia.** Configurados en TOML, chequeados antes de cada orden. No hay estrategia sin límite.

4. **Daily loss limit global.** Si la pérdida del día supera X% del capital asignado, el engine pausa todas las estrategias y envía alerta crítica.

5. **Heartbeat.** El engine publica heartbeat a Redis cada 10 s. Si falta por >60 s, el bot de Telegram avisa.

6. **Reconciliación.** Cada N minutos, el engine compara sus posiciones internas contra lo que reporta el broker. Si divergen, pausa y alerta.

7. **Idempotencia.** Toda orden enviada al broker lleva un `client_order_id` determinístico desde el estado de la estrategia, de modo que re-enviar la misma orden por reconexión no duplica ejecución.

---

## Parte II — Fases de implementación

A cada fase le corresponde un prompt separado para Claude Code (ver Parte IV). Las fases están diseñadas para ser prompts independientes: Claude Code ejecuta una fase, vos validás los criterios de aceptación, y recién entonces le pasás el prompt de la siguiente.

### Fase 0 — Infraestructura base

**Objetivo:** tener el VPS listo para correr los contenedores, con red segura, backups, y un "hello world" que muestre que la cadena Postgres → API → Grafana funciona.

**Duración estimada:** 1 semana (tiempo efectivo, no calendario).

**Entregables:**

- VPS con usuario no-root con `sudo`, SSH solo con llaves (password deshabilitado), UFW con puertos 22/80/443 abiertos, Fail2ban activo.
- Docker + Docker Compose v2 instalados.
- `docker-compose.yml` con los 8 servicios (pero muchos son stubs en esta fase: el engine y el ingestor pueden ser solo un contenedor con `python -c "while True: time.sleep(10)"` para validar que corren).
- Postgres + TimescaleDB corriendo, con la extensión habilitada, y los schemas `market_data`, `trading`, `research` creados pero vacíos de tablas (las tablas vienen en Fase 1 para `market_data` y Fase 2 para las demás).
- Redis corriendo, sin auth (es red interna Docker; no expuesto).
- Caddy sirviendo Grafana en `https://<hostname>/grafana` con HTTPS válido.
- Grafana accesible con login, con datasource Postgres pre-configurado vía provisioning.
- Script `infra/scripts/backup_db.sh` que hace `pg_dump` comprimido a un bucket S3-compatible. Configurado en cron para correr diario a las 04:00 UTC.
- Repo Git con GitHub Actions que corre `ruff` y `pytest` (aunque no haya tests todavía, el pipeline existe).
- Documento `docs/runbook.md` inicial con: cómo reiniciar cada contenedor, cómo ver logs, cómo ejecutar backup manual, cómo entrar al VPS.

**Criterios de aceptación:**

1. Puedo `ssh` al VPS y ver los 8 contenedores en `docker compose ps` como `Up`.
2. Grafana responde en HTTPS con certificado válido (no self-signed).
3. Puedo crear un panel de Grafana que consulte `SELECT now()` desde Postgres y ver el resultado.
4. Hago un cambio trivial en Python (ej. agregar un archivo `src/trading/__init__.py`), push a `main`, y el CI corre y pasa.
5. Ejecuto `infra/scripts/backup_db.sh` manualmente y veo el backup en el bucket.
6. `docker compose down` y `docker compose up -d` deja todo corriendo igual que antes, sin pérdida de data.

**Decisiones provisionales de esta fase:**

- **Ubicación de backups:** **[PROVISIONAL]** Backblaze B2. Barato, S3-compatible. Se puede cambiar a Wasabi o AWS después.
- **Grafana auth:** **[PROVISIONAL]** user/password admin. Para v2, SSO con algún IdP.

---

### Fase 1 — Ingesta de datos históricos y en tiempo real

**Objetivo:** tener la DB poblada con 6-12 meses de histórico para Binance, Bybit y Polymarket (en los instrumentos que uses), y WebSockets de tiempo real activos escribiendo continuamente.

**Duración estimada:** 2-3 semanas.

**Entregables:**

- Tablas en schema `market_data` creadas (las de la sección I.5).
- Tres adapters de ingesta en `src/trading/ingest/`, uno por broker. Cada uno implementa una interfaz común:
  - `backfill(symbol, from, to)` — llena histórico idempotentemente.
  - `stream(symbols)` — WebSocket que escribe a DB en tiempo real.
  - `healthcheck()` — devuelve estado del stream.
- Un supervisor en el proceso `ingestor` que arranca los streams, los reinicia si mueren, y expone métricas simples en `/metrics` (Prometheus-compatible).
- CLI `python -m trading.cli.backfill --broker binance --symbol BTCUSDT --interval 5m --from 2024-01-01 --to 2024-12-31` que llena la DB.
- **Migrar el fetcher actual de `polybot-btc5m`** al nuevo adapter de Polymarket. El código existente se reescribe (no se copia tal cual) siguiendo la interfaz común. Preservar la lógica de reconstrucción de `window_ts` y el uso de múltiples fuentes (Goldsky, Data API, NautilusTrader loader).
- Panel en Grafana "Data freshness" que muestra: último timestamp escrito por cada tabla/símbolo, gap respecto a "ahora", tasa de escritura en trades/minuto.
- Tests unitarios para cada adapter usando datos sintéticos o fixtures grabadas de las APIs.
- Tests de integración **opcionales** que pegan contra las APIs reales, marcados con `@pytest.mark.integration` y no corren en CI por default.

**Instrumentos mínimos a cubrir en Fase 1:**

- Binance: BTCUSDT y ETHUSDT spot, intervalos 1m/5m/1h/1d. Trades tick para BTCUSDT (no para ETH al principio — mucho volumen, poco uso por ahora).
- Bybit: lo mismo que Binance.
- Polymarket: todos los mercados `btc-updown-5m-*` de los últimos 6 meses (backfill completo), y stream en vivo de los que están abiertos.

**Criterios de aceptación:**

1. `SELECT count(*) FROM market_data.crypto_ohlcv WHERE exchange='binance' AND symbol='BTCUSDT' AND interval='5m'` devuelve ≥50,000 filas (>6 meses de 5m candles).
2. `SELECT count(*) FROM market_data.polymarket_markets WHERE slug LIKE 'btc-updown-5m-%'` devuelve ≥15,000 filas.
3. El stream en vivo tiene menos de 60s de gap respecto a "ahora" en condiciones normales.
4. Mato el contenedor `ingestor` y al reiniciarlo el stream se reanuda sin duplicados (verificable por `SELECT count(*)` antes y después).
5. Corro `backfill` dos veces sobre el mismo rango y la cantidad de filas no cambia (idempotencia).
6. Los tests unitarios pasan en CI.
7. El panel de Grafana "Data freshness" muestra datos verdes.

**Decisiones provisionales de esta fase:**

- **Fuente primaria para Polymarket:** **[PROVISIONAL]** depende de lo que ya exista en `polybot-btc5m`. Probablemente Data API + Goldsky subgraph. Claude Code debe estudiar el código actual antes de decidir.
- **Retención de trades tick:** **[PROVISIONAL]** infinito por ahora. Si el disco sufre, aplicar TimescaleDB retention policy (ej. comprimir >30 días, drop >1 año).

---

### Fase 2 — Integración con Nautilus + motor de backtest

**Objetivo:** tener el `TradingNode` de Nautilus corriendo dentro del contenedor `trading-engine`, con al menos una estrategia migrada, y un runner de backtest que produce reportes estándar.

**Duración estimada:** 2-3 semanas.

**Entregables:**

- `src/trading/engine/node.py` — factory del `TradingNode` que, según config, devuelve un nodo en modo backtest, paper o live. **En esta fase solo backtest está implementado funcionalmente**; paper y live quedan con el cableado listo pero sin conectarse a los brokers.
- Adapter de datos de Nautilus que lee desde la DB. Nautilus usa `DataCatalog` nativamente; vamos a construir un `DataLoader` custom que lea de Postgres y genere los tipos de Nautilus (`Bar`, `TradeTick`, etc.).
- Schema `trading` en la DB finalizado. Tablas:
  - `trading.orders` — órdenes enviadas, con estado (NEW, FILLED, CANCELED, etc.).
  - `trading.fills` — ejecuciones.
  - `trading.positions_snapshots` — snapshot de posiciones por timestamp, para reconstruir PnL histórico.
- Al menos una estrategia migrada al formato Nautilus `Strategy`. **[FIRME]** La primera es la `delta_naive_v1` del proyecto `polybot-btc5m`. Esto permite comparar números contra el backtest viejo.
- Runner CLI: `python -m trading.cli.backtest --strategy polymarket_btc5m/delta_naive_v1 --params configs/strategies/pbt5m_delta_naive.toml --from 2024-07-01 --to 2024-12-31`.
- Generador de reportes que produce:
  - HTML en `src/trading/research/reports/YYYY-MM-DD_<hash>.html` con equity curve, drawdown, trade log, distribución de PnL, estadísticas por hora/día.
  - Fila en `research.backtests` con metadatos + métricas.
  - Filas en `research.backtest_trades` con cada trade.
- **Test de paridad:** el mismo dataset corrido por la estrategia vieja de `polybot-btc5m` y por la estrategia nueva en Nautilus debe dar el mismo vector de trades (mismo timestamp, mismo side, mismo size). Tolerancia: 0. Si difieren, es un bug.
- Test de estabilidad automatizado: el runner corre walk-forward (ej. 6 meses in-sample / 2 meses out-of-sample rolling) y reporta si las métricas fuera de muestra están dentro de ±30% de las in-sample.
- Panel de Grafana "Backtests" con lista de últimos backtests y sus Sharpe/DD.

**Criterios de aceptación:**

1. `python -m trading.cli.backtest ...` corre sin errores y deposita un reporte HTML y filas en `research.backtests`.
2. El test de paridad contra la estrategia vieja pasa (diferencia cero en trades).
3. El reporte HTML se abre en el browser y se ve correctamente (equity curve, estadísticas, tabla de trades).
4. El test de estabilidad walk-forward corre y produce un veredicto (stable / unstable) con justificación numérica.
5. Los test unitarios de la estrategia (que corren sobre datos sintéticos determinísticos y validan que produce las órdenes esperadas) pasan en CI.
6. Un backtest de 6 meses a 5m de granularidad corre en menos de 5 minutos en el VPS.

**Decisiones provisionales de esta fase:**

- **Modelo de fills en backtest para Polymarket:** **[PROVISIONAL]** el que ya usa `polybot-btc5m` (parabolic fee model o lo que sea). Debe importarse/replicarse tal cual. Se revisa en Fase 3 comparando paper vs. backtest.
- **Formato del reporte HTML:** **[PROVISIONAL]** usar `quantstats` o similar. Se evalúa si alcanza; si no, se customiza en Fase 5.

---

### Fase 3 — Paper trading en vivo

**Objetivo:** la misma estrategia que pasó backtest corre ahora en paper contra las feeds reales de los brokers, 24/7, durante al menos 2-4 semanas, y se compara lo que hizo contra lo que predijo el backtest.

**Duración estimada:** 2 semanas de setup + 2-4 semanas de observación.

**Entregables:**

- Modo `paper` del `TradingNode` completamente cableado. Recibe datos reales de los brokers vía los adapters de Nautilus, pero las órdenes nunca salen: un `SimulatedExecutionClient` las "ejecuta" localmente aplicando el mismo modelo de fills que el backtest.
- Config `environments/staging.toml` que selecciona modo paper.
- Despliegue automatizado del engine en el VPS. Comando (por ejemplo `make deploy-staging`) que reinicia el contenedor con la nueva config.
- Logging estructurado completo: cada decisión de estrategia, cada orden (simulada), cada fill, cada error, con contexto suficiente para debuggear offline.
- Panel Grafana "Paper trading live" con: PnL intraday, posiciones actuales, últimos 20 trades, gráfico de precio con markers de entradas/salidas.
- Alertas de Telegram (integración mínima, el bot completo es Fase 4):
  - Nuevo trade ejecutado (con PnL si es cierre).
  - Pérdida del día > X (threshold configurable).
  - Estrategia detenida por error.
  - Heartbeat perdido >60s.
- Reporte diario automático: todos los días a las 00:05 UTC el engine genera un resumen del día anterior (PnL, # trades, hit rate, mayor drawdown intraday) y lo publica a un canal de Telegram.
- Job de comparación paper-vs-backtest: al final de cada semana, el engine corre un backtest sobre exactamente el mismo período y los mismos datos que acaba de ver en paper, y compara los vectores de trades. Divergencia esperada baja; divergencia alta es un bug del modelo de fills o de los datos.

**Criterios de aceptación:**

1. El contenedor `trading-engine` corre en modo paper ininterrumpidamente por 7 días sin crashes ni reinicios manuales.
2. Durante ese período, ejecuta al menos 50 trades simulados (ajustable según la estrategia — si la estrategia por diseño opera menos, el número baja).
3. El reporte de comparación paper-vs-backtest semanal muestra divergencia < 10% en número de trades y < 20% en PnL total.
4. Las alertas de Telegram llegan (verificable provocando condiciones controladas: matar el engine y ver que la alerta de heartbeat perdido llega).
5. Hay un runbook actualizado para: qué hacer si el engine crashea, cómo interpretar divergencias paper-vs-backtest, cómo pausar la estrategia.

**Decisiones provisionales de esta fase:**

- **Capital simulado asignado:** **[PROVISIONAL]** 1000 USDC equivalente por estrategia. Realista pero chico.
- **Threshold de alerta de pérdida:** **[PROVISIONAL]** -3% del capital asignado.

---

### Fase 4 — Dashboard de research + bot de Telegram completo

**Objetivo:** cerrar la superficie de control humano. Dashboard decente para explorar, bot de Telegram con comandos reales, no solo alertas.

**Duración estimada:** 1-2 semanas.

**Entregables:**

- **Paneles Grafana adicionales:**
  - "Market data explorer" — gráficos de precio/volumen por símbolo, con capacidad de zoom temporal.
  - "Strategy performance comparator" — permite seleccionar 2-3 backtests y verlos superpuestos.
  - "System health" — CPU/RAM/disco del VPS, latencia de WebSockets, errores últimas 24h.
- **Dashboard custom inicial (FastAPI + frontend minimal):**
  - Una sola página `/research` que lista últimos backtests con filtros por estrategia/fecha.
  - Detalle de backtest muestra el HTML ya generado en Fase 2.
  - Botón "Run backtest" que acepta strategy + params + rango y lanza un job. El job corre en un worker separado (Celery o un simple `multiprocessing.Process`), no en el request handler.
  - **[FIRME]** Frontend: HTML + HTMX + Alpine.js. Razón: para un dashboard de uso personal, SPAs con React son overkill. HTMX hace interactividad suficiente con HTML renderizado del lado server. Menos código, menos bugs, menos dependencias.
- **Bot de Telegram completo:**
  - Comandos:
    - `/status` — resumen: engine up/down, estrategias activas, PnL día.
    - `/positions` — posiciones abiertas.
    - `/trades [N]` — últimos N trades (default 5).
    - `/pnl [período]` — PnL hoy/semana/mes.
    - `/pause <strategy>` — pausa una estrategia.
    - `/resume <strategy>` — la reanuda.
    - `/killswitch` — activa el kill switch global. Requiere confirmación ("sí, lo entiendo").
    - `/backtest <strategy> <desde> <hasta>` — lanza un backtest; cuando termina, envía el reporte.
    - `/help` — lista los comandos.
  - Autorización: lista de `user_id` autorizados en config. Cualquier otro usuario recibe "no autorizado" y se loggea el intento.
  - Estado de conversación para los comandos que requieren más de una interacción (ej. `/killswitch` pide confirmación).

**Criterios de aceptación:**

1. Desde Telegram puedo ejecutar los 9 comandos y cada uno responde correctamente.
2. Un usuario no autorizado intentando `/status` recibe rechazo y queda loggeado.
3. El dashboard en `/research` muestra la lista de backtests y permite abrir el detalle.
4. El botón "Run backtest" lanza un job y el resultado aparece en la lista cuando termina (sin refresh manual — HTMX polling cada 5s está OK).
5. El panel "System health" en Grafana muestra CPU/RAM/disco actualizándose.

---

### Fase 5 — LLM como copiloto de research

**Objetivo:** integrar OpenRouter para que puedas tirarle ideas de trading al sistema y te ayude a construirlas.

**Duración estimada:** 1 semana.

**Arquitectura:**

Endpoint `POST /api/llm/chat` en el servicio `api`. Recibe:

```json
{
  "session_id": "uuid",
  "message": "texto libre del usuario",
  "context_refs": [
    {"type": "backtest", "id": "uuid"},
    {"type": "strategy", "name": "polymarket_btc5m/delta_naive_v1"}
  ],
  "model": "anthropic/claude-sonnet-4.6"
}
```

El handler:
1. Carga el contexto referenciado desde la DB (reporte del backtest, código de la estrategia, últimos trades si se pide).
2. Construye un system prompt **[FIRME] limitado a tres roles explícitos:**
   - "Sos un copiloto de research cuantitativo."
   - "Podés proponer hipótesis, leer reportes, sugerir modificaciones a estrategias en pseudocódigo o código Python, proponer experimentos."
   - "NO podés emitir órdenes, modificar configs de producción, ni ejecutar código."
3. Llama a OpenRouter con el prompt + mensaje + contexto.
4. Devuelve la respuesta, y la loggea en `research.llm_conversations`.

**Sin acceso a ejecución:** este endpoint no tiene función-call ni tool-use que toque el motor. Si querés que el LLM "corra un backtest", el LLM devuelve el comando en texto plano, vos lo revisás y lo ejecutás manualmente. **Esta es una pared de diseño, no una configuración.** No hay flag para "darle acceso".

**Entregables:**

- Endpoint funcionando.
- Integración con el bot de Telegram: comando `/ask` que inicia una conversación con el LLM. Respuestas multi-turno en el mismo chat.
- Panel en el dashboard custom `/research/chat` para sesiones más largas (mejor UX para código).
- Almacenamiento de conversaciones en DB para poder retomarlas y analizarlas después.
- Tests que validan que el sistema rechaza cualquier intento del LLM de invocar endpoints de ejecución (el test pretende que el LLM devolvió una función-call maliciosa y verifica que el handler la ignora).

**Criterios de aceptación:**

1. `/ask en Polymarket BTC 5m, ¿tiene sentido estudiar edge entre implied_prob y realized volatility de los últimos 30s?` responde con análisis del LLM usando el contexto de datos.
2. Una misma pregunta con el mismo `session_id` mantiene contexto conversacional.
3. Intentar que el LLM ejecute algo (via jailbreak, prompt injection en un reporte de backtest) no produce efecto ejecutivo. Hay al menos 3 tests que verifican esto.

**Decisiones provisionales:**

- **Modelo default:** **[PROVISIONAL]** `anthropic/claude-sonnet-4.6`. Buen equilibrio calidad/costo. Ajustable via config.
- **Límite de tokens por sesión:** **[PROVISIONAL]** 200k por sesión, 50 sesiones por día. Previene costos runaway.

---

### Fase 6 — Capital real con una sola estrategia

**Objetivo:** una (y solo una) estrategia pasa a vivir de verdad con capital real asignado. Con límites estrictos, monitoreo pesado, y la posibilidad de killear todo desde el teléfono.

**Duración estimada:** 1 semana de setup + observación indefinida.

**Pre-requisitos duros para entrar en Fase 6:**

1. La estrategia corrió en paper por ≥4 semanas.
2. El reporte paper-vs-backtest muestra divergencia <10% consistente.
3. La estrategia tiene Sharpe >1.5 en backtest out-of-sample.
4. Drawdown máximo <15% en backtest.
5. Al menos 200 trades en paper (suficiente muestra).
6. **Nada de esto es negociable.** Si alguno falla, la estrategia no pasa a live. Y el sistema lo chequea automáticamente al intentar switchear a modo live — no es honor system.

**Entregables:**

- Modo `live` del `TradingNode` completamente cableado (las clases ya existen desde Fase 2; ahora se conectan a los `ExecutionClient` reales de Nautilus).
- Archivo `I_UNDERSTAND_THIS_IS_REAL_MONEY` requerido.
- Position sizing conservador inicial: **[FIRME]** la estrategia arranca con 10% del capital que pensás asignarle en régimen normal. Si va bien 2 semanas, 25%. Un mes más, 50%. Tres meses de track record, 100%. No hay atajos para este schedule.
- Panel Grafana "LIVE — [strategy name]" dedicado con PnL realtime, posiciones, slippage observado vs. modelado.
- Alerta crítica de Telegram (con sonido distinto) si:
  - Pérdida del día > threshold.
  - Divergencia fill observed vs. modeled > 2x la típica del paper.
  - Reconciliación con broker falla.
  - Latencia de órdenes > X.
- **Weekly live review** obligatorio: una vez por semana, un reporte que compara la semana en live contra lo que predijo backtest + paper. Si divergencia >20%, investigar antes de seguir operando.

**Criterios de aceptación:**

1. La estrategia corre en live con capital real por al menos 1 semana sin incidentes críticos.
2. Las reconciliaciones con broker pasan todas.
3. El kill switch funciona (verificado en horario de bajo tráfico).
4. El weekly review muestra métricas en línea con las esperadas.

**Decisiones provisionales:**

- **Cuál estrategia es la primera en live:** **[ABIERTO]** se decide con los resultados de Fase 3.
- **Capital inicial total asignado:** **[ABIERTO]** depende de vos.

---

## Parte III — Lo que deliberadamente NO está en el diseño

Esta sección existe para cerrar tentaciones.

- **Multi-usuario / multi-tenancy.** No. Es para una persona.
- **Mobile app nativa.** No. Telegram cubre el caso.
- **Arbitraje cross-broker automático.** No. Complejidad x10, edge marginal.
- **Market making.** No. Requiere infra de latencia que no tenemos.
- **Strategy marketplace / compartir estrategias.** No. Diseñarlo como producto agrega fricción sin retorno.
- **ML pipeline automatizado.** No en v1. Primero que funcione el research honesto con estrategias interpretables.
- **Kubernetes, service mesh, event sourcing.** No. Es una persona en un VPS.
- **Autoescalado.** No. Si necesitás autoscale, hay algo mal en el diseño.
- **WhatsApp.** No en v1. Se puede agregar después vía Twilio.
- **Sharing de notebooks públicos, blog integrado, newsletter.** No. Foco.

Si en algún momento te entra la tentación de agregar algo de esta lista, la regla es: primero termina Fase 6, dejala viva 3 meses con capital real, y recién entonces consideralo.

---

## Parte IV — Prompts para Claude Code (uno por fase)

Los prompts de Claude Code son el medio por el cual este diseño se ejecuta. Cada uno sigue la misma estructura:

1. Contexto del proyecto (resumen de 2-3 párrafos + ref a este doc).
2. Fase específica a ejecutar + sus entregables.
3. Regla de no-código-hasta-aprobación-de-plan.
4. Criterios de aceptación.
5. Invariantes globales (de la sección I.1 y I.7).
6. Qué NO hacer (sección III).

**[ABIERTO]** Los prompts concretos se escriben fase por fase, justo antes de iniciar la fase. Razón: el prompt de Fase 3 se beneficia de saber qué terminó pasando en Fase 2. Escribir los 6 prompts ahora sería comprometerse a cosas que todavía no sabemos.

Lo que SÍ podemos definir ahora: la plantilla común.

```markdown
# Prompt Claude Code — Fase N: [título]

## Contexto
Estoy construyendo un sistema de trading multi-broker (Binance/Bybit/Polymarket) sobre NautilusTrader, con research primero y capital real después. El diseño completo está en `docs/design.md` en este repo. Por favor leélo antes de proponer plan.

Estamos en la Fase N. La fase anterior cerró con [estado actual — qué funciona, qué no].

## Objetivo de esta fase
[Copiar de la sección "Objetivo" de la fase correspondiente]

## Entregables
[Copiar de la sección "Entregables"]

## Criterios de aceptación
[Copiar]

## Invariantes no-negociables
1. Mismo código de estrategia en backtest/paper/live (no `if mode == "live"`).
2. Modo por default = paper. Live requiere archivo de confirmación explícito.
3. Kill switch físico respetado en cada tick.
4. Idempotencia en órdenes (client_order_id determinístico).
5. Logs estructurados en cada decisión.
6. Position limits en config, chequeados antes de cada orden.
7. Reproducibilidad: seeds fijadas, deps pinneadas, commit hash en reportes.

## Prohibiciones
- No ML, no libs de backtesting alternativas (ya decidimos Nautilus).
- No features no pedidos en los entregables.
- No cambiar el stack tecnológico sin pedir explicitamente.
- No tocar código de Fases futuras.

## Regla de plan primero
NO ESCRIBIR CÓDIGO HASTA QUE YO APRUEBE TU PLAN. Tu plan debe cubrir:
- Pasos concretos y en orden.
- Archivos que vas a crear/modificar.
- Cómo vas a verificar cada criterio de aceptación.
- Riesgos que ves y cómo los mitigás.
- Preguntas que tenés para mí antes de empezar.

Presentá el plan y esperá mi OK.
```

---

## Parte V — Riesgos, checkpoints y criterios de abandono

Esto existe porque los proyectos ambiciosos sin criterios de "pararlo" tienden a continuar por inercia incluso cuando ya no tienen sentido.

### Riesgos principales

1. **Scope creep.** Alto. Mitigación: la lista de la Parte III. Si se quiebra, pausar y revisar el diseño.

2. **Edge que no existe.** Muy alto. La mayoría de las estrategias "prometedoras" no sobreviven test de estabilidad. Mitigación: Fase 2 tiene walk-forward obligatorio; Fase 3 tiene comparación paper-vs-backtest; Fase 6 tiene pre-requisitos duros.

3. **Bug en el modelo de fills que hace parecer rentable algo que no lo es.** Alto. Mitigación: Fase 3 compara paper (que usa feeds reales) contra backtest, y la divergencia se auditará. Bug en fees se detecta acá.

4. **Desconexión prolongada del broker durante live.** Medio. Mitigación: reconciliación obligatoria, heartbeat, kill switch manual.

5. **Fuga de credenciales.** Alto si pasa, bajo en probabilidad. Mitigación: secretos en env vars con permisos 600, nunca en repo, separados por environment.

6. **Pérdida de datos históricos.** Bajo. Mitigación: backups diarios automáticos, restore testeado trimestralmente.

7. **Agotamiento del constructor (vos).** Alto si el proyecto se estira. Mitigación: fases cerradas con entregables útiles por sí solos, documentación buena, runbooks.

### Checkpoints de decisión

Al final de cada fase, respondé con honestidad:

- ¿Cumplí todos los criterios de aceptación? Si no: ¿cuáles y por qué?
- ¿Sigo motivado con este proyecto? Si no: pausar y volver en un mes antes de decidir abandonar.
- ¿Aprendí algo que cambia el diseño de fases siguientes? Si sí: actualizar el doc antes de seguir.

### Criterios de abandono razonable

- Si al final de Fase 3, ninguna estrategia muestra edge después de paper trading honesto, y tu mejor hipótesis para por qué es "los mercados que elegí son demasiado eficientes para retail", el proyecto cumplió su función de research y no tiene sentido forzar Fase 6.
- Si en cualquier fase detectás que el diseño no soporta un caso importante que descubriste en el camino, pará, volvé al doc, actualizalo, y reasumí.

---

*Fin del documento v1.0*
