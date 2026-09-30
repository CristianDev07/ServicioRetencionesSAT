"""Preparación y cierre de cada ejecución del proceso.

Paso 1: interpreta las opciones, carga ``.env`` y configura el log.
Paso 2: descubre las empresas y construye el contexto compartido.
Paso 3: ejecuta un ciclo protegido por un bloqueo de instancia.
Paso 4: resume resultados y, si se solicitó, repite en modo servicio.

Las reglas de solicitar, consultar, descargar e importar no viven aquí. Este
módulo sólo prepara el entorno común que utilizan esos cuatro archivos.
"""

from __future__ import annotations

import argparse
import logging
import signal
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from logging.handlers import RotatingFileHandler
from threading import Event
from time import perf_counter
from typing import Callable, Iterator, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

from auxiliares.bloqueo_instancia import (
    BloqueoInstanciaRetenciones,
    InstanciaRetencionesActivaError,
)
from auxiliares.cliente_sat import ClienteSatRetenciones
from auxiliares.configuracion import (
    ConfigurationError,
    SatSettings,
    Settings,
    load_sat_settings,
)
from auxiliares.importador_archivos import ImportadorRetenciones
from auxiliares.modelos_retenciones import EmpresaRetenciones, TipoPeriodo
from auxiliares.notificador_telegram import NotificadorTelegram
from auxiliares.operaciones_sat import GestorSolicitudesRetenciones
from auxiliares.planificador_automatico import planificar_empresas
from auxiliares.repositorios_sql import (
    RepositorioEmpresasRetenciones,
    RepositorioSolicitudesRetenciones,
)

NOMBRE_LOGGER = "retenciones"
NOMBRE_ARCHIVO_LOG = "importador_retenciones.log"


@dataclass(frozen=True, slots=True)
class OpcionesEjecucion:
    """Opciones ya validadas; ningún paso vuelve a interpretar argumentos."""

    servicio: bool
    max_ciclos: int | None
    recuperar_paquetes: bool
    solo_validar_recuperacion: bool
    solo_importar_manuales: bool
    manual: bool
    historico: bool
    continuar_pendientes: bool
    fecha_inicial: date | None
    fecha_final: date | None
    rfc: str | None

    @property
    def periodo_explicito(self) -> bool:
        return self.manual or self.historico


@dataclass(slots=True)
class ResumenCiclo:
    """Contadores compartidos por los cuatro pasos principales."""

    empresas_disponibles: int = 0
    empresas_con_error: int = 0
    solicitudes_creadas: int = 0
    solicitudes_reutilizadas: int = 0
    solicitudes_rechazadas: int = 0
    solicitudes_con_error: int = 0
    solicitudes_consultadas: int = 0
    paquetes_descargados: int = 0
    paquetes_con_error: int = 0
    bytes_paquetes_disponibles: int = 0
    lotes_procesados: int = 0
    lotes_con_error: int = 0
    archivos_xml: int = 0
    archivos_importados: int = 0
    archivos_duplicados: int = 0
    archivos_con_conflicto: int = 0
    archivos_con_error: int = 0

    @property
    def tiene_errores(self) -> bool:
        return any(
            (
                self.empresas_con_error,
                self.solicitudes_rechazadas,
                self.solicitudes_con_error,
                self.paquetes_con_error,
                self.lotes_con_error,
            )
        )


@dataclass(slots=True)
class ResumenEmpresa:
    """Contadores compactos para monitorear una empresa en Telegram."""

    solicitudes_creadas: int = 0
    solicitudes_reutilizadas: int = 0
    solicitudes_rechazadas: int = 0
    solicitudes_con_error: int = 0
    solicitudes_consultadas: int = 0
    paquetes_descargados: int = 0
    paquetes_con_error: int = 0
    bytes_zip: int = 0
    lotes_procesados: int = 0
    lotes_con_error: int = 0
    archivos_xml: int = 0
    archivos_importados: int = 0
    archivos_duplicados: int = 0
    archivos_con_conflicto: int = 0
    archivos_con_error: int = 0

    @property
    def tiene_errores(self) -> bool:
        return any(
            (
                self.solicitudes_rechazadas,
                self.solicitudes_con_error,
                self.paquetes_con_error,
                self.lotes_con_error,
                self.archivos_con_conflicto,
                self.archivos_con_error,
            )
        )


