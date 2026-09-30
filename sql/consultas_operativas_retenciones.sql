/*
    Consultas de sólo lectura para seguir una carga de retenciones.

    Paso 1: cambiar únicamente @Rfc y, si hace falta, las fechas.
    Paso 2: ejecutar cada SELECT por separado para revisar avance y volumen.
    Paso 3: conservar el filtro servicio_sat = N'RETENCIONES' para no mezclar
            estas solicitudes con las del servicio de CFDI.

    Este archivo NO crea, altera, actualiza ni elimina objetos o registros.
*/

DECLARE @Rfc NVARCHAR(13) = N'GAS060708772';
DECLARE @Desde DATE = '2020-09-08';
DECLARE @Hasta DATE = '2026-09-08';

/* 1. Detalle completo de solicitudes y tiempos por criterio. */
SELECT
    s.id,
    s.servicio_sat,
    s.rfc,
    s.tipo_periodo,
    s.tipo_descarga,
    s.estado_comprobante,
    s.fecha_inicial,
    s.fecha_final,
    s.fecha_solicitud,
    s.id_solicitud_sat,
    s.codigo_estatus_solicitud,
    s.estado_solicitud_sat,
    s.codigo_estado_solicitud,
    s.numero_cfdis,
    s.estado,
    s.intentos_consulta,
    s.ultima_consulta_en,
    s.completada_en,
    DATEDIFF_BIG(SECOND, s.fecha_solicitud, s.completada_en)
        AS segundos_hasta_completar,
    s.sin_resultados,
    s.paquetes_descargados,
    s.ruta_descarga,
    s.solicitud_padre_id,
    s.turno_diario,
    s.mensaje_solicitud,
    s.mensaje_consulta,
    s.ultimo_error
FROM dbo.Fiscal_solicitudsat AS s
WHERE s.servicio_sat = N'RETENCIONES'
  AND s.rfc = @Rfc
  AND s.fecha_inicial >= @Desde
  AND s.fecha_final < DATEADD(DAY, 1, @Hasta)
ORDER BY s.fecha_inicial, s.tipo_descarga, s.estado_comprobante, s.id;

/* 2. Resumen anual: deben existir tres criterios por bloque ejecutado. */
SELECT
    YEAR(s.fecha_inicial) AS anio,
    MIN(s.fecha_inicial) AS desde,
    MAX(s.fecha_final) AS hasta,
    COUNT_BIG(*) AS solicitudes,
    SUM(CASE WHEN s.estado = N'COMPLETADA' THEN 1 ELSE 0 END)
        AS completadas,
    SUM(CASE WHEN s.sin_resultados = 1 THEN 1 ELSE 0 END)
        AS sin_resultados,
    SUM(CASE WHEN s.completada_en IS NULL THEN 1 ELSE 0 END)
        AS pendientes,
    SUM(COALESCE(s.numero_cfdis, 0)) AS retenciones_reportadas_sat,
    MAX(DATEDIFF_BIG(SECOND, s.fecha_solicitud, s.completada_en))
        AS segundos_maximos_para_completar
FROM dbo.Fiscal_solicitudsat AS s
WHERE s.servicio_sat = N'RETENCIONES'
  AND s.rfc = @Rfc
  AND s.tipo_periodo = N'HISTORIAL'
  AND s.fecha_inicial >= @Desde
  AND s.fecha_final < DATEADD(DAY, 1, @Hasta)
GROUP BY YEAR(s.fecha_inicial)
ORDER BY anio;

/* 3. Solicitudes que todavía requieren nuevas consultas al SAT. */
SELECT
    s.id,
    s.fecha_inicial,
    s.fecha_final,
    s.tipo_descarga,
    s.estado_comprobante,
    s.id_solicitud_sat,
    s.estado,
    s.estado_solicitud_sat,
    s.codigo_estado_solicitud,
    s.intentos_consulta,
    s.ultima_consulta_en,
    s.ultimo_error
FROM dbo.Fiscal_solicitudsat AS s
WHERE s.servicio_sat = N'RETENCIONES'
  AND s.rfc = @Rfc
  AND s.id_solicitud_sat IS NOT NULL
  AND s.completada_en IS NULL
  AND s.estado IN
  (
      N'SOLICITUD_ACEPTADA',
      N'CONSULTANDO',
      N'ERROR_CONSULTA'
  )
ORDER BY s.fecha_inicial, s.id;

