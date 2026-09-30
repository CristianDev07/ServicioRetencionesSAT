"""Modelos y estados compartidos por las cuatro fases.

Paso 1: normaliza RFC, periodos, tipos de descarga y estados.
Paso 2: valida que cada objeto del dominio tenga datos coherentes.
Paso 3: transporta respuestas entre SAT, SQL y la orquestación.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class ErrorModeloRetenciones(ValueError):
    """Indica que un dato de dominio para el SAT es inválido."""


class ServicioSat(str, Enum):
    RETENCIONES = "RETENCIONES"


class TipoDescarga(str, Enum):
    EMITIDAS = "EMITIDAS"
    RECIBIDAS = "RECIBIDAS"


class EstadoComprobante(str, Enum):
    VIGENTE = "VIGENTE"
    CANCELADO = "CANCELADO"


class TipoSolicitud(str, Enum):
    CFDI = "CFDI"
    METADATA = "METADATA"


class TipoPeriodo(str, Enum):
    HISTORIAL = "HISTORIAL"
    DIARIA = "DIARIA"
    SEMANAL = "SEMANAL"
    MANUAL = "MANUAL"


class EstadoSolicitudLocal(str, Enum):
    SOLICITUD_ACEPTADA = "SOLICITUD_ACEPTADA"
    CONSULTANDO = "CONSULTANDO"
    COMPLETADA = "COMPLETADA"
    COMPLETADA_SIN_RESULTADOS = "COMPLETADA_SIN_RESULTADOS"
    ERROR_SOLICITUD = "ERROR_SOLICITUD"
    ERROR_CONSULTA = "ERROR_CONSULTA"


class EstadoSolicitudSat(IntEnum):
    TOKEN_INVALIDO = 0
    ACEPTADA = 1
    EN_PROCESO = 2
    TERMINADA = 3
    ERROR = 4
    RECHAZADA = 5
    VENCIDA = 6

    @property
    def descripcion(self) -> str:
        return {
            self.TOKEN_INVALIDO: "Token inválido",
            self.ACEPTADA: "Aceptada",
            self.EN_PROCESO: "En proceso",
            self.TERMINADA: "Terminada",
            self.ERROR: "Error",
            self.RECHAZADA: "Rechazada",
            self.VENCIDA: "Vencida",
        }[self]


class EstadoPaquete(str, Enum):
    PENDIENTE = "PENDIENTE"
    DESCARGANDO = "DESCARGANDO"
    DESCARGADO = "DESCARGADO"
    ERROR_DESCARGA = "ERROR_DESCARGA"
    PROCESANDO = "PROCESANDO"
    PROCESADO = "PROCESADO"
    PROCESADO_CON_ERRORES = "PROCESADO_CON_ERRORES"


@dataclass(frozen=True, slots=True)
class CredencialFiel:
    """Ubicación y secreto cifrado de una e.firma.

    Este objeto no abre archivos ni descifra la contraseña. Esas acciones
    pertenecen a la infraestructura y al cliente SAT, respectivamente.
    """

    certificado_cer: Path
    clave_privada_key: Path
    contrasena_cifrada: str = field(repr=False)

    def __post_init__(self) -> None:
        certificado = Path(self.certificado_cer)
        llave = Path(self.clave_privada_key)

        if certificado.suffix.lower() != ".cer":
            raise ErrorModeloRetenciones(
                "El certificado de la e.firma debe tener extensión .cer."
            )
        if llave.suffix.lower() != ".key":
            raise ErrorModeloRetenciones(
                "La llave privada de la e.firma debe tener extensión .key."
            )
        if not self.contrasena_cifrada:
            raise ErrorModeloRetenciones(
                "La contraseña cifrada de la e.firma es obligatoria."
            )

        object.__setattr__(self, "certificado_cer", certificado)
        object.__setattr__(self, "clave_privada_key", llave)


@dataclass(frozen=True, slots=True)
class EmpresaRetenciones:
    """Razón social habilitada para consultar retenciones en el SAT."""

    razon_social_id: int
    rfc: str
    razon_social: str
    fiel: CredencialFiel

    def __post_init__(self) -> None:
        if self.razon_social_id <= 0:
            raise ErrorModeloRetenciones(
                "razon_social_id debe ser un entero positivo."
            )

        rfc = normalizar_rfc(self.rfc)
        razon_social = _texto_obligatorio(
            self.razon_social,
            "razon_social",
        )
        object.__setattr__(self, "rfc", rfc)
        object.__setattr__(self, "razon_social", razon_social)


@dataclass(frozen=True, slots=True)
class CriterioSolicitudRetenciones:
    """Identidad completa de una solicitud de descarga masiva.

    Todos estos campos forman parte de la búsqueda de una solicitud
    reutilizable. Dos solicitudes solamente son equivalentes cuando sus
    criterios coinciden por completo.
    """

    razon_social_id: int
    rfc: str
    fecha_inicial: datetime
    fecha_final: datetime
    tipo_descarga: TipoDescarga
    estado_comprobante: EstadoComprobante
    tipo_periodo: TipoPeriodo = TipoPeriodo.MANUAL
    tipo_solicitud: TipoSolicitud = TipoSolicitud.CFDI
    servicio_sat: ServicioSat = ServicioSat.RETENCIONES
    turno_diario: int | None = None

    def __post_init__(self) -> None:
        if self.razon_social_id <= 0:
            raise ErrorModeloRetenciones(
                "razon_social_id debe ser un entero positivo."
            )

        object.__setattr__(self, "rfc", normalizar_rfc(self.rfc))
        object.__setattr__(
            self,
            "tipo_descarga",
            _convertir_enum(TipoDescarga, self.tipo_descarga, "tipo_descarga"),
        )
        object.__setattr__(
            self,
            "estado_comprobante",
            _convertir_enum(
                EstadoComprobante,
                self.estado_comprobante,
                "estado_comprobante",
            ),
        )
        object.__setattr__(
            self,
            "tipo_periodo",
            _convertir_enum(
                TipoPeriodo,
                self.tipo_periodo,
                "tipo_periodo",
            ),
        )
        object.__setattr__(
            self,
            "tipo_solicitud",
            _convertir_enum(
                TipoSolicitud,
                self.tipo_solicitud,
                "tipo_solicitud",
            ),
        )
        object.__setattr__(
            self,
            "servicio_sat",
            _convertir_enum(
                ServicioSat,
                self.servicio_sat,
                "servicio_sat",
            ),
        )

        if not isinstance(self.fecha_inicial, datetime):
            raise ErrorModeloRetenciones(
                "fecha_inicial debe ser un datetime."
            )
        if not isinstance(self.fecha_final, datetime):
            raise ErrorModeloRetenciones(
                "fecha_final debe ser un datetime."
            )
        if self.fecha_final < self.fecha_inicial:
            raise ErrorModeloRetenciones(
                "fecha_final no puede ser anterior a fecha_inicial."
            )
        if (
            self.tipo_descarga is TipoDescarga.RECIBIDAS
            and self.estado_comprobante is EstadoComprobante.CANCELADO
        ):
            raise ErrorModeloRetenciones(
                "El SAT no permite solicitar retenciones recibidas canceladas."
            )
        if self.tipo_periodo is TipoPeriodo.DIARIA:
            if self.turno_diario != 1:
                raise ErrorModeloRetenciones(
                    "Las solicitudes DIARIA deben usar turno_diario=1."
                )
        elif self.turno_diario is not None:
            raise ErrorModeloRetenciones(
                "turno_diario sólo se usa con tipo_periodo=DIARIA."
            )

    @property
    def clave(self) -> tuple[object, ...]:
        """Clave estable utilizada para buscar y reutilizar la solicitud."""

        return (
            self.razon_social_id,
            self.servicio_sat.value,
            self.tipo_descarga.value,
            self.estado_comprobante.value,
            self.tipo_solicitud.value,
            self.tipo_periodo.value,
            self.fecha_inicial,
            self.fecha_final,
            self.turno_diario,
        )

    @property
    def nombre_flujo(self) -> str:
        return (
            f"{self.tipo_descarga.value.lower()}_"
            f"{self.estado_comprobante.value.lower()}"
        )


@dataclass(frozen=True, slots=True)
class RespuestaSolicitudSat:
    id_solicitud: str | None
    codigo_estatus: str
    mensaje: str
    respuesta_cruda: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        id_solicitud = _texto_opcional(self.id_solicitud)
        codigo = _texto_obligatorio(self.codigo_estatus, "codigo_estatus")
        mensaje = str(self.mensaje or "").strip()

        object.__setattr__(self, "id_solicitud", id_solicitud)
        object.__setattr__(self, "codigo_estatus", codigo)
        object.__setattr__(self, "mensaje", mensaje)
        object.__setattr__(
            self,
            "respuesta_cruda",
            _mapping_inmutable(self.respuesta_cruda),
        )

    @property
    def aceptada(self) -> bool:
        return self.codigo_estatus == "5000" and bool(self.id_solicitud)


@dataclass(frozen=True, slots=True)
class RespuestaVerificacionSat:
    codigo_estatus: str
    estado_solicitud: EstadoSolicitudSat
    codigo_estado_solicitud: str
    numero_cfdis: int
    mensaje: str
    ids_paquetes: tuple[str, ...] = ()
    respuesta_cruda: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "codigo_estatus",
            _texto_obligatorio(self.codigo_estatus, "codigo_estatus"),
        )
        object.__setattr__(
            self,
            "estado_solicitud",
            _convertir_estado_solicitud(self.estado_solicitud),
        )
        object.__setattr__(
            self,
            "codigo_estado_solicitud",
            str(self.codigo_estado_solicitud or "").strip(),
        )
        if not isinstance(self.numero_cfdis, int) or self.numero_cfdis < 0:
            raise ErrorModeloRetenciones(
                "numero_cfdis debe ser un entero mayor o igual a cero."
            )
        object.__setattr__(self, "mensaje", str(self.mensaje or "").strip())
        object.__setattr__(
            self,
            "ids_paquetes",
            _normalizar_ids_paquetes(self.ids_paquetes),
        )
        object.__setattr__(
            self,
            "respuesta_cruda",
            _mapping_inmutable(self.respuesta_cruda),
        )

    @property
    def en_proceso(self) -> bool:
        return self.estado_solicitud in {
            EstadoSolicitudSat.ACEPTADA,
            EstadoSolicitudSat.EN_PROCESO,
        }

    @property
    def terminada(self) -> bool:
        return self.estado_solicitud is EstadoSolicitudSat.TERMINADA

    @property
    def sin_resultados(self) -> bool:
        return self.codigo_estado_solicitud == "5004"


@dataclass(frozen=True, slots=True)
class RespuestaDescargaSat:
    id_paquete: str
    codigo_estatus: str
    mensaje: str
    contenido_zip: bytes = field(repr=False)
    respuesta_cruda: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id_paquete",
            _texto_obligatorio(self.id_paquete, "id_paquete"),
        )
        object.__setattr__(
            self,
            "codigo_estatus",
            _texto_obligatorio(self.codigo_estatus, "codigo_estatus"),
        )
        object.__setattr__(self, "mensaje", str(self.mensaje or "").strip())
        if not isinstance(self.contenido_zip, bytes):
            raise ErrorModeloRetenciones("contenido_zip debe ser bytes.")
        object.__setattr__(
            self,
            "respuesta_cruda",
            _mapping_inmutable(self.respuesta_cruda),
        )

    @property
    def descargada(self) -> bool:
        return self.codigo_estatus == "5000" and bool(self.contenido_zip)


def crear_criterios_requeridos(
    empresa: EmpresaRetenciones,
    fecha_inicial: datetime,
    fecha_final: datetime,
    tipo_periodo: TipoPeriodo = TipoPeriodo.MANUAL,
) -> tuple[CriterioSolicitudRetenciones, ...]:
    """Crea los tres flujos admitidos actualmente por el negocio."""

    configuraciones = (
        (TipoDescarga.EMITIDAS, EstadoComprobante.VIGENTE),
        (TipoDescarga.RECIBIDAS, EstadoComprobante.VIGENTE),
        (TipoDescarga.EMITIDAS, EstadoComprobante.CANCELADO),
    )
    return tuple(
        CriterioSolicitudRetenciones(
            razon_social_id=empresa.razon_social_id,
            rfc=empresa.rfc,
            fecha_inicial=fecha_inicial,
            fecha_final=fecha_final,
            tipo_descarga=tipo_descarga,
            estado_comprobante=estado_comprobante,
            tipo_periodo=tipo_periodo,
            turno_diario=(1 if tipo_periodo is TipoPeriodo.DIARIA else None),
        )
        for tipo_descarga, estado_comprobante in configuraciones
    )


_RFC_PATTERN = re.compile(
    r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$",
    flags=re.ASCII,
)


def normalizar_rfc(value: str) -> str:
    rfc = str(value or "").strip().upper()
    if not _RFC_PATTERN.fullmatch(rfc):
        raise ErrorModeloRetenciones(
            f"RFC inválido para una solicitud SAT: {value!r}."
        )
    return rfc


def _convertir_enum(
    enum_type: type[Enum],
    value: Any,
    field_name: str,
) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value).strip().upper())
    except (TypeError, ValueError) as exc:
        permitidos = ", ".join(item.value for item in enum_type)
        raise ErrorModeloRetenciones(
            f"{field_name} debe ser uno de estos valores: {permitidos}."
        ) from exc


def _convertir_estado_solicitud(value: Any) -> EstadoSolicitudSat:
    if isinstance(value, EstadoSolicitudSat):
        return value
    try:
        return EstadoSolicitudSat(int(value))
    except (TypeError, ValueError) as exc:
        raise ErrorModeloRetenciones(
            f"Estado de solicitud SAT desconocido: {value!r}."
        ) from exc


def _normalizar_ids_paquetes(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)

    resultado: list[str] = []
    vistos: set[str] = set()
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ErrorModeloRetenciones(
            "ids_paquetes debe ser una colección de identificadores."
        ) from exc

    for value in iterator:
        paquete = _texto_obligatorio(value, "id_paquete")
        if paquete not in vistos:
            resultado.append(paquete)
            vistos.add(paquete)
    return tuple(resultado)


def _mapping_inmutable(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ErrorModeloRetenciones("respuesta_cruda debe ser un mapping.")
    return MappingProxyType(dict(value))


def _texto_obligatorio(value: Any, field_name: str) -> str:
    resultado = str(value or "").strip()
    if not resultado:
        raise ErrorModeloRetenciones(f"{field_name} es obligatorio.")
    return resultado


def _texto_opcional(value: Any) -> str | None:
    resultado = str(value or "").strip()
    return resultado or None