@dataclass(slots=True)
class EmpresaEnProceso:
    """Estado mínimo que una empresa comparte entre las cuatro fases."""

    empresa: EmpresaRetenciones
    periodo: tuple[datetime, datetime, TipoPeriodo] | None = None
    resumen: ResumenEmpresa = field(default_factory=ResumenEmpresa)
    cliente: ClienteSatRetenciones | None = None
    gestor_solicitudes: GestorSolicitudesRetenciones | None = None
    error: str | None = None

    @property
    def disponible(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class ContextoEjecucion:
    """Dependencias y estado de un único ciclo de retenciones."""

    settings: SatSettings
    opciones: OpcionesEjecucion
    logger: logging.Logger
    importador: ImportadorRetenciones
    resumen: ResumenCiclo
    empresas: tuple[EmpresaEnProceso, ...] = ()
    repositorio_empresas: RepositorioEmpresasRetenciones | None = None
    repositorio_solicitudes: RepositorioSolicitudesRetenciones | None = None
    repetir_pronto: bool = False
    id_ejecucion: str = ""
    inicio_monotonic: float = 0.0
    codigo_salida_forzado: int | None = None

    @property
    def usa_flujo_sat(self) -> bool:
        return not (
            self.opciones.recuperar_paquetes
            or self.opciones.solo_importar_manuales
        )

    @property
    def crea_solicitudes(self) -> bool:
        """Indica si el ciclo puede enviar solicitudes nuevas al SAT."""

        return self.usa_flujo_sat and not self.opciones.continuar_pendientes

    def registrar_error_empresa(
        self,
        estado: EmpresaEnProceso,
        fase: str,
        error: Exception,
    ) -> None:
        if estado.error is None:
            self.resumen.empresas_con_error += 1
        estado.error = f"{fase}: {error}"
        self.logger.exception(
            "[%s] La empresa %s (%s) no puede continuar: %s",
            fase,
            estado.empresa.razon_social,
            estado.empresa.rfc,
            error,
        )

    def codigo_salida(self) -> int:
        if self.codigo_salida_forzado is not None:
            return self.codigo_salida_forzado
        return 1 if self.resumen.tiene_errores else 0


FlujoRetenciones = Callable[[ContextoEjecucion], None]


@dataclass(frozen=True, slots=True)
class ResultadoCiclo:
    codigo_salida: int
    repetir_pronto: bool
    id_ejecucion: str
    resumen: ResumenCiclo | None = None
    empresas: tuple[EmpresaEnProceso, ...] = ()


class ErrorPreparacion(RuntimeError):
    """El ciclo no puede iniciar por un error de descubrimiento o filtro."""


def ejecutar_aplicacion(
    flujo: FlujoRetenciones,
    argv: Sequence[str] | None = None,
) -> int:
    """Prepara la aplicación y delega las cuatro fases al ``flujo``."""

    # Paso 1: validar argumentos antes de abrir conexiones o crear carpetas.
    parser = _crear_parser()
    namespace = parser.parse_args(argv)
    opciones = _validar_opciones(parser, namespace)

    # Paso 2: cargar configuración y preparar observabilidad.
    try:
        settings = load_sat_settings()
        _crear_directorios(settings.base)
        logger = _configurar_logging(settings.base)
        notificador = NotificadorTelegram(settings, logger)
    except (ConfigurationError, OSError) as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger(NOMBRE_LOGGER).error(
            "Configuración inválida: %s",
            exc,
        )
        return 2

    # Paso 3: impedir dos procesos que puedan duplicar trabajo.
    ruta_bloqueo = settings.base.paths.process_dir / "servicio_retenciones.lock"
    try:
        with BloqueoInstanciaRetenciones(ruta_bloqueo):
            if opciones.servicio:
                return _ejecutar_como_servicio(
                    settings,
                    opciones,
                    flujo,
                    logger,
                    notificador,
                )
            return _ejecutar_ciclo(
                settings,
                opciones,
                flujo,
                logger,
            ).codigo_salida
    except InstanciaRetencionesActivaError as exc:
        logger.error("%s Archivo de bloqueo: %s", exc, ruta_bloqueo)
        return 3
    except KeyboardInterrupt:
        logger.info("Ejecución detenida por el usuario")
        return 130


def _ejecutar_ciclo(
    settings: SatSettings,
    opciones: OpcionesEjecucion,
    flujo: FlujoRetenciones,
    logger: logging.Logger,
) -> ResultadoCiclo:
    id_ejecucion = uuid4().hex[:12].upper()
    inicio_monotonic = perf_counter()
    logger.info(
        "========== INICIO RETENCIONES | ejecucion=%s ==========",
        id_ejecucion,
    )
    try:
        contexto = _crear_contexto(
            settings,
            opciones,
            logger,
            id_ejecucion=id_ejecucion,
            inicio_monotonic=inicio_monotonic,
        )
    except ErrorPreparacion as exc:
        logger.error("No fue posible preparar el ciclo: %s", exc)
        return ResultadoCiclo(2, True, id_ejecucion)
    except Exception:
        logger.exception("No fue posible preparar el ciclo de retenciones")
        return ResultadoCiclo(2, True, id_ejecucion)

    try:
        flujo(contexto)
    except Exception:
        logger.exception("El ciclo terminó por un error no controlado")
        codigo_salida = 1
    else:
        codigo_salida = contexto.codigo_salida()

    duracion = perf_counter() - inicio_monotonic
    _registrar_resumen(contexto.resumen, logger, duracion, id_ejecucion)
    logger.info(
        "========== FIN RETENCIONES | ejecucion=%s | duracion=%.2fs ==========",
        id_ejecucion,
        duracion,
    )
    return ResultadoCiclo(
        codigo_salida,
        contexto.repetir_pronto,
        id_ejecucion,
        contexto.resumen,
        contexto.empresas,
    )


def _crear_contexto(
    settings: SatSettings,
    opciones: OpcionesEjecucion,
    logger: logging.Logger,
    *,
    id_ejecucion: str,
    inicio_monotonic: float,
) -> ContextoEjecucion:
    importador = ImportadorRetenciones(settings.base, logger)
    resumen = ResumenCiclo()

    # Recuperación e importación manual no necesitan descubrir e.firmas.
    if opciones.recuperar_paquetes or opciones.solo_importar_manuales:
        return ContextoEjecucion(
            settings=settings,
            opciones=opciones,
            logger=logger,
            importador=importador,
            resumen=resumen,
            id_ejecucion=id_ejecucion,
            inicio_monotonic=inicio_monotonic,
        )

    repositorio_empresas = RepositorioEmpresasRetenciones(settings)
    descubrimiento = repositorio_empresas.descubrir_empresas()
    problemas = descubrimiento.problemas
    if opciones.rfc:
        problemas = tuple(
            problema
            for problema in problemas
            if _normalizar_rfc(problema.rfc) == opciones.rfc
        )

    for problema in problemas:
        logger.warning(
            "Empresa no disponible: id=%s, RFC=%s, código=%s, detalle=%s",
            problema.razon_social_id,
            problema.rfc,
            problema.codigo,
            problema.mensaje,
        )

    empresas = descubrimiento.empresas
    if opciones.rfc:
        empresas = tuple(
            empresa for empresa in empresas if empresa.rfc == opciones.rfc
        )
        if not empresas:
            detalle = (
                "está registrada, pero no está disponible"
                if problemas
                else "no existe o no está habilitada"
            )
            raise ErrorPreparacion(
                f"La empresa {opciones.rfc} {detalle} para el proceso."
            )

    repositorio_solicitudes = RepositorioSolicitudesRetenciones(settings)
    repetir_pronto = False
    if opciones.periodo_explicito or opciones.continuar_pendientes:
        periodo = _determinar_periodo(settings, opciones)
        empresas_en_proceso = tuple(
            EmpresaEnProceso(empresa, periodo=periodo)
            for empresa in empresas
        )
    else:
        registros = repositorio_solicitudes.listar_estado_planificacion(
            tuple(empresa.razon_social_id for empresa in empresas)
        )
        zona = ZoneInfo(settings.timezone_name)
        planificacion = planificar_empresas(
            empresas,
            registros,
            hoy=datetime.now(zona).date(),
            limite_historicos=settings.historical_companies,
        )
        empresas_en_proceso = tuple(
            EmpresaEnProceso(
                trabajo.empresa,
                periodo=(
                    trabajo.periodo.fecha_inicial,
                    trabajo.periodo.fecha_final,
                    trabajo.periodo.tipo_periodo,
                ),
            )
            for trabajo in planificacion.trabajos
        )
        repetir_pronto = planificacion.hay_trabajo
        logger.info(
            "[PLAN AUTOMÁTICO] históricos_pendientes=%d | "
            "históricos_activos=%d | diarias=%d | al_corriente=%d",
            planificacion.historicos_pendientes,
            planificacion.historicos_seleccionados,
            planificacion.diarias_seleccionadas,
            planificacion.empresas_al_corriente,
        )
        for trabajo in planificacion.trabajos:
            logger.info(
                "[PLAN][%s] %s | %s a %s | ancla_historico=%s",
                trabajo.empresa.rfc,
                trabajo.periodo.tipo_periodo.value,
                trabajo.periodo.fecha_inicial,
                trabajo.periodo.fecha_final,
                trabajo.ancla_historico,
            )

    resumen.empresas_disponibles = len(empresas_en_proceso)
    if not opciones.periodo_explicito and not opciones.continuar_pendientes:
        resumen.empresas_con_error = len(problemas)
    logger.info(
        "[CONTEXTO] ejecucion=%s | base=%s | modo=%s | RFC=%s | "
        "empresas_seleccionadas=%d | empresas_validas=%d",
        id_ejecucion,
        settings.base.database.database,
        _nombre_modo(opciones),
        opciones.rfc or "TODOS",
        len(empresas_en_proceso),
        len(empresas),
    )
    return ContextoEjecucion(
        settings=settings,
        opciones=opciones,
        logger=logger,
        importador=importador,
        resumen=resumen,
        empresas=empresas_en_proceso,
        repositorio_empresas=repositorio_empresas,
        repositorio_solicitudes=repositorio_solicitudes,
        repetir_pronto=repetir_pronto,
        id_ejecucion=id_ejecucion,
        inicio_monotonic=inicio_monotonic,
    )


def _ejecutar_como_servicio(
    settings: SatSettings,
    opciones: OpcionesEjecucion,
    flujo: FlujoRetenciones,
    logger: logging.Logger,
    notificador: NotificadorTelegram,
    evento_detencion: Event | None = None,
) -> int:
    detener = evento_detencion or Event()
    ciclos_ejecutados = 0
    ultimo_codigo = 0
    logger.info(
        "Modo servicio iniciado; ejecución diaria=%s | seguimiento=%d min | "
        "zona=%s",
        settings.daily_time,
        settings.followup_minutes,
        settings.timezone_name,
    )
    contexto_detencion = (
        _manejadores_detencion(detener, logger)
        if evento_detencion is None
        else nullcontext()
    )
    with contexto_detencion:
        while not detener.is_set():
            # --max-ciclos permite una prueba inmediata sin esperar las 02:00.
            if opciones.max_ciclos is None:
                segundos, programada = _segundos_hasta_siguiente_ejecucion(
                    settings
                )
                logger.info(
                    "Próxima jornada automática: %s (en %.1f hora(s))",
                    programada,
                    segundos / 3600,
                )
                if detener.wait(segundos):
                    break

            notificador.enviar(
                "🕑 Retenciones: inició la jornada automática.\n"
                f"Base: {settings.base.database.database}"
            )
            while not detener.is_set():
                resultado = _ejecutar_ciclo(
                    settings,
                    opciones,
                    flujo,
                    logger,
                )
                ultimo_codigo = resultado.codigo_salida
                ciclos_ejecutados += 1
                limite_alcanzado = (
                    opciones.max_ciclos is not None
                    and ciclos_ejecutados >= opciones.max_ciclos
                )
                _notificar_resultado_ciclo(
                    notificador,
                    resultado,
                    settings,
                    limite_alcanzado=limite_alcanzado,
                )

                if limite_alcanzado:
                    logger.info(
                        "Modo servicio finalizado al alcanzar %d ciclo(s)",
                        opciones.max_ciclos,
                    )
                    detener.set()
                    break

                if not resultado.repetir_pronto:
                    break

                logger.info(
                    "Hay trabajo pendiente; próxima revisión en %d minuto(s)",
                    settings.followup_minutes,
                )
                detener.wait(settings.followup_minutes * 60)
    logger.info("Modo servicio detenido de forma controlada")
    return ultimo_codigo


def _segundos_hasta_siguiente_ejecucion(
    settings: SatSettings,
    ahora: datetime | None = None,
) -> tuple[float, datetime]:
    zona = ZoneInfo(settings.timezone_name)
    momento = ahora or datetime.now(zona)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=zona)
    else:
        momento = momento.astimezone(zona)
    programada = datetime.combine(
        momento.date(),
        settings.daily_time,
        tzinfo=zona,
    )
    if programada <= momento:
        programada += timedelta(days=1)
    return max(0.0, (programada - momento).total_seconds()), programada


