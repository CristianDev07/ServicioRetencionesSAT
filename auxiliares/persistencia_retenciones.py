"""Persistencia transaccional de comprobantes de retenciones.

Paso 1: clasifica la participación corporativa como ingreso o gasto.
Paso 2: detecta UUID nuevos, duplicados y conflictos de contenido.
Paso 3: guarda SQL y XML sin sobrescribir archivos incompatibles.

Recibe objetos ya interpretados por :mod:`auxiliares.lector_xml`; no descubre
ZIP ni vuelve a interpretar el documento.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from uuid import uuid4

import pyodbc

from auxiliares.configuracion import Settings
from auxiliares.lector_xml import ElementoComplemento, RetencionXML

EstadoGuardado = Literal["IMPORTADO", "DUPLICADO"]
EstadoComprobante = Literal["VIGENTE", "CANCELADO"]


class ErrorPersistencia(RuntimeError):
    """Error base de las reglas o de la persistencia de una retención."""


class SinRfcCorporativo(ErrorPersistencia):
    """Ninguna de las partes del XML pertenece a Fiscal_razonsocial."""

    def __init__(self, uuid: str, ruta_cuarentena: Path) -> None:
        self.uuid = uuid
        self.ruta_cuarentena = ruta_cuarentena
        super().__init__(
            f"La retención {uuid} no contiene RFC corporativos. "
            f"Se conservó en cuarentena: {ruta_cuarentena}"
        )


class ConflictoUUID(ErrorPersistencia):
    """El UUID ya existe en SQL con contenido XML diferente."""

    def __init__(
        self,
        uuid: str,
        hash_existente: str,
        hash_recibido: str,
        ruta_cuarentena: Path,
    ) -> None:
        self.uuid = uuid
        self.hash_existente = hash_existente
        self.hash_recibido = hash_recibido
        self.ruta_cuarentena = ruta_cuarentena
        super().__init__(
            f"Conflicto para UUID {uuid}: el hash almacenado "
            f"({hash_existente}) difiere del recibido ({hash_recibido}). "
            f"El archivo recibido se conservó en: {ruta_cuarentena}"
        )


class ConflictoRutaXML(ErrorPersistencia):
    """Una ruta final ya contiene bytes diferentes a los del XML recibido."""


@dataclass(frozen=True, slots=True)
class RazonSocialCorporativa:
    id: int
    rfc: str
    razon_social: str
    activa: bool


@dataclass(frozen=True, slots=True)
class ParticipacionPlaneada:
    razon_social: RazonSocialCorporativa
    rol: Literal["EMISOR", "RECEPTOR"]
    tipo_operacion: Literal["GASTO", "INGRESO"]
    ruta_xml_relativa: str


@dataclass(frozen=True, slots=True)
class ResultadoGuardado:
    retencion_id: int
    uuid: str
    estado: EstadoGuardado
    estado_comprobante: EstadoComprobante
    participaciones_creadas: int
    rutas_xml: tuple[str, ...]


class GuardadorRetenciones:
    """Guarda retenciones de forma idempotente en SQL y disco."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def planificar(
        self,
        retencion: RetencionXML,
    ) -> tuple[ParticipacionPlaneada, ...]:
        """Clasifica una retención sin insertar datos ni copiar archivos."""
        conexion = self._abrir_conexion()
        try:
            razones_sociales = self._cargar_razones_sociales(conexion)
            return self._clasificar(retencion, razones_sociales)
        finally:
            conexion.close()

    def guardar(
        self,
        retencion: RetencionXML,
        estado_comprobante: EstadoComprobante = "VIGENTE",
    ) -> ResultadoGuardado:
        """Guarda una retención completa dentro de una transacción.

        Un UUID nuevo produce ``IMPORTADO``. Un UUID existente con el mismo
        hash produce ``DUPLICADO`` y aun así reconstruye participaciones o
        archivos que pudieran faltar. Un hash diferente produce conflicto.
        ``CANCELADO`` siempre tiene prioridad y nunca se degrada a
        ``VIGENTE`` por una importación posterior.
        """
        estado_recibido = _normalizar_estado_comprobante(estado_comprobante)
        conexion = self._abrir_conexion()
        archivos_creados: list[Path] = []

        try:
            razones_sociales = self._cargar_razones_sociales(conexion)
            participaciones = self._clasificar(retencion, razones_sociales)
            if not participaciones:
                conexion.rollback()
                ruta = copiar_a_cuarentena(
                    retencion,
                    self._settings.paths.quarantine_dir,
                    categoria="sin_rfc_corporativo",
                    incluir_hash_en_nombre=True,
                )
                raise SinRfcCorporativo(retencion.uuid, ruta)

            existente = self._buscar_retencion(conexion, retencion.uuid)
            if existente is None:
                retencion_id = self._insertar_retencion(
                    conexion,
                    retencion,
                    estado_recibido,
                )
                self._insertar_impuestos(conexion, retencion_id, retencion)
                estado: EstadoGuardado = "IMPORTADO"
                estado_final = estado_recibido
            else:
                retencion_id, hash_existente, estado_existente = existente
                if hash_existente.casefold() != retencion.hash_sha256.casefold():
                    conexion.rollback()
                    ruta = copiar_a_cuarentena(
                        retencion,
                        self._settings.paths.quarantine_dir,
                        categoria="conflicto_uuid",
                        incluir_hash_en_nombre=True,
                    )
                    raise ConflictoUUID(
                        retencion.uuid,
                        hash_existente,
                        retencion.hash_sha256,
                        ruta,
                    )
                estado = "DUPLICADO"
                estado_final = _estado_comprobante_prioritario(
                    estado_existente,
                    estado_recibido,
                )
                if estado_final != estado_existente:
                    self._actualizar_estado_comprobante(
                        conexion,
                        retencion_id,
                        estado_final,
                    )

            participaciones_creadas = 0
            rutas_efectivas: list[str] = []
            for participacion in participaciones:
                creada, ruta_efectiva = self._guardar_participacion(
                    conexion,
                    retencion_id,
                    participacion,
                )
                participaciones_creadas += int(creada)
                rutas_efectivas.append(ruta_efectiva)

            for ruta_relativa in dict.fromkeys(rutas_efectivas):
                ruta_creada = _asegurar_archivo_final(
                    retencion,
                    self._settings.paths.storage_dir,
                    ruta_relativa,
                )
                if ruta_creada is not None:
                    archivos_creados.append(ruta_creada)

            conexion.commit()
            return ResultadoGuardado(
                retencion_id=retencion_id,
                uuid=retencion.uuid,
                estado=estado,
                estado_comprobante=estado_final,
                participaciones_creadas=participaciones_creadas,
                rutas_xml=tuple(dict.fromkeys(rutas_efectivas)),
            )
        except (SinRfcCorporativo, ConflictoUUID):
            raise
        except Exception as exc:
            conexion.rollback()
            _eliminar_archivos_creados(archivos_creados)
            if isinstance(exc, ErrorPersistencia):
                raise
            raise ErrorPersistencia(
                f"No se pudo guardar la retención {retencion.uuid}: {exc}"
            ) from exc
        finally:
            conexion.close()

    def _abrir_conexion(self) -> pyodbc.Connection:
        try:
            return pyodbc.connect(
                self._settings.database.connection_string,
                autocommit=False,
            )
        except pyodbc.Error as exc:
            raise ErrorPersistencia(f"No se pudo conectar a SQL Server: {exc}") from exc

    @staticmethod
    def _cargar_razones_sociales(
        conexion: pyodbc.Connection,
    ) -> dict[str, RazonSocialCorporativa]:
        """Carga razones activas e inactivas para conservar el histórico."""
        consulta = """
            SELECT id, rfc, razon_social, Activo_Inactivo
            FROM dbo.Fiscal_razonsocial
        """
        razones: dict[str, RazonSocialCorporativa] = {}
        for row in conexion.cursor().execute(consulta):
            rfc = _normalizar_rfc(row.rfc)
            if rfc in razones:
                raise ErrorPersistencia(
                    f"Fiscal_razonsocial contiene el RFC duplicado: {rfc}"
                )
            razones[rfc] = RazonSocialCorporativa(
                id=int(row.id),
                rfc=rfc,
                razon_social=str(row.razon_social).strip(),
                activa=bool(row.Activo_Inactivo),
            )
        return razones

    def _clasificar(
        self,
        retencion: RetencionXML,
        razones: dict[str, RazonSocialCorporativa],
    ) -> tuple[ParticipacionPlaneada, ...]:
        participaciones: list[ParticipacionPlaneada] = []

        emisor = razones.get(_normalizar_rfc(retencion.emisor.rfc))
        if emisor is not None:
            participaciones.append(
                self._crear_participacion(
                    retencion,
                    emisor,
                    rol="EMISOR",
                    tipo_operacion="GASTO",
                )
            )

        if retencion.receptor.rfc:
            receptor = razones.get(_normalizar_rfc(retencion.receptor.rfc))
            if receptor is not None:
                participaciones.append(
                    self._crear_participacion(
                        retencion,
                        receptor,
                        rol="RECEPTOR",
                        tipo_operacion="INGRESO",
                    )
                )

        _validar_colisiones_carpetas(participaciones)
        return tuple(participaciones)

    @staticmethod
    def _crear_participacion(
        retencion: RetencionXML,
        razon_social: RazonSocialCorporativa,
        rol: Literal["EMISOR", "RECEPTOR"],
        tipo_operacion: Literal["GASTO", "INGRESO"],
    ) -> ParticipacionPlaneada:
        direccion = "EMITIDAS" if rol == "EMISOR" else "RECIBIDAS"
        contraparte = (
            retencion.receptor.razon_social
            if rol == "EMISOR"
            else retencion.emisor.razon_social
        )
        ruta = PurePosixPath(
            _nombre_carpeta_seguro(razon_social.rfc),
            "Retenciones",
            "XML",
            direccion,
            str(retencion.fecha_expedicion.year),
            f"{retencion.fecha_expedicion.month:02d}",
            _nombre_carpeta_seguro(contraparte),
            f"{retencion.uuid}.xml",
        )
        _validar_ruta_xml_relativa(str(ruta))
        return ParticipacionPlaneada(
            razon_social=razon_social,
            rol=rol,
            tipo_operacion=tipo_operacion,
            ruta_xml_relativa=str(ruta),
        )

    @staticmethod
    def _buscar_retencion(
        conexion: pyodbc.Connection,
        uuid: str,
    ) -> tuple[int, str, EstadoComprobante] | None:
        consulta = """
            SELECT id, HashSha256, EstadoComprobante
            FROM dbo.Fiscal_Retenciones WITH (UPDLOCK, HOLDLOCK)
            WHERE UUID = ?
        """
        row = conexion.cursor().execute(consulta, uuid).fetchone()
        if row is None:
            return None
        return (
            int(row.id),
            str(row.HashSha256).strip(),
            _normalizar_estado_comprobante(row.EstadoComprobante),
        )

    @staticmethod
    def _actualizar_estado_comprobante(
        conexion: pyodbc.Connection,
        retencion_id: int,
        estado_comprobante: EstadoComprobante,
    ) -> None:
        consulta = """
            UPDATE dbo.Fiscal_Retenciones
            SET EstadoComprobante = ?,
                EstadoActualizadoEn = SYSUTCDATETIME(),
                ActualizadoEn = SYSUTCDATETIME()
            WHERE id = ?
        """
        cursor = conexion.cursor()
        cursor.execute(consulta, estado_comprobante, retencion_id)
        if cursor.rowcount != 1:
            raise ErrorPersistencia(
                "No se pudo actualizar el estado de la retención "
                f"con id {retencion_id}"
            )

    @staticmethod
    def _insertar_retencion(
        conexion: pyodbc.Connection,
        retencion: RetencionXML,
        estado_comprobante: EstadoComprobante,
    ) -> int:
        columnas = (
            "UUID",
            "Version",
            "FechaExp",
            "FolioInterno",
            "LugarExpedicion",
            "CveRetenc",
            "DescRetenc",
            "NoCertificado",
            "TipoRelacion",
            "UUIDRelacionado",
            "EmisorRFC",
            "EmisorRazonSocial",
            "EmisorRegimenFiscal",
            "EmisorCURP",
            "ReceptorNacionalidad",
            "ReceptorRFC",
            "ReceptorRazonSocial",
            "ReceptorDomicilioFiscal",
            "ReceptorNumRegIdTrib",
            "ReceptorCURP",
            "MesInicial",
            "MesFinal",
            "Ejercicio",
            "MontoTotalOperacion",
            "MontoTotalGravado",
            "MontoTotalExento",
            "MontoTotalRetenido",
            "UtilidadBimestral",
            "ISRCorrespondiente",
            "TimbreVersion",
            "FechaTimbrado",
            "RfcProveedorCertificacion",
            "NoCertificadoSAT",
            "HashSha256",
            "NombreArchivoOriginal",
            "ComplementosJson",
            "EstadoComprobante",
        )
        relacionada = retencion.retencion_relacionada
        valores = (
            retencion.uuid,
            retencion.version,
            retencion.fecha_expedicion,
            retencion.folio_interno,
            retencion.lugar_expedicion,
            retencion.clave_retencion,
            retencion.descripcion_retencion,
            retencion.numero_certificado,
            relacionada.tipo_relacion if relacionada else None,
            relacionada.uuid if relacionada else None,
            retencion.emisor.rfc,
            retencion.emisor.razon_social,
            retencion.emisor.regimen_fiscal,
            retencion.emisor.curp,
            retencion.receptor.nacionalidad,
            retencion.receptor.rfc,
            retencion.receptor.razon_social,
            retencion.receptor.domicilio_fiscal,
            retencion.receptor.registro_tributario,
            retencion.receptor.curp,
            retencion.periodo.mes_inicial,
            retencion.periodo.mes_final,
            retencion.periodo.ejercicio,
            retencion.totales.monto_total_operacion,
            retencion.totales.monto_total_gravado,
            retencion.totales.monto_total_exento,
            retencion.totales.monto_total_retenido,
            retencion.totales.utilidad_bimestral,
            retencion.totales.isr_correspondiente,
            retencion.timbre.version,
            retencion.timbre.fecha_timbrado,
            retencion.timbre.rfc_proveedor_certificacion,
            retencion.timbre.numero_certificado_sat,
            retencion.hash_sha256,
            retencion.nombre_archivo,
            _complementos_json(retencion.complementos),
            estado_comprobante,
        )
        marcadores = ", ".join("?" for _ in columnas)
        consulta = f"""
            INSERT INTO dbo.Fiscal_Retenciones
                ({', '.join(columnas)})
            OUTPUT INSERTED.id
            VALUES ({marcadores})
        """
        row = conexion.cursor().execute(consulta, valores).fetchone()
        if row is None:
            raise ErrorPersistencia("SQL Server no devolvió el id de la retención")
        return int(row[0])

    @staticmethod
    def _insertar_impuestos(
        conexion: pyodbc.Connection,
        retencion_id: int,
        retencion: RetencionXML,
    ) -> None:
        consulta = """
            INSERT INTO dbo.Fiscal_Retenciones_Impuestos
                (RetencionId, Secuencia, BaseRetencion, Impuesto,
                 MontoRetenido, TipoPagoRetencion)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        cursor = conexion.cursor()
        for secuencia, impuesto in enumerate(retencion.impuestos, start=1):
            cursor.execute(
                consulta,
                retencion_id,
                secuencia,
                impuesto.base_retencion,
                impuesto.impuesto,
                impuesto.monto_retenido,
                impuesto.tipo_pago_retencion,
            )

    @staticmethod
    def _guardar_participacion(
        conexion: pyodbc.Connection,
        retencion_id: int,
        participacion: ParticipacionPlaneada,
    ) -> tuple[bool, str]:
        consulta_existente = """
            SELECT RutaXmlRelativa
            FROM dbo.Fiscal_Retenciones_Participaciones WITH (UPDLOCK, HOLDLOCK)
            WHERE RetencionId = ? AND RazonSocialId = ? AND Rol = ?
        """
        cursor = conexion.cursor()
        existente = cursor.execute(
            consulta_existente,
            retencion_id,
            participacion.razon_social.id,
            participacion.rol,
        ).fetchone()
        if existente is not None:
            consulta_actualizar = """
                UPDATE dbo.Fiscal_Retenciones_Participaciones
                SET RfcCorporativo = ?,
                    RazonSocialCarpeta = ?,
                    TipoOperacion = ?,
                    RutaXmlRelativa = ?
                WHERE RetencionId = ?
                  AND RazonSocialId = ?
                  AND Rol = ?
            """
            cursor.execute(
                consulta_actualizar,
                participacion.razon_social.rfc,
                _nombre_carpeta_seguro(
                    participacion.razon_social.razon_social
                ),
                participacion.tipo_operacion,
                participacion.ruta_xml_relativa,
                retencion_id,
                participacion.razon_social.id,
                participacion.rol,
            )
            return False, participacion.ruta_xml_relativa

        consulta_insertar = """
            INSERT INTO dbo.Fiscal_Retenciones_Participaciones
                (RetencionId, RazonSocialId, RfcCorporativo,
                 RazonSocialCarpeta, Rol, TipoOperacion, RutaXmlRelativa)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        cursor.execute(
            consulta_insertar,
            retencion_id,
            participacion.razon_social.id,
            participacion.razon_social.rfc,
            _nombre_carpeta_seguro(
                participacion.razon_social.razon_social
            ),
            participacion.rol,
            participacion.tipo_operacion,
            participacion.ruta_xml_relativa,
        )
        return True, participacion.ruta_xml_relativa


