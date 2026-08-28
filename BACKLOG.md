# Backlog

Temas identificados pero no urgentes — para retomar en otra iteración.

## Bugs conocidos

### `macd_strategy` revienta con pocas velas
- **Archivo:** `app/services/strategy_engine.py`, función `macd_strategy`.
- **Qué pasa:** con menos de ~34 velas (para `fast=12/slow=26/signal=9`; el umbral
  real depende de los parámetros), `ta.macd(...)` devuelve `None` en vez de un
  DataFrame con columnas en NaN. `pd.concat([df, None], axis=1)` no falla, pero
  deja el DataFrame sin la columna `MACDh_{fast}_{slow}_{signal}`. La línea
  siguiente, `df.dropna(subset=[hist_col])`, revienta con
  `KeyError: ['MACDh_...']` en vez de caer en el `if len(df) < 2: return HOLD`
  que ya existe (y que sí funciona en `sma_crossover` y `rsi_strategy`, porque
  `ta.sma`/`ta.rsi` siempre devuelven una `Series`, aunque sea puro NaN).
- **Dónde puede dispararse:** no en el ciclo en vivo normal (`scheduler.py` pide
  velas con el `limit=200` por defecto de `BinanceService.fetch_ohlcv`, muy por
  encima del umbral). Sí puede dispararse en `POST /backtest`, donde `body.limit`
  es controlable por quien llama al endpoint — un backtest con `limit` bajo sobre
  una estrategia MACD tumba la corrida.
- **Fix sugerido:** guardar contra la columna faltante antes del `dropna`, ej.
  `if hist_col not in df.columns: return StrategyResult(Signal.HOLD, 0.0, ...)`,
  igual que el guard de `len(df) < 2` en las otras dos estrategias.
- **Test ya escrito** (documenta el comportamiento actual, no lo corrige):
  `tests/test_strategy_engine.py::TestMacdStrategy::test_not_enough_data_is_hold`.

## Roadmap — Fase 1 (antes de tocar dinero real)

- [x] Cierre por take-profit en `scheduler.py` (commit `fdde847`, tests en
  `tests/test_scheduler.py`)
- [x] Fix de clasificación de Market Stage — reconocer tendencia sin el stack
  completo de medias (commit `9ae7f05`, tests en `tests/test_market_stage.py`)
- [x] Tests unitarios de `strategy_engine.py` (`tests/test_strategy_engine.py`)
  y `RiskManager` (`tests/test_risk_manager.py`) — pendiente de commit
- [ ] Tests de integración del ciclo `run_strategy_cycle` completo con Binance
  mockeado (abre trade, cierra por stop/take-profit, etc.)
- [ ] Introducir Alembic de verdad y quitar el `ALTER TABLE ... ADD COLUMN IF
  NOT EXISTS` manual de `database.py` (ver explicación de Alembic dada en el chat)

## Roadmap — Fase 2 (confiabilidad de ejecución)

- [ ] Reconciliación al iniciar el bot: sincronizar posiciones abiertas contra
  el balance/órdenes reales de Binance, no solo contra la tabla `trades`.
- [ ] Mover el stop-loss (y opcionalmente el take-profit) a órdenes reales en
  el exchange en vez de simulación por polling.
- [ ] Manejo de rate limits / reintentos con backoff en `binance_service.py`.
- [ ] Prorrateo de capital entre estrategias activas de un mismo usuario (o un
  límite global de exposición).

## Roadmap — Fase 3 (observabilidad)

- [ ] Alertas (Telegram/email/push con `firebase_messaging`, confirmar si ya
  está cableado) cuando un `StrategyRun` sale `FAILED` o una orden falla.
- [ ] Logging estructurado + algo tipo Sentry para errores no capturados del
  scheduler.

## Roadmap — Fase 4 (producción)

- [ ] Nginx + Let's Encrypt, password real de DB vía secrets, `SECRET_KEY`/
  `FERNET_KEY` fuera del repo.
- [ ] CI (lint + tests) antes de cada merge a `develop`/`main`.
- [ ] Solo entonces evaluar `testnet: false`.

## Otras notas menores

- `requirements-dev.txt` fija `pytest==8.2.2`, pero el venv actual tiene
  `pytest 9.1.1` instalado — no reconciliado, revisar si vale la pena pinnear
  o actualizar el archivo.