def _notificar_resultado_ciclo(
    notificador: NotificadorTelegram,
    resultado: ResultadoCiclo,
    settings: SatSettings,
    *,
    limite_alcanzado: bool,
) -> None:
    """Envía un resumen funcional; el detalle técnico permanece en el log."""

    if not notificador.habilitado:
        return

    resumen = resultado.resumen
    hay_error = resultado.codigo_salida != 0 or any(
        empresa.error is not None or empresa.resumen.tiene_errores
        for empresa in resultado.empresas
    )
    icono = "🚨" if hay_error else "✅"
    lineas = [
        f"{icono} Retenciones | ciclo terminado",
        f"Ejecución: {resultado.id_ejecucion}",
        f"Base: {settings.base.database.database}",
    ]

    if resultado.empresas:
        for estado in resultado.empresas:
            periodo = estado.periodo
            if periodo is None:
                tipo = "SEGUIMIENTO"
                fechas = "periodos pendientes"
            else:
                fecha_inicial, fecha_final, tipo_periodo = periodo
                tipo = tipo_periodo.value
                fechas = (
                    f"{fecha_inicial.date().isoformat()} → "
                    f"{fecha_final.date().isoformat()}"
                )
            empresa = estado.resumen
            errores_xml = (
                empresa.archivos_con_error
                + empresa.archivos_con_conflicto
            )
            incidencias_solicitudes = (
                empresa.solicitudes_rechazadas
                + empresa.solicitudes_con_error
            )
            estado_icono = (
                "🚨"
                if estado.error is not None or empresa.tiene_errores
                else "•"
            )
            lineas.extend(
                (
                    "",
                    f"{estado_icono} {estado.empresa.rfc} | {tipo}",
                    f"Periodo: {fechas}",
                    "SAT: "
                    f"{empresa.solicitudes_creadas} creadas, "
                    f"{empresa.solicitudes_reutilizadas} reutilizadas, "
                    f"{empresa.solicitudes_consultadas} consultadas",
                    "ZIP: "
                    f"{empresa.paquetes_descargados} descargados | "
                    f"{empresa.bytes_zip / (1024 * 1024):.3f} MiB",
                    "XML: "
                    f"{empresa.archivos_importados}/"
                    f"{empresa.archivos_xml} importados | "
                    f"duplicados={empresa.archivos_duplicados} | "
                    f"errores={errores_xml}",
                )
            )
            if estado.error:
                lineas.append(
                    "Error crítico: "
                    + " ".join(estado.error.split())[:250]
                )
            elif empresa.tiene_errores:
                lineas.append(
                    "Incidencias: "
                    f"solicitudes={incidencias_solicitudes}, "
                    f"paquetes={empresa.paquetes_con_error}, "
                    f"lotes={empresa.lotes_con_error}"
                )
    elif hay_error:
        lineas.append("No fue posible preparar o completar el ciclo.")
    else:
        lineas.append("No hay periodos pendientes en este momento.")

    if resumen is not None and resumen.empresas_con_error:
        lineas.append(
            f"Incidencias de empresas: {resumen.empresas_con_error}"
        )

    if limite_alcanzado:
        siguiente = "Servicio detenido: alcanzó --max-ciclos."
    elif resultado.repetir_pronto:
        siguiente = (
            "Trabajo pendiente: nueva revisión en "
            f"{settings.followup_minutes} minutos."
        )
    else:
        _, programada = _segundos_hasta_siguiente_ejecucion(settings)
        siguiente = (
            "Todo al corriente. Próxima ejecución: "
            f"{programada:%Y-%m-%d %H:%M}."
        )
    lineas.extend(("", siguiente))
    notificador.enviar("\n".join(lineas))


