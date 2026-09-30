# Retenciones SAT — versión refactorizada

Esta carpeta contiene una versión más directa del proceso de retenciones. La
lógica fiscal y las protecciones del proyecto original se conservaron, pero la
ejecución ahora se entiende leyendo cinco archivos, en este orden:

1. `main.py`: llama los cuatro pasos y no contiene reglas de negocio.
2. `solicitar_retenciones.py`: crea o reutiliza solicitudes SAT.
3. `consultar_retenciones.py`: consulta solicitudes activas una sola vez.
4. `descargar_retenciones.py`: descarga y valida paquetes ZIP.
5. `importar_retenciones.py`: importa paquetes SAT y ZIP manuales.

El código original de la carpeta superior no fue reemplazado. Permanece como
respaldo.

## Estructura

```text
proyecto_refactorizado/
|-- main.py
|-- solicitar_retenciones.py       # PASO 1
|-- consultar_retenciones.py       # PASO 2
|-- descargar_retenciones.py       # PASO 3
|-- importar_retenciones.py        # PASO 4
|-- auxiliares/
|   |-- control_ejecucion.py        # CLI, contexto, log y modo servicio
|   |-- configuracion.py            # Carga y valida .env
|   |-- modelos_retenciones.py      # Estados y objetos compartidos
|   |-- cliente_sat.py              # Comunicación HTTPS con el SAT
|   |-- operaciones_sat.py          # Reglas idempotentes de SAT
|   |-- repositorios_sql.py         # Empresas, solicitudes y paquetes en SQL
|   |-- planificador_automatico.py  # Histórico anual y ventanas diarias
|   |-- notificador_telegram.py     # Avisos sin detener el proceso
|   |-- bloqueo_instancia.py        # Evita dos ejecuciones simultáneas
|   |-- importador_archivos.py      # Auditoría y proceso de lotes
|   |-- lector_xml.py               # ZIP seguro y lectura XML
|   `-- persistencia_retenciones.py # Reglas fiscales, SQL y archivo final
|-- sql/
|-- .env.example
`-- requirements.txt
```

Cada archivo tiene una explicación inicial numerada como `Paso 1`, `Paso 2`,
etc. Las funciones privadas permanecen dentro del auxiliar que realmente las
necesita.

## Preparación

Desde PowerShell:

```powershell
*RUTA DEFINITIVA..*
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Después se deben completar los valores reales de `.env`. El archivo no se
incluye intencionalmente porque contiene secretos. La e.firma, los XML y los ZIP
tampoco deben agregarse a Git.

Los scripts `001` y `002` se conservan sólo como referencia para una instalación
nueva. **No deben ejecutarse sobre la base actual**, cuya tabla
`Fiscal_solicitudsat` ya fue modificada y no requiere cambios adicionales.

En una base realmente nueva, ejecutar con respaldo previo:

1. `sql/001_crear_esquema_retenciones.sql`.
2. `sql/002_integrar_solicitudes_sat_retenciones.sql`.

## Formas de ejecución

### Flujo automático

Ejecuta inmediatamente un ciclo del plan automático:

```powershell
python main.py
```

Para una sola empresa:

```powershell
python main.py --rfc FIM970805IY0
```

No usa las banderas `DescargaAutomatica`, `Historial` ni `Diaria`, porque son
compartidas con el servicio de CFDI. Una razón social participa cuando está
activa y tiene una e.firma utilizable.

El avance se reconstruye con las solicitudes y paquetes RETENCIONES que ya
existen en SQL; no requiere otra tabla ni un procedimiento almacenado:

1. Una empresa nueva fija como ancla la fecha de su primera ejecución.
2. Consulta seis años hacia atrás en bloques anuales, del más reciente al más
   antiguo.
3. Cada bloque termina cuando sus tres flujos están sin resultados o todos sus
   paquetes están importados correctamente.
4. Después crea una ventana `DIARIA` por fecha faltante, desde ayer a las
   `00:00:00` hasta hoy a las `23:59:59`.
5. Una diaria usa `turno_diario=1`; los bloques históricos dejan ese campo en
   `NULL`.

`SAT_HISTORICAL_COMPANIES=1` conserva una sola empresa histórica activa. Al
terminarla comienza la siguiente. Las empresas que ya terminaron el histórico
continúan sus diarias aunque todavía exista cola histórica.

### Periodo manual por fechas

Se conserva el rango inclusivo desde las `00:00:00` de la fecha inicial hasta
las `23:59:59` de la final:

```powershell
python main.py `
    --manual `
    --fecha-inicial 2025-01-01 `
    --fecha-final 2025-01-31 `
    --rfc FIM970805IY0