def copiar_a_cuarentena(
    retencion: RetencionXML,
    raiz_cuarentena: Path,
    categoria: str,
    incluir_hash_en_nombre: bool = False,
) -> Path:
    """Conserva un XML fuera del flujo normal sin modificar el original."""
    nombre = retencion.uuid
    if incluir_hash_en_nombre:
        nombre = f"{nombre}-{retencion.hash_sha256[:12]}"
    ruta_relativa = Path(
        _nombre_carpeta_seguro(categoria),
        str(retencion.fecha_expedicion.year),
        f"{retencion.fecha_expedicion.month:02d}",
        f"{nombre}.xml",
    )
    destino = Path(raiz_cuarentena) / ruta_relativa
    creada = _copiar_verificando_hash(
        retencion.ruta_origen,
        destino,
        retencion.hash_sha256,
    )
    return creada or destino


def copiar_archivo_a_cuarentena(
    ruta_origen: Path,
    raiz_cuarentena: Path,
    categoria: str,
) -> Path:
    """Conserva un archivo que no pudo convertirse en ``RetencionXML``."""
    ruta_origen = Path(ruta_origen)
    hash_archivo = _hash_archivo(ruta_origen)
    ahora = datetime.now(timezone.utc)
    nombre_base = _nombre_carpeta_seguro(ruta_origen.stem)
    extension = ruta_origen.suffix.lower() or ".xml"
    destino = Path(
        raiz_cuarentena,
        _nombre_carpeta_seguro(categoria),
        str(ahora.year),
        f"{ahora.month:02d}",
        f"{nombre_base}-{hash_archivo[:12]}{extension}",
    )
    creada = _copiar_verificando_hash(ruta_origen, destino, hash_archivo)
    return creada or destino


