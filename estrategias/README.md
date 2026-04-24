# estrategias/ — flujo de investigación e iteración

Capa de documentación (sólo markdown) para el ciclo diario de ideación,
validación y descarte de estrategias. **No** duplica código ni configuración:

- Código ejecutable: `src/trading/strategies/<family>/<name>.py` (ya existe).
- Parámetros: `config/strategies/<name>.toml` (ya existe).
- Reportes HTML + filas en `research.backtests`: generados por `report.py`.

Esta carpeta sólo contiene: hipótesis, diseño conceptual, bitácora de
investigación, estado del pipeline, y resúmenes markdown de resultados
(los escribe el VPS).

---

## Estado de una estrategia

Define en qué subcarpeta vive su archivo `.md`:

| Carpeta | Significado |
|---|---|
| `en-desarrollo/` | Hipótesis viva. Puede o no tener código/config todavía. |
| `activas/` | Código + config en main, backtest pasa criterios, corriendo en paper o live. |
| `descartadas/` | Probada y rechazada. Se conserva el `.md` como aprendizaje. **Nunca borrar.** |

Mover el archivo entre carpetas es la forma canónica de cambiar estado.
No usar flags dentro del archivo.

---

## Reglas del flujo para Claude en Cowork

Estas reglas son vinculantes. Cada sesión empieza y termina siguiéndolas.

### 1. Al inicio de la sesión, Claude lee en este orden

1. `estrategias/resultados/_last_run_status.md` — **primero siempre**. Si
   status es `FAIL`, avisar al usuario inmediatamente antes de hacer
   cualquier otra cosa; la prioridad de la sesión cambia a "investigar
   el fallo del VPS", no a iterar estrategias.
2. `estrategias/INDICE.md` (una línea por estrategia, estado + último resultado).
3. **Sólo** los `.md` que el usuario pida o que el INDICE marque como
   prioritarios para esa sesión. No leer la carpeta entera.
4. Si la sesión continúa una estrategia existente, leer también el último
   archivo en `resultados/<nombre>/` (el más reciente por fecha en nombre).
5. `BITACORA.md` sólo si el usuario hace referencia a notas pasadas o pide
   contexto histórico. No leerlo por defecto.

**No leer** `src/` ni `config/` salvo que la conversación lo requiera.
El `.md` de la estrategia debe contener lo suficiente para razonar sobre ella.

### 2. Crear una estrategia nueva

1. Copiar `plantillas/estrategia.md` a `en-desarrollo/<nombre>.md`.
2. `<nombre>` es `snake_case`, termina en `_vN` donde N es la versión.
   Debe coincidir con el nombre del futuro módulo y del TOML.
3. Completar **hipótesis**, **variables clave**, **falsificación** (qué
   resultado la mata). Dejar **Implementación** y **Resultados** vacíos.
4. Agregar una línea al INDICE.

### 3. Pasar de hipótesis a código ejecutable

Cuando el usuario pida implementar:

1. Crear `src/trading/strategies/<family>/<nombre>.py` heredando
   `StrategyBase`. Seguir el patrón de `trend_confirm_t1_v1.py`.
2. Crear `config/strategies/<nombre>.toml` con secciones
   `[params]`, `[sizing]`, `[backtest]`, `[fill_model]`, `[risk]`, `[paper]`
   (mirar cualquier TOML existente como referencia).
3. Registrar el dispatch en `src/trading/cli/backtest.py` (el bloque
   `_load_strategy`). Sin esto, el VPS no lo puede correr.
4. En el `.md` de la estrategia, completar la sección **Implementación**
   con rutas exactas a esos tres archivos + qué commit los introdujo.

### 4. Condensar una conversación larga al `.md`

Cada vez que una sesión termina con decisiones no triviales, Claude
**debe** actualizar el `.md` de la estrategia antes de cerrar. El formato
de actualización es una entrada nueva bajo la sección **Historial**, con
fecha y una línea de resumen. No reescribir las secciones anteriores: las
nuevas decisiones se agregan como capas, preservando el razonamiento previo.

Regla: si el resumen de la sesión no cabe en ~15 líneas, la estrategia
está sobre-documentada o hay dos estrategias mezcladas. Separar.

### 5. Actualizar el INDICE