```

### Histórico controlado por año (prueba manual)

Este modo permite probar manualmente un solo bloque, sin alterar la cola
automática. Cada ejecución acepta solamente un año calendario y exige un RFC.
El bloque queda registrado con
`tipo_periodo=HISTORIAL` y `servicio_sat=RETENCIONES`.

Las fechas del CLI son inclusivas. Internamente y en SQL se conservan como
`00:00:00` para el día inicial y `23:59:59` para el día final. `cfdiclient`
envía al SAT el formato `AAAA-MM-DDTHH:MM:SS`, por lo que no queda un hueco
entre el cierre de un año y el inicio del siguiente.

Un bloque anual produce los tres criterios que exige el proceso:

1. Emitidas vigentes.
2. Recibidas vigentes.
3. Emitidas canceladas.

Por lo tanto, “un periodo por año” equivale a tres solicitudes SAT separadas;
el SAT no permite combinar esas tres direcciones/estados en una sola.

Ejemplo:

```powershell
python main.py `
    --historico `
    --fecha-inicial 2025-01-01 `
    --fecha-final 2025-12-31 `
    --rfc GAS060708772
```

Para la prueba controlada de Gasopipas del 8 de septiembre de 2020 al 8 de
septiembre de 2026, ejecutar **un bloque y terminar su seguimiento antes de
iniciar el siguiente**:

```powershell
python main.py --historico --fecha-inicial 2020-09-08 --fecha-final 2020-12-31 --rfc GAS060708772
python main.py --historico --fecha-inicial 2021-01-01 --fecha-final 2021-12-31 --rfc GAS060708772
python main.py --historico --fecha-inicial 2022-01-01 --fecha-final 2022-12-31 --rfc GAS060708772
python main.py --historico --fecha-inicial 2023-01-01 --fecha-final 2023-12-31 --rfc GAS060708772
python main.py --historico --fecha-inicial 2024-01-01 --fecha-final 2024-12-31 --rfc GAS060708772
python main.py --historico --fecha-inicial 2025-01-01 --fecha-final 2025-12-31 --rfc GAS060708772
python main.py --historico --fecha-inicial 2026-01-01 --fecha-final 2026-09-08 --rfc GAS060708772
```

No pegar las siete líneas de una sola vez si el objetivo es medir cada año por
separado. Después de cada línea, usar el modo de seguimiento y las consultas
SQL descritas abajo hasta que el bloque ya no tenga solicitudes pendientes.

El mismo comando es idempotente: reutiliza las solicitudes que ya coincidan en
SQL. Para revisar lo pendiente sin crear solicitudes nuevas:

```powershell
python main.py --continuar-pendientes --rfc GAS060708772
```

También puede repetirse una cantidad limitada de veces para una prueba:

```powershell
python main.py `
    --servicio `
    --continuar-pendientes `
    --rfc GAS060708772 `
    --max-ciclos 8
```

### Importador manual de ZIP

Colocar los ZIP en:

```text
{RETENCIONES_ROOT_DIR}\_procesos\Retenciones\paquetes\manuales\pendientes
```

Para importar sólo esa bandeja, sin solicitar, consultar ni descargar del SAT:

```powershell
python main.py --solo-importar-manuales
```

Una ejecución automática o por fechas también revisa la bandeja manual al
terminar el paso 4.

### Servicio y recuperación

```powershell
python main.py --servicio
python main.py --servicio --max-ciclos 1
python main.py --recuperar-paquetes --solo-validar-recuperacion
python main.py --recuperar-paquetes --rfc FIM970805IY0
```

Sin `--max-ciclos`, el servicio permanece encendido y espera todos los días la
hora `SAT_DAILY_TIME` en `SAT_TIMEZONE`. Si quedan solicitudes, paquetes,
históricos o diarias atrasadas, vuelve a revisar cada
`SAT_FOLLOWUP_MINUTES`. No crea otro periodo para una empresa hasta cerrar el
actual.

Para instalarlo con NSSM desde una consola como administrador:

```powershell
nssm install RetencionesSAT "C:\Scripts_PY\Sistema-Corporativo\proyecto_refactorizado\.venv\Scripts\python.exe"
nssm set RetencionesSAT AppDirectory "C:\Scripts_PY\Sistema-Corporativo\proyecto_refactorizado"
nssm set RetencionesSAT AppParameters "main.py --servicio"
nssm set RetencionesSAT Start SERVICE_AUTO_START
nssm start RetencionesSAT
```

NSSM sólo mantiene vivo el proceso; el horario de las 02:00 se controla en el
`.env`. Con Telegram habilitado, se avisa el inicio, las incidencias y el cierre
de cada jornada. Además, cada ciclo envía un resumen con RFC,
HISTORIAL/DIARIA, fechas, solicitudes, ZIP, XML, incidencias y la siguiente
ejecución. Un fallo de Telegram se registra en el log, pero no detiene la
descarga.

## Qué hace cada ciclo

```text
main.py
  -> planificar una empresa/año o sus diarias pendientes
  -> PASO 1 solicitar o reutilizar
  -> PASO 2 consultar
  -> PASO 3 descargar ZIP
  -> PASO 4 importar XML + revisar bandeja manual
  -> pendiente: esperar SAT_FOLLOWUP_MINUTES y continuar
  -> al corriente: esperar la próxima ejecución diaria
