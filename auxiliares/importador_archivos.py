"""Procesamiento de paquetes SAT y ZIP colocados manualmente.

Paso 1: registra el lote y extrae sus XML de forma segura.
Paso 2: valida el contexto, interpreta y persiste cada comprobante.
Paso 3: archiva el ZIP o conserva los errores en cuarentena.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import pyodbc

from auxiliares.configuracion import Settings
from auxiliares.lector_xml import (
    ErrorArchivoXML,
    ErrorArchivoZip,
    LectorXML,
    RetencionXML,
)
from auxiliares.persistencia_retenciones import (
    ConflictoUUID,
    ErrorPersistencia,
    GuardadorRetenciones,
    SinRfcCorporativo,
    copiar_archivo_a_cuarentena,
)
from auxiliares.repositorios_sql import (
    ErrorRepositorioRetenciones,
    PaqueteSatRegistrado,
    RepositorioSolicitudesRetenciones,
)

EstadoImportacion = Literal[
    "COMPLETADO",
    "COMPLETADO_CON_ERRORES",
    "FALLIDO",
]


@dataclass(frozen=True, slots=True)
class ResultadoImportacion:
    ruta_zip: Path
    importacion_id: int | None
    lote_uuid: str | None
    estado: EstadoImportacion
    total_archivos: int
    archivos_importados: int
    archivos_duplicados: int
    archivos_con_conflicto: int
    archivos_con_error: int
    solicitud_sat_paquete_id: int | None = None
    mensaje: str | None = None


@dataclass(frozen=True, slots=True)
class PaqueteArchivadoRecuperable:
    """Contexto verificable de un ZIP SAT ya descargado y archivado."""

    ruta_zip: Path
    rfc: str
    periodo: str
    tipo_descarga: Literal["EMITIDAS", "RECIBIDAS"]
    estado_comprobante: Literal["VIGENTE", "CANCELADO"]
    id_paquete_sat: str
    total_xml: int


class ErrorImportacion(RuntimeError):
    """Indica que un lote completo no pudo terminarse."""


class ErrorContextoPaqueteSat(ErrorImportacion):
    """El XML no corresponde al RFC o dirección solicitados al SAT."""


class ImportadorRetenciones:
    """Procesa los ZIP pendientes y conserva auditoría de cada archivo."""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._lector = LectorXML(settings)
        self._guardador = GuardadorRetenciones(settings)

    def procesar_pendientes(self) -> tuple[ResultadoImportacion, ...]:
        """Procesa todos los ZIP de entrada sin detenerse por un lote fallido."""
        rutas_zip = self._lector.descubrir_zips()
        self._logger.info(
            "[FASE 1] Descubrimiento terminado: %d ZIP pendiente(s)",
            len(rutas_zip),
        )
        resultados: list[ResultadoImportacion] = []
        for numero, ruta_zip in enumerate(rutas_zip, start=1):
            self._logger.info(
                "[LOTE %d/%d] Iniciando %s",
                numero,
                len(rutas_zip),
                ruta_zip.name,
            )
            resultados.append(self.procesar_zip(ruta_zip))
        return tuple(resultados)

    def descubrir_paquetes_archivados(
        self,
        *,
        rfc: str | None = None,
    ) -> tuple[PaqueteArchivadoRecuperable, ...]:
        """Localiza ZIP SAT procesados sin modificar archivos ni SQL."""

        raiz = self._settings.paths.sat_processed_dir.resolve()
        rfc_buscado = (
            _normalizar_rfc_contexto(rfc)
            if rfc
            else None
        )
        paquetes: list[PaqueteArchivadoRecuperable] = []

        for ruta_zip in sorted(raiz.rglob("*.zip")):
            paquete = _crear_contexto_paquete_archivado(
                ruta_zip,
                raiz,
            )
            if rfc_buscado and paquete.rfc != rfc_buscado:
                continue
            paquetes.append(paquete)

        return tuple(paquetes)

    def procesar_paquete_archivado(
        self,
        paquete: PaqueteArchivadoRecuperable,
    ) -> ResultadoImportacion:
        """Recupera un paquete local sin moverlo ni contactar al SAT."""

        return self.procesar_zip(
            paquete.ruta_zip,
            contexto_recuperacion=paquete,
        )

    def procesar_paquetes_sat(
        self,
        repositorio: RepositorioSolicitudesRetenciones,
        *,
        razon_social_id: int | None = None,
        limite: int | None = None,
    ) -> tuple[ResultadoImportacion, ...]:
        """Procesa ZIP descargados por el flujo SAT sin mezclar los manuales."""

        if limite is not None and limite <= 0:
            raise ValueError("limite debe ser mayor que cero.")

        candidatos = repositorio.listar_paquetes_por_procesar(
            razon_social_id
        )
        if limite is not None:
            candidatos = candidatos[:limite]

        self._logger.info(
            "[SAT/FASE 1] Descubrimiento terminado: %d paquete(s) por "
            "procesar",
            len(candidatos),
        )
        resultados: list[ResultadoImportacion] = []
        for numero, candidato in enumerate(candidatos, start=1):
            reservado = repositorio.reservar_paquete_para_procesar(
                candidato.id
            )
            if reservado is None:
                self._logger.info(
                    "[SAT/LOTE %d/%d] El paquete %s fue reservado por "
                    "otro proceso",
                    numero,
                    len(candidatos),
                    candidato.id_paquete_sat,
                )
                continue

            self._logger.info(
                "[SAT/LOTE %d/%d] Iniciando %s (%s/%s)",
                numero,
                len(candidatos),
                reservado.id_paquete_sat,
                reservado.tipo_descarga,
                reservado.estado_comprobante,
            )
            try:
                ruta_zip = self._resolver_zip_sat(reservado)
            except Exception as exc:
                mensaje = str(exc)
                self._logger.error(
                    "No se puede procesar el paquete %s: %s",
                    reservado.id_paquete_sat,
                    mensaje,
                )
                repositorio.devolver_paquete_a_descarga(
                    reservado.id,
                    mensaje,
                )
                resultados.append(
                    ResultadoImportacion(
                        ruta_zip=Path(reservado.ruta_descarga or ""),
                        importacion_id=None,
                        lote_uuid=None,
                        estado="FALLIDO",
                        total_archivos=0,
                        archivos_importados=0,
                        archivos_duplicados=0,
                        archivos_con_conflicto=0,
                        archivos_con_error=0,
                        solicitud_sat_paquete_id=reservado.id,
                        mensaje=mensaje,
                    )
                )
                continue

            resultado = self.procesar_zip(
                ruta_zip,
                paquete_sat=reservado,
                repositorio_paquetes=repositorio,
            )
            estado_paquete = (
                "PROCESADO"
                if resultado.estado == "COMPLETADO"
                else "PROCESADO_CON_ERRORES"
            )
            mensaje_error = (
                None
                if estado_paquete == "PROCESADO"
                else resultado.mensaje
                or (
                    "El paquete contiene XML con conflicto o error. "
                    f"Conflictos={resultado.archivos_con_conflicto}, "
                    f"errores={resultado.archivos_con_error}."
                )
            )

            try:
                destino = self._archivar_zip_sat(
                    ruta_zip,
                    reservado,
                    _calcular_hash(ruta_zip),
                    con_errores=(estado_paquete != "PROCESADO"),
                )
                repositorio.finalizar_procesamiento_paquete(
                    reservado.id,
                    estado=estado_paquete,
                    ruta_descarga=self._ruta_relativa_raiz(destino),
                    mensaje_error=mensaje_error,
                )
            except (OSError, ErrorImportacion, ErrorRepositorioRetenciones):
                self._logger.exception(
                    "El paquete %s fue importado, pero no se pudo cerrar "
                    "su ciclo de archivo/SQL. Se recuperará después de "
                    "vencer la reserva.",
                    reservado.id_paquete_sat,
                )
                raise

            self._logger.info(
                "[SAT/FASE 6] Paquete registrado como %s en %s",
                estado_paquete,
                destino,
            )
            resultados.append(resultado)

        return tuple(resultados)

    def procesar_zip(
        self,
        ruta_zip: Path,
        *,
        paquete_sat: PaqueteSatRegistrado | None = None,
        repositorio_paquetes: (
            RepositorioSolicitudesRetenciones | None
        ) = None,
        contexto_recuperacion: PaqueteArchivadoRecuperable | None = None,
    ) -> ResultadoImportacion:
        if (paquete_sat is None) != (repositorio_paquetes is None):
            raise ValueError(
                "paquete_sat y repositorio_paquetes deben proporcionarse "
                "juntos."
            )
        if paquete_sat is not None and contexto_recuperacion is not None:
            raise ValueError(
                "paquete_sat y contexto_recuperacion son excluyentes."
            )
        contexto_sat = paquete_sat or contexto_recuperacion
        ruta_zip = Path(ruta_zip)
        importacion_id: int | None = None
        lote_uuid: str | None = None
        total = importados = duplicados = conflictos = errores = 0
        directorio_extraido = self._settings.paths.work_dir / ruta_zip.stem
        conexion: pyodbc.Connection | None = None
        extraccion_creada = False

        try:
            hash_zip = _calcular_hash(ruta_zip)
            conexion = self._abrir_conexion_auditoria()
            importacion_id, lote_uuid = self._crear_importacion(
                conexion,
                ruta_zip,
                hash_zip,
                solicitud_sat_paquete_id=(
                    paquete_sat.id if paquete_sat is not None else None
                ),
            )
            self._logger.info(
                "[FASE 2] Auditoría creada: importación=%d, lote=%s",
                importacion_id,
                lote_uuid,
            )

            rutas_xml = self._lector.extraer_zip(ruta_zip)
            extraccion_creada = True
            total = len(rutas_xml)
            self._actualizar_total(conexion, importacion_id, total)
            self._logger.info(
                "[FASE 3] %s extraído de forma segura: %d XML",
                ruta_zip.name,
                total,
            )

            for numero, ruta_xml in enumerate(rutas_xml, start=1):
                ruta_interna = _ruta_interna(ruta_xml, directorio_extraido)
                self._logger.info(
                    "[FASE 4] XML %d/%d: %s",
                    numero,
                    total,
                    ruta_interna,
                )
                resultado_archivo = self._procesar_xml(
                    conexion,
                    importacion_id,
                    ruta_xml,
                    ruta_interna,
                    contexto_sat=contexto_sat,
                )
                if resultado_archivo == "IMPORTADO":
                    importados += 1
                elif resultado_archivo == "DUPLICADO":
                    duplicados += 1
                elif resultado_archivo == "CONFLICTO":
                    conflictos += 1
                else:
                    errores += 1

                if repositorio_paquetes is not None and paquete_sat is not None:
                    repositorio_paquetes.renovar_procesamiento_paquete(
                        paquete_sat.id
                    )

            estado: EstadoImportacion = (
                "COMPLETADO_CON_ERRORES"
                if conflictos or errores
                else "COMPLETADO"
            )
            self._finalizar_importacion(
                conexion,
                importacion_id,
                estado,
                total,
                importados,
                duplicados,
                conflictos,
                errores,
            )
            self._logger.info(
                "[FASE 5] Lote registrado como %s: "
                "importados=%d, duplicados=%d, conflictos=%d, errores=%d",
                estado,
                importados,
                duplicados,
                conflictos,
                errores,
            )

            if paquete_sat is None and contexto_recuperacion is None:
                destino_zip = self._archivar_zip(ruta_zip, hash_zip)
                self._logger.info(
                    "[FASE 6] ZIP archivado en %s",
                    destino_zip,
                )
            return ResultadoImportacion(
                ruta_zip=ruta_zip,
                importacion_id=importacion_id,
                lote_uuid=lote_uuid,
                estado=estado,
                total_archivos=total,
                archivos_importados=importados,
                archivos_duplicados=duplicados,
                archivos_con_conflicto=conflictos,
                archivos_con_error=errores,
                solicitud_sat_paquete_id=(
                    paquete_sat.id if paquete_sat is not None else None
                ),
            )
        except Exception as exc:
            mensaje = str(exc)
            if importacion_id is not None and conexion is not None:
                self._marcar_fallida(
                    conexion,
                    importacion_id,
                    total,
                    importados,
                    duplicados,
                    conflictos,
                    errores,
                    mensaje,
                )
            self._logger.exception(
                "El lote %s terminó como FALLIDO: %s",
                ruta_zip.name,
                mensaje,
            )
            if (
                paquete_sat is None
                and contexto_recuperacion is None
                and isinstance(exc, ErrorArchivoZip)
                and not directorio_extraido.exists()
            ):
                self._poner_zip_en_cuarentena(ruta_zip)
            return ResultadoImportacion(
                ruta_zip=ruta_zip,
                importacion_id=importacion_id,
                lote_uuid=lote_uuid,
                estado="FALLIDO",
                total_archivos=total,
                archivos_importados=importados,
                archivos_duplicados=duplicados,
                archivos_con_conflicto=conflictos,
                archivos_con_error=errores,
                solicitud_sat_paquete_id=(
                    paquete_sat.id if paquete_sat is not None else None
                ),
                mensaje=mensaje,
            )
        finally:
            if conexion is not None:
                conexion.close()
            if extraccion_creada:
                _eliminar_directorio_extraido(
                    directorio_extraido,
                    self._settings.paths.work_dir,
                    self._logger,
                )

    def _procesar_xml(
        self,
        conexion: pyodbc.Connection,
        importacion_id: int,
        ruta_xml: Path,
        ruta_interna: str,
        *,
        contexto_sat: (
            PaqueteSatRegistrado
            | PaqueteArchivadoRecuperable
            | None
        ) = None,
    ) -> Literal["IMPORTADO", "DUPLICADO", "CONFLICTO", "ERROR"]:
        try:
            retencion = self._lector.leer_xml(ruta_xml)
        except ErrorArchivoXML as exc:
            hash_xml = _calcular_hash_si_existe(ruta_xml)
            cuarentena = copiar_archivo_a_cuarentena(
                ruta_xml,
                self._settings.paths.quarantine_dir,
                "xml_invalido",
            )
            mensaje = f"{exc}. Cuarentena: {cuarentena}"
            self._registrar_detalle(
                conexion, importacion_id, None, ruta_xml.name, ruta_interna,
                hash_xml, None, "ERROR", mensaje,
            )
            self._logger.warning("XML inválido: %s", mensaje)
            return "ERROR"

        if contexto_sat is not None:
            try:
                _validar_contexto_xml_sat(retencion, contexto_sat)
            except ErrorContextoPaqueteSat as exc:
                cuarentena = copiar_archivo_a_cuarentena(
                    ruta_xml,
                    self._settings.paths.quarantine_dir,
                    "contexto_sat_invalido",
                )
                mensaje = f"{exc}. Copia de revisión: {cuarentena}"
                self._registrar_detalle(
                    conexion,
                    importacion_id,
                    None,
                    ruta_xml.name,
                    ruta_interna,
                    retencion.hash_sha256,
                    retencion.uuid,
                    "ERROR",
                    mensaje,
                )
                self._logger.warning("%s", mensaje)
                return "ERROR"

        try:
            resultado = self._guardador.guardar(
                retencion,
                estado_comprobante=(
                    contexto_sat.estado_comprobante
                    if contexto_sat is not None
                    else "VIGENTE"
                ),
            )
        except ConflictoUUID as exc:
            self._registrar_detalle(
                conexion, importacion_id, None, ruta_xml.name, ruta_interna,
                retencion.hash_sha256, retencion.uuid, "CONFLICTO", str(exc),
            )
            self._logger.warning("%s", exc)
            return "CONFLICTO"
        except SinRfcCorporativo as exc:
            self._registrar_detalle(
                conexion, importacion_id, None, ruta_xml.name, ruta_interna,
                retencion.hash_sha256, retencion.uuid, "ERROR", str(exc),
            )
            self._logger.warning("%s", exc)
            return "ERROR"
        except ErrorPersistencia as exc:
            cuarentena = copiar_archivo_a_cuarentena(
                ruta_xml,
                self._settings.paths.quarantine_dir,
                "error_persistencia",
            )
            mensaje = f"{exc}. Copia de revisión: {cuarentena}"
            self._registrar_detalle(
                conexion, importacion_id, None, ruta_xml.name, ruta_interna,
                retencion.hash_sha256, retencion.uuid, "ERROR", mensaje,
            )
            self._logger.error("%s", mensaje)
            return "ERROR"
        except Exception as exc:
            cuarentena = copiar_archivo_a_cuarentena(
                ruta_xml,
                self._settings.paths.quarantine_dir,
                "error_inesperado",
            )
            mensaje = f"Error inesperado: {exc}. Copia de revisión: {cuarentena}"
            self._registrar_detalle(
                conexion, importacion_id, None, ruta_xml.name, ruta_interna,
                retencion.hash_sha256, retencion.uuid, "ERROR", mensaje,
            )
            self._logger.exception("%s", mensaje)
            return "ERROR"

        self._registrar_detalle(
            conexion,
            importacion_id,
            resultado.retencion_id,
            ruta_xml.name,
            ruta_interna,
            retencion.hash_sha256,
            retencion.uuid,
            resultado.estado,
            None,
        )
        return resultado.estado

    def _abrir_conexion_auditoria(self) -> pyodbc.Connection:
        try:
            return pyodbc.connect(
                self._settings.database.connection_string,
                autocommit=False,
            )
        except pyodbc.Error as exc:
            raise ErrorImportacion(
                f"No se pudo conectar a SQL Server para auditar el lote: {exc}"
            ) from exc

    @staticmethod
    def _crear_importacion(
        conexion: pyodbc.Connection,
        ruta_zip: Path,
        hash_zip: str,
        solicitud_sat_paquete_id: int | None = None,
    ) -> tuple[int, str]:
        consulta = """
            INSERT INTO dbo.Fiscal_Retenciones_Importaciones
                (NombreZip, HashZip, RutaZipOrigen, Estado,
                 SolicitudSatPaqueteId)
            OUTPUT INSERTED.id, INSERTED.LoteUUID
            VALUES (?, ?, ?, 'PROCESANDO', ?)
        """
        row = conexion.cursor().execute(
            consulta,
            ruta_zip.name,
            hash_zip,
            str(ruta_zip.resolve()),
            solicitud_sat_paquete_id,
        ).fetchone()
        if row is None:
            conexion.rollback()
            raise ErrorImportacion("SQL Server no devolvió el id del lote")
        conexion.commit()
        return int(row[0]), str(row[1])

    @staticmethod
    def _actualizar_total(
        conexion: pyodbc.Connection,
        importacion_id: int,
        total: int,
    ) -> None:
        conexion.cursor().execute(
            """
            UPDATE dbo.Fiscal_Retenciones_Importaciones
            SET TotalArchivos = ?
            WHERE id = ?
            """,
            total,
            importacion_id,
        )
        conexion.commit()

    @staticmethod
    def _registrar_detalle(
        conexion: pyodbc.Connection,
        importacion_id: int,
        retencion_id: int | None,
        nombre_archivo: str,
        ruta_interna_zip: str,
        hash_xml: str | None,
        uuid: str | None,
        estado: str,
        mensaje: str | None,
    ) -> None:
        conexion.cursor().execute(
            """
            INSERT INTO dbo.Fiscal_Retenciones_ImportacionDetalle
                (ImportacionId, RetencionId, NombreArchivo, RutaInternaZip,
                 HashXml, UUID, Estado, Mensaje)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            importacion_id,
            retencion_id,
            nombre_archivo,
            ruta_interna_zip,
            hash_xml,
            uuid,
            estado,
            mensaje,
        )
        conexion.commit()

    @staticmethod
    def _finalizar_importacion(
        conexion: pyodbc.Connection,
        importacion_id: int,
        estado: EstadoImportacion,
        total: int,
        importados: int,
        duplicados: int,
        conflictos: int,
        errores: int,
    ) -> None:
        conexion.cursor().execute(
            """
            UPDATE dbo.Fiscal_Retenciones_Importaciones
            SET Estado = ?, TotalArchivos = ?, ArchivosImportados = ?,
                ArchivosDuplicados = ?, ArchivosConConflicto = ?,
                ArchivosConError = ?, FechaFin = SYSUTCDATETIME(),
                MensajeError = NULL
            WHERE id = ?
            """,
            estado,
            total,
            importados,
            duplicados,
            conflictos,
            errores,
            importacion_id,
        )
        conexion.commit()

    @staticmethod
    def _marcar_fallida(
        conexion: pyodbc.Connection,
        importacion_id: int,
        total: int,
        importados: int,
        duplicados: int,
        conflictos: int,
        errores: int,
        mensaje: str,
    ) -> None:
        try:
            conexion.rollback()
            conexion.cursor().execute(
                """
                UPDATE dbo.Fiscal_Retenciones_Importaciones
                SET Estado = 'FALLIDO', TotalArchivos = ?,
                    ArchivosImportados = ?, ArchivosDuplicados = ?,
                    ArchivosConConflicto = ?, ArchivosConError = ?,
                    FechaFin = SYSUTCDATETIME(), MensajeError = ?
                WHERE id = ?
                """,
                total,
                importados,
                duplicados,
                conflictos,
                errores,
                mensaje,
                importacion_id,
            )
            conexion.commit()
        except pyodbc.Error:
            conexion.rollback()

    def _archivar_zip(self, ruta_zip: Path, hash_zip: str) -> Path:
        ahora = datetime.now(timezone.utc)
        directorio = self._settings.paths.processed_dir / str(ahora.year) / f"{ahora.month:02d}"
        destino = directorio / ruta_zip.name
        if destino.exists() and _calcular_hash(destino).casefold() != hash_zip.casefold():
            destino = directorio / f"{ruta_zip.stem}-{hash_zip[:12]}{ruta_zip.suffix.lower()}"
        return _mover_archivo_verificado(ruta_zip, destino, hash_zip)

    def _resolver_zip_sat(self, paquete: PaqueteSatRegistrado) -> Path:
        if not paquete.ruta_descarga:
            raise ErrorImportacion(
                f"El paquete {paquete.id_paquete_sat} no tiene ruta_descarga."
            )
        if not paquete.hash_sha256 or len(paquete.hash_sha256) != 64:
            raise ErrorImportacion(
                f"El paquete {paquete.id_paquete_sat} no tiene un SHA-256 "
                "válido en SQL."
            )
        if paquete.tamanio_bytes is None or paquete.tamanio_bytes <= 0:
            raise ErrorImportacion(
                f"El paquete {paquete.id_paquete_sat} no tiene un tamaño "
                "válido en SQL."
            )

        relativa = PurePosixPath(paquete.ruta_descarga.replace("\\", "/"))
        if relativa.is_absolute() or any(
            parte in {"", ".", ".."} for parte in relativa.parts
        ):
            raise ErrorImportacion(
                f"Ruta de paquete SAT insegura: {paquete.ruta_descarga}"
            )

        raiz = self._settings.paths.root_dir.resolve()
        registrada = raiz.joinpath(*relativa.parts).resolve()
        try:
            registrada.relative_to(raiz)
        except ValueError as exc:
            raise ErrorImportacion(
                f"La ruta del paquete sale de RETENCIONES_ROOT_DIR: "
                f"{registrada}"
            ) from exc

        bases = (
            self._settings.paths.sat_pending_dir.resolve(),
            self._settings.paths.sat_processed_dir.resolve(),
            self._settings.paths.sat_error_dir.resolve(),
        )
        sufijo: Path | None = None
        for base in bases:
            try:
                sufijo = registrada.relative_to(base)
                break
            except ValueError:
                continue
        if sufijo is None:
            raise ErrorImportacion(
                "La ruta registrada no pertenece a las carpetas SAT de "
                f"paquetes: {registrada}"
            )

        candidatos = [registrada]
        candidatos.extend(
            base / sufijo
            for base in bases
            if (base / sufijo).resolve() != registrada
        )
        problemas: list[str] = []
        for candidato in candidatos:
            if not candidato.is_file():
                continue
            tamanio = candidato.stat().st_size
            if tamanio != paquete.tamanio_bytes:
                problemas.append(
                    f"{candidato}: tamaño {tamanio}, esperado "
                    f"{paquete.tamanio_bytes}"
                )
                continue
            hash_actual = _calcular_hash(candidato)
            if hash_actual.casefold() != paquete.hash_sha256.casefold():
                problemas.append(
                    f"{candidato}: SHA-256 {hash_actual}, esperado "
                    f"{paquete.hash_sha256}"
                )
                continue
            return candidato

        detalle = "; ".join(problemas) or "no se encontró el archivo"
        raise ErrorImportacion(
            f"No se encontró una copia íntegra del paquete "
            f"{paquete.id_paquete_sat}: {detalle}."
        )

    def _archivar_zip_sat(
        self,
        ruta_zip: Path,
        paquete: PaqueteSatRegistrado,
        hash_zip: str,
        *,
        con_errores: bool,
    ) -> Path:
        origen = Path(ruta_zip).resolve()
        bases = (
            self._settings.paths.sat_pending_dir.resolve(),
            self._settings.paths.sat_processed_dir.resolve(),
            self._settings.paths.sat_error_dir.resolve(),
        )
        sufijo: Path | None = None
        for base in bases:
            try:
                sufijo = origen.relative_to(base)
                break
            except ValueError:
                continue
        if sufijo is None:
            raise ErrorImportacion(
                f"El ZIP SAT está fuera de sus carpetas controladas: {origen}"
            )

        base_destino = (
            self._settings.paths.sat_error_dir
            if con_errores
            else self._settings.paths.sat_processed_dir
        ).resolve()
        destino = (base_destino / sufijo).resolve()
        try:
            destino.relative_to(base_destino)
        except ValueError as exc:
            raise ErrorImportacion(
                f"El destino del paquete SAT es inseguro: {destino}"
            ) from exc

        if destino == origen:
            return origen
        return _mover_archivo_verificado(origen, destino, hash_zip)

    def _ruta_relativa_raiz(self, ruta: Path) -> str:
        raiz = self._settings.paths.root_dir.resolve()
        resuelta = Path(ruta).resolve()
        try:
            return resuelta.relative_to(raiz).as_posix()
        except ValueError as exc:
            raise ErrorImportacion(
                f"La ruta {resuelta} sale de RETENCIONES_ROOT_DIR."
            ) from exc

    def _poner_zip_en_cuarentena(self, ruta_zip: Path) -> None:
        if not ruta_zip.exists():
            return
        try:
            destino = copiar_archivo_a_cuarentena(
                ruta_zip,
                self._settings.paths.quarantine_dir,
                "zip_invalido",
            )
            if _calcular_hash(destino) == _calcular_hash(ruta_zip):
                ruta_zip.unlink()
            self._logger.warning("ZIP enviado a cuarentena: %s", destino)
        except Exception as exc:
            self._logger.error("No se pudo enviar el ZIP a cuarentena: %s", exc)