def _asegurar_archivo_final(
    retencion: RetencionXML,
    raiz_almacenamiento: Path,
    ruta_relativa: str,
) -> Path | None:
    _validar_ruta_xml_relativa(ruta_relativa)
    raiz = Path(raiz_almacenamiento).resolve()
    relativa = PurePosixPath(ruta_relativa)
    if relativa.is_absolute() or ".." in relativa.parts:
        raise ErrorPersistencia(f"Ruta XML relativa insegura: {ruta_relativa}")

    destino = raiz.joinpath(*relativa.parts)
    try:
        destino.resolve().relative_to(raiz)
    except ValueError as exc:
        raise ErrorPersistencia(
            f"La ruta XML sale del almacenamiento permitido: {ruta_relativa}"
        ) from exc
    return _copiar_verificando_hash(
        retencion.ruta_origen,
        destino,
        retencion.hash_sha256,
    )


def _copiar_verificando_hash(
    origen: Path,
    destino: Path,
    hash_esperado: str,
) -> Path | None:
    if destino.exists():
        hash_existente = _hash_archivo(destino)
        if hash_existente.casefold() != hash_esperado.casefold():
            raise ConflictoRutaXML(
                f"La ruta {destino} ya contiene un XML con hash diferente"
            )
        return None

    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.parent / f".{destino.name}.{uuid4().hex}.temporal"
    try:
        shutil.copyfile(origen, temporal)
        hash_copiado = _hash_archivo(temporal)
        if hash_copiado.casefold() != hash_esperado.casefold():
            raise ErrorPersistencia(
                f"La copia temporal de {origen} no conserva el SHA-256"
            )
        os.replace(temporal, destino)
        return destino
    finally:
        if temporal.exists():
            temporal.unlink()


