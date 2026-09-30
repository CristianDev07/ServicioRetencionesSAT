"""Reglas para solicitar, consultar y descargar paquetes del SAT.

Paso 1: planea los criterios que todavía no existen en SQL Server.
Paso 2: crea o consulta solicitudes conservando su auditoría.
Paso 3: reserva y descarga cada paquete de forma idempotente.
"""

from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from auxiliares.configuracion import SatSettings
from auxiliares.cliente_sat import (
    ClienteSatRetenciones,
    ErrorClienteSatRetenciones,
)
from auxiliares.modelos_retenciones import (
    CriterioSolicitudRetenciones,
    EmpresaRetenciones,
    EstadoComprobante,
    EstadoSolicitudLocal,
    RespuestaSolicitudSat,
    TipoDescarga,
    TipoPeriodo,
    TipoSolicitud,
    crear_criterios_requeridos,
)
from auxiliares.repositorios_sql import (
    ErrorRepositorioRetenciones,
    PaqueteSatRegistrado,
    RepositorioSolicitudesRetenciones,
    SolicitudSatRegistrada,
)


EstadoPlan = Literal["PENDIENTE", "REUTILIZABLE"]
EstadoCreacion = Literal[
    "REUTILIZADA",
    "CREADA",
    "RECHAZADA",
    "ERROR_COMUNICACION",
]
EstadoConsulta = Literal[
    "SIN_SOLICITUD",
    "EN_PROCESO",
    "COMPLETADA",
    "SIN_RESULTADOS",
    "ERROR_SAT",
    "ERROR_COMUNICACION",
    "YA_COMPLETADA",
]
EstadoDescarga = Literal[
    "DESCARGADO",
    "RECUPERADO_LOCAL",
    "ERROR_SAT",
    "ERROR_COMUNICACION",
    "ERROR_ARCHIVO",
    "OMITIDO",
]


class ErrorOrquestacionRetenciones(RuntimeError):
    """Fallo que impide continuar sin arriesgar un identificador SAT."""


class ErrorPaqueteZipRetenciones(ValueError):
    """El contenido recibido no es un ZIP íntegro con archivos XML."""


@dataclass(frozen=True, slots=True)
class SolicitudPlaneada:
    criterio: CriterioSolicitudRetenciones
    estado: EstadoPlan
    solicitud_existente: SolicitudSatRegistrada | None


@dataclass(frozen=True, slots=True)
class PlanSolicitudesRetenciones:
    empresa: EmpresaRetenciones
    solicitudes: tuple[SolicitudPlaneada, ...]

    @property
    def total(self) -> int:
        return len(self.solicitudes)

    @property
    def pendientes(self) -> int:
        return sum(
            item.estado == "PENDIENTE"
            for item in self.solicitudes
        )

    @property
    def reutilizables(self) -> int:
        return sum(
            item.estado == "REUTILIZABLE"
            for item in self.solicitudes
        )


@dataclass(frozen=True, slots=True)
class ResultadoSolicitudFlujo:
    criterio: CriterioSolicitudRetenciones
    estado: EstadoCreacion
    solicitud: SolicitudSatRegistrada | None
    codigo_estatus: str | None
    mensaje: str
    duracion_segundos: float


@dataclass(frozen=True, slots=True)
class ResultadoCreacionSolicitudes:
    empresa: EmpresaRetenciones
    resultados: tuple[ResultadoSolicitudFlujo, ...]

    @property
    def creadas(self) -> int:
        return sum(item.estado == "CREADA" for item in self.resultados)

    @property
    def reutilizadas(self) -> int:
        return sum(
            item.estado == "REUTILIZADA"
            for item in self.resultados
        )

    @property
    def rechazadas(self) -> int:
        return sum(
            item.estado == "RECHAZADA"
            for item in self.resultados
        )

    @property
    def errores_comunicacion(self) -> int:
        return sum(
            item.estado == "ERROR_COMUNICACION"
            for item in self.resultados
        )


