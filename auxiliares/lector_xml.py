"""Extracción segura de ZIP y lectura de XML de retenciones.

Paso 1: valida límites, rutas y contenido del archivo ZIP.
Paso 2: extrae únicamente XML dentro de una carpeta temporal controlada.
Paso 3: convierte cada XML fiscal en un objeto fácil de persistir.
"""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Iterable
from uuid import UUID, uuid4
from xml.etree.ElementTree import Element, ParseError
from zipfile import BadZipFile, ZipFile, ZipInfo

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

from auxiliares.configuracion import Settings

MAX_ARCHIVOS_POR_ZIP = 10_000
MAX_TAMANIO_XML = 20 * 1024 * 1024
MAX_TAMANIO_TOTAL_ZIP = 2 * 1024 * 1024 * 1024
MAX_LONGITUD_RUTA_INTERNA_ZIP = 800


class ErrorLectorXML(RuntimeError):
    """Error base del proceso de lectura de retenciones."""


class ErrorArchivoXML(ErrorLectorXML):
    """El XML no existe, está mal formado o carece de información obligatoria."""


class ErrorArchivoZip(ErrorLectorXML):
    """El ZIP es inválido o representa un riesgo al extraerse."""


@dataclass(frozen=True, slots=True)
class EmisorRetencion:
    rfc: str
    razon_social: str
    regimen_fiscal: str | None
    curp: str | None


@dataclass(frozen=True, slots=True)
class ReceptorRetencion:
    nacionalidad: str
    rfc: str | None
    razon_social: str
    domicilio_fiscal: str | None
    registro_tributario: str | None
    curp: str | None


@dataclass(frozen=True, slots=True)
class PeriodoRetencion:
    mes_inicial: int
    mes_final: int
    ejercicio: int


@dataclass(frozen=True, slots=True)
class TotalesRetencion:
    monto_total_operacion: Decimal
    monto_total_gravado: Decimal
    monto_total_exento: Decimal
    monto_total_retenido: Decimal
    utilidad_bimestral: Decimal | None
    isr_correspondiente: Decimal | None


@dataclass(frozen=True, slots=True)
class ImpuestoRetenido:
    base_retencion: Decimal | None
    impuesto: str | None
    monto_retenido: Decimal
    tipo_pago_retencion: str


@dataclass(frozen=True, slots=True)
class TimbreFiscal:
    uuid: str
    version: str
    fecha_timbrado: datetime
    rfc_proveedor_certificacion: str | None
    numero_certificado_sat: str | None


@dataclass(frozen=True, slots=True)
class RetencionRelacionada:
    tipo_relacion: str
    uuid: str


@dataclass(frozen=True, slots=True)
class ElementoComplemento:
    """Elemento genérico de un complemento, incluso si aún no lo conocemos."""

    ruta: str
    namespace: str | None
    nombre: str
    atributos: dict[str, str]


@dataclass(frozen=True, slots=True)
class RetencionXML:
    """Representación fiscal de un comprobante de retenciones."""

    ruta_origen: Path
    nombre_archivo: str
    hash_sha256: str
    version: str
    fecha_expedicion: datetime
    folio_interno: str | None
    lugar_expedicion: str | None
    clave_retencion: str
    descripcion_retencion: str | None
    numero_certificado: str
    emisor: EmisorRetencion
    receptor: ReceptorRetencion
    periodo: PeriodoRetencion
    totales: TotalesRetencion
    impuestos: tuple[ImpuestoRetenido, ...]
    timbre: TimbreFiscal
    retencion_relacionada: RetencionRelacionada | None
    complementos: tuple[ElementoComplemento, ...]
    atributos_documento: dict[str, str]

    @property
    def uuid(self) -> str:
        return self.timbre.uuid


