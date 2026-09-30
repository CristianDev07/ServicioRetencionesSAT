"""Comunicación con los servicios de retenciones del SAT.

Paso 1: prepara la e.firma de una empresa y obtiene/reutiliza su token.
Paso 2: envía solicitudes o consultas al servicio correspondiente.
Paso 3: descarga y valida la respuesta base64 entregada por el SAT.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections.abc import Mapping
from typing import Any, TypeVar

from cfdiclient import (
    Autenticacion,
    DescargaMasiva,
    Fiel,
    SolicitaDescargaEmitidos,
    SolicitaDescargaRecibidos,
    VerificaSolicitudDescarga,
)

from auxiliares.configuracion import SatSettings
from auxiliares.modelos_retenciones import (
    CriterioSolicitudRetenciones,
    EmpresaRetenciones,
    EstadoComprobante,
    RespuestaDescargaSat,
    RespuestaSolicitudSat,
    RespuestaVerificacionSat,
    TipoDescarga,
)

URL_AUTENTICACION_RETENCIONES = (
    "https://retendescargamasivasolicitud.clouda.sat.gob.mx/"
    "Autenticacion/Autenticacion.svc"
)
URL_SOLICITUD_RETENCIONES = (
    "https://retendescargamasivasolicitud.clouda.sat.gob.mx/"
    "SolicitaDescargaService.svc"
)
URL_VERIFICACION_RETENCIONES = (
    "https://retendescargamasivasolicitud.clouda.sat.gob.mx/"
    "VerificaSolicitudDescargaService.svc"
)
URL_DESCARGA_RETENCIONES = (
    "https://retendescargamasiva.clouda.sat.gob.mx/"
    "DescargaMasivaService.svc"
)


class ErrorClienteSatRetenciones(RuntimeError):
    """Fallo al preparar o comunicarse con los servicios del SAT."""


class AutenticacionRetenciones(Autenticacion):
    soap_url = URL_AUTENTICACION_RETENCIONES


class SolicitaRetencionesEmitidas(SolicitaDescargaEmitidos):
    soap_url = URL_SOLICITUD_RETENCIONES


class SolicitaRetencionesRecibidas(SolicitaDescargaRecibidos):
    soap_url = URL_SOLICITUD_RETENCIONES


class VerificaRetenciones(VerificaSolicitudDescarga):
    soap_url = URL_VERIFICACION_RETENCIONES


class DescargaRetenciones(DescargaMasiva):
    soap_url = URL_DESCARGA_RETENCIONES


class ProveedorTokenSat:
    """Conserva un token en memoria por cliente/RFC y lo renueva a tiempo."""

    def __init__(
        self,
        fiel: Fiel,
        settings: SatSettings,
        logger: logging.Logger,
    ) -> None:
        self._fiel = fiel
        self._settings = settings
        self._logger = logger
        self._token = ""
        self._vigente_hasta = 0.0
        self._lock = threading.Lock()

    def obtener(self) -> str:
        with self._lock:
            ahora = time.monotonic()
            if self._token and ahora < self._vigente_hasta:
                return self._token

            try:
                cliente = _nuevo_cliente(
                    AutenticacionRetenciones,
                    self._fiel,
                    self._settings,
                )
                token = cliente.obtener_token()
            except Exception as exc:
                raise ErrorClienteSatRetenciones(
                    "No fue posible obtener el token de autenticación SAT."
                ) from exc

            token = str(token or "").strip()
            if not token:
                raise ErrorClienteSatRetenciones(
                    "El SAT no devolvió un token de autenticación."
                )

            self._token = token
            self._vigente_hasta = (
                ahora + self._settings.token_reuse_seconds
            )
            self._logger.info(
                "Token SAT de retenciones obtenido y almacenado temporalmente"
            )
            return self._token

    def invalidar(self) -> None:
        with self._lock:
            self._token = ""
            self._vigente_hasta = 0.0


class ClienteSatRetenciones:
    """Adaptador de cfdiclient para los endpoints SAT de retenciones.

    Cada instancia pertenece a una sola razón social. De esta manera el
    material criptográfico y el token nunca se comparten entre RFC distintos.
    """

    def __init__(
        self,
        settings: SatSettings,
        empresa: EmpresaRetenciones,
        contrasena_fiel: str,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        if not contrasena_fiel:
            raise ErrorClienteSatRetenciones(
                "La contraseña de la e.firma está vacía."
            )

        self._settings = settings
        self._empresa = empresa
        self._logger = logger or logging.getLogger(
            "retenciones.sat"
        )
        self._fiel = self._crear_fiel(contrasena_fiel)
        self._tokens = ProveedorTokenSat(
            self._fiel,
            settings,
            self._logger,
        )

    @property
    def empresa(self) -> EmpresaRetenciones:
        return self._empresa

    def obtener_token(self) -> str:
        """Autentica o devuelve el token vigente sin crear solicitudes."""

        return self._tokens.obtener()

    def invalidar_token(self) -> None:
        self._tokens.invalidar()

    def solicitar(
        self,
        criterio: CriterioSolicitudRetenciones,
    ) -> RespuestaSolicitudSat:
        """Crea una solicitud SAT; el orquestador debe evitar duplicados."""

        self._validar_criterio(criterio)
        token = self.obtener_token()
        argumentos: dict[str, Any] = {
            "token": token,
            "rfc_solicitante": self._empresa.rfc,
            "fecha_inicial": criterio.fecha_inicial,
            "fecha_final": criterio.fecha_final,
            "tipo_solicitud": criterio.tipo_solicitud.value,
            "estado_comprobante": _estado_para_sat(
                criterio.estado_comprobante
            ),
            "complemento": None,
        }

        if criterio.tipo_descarga is TipoDescarga.EMITIDAS:
            cliente = _nuevo_cliente(
                SolicitaRetencionesEmitidas,
                self._fiel,
                self._settings,
            )
            argumentos["rfc_emisor"] = self._empresa.rfc
        else:
            cliente = _nuevo_cliente(
                SolicitaRetencionesRecibidas,
                self._fiel,
                self._settings,
            )
            argumentos["rfc_receptor"] = self._empresa.rfc

        try:
            respuesta = _mapping_respuesta(
                cliente.solicitar_descarga(**argumentos),
                operacion="solicitud",
            )
        except ErrorClienteSatRetenciones:
            raise
        except Exception as exc:
            raise ErrorClienteSatRetenciones(
                "Falló la solicitud de descarga de retenciones para "
                f"{self._empresa.rfc}."
            ) from exc

        return RespuestaSolicitudSat(
            id_solicitud=respuesta.get("id_solicitud"),
            codigo_estatus=respuesta.get("cod_estatus"),
            mensaje=respuesta.get("mensaje"),
            respuesta_cruda=respuesta,
        )

    def verificar(
        self,
        id_solicitud: str,
    ) -> RespuestaVerificacionSat:
        solicitud = _texto_obligatorio(id_solicitud, "id_solicitud")
        token = self.obtener_token()
        cliente = _nuevo_cliente(
            VerificaRetenciones,
            self._fiel,
            self._settings,
        )
        try:
            respuesta = _mapping_respuesta(
                cliente.verificar_descarga(
                    token,
                    self._empresa.rfc,
                    solicitud,
                ),
                operacion="verificación",
            )
            numero_cfdis = _entero_no_negativo(
                respuesta.get("numero_cfdis"),
                "numero_cfdis",
            )
        except ErrorClienteSatRetenciones:
            raise
        except Exception as exc:
            raise ErrorClienteSatRetenciones(
                "Falló la verificación de una solicitud de retenciones "
                f"para {self._empresa.rfc}."
            ) from exc

        return RespuestaVerificacionSat(
            codigo_estatus=respuesta.get("cod_estatus"),
            estado_solicitud=respuesta.get("estado_solicitud"),
            codigo_estado_solicitud=respuesta.get(
                "codigo_estado_solicitud"
            ),
            numero_cfdis=numero_cfdis,
            mensaje=respuesta.get("mensaje"),
            ids_paquetes=respuesta.get("paquetes") or (),
            respuesta_cruda=respuesta,
        )

    def descargar(self, id_paquete: str) -> RespuestaDescargaSat:
        paquete = _texto_obligatorio(id_paquete, "id_paquete")
        token = self.obtener_token()
        cliente = _nuevo_cliente(
            DescargaRetenciones,
            self._fiel,
            self._settings,
        )
        try:
            respuesta = _mapping_respuesta(
                cliente.descargar_paquete(
                    token,
                    self._empresa.rfc,
                    paquete,
                ),
                operacion="descarga",
            )
            contenido = _decodificar_paquete(respuesta)
        except ErrorClienteSatRetenciones:
            raise
        except Exception as exc:
            raise ErrorClienteSatRetenciones(
                f"Falló la descarga del paquete {paquete}."
            ) from exc

        return RespuestaDescargaSat(
            id_paquete=paquete,
            codigo_estatus=respuesta.get("cod_estatus"),
            mensaje=respuesta.get("mensaje"),
            contenido_zip=contenido,
            # El base64 ya fue convertido al ZIP y no se persiste en SQL.
            # La auditoría de descarga conserva solamente datos pequeños.
            respuesta_cruda={
                "cod_estatus": respuesta.get("cod_estatus"),
                "mensaje": respuesta.get("mensaje"),
            },
        )

    def _crear_fiel(self, contrasena_fiel: str) -> Fiel:
        try:
            certificado = self._empresa.fiel.certificado_cer.read_bytes()
            llave = self._empresa.fiel.clave_privada_key.read_bytes()
            return Fiel(certificado, llave, contrasena_fiel)
        except Exception as exc:
            raise ErrorClienteSatRetenciones(
                "No fue posible abrir la e.firma de "
                f"{self._empresa.rfc}. Verifica el .cer, el .key y su "
                "contraseña."
            ) from exc

    def _validar_criterio(
        self,
        criterio: CriterioSolicitudRetenciones,
    ) -> None:
        if criterio.razon_social_id != self._empresa.razon_social_id:
            raise ErrorClienteSatRetenciones(
                "El criterio pertenece a una razón social diferente."
            )
        if criterio.rfc != self._empresa.rfc:
            raise ErrorClienteSatRetenciones(
                "El RFC del criterio no corresponde al cliente SAT."
            )


_ClienteCfd = TypeVar("_ClienteCfd")


def _nuevo_cliente(
    client_type: type[_ClienteCfd],
    fiel: Fiel,
    settings: SatSettings,
) -> _ClienteCfd:
    return client_type(
        fiel,
        verify=settings.verify_tls,
        timeout=settings.request_timeout_seconds,
    )


def _estado_para_sat(estado: EstadoComprobante) -> str:
    if estado is EstadoComprobante.VIGENTE:
        return "Vigente"
    return "Cancelado"


def _mapping_respuesta(
    value: Any,
    *,
    operacion: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ErrorClienteSatRetenciones(
            f"El SAT devolvió una respuesta inválida durante la {operacion}."
        )
    return dict(value)


def _decodificar_paquete(respuesta: Mapping[str, Any]) -> bytes:
    codigo = str(respuesta.get("cod_estatus") or "").strip()
    paquete_b64 = respuesta.get("paquete_b64")
    if codigo != "5000" or not paquete_b64:
        return b""

    try:
        normalizado = re.sub(r"\s+", "", str(paquete_b64))
        return base64.b64decode(normalizado, validate=True)
    except (TypeError, ValueError) as exc:
        raise ErrorClienteSatRetenciones(
            "El SAT devolvió un paquete con Base64 inválido."
        ) from exc


def _entero_no_negativo(value: Any, field_name: str) -> int:
    if value in (None, ""):
        return 0
    try:
        resultado = int(value)
    except (TypeError, ValueError) as exc:
        raise ErrorClienteSatRetenciones(
            f"{field_name} no contiene un entero válido."
        ) from exc
    if resultado < 0:
        raise ErrorClienteSatRetenciones(
            f"{field_name} no puede ser negativo."
        )
    return resultado


def _texto_obligatorio(value: Any, field_name: str) -> str:
    resultado = str(value or "").strip()
    if not resultado:
        raise ErrorClienteSatRetenciones(
            f"{field_name} es obligatorio."
        )
    return resultado
