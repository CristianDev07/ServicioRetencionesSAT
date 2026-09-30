/*
    Corrige la longitud de TipoPagoRetencion en instalaciones existentes.

    El XML de Retenciones 1.0 puede contener los textos completos
    "Pago definitivo" (15) y "Pago provisional" (16). NVARCHAR(10)
    provoca el error SQL 8152 y revierte la importación completa.

    Este script no cambia ni elimina datos. Puede ejecutarse más de una vez.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRANSACTION;

    IF OBJECT_ID(N'dbo.Fiscal_Retenciones_Impuestos', N'U') IS NULL
        THROW 50001,
            'No existe dbo.Fiscal_Retenciones_Impuestos.',
            1;

    IF COL_LENGTH(
        N'dbo.Fiscal_Retenciones_Impuestos',
        N'TipoPagoRetencion'
    ) IS NULL
        THROW 50002,
            'No existe la columna TipoPagoRetencion.',
            1;

    IF EXISTS
    (
        SELECT 1
        FROM sys.columns AS c
        INNER JOIN sys.types AS t
            ON t.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(
            N'dbo.Fiscal_Retenciones_Impuestos'
        )
          AND c.name = N'TipoPagoRetencion'
          AND
          (
              t.name <> N'nvarchar'
              OR c.max_length < 40 -- NVARCHAR(20) usa 40 bytes.
          )
    )
    BEGIN
        ALTER TABLE dbo.Fiscal_Retenciones_Impuestos
            ALTER COLUMN TipoPagoRetencion NVARCHAR(20) NOT NULL;
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;

SELECT
    c.name AS columna,
    t.name AS tipo,
    c.max_length / 2 AS longitud_caracteres,
    c.is_nullable
FROM sys.columns AS c
INNER JOIN sys.types AS t
    ON t.user_type_id = c.user_type_id
WHERE c.object_id = OBJECT_ID(
    N'dbo.Fiscal_Retenciones_Impuestos'
)
  AND c.name = N'TipoPagoRetencion';
