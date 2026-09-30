"""Acceso a SQL Server para empresas, solicitudes y paquetes.

Paso 1: descubre empresas habilitadas y valida los datos de su e.firma.
Paso 2: registra y actualiza solicitudes sin duplicarlas.
Paso 3: reserva paquetes y conserva sus estados de procesamiento.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

import pyodbc
from cryptography.fernet import Fernet, InvalidToken

from auxiliares.configuracion import SatSettings
from auxiliares.modelos_retenciones import (
    CriterioSolicitudRetenciones,
    CredencialFiel,
    EmpresaRetenciones,
    ErrorModeloRetenciones,
    EstadoSolicitudLocal,
    EstadoSolicitudSat,
    RespuestaSolicitudSat,
    RespuestaVerificacionSat,
)


class ErrorRepositorioRetenciones(RuntimeError):
    """Fallo global de configuración o acceso a la persistencia."""


@dataclass(frozen=True, slots=True)
class ProblemaEmpresaRetenciones:
    codigo: str
    mensaje: str
    razon_social_id: int | None = None
    rfc: str | None = None
    razon_social: str | None = None


@dataclass(frozen=True, slots=True)
class ResultadoDescubrimientoEmpresas:
    total_candidatas: int
    empresas: tuple[EmpresaRetenciones, ...]
    problemas: tuple[ProblemaEmpresaRetenciones, ...]

    @property
    def total_disponibles(self) -> int:
        return len(self.empresas)

    @property
    def total_con_problemas(self) -> int:
        return len(self.problemas)


class RepositorioEmpresasRetenciones:
    """Obtiene las razones sociales habilitadas y valida sus e.firmas.

    Esta primera etapa del repositorio es de solo lectura. No crea ni modifica
    solicitudes SAT y tampoco cambia registros de razones sociales.
    """

    def __init__(self, settings: SatSettings) -> None:
        self._settings = settings
        self._storage_dir = settings.fiel.storage_dir.resolve()
        try:
            self._fernet = Fernet(settings.fiel.fernet_key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ErrorRepositorioRetenciones(
                "FERNET_KEY no tiene un formato Fernet válido."
            ) from exc

    def descubrir_empresas(self) -> ResultadoDescubrimientoEmpresas:
        """Devuelve empresas utilizables y problemas aislados por empresa."""

        if not self._storage_dir.is_dir():
            raise ErrorRepositorioRetenciones(
                "No existe el directorio configurado en FIEL_STORAGE_DIR: "
                f"{self._storage_dir}"
            )

        conexion = self._abrir_conexion()
        try:
            rows = conexion.cursor().execute(self._consulta_empresas()).fetchall()
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible consultar las razones sociales y sus e.firmas."
            ) from exc
        finally:
            conexion.close()

        empresas: list[EmpresaRetenciones] = []
        problemas: list[ProblemaEmpresaRetenciones] = []
        for row in rows:
            empresa, problema = self._convertir_empresa(row)
            if empresa is not None:
                empresas.append(empresa)
            if problema is not None:
                problemas.append(problema)

        return ResultadoDescubrimientoEmpresas(
            total_candidatas=len(rows),
            empresas=tuple(empresas),
            problemas=tuple(problemas),
        )

    def descifrar_contrasena(
        self,
        credencial: CredencialFiel,
    ) -> str:
        """Descifra la contraseña exclusivamente cuando el cliente la usa."""

        try:
            contrasena = self._fernet.decrypt(
                credencial.contrasena_cifrada.encode("utf-8")
            ).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible descifrar la contraseña de la e.firma."
            ) from exc

        if not contrasena:
            raise ErrorRepositorioRetenciones(
                "La contraseña descifrada de la e.firma está vacía."
            )
        return contrasena

    def _abrir_conexion(self) -> pyodbc.Connection:
        try:
            return pyodbc.connect(
                self._settings.base.database.connection_string,
                autocommit=False,
            )
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible conectar con SQL Server."
            ) from exc

    @staticmethod
    def _consulta_empresas() -> str:
        return """
            SELECT
                rs.id,
                rs.rfc,
                rs.razon_social,
                cd.certificado_cer,
                cd.clave_privada_key,
                cd.contrasena_clave_privada
            FROM dbo.Fiscal_razonsocial AS rs
            LEFT JOIN dbo.Fiscal_certificadodigital AS cd
                ON cd.razon_social_id = rs.id
            WHERE rs.Activo_Inactivo = 1
            ORDER BY rs.id
        """

    def _convertir_empresa(
        self,
        row: Any,
    ) -> tuple[
        EmpresaRetenciones | None,
        ProblemaEmpresaRetenciones | None,
    ]:
        razon_social_id = _entero_opcional(row.id)
        rfc = _texto_opcional(row.rfc)
        razon_social = _texto_opcional(row.razon_social)

        contexto = {
            "razon_social_id": razon_social_id,
            "rfc": rfc,
            "razon_social": razon_social,
        }

        if row.certificado_cer is None and row.clave_privada_key is None:
            return None, ProblemaEmpresaRetenciones(
                codigo="SIN_CERTIFICADO_DIGITAL",
                mensaje=(
                    "La razón social no tiene un registro en "
                    "Fiscal_certificadodigital."
                ),
                **contexto,
            )

        certificado = _texto_opcional(row.certificado_cer)
        llave = _texto_opcional(row.clave_privada_key)
        contrasena_cifrada = _secreto_opcional(
            row.contrasena_clave_privada
        )

        if not certificado:
            return None, ProblemaEmpresaRetenciones(
                codigo="CERTIFICADO_CER_NO_CONFIGURADO",
                mensaje="No hay un nombre de certificado .cer en SQL.",
                **contexto,
            )
        if not llave:
            return None, ProblemaEmpresaRetenciones(
                codigo="CLAVE_KEY_NO_CONFIGURADA",
                mensaje="No hay un nombre de llave privada .key en SQL.",
                **contexto,
            )
        if not contrasena_cifrada:
            return None, ProblemaEmpresaRetenciones(
                codigo="CONTRASENA_NO_CONFIGURADA",
                mensaje="No hay una contraseña cifrada de e.firma en SQL.",
                **contexto,
            )

        try:
            ruta_certificado = self._resolver_archivo(
                certificado,
                extension=".cer",
            )
            ruta_llave = self._resolver_archivo(
                llave,
                extension=".key",
            )
        except ErrorRepositorioRetenciones as exc:
            return None, ProblemaEmpresaRetenciones(
                codigo="RUTA_CREDENCIAL_INVALIDA",
                mensaje=str(exc),
                **contexto,
            )

        if not ruta_certificado.is_file():
            return None, ProblemaEmpresaRetenciones(
                codigo="CERTIFICADO_CER_NO_ENCONTRADO",
                mensaje=f"No se encontró el certificado: {ruta_certificado}",
                **contexto,
            )
        if not ruta_llave.is_file():
            return None, ProblemaEmpresaRetenciones(
                codigo="CLAVE_KEY_NO_ENCONTRADA",
                mensaje=f"No se encontró la llave privada: {ruta_llave}",
                **contexto,
            )

        credencial = CredencialFiel(
            certificado_cer=ruta_certificado,
            clave_privada_key=ruta_llave,
            contrasena_cifrada=contrasena_cifrada,
        )
        try:
            self.descifrar_contrasena(credencial)
            empresa = EmpresaRetenciones(
                razon_social_id=int(row.id),
                rfc=str(row.rfc),
                razon_social=str(row.razon_social),
                fiel=credencial,
            )
        except (ErrorModeloRetenciones, ErrorRepositorioRetenciones) as exc:
            return None, ProblemaEmpresaRetenciones(
                codigo="CREDENCIAL_O_EMPRESA_INVALIDA",
                mensaje=str(exc),
                **contexto,
            )

        return empresa, None

    def _resolver_archivo(self, value: str, *, extension: str) -> Path:
        """Resuelve solo el nombre final guardado por Django.

        También tolera registros históricos con una ruta relativa, pero
        ignora sus directorios y conserva exclusivamente el nombre seguro.
        """

        normalizado = value.strip().replace("\\", "/")
        ruta_sql = PurePosixPath(normalizado)
        nombre = ruta_sql.name
        if not nombre or nombre in {".", ".."}:
            raise ErrorRepositorioRetenciones(
                f"Nombre de archivo inválido en SQL: {value!r}."
            )
        if Path(nombre).suffix.lower() != extension:
            raise ErrorRepositorioRetenciones(
                f"El archivo {nombre!r} debe terminar en {extension}."
            )

        destino = (self._storage_dir / nombre).resolve()
        if destino.parent != self._storage_dir:
            raise ErrorRepositorioRetenciones(
                f"El archivo {nombre!r} sale de FIEL_STORAGE_DIR."
            )
        return destino


@dataclass(frozen=True, slots=True)
class SolicitudSatRegistrada:
    id: int
    razon_social_id: int
    rfc: str
    tipo_periodo: str
    tipo_descarga: str
    tipo_solicitud: str
    estado_comprobante: str
    fecha_inicial: datetime
    fecha_final: datetime
    id_solicitud_sat: str | None
    codigo_estatus_solicitud: str | None
    estado_solicitud_sat: int | None
    codigo_estado_solicitud: str | None
    numero_cfdis: int | None
    ids_paquetes: tuple[str, ...]
    sin_resultados: bool
    estado: str
    intentos_consulta: int
    servicio_sat: str
    solicitud_padre_id: int | None
    fecha_solicitud: datetime | None
    turno_diario: int | None


@dataclass(frozen=True, slots=True)
class PaqueteSatRegistrado:
    id: int
    solicitud_sat_id: int
    razon_social_id: int
    rfc: str
    tipo_descarga: str
    estado_comprobante: str
    fecha_inicial: datetime
    fecha_final: datetime
    id_paquete_sat: str
    estado: str
    intentos_descarga: int
    ruta_descarga: str | None
    hash_sha256: str | None
    tamanio_bytes: int | None
    codigo_estatus_descarga: str | None
    mensaje_descarga: str | None
    ultimo_error: str | None


@dataclass(frozen=True, slots=True)
class EstadoPlanificacionRetenciones:
    """Datos mínimos usados para decidir el siguiente periodo automático."""

    razon_social_id: int
    tipo_periodo: str
    tipo_descarga: str
    estado_comprobante: str
    fecha_inicial: datetime
    fecha_final: datetime
    id_solicitud_sat: str | None
    estado: str
    sin_resultados: bool
    numero_cfdis: int | None
    total_paquetes: int
    paquetes_procesados: int


class RepositorioSolicitudesRetenciones:
    """Persistencia transaccional de solicitudes y paquetes SAT."""

    def __init__(self, settings: SatSettings) -> None:
        self._settings = settings

    def buscar_reutilizable(
        self,
        criterio: CriterioSolicitudRetenciones,
    ) -> SolicitudSatRegistrada | None:
        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            row = cursor.execute(
                self._select_solicitud()
                + """
                    WHERE razon_social_id = ?
                      AND servicio_sat = N'RETENCIONES'
                      AND tipo_periodo = ?
                      AND tipo_descarga = ?
                      AND estado_comprobante = ?
                      AND tipo_solicitud = ?
                      AND fecha_inicial = ?
                      AND fecha_final = ?
                      AND id_solicitud_sat IS NOT NULL
                      AND estado <> N'ERROR_SOLICITUD'
                      AND
                      (
                          estado <> N'ERROR_CONSULTA'
                          OR completada_en IS NULL
                      )
                    ORDER BY id DESC
                """,
                criterio.razon_social_id,
                criterio.tipo_periodo.value,
                criterio.tipo_descarga.value,
                criterio.estado_comprobante.value,
                criterio.tipo_solicitud.value,
                criterio.fecha_inicial,
                criterio.fecha_final,
            ).fetchone()
            return _fila_a_solicitud(row) if row is not None else None
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible buscar solicitudes reutilizables."
            ) from exc
        finally:
            conexion.close()

    def obtener_por_id(
        self,
        solicitud_id: int,
    ) -> SolicitudSatRegistrada | None:
        conexion = self._abrir_conexion()
        try:
            return self._obtener_por_id_conexion(conexion, solicitud_id)
        finally:
            conexion.close()

    def listar_solicitudes_por_consultar(
        self,
        razon_social_id: int | None = None,
    ) -> tuple[SolicitudSatRegistrada, ...]:
        """Devuelve todas las solicitudes RETENCIONES todavía activas."""

        parametros: list[Any] = []
        filtro_empresa = ""
        if razon_social_id is not None:
            if razon_social_id <= 0:
                raise ErrorRepositorioRetenciones(
                    "razon_social_id debe ser un entero positivo."
                )
            filtro_empresa = " AND razon_social_id = ?"
            parametros.append(razon_social_id)

        conexion = self._abrir_conexion()
        try:
            rows = conexion.cursor().execute(
                self._select_solicitud()
                + """
                    WHERE servicio_sat = N'RETENCIONES'
                      AND id_solicitud_sat IS NOT NULL
                      AND completada_en IS NULL
                      AND estado IN
                      (
                          N'SOLICITUD_ACEPTADA',
                          N'CONSULTANDO',
                          N'ERROR_CONSULTA'
                      )
                """
                + filtro_empresa
                + " ORDER BY id",
                *parametros,
            ).fetchall()
            return tuple(_fila_a_solicitud(row) for row in rows)
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible consultar las solicitudes SAT pendientes."
            ) from exc
        finally:
            conexion.close()

    def listar_estado_planificacion(
        self,
        razones_sociales: tuple[int, ...],
    ) -> tuple[EstadoPlanificacionRetenciones, ...]:
        """Lee en una sola consulta el avance RETENCIONES de las empresas."""

        ids = tuple(sorted(set(int(item) for item in razones_sociales)))
        if not ids:
            return ()
        if any(item <= 0 for item in ids):
            raise ErrorRepositorioRetenciones(
                "Las razones sociales deben tener identificadores positivos."
            )

        marcadores = ", ".join("?" for _ in ids)
        consulta = f"""
            SELECT
                s.razon_social_id,
                s.tipo_periodo,
                s.tipo_descarga,
                s.estado_comprobante,
                s.fecha_inicial,
                s.fecha_final,
                s.id_solicitud_sat,
                s.estado,
                s.sin_resultados,
                s.numero_cfdis,
                COUNT_BIG(p.id) AS total_paquetes,
                COALESCE(SUM(
                    CASE WHEN p.estado = N'PROCESADO' THEN 1 ELSE 0 END
                ), 0) AS paquetes_procesados
            FROM dbo.Fiscal_solicitudsat AS s
            LEFT JOIN dbo.Fiscal_solicitudsat_paquete AS p
                ON p.solicitud_sat_id = s.id
            WHERE s.servicio_sat = N'RETENCIONES'
              AND s.tipo_periodo IN (N'HISTORIAL', N'DIARIA')
              AND s.razon_social_id IN ({marcadores})
            GROUP BY
                s.id,
                s.razon_social_id,
                s.tipo_periodo,
                s.tipo_descarga,
                s.estado_comprobante,
                s.fecha_inicial,
                s.fecha_final,
                s.id_solicitud_sat,
                s.estado,
                s.sin_resultados,
                s.numero_cfdis
            ORDER BY s.razon_social_id, s.id
        """

        conexion = self._abrir_conexion()
        try:
            rows = conexion.cursor().execute(consulta, *ids).fetchall()
            return tuple(
                EstadoPlanificacionRetenciones(
                    razon_social_id=int(row.razon_social_id),
                    tipo_periodo=str(row.tipo_periodo or "").strip().upper(),
                    tipo_descarga=str(row.tipo_descarga or "").strip().upper(),
                    estado_comprobante=str(
                        row.estado_comprobante or ""
                    ).strip().upper(),
                    fecha_inicial=_fecha_hora_sql(
                        row.fecha_inicial,
                        es_final=False,
                    ),
                    fecha_final=_fecha_hora_sql(
                        row.fecha_final,
                        es_final=True,
                    ),
                    id_solicitud_sat=_texto_opcional(row.id_solicitud_sat),
                    estado=str(row.estado or "").strip().upper(),
                    sin_resultados=bool(row.sin_resultados),
                    numero_cfdis=(
                        int(row.numero_cfdis)
                        if row.numero_cfdis is not None
                        else None
                    ),
                    total_paquetes=int(row.total_paquetes),
                    paquetes_procesados=int(row.paquetes_procesados),
                )
                for row in rows
            )
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible leer el avance de periodos automáticos."
            ) from exc
        finally:
            conexion.close()

    def registrar_respuesta_solicitud(
        self,
        criterio: CriterioSolicitudRetenciones,
        respuesta: RespuestaSolicitudSat,
    ) -> SolicitudSatRegistrada:
        """Registra la respuesta inicial sin reemplazar IDs ya auditados."""

        if respuesta.id_solicitud and len(respuesta.id_solicitud) > 200:
            raise ErrorRepositorioRetenciones(
                "El id_solicitud_sat excede los 200 caracteres permitidos."
            )

        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            if respuesta.id_solicitud:
                existente = cursor.execute(
                    self._select_solicitud()
                    + " WHERE id_solicitud_sat = ?",
                    respuesta.id_solicitud,
                ).fetchone()
                if existente is not None:
                    solicitud = _fila_a_solicitud(existente)
                    _validar_solicitud_coincidente(solicitud, criterio)
                    conexion.rollback()
                    return solicitud

            estado = (
                EstadoSolicitudLocal.SOLICITUD_ACEPTADA
                if respuesta.aceptada
                else EstadoSolicitudLocal.ERROR_SOLICITUD
            )
            ultimo_error = None if respuesta.aceptada else respuesta.mensaje
            row = cursor.execute(
                """
                    INSERT INTO dbo.Fiscal_solicitudsat
                    (
                        razon_social_id,
                        rfc,
                        tipo_periodo,
                        tipo_descarga,
                        tipo_solicitud,
                        fecha_inicial,
                        fecha_final,
                        id_solicitud_sat,
                        codigo_estatus_solicitud,
                        mensaje_solicitud,
                        respuesta_solicitud_sat,
                        ids_paquetes,
                        estado,
                        intentos_consulta,
                        servicio_sat,
                        estado_comprobante,
                        sin_resultados,
                        ultimo_error,
                        solicitud_padre_id,
                        fecha_solicitud,
                        turno_diario
                    )
                    OUTPUT INSERTED.id
                    VALUES
                    (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        N'[]', ?, 0, ?, ?, 0, ?,
                        NULL, SYSDATETIMEOFFSET(), ?
                    )
                """,
                criterio.razon_social_id,
                criterio.rfc,
                criterio.tipo_periodo.value,
                criterio.tipo_descarga.value,
                criterio.tipo_solicitud.value,
                criterio.fecha_inicial,
                criterio.fecha_final,
                respuesta.id_solicitud,
                respuesta.codigo_estatus,
                respuesta.mensaje,
                _a_json(respuesta.respuesta_cruda),
                estado.value,
                criterio.servicio_sat.value,
                criterio.estado_comprobante.value,
                ultimo_error,
                criterio.turno_diario,
            ).fetchone()
            if row is None:
                raise ErrorRepositorioRetenciones(
                    "SQL Server no devolvió el ID de la solicitud insertada."
                )
            solicitud_id = int(row[0])
            solicitud = self._obtener_por_id_conexion(
                conexion,
                solicitud_id,
            )
            if solicitud is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar la solicitud insertada."
                )
            # Confirmar solamente después de comprobar que el registro puede
            # recuperarse completo. Así, cualquier incompatibilidad del
            # controlador ODBC revierte realmente toda la operación.
            conexion.commit()
            return solicitud
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible registrar la respuesta de solicitud SAT."
            ) from exc
        finally:
            conexion.close()

    def actualizar_verificacion(
        self,
        solicitud_id: int,
        respuesta: RespuestaVerificacionSat,
    ) -> SolicitudSatRegistrada:
        """Actualiza el encabezado y crea los paquetes en una transacción."""

        estado_local, completada, ultimo_error = (
            _clasificar_verificacion(respuesta)
        )
        ids_paquetes = tuple(respuesta.ids_paquetes)
        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            existente = self._obtener_por_id_conexion(
                conexion,
                solicitud_id,
            )
            if existente is None:
                raise ErrorRepositorioRetenciones(
                    f"No existe la solicitud de retenciones {solicitud_id}."
                )
            if existente.estado in {
                EstadoSolicitudLocal.COMPLETADA.value,
                EstadoSolicitudLocal.COMPLETADA_SIN_RESULTADOS.value,
            }:
                conexion.rollback()
                return existente

            cursor.execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat
                    SET codigo_estatus_consulta = ?,
                        estado_solicitud_sat = ?,
                        codigo_estado_solicitud = ?,
                        numero_cfdis = ?,
                        mensaje_consulta = ?,
                        respuesta_consulta_sat = ?,
                        ids_paquetes = ?,
                        ultima_consulta_en = SYSDATETIMEOFFSET(),
                        completada_en =
                            CASE
                                WHEN ? = 1
                                    THEN COALESCE(
                                        completada_en,
                                        SYSDATETIMEOFFSET()
                                    )
                                ELSE completada_en
                            END,
                        sin_resultados = ?,
                        estado = ?,
                        intentos_consulta = intentos_consulta + 1,
                        ultimo_error = ?,
                        actualizado_en = SYSDATETIMEOFFSET()
                    WHERE id = ?
                      AND servicio_sat = N'RETENCIONES'
                """,
                respuesta.codigo_estatus,
                int(respuesta.estado_solicitud),
                respuesta.codigo_estado_solicitud,
                respuesta.numero_cfdis,
                respuesta.mensaje,
                _a_json(respuesta.respuesta_cruda),
                _a_json(ids_paquetes),
                int(completada),
                int(respuesta.sin_resultados),
                estado_local.value,
                ultimo_error,
                solicitud_id,
            )

            for id_paquete in ids_paquetes:
                cursor.execute(
                    """
                        IF NOT EXISTS
                        (
                            SELECT 1
                            FROM dbo.Fiscal_solicitudsat_paquete
                                WITH (UPDLOCK, HOLDLOCK)
                            WHERE solicitud_sat_id = ?
                              AND id_paquete_sat = ?
                        )
                        BEGIN
                            INSERT INTO dbo.Fiscal_solicitudsat_paquete
                            (
                                solicitud_sat_id,
                                id_paquete_sat,
                                estado
                            )
                            VALUES (?, ?, N'PENDIENTE');
                        END;
                    """,
                    solicitud_id,
                    id_paquete,
                    solicitud_id,
                    id_paquete,
                )

            conexion.commit()
            actualizado = self._obtener_por_id_conexion(
                conexion,
                solicitud_id,
            )
            if actualizado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar la solicitud actualizada."
                )
            return actualizado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible actualizar la verificación SAT."
            ) from exc
        finally:
            conexion.close()

    def registrar_error_consulta(
        self,
        solicitud_id: int,
        mensaje: str,
    ) -> SolicitudSatRegistrada:
        """Registra un fallo transitorio sin perder el identificador SAT."""

        error = str(mensaje or "").strip()
        if not error:
            error = "Error de comunicación durante la consulta al SAT."

        conexion = self._abrir_conexion()
        try:
            existente = self._obtener_por_id_conexion(
                conexion,
                solicitud_id,
            )
            if existente is None:
                raise ErrorRepositorioRetenciones(
                    f"No existe la solicitud de retenciones {solicitud_id}."
                )
            if existente.estado in {
                EstadoSolicitudLocal.COMPLETADA.value,
                EstadoSolicitudLocal.COMPLETADA_SIN_RESULTADOS.value,
            }:
                conexion.rollback()
                return existente

            conexion.cursor().execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat
                    SET estado = N'ERROR_CONSULTA',
                        intentos_consulta = intentos_consulta + 1,
                        ultima_consulta_en = SYSDATETIMEOFFSET(),
                        ultimo_error = ?,
                        actualizado_en = SYSDATETIMEOFFSET()
                    WHERE id = ?
                      AND servicio_sat = N'RETENCIONES'
                """,
                error,
                solicitud_id,
            )
            conexion.commit()
            actualizado = self._obtener_por_id_conexion(
                conexion,
                solicitud_id,
            )
            if actualizado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar la solicitud con error."
                )
            return actualizado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible registrar el error de consulta SAT."
            ) from exc
        finally:
            conexion.close()

    def listar_paquetes_por_descargar(
        self,
        razon_social_id: int | None = None,
    ) -> tuple[PaqueteSatRegistrado, ...]:
        """Lista paquetes pendientes, errores reintentables y reservas vencidas."""

        parametros: list[Any] = [-self._settings.download_lease_minutes]
        filtro_empresa = ""
        if razon_social_id is not None:
            if razon_social_id <= 0:
                raise ErrorRepositorioRetenciones(
                    "razon_social_id debe ser un entero positivo."
                )
            filtro_empresa = " AND s.razon_social_id = ?"
            parametros.append(razon_social_id)

        conexion = self._abrir_conexion()
        try:
            rows = conexion.cursor().execute(
                self._select_paquete()
                + """
                    WHERE s.servicio_sat = N'RETENCIONES'
                      AND
                      (
                          p.estado IN (N'PENDIENTE', N'ERROR_DESCARGA')
                          OR
                          (
                              p.estado = N'DESCARGANDO'
                              AND p.actualizado_en < DATEADD(
                                  MINUTE,
                                  ?,
                                  SYSDATETIMEOFFSET()
                              )
                          )
                      )
                """
                + filtro_empresa
                + " ORDER BY p.id",
                *parametros,
            ).fetchall()
            return tuple(_fila_a_paquete(row) for row in rows)
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible consultar los paquetes pendientes."
            ) from exc
        finally:
            conexion.close()

    def obtener_paquete_por_id(
        self,
        paquete_id: int,
    ) -> PaqueteSatRegistrado | None:
        conexion = self._abrir_conexion()
        try:
            return self._obtener_paquete_por_id_conexion(
                conexion,
                paquete_id,
            )
        finally:
            conexion.close()

    def listar_paquetes_por_procesar(
        self,
        razon_social_id: int | None = None,
    ) -> tuple[PaqueteSatRegistrado, ...]:
        """Lista ZIP descargados, fallidos y concesiones vencidas."""

        parametros: list[Any] = [-self._settings.download_lease_minutes]
        filtro_empresa = ""
        if razon_social_id is not None:
            if razon_social_id <= 0:
                raise ErrorRepositorioRetenciones(
                    "razon_social_id debe ser un entero positivo."
                )
            filtro_empresa = " AND s.razon_social_id = ?"
            parametros.append(razon_social_id)

        conexion = self._abrir_conexion()
        try:
            rows = conexion.cursor().execute(
                self._select_paquete()
                + """
                    WHERE s.servicio_sat = N'RETENCIONES'
                      AND
                      (
                          p.estado IN
                          (
                              N'DESCARGADO',
                              N'PROCESADO_CON_ERRORES'
                          )
                          OR
                          (
                              p.estado = N'PROCESANDO'
                              AND p.actualizado_en < DATEADD(
                                  MINUTE,
                                  ?,
                                  SYSDATETIMEOFFSET()
                              )
                          )
                      )
                """
                + filtro_empresa
                + " ORDER BY p.id",
                *parametros,
            ).fetchall()
            return tuple(_fila_a_paquete(row) for row in rows)
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible consultar los paquetes por procesar."
            ) from exc
        finally:
            conexion.close()

    def reservar_paquete_para_procesar(
        self,
        paquete_id: int,
    ) -> PaqueteSatRegistrado | None:
        """Reserva un ZIP descargado sin permitir dos importadores activos."""

        conexion = self._abrir_conexion()
        try:
            row = conexion.cursor().execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat_paquete WITH (ROWLOCK)
                    SET estado = N'PROCESANDO',
                        ultimo_error = NULL,
                        actualizado_en = SYSDATETIMEOFFSET()
                    OUTPUT INSERTED.id
                    WHERE id = ?
                      AND
                      (
                          estado IN
                          (
                              N'DESCARGADO',
                              N'PROCESADO_CON_ERRORES'
                          )
                          OR
                          (
                              estado = N'PROCESANDO'
                              AND actualizado_en < DATEADD(
                                  MINUTE,
                                  ?,
                                  SYSDATETIMEOFFSET()
                              )
                          )
                      )
                """,
                paquete_id,
                -self._settings.download_lease_minutes,
            ).fetchone()
            if row is None:
                conexion.rollback()
                return None

            conexion.commit()
            reservado = self._obtener_paquete_por_id_conexion(
                conexion,
                int(row[0]),
            )
            if reservado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar el paquete reservado."
                )
            return reservado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible reservar el paquete para procesarlo."
            ) from exc
        finally:
            conexion.close()

    def renovar_procesamiento_paquete(self, paquete_id: int) -> None:
        """Mantiene vigente la concesión mientras se recorren sus XML."""

        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            cursor.execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat_paquete
                    SET actualizado_en = SYSDATETIMEOFFSET()
                    WHERE id = ?
                      AND estado = N'PROCESANDO'
                """,
                paquete_id,
            )
            if cursor.rowcount != 1:
                raise ErrorRepositorioRetenciones(
                    "El paquete ya no está reservado para procesamiento."
                )
            conexion.commit()
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible renovar la reserva de procesamiento."
            ) from exc
        finally:
            conexion.close()

    def finalizar_procesamiento_paquete(
        self,
        paquete_id: int,
        *,
        estado: str,
        ruta_descarga: str,
        mensaje_error: str | None = None,
    ) -> PaqueteSatRegistrado:
        estado_final = str(estado or "").strip().upper()
        if estado_final not in {"PROCESADO", "PROCESADO_CON_ERRORES"}:
            raise ErrorRepositorioRetenciones(
                "El estado final debe ser PROCESADO o "
                "PROCESADO_CON_ERRORES."
            )
        ruta = _normalizar_ruta_relativa(ruta_descarga)
        error = str(mensaje_error or "").strip() or None
        if estado_final == "PROCESADO" and error is not None:
            raise ErrorRepositorioRetenciones(
                "Un paquete PROCESADO no debe conservar ultimo_error."
            )

        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            cursor.execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat_paquete
                    SET estado = ?,
                        procesado_en = SYSDATETIMEOFFSET(),
                        ruta_descarga = ?,
                        ultimo_error = ?,
                        actualizado_en = SYSDATETIMEOFFSET()
                    WHERE id = ?
                      AND estado = N'PROCESANDO'
                """,
                estado_final,
                ruta,
                error,
                paquete_id,
            )
            if cursor.rowcount != 1:
                raise ErrorRepositorioRetenciones(
                    "El paquete no está reservado para procesamiento."
                )

            # La cabecera conserva una ruta resumida. Debe acompañar al ZIP
            # cuando pasa de pendientes a procesados o errores; el detalle
            # completo de todos los paquetes permanece en la tabla hija.
            cursor.execute(
                """
                    UPDATE s
                    SET ruta_descarga = ?,
                        actualizado_en = SYSDATETIMEOFFSET()
                    FROM dbo.Fiscal_solicitudsat AS s
                    WHERE s.id =
                    (
                        SELECT solicitud_sat_id
                        FROM dbo.Fiscal_solicitudsat_paquete
                        WHERE id = ?
                    )
                      AND s.servicio_sat = N'RETENCIONES'
                """,
                ruta,
                paquete_id,
            )
            conexion.commit()
            actualizado = self._obtener_paquete_por_id_conexion(
                conexion,
                paquete_id,
            )
            if actualizado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar el paquete procesado."
                )
            return actualizado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible finalizar el procesamiento del paquete."
            ) from exc
        finally:
            conexion.close()

    def devolver_paquete_a_descarga(
        self,
        paquete_id: int,
        mensaje: str,
    ) -> PaqueteSatRegistrado:
        """Reabre la descarga cuando el ZIP físico falta o perdió integridad."""

        error = str(mensaje or "").strip() or (
            "El ZIP descargado no está disponible o perdió integridad."
        )
        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            cursor.execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat_paquete
                    SET estado = N'ERROR_DESCARGA',
                        codigo_estatus_descarga = N'ARCHIVO_LOCAL_INVALIDO',
                        mensaje_descarga = ?,
                        ultimo_error = ?,
                        actualizado_en = SYSDATETIMEOFFSET()
                    WHERE id = ?
                      AND estado = N'PROCESANDO'
                """,
                error,
                error,
                paquete_id,
            )
            if cursor.rowcount != 1:
                raise ErrorRepositorioRetenciones(
                    "El paquete no está reservado para procesamiento."
                )
            conexion.commit()
            actualizado = self._obtener_paquete_por_id_conexion(
                conexion,
                paquete_id,
            )
            if actualizado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar el paquete reabierto."
                )
            return actualizado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible devolver el paquete a descarga."
            ) from exc
        finally:
            conexion.close()

    def reservar_paquete(
        self,
        paquete_id: int,
    ) -> PaqueteSatRegistrado | None:
        """Obtiene una concesión exclusiva y recuperable para descargar."""

        conexion = self._abrir_conexion()
        try:
            row = conexion.cursor().execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat_paquete WITH (ROWLOCK)
                    SET estado = N'DESCARGANDO',
                        intentos_descarga = intentos_descarga + 1,
                        ultimo_error = NULL,
                        actualizado_en = SYSDATETIMEOFFSET()
                    OUTPUT INSERTED.id
                    WHERE id = ?
                      AND
                      (
                          estado IN (N'PENDIENTE', N'ERROR_DESCARGA')
                          OR
                          (
                              estado = N'DESCARGANDO'
                              AND actualizado_en < DATEADD(
                                  MINUTE,
                                  ?,
                                  SYSDATETIMEOFFSET()
                              )
                          )
                      )
                """,
                paquete_id,
                -self._settings.download_lease_minutes,
            ).fetchone()
            if row is None:
                conexion.rollback()
                return None

            conexion.commit()
            reservado = self._obtener_paquete_por_id_conexion(
                conexion,
                int(row[0]),
            )
            if reservado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar el paquete reservado."
                )
            return reservado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible reservar el paquete para su descarga."
            ) from exc
        finally:
            conexion.close()

    def marcar_paquete_descargado(
        self,
        paquete_id: int,
        *,
        ruta_descarga: str,
        hash_sha256: str,
        tamanio_bytes: int,
        codigo_estatus: str,
        mensaje: str,
        respuesta_descarga_sat: Mapping[str, Any],
    ) -> PaqueteSatRegistrado:
        ruta = _normalizar_ruta_relativa(ruta_descarga)
        hash_xml = _normalizar_hash_sha256(hash_sha256)
        if tamanio_bytes <= 0:
            raise ErrorRepositorioRetenciones(
                "tamanio_bytes debe ser mayor que cero."
            )
        respuesta_json = _a_json(
            _respuesta_descarga_reducida(
                respuesta_descarga_sat,
                codigo_estatus=codigo_estatus,
                mensaje=mensaje,
            )
        )

        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            cursor.execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat_paquete
                    SET estado = N'DESCARGADO',
                        fecha_descarga = SYSDATETIMEOFFSET(),
                        ruta_descarga = ?,
                        hash_sha256 = ?,
                        tamanio_bytes = ?,
                        codigo_estatus_descarga = ?,
                        mensaje_descarga = ?,
                        respuesta_descarga_sat = ?,
                        ultimo_error = NULL,
                        actualizado_en = SYSDATETIMEOFFSET()
                    WHERE id = ?
                      AND estado = N'DESCARGANDO'
                """,
                ruta,
                hash_xml,
                tamanio_bytes,
                str(codigo_estatus or "").strip() or None,
                str(mensaje or "").strip() or None,
                respuesta_json,
                paquete_id,
            )
            if cursor.rowcount != 1:
                raise ErrorRepositorioRetenciones(
                    "El paquete no está reservado por este proceso."
                )

            cursor.execute(
                """
                    UPDATE s
                    SET paquetes_descargados = resumen.total,
                        fecha_descarga = SYSDATETIMEOFFSET(),
                        ruta_descarga = ?,
                        actualizado_en = SYSDATETIMEOFFSET()
                    FROM dbo.Fiscal_solicitudsat AS s
                    CROSS APPLY
                    (
                        SELECT COUNT_BIG(*) AS total
                        FROM dbo.Fiscal_solicitudsat_paquete AS p
                        WHERE p.solicitud_sat_id = s.id
                          AND p.estado IN
                          (
                              N'DESCARGADO', N'PROCESANDO',
                              N'PROCESADO', N'PROCESADO_CON_ERRORES'
                          )
                    ) AS resumen
                    WHERE s.id =
                    (
                        SELECT solicitud_sat_id
                        FROM dbo.Fiscal_solicitudsat_paquete
                        WHERE id = ?
                    )
                      AND s.servicio_sat = N'RETENCIONES'
                """,
                ruta,
                paquete_id,
            )
            conexion.commit()
            actualizado = self._obtener_paquete_por_id_conexion(
                conexion,
                paquete_id,
            )
            if actualizado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar el paquete descargado."
                )
            return actualizado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible registrar el paquete descargado."
            ) from exc
        finally:
            conexion.close()

    def marcar_error_descarga(
        self,
        paquete_id: int,
        *,
        codigo_estatus: str,
        mensaje: str,
        ruta_descarga: str | None = None,
        hash_sha256: str | None = None,
        tamanio_bytes: int | None = None,
        respuesta_descarga_sat: Mapping[str, Any] | None = None,
    ) -> PaqueteSatRegistrado:
        error = str(mensaje or "").strip() or "Error al descargar el paquete."
        ruta = (
            _normalizar_ruta_relativa(ruta_descarga)
            if ruta_descarga
            else None
        )
        hash_zip = (
            _normalizar_hash_sha256(hash_sha256)
            if hash_sha256
            else None
        )
        if tamanio_bytes is not None and tamanio_bytes < 0:
            raise ErrorRepositorioRetenciones(
                "tamanio_bytes no puede ser negativo."
            )
        respuesta_json = _a_json(
            _respuesta_descarga_reducida(
                respuesta_descarga_sat,
                codigo_estatus=codigo_estatus,
                mensaje=error,
            )
        )

        conexion = self._abrir_conexion()
        try:
            cursor = conexion.cursor()
            cursor.execute(
                """
                    UPDATE dbo.Fiscal_solicitudsat_paquete
                    SET estado = N'ERROR_DESCARGA',
                        ruta_descarga = ?,
                        hash_sha256 = ?,
                        tamanio_bytes = ?,
                        codigo_estatus_descarga = ?,
                        mensaje_descarga = ?,
                        respuesta_descarga_sat = ?,
                        ultimo_error = ?,
                        actualizado_en = SYSDATETIMEOFFSET()
                    WHERE id = ?
                      AND estado = N'DESCARGANDO'
                """,
                ruta,
                hash_zip,
                tamanio_bytes,
                str(codigo_estatus or "").strip() or None,
                error,
                respuesta_json,
                error,
                paquete_id,
            )
            if cursor.rowcount != 1:
                raise ErrorRepositorioRetenciones(
                    "El paquete no está reservado por este proceso."
                )
            conexion.commit()
            actualizado = self._obtener_paquete_por_id_conexion(
                conexion,
                paquete_id,
            )
            if actualizado is None:
                raise ErrorRepositorioRetenciones(
                    "No fue posible recuperar el paquete con error."
                )
            return actualizado
        except ErrorRepositorioRetenciones:
            conexion.rollback()
            raise
        except pyodbc.Error as exc:
            conexion.rollback()
            raise ErrorRepositorioRetenciones(
                "No fue posible registrar el error de descarga."
            ) from exc
        finally:
            conexion.close()

    def _obtener_paquete_por_id_conexion(
        self,
        conexion: pyodbc.Connection,
        paquete_id: int,
    ) -> PaqueteSatRegistrado | None:
        row = conexion.cursor().execute(
            self._select_paquete()
            + """
                WHERE p.id = ?
                  AND s.servicio_sat = N'RETENCIONES'
            """,
            paquete_id,
        ).fetchone()
        return _fila_a_paquete(row) if row is not None else None

    def _obtener_por_id_conexion(
        self,
        conexion: pyodbc.Connection,
        solicitud_id: int,
    ) -> SolicitudSatRegistrada | None:
        row = conexion.cursor().execute(
            self._select_solicitud()
            + """
                WHERE id = ?
                  AND servicio_sat = N'RETENCIONES'
            """,
            solicitud_id,
        ).fetchone()
        return _fila_a_solicitud(row) if row is not None else None

    def _abrir_conexion(self) -> pyodbc.Connection:
        try:
            return pyodbc.connect(
                self._settings.base.database.connection_string,
                autocommit=False,
            )
        except pyodbc.Error as exc:
            raise ErrorRepositorioRetenciones(
                "No fue posible conectar con SQL Server."
            ) from exc

    @staticmethod
    def _select_solicitud() -> str:
        return """
            SELECT
                id,
                razon_social_id,
                rfc,
                tipo_periodo,
                tipo_descarga,
                tipo_solicitud,
                estado_comprobante,
                fecha_inicial,
                fecha_final,
                id_solicitud_sat,
                codigo_estatus_solicitud,
                estado_solicitud_sat,
                codigo_estado_solicitud,
                numero_cfdis,
                ids_paquetes,
                sin_resultados,
                estado,
                intentos_consulta,
                servicio_sat,
                solicitud_padre_id,
                CAST(fecha_solicitud AS datetime2) AS fecha_solicitud,
                turno_diario
            FROM dbo.Fiscal_solicitudsat
        """

    @staticmethod
    def _select_paquete() -> str:
        return """
            SELECT
                p.id,
                p.solicitud_sat_id,
                s.razon_social_id,
                s.rfc,
                s.tipo_descarga,
                s.estado_comprobante,
                s.fecha_inicial,
                s.fecha_final,
                p.id_paquete_sat,
                p.estado,
                p.intentos_descarga,
                p.ruta_descarga,
                p.hash_sha256,
                p.tamanio_bytes,
                p.codigo_estatus_descarga,
                p.mensaje_descarga,
                p.ultimo_error
            FROM dbo.Fiscal_solicitudsat_paquete AS p
            INNER JOIN dbo.Fiscal_solicitudsat AS s
                ON s.id = p.solicitud_sat_id
        """


def _fila_a_solicitud(row: Any) -> SolicitudSatRegistrada:
    return SolicitudSatRegistrada(
        id=int(row.id),
        razon_social_id=int(row.razon_social_id),
        rfc=str(row.rfc).strip().upper(),
        tipo_periodo=str(row.tipo_periodo).strip().upper(),
        tipo_descarga=str(row.tipo_descarga).strip().upper(),
        tipo_solicitud=str(row.tipo_solicitud).strip().upper(),
        estado_comprobante=str(row.estado_comprobante).strip().upper(),
        fecha_inicial=_fecha_hora_sql(row.fecha_inicial, es_final=False),
        fecha_final=_fecha_hora_sql(row.fecha_final, es_final=True),
        id_solicitud_sat=_texto_opcional(row.id_solicitud_sat),
        codigo_estatus_solicitud=_texto_opcional(
            row.codigo_estatus_solicitud
        ),
        estado_solicitud_sat=(
            int(row.estado_solicitud_sat)
            if row.estado_solicitud_sat is not None
            else None
        ),
        codigo_estado_solicitud=_texto_opcional(
            row.codigo_estado_solicitud
        ),
        numero_cfdis=(
            int(row.numero_cfdis)
            if row.numero_cfdis is not None
            else None
        ),
        ids_paquetes=_leer_ids_paquetes(row.ids_paquetes),
        sin_resultados=bool(row.sin_resultados),
        estado=str(row.estado).strip().upper(),
        intentos_consulta=int(row.intentos_consulta),
        servicio_sat=str(row.servicio_sat).strip().upper(),
        solicitud_padre_id=(
            int(row.solicitud_padre_id)
            if row.solicitud_padre_id is not None
            else None
        ),
        fecha_solicitud=row.fecha_solicitud,
        turno_diario=(
            int(row.turno_diario)
            if row.turno_diario is not None
            else None
        ),
    )


def _fila_a_paquete(row: Any) -> PaqueteSatRegistrado:
    return PaqueteSatRegistrado(
        id=int(row.id),
        solicitud_sat_id=int(row.solicitud_sat_id),
        razon_social_id=int(row.razon_social_id),
        rfc=str(row.rfc).strip().upper(),
        tipo_descarga=str(row.tipo_descarga).strip().upper(),
        estado_comprobante=str(row.estado_comprobante).strip().upper(),
        fecha_inicial=_fecha_hora_sql(row.fecha_inicial, es_final=False),
        fecha_final=_fecha_hora_sql(row.fecha_final, es_final=True),
        id_paquete_sat=str(row.id_paquete_sat).strip(),
        estado=str(row.estado).strip().upper(),
        intentos_descarga=int(row.intentos_descarga),
        ruta_descarga=_texto_opcional(row.ruta_descarga),
        hash_sha256=_texto_opcional(row.hash_sha256),
        tamanio_bytes=(
            int(row.tamanio_bytes)
            if row.tamanio_bytes is not None
            else None
        ),
        codigo_estatus_descarga=_texto_opcional(
            row.codigo_estatus_descarga
        ),
        mensaje_descarga=_texto_opcional(row.mensaje_descarga),
        ultimo_error=_texto_opcional(row.ultimo_error),
    )


def _validar_solicitud_coincidente(
    solicitud: SolicitudSatRegistrada,
    criterio: CriterioSolicitudRetenciones,
) -> None:
    esperado = (
        criterio.razon_social_id,
        criterio.rfc,
        criterio.tipo_periodo.value,
        criterio.tipo_descarga.value,
        criterio.tipo_solicitud.value,
        criterio.estado_comprobante.value,
        criterio.fecha_inicial,
        criterio.fecha_final,
        criterio.servicio_sat.value,
        criterio.turno_diario,
    )
    recibido = (
        solicitud.razon_social_id,
        solicitud.rfc,
        solicitud.tipo_periodo,
        solicitud.tipo_descarga,
        solicitud.tipo_solicitud,
        solicitud.estado_comprobante,
        solicitud.fecha_inicial,
        solicitud.fecha_final,
        solicitud.servicio_sat,
        solicitud.turno_diario,
    )
    if recibido != esperado:
        raise ErrorRepositorioRetenciones(
            "El id_solicitud_sat ya existe con criterios diferentes."
        )


def _fecha_hora_sql(valor: Any, *, es_final: bool) -> datetime:
    """Conserva DATETIME2 y tolera instalaciones antiguas con columnas DATE."""

    if isinstance(valor, datetime):
        return valor
    if isinstance(valor, date):
        hora = time(23, 59, 59) if es_final else time.min
        return datetime.combine(valor, hora)
    raise ErrorRepositorioRetenciones(
        "SQL devolvió una fecha/hora con tipo inesperado: "
        f"{type(valor).__name__}."
    )


def _clasificar_verificacion(
    respuesta: RespuestaVerificacionSat,
) -> tuple[EstadoSolicitudLocal, bool, str | None]:
    if respuesta.sin_resultados:
        return (
            EstadoSolicitudLocal.COMPLETADA_SIN_RESULTADOS,
            True,
            None,
        )
    if respuesta.estado_solicitud is EstadoSolicitudSat.TERMINADA:
        return EstadoSolicitudLocal.COMPLETADA, True, None
    if respuesta.en_proceso:
        return EstadoSolicitudLocal.CONSULTANDO, False, None
    mensaje = respuesta.mensaje or (
        "La solicitud terminó con estado SAT "
        f"{int(respuesta.estado_solicitud)} "
        f"({respuesta.estado_solicitud.descripcion})."
    )
    return EstadoSolicitudLocal.ERROR_CONSULTA, True, mensaje


def _leer_ids_paquetes(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    try:
        datos = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ErrorRepositorioRetenciones(
            "ids_paquetes contiene JSON inválido en SQL."
        ) from exc
    if not isinstance(datos, list):
        raise ErrorRepositorioRetenciones(
            "ids_paquetes debe contener una lista JSON."
        )

    resultado: list[str] = []
    vistos: set[str] = set()
    for item in datos:
        paquete = str(item or "").strip()
        if paquete and paquete not in vistos:
            resultado.append(paquete)
            vistos.add(paquete)
    return tuple(resultado)


def _a_json(value: Any) -> str:
    return json.dumps(
        _compatible_json(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _respuesta_descarga_reducida(
    respuesta: Mapping[str, Any] | None,
    *,
    codigo_estatus: str,
    mensaje: str,
) -> dict[str, str | None]:
    """Conserva solo los datos de auditoría; nunca persiste paquete_b64."""

    datos = respuesta or {}
    return {
        "cod_estatus": (
            _texto_opcional(datos.get("cod_estatus"))
            or _texto_opcional(codigo_estatus)
        ),
        "mensaje": (
            _texto_opcional(datos.get("mensaje"))
            or _texto_opcional(mensaje)
        ),
    }


def _compatible_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _compatible_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_compatible_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ErrorRepositorioRetenciones(
        f"No se puede convertir {type(value).__name__} a JSON de auditoría."
    )


def _texto_opcional(value: Any) -> str | None:
    resultado = str(value or "").strip()
    return resultado or None


def _secreto_opcional(value: Any) -> str | None:
    if value is None:
        return None
    resultado = str(value)
    return resultado if resultado else None


def _entero_opcional(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalizar_ruta_relativa(value: str) -> str:
    texto = str(value or "").strip().replace("\\", "/")
    ruta = PurePosixPath(texto)
    if (
        not texto
        or ruta.is_absolute()
        or any(parte in {"", ".", ".."} for parte in ruta.parts)
    ):
        raise ErrorRepositorioRetenciones(
            "ruta_descarga debe ser una ruta relativa segura."
        )
    normalizada = ruta.as_posix()
    if len(normalizada) > 1000:
        raise ErrorRepositorioRetenciones(
            "ruta_descarga excede los 1000 caracteres permitidos."
        )
    return normalizada


def _normalizar_hash_sha256(value: str) -> str:
    resultado = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", resultado) is None:
        raise ErrorRepositorioRetenciones(
            "hash_sha256 debe contener 64 caracteres hexadecimales."
        )
    return resultado