@dataclass(frozen=True, slots=True)
class ResultadoConsultaFlujo:
    criterio: CriterioSolicitudRetenciones
    estado: EstadoConsulta
    solicitud: SolicitudSatRegistrada | None
    mensaje: str
    duracion_segundos: float


@dataclass(frozen=True, slots=True)
class ResultadoConsultaSolicitudes:
    empresa: EmpresaRetenciones
    resultados: tuple[ResultadoConsultaFlujo, ...]

    @property
    def en_proceso(self) -> int:
        return sum(
            item.estado == "EN_PROCESO"
            for item in self.resultados
        )

    @property
    def completadas(self) -> int:
        return sum(
            item.estado in {"COMPLETADA", "YA_COMPLETADA"}
            for item in self.resultados
        )

    @property
    def sin_resultados(self) -> int:
        return sum(
            item.estado == "SIN_RESULTADOS"
            for item in self.resultados
        )

    @property
    def con_error(self) -> int:
        return sum(
            item.estado
            in {"SIN_SOLICITUD", "ERROR_SAT", "ERROR_COMUNICACION"}
            for item in self.resultados
        )


@dataclass(frozen=True, slots=True)
class ResultadoDescargaPaquete:
    paquete_id: int
    id_paquete_sat: str
    estado: EstadoDescarga
    paquete: PaqueteSatRegistrado | None
    mensaje: str
    duracion_segundos: float


@dataclass(frozen=True, slots=True)
class ResultadoDescargaPaquetes:
    empresa: EmpresaRetenciones
    resultados: tuple[ResultadoDescargaPaquete, ...]

    @property
    def descargados(self) -> int:
        return sum(
            item.estado in {"DESCARGADO", "RECUPERADO_LOCAL"}
            for item in self.resultados
        )

    @property
    def recuperados_localmente(self) -> int:
        return sum(
            item.estado == "RECUPERADO_LOCAL"
            for item in self.resultados
        )

    @property
    def con_error(self) -> int:
        return sum(
            item.estado
            in {"ERROR_SAT", "ERROR_COMUNICACION", "ERROR_ARCHIVO"}
            for item in self.resultados
        )

    @property
    def omitidos(self) -> int:
        return sum(item.estado == "OMITIDO" for item in self.resultados)