/* 4. Tamaño, descarga y procesamiento de cada ZIP. */
SELECT
    s.fecha_inicial,
    s.fecha_final,
    s.tipo_descarga,
    s.estado_comprobante,
    p.id AS paquete_sql_id,
    p.id_paquete_sat,
    p.estado AS estado_paquete,
    p.intentos_descarga,
    p.tamanio_bytes,
    CAST(COALESCE(p.tamanio_bytes, 0) / 1048576.0 AS DECIMAL(18, 3))
        AS tamanio_mib,
    p.fecha_descarga,
    p.procesado_en,
    DATEDIFF_BIG(SECOND, p.creado_en, p.fecha_descarga)
        AS segundos_hasta_descarga,
    p.hash_sha256,
    p.ruta_descarga,
    p.codigo_estatus_descarga,
    p.mensaje_descarga,
    CASE
        WHEN p.respuesta_descarga_sat IS NULL THEN NULL
        ELSE ISJSON(p.respuesta_descarga_sat)
    END AS respuesta_es_json,
    DATALENGTH(p.respuesta_descarga_sat) AS bytes_respuesta_json,
    JSON_VALUE(p.respuesta_descarga_sat, '$.cod_estatus')
        AS codigo_en_respuesta_json,
    JSON_VALUE(p.respuesta_descarga_sat, '$.mensaje')
        AS mensaje_en_respuesta_json,
    p.ultimo_error
FROM dbo.Fiscal_solicitudsat_paquete AS p
INNER JOIN dbo.Fiscal_solicitudsat AS s
    ON s.id = p.solicitud_sat_id
WHERE s.servicio_sat = N'RETENCIONES'
  AND s.rfc = @Rfc
  AND s.fecha_inicial >= @Desde
  AND s.fecha_final < DATEADD(DAY, 1, @Hasta)
ORDER BY s.fecha_inicial, p.id;

/* 5. Resultado de importación de los ZIP ligados a solicitudes SAT. */
SELECT
    s.fecha_inicial,
    s.fecha_final,
    s.tipo_descarga,
    s.estado_comprobante,
    i.id AS importacion_id,
    i.LoteUUID,
    i.NombreZip,
    i.HashZip,
    i.Estado,
    i.TotalArchivos,
    i.ArchivosImportados,
    i.ArchivosDuplicados,
    i.ArchivosConConflicto,
    i.ArchivosConError,
    i.FechaInicio,
    i.FechaFin,
    DATEDIFF_BIG(SECOND, i.FechaInicio, i.FechaFin)
        AS segundos_importacion,
    i.MensajeError
FROM dbo.Fiscal_Retenciones_Importaciones AS i
INNER JOIN dbo.Fiscal_solicitudsat_paquete AS p
    ON p.id = i.SolicitudSatPaqueteId
INNER JOIN dbo.Fiscal_solicitudsat AS s
    ON s.id = p.solicitud_sat_id
WHERE s.servicio_sat = N'RETENCIONES'
  AND s.rfc = @Rfc
  AND s.fecha_inicial >= @Desde
  AND s.fecha_final < DATEADD(DAY, 1, @Hasta)
ORDER BY s.fecha_inicial, i.id;

/* 6. Peso y XML acumulados para estimar una empresa completa. */
SELECT
    paquetes.total_paquetes,
    paquetes.total_bytes_zip,
    CAST(paquetes.total_bytes_zip / 1048576.0 AS DECIMAL(18, 3))
        AS total_mib_zip,
    importaciones.total_xml,
    importaciones.xml_importados,
    importaciones.xml_duplicados,
    importaciones.xml_con_conflicto,
    importaciones.xml_con_error
FROM
(
    SELECT
        COUNT_BIG(*) AS total_paquetes,
        SUM(COALESCE(p.tamanio_bytes, 0)) AS total_bytes_zip
    FROM dbo.Fiscal_solicitudsat AS s
    INNER JOIN dbo.Fiscal_solicitudsat_paquete AS p
        ON p.solicitud_sat_id = s.id
    WHERE s.servicio_sat = N'RETENCIONES'
      AND s.rfc = @Rfc
      AND s.fecha_inicial >= @Desde
      AND s.fecha_final < DATEADD(DAY, 1, @Hasta)
) AS paquetes
CROSS JOIN
(
    SELECT
        SUM(COALESCE(i.TotalArchivos, 0)) AS total_xml,
        SUM(COALESCE(i.ArchivosImportados, 0)) AS xml_importados,
        SUM(COALESCE(i.ArchivosDuplicados, 0)) AS xml_duplicados,
        SUM(COALESCE(i.ArchivosConConflicto, 0)) AS xml_con_conflicto,
        SUM(COALESCE(i.ArchivosConError, 0)) AS xml_con_error
    FROM dbo.Fiscal_solicitudsat AS s
    INNER JOIN dbo.Fiscal_solicitudsat_paquete AS p
        ON p.solicitud_sat_id = s.id
    INNER JOIN dbo.Fiscal_Retenciones_Importaciones AS i
        ON i.SolicitudSatPaqueteId = p.id
    WHERE s.servicio_sat = N'RETENCIONES'
      AND s.rfc = @Rfc
      AND s.fecha_inicial >= @Desde
      AND s.fecha_final < DATEADD(DAY, 1, @Hasta)
) AS importaciones;