def _eliminar_archivos_creados(rutas: list[Path]) -> None:
    for ruta in reversed(rutas):
        try:
            ruta.unlink(missing_ok=True)
        except OSError:
            pass


def _hash_archivo(ruta: Path) -> str:
    digest = sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _complementos_json(
    complementos: tuple[ElementoComplemento, ...],
) -> str:
    datos = [
        {
            "ruta": complemento.ruta,
            "namespace": complemento.namespace,
            "nombre": complemento.nombre,
            "atributos": complemento.atributos,
        }
        for complemento in complementos
    ]
    return json.dumps(datos, ensure_ascii=False, separators=(",", ":"))


def _validar_colisiones_carpetas(
    participaciones: list[ParticipacionPlaneada],
) -> None:
    rutas: dict[str, str] = {}
    for participacion in participaciones:
        clave = participacion.ruta_xml_relativa.casefold()
        rfc_previo = rutas.get(clave)
        if rfc_previo is not None and rfc_previo != participacion.razon_social.rfc:
            raise ErrorPersistencia(
                "Dos RFC corporativos producen la misma ruta XML final: "
                f"{rfc_previo} y {participacion.razon_social.rfc}"
            )
        rutas[clave] = participacion.razon_social.rfc


def _validar_ruta_xml_relativa(ruta_xml_relativa: str) -> None:
    ruta = PurePosixPath(ruta_xml_relativa)
    if ruta.is_absolute() or any(
        parte in {"", ".", ".."}
        for parte in ruta.parts
    ):
        raise ErrorPersistencia(
            f"Ruta XML relativa insegura: {ruta_xml_relativa}"
        )

    longitud_nvarchar = len(ruta_xml_relativa.encode("utf-16-le")) // 2
    if longitud_nvarchar > 1000:
        raise ErrorPersistencia(
            "La ruta XML relativa supera los 1000 caracteres permitidos "
            f"por SQL Server: {ruta_xml_relativa}"
        )