def _crear_contexto_paquete_archivado(
    ruta_zip: Path,
    raiz_procesados: Path,
) -> PaqueteArchivadoRecuperable:
    ruta = Path(ruta_zip).resolve()
    raiz = Path(raiz_procesados).resolve()
    try:
        relativa = ruta.relative_to(raiz)
    except ValueError as exc:
        raise ErrorImportacion(
            f"El ZIP está fuera de la carpeta SAT procesados: {ruta}"
        ) from exc

    if len(relativa.parts) != 5:
        raise ErrorImportacion(
            "La ruta del paquete archivado no cumple la estructura "
            "RFC/PERIODO/TIPO/ESTADO/PAQUETE.zip: "
            f"{relativa.as_posix()}"
        )

    rfc_original, periodo, tipo, estado, nombre_zip = relativa.parts
    rfc = _normalizar_rfc_contexto(rfc_original)
    if not 12 <= len(rfc) <= 13:
        raise ErrorImportacion(
            f"RFC inválido en la ruta del paquete: {rfc_original}"
        )

    try:
        fecha_inicial_texto, fecha_final_texto = periodo.split("_", 1)
        fecha_inicial = date.fromisoformat(fecha_inicial_texto)
        fecha_final = date.fromisoformat(fecha_final_texto)
    except ValueError as exc:
        raise ErrorImportacion(
            f"Periodo inválido en la ruta del paquete: {periodo}"
        ) from exc
    if fecha_final < fecha_inicial:
        raise ErrorImportacion(
            f"El periodo del paquete está invertido: {periodo}"
        )

    tipo_normalizado = tipo.strip().upper()
    if tipo_normalizado not in {"EMITIDAS", "RECIBIDAS"}:
        raise ErrorImportacion(
            f"Tipo de descarga inválido en la ruta: {tipo}"
        )

    estado_normalizado = estado.strip().upper()
    if estado_normalizado not in {"VIGENTE", "CANCELADO"}:
        raise ErrorImportacion(
            f"Estado de comprobante inválido en la ruta: {estado}"
        )
    if (
        tipo_normalizado == "RECIBIDAS"
        and estado_normalizado == "CANCELADO"
    ):
        raise ErrorImportacion(
            "El SAT no proporciona paquetes RECIBIDAS/CANCELADO."
        )

    if Path(nombre_zip).suffix.casefold() != ".zip":
        raise ErrorImportacion(
            f"El paquete archivado no tiene extensión ZIP: {nombre_zip}"
        )
    id_paquete_sat = Path(nombre_zip).stem.strip()
    if not id_paquete_sat:
        raise ErrorImportacion(
            f"El paquete no tiene identificador SAT: {nombre_zip}"
        )

    try:
        with ZipFile(ruta) as archivo_zip:
            nombres_xml = [
                info.filename
                for info in archivo_zip.infolist()
                if not info.is_dir()
                and PurePosixPath(info.filename).suffix.casefold() == ".xml"
            ]
            archivo_corrupto = archivo_zip.testzip()
    except (BadZipFile, OSError) as exc:
        raise ErrorImportacion(
            f"No fue posible validar el paquete archivado {ruta}: {exc}"
        ) from exc

    if archivo_corrupto is not None:
        raise ErrorImportacion(
            f"El ZIP contiene un archivo corrupto: {archivo_corrupto}"
        )
    if not nombres_xml:
        raise ErrorImportacion(
            f"El paquete archivado no contiene XML: {ruta}"
        )

    return PaqueteArchivadoRecuperable(
        ruta_zip=ruta,
        rfc=rfc,
        periodo=periodo,
        tipo_descarga=tipo_normalizado,
        estado_comprobante=estado_normalizado,
        id_paquete_sat=id_paquete_sat,
        total_xml=len(nombres_xml),
    )