class GestorSolicitudesRetenciones:
    """Planifica y crea solicitudes sin repetir criterios ya registrados."""

    def __init__(
        self,
        repositorio: RepositorioSolicitudesRetenciones,
        cliente: ClienteSatRetenciones,
    ) -> None:
        self._repositorio = repositorio
        self._cliente = cliente

    def planificar(
        self,
        fecha_inicial: datetime,
        fecha_final: datetime,
        *,
        tipo_periodo: TipoPeriodo = TipoPeriodo.MANUAL,
    ) -> PlanSolicitudesRetenciones:
        criterios = crear_criterios_requeridos(
            self._cliente.empresa,
            fecha_inicial,
            fecha_final,
            tipo_periodo=tipo_periodo,
        )
        solicitudes = tuple(
            self._planificar_criterio(criterio)
            for criterio in criterios
        )
        return PlanSolicitudesRetenciones(
            empresa=self._cliente.empresa,
            solicitudes=solicitudes,
        )

    def crear_faltantes(
        self,
        plan: PlanSolicitudesRetenciones,
    ) -> ResultadoCreacionSolicitudes:
        if plan.empresa.razon_social_id != (
            self._cliente.empresa.razon_social_id
        ):
            raise ErrorOrquestacionRetenciones(
                "El plan pertenece a una empresa diferente del cliente SAT."
            )

        resultados: list[ResultadoSolicitudFlujo] = []
        for item in plan.solicitudes:
            if item.solicitud_existente is not None:
                resultados.append(
                    ResultadoSolicitudFlujo(
                        criterio=item.criterio,
                        estado="REUTILIZADA",
                        solicitud=item.solicitud_existente,
                        codigo_estatus=(
                            item.solicitud_existente
                            .codigo_estatus_solicitud
                        ),
                        mensaje=(
                            "Se reutilizó la solicitud SAT almacenada."
                        ),
                        duracion_segundos=0.0,
                    )
                )
                continue

            inicio = perf_counter()
            try:
                respuesta = self._cliente.solicitar(item.criterio)
            except ErrorClienteSatRetenciones as exc:
                try:
                    solicitud_error = (
                        self._repositorio.registrar_respuesta_solicitud(
                            item.criterio,
                            RespuestaSolicitudSat(
                                id_solicitud=None,
                                codigo_estatus="ERROR_COMUNICACION",
                                mensaje=str(exc),
                                respuesta_cruda={
                                    "origen": "CLIENTE_RETENCIONES",
                                    "tipo_error": type(exc).__name__,
                                },
                            ),
                        )
                    )
                except ErrorRepositorioRetenciones as persistencia_exc:
                    raise ErrorOrquestacionRetenciones(
                        "Falló el envío al SAT y tampoco fue posible "
                        "registrar el intento en SQL."
                    ) from persistencia_exc
                resultados.append(
                    ResultadoSolicitudFlujo(
                        criterio=item.criterio,
                        estado="ERROR_COMUNICACION",
                        solicitud=solicitud_error,
                        codigo_estatus=None,
                        mensaje=str(exc),
                        duracion_segundos=perf_counter() - inicio,
                    )
                )
                continue

            try:
                solicitud = (
                    self._repositorio.registrar_respuesta_solicitud(
                        item.criterio,
                        respuesta,
                    )
                )
            except ErrorRepositorioRetenciones as exc:
                identificador = respuesta.id_solicitud or "SIN_ID"
                raise ErrorOrquestacionRetenciones(
                    "El SAT respondió la solicitud pero no fue posible "
                    "guardarla en SQL. Conserva inmediatamente este "
                    f"identificador: {identificador}."
                ) from exc

            resultados.append(
                ResultadoSolicitudFlujo(
                    criterio=item.criterio,
                    estado=("CREADA" if respuesta.aceptada else "RECHAZADA"),
                    solicitud=solicitud,
                    codigo_estatus=respuesta.codigo_estatus,
                    mensaje=respuesta.mensaje,
                    duracion_segundos=perf_counter() - inicio,
                )
            )

        return ResultadoCreacionSolicitudes(
            empresa=plan.empresa,
            resultados=tuple(resultados),
        )

    def consultar_una_vez(
        self,
        fecha_inicial: datetime,
        fecha_final: datetime,
        *,
        tipo_periodo: TipoPeriodo = TipoPeriodo.MANUAL,
    ) -> ResultadoConsultaSolicitudes:
        """Consulta cada solicitud pendiente exactamente una vez."""

        plan = self.planificar(
            fecha_inicial,
            fecha_final,
            tipo_periodo=tipo_periodo,
        )
        resultados: list[ResultadoConsultaFlujo] = []
        for item in plan.solicitudes:
            resultados.append(
                self._consultar_solicitud(
                    item.criterio,
                    item.solicitud_existente,
                )
            )

        return ResultadoConsultaSolicitudes(
            empresa=plan.empresa,
            resultados=tuple(resultados),
        )

    def consultar_pendientes_una_vez(
        self,
    ) -> ResultadoConsultaSolicitudes:
        """Consulta una vez todas las solicitudes activas de la empresa."""

        empresa = self._cliente.empresa
        solicitudes = self._repositorio.listar_solicitudes_por_consultar(
            empresa.razon_social_id
        )
        resultados = tuple(
            self._consultar_solicitud(
                _criterio_desde_solicitud(solicitud),
                solicitud,
            )
            for solicitud in solicitudes
        )
        return ResultadoConsultaSolicitudes(
            empresa=empresa,
            resultados=resultados,
        )

    def _consultar_solicitud(
        self,
        criterio: CriterioSolicitudRetenciones,
        solicitud: SolicitudSatRegistrada | None,
    ) -> ResultadoConsultaFlujo:
        if solicitud is None or not solicitud.id_solicitud_sat:
            return ResultadoConsultaFlujo(
                criterio=criterio,
                estado="SIN_SOLICITUD",
                solicitud=solicitud,
                mensaje=(
                    "No existe un identificador SAT reutilizable para "
                    "estos criterios."
                ),
                duracion_segundos=0.0,
            )

        if solicitud.estado in {
            EstadoSolicitudLocal.COMPLETADA.value,
            EstadoSolicitudLocal.COMPLETADA_SIN_RESULTADOS.value,
        }:
            return ResultadoConsultaFlujo(
                criterio=criterio,
                estado=(
                    "SIN_RESULTADOS"
                    if solicitud.sin_resultados
                    else "YA_COMPLETADA"
                ),
                solicitud=solicitud,
                mensaje="La solicitud ya estaba completada en SQL.",
                duracion_segundos=0.0,
            )

        inicio = perf_counter()
        try:
            respuesta = self._cliente.verificar(
                solicitud.id_solicitud_sat
            )
        except ErrorClienteSatRetenciones as exc:
            try:
                actualizada = self._repositorio.registrar_error_consulta(
                    solicitud.id,
                    str(exc),
                )
            except ErrorRepositorioRetenciones as persistencia_exc:
                raise ErrorOrquestacionRetenciones(
                    "Falló la consulta SAT y tampoco fue posible "
                    "registrar el error en SQL."
                ) from persistencia_exc
            return ResultadoConsultaFlujo(
                criterio=criterio,
                estado="ERROR_COMUNICACION",
                solicitud=actualizada,
                mensaje=str(exc),
                duracion_segundos=perf_counter() - inicio,
            )

        try:
            actualizada = self._repositorio.actualizar_verificacion(
                solicitud.id,
                respuesta,
            )
        except ErrorRepositorioRetenciones as exc:
            raise ErrorOrquestacionRetenciones(
                "El SAT respondió la verificación, pero no fue posible "
                "actualizar SQL."
            ) from exc

        if respuesta.sin_resultados:
            estado: EstadoConsulta = "SIN_RESULTADOS"
        elif respuesta.terminada:
            estado = "COMPLETADA"
        elif respuesta.en_proceso:
            estado = "EN_PROCESO"
        else:
            estado = "ERROR_SAT"
        return ResultadoConsultaFlujo(
            criterio=criterio,
            estado=estado,
            solicitud=actualizada,
            mensaje=respuesta.mensaje,
            duracion_segundos=perf_counter() - inicio,
        )

    def _planificar_criterio(
        self,
        criterio: CriterioSolicitudRetenciones,
    ) -> SolicitudPlaneada:
        existente = self._repositorio.buscar_reutilizable(criterio)
        return SolicitudPlaneada(
            criterio=criterio,
            estado=("REUTILIZABLE" if existente else "PENDIENTE"),
            solicitud_existente=existente,
        )