class LectorXML:
    """Coordina la lectura usando las rutas definidas en la configuración."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def descubrir_zips(self) -> tuple[Path, ...]:
        """Devuelve los ZIP pendientes en orden estable por nombre."""
        inbox = self._settings.paths.inbox_dir
        if not inbox.exists():
            raise ErrorArchivoZip(f"No existe la carpeta de entrada: {inbox}")
        if not inbox.is_dir():
            raise ErrorArchivoZip(f"La ruta de entrada no es una carpeta: {inbox}")

        return tuple(
            sorted(
                (
                    path
                    for path in inbox.glob(self._settings.zip_pattern)
                    if path.is_file() and path.suffix.lower() == ".zip"
                ),
                key=lambda path: path.name.casefold(),
            )
        )

    def extraer_zip(self, ruta_zip: Path) -> tuple[Path, ...]:
        """Extrae únicamente XML al área de trabajo configurada."""
        return extraer_xmls_zip(ruta_zip, self._settings.paths.work_dir)

    def leer_xml(self, ruta_xml: Path) -> RetencionXML:
        return leer_retencion_xml(ruta_xml)

    def leer_directorio(self, directorio: Path) -> tuple[RetencionXML, ...]:
        """Lee recursivamente todos los XML de un directorio."""
        if not directorio.exists():
            raise ErrorArchivoXML(f"No existe el directorio: {directorio}")
        if not directorio.is_dir():
            raise ErrorArchivoXML(f"La ruta no es un directorio: {directorio}")

        rutas_xml = sorted(
            (
                path
                for path in directorio.rglob("*")
                if path.is_file() and path.suffix.lower() == ".xml"
            ),
            key=lambda path: str(path).casefold(),
        )
        return tuple(self.leer_xml(path) for path in rutas_xml)


def extraer_xmls_zip(ruta_zip: Path, directorio_trabajo: Path) -> tuple[Path, ...]:
    """Extrae de forma segura los XML de un ZIP.

    La extracción se realiza primero en una carpeta temporal. La carpeta final
    se publica únicamente cuando todos los XML se extrajeron correctamente.
    """
    ruta_zip = Path(ruta_zip)
    directorio_trabajo = Path(directorio_trabajo)

    if not ruta_zip.is_file() or ruta_zip.suffix.lower() != ".zip":
        raise ErrorArchivoZip(f"No es un archivo ZIP válido: {ruta_zip}")

    directorio_trabajo.mkdir(parents=True, exist_ok=True)
    destino_final = directorio_trabajo / ruta_zip.stem
    if destino_final.exists():
        raise ErrorArchivoZip(
            f"Ya existe la extracción de {ruta_zip.name}: {destino_final}"
        )

    destino_temporal = directorio_trabajo / (
        f".{ruta_zip.stem}-{uuid4().hex}.temporal"
    )
    destino_temporal.mkdir(parents=False, exist_ok=False)

    try:
        with ZipFile(ruta_zip, mode="r") as archivo_zip:
            miembros_xml = _validar_miembros_zip(archivo_zip.infolist())
            rutas_extraidas = _extraer_miembros(
                archivo_zip,
                miembros_xml,
                destino_temporal,
            )

        if not rutas_extraidas:
            raise ErrorArchivoZip(f"El ZIP no contiene archivos XML: {ruta_zip}")

        destino_temporal.rename(destino_final)
        return tuple(
            destino_final / ruta.relative_to(destino_temporal)
            for ruta in rutas_extraidas
        )
    except (BadZipFile, OSError) as exc:
        raise ErrorArchivoZip(f"No se pudo extraer {ruta_zip}: {exc}") from exc
    finally:
        if destino_temporal.exists():
            shutil.rmtree(destino_temporal)


def leer_retencion_xml(ruta_xml: Path) -> RetencionXML:
    """Lee un XML de Retenciones 1.x o 2.x sin depender de su namespace."""
    ruta_xml = Path(ruta_xml)
    if not ruta_xml.is_file() or ruta_xml.suffix.lower() != ".xml":
        raise ErrorArchivoXML(f"No es un archivo XML: {ruta_xml}")

    try:
        contenido = ruta_xml.read_bytes()
    except OSError as exc:
        raise ErrorArchivoXML(f"No se pudo leer {ruta_xml}: {exc}") from exc

    if len(contenido) > MAX_TAMANIO_XML:
        raise ErrorArchivoXML(
            f"El XML supera el máximo de {MAX_TAMANIO_XML} bytes: {ruta_xml}"
        )

    try:
        raiz = SafeElementTree.fromstring(contenido)
    except (ParseError, DefusedXmlException) as exc:
        raise ErrorArchivoXML(f"XML inválido o inseguro {ruta_xml}: {exc}") from exc

    if _nombre_local(raiz.tag) != "Retenciones":
        raise ErrorArchivoXML(
            f"La raíz de {ruta_xml.name} no es un comprobante de Retenciones"
        )

    try:
        emisor = _leer_emisor(raiz)
        receptor = _leer_receptor(raiz)
        periodo = _leer_periodo(raiz)
        totales = _leer_totales(raiz)
        impuestos = _leer_impuestos(raiz)
        timbre = _leer_timbre(raiz)

        return RetencionXML(
            ruta_origen=ruta_xml.resolve(),
            nombre_archivo=ruta_xml.name,
            hash_sha256=sha256(contenido).hexdigest(),
            version=_atributo_obligatorio(raiz, "Version", "version"),
            fecha_expedicion=_fecha(
                "FechaExp",
                _atributo_obligatorio(raiz, "FechaExp"),
            ),
            folio_interno=_atributo_opcional(raiz, "FolioInt"),
            lugar_expedicion=_atributo_opcional(
                raiz,
                "LugarExpRetenc",
                "LugarExpRet",
            ),
            clave_retencion=_atributo_obligatorio(raiz, "CveRetenc"),
            descripcion_retencion=_atributo_opcional(raiz, "DescRetenc"),
            numero_certificado=_atributo_obligatorio(
                raiz,
                "NoCertificado",
                "NumCert",
            ),
            emisor=emisor,
            receptor=receptor,
            periodo=periodo,
            totales=totales,
            impuestos=impuestos,
            timbre=timbre,
            retencion_relacionada=_leer_retencion_relacionada(raiz),
            complementos=_leer_complementos(raiz),
            atributos_documento=_atributos_sin_namespace(raiz),
        )
    except (InvalidOperation, ValueError) as exc:
        raise ErrorArchivoXML(f"Datos inválidos en {ruta_xml}: {exc}") from exc


def _leer_emisor(raiz: Element) -> EmisorRetencion:
    elemento = _hijo_obligatorio(raiz, "Emisor")
    return EmisorRetencion(
        rfc=_normalizar_rfc(
            _atributo_obligatorio(elemento, "RfcE", "RFCEmisor")
        ),
        razon_social=_atributo_obligatorio(elemento, "NomDenRazSocE"),
        regimen_fiscal=_atributo_opcional(elemento, "RegimenFiscalE"),
        curp=_atributo_opcional(elemento, "CURPE", "CurpE"),
    )


def _leer_receptor(raiz: Element) -> ReceptorRetencion:
    receptor = _hijo_obligatorio(raiz, "Receptor")
    nacionalidad = _atributo_obligatorio(
        receptor,
        "NacionalidadR",
        "Nacionalidad",
    )
    detalle = _primer_hijo(receptor, "Nacional", "Extranjero")
    if detalle is None:
        raise ValueError("Receptor no contiene Nacional ni Extranjero")

    es_nacional = _nombre_local(detalle.tag) == "Nacional"
    rfc = _atributo_opcional(detalle, "RfcR", "RFCRecep")
    if es_nacional and not rfc:
        raise ValueError("El receptor nacional no contiene RFC")

    return ReceptorRetencion(
        nacionalidad=nacionalidad,
        rfc=_normalizar_rfc(rfc) if rfc else None,
        razon_social=_atributo_obligatorio(detalle, "NomDenRazSocR"),
        domicilio_fiscal=_atributo_opcional(detalle, "DomicilioFiscalR"),
        registro_tributario=_atributo_opcional(
            detalle,
            "NumRegIdTribR",
            "NumRegIdTrib",
        ),
        curp=_atributo_opcional(detalle, "CURPR", "CurpR"),
    )


def _leer_periodo(raiz: Element) -> PeriodoRetencion:
    elemento = _hijo_obligatorio(raiz, "Periodo")
    mes_inicial = _entero(
        "MesIni",
        _atributo_obligatorio(elemento, "MesIni"),
    )
    mes_final = _entero(
        "MesFin",
        _atributo_obligatorio(elemento, "MesFin"),
    )
    ejercicio = _entero(
        "Ejercicio",
        _atributo_obligatorio(elemento, "Ejercicio", "Ejerc"),
    )

    if not 1 <= mes_inicial <= 12 or not 1 <= mes_final <= 12:
        raise ValueError("MesIni y MesFin deben estar entre 1 y 12")
    if mes_inicial > mes_final:
        raise ValueError("MesIni no puede ser mayor que MesFin")

    return PeriodoRetencion(mes_inicial, mes_final, ejercicio)


def _leer_totales(raiz: Element) -> TotalesRetencion:
    elemento = _hijo_obligatorio(raiz, "Totales")
    return TotalesRetencion(
        monto_total_operacion=_decimal_obligatorio(
            elemento,
            "MontoTotOperacion",
            "montoTotOperacion",
        ),
        monto_total_gravado=_decimal_obligatorio(
            elemento,
            "MontoTotGrav",
            "montoTotGrav",
        ),
        monto_total_exento=_decimal_obligatorio(
            elemento,
            "MontoTotExent",
            "montoTotExent",
        ),
        monto_total_retenido=_decimal_obligatorio(
            elemento,
            "MontoTotRet",
            "montoTotRet",
        ),
        utilidad_bimestral=_decimal_opcional(elemento, "UtilidadBimestral"),
        isr_correspondiente=_decimal_opcional(elemento, "ISRCorrespondiente"),
    )


def _leer_impuestos(raiz: Element) -> tuple[ImpuestoRetenido, ...]:
    totales = _hijo_obligatorio(raiz, "Totales")
    impuestos: list[ImpuestoRetenido] = []

    for elemento in _hijos(totales, "ImpRetenidos"):
        base = _atributo_opcional(elemento, "BaseRet")
        impuestos.append(
            ImpuestoRetenido(
                base_retencion=_decimal("BaseRet", base) if base else None,
                impuesto=_atributo_opcional(
                    elemento,
                    "ImpuestoRet",
                    "Impuesto",
                ),
                monto_retenido=_decimal_obligatorio(
                    elemento,
                    "MontoRet",
                    "montoRet",
                ),
                tipo_pago_retencion=_atributo_obligatorio(
                    elemento,
                    "TipoPagoRet",
                ),
            )
        )

    return tuple(impuestos)


def _leer_retencion_relacionada(raiz: Element) -> RetencionRelacionada | None:
    elemento = _primer_hijo(raiz, "CfdiRetenRelacionados")
    if elemento is None:
        return None
    return RetencionRelacionada(
        tipo_relacion=_atributo_obligatorio(elemento, "TipoRelacion"),
        uuid=_uuid(_atributo_obligatorio(elemento, "UUID")),
    )


def _leer_timbre(raiz: Element) -> TimbreFiscal:
    elemento = _primer_descendiente(raiz, "TimbreFiscalDigital")
    if elemento is None:
        raise ValueError("No se encontró TimbreFiscalDigital")

    uuid = _uuid(_atributo_obligatorio(elemento, "UUID"))
    return TimbreFiscal(
        uuid=uuid,
        version=_atributo_obligatorio(elemento, "Version", "version"),
        fecha_timbrado=_fecha(
            "FechaTimbrado",
            _atributo_obligatorio(elemento, "FechaTimbrado"),
        ),
        rfc_proveedor_certificacion=_atributo_opcional(
            elemento,
            "RfcProvCertif",
        ),
        numero_certificado_sat=_atributo_opcional(
            elemento,
            "NoCertificadoSAT",
        ),
    )


def _leer_complementos(raiz: Element) -> tuple[ElementoComplemento, ...]:
    contenedor = _primer_hijo(raiz, "Complemento")
    if contenedor is None:
        return ()

    elementos: list[ElementoComplemento] = []
    for hijo in list(contenedor):
        if _nombre_local(hijo.tag) == "TimbreFiscalDigital":
            continue
        _aplanar_complemento(hijo, "Complemento", elementos)
    return tuple(elementos)


def _aplanar_complemento(
    elemento: Element,
    ruta_padre: str,
    resultado: list[ElementoComplemento],
) -> None:
    nombre = _nombre_local(elemento.tag)
    ruta = f"{ruta_padre}/{nombre}"
    resultado.append(
        ElementoComplemento(
            ruta=ruta,
            namespace=_namespace(elemento.tag),
            nombre=nombre,
            atributos=_atributos_sin_namespace(elemento),
        )
    )
    for hijo in list(elemento):
        _aplanar_complemento(hijo, ruta, resultado)


def _validar_miembros_zip(miembros: Iterable[ZipInfo]) -> tuple[ZipInfo, ...]:
    archivos = [miembro for miembro in miembros if not miembro.is_dir()]
    if len(archivos) > MAX_ARCHIVOS_POR_ZIP:
        raise ErrorArchivoZip(
            f"El ZIP supera el máximo de {MAX_ARCHIVOS_POR_ZIP} archivos"
        )

    xmls: list[ZipInfo] = []
    tamanio_total = 0
    rutas_vistas: set[str] = set()

    for miembro in archivos:
        if miembro.flag_bits & 0x1:
            raise ErrorArchivoZip(
                f"No se admiten archivos ZIP cifrados: {miembro.filename}"
            )
        if _es_enlace_simbolico(miembro):
            raise ErrorArchivoZip(
                f"No se admiten enlaces simbólicos: {miembro.filename}"
            )

        ruta_interna = _ruta_zip_segura(miembro.filename)
        if ruta_interna.suffix.lower() != ".xml":
            continue
        ruta_interna_texto = ruta_interna.as_posix()
        if _longitud_nvarchar(ruta_interna_texto) > MAX_LONGITUD_RUTA_INTERNA_ZIP:
            raise ErrorArchivoZip(
                "La ruta interna del XML supera el máximo de "
                f"{MAX_LONGITUD_RUTA_INTERNA_ZIP} caracteres admitidos: "
                f"{miembro.filename}"
            )
        if miembro.file_size > MAX_TAMANIO_XML:
            raise ErrorArchivoZip(
                f"{miembro.filename} supera el máximo de {MAX_TAMANIO_XML} bytes"
            )

        tamanio_total += miembro.file_size
        if tamanio_total > MAX_TAMANIO_TOTAL_ZIP:
            raise ErrorArchivoZip("El contenido XML descomprimido es demasiado grande")

        clave_ruta = str(ruta_interna).casefold()
        if clave_ruta in rutas_vistas:
            raise ErrorArchivoZip(
                f"El ZIP contiene una ruta XML duplicada: {miembro.filename}"
            )
        rutas_vistas.add(clave_ruta)
        xmls.append(miembro)

    return tuple(xmls)


def _extraer_miembros(
    archivo_zip: ZipFile,
    miembros: Iterable[ZipInfo],
    destino: Path,
) -> tuple[Path, ...]:
    rutas: list[Path] = []
    for miembro in miembros:
        ruta_relativa = _ruta_zip_segura(miembro.filename)
        ruta_destino = destino.joinpath(*ruta_relativa.parts)
        ruta_destino.parent.mkdir(parents=True, exist_ok=True)
        with archivo_zip.open(miembro, mode="r") as origen:
            with ruta_destino.open(mode="xb") as salida:
                shutil.copyfileobj(origen, salida)
        rutas.append(ruta_destino)
    return tuple(rutas)


def _ruta_zip_segura(nombre: str) -> PurePosixPath:
    ruta = PurePosixPath(nombre.replace("\\", "/"))
    if ruta.is_absolute() or not ruta.parts:
        raise ErrorArchivoZip(f"Ruta inválida dentro del ZIP: {nombre}")
    if any(parte in {"", ".", ".."} for parte in ruta.parts):
        raise ErrorArchivoZip(f"Ruta insegura dentro del ZIP: {nombre}")
    if ":" in ruta.parts[0]:
        raise ErrorArchivoZip(f"Ruta absoluta dentro del ZIP: {nombre}")
    return ruta


def _longitud_nvarchar(valor: str) -> int:
    """Cuenta unidades UTF-16, equivalentes al tamaño lógico de NVARCHAR."""
    return len(valor.encode("utf-16-le")) // 2


def _es_enlace_simbolico(miembro: ZipInfo) -> bool:
    modo = miembro.external_attr >> 16
    return bool(modo) and stat.S_ISLNK(modo)


def _hijo_obligatorio(elemento: Element, nombre: str) -> Element:
    hijo = _primer_hijo(elemento, nombre)
    if hijo is None:
        raise ValueError(f"Falta el elemento {nombre}")
    return hijo


def _primer_hijo(elemento: Element, *nombres: str) -> Element | None:
    nombres_validos = set(nombres)
    return next(
        (
            hijo
            for hijo in list(elemento)
            if _nombre_local(hijo.tag) in nombres_validos
        ),
        None,
    )


def _hijos(elemento: Element, nombre: str) -> tuple[Element, ...]:
    return tuple(
        hijo for hijo in list(elemento) if _nombre_local(hijo.tag) == nombre
    )


def _primer_descendiente(elemento: Element, nombre: str) -> Element | None:
    return next(
        (
            descendiente
            for descendiente in elemento.iter()
            if _nombre_local(descendiente.tag) == nombre
        ),
        None,
    )


def _atributo_obligatorio(elemento: Element, *nombres: str) -> str:
    valor = _atributo_opcional(elemento, *nombres)
    if valor is None:
        opciones = " o ".join(nombres)
        raise ValueError(
            f"Falta el atributo {opciones} en {_nombre_local(elemento.tag)}"
        )
    return valor


def _atributo_opcional(elemento: Element, *nombres: str) -> str | None:
    for nombre in nombres:
        valor = elemento.attrib.get(nombre)
        if valor is not None and valor.strip():
            return valor.strip()
    return None


def _atributos_sin_namespace(elemento: Element) -> dict[str, str]:
    return {
        _nombre_local(nombre): valor
        for nombre, valor in elemento.attrib.items()
    }


def _decimal_obligatorio(elemento: Element, *nombres: str) -> Decimal:
    nombre = nombres[0]
    return _decimal(nombre, _atributo_obligatorio(elemento, *nombres))


def _decimal_opcional(elemento: Element, *nombres: str) -> Decimal | None:
    valor = _atributo_opcional(elemento, *nombres)
    return _decimal(nombres[0], valor) if valor is not None else None


def _decimal(nombre: str, valor: str) -> Decimal:
    try:
        return Decimal(valor)
    except InvalidOperation as exc:
        raise ValueError(f"{nombre} no es un decimal válido: {valor}") from exc


def _entero(nombre: str, valor: str) -> int:
    try:
        return int(valor)
    except ValueError as exc:
        raise ValueError(f"{nombre} no es un entero válido: {valor}") from exc


def _fecha(nombre: str, valor: str) -> datetime:
    try:
        return datetime.fromisoformat(valor)
    except ValueError as exc:
        raise ValueError(f"{nombre} no es una fecha ISO válida: {valor}") from exc


def _uuid(valor: str) -> str:
    try:
        return str(UUID(valor)).upper()
    except ValueError as exc:
        raise ValueError(f"UUID inválido: {valor}") from exc


def _normalizar_rfc(valor: str) -> str:
    return "".join(valor.split()).upper()


def _nombre_local(nombre_expandido: str) -> str:
    return nombre_expandido.rsplit("}", 1)[-1]


def _namespace(nombre_expandido: str) -> str | None:
    if nombre_expandido.startswith("{") and "}" in nombre_expandido:
        return nombre_expandido[1:].split("}", 1)[0]
    return None