@contextmanager
def _manejadores_detencion(
    detener: Event,
    logger: logging.Logger,
) -> Iterator[None]:
    anteriores: dict[signal.Signals, object] = {}

    def solicitar_detencion(numero_senal: int, frame: object) -> None:
        del frame
        nombre = signal.Signals(numero_senal).name
        if not detener.is_set():
            logger.info("Se recibió %s; se solicitó detener el servicio", nombre)
        detener.set()

    senales = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        senales.append(signal.SIGBREAK)

    try:
        for senal in senales:
            anteriores[senal] = signal.getsignal(senal)
            signal.signal(senal, solicitar_detencion)
        yield
    finally:
        for senal, manejador in anteriores.items():
            signal.signal(senal, manejador)


def _determinar_periodo(
    settings: SatSettings,
    opciones: OpcionesEjecucion,
) -> tuple[datetime, datetime, TipoPeriodo] | None:
    del settings
    if opciones.continuar_pendientes:
        return None
    if opciones.periodo_explicito:
        assert opciones.fecha_inicial is not None
        assert opciones.fecha_final is not None
        return (
            datetime.combine(opciones.fecha_inicial, time.min),
            datetime.combine(opciones.fecha_final, time(23, 59, 59)),
            (
                TipoPeriodo.HISTORIAL
                if opciones.historico
                else TipoPeriodo.MANUAL
            ),
        )
    return None