def _validar_contexto_xml_sat(
    retencion: RetencionXML,
    paquete: PaqueteSatRegistrado | PaqueteArchivadoRecuperable,
) -> None:
    tipo = paquete.tipo_descarga.strip().upper()
    emisor_rfc = _normalizar_rfc_contexto(retencion.emisor.rfc)
    receptor_rfc = _normalizar_rfc_contexto(retencion.receptor.rfc)
    rfc_solicitante = _normalizar_rfc_contexto(paquete.rfc)

    if tipo == "EMITIDAS":
        coincide = emisor_rfc == rfc_solicitante
        rol_esperado = "emisor"
    elif tipo == "RECIBIDAS":
        coincide = receptor_rfc == rfc_solicitante
        rol_esperado = "receptor"
    else:
        raise ErrorContextoPaqueteSat(
            f"Tipo de descarga SAT no soportado: {paquete.tipo_descarga}"
        )

    if not coincide:
        raise ErrorContextoPaqueteSat(
            f"El XML {retencion.uuid} no contiene el RFC corporativo "
            f"{rfc_solicitante} como {rol_esperado}, aunque proviene del "
            f"paquete {paquete.id_paquete_sat} marcado como {tipo}"
        )


def _normalizar_rfc_contexto(valor: object) -> str:
    return "".join(str(valor or "").split()).upper()


