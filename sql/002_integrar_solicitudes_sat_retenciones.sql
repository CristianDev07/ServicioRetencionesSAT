SET NOCOUNT ON;
SET XACT_ABORT ON;

/*
    Integra el flujo de Descarga Masiva de Retenciones con el esquema actual.

    Convenciones:
      - servicio_sat distingue CFDI y RETENCIONES.
      - tipo_solicitud conserva su significado actual: CFDI o METADATA.
      - tipo_descarga representa EMITIDAS o RECIBIDAS para retenciones.
      - estado_comprobante representa VIGENTE o CANCELADO.
      - Los JSON del encabezado se conservan como resumen y auditoría.
      - Fiscal_solicitudsat_paquete es la fuente de verdad por paquete ZIP.
*/

BEGIN TRY
    BEGIN TRANSACTION;

    IF OBJECT_ID(N'dbo.Fiscal_solicitudsat', N'U') IS NULL
        THROW 51000,
            'No existe dbo.Fiscal_solicitudsat. No se puede aplicar la migración.',
            1;

    IF OBJECT_ID(N'dbo.Fiscal_Retenciones', N'U') IS NULL
        THROW 51001,
            'No existe dbo.Fiscal_Retenciones. No se puede aplicar la migración.',
            1;

    IF OBJECT_ID(N'dbo.Fiscal_Retenciones_Importaciones', N'U') IS NULL
        THROW 51002,
            'No existe dbo.Fiscal_Retenciones_Importaciones. No se puede aplicar la migración.',
            1;

    /* ================================================================
       1. Ampliación de dbo.Fiscal_solicitudsat
       ================================================================ */

    IF COL_LENGTH(N'dbo.Fiscal_solicitudsat', N'servicio_sat') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat
            ADD servicio_sat NVARCHAR(20) NULL;

        /*
            Los registros históricos existentes usan tipo_solicitud como
            formato (CFDI/METADATA). Si existiera una prueba antigua de
            retenciones, también se clasifica sin perderla.
        */
        EXEC sys.sp_executesql N'
            UPDATE dbo.Fiscal_solicitudsat
            SET servicio_sat =
                CASE
                    WHEN UPPER(LTRIM(RTRIM(tipo_solicitud))) = N''RETENCIONES''
                        THEN N''RETENCIONES''
                    ELSE N''CFDI''
                END
            WHERE servicio_sat IS NULL;

            ALTER TABLE dbo.Fiscal_solicitudsat
                ALTER COLUMN servicio_sat NVARCHAR(20) NOT NULL;

            ALTER TABLE dbo.Fiscal_solicitudsat
                ADD CONSTRAINT DF_Fiscal_solicitudsat_servicio_sat
                DEFAULT N''CFDI'' FOR servicio_sat;
        ';
    END;

    IF COL_LENGTH(N'dbo.Fiscal_solicitudsat', N'estado_comprobante') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat
            ADD estado_comprobante NVARCHAR(15) NULL;

        /*
            El flujo anterior de retenciones solamente solicitaba vigentes.
            Las solicitudes CFDI existentes permanecen en NULL porque no se
            debe inventar su criterio histórico.
        */
        EXEC sys.sp_executesql N'
            UPDATE dbo.Fiscal_solicitudsat
            SET estado_comprobante = N''VIGENTE''
            WHERE servicio_sat = N''RETENCIONES''
              AND estado_comprobante IS NULL;
        ';
    END;

    IF COL_LENGTH(N'dbo.Fiscal_solicitudsat', N'ultima_consulta_en') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat
            ADD ultima_consulta_en DATETIMEOFFSET(0) NULL;
    END;

    IF COL_LENGTH(N'dbo.Fiscal_solicitudsat', N'completada_en') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat
            ADD completada_en DATETIMEOFFSET(0) NULL;
    END;

    IF COL_LENGTH(N'dbo.Fiscal_solicitudsat', N'sin_resultados') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat
            ADD sin_resultados BIT NOT NULL
                CONSTRAINT DF_Fiscal_solicitudsat_sin_resultados
                DEFAULT ((0)) WITH VALUES;
    END;

    IF COL_LENGTH(N'dbo.Fiscal_solicitudsat', N'ultimo_error') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat
            ADD ultimo_error NVARCHAR(MAX) NULL;
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat')
          AND name = N'CK_Fiscal_solicitudsat_servicio_sat'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE dbo.Fiscal_solicitudsat WITH CHECK
                ADD CONSTRAINT CK_Fiscal_solicitudsat_servicio_sat
                CHECK (servicio_sat IN (N''CFDI'', N''RETENCIONES''));
        ';
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat')
          AND name = N'CK_Fiscal_solicitudsat_criterios_retenciones'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE dbo.Fiscal_solicitudsat WITH CHECK
                ADD CONSTRAINT CK_Fiscal_solicitudsat_criterios_retenciones
                CHECK
                (
                    servicio_sat <> N''RETENCIONES''
                    OR
                    (
                        tipo_solicitud IN (N''CFDI'', N''METADATA'')
                        AND tipo_descarga IN (N''EMITIDAS'', N''RECIBIDAS'')
                        AND estado_comprobante IN
                            (N''VIGENTE'', N''CANCELADO'')
                        AND NOT
                        (
                            tipo_descarga = N''RECIBIDAS''
                            AND estado_comprobante = N''CANCELADO''
                        )
                    )
                );
        ';
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat')
          AND name = N'CK_Fiscal_solicitudsat_respuesta_solicitud_json'
    )
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat WITH CHECK
            ADD CONSTRAINT CK_Fiscal_solicitudsat_respuesta_solicitud_json
            CHECK
            (
                respuesta_solicitud_sat IS NULL
                OR ISJSON(respuesta_solicitud_sat) = 1
            );
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat')
          AND name = N'CK_Fiscal_solicitudsat_respuesta_consulta_json'
    )
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat WITH CHECK
            ADD CONSTRAINT CK_Fiscal_solicitudsat_respuesta_consulta_json
            CHECK
            (
                respuesta_consulta_sat IS NULL
                OR ISJSON(respuesta_consulta_sat) = 1
            );
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat')
          AND name = N'CK_Fiscal_solicitudsat_ids_paquetes_json'
    )
    BEGIN
        ALTER TABLE dbo.Fiscal_solicitudsat WITH CHECK
            ADD CONSTRAINT CK_Fiscal_solicitudsat_ids_paquetes_json
            CHECK
            (
                ids_paquetes IS NULL
                OR ISJSON(ids_paquetes) = 1
            );
    END;

    /* Una cadena vacía no representa un identificador válido del SAT. */
    UPDATE dbo.Fiscal_solicitudsat
    SET id_solicitud_sat = NULL
    WHERE id_solicitud_sat IS NOT NULL
      AND LTRIM(RTRIM(id_solicitud_sat)) = N'';

    IF EXISTS
    (
        SELECT id_solicitud_sat
        FROM dbo.Fiscal_solicitudsat
        WHERE id_solicitud_sat IS NOT NULL
        GROUP BY id_solicitud_sat
        HAVING COUNT_BIG(*) > 1
    )
        THROW 51003,
            'Existen id_solicitud_sat duplicados. Corrígelos antes de crear el índice único.',
            1;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat')
          AND name = N'UX_Fiscal_solicitudsat_id_solicitud_sat'
    )
    BEGIN
        CREATE UNIQUE INDEX UX_Fiscal_solicitudsat_id_solicitud_sat
            ON dbo.Fiscal_solicitudsat (id_solicitud_sat)
            WHERE id_solicitud_sat IS NOT NULL;
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat')
          AND name = N'IX_Fiscal_solicitudsat_criterios'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            CREATE INDEX IX_Fiscal_solicitudsat_criterios
                ON dbo.Fiscal_solicitudsat
                (
                    razon_social_id,
                    servicio_sat,
                    tipo_descarga,
                    estado_comprobante,
                    tipo_solicitud,
                    fecha_inicial,
                    fecha_final,
                    creado_en DESC
                )
                INCLUDE
                (
                    id_solicitud_sat,
                    estado,
                    estado_solicitud_sat,
                    sin_resultados,
                    actualizado_en
                );
        ';
    END;

    /* ================================================================
       2. Tabla hija: un registro por paquete ZIP entregado por el SAT
       ================================================================ */

    IF OBJECT_ID(N'dbo.Fiscal_solicitudsat_paquete', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.Fiscal_solicitudsat_paquete
        (
            id                          BIGINT IDENTITY(1, 1) NOT NULL,
            solicitud_sat_id            BIGINT NOT NULL,
            id_paquete_sat              NVARCHAR(200) NOT NULL,
            estado                      NVARCHAR(30) NOT NULL
                CONSTRAINT DF_Fiscal_solicitudsat_paquete_estado
                DEFAULT N'PENDIENTE',
            intentos_descarga           INT NOT NULL
                CONSTRAINT DF_Fiscal_solicitudsat_paquete_intentos
                DEFAULT ((0)),
            fecha_descarga              DATETIMEOFFSET(0) NULL,
            procesado_en                DATETIMEOFFSET(0) NULL,
            ruta_descarga               NVARCHAR(1000) NULL,
            hash_sha256                 CHAR(64) NULL,
            tamanio_bytes               BIGINT NULL,
            codigo_estatus_descarga     NVARCHAR(100) NULL,
            mensaje_descarga            NVARCHAR(MAX) NULL,
            ultimo_error                NVARCHAR(MAX) NULL,
            creado_en                   DATETIMEOFFSET(0) NOT NULL
                CONSTRAINT DF_Fiscal_solicitudsat_paquete_creado_en
                DEFAULT SYSDATETIMEOFFSET(),
            actualizado_en              DATETIMEOFFSET(0) NOT NULL
                CONSTRAINT DF_Fiscal_solicitudsat_paquete_actualizado_en
                DEFAULT SYSDATETIMEOFFSET(),

            CONSTRAINT PK_Fiscal_solicitudsat_paquete
                PRIMARY KEY CLUSTERED (id),

            CONSTRAINT FK_Fiscal_solicitudsat_paquete_solicitud
                FOREIGN KEY (solicitud_sat_id)
                REFERENCES dbo.Fiscal_solicitudsat (id),

            CONSTRAINT UQ_Fiscal_solicitudsat_paquete_solicitud_id
                UNIQUE (solicitud_sat_id, id_paquete_sat),

            CONSTRAINT CK_Fiscal_solicitudsat_paquete_estado
                CHECK
                (
                    estado IN
                    (
                        N'PENDIENTE',
                        N'DESCARGANDO',
                        N'DESCARGADO',
                        N'ERROR_DESCARGA',
                        N'PROCESANDO',
                        N'PROCESADO',
                        N'PROCESADO_CON_ERRORES'
                    )
                ),

            CONSTRAINT CK_Fiscal_solicitudsat_paquete_intentos
                CHECK (intentos_descarga >= 0),

            CONSTRAINT CK_Fiscal_solicitudsat_paquete_hash
                CHECK (hash_sha256 IS NULL OR LEN(hash_sha256) = 64),

            CONSTRAINT CK_Fiscal_solicitudsat_paquete_tamanio
                CHECK (tamanio_bytes IS NULL OR tamanio_bytes >= 0)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'dbo.Fiscal_solicitudsat_paquete')
          AND name = N'IX_Fiscal_solicitudsat_paquete_pendientes'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            CREATE INDEX IX_Fiscal_solicitudsat_paquete_pendientes
                ON dbo.Fiscal_solicitudsat_paquete
                (
                    estado,
                    solicitud_sat_id,
                    actualizado_en
                )
                INCLUDE
                (
                    id_paquete_sat,
                    intentos_descarga,
                    ruta_descarga,
                    hash_sha256
                );
        ';
    END;

    /*
        Una misma descarga puede reprocesarse varias veces. Por ello la
        relación se guarda en Importaciones y no lleva índice UNIQUE.
    */
    IF COL_LENGTH(
        N'dbo.Fiscal_Retenciones_Importaciones',
        N'SolicitudSatPaqueteId'
    ) IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_Retenciones_Importaciones
            ADD SolicitudSatPaqueteId BIGINT NULL;
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.foreign_keys
        WHERE parent_object_id =
            OBJECT_ID(N'dbo.Fiscal_Retenciones_Importaciones')
          AND name =
            N'FK_Fiscal_Retenciones_Importaciones_SolicitudSatPaquete'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE dbo.Fiscal_Retenciones_Importaciones WITH CHECK
                ADD CONSTRAINT
                    FK_Fiscal_Retenciones_Importaciones_SolicitudSatPaquete
                FOREIGN KEY (SolicitudSatPaqueteId)
                REFERENCES dbo.Fiscal_solicitudsat_paquete (id);
        ';
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id =
            OBJECT_ID(N'dbo.Fiscal_Retenciones_Importaciones')
          AND name =
            N'IX_Fiscal_Retenciones_Importaciones_SolicitudSatPaquete'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            CREATE INDEX
                IX_Fiscal_Retenciones_Importaciones_SolicitudSatPaquete
                ON dbo.Fiscal_Retenciones_Importaciones
                    (SolicitudSatPaqueteId, FechaInicio DESC)
                WHERE SolicitudSatPaqueteId IS NOT NULL;
        ';
    END;

    /* ================================================================
       3. Estado actual del comprobante de retenciones
       ================================================================ */

    IF COL_LENGTH(N'dbo.Fiscal_Retenciones', N'EstadoComprobante') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_Retenciones
            ADD EstadoComprobante VARCHAR(10) NOT NULL
                CONSTRAINT DF_Fiscal_Retenciones_EstadoComprobante
                DEFAULT 'VIGENTE' WITH VALUES;
    END;

    IF COL_LENGTH(N'dbo.Fiscal_Retenciones', N'EstadoActualizadoEn') IS NULL
    BEGIN
        ALTER TABLE dbo.Fiscal_Retenciones
            ADD EstadoActualizadoEn DATETIME2(0) NOT NULL
                CONSTRAINT DF_Fiscal_Retenciones_EstadoActualizadoEn
                DEFAULT SYSUTCDATETIME() WITH VALUES;
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'dbo.Fiscal_Retenciones')
          AND name = N'CK_Fiscal_Retenciones_EstadoComprobante'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE dbo.Fiscal_Retenciones WITH CHECK
                ADD CONSTRAINT CK_Fiscal_Retenciones_EstadoComprobante
                CHECK (EstadoComprobante IN (''VIGENTE'', ''CANCELADO''));
        ';
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'dbo.Fiscal_Retenciones')
          AND name = N'IX_Fiscal_Retenciones_Estado_FechaExp'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            CREATE INDEX IX_Fiscal_Retenciones_Estado_FechaExp
                ON dbo.Fiscal_Retenciones
                    (EstadoComprobante, FechaExp DESC, id DESC)
                INCLUDE
                    (UUID, MontoTotalRetenido, EmisorRFC, ReceptorRFC);
        ';
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0
        ROLLBACK TRANSACTION;

    THROW;
END CATCH;