class GestorDescargaPaquetesRetenciones:
    """Descarga y conserva paquetes SAT íntegros antes de importar sus XML."""

    def __init__(
        self,
        settings: SatSettings,
        repositorio: RepositorioSolicitudesRetenciones,
        cliente: ClienteSatRetenciones,
    ) -> None:
        self._settings = settings
        self._repositorio = repositorio
        self._cliente = cliente
        self._root_dir = settings.base.paths.root_dir.resolve()
        self._pending_dir = settings.base.paths.sat_pending_dir.resolve()
        self._error_dir = settings.base.paths.sat_error_dir.resolve()
        _validar_directorio_dentro_raiz(self._pending_dir, self._root_dir)
        _validar_directorio_dentro_raiz(self._error_dir, self._root_dir)

    def descargar_pendientes(self) -> ResultadoDescargaPaquetes:
        empresa = self._cliente.empresa
        candidatos = self._repositorio.listar_paquetes_por_descargar(
            empresa.razon_social_id
        )
        resultados: list[ResultadoDescargaPaquete] = []

        for candidato in candidatos:
            inicio = perf_counter()
            if candidato.rfc != empresa.rfc:
                raise ErrorOrquestacionRetenciones(
                    "SQL devolvió un paquete asociado a un RFC diferente "
                    "del cliente SAT activo."
                )

            reservado = self._repositorio.reservar_paquete(candidato.id)
            if reservado is None:
                resultados.append(
                    ResultadoDescargaPaquete(
                        paquete_id=candidato.id,
                        id_paquete_sat=candidato.id_paquete_sat,
                        estado="OMITIDO",
                        paquete=None,
                        mensaje=(
                            "Otro proceso reservó el paquete antes de "
                            "esta ejecución."
                        ),
                        duracion_segundos=perf_counter() - inicio,
                    )
                )
                continue

            resultado = self._descargar_reservado(reservado)
            resultados.append(
                replace(
                    resultado,
                    duracion_segundos=perf_counter() - inicio,
                )
            )

        return ResultadoDescargaPaquetes(
            empresa=empresa,
            resultados=tuple(resultados),
        )

    def _descargar_reservado(
        self,
        paquete: PaqueteSatRegistrado,
    ) -> ResultadoDescargaPaquete:
        destino = self._ruta_paquete(self._pending_dir, paquete)

        if destino.is_file():
            try:
                contenido = destino.read_bytes()
                _validar_zip_retenciones(contenido)
            except (OSError, ErrorPaqueteZipRetenciones) as exc:
                self._preservar_archivo_invalido(destino, paquete)
            else:
                mensaje_recuperado = (
                    "El ZIP ya existía, se validó y se recuperó sin "
                    "solicitarlo nuevamente al SAT."
                )
                actualizado = self._registrar_descargado(
                    paquete,
                    destino,
                    contenido,
                    codigo_estatus="RECUPERADO_LOCAL",
                    mensaje=mensaje_recuperado,
                    respuesta_cruda={
                        "cod_estatus": "RECUPERADO_LOCAL",
                        "mensaje": mensaje_recuperado,
                    },
                )
                return ResultadoDescargaPaquete(
                    paquete_id=paquete.id,
                    id_paquete_sat=paquete.id_paquete_sat,
                    estado="RECUPERADO_LOCAL",
                    paquete=actualizado,
                    mensaje="Paquete recuperado desde almacenamiento local.",
                    duracion_segundos=0.0,
                )

        try:
            respuesta = self._cliente.descargar(paquete.id_paquete_sat)
        except ErrorClienteSatRetenciones as exc:
            actualizado = self._registrar_error(
                paquete,
                codigo_estatus="ERROR_COMUNICACION",
                mensaje=str(exc),
                respuesta_cruda={
                    "cod_estatus": "ERROR_COMUNICACION",
                    "mensaje": str(exc),
                },
            )
            return ResultadoDescargaPaquete(
                paquete_id=paquete.id,
                id_paquete_sat=paquete.id_paquete_sat,
                estado="ERROR_COMUNICACION",
                paquete=actualizado,
                mensaje=str(exc),
                duracion_segundos=0.0,
            )

        if not respuesta.descargada:
            mensaje = respuesta.mensaje or (
                "El SAT no entregó contenido para el paquete."
            )
            actualizado = self._registrar_error(
                paquete,
                codigo_estatus=respuesta.codigo_estatus,
                mensaje=mensaje,
                respuesta_cruda=respuesta.respuesta_cruda,
            )
            return ResultadoDescargaPaquete(
                paquete_id=paquete.id,
                id_paquete_sat=paquete.id_paquete_sat,
                estado="ERROR_SAT",
                paquete=actualizado,
                mensaje=mensaje,
                duracion_segundos=0.0,
            )

        contenido = respuesta.contenido_zip
        try:
            _validar_zip_retenciones(contenido)
        except ErrorPaqueteZipRetenciones as exc:
            ruta_error = self._guardar_contenido_invalido(
                contenido,
                paquete,
            )
            actualizado = self._registrar_error(
                paquete,
                codigo_estatus="ZIP_INVALIDO",
                mensaje=str(exc),
                ruta=ruta_error,
                contenido=contenido,
                respuesta_cruda=respuesta.respuesta_cruda,
            )
            return ResultadoDescargaPaquete(
                paquete_id=paquete.id,
                id_paquete_sat=paquete.id_paquete_sat,
                estado="ERROR_ARCHIVO",
                paquete=actualizado,
                mensaje=str(exc),
                duracion_segundos=0.0,
            )

        try:
            _escribir_atomico(destino, contenido)
        except OSError as exc:
            mensaje = f"No fue posible guardar el ZIP descargado: {exc}"
            actualizado = self._registrar_error(
                paquete,
                codigo_estatus="ERROR_ESCRITURA",
                mensaje=mensaje,
                respuesta_cruda=respuesta.respuesta_cruda,
            )
            return ResultadoDescargaPaquete(
                paquete_id=paquete.id,
                id_paquete_sat=paquete.id_paquete_sat,
                estado="ERROR_ARCHIVO",
                paquete=actualizado,
                mensaje=mensaje,
                duracion_segundos=0.0,
            )

        actualizado = self._registrar_descargado(
            paquete,
            destino,
            contenido,
            codigo_estatus=respuesta.codigo_estatus,
            mensaje=respuesta.mensaje,
            respuesta_cruda=respuesta.respuesta_cruda,
        )
        return ResultadoDescargaPaquete(
            paquete_id=paquete.id,
            id_paquete_sat=paquete.id_paquete_sat,
            estado="DESCARGADO",
            paquete=actualizado,
            mensaje=respuesta.mensaje or "Paquete descargado correctamente.",
            duracion_segundos=0.0,
        )

    def _ruta_paquete(
        self,
        base_dir: Path,
        paquete: PaqueteSatRegistrado,
        *,
        sufijo: str = "",
    ) -> Path:
        id_paquete = _segmento_archivo(paquete.id_paquete_sat)
        rfc = _segmento_directorio(paquete.rfc, "RFC")
        tipo = _segmento_directorio(paquete.tipo_descarga, "tipo_descarga")
        estado = _segmento_directorio(
            paquete.estado_comprobante,
            "estado_comprobante",
        )
        periodo = (
            f"{paquete.fecha_inicial:%Y-%m-%d}_"
            f"{paquete.fecha_final:%Y-%m-%d}"
        )
        nombre = f"{id_paquete}{sufijo}.zip"
        destino = (
            base_dir / rfc / periodo / tipo / estado / nombre
        ).resolve()
        _validar_directorio_dentro_raiz(destino, base_dir)
        return destino

    def _registrar_descargado(
        self,
        paquete: PaqueteSatRegistrado,
        destino: Path,
        contenido: bytes,
        *,
        codigo_estatus: str,
        mensaje: str,
        respuesta_cruda: Mapping[str, object],
    ) -> PaqueteSatRegistrado:
        try:
            return self._repositorio.marcar_paquete_descargado(
                paquete.id,
                ruta_descarga=self._ruta_relativa(destino),
                hash_sha256=sha256(contenido).hexdigest(),
                tamanio_bytes=len(contenido),
                codigo_estatus=codigo_estatus,
                mensaje=mensaje,
                respuesta_descarga_sat=respuesta_cruda,
            )
        except ErrorRepositorioRetenciones as exc:
            raise ErrorOrquestacionRetenciones(
                "El ZIP quedó guardado y validado, pero no fue posible "
                "registrarlo en SQL. La próxima ejecución lo recuperará "
                "desde almacenamiento local."
            ) from exc

    def _registrar_error(
        self,
        paquete: PaqueteSatRegistrado,
        *,
        codigo_estatus: str,
        mensaje: str,
        ruta: Path | None = None,
        contenido: bytes | None = None,
        respuesta_cruda: Mapping[str, object] | None = None,
    ) -> PaqueteSatRegistrado:
        try:
            return self._repositorio.marcar_error_descarga(
                paquete.id,
                codigo_estatus=codigo_estatus,
                mensaje=mensaje,
                ruta_descarga=(self._ruta_relativa(ruta) if ruta else None),
                hash_sha256=(
                    sha256(contenido).hexdigest()
                    if contenido is not None
                    else None
                ),
                tamanio_bytes=(
                    len(contenido) if contenido is not None else None
                ),
                respuesta_descarga_sat=respuesta_cruda,
            )
        except ErrorRepositorioRetenciones as exc:
            raise ErrorOrquestacionRetenciones(
                "No fue posible registrar en SQL el error de descarga del "
                f"paquete {paquete.id_paquete_sat}."
            ) from exc

    def _preservar_archivo_invalido(
        self,
        origen: Path,
        paquete: PaqueteSatRegistrado,
    ) -> Path:
        contenido = origen.read_bytes()
        sufijo = f"-{sha256(contenido).hexdigest()[:12]}"
        destino = self._ruta_paquete(
            self._error_dir,
            paquete,
            sufijo=sufijo,
        )
        destino.parent.mkdir(parents=True, exist_ok=True)
        os.replace(origen, destino)
        return destino

    def _guardar_contenido_invalido(
        self,
        contenido: bytes,
        paquete: PaqueteSatRegistrado,
    ) -> Path:
        sufijo = f"-{sha256(contenido).hexdigest()[:12]}"
        destino = self._ruta_paquete(
            self._error_dir,
            paquete,
            sufijo=sufijo,
        )
        _escribir_atomico(destino, contenido)
        return destino

    def _ruta_relativa(self, ruta: Path) -> str:
        resuelta = ruta.resolve()
        _validar_directorio_dentro_raiz(resuelta, self._root_dir)
        return resuelta.relative_to(self._root_dir).as_posix()