def _nombre_carpeta_seguro(valor: str) -> str:
    invalidos = '<>:"/\\|?*'
    nombre = " ".join(valor.split()).translate(
        str.maketrans({caracter: "_" for caracter in invalidos})
    )
    nombre = nombre.rstrip(" .")
    reservados = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{numero}" for numero in range(1, 10)),
        *(f"LPT{numero}" for numero in range(1, 10)),
    }
    if not nombre:
        raise ErrorPersistencia("La razón social no produce un nombre de carpeta válido")
    if nombre.upper() in reservados:
        nombre = f"_{nombre}"
    if len(nombre) > 255:
        raise ErrorPersistencia(
            f"La razón social supera el límite de carpeta de Windows: {nombre}"
        )
    return nombre


def _normalizar_rfc(valor: str) -> str:
    return "".join(str(valor).split()).upper()


def _normalizar_estado_comprobante(valor: object) -> EstadoComprobante:
    estado = str(valor).strip().upper()
    if estado not in {"VIGENTE", "CANCELADO"}:
        raise ErrorPersistencia(
            "Estado de comprobante inválido. Debe ser VIGENTE o CANCELADO: "
            f"{valor}"
        )
    return cast(EstadoComprobante, estado)


def _estado_comprobante_prioritario(
    estado_existente: EstadoComprobante,
    estado_recibido: EstadoComprobante,
) -> EstadoComprobante:
    if "CANCELADO" in {estado_existente, estado_recibido}:
        return "CANCELADO"
    return "VIGENTE"