def _registrar_resumen(
    resumen: ResumenCiclo,
    logger: logging.Logger,
    duracion_segundos: float,
    id_ejecucion: str,
) -> None:
    logger.info(
        "RESUMEN | ejecucion=%s, duracion=%.2fs, empresas=%d, "
        "empresas_con_error=%d, "
        "solicitudes_creadas=%d, solicitudes_reutilizadas=%d, "
        "solicitudes_rechazadas=%d, solicitudes_con_error=%d, "
        "solicitudes_consultadas=%d, paquetes_descargados=%d, "
        "paquetes_con_error=%d, bytes_zip=%d, mib_zip=%.3f, "
        "lotes_procesados=%d, lotes_con_error=%d, XML=%d, "
        "importados=%d, duplicados=%d, conflictos=%d, errores_xml=%d",
        id_ejecucion,
        duracion_segundos,
        resumen.empresas_disponibles,
        resumen.empresas_con_error,
        resumen.solicitudes_creadas,
        resumen.solicitudes_reutilizadas,
        resumen.solicitudes_rechazadas,
        resumen.solicitudes_con_error,
        resumen.solicitudes_consultadas,
        resumen.paquetes_descargados,
        resumen.paquetes_con_error,
        resumen.bytes_paquetes_disponibles,
        resumen.bytes_paquetes_disponibles / (1024 * 1024),
        resumen.lotes_procesados,
        resumen.lotes_con_error,
        resumen.archivos_xml,
        resumen.archivos_importados,
        resumen.archivos_duplicados,
        resumen.archivos_con_conflicto,
        resumen.archivos_con_error,
    )


