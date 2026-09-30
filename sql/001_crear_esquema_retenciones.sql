/*
    Esquema inicial para la importación de XML de Retenciones del SAT.

    Este script debe ejecutarse de forma controlada en la base configurada.
    El servicio de Python no crea ni modifica tablas automáticamente.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRANSACTION;

    CREATE TABLE dbo.Fiscal_Retenciones
    (
        id                          BIGINT IDENTITY(1, 1) NOT NULL,
        UUID                        UNIQUEIDENTIFIER NOT NULL,
        Version                     NVARCHAR(10) NOT NULL,
        FechaExp                    DATETIME2(0) NOT NULL,
        FolioInterno                NVARCHAR(50) NULL,
        LugarExpedicion             NVARCHAR(10) NULL,
        CveRetenc                   NVARCHAR(10) NOT NULL,
        DescRetenc                  NVARCHAR(255) NULL,
        NoCertificado               NVARCHAR(30) NOT NULL,
        TipoRelacion                NVARCHAR(10) NULL,
        UUIDRelacionado             UNIQUEIDENTIFIER NULL,

        EmisorRFC                   NVARCHAR(13) NOT NULL,
        EmisorRazonSocial           NVARCHAR(255) NOT NULL,
        EmisorRegimenFiscal         NVARCHAR(10) NULL,
        EmisorCURP                  NVARCHAR(18) NULL,

        ReceptorNacionalidad        NVARCHAR(20) NOT NULL,
        ReceptorRFC                 NVARCHAR(13) NULL,
        ReceptorRazonSocial         NVARCHAR(300) NOT NULL,
        ReceptorDomicilioFiscal     NVARCHAR(10) NULL,
        ReceptorNumRegIdTrib        NVARCHAR(100) NULL,
        ReceptorCURP                NVARCHAR(18) NULL,

        MesInicial                  TINYINT NOT NULL,
        MesFinal                    TINYINT NOT NULL,
        Ejercicio                   SMALLINT NOT NULL,

        MontoTotalOperacion         DECIMAL(19, 6) NOT NULL,
        MontoTotalGravado           DECIMAL(19, 6) NOT NULL,
        MontoTotalExento            DECIMAL(19, 6) NOT NULL,
        MontoTotalRetenido          DECIMAL(19, 6) NOT NULL,
        UtilidadBimestral           DECIMAL(19, 6) NULL,
        ISRCorrespondiente          DECIMAL(19, 6) NULL,

        TimbreVersion               NVARCHAR(10) NOT NULL,
        FechaTimbrado               DATETIME2(0) NOT NULL,
        RfcProveedorCertificacion   NVARCHAR(13) NULL,
        NoCertificadoSAT            NVARCHAR(30) NULL,

        HashSha256                  CHAR(64) NOT NULL,
        NombreArchivoOriginal       NVARCHAR(260) NOT NULL,
        ComplementosJson            NVARCHAR(MAX) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_ComplementosJson DEFAULT N'[]',

        CreadoEn                    DATETIME2(0) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_CreadoEn DEFAULT SYSUTCDATETIME(),
        ActualizadoEn               DATETIME2(0) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_ActualizadoEn DEFAULT SYSUTCDATETIME(),

        CONSTRAINT PK_Fiscal_Retenciones PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_Fiscal_Retenciones_UUID UNIQUE (UUID),
        CONSTRAINT CK_Fiscal_Retenciones_Relacion CHECK
            ((TipoRelacion IS NULL AND UUIDRelacionado IS NULL)
             OR (TipoRelacion IS NOT NULL AND UUIDRelacionado IS NOT NULL)),
        CONSTRAINT CK_Fiscal_Retenciones_Meses CHECK
            (MesInicial BETWEEN 1 AND 12
             AND MesFinal BETWEEN 1 AND 12
             AND MesInicial <= MesFinal),
        CONSTRAINT CK_Fiscal_Retenciones_Montos CHECK
            (MontoTotalOperacion >= 0
             AND MontoTotalGravado >= 0
             AND MontoTotalExento >= 0
             AND MontoTotalRetenido >= 0
             AND (UtilidadBimestral IS NULL OR UtilidadBimestral >= 0)
             AND (ISRCorrespondiente IS NULL OR ISRCorrespondiente >= 0)),
        CONSTRAINT CK_Fiscal_Retenciones_Hash CHECK (LEN(HashSha256) = 64),
        CONSTRAINT CK_Fiscal_Retenciones_ComplementosJson CHECK
            (ISJSON(ComplementosJson) = 1)
    );

    CREATE INDEX IX_Fiscal_Retenciones_FechaExp
        ON dbo.Fiscal_Retenciones (FechaExp DESC, id DESC);

    CREATE INDEX IX_Fiscal_Retenciones_EmisorRFC
        ON dbo.Fiscal_Retenciones (EmisorRFC, FechaExp DESC)
        INCLUDE (UUID, ReceptorRFC, MontoTotalRetenido, CveRetenc);

    CREATE INDEX IX_Fiscal_Retenciones_ReceptorRFC
        ON dbo.Fiscal_Retenciones (ReceptorRFC, FechaExp DESC)
        INCLUDE (UUID, EmisorRFC, MontoTotalRetenido, CveRetenc);

    CREATE TABLE dbo.Fiscal_Retenciones_Impuestos
    (
        id                  BIGINT IDENTITY(1, 1) NOT NULL,
        RetencionId         BIGINT NOT NULL,
        Secuencia           SMALLINT NOT NULL,
        BaseRetencion       DECIMAL(19, 6) NULL,
        Impuesto            NVARCHAR(10) NULL,
        MontoRetenido       DECIMAL(19, 6) NOT NULL,
        TipoPagoRetencion   NVARCHAR(20) NOT NULL,
        CreadoEn            DATETIME2(0) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Impuestos_CreadoEn
            DEFAULT SYSUTCDATETIME(),

        CONSTRAINT PK_Fiscal_Retenciones_Impuestos
            PRIMARY KEY CLUSTERED (id),
        CONSTRAINT FK_Fiscal_Retenciones_Impuestos_Retencion
            FOREIGN KEY (RetencionId)
            REFERENCES dbo.Fiscal_Retenciones (id),
        CONSTRAINT UQ_Fiscal_Retenciones_Impuestos_Secuencia
            UNIQUE (RetencionId, Secuencia),
        CONSTRAINT CK_Fiscal_Retenciones_Impuestos_Secuencia
            CHECK (Secuencia > 0),
        CONSTRAINT CK_Fiscal_Retenciones_Impuestos_Montos
            CHECK ((BaseRetencion IS NULL OR BaseRetencion >= 0)
                   AND MontoRetenido >= 0)
    );

    CREATE INDEX IX_Fiscal_Retenciones_Impuestos_Retencion
        ON dbo.Fiscal_Retenciones_Impuestos (RetencionId);

    CREATE TABLE dbo.Fiscal_Retenciones_Participaciones
    (
        id                      BIGINT IDENTITY(1, 1) NOT NULL,
        RetencionId             BIGINT NOT NULL,
        RazonSocialId           BIGINT NOT NULL,
        RfcCorporativo          NVARCHAR(13) NOT NULL,
        RazonSocialCarpeta      NVARCHAR(255) NOT NULL,
        Rol                     VARCHAR(8) NOT NULL,
        TipoOperacion           VARCHAR(7) NOT NULL,
        RutaXmlRelativa         NVARCHAR(1000) NOT NULL,
        CreadoEn                DATETIME2(0) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Participaciones_CreadoEn
            DEFAULT SYSUTCDATETIME(),

        CONSTRAINT PK_Fiscal_Retenciones_Participaciones
            PRIMARY KEY CLUSTERED (id),
        CONSTRAINT FK_Fiscal_Retenciones_Participaciones_Retencion
            FOREIGN KEY (RetencionId)
            REFERENCES dbo.Fiscal_Retenciones (id),
        CONSTRAINT FK_Fiscal_Retenciones_Participaciones_RazonSocial
            FOREIGN KEY (RazonSocialId)
            REFERENCES dbo.Fiscal_razonsocial (id),
        CONSTRAINT UQ_Fiscal_Retenciones_Participaciones_Rol
            UNIQUE (RetencionId, RazonSocialId, Rol),
        CONSTRAINT CK_Fiscal_Retenciones_Participaciones_RolTipo CHECK
            ((Rol = 'EMISOR' AND TipoOperacion = 'GASTO')
             OR (Rol = 'RECEPTOR' AND TipoOperacion = 'INGRESO'))
    );

    CREATE INDEX IX_Fiscal_Retenciones_Participaciones_Consulta
        ON dbo.Fiscal_Retenciones_Participaciones
            (RazonSocialId, TipoOperacion, RetencionId);

    CREATE INDEX IX_Fiscal_Retenciones_Participaciones_Rfc
        ON dbo.Fiscal_Retenciones_Participaciones
            (RfcCorporativo, TipoOperacion, RetencionId);

    CREATE TABLE dbo.Fiscal_Retenciones_Importaciones
    (
        id                      BIGINT IDENTITY(1, 1) NOT NULL,
        LoteUUID                UNIQUEIDENTIFIER NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_LoteUUID
            DEFAULT NEWID(),
        NombreZip               NVARCHAR(260) NOT NULL,
        HashZip                 CHAR(64) NOT NULL,
        RutaZipOrigen           NVARCHAR(1000) NOT NULL,
        Estado                  VARCHAR(30) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_Estado
            DEFAULT 'PENDIENTE',
        TotalArchivos           INT NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_Total DEFAULT 0,
        ArchivosImportados      INT NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_Importados DEFAULT 0,
        ArchivosDuplicados      INT NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_Duplicados DEFAULT 0,
        ArchivosConConflicto    INT NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_Conflictos DEFAULT 0,
        ArchivosConError        INT NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_Errores DEFAULT 0,
        FechaInicio             DATETIME2(0) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_Importaciones_Inicio
            DEFAULT SYSUTCDATETIME(),
        FechaFin                DATETIME2(0) NULL,
        MensajeError            NVARCHAR(MAX) NULL,

        CONSTRAINT PK_Fiscal_Retenciones_Importaciones
            PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_Fiscal_Retenciones_Importaciones_LoteUUID
            UNIQUE (LoteUUID),
        CONSTRAINT CK_Fiscal_Retenciones_Importaciones_Hash
            CHECK (LEN(HashZip) = 64),
        CONSTRAINT CK_Fiscal_Retenciones_Importaciones_Estado CHECK
            (Estado IN
                ('PENDIENTE', 'PROCESANDO', 'COMPLETADO',
                 'COMPLETADO_CON_ERRORES', 'FALLIDO')),
        CONSTRAINT CK_Fiscal_Retenciones_Importaciones_Conteos CHECK
            (TotalArchivos >= 0
             AND ArchivosImportados >= 0
             AND ArchivosDuplicados >= 0
             AND ArchivosConConflicto >= 0
             AND ArchivosConError >= 0
             AND
             (
                 Estado NOT IN ('COMPLETADO', 'COMPLETADO_CON_ERRORES')
                 OR TotalArchivos =
                    ArchivosImportados + ArchivosDuplicados
                    + ArchivosConConflicto + ArchivosConError
             )
             AND
             (
                 Estado <> 'COMPLETADO'
                 OR (ArchivosConConflicto = 0 AND ArchivosConError = 0)
             ))
    );

    CREATE INDEX IX_Fiscal_Retenciones_Importaciones_HashZip
        ON dbo.Fiscal_Retenciones_Importaciones (HashZip, FechaInicio DESC)
        INCLUDE (LoteUUID, Estado);

    CREATE TABLE dbo.Fiscal_Retenciones_ImportacionDetalle
    (
        id                  BIGINT IDENTITY(1, 1) NOT NULL,
        ImportacionId       BIGINT NOT NULL,
        RetencionId         BIGINT NULL,
        NombreArchivo       NVARCHAR(260) NOT NULL,
        RutaInternaZip      NVARCHAR(800) NOT NULL,
        HashXml             CHAR(64) NULL,
        UUID                UNIQUEIDENTIFIER NULL,
        Estado              VARCHAR(20) NOT NULL,
        Mensaje             NVARCHAR(MAX) NULL,
        CreadoEn            DATETIME2(0) NOT NULL
            CONSTRAINT DF_Fiscal_Retenciones_ImportacionDetalle_CreadoEn
            DEFAULT SYSUTCDATETIME(),

        CONSTRAINT PK_Fiscal_Retenciones_ImportacionDetalle
            PRIMARY KEY CLUSTERED (id),
        CONSTRAINT FK_Fiscal_Retenciones_ImportacionDetalle_Importacion
            FOREIGN KEY (ImportacionId)
            REFERENCES dbo.Fiscal_Retenciones_Importaciones (id),
        CONSTRAINT FK_Fiscal_Retenciones_ImportacionDetalle_Retencion
            FOREIGN KEY (RetencionId)
            REFERENCES dbo.Fiscal_Retenciones (id),
        CONSTRAINT UQ_Fiscal_Retenciones_ImportacionDetalle_Archivo
            UNIQUE (ImportacionId, RutaInternaZip),
        CONSTRAINT CK_Fiscal_Retenciones_ImportacionDetalle_Hash
            CHECK (HashXml IS NULL OR LEN(HashXml) = 64),
        CONSTRAINT CK_Fiscal_Retenciones_ImportacionDetalle_Estado CHECK
            (Estado IN ('IMPORTADO', 'DUPLICADO', 'CONFLICTO', 'ERROR'))
    );

    CREATE INDEX IX_Fiscal_Retenciones_ImportacionDetalle_Retencion
        ON dbo.Fiscal_Retenciones_ImportacionDetalle (RetencionId)
        WHERE RetencionId IS NOT NULL;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