Cada vez que se crea, mueve, o reevalúa una estrategia, actualizar
`INDICE.md`. Una línea por estrategia. Mantener ordenado: activas primero,
en-desarrollo después, descartadas al final. El INDICE es lo primero que
Claude lee (después del status); debe caber en una pantalla.

**Flag de "prioridad de la sesión actual" (línea final del INDICE):**

- **Quién:** lo actualiza Claude en Cowork, no el usuario ni el VPS.
- **Cuándo:** al **final** de cada sesión, antes de cerrar, como parte de
  condensar la conversación. Refleja qué estrategia merece atención en la
  próxima sesión según lo recién aprendido.
- **Por qué al final y no al inicio:** quien cierra la sesión tiene el
  contexto más reciente (hipótesis maduras, resultado OK/FAIL recién
  visto) y sabe mejor qué queda pendiente. Si se actualiza al inicio, se
  repite el ejercicio de cargar contexto. Si lo hace el VPS, no sabe de
  contexto conversacional. Al cierre es el único momento con información
  completa.
- **Override del usuario:** Hector puede editar la línea a mano cuando
  quiera forzar otra prioridad (p. ej. "hoy quiero revisar X aunque
  ayer priorizamos Y"). El override manual gana.
- **Si la sesión no cierra con decisión:** dejar la prioridad como estaba
  o escribir `prioridad: sin cambios` en la línea. No inventar.

### 6. Mover entre estados

- **en-desarrollo → activas**: requiere (a) código + config en main,
  (b) al menos un backtest en `resultados/<nombre>/` con verdict OK,
  (c) entrada de Historial justificando el pase.
- **en-desarrollo → descartadas**: dejar explícito en Historial qué
  resultado falsificó la hipótesis. El archivo se conserva completo.
- **activas → descartadas**: requiere decisión del usuario. Mover el
  `.md` y agregar entrada final de Historial con el motivo.

### 7. BITACORA

`BITACORA.md` es el único archivo append-only. Sirve para notas de
investigación sin estrategia asignada todavía (ideas crudas, papers
leídos, observaciones del mercado). Cada entrada: fecha ISO + 1-5
líneas. Cuando una entrada madura, convertirla en archivo bajo
`en-desarrollo/` y borrar su párrafo de la bitácora.

### 8. Resultados los escribe el VPS, no Claude

Los archivos bajo `resultados/<nombre>/backtest-YYYY-MM-DD.md` los genera
el script del VPS (`scripts/vps_daily.sh`). **Claude nunca los edita.**
Son consumo de lectura.

### 9. Token-efficiency

Regla operativa: si Claude está a punto de leer más de 3 archivos para
responder, debe primero preguntar al usuario qué subconjunto es relevante.
Mejor preguntar que escanear ciego.

---

## Contrato estrategia-código (cómo se conecta `.md` con ejecución)

Para cada estrategia hay exactamente tres artefactos paralelos con el
mismo `<nombre>`:

```
estrategias/<estado>/<nombre>.md         ← diseño + historial (este dir)
src/trading/strategies/<family>/<nombre>.py   ← código ejecutable
config/strategies/<prefix>_<nombre>.toml      ← parámetros (manifiesto)
```

El TOML **es** el manifiesto. No se crea un YAML paralelo — duplicaría
información y divergerían. La sección `[params]` del TOML es la fuente
de verdad de parámetros tuneables.

El `.md` de estrategia referencia las rutas exactas a `.py` y `.toml` en
su sección **Implementación**, para que Claude pueda saltar al código si
hace falta sin buscar.

El VPS dispara backtests con:

```
python -m trading.cli.backtest \
  --strategy <family>/<nombre> \
  --params config/strategies/<prefix>_<nombre>.toml \
  --from <iso> --to <iso> \
  --source <data_source>
```

---

## Cómo se conecta con `Docs/`

- `Docs/Design.md` — diseño global del sistema (FIRME/PROVISIONAL/ABIERTO).
- `Docs/decisions/NNNN-*.md` — ADRs técnicos. **Cualquier decisión que
  afecte a más de una estrategia va como ADR, no en un `.md` de estrategia.**
- `Docs/runbook.md` — ops del VPS.
- `estrategias/` — ciclo de investigación por estrategia individual.

Si una estrategia tiene una decisión que potencialmente afecta a otras
(p. ej. cambio de fuente de datos, nuevo gate compartido), abrir ADR.