def _ruta_interna(ruta_xml: Path, raiz_extraida: Path) -> str:
    try:
        return ruta_xml.resolve().relative_to(raiz_extraida.resolve()).as_posix()
    except ValueError as exc:
        raise ErrorImportacion(
            f"El XML extraído está fuera del directorio del lote: {ruta_xml}"
        ) from exc


def _calcular_hash(ruta: Path) -> str:
    digest = sha256()
    with Path(ruta).open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _calcular_hash_si_existe(ruta: Path) -> str | None:
    try:
        return _calcular_hash(ruta)
    except OSError:
        return None


def _mover_archivo_verificado(origen: Path, destino: Path, hash_esperado: str) -> Path:
    if destino.exists():
        if _calcular_hash(destino).casefold() != hash_esperado.casefold():
            raise ErrorImportacion(f"El destino ya existe con contenido distinto: {destino}")
        origen.unlink()
        return destino

    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.parent / f".{destino.name}.{uuid4().hex}.temporal"
    try:
        shutil.copyfile(origen, temporal)
        if _calcular_hash(temporal).casefold() != hash_esperado.casefold():
            raise ErrorImportacion(f"La copia del ZIP no conserva el SHA-256: {origen}")
        os.replace(temporal, destino)
        origen.unlink()
        return destino
    finally:
        temporal.unlink(missing_ok=True)


def _eliminar_directorio_extraido(
    directorio: Path,
    raiz_trabajo: Path,
    logger: logging.Logger,
) -> None:
    if not directorio.exists():
        return
    raiz = raiz_trabajo.resolve()
    objetivo = directorio.resolve()
    try:
        objetivo.relative_to(raiz)
    except ValueError:
        logger.error("No se eliminó una ruta de trabajo insegura: %s", objetivo)
        return
    if objetivo == raiz:
        logger.error("No se eliminó la raíz de trabajo: %s", objetivo)
        return
    try:
        shutil.rmtree(objetivo)
    except OSError as exc:
        logger.warning("No se pudo limpiar %s: %s", objetivo, exc)