def _crear_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Solicita, consulta, descarga e importa retenciones SAT."
    )
    parser.add_argument(
        "--servicio",
        action="store_true",
        help=(
            "Ejecuta diariamente a SAT_DAILY_TIME y continúa pendientes "
            "cada SAT_FOLLOWUP_MINUTES."
        ),
    )
    parser.add_argument(
        "--max-ciclos",
        type=_entero_positivo,
        help="Finaliza el servicio después de esta cantidad de ciclos.",
    )
    parser.add_argument(
        "--recuperar-paquetes",
        action="store_true",
        help="Reprocesa ZIP SAT archivados sin contactar al SAT.",
    )
    parser.add_argument(
        "--solo-validar-recuperacion",
        action="store_true",
        help="Muestra el plan de recuperación sin modificar SQL.",
    )
    parser.add_argument(
        "--solo-importar-manuales",
        action="store_true",
        help="Procesa únicamente los ZIP de la bandeja manual.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Solicita al SAT un intervalo explícito de fechas.",
    )
    parser.add_argument(
        "--historico",
        action="store_true",
        help=(
            "Solicita manualmente un solo año calendario y guarda "
            "tipo_periodo=HISTORIAL. No automatiza el histórico."
        ),
    )
    parser.add_argument(
        "--continuar-pendientes",
        action="store_true",
        help=(
            "No crea solicitudes; sólo consulta, descarga e importa las "
            "solicitudes RETENCIONES pendientes del RFC."
        ),
    )
    parser.add_argument("--fecha-inicial", type=_fecha_iso)
    parser.add_argument("--fecha-final", type=_fecha_iso)
    parser.add_argument(
        "--rfc",
        help="Limita el flujo SAT o la recuperación a un RFC.",
    )
    return parser


def _validar_opciones(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
) -> OpcionesEjecucion:
    periodo_explicito = bool(namespace.manual or namespace.historico)
    if namespace.manual and namespace.historico:
        parser.error("--manual y --historico son mutuamente excluyentes.")
    if namespace.servicio and periodo_explicito:
        parser.error("--servicio no puede combinarse con fechas explícitas.")
    if namespace.max_ciclos is not None and not namespace.servicio:
        parser.error("--max-ciclos solamente se permite con --servicio.")
    if periodo_explicito and (
        namespace.fecha_inicial is None or namespace.fecha_final is None
    ):
        parser.error(
            "--manual/--historico requiere --fecha-inicial y --fecha-final."
        )
    if not periodo_explicito and (
        namespace.fecha_inicial is not None or namespace.fecha_final is not None
    ):
        parser.error(
            "Las fechas explícitas requieren --manual o --historico."
        )
    if (
        namespace.fecha_inicial is not None
        and namespace.fecha_final is not None
        and namespace.fecha_final < namespace.fecha_inicial
    ):
        parser.error("--fecha-final no puede ser anterior a --fecha-inicial.")
    if namespace.historico and not namespace.rfc:
        parser.error("--historico requiere --rfc para limitarlo a una empresa.")
    if (
        namespace.historico
        and namespace.fecha_inicial is not None
        and namespace.fecha_final is not None
        and namespace.fecha_inicial.year != namespace.fecha_final.year
    ):
        parser.error(
            "Cada ejecución --historico debe permanecer dentro de un solo "
            "año calendario."
        )
    if namespace.continuar_pendientes and not namespace.rfc:
        parser.error("--continuar-pendientes requiere --rfc.")
    if namespace.continuar_pendientes and periodo_explicito:
        parser.error(
            "--continuar-pendientes no puede combinarse con --manual o "
            "--historico."
        )
    if namespace.solo_validar_recuperacion and not namespace.recuperar_paquetes:
        parser.error(
            "--solo-validar-recuperacion requiere --recuperar-paquetes."
        )
    if namespace.recuperar_paquetes and (
        namespace.servicio
        or namespace.manual
        or namespace.historico
        or namespace.continuar_pendientes
        or namespace.solo_importar_manuales
        or namespace.max_ciclos is not None
        or namespace.fecha_inicial is not None
        or namespace.fecha_final is not None
    ):
        parser.error(
            "--recuperar-paquetes no puede combinarse con servicio, fechas "
            "o importación manual exclusiva."
        )
    if namespace.solo_importar_manuales and (
        namespace.servicio
        or namespace.manual
        or namespace.historico
        or namespace.continuar_pendientes
        or namespace.max_ciclos is not None
        or namespace.rfc
    ):
        parser.error(
            "--solo-importar-manuales no puede combinarse con servicio, "
            "fechas, --max-ciclos o --rfc."
        )

    return OpcionesEjecucion(
        servicio=bool(namespace.servicio),
        max_ciclos=namespace.max_ciclos,
        recuperar_paquetes=bool(namespace.recuperar_paquetes),
        solo_validar_recuperacion=bool(namespace.solo_validar_recuperacion),
        solo_importar_manuales=bool(namespace.solo_importar_manuales),
        manual=bool(namespace.manual),
        historico=bool(namespace.historico),
        continuar_pendientes=bool(namespace.continuar_pendientes),
        fecha_inicial=namespace.fecha_inicial,
        fecha_final=namespace.fecha_final,
        rfc=_normalizar_rfc(namespace.rfc) if namespace.rfc else None,
    )


def _fecha_iso(valor: str) -> date:
    try:
        return date.fromisoformat(valor)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "La fecha debe usar el formato AAAA-MM-DD."
        ) from exc


def _entero_positivo(valor: str) -> int:
    try:
        numero = int(valor)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "El valor debe ser un número entero positivo."
        ) from exc
    if numero <= 0:
        raise argparse.ArgumentTypeError("El valor debe ser mayor que cero.")
    return numero


def _normalizar_rfc(valor: object) -> str:
    return "".join(str(valor or "").split()).upper()


def _nombre_modo(opciones: OpcionesEjecucion) -> str:
    if opciones.continuar_pendientes:
        return "CONTINUAR_PENDIENTES"
    if opciones.historico:
        return "HISTORICO_CONTROLADO"
    if opciones.manual:
        return "MANUAL"
    if opciones.recuperar_paquetes:
        return "RECUPERAR_PAQUETES"
    if opciones.solo_importar_manuales:
        return "IMPORTAR_MANUALES"
    return "AUTOMATICO_HISTORICO_DIARIO"


def _crear_directorios(settings: Settings) -> None:
    for directorio in settings.paths.all_directories():
        directorio.mkdir(parents=True, exist_ok=True)


def _configurar_logging(settings: Settings) -> logging.Logger:
    logger = logging.getLogger(NOMBRE_LOGGER)
    logger.setLevel(getattr(logging, settings.log_level))
    logger.propagate = False
    logger.handlers.clear()
    formato = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    consola = logging.StreamHandler()
    consola.setFormatter(formato)
    archivo = RotatingFileHandler(
        settings.paths.log_dir / NOMBRE_ARCHIVO_LOG,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    archivo.setFormatter(formato)
    logger.addHandler(consola)
    logger.addHandler(archivo)
    return logger