```

Si una empresa falla, se marca como no disponible para los pasos restantes del
ciclo, pero las demás empresas continúan. Las solicitudes y los paquetes se
reutilizan por sus estados en SQL para evitar duplicar consumo del SAT.

En `--continuar-pendientes`, el paso 1 únicamente prepara la autenticación. No
crea solicitudes y el paso 4 no mezcla la bandeja manual con la medición SAT.

## Auditoría y medición

Cada ciclo genera un identificador `ejecucion` y registra:

- duración y resultado de cada solicitud y consulta;
- estado SAT, códigos, número de comprobantes e intentos;
- bytes, MiB, SHA-256 y ruta de cada ZIP;
- respuesta reducida de descarga SAT en `respuesta_descarga_sat`, limitada a
  `cod_estatus` y `mensaje`; el contenido `paquete_b64` no se guarda en SQL;
- totales de XML importados, duplicados, conflictivos y con error;
- duración y resumen acumulado del ciclo.

El log rotativo se encuentra en `RETENCIONES_LOG_DIR` y puede observarse desde
PowerShell con:

```powershell
Get-Content "RUTA_DE_RETENCIONES_LOG_DIR\importador_retenciones.log" `
    -Tail 80 -Wait
```

El archivo no crece indefinidamente. Con los valores predeterminados, el log
activo rota al llegar a 20 MiB (`LOG_MAX_BYTES=20971520`) y conserva 10 copias
anteriores (`LOG_BACKUP_COUNT=10`): aproximadamente 220 MiB como máximo entre
el archivo actual y sus respaldos. El proceso escribe al final del archivo y no
lo lee completo en cada mensaje, por lo que el crecimiento no vuelve lenta la
ejecución.

Las consultas de seguimiento y cálculo de volumen están en
`sql/consultas_operativas_retenciones.sql`. Son exclusivamente `SELECT`; no
modifican tablas ni registros.

En bases creadas con la longitud anterior de `TipoPagoRetencion`, ejecutar una
sola vez `sql/003_ampliar_tipo_pago_retencion.sql`. El ajuste amplía la columna
de `NVARCHAR(10)` a `NVARCHAR(20)` para conservar completos los valores
`Pago definitivo` y `Pago provisional`; no elimina ni transforma registros.

Al insertar una solicitud, el código escribe explícitamente
`servicio_sat=RETENCIONES` y `fecha_solicitud=SYSDATETIMEOFFSET()`. En históricos
`turno_diario` queda en `NULL`; en ventanas diarias se guarda `1`.

## Códigos de salida

| Código | Significado |
| ---: | --- |
| `0` | Ciclo terminado sin errores reportados. |
| `1` | Una empresa, solicitud, paquete o lote tuvo error. |
| `2` | Configuración, descubrimiento o recuperación inválida. |
| `3` | Ya existe otra instancia activa. |
| `130` | Interrupción manual. |

## Mantenimiento

- `main.py` sólo debe conservar las cuatro llamadas principales.
- Una regla específica del SAT pertenece a `auxiliares/operaciones_sat.py`.
- Una consulta o actualización SQL pertenece a
  `auxiliares/repositorios_sql.py`.
- La lectura de XML pertenece a `auxiliares/lector_xml.py`.
- La clasificación y persistencia fiscal pertenecen a
  `auxiliares/persistencia_retenciones.py`.
- No se debe crear una solicitud nueva si SQL ya contiene otra con los mismos
  criterios.
- No se deben sobrescribir XML ni ZIP sin validar su SHA-256.