def _criterio_desde_solicitud(
    solicitud: SolicitudSatRegistrada,
) -> CriterioSolicitudRetenciones:
    return CriterioSolicitudRetenciones(
        razon_social_id=solicitud.razon_social_id,
        rfc=solicitud.rfc,
        fecha_inicial=solicitud.fecha_inicial,
        fecha_final=solicitud.fecha_final,
        tipo_descarga=TipoDescarga(solicitud.tipo_descarga),
        estado_comprobante=EstadoComprobante(
            solicitud.estado_comprobante
        ),
        tipo_periodo=TipoPeriodo(solicitud.tipo_periodo),
        tipo_solicitud=TipoSolicitud(solicitud.tipo_solicitud),
        turno_diario=solicitud.turno_diario,
    )


def _validar_zip_retenciones(contenido: bytes) -> int:
    if not contenido:
        raise ErrorPaqueteZipRetenciones("El paquete ZIP está vacío.")
    try:
        with zipfile.ZipFile(BytesIO(contenido), "r") as archivo:
            corrupto = archivo.testzip()
            if corrupto is not None:
                raise ErrorPaqueteZipRetenciones(
                    f"El ZIP contiene una entrada corrupta: {corrupto}."
                )
            cantidad_xml = sum(
                1
                for info in archivo.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".xml")
            )
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ErrorPaqueteZipRetenciones(
            "El contenido recibido no es un archivo ZIP válido."
        ) from exc
    if cantidad_xml == 0:
        raise ErrorPaqueteZipRetenciones(
            "El ZIP no contiene archivos XML de retenciones."
        )
    return cantidad_xml


def _escribir_atomico(destino: Path, contenido: bytes) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(f".{destino.name}.{uuid4().hex}.tmp")
    try:
        with temporal.open("xb") as archivo:
            archivo.write(contenido)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, destino)
    finally:
        if temporal.exists():
            temporal.unlink()


def _segmento_archivo(value: str) -> str:
    resultado = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9_-]{1,200}", resultado) is None:
        raise ErrorOrquestacionRetenciones(
            "id_paquete_sat contiene caracteres no permitidos."
        )
    return resultado


def _segmento_directorio(value: str, nombre: str) -> str:
    resultado = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9_-]{1,30}", resultado) is None:
        raise ErrorOrquestacionRetenciones(
            f"{nombre} contiene caracteres no permitidos."
        )
    return resultado


def _validar_directorio_dentro_raiz(ruta: Path, raiz: Path) -> None:
    try:
        ruta.resolve().relative_to(raiz.resolve())
    except ValueError as exc:
        raise ErrorOrquestacionRetenciones(
            f"La ruta {ruta} sale del directorio permitido {raiz}."
        ) from exc
