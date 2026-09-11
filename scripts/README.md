# scripts

Carga y verificación del forecast. Todos los scripts se corren desde la raíz del
repo, con el venv activo y `PYTHONPATH` apuntando a la raíz:

```powershell
& .venv\Scripts\Activate.ps1
$env:PYTHONPATH="."
```

Los importers aceptan `--dry-run`. **Correr siempre en dry-run primero**: borran
y reescriben bloques, y un archivo con el layout corrido puede dejar la base
peor que como estaba.

---

## Las dos fuentes

El forecast no sale de un solo Excel. Cada grupo viene de una planilla distinta
y **un EID sale de una sola fuente, nunca de las dos**:

| Grupo | Archivo | Hoja | Cómo trae HL/SL |
|---|---|---|---|
| SO · PR · Tools (ARG, MX, CR) | `2026 M D - Forecast S&P.xlsx` | `Forecast Update` | por color de celda |
| S4 · Ariba · Oracle (Digi) | `01 - FORECAST CONTROL S&P.xlsm` | `S&P` | columnas explícitas |

Por qué están separados: el Forecast S&P no incluye a la gente de Digi, y el
FORECAST CONTROL clasifica distinto a los legacy — los 17 que el S&P pone en
`Tools`, el CONTROL los pone en `PR`. Mezclar los dos por offering deja el HC
descuadrado. El criterio lo definió el negocio: el S&P manda para legacy, el
CONTROL para Digi.

### Colores del Forecast S&P

La leyenda vive en la hoja `Forecast Update`, celdas `B93:B96`.

| Color | Significado | Se carga como |
|---|---|---|
| sin relleno | confirmado | HL |
| `FFFFEB9C` | Assumptions ISG Assessment | SL · `isg_assessment` |
| `FFFFCCCC` / `FFFFC7CE` | Assumptions No R | SL · `no_r` |
| `THEME4+0.8` | Assumptions R | SL · `r` |
| `FFCC66FF` | Cascadeo de horas (PPA) | HL |

El `THEME4+0.8` es la razón por la que hay que leer el color con soporte de
theme y no solo `rgb`: leyendo únicamente `rgb` esas celdas se ven como sin
relleno y las assumptions se cargan como confirmadas.

---

## Orden de ejecución

```powershell
# 1. Altas y normalización de country. Solo si el Excel trae gente nueva.
python scripts\sync_empleados.py --dry-run
python scripts\sync_empleados.py

# 2. Carga de horas y offerings desde las dos fuentes.
python scripts\import_forecast_horas.py --dry-run
python scripts\import_forecast_horas.py

# 3. Deriva las horas diarias que consume la vista Diario.
python create_daily_hours.py

# 4. Verificación contra el bloque de totales del Excel.
python scripts\verificar_forecast.py
```

El paso 1 va primero porque `import_forecast_horas.py` saltea los EIDs que no
existen en `employees`, y eso se ve como HC que no cierra contra el Excel.

El paso 3 no es opcional: `employee_daily_hours` no se recalcula sola y la vista
Diario queda mostrando la corrida anterior. `create_daily_hours.py` está en la
raíz, no en esta carpeta.

---

## `import_forecast.py` es otra cosa

No confundir con `import_forecast_horas.py`. El primero importa el **roster**
desde CSV: location, cliente, status, roll-on/roll-off, T&E approver. El
segundo carga **horas y offerings** desde los dos Excel. Son independientes.

---

## El bloque vigente se corre cada mes

En `Forecast Update` los labels de período se repiten entre el bloque anterior y
el vigente (`SEP P1` aparece en la columna 16 y en la 88). El importer lee a
partir de `--min-period-col`, que hoy vale 82.

Cuando el bloque se corra, el log va a mostrar períodos que no corresponden:

```
[9-1] Periodos vigentes: 8 -> ['Ago-P1', 'Ago-P2', ...]
```

Si esa lista no arranca en el mes que se está cargando, pasar el valor correcto:

```powershell
python scripts\import_forecast_horas.py --min-period-col 85 --dry-run
```

No hace falta editar código.

---

## Validaciones que avisan antes de escribir

Cada celda se compara contra el `CHG%` que el Excel ya trae calculado. Si no
coinciden:

```
[9-1] 3 celdas donde el CHG% calculado no coincide con el del Excel
```

Eso casi siempre significa que se están leyendo columnas de períodos distintos:
revisar `--min-period-col` antes de sacar el `--dry-run`.

---

## Dos tablas guardan el offering

| Tabla | Rol |
|---|---|
| `employees.offering` | maestro del empleado |
| `forecast_update.offering` | por acá agrupan los totales de la app |

`totals_service` arma las filas por offering con un JOIN contra
`forecast_update` (CTE `latest_fu`), no contra `employees`. Actualizar solo
`employees` deja la UI mostrando el offering viejo aunque el maestro esté bien.
`import_forecast_horas.py` escribe en las dos y verifica al final que quedaron
alineadas.

Un empleado sin ninguna fila en `forecast_update` cae en `SIN OFFERING` y su SAH
queda fuera del denominador del total.

---

## `country` usa el código corto

`AR`, `MX`, `CR`. Es lo que espera el filtro de País del front. Si se carga
`Argentina` el empleado no entra en ningún grupo de país.
`sync_empleados.py` normaliza los que estén con el formato largo.

Por eso `import_forecast_horas.py` **no toca** `location` ni `country` salvo que
se le pase `--update-location`: el Excel usa `ARG` y la base `AR`, y sobrescribir
sin querer saca a la gente de los totales por país.

---

## Pendiente

`ppa_log` no se está escribiendo. El Forecast S&P marca el cascadeo de horas con
el color `FFCC66FF` y el detalle en el comentario de la celda
(`+40hs PPA Dic P1`). Hoy el importer carga esas horas como HL —el CHG es
correcto— pero no registra la transferencia entre períodos. Son 11 celdas en la
corrida de septiembre. `import_from_excel.py` tiene ese parseo si hace falta
recuperarlo.