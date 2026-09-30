"""Decide el siguiente periodo automático de cada razón social.

Paso 1: reconstruye el avance usando únicamente las solicitudes RETENCIONES.
Paso 2: divide los últimos seis años en bloques anuales, de nuevo a antiguo.
Paso 3: al terminar el histórico, crea ventanas diarias de ayer y hoy.

No modifica SQL. La evidencia del avance permanece en las tablas que ya
existen: solicitud, paquete e importación.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol

from auxiliares.modelos_retenciones import EmpresaRetenciones, TipoPeriodo


class RegistroPlanificacion(Protocol):
    razon_social_id: int
    tipo_periodo: str
    tipo_descarga: str
    estado_comprobante: str
    fecha_inicial: datetime
    fecha_final: datetime
    estado: str
    sin_resultados: bool
    numero_cfdis: int | None
    total_paquetes: int
    paquetes_procesados: int


@dataclass(frozen=True, slots=True)
class PeriodoAutomatico:
    fecha_inicial: datetime
    fecha_final: datetime
    tipo_periodo: TipoPeriodo


@dataclass(frozen=True, slots=True)
class TrabajoEmpresa:
    empresa: EmpresaRetenciones
    periodo: PeriodoAutomatico
    ancla_historico: date


@dataclass(frozen=True, slots=True)
class ResultadoPlanificacion:
    trabajos: tuple[TrabajoEmpresa, ...]
    historicos_pendientes: int
    historicos_seleccionados: int
    diarias_seleccionadas: int
    empresas_al_corriente: int

    @property
    def hay_trabajo(self) -> bool:
        return bool(self.trabajos)


_FLUJOS = (
    ("EMITIDAS", "VIGENTE"),
    ("RECIBIDAS", "VIGENTE"),
    ("EMITIDAS", "CANCELADO"),
)


def planificar_empresas(
    empresas: tuple[EmpresaRetenciones, ...],
    registros: tuple[RegistroPlanificacion, ...],
    *,
    hoy: date,
    limite_historicos: int,
) -> ResultadoPlanificacion:
    """Selecciona todas las diarias y una cola limitada de históricos."""

    por_empresa: dict[int, list[RegistroPlanificacion]] = defaultdict(
        list
    )
    for registro in registros:
        por_empresa[registro.razon_social_id].append(registro)

    historicos: list[tuple[bool, TrabajoEmpresa]] = []
    diarios: list[TrabajoEmpresa] = []
    al_corriente = 0

    for empresa in sorted(empresas, key=lambda item: item.razon_social_id):
        avance = tuple(por_empresa.get(empresa.razon_social_id, ()))
        ancla = _obtener_ancla_historico(avance, hoy)
        periodo_historico = _primer_historico_pendiente(avance, ancla)
        if periodo_historico is not None:
            ya_iniciado = any(
                item.tipo_periodo == TipoPeriodo.HISTORIAL.value
                for item in avance
            )
            historicos.append(
                (
                    ya_iniciado,
                    TrabajoEmpresa(empresa, periodo_historico, ancla),
                )
            )
            continue

        periodo_diario = _primera_diaria_pendiente(avance, ancla, hoy)
        if periodo_diario is None:
            al_corriente += 1
        else:
            diarios.append(TrabajoEmpresa(empresa, periodo_diario, ancla))

    # Las empresas cuyo histórico ya comenzó conservan primero su lugar.
    historicos.sort(
        key=lambda item: (
            not item[0],
            item[1].empresa.razon_social_id,
        )
    )
    seleccion_historica = tuple(
        item[1] for item in historicos[:limite_historicos]
    )
    trabajos = tuple(diarios) + seleccion_historica
    return ResultadoPlanificacion(
        trabajos=trabajos,
        historicos_pendientes=len(historicos),
        historicos_seleccionados=len(seleccion_historica),
        diarias_seleccionadas=len(diarios),
        empresas_al_corriente=al_corriente,
    )


def _obtener_ancla_historico(
    registros: tuple[RegistroPlanificacion, ...],
    hoy: date,
) -> date:
    """Recupera la fecha fijada al iniciar el histórico o usa hoy."""

    fechas = [
        item.fecha_final.date()
        for item in registros
        if item.tipo_periodo == TipoPeriodo.HISTORIAL.value
        and item.fecha_final.year == hoy.year
        and item.fecha_final.date() <= hoy
    ]
    return max(fechas) if fechas else hoy


def _primer_historico_pendiente(
    registros: tuple[RegistroPlanificacion, ...],
    ancla: date,
) -> PeriodoAutomatico | None:
    for fecha_inicial, fecha_final in _bloques_historicos(ancla):
        periodo = PeriodoAutomatico(
            fecha_inicial=_inicio_dia(fecha_inicial),
            fecha_final=_fin_dia(fecha_final),
            tipo_periodo=TipoPeriodo.HISTORIAL,
        )
        if not _periodo_completo(registros, periodo):
            return periodo
    return None


def _primera_diaria_pendiente(
    registros: tuple[RegistroPlanificacion, ...],
    ancla: date,
    hoy: date,
) -> PeriodoAutomatico | None:
    fecha_ejecucion = ancla + timedelta(days=1)
    while fecha_ejecucion <= hoy:
        periodo = PeriodoAutomatico(
            fecha_inicial=_inicio_dia(fecha_ejecucion - timedelta(days=1)),
            fecha_final=_fin_dia(fecha_ejecucion),
            tipo_periodo=TipoPeriodo.DIARIA,
        )
        if not _periodo_completo(registros, periodo):
            return periodo
        fecha_ejecucion += timedelta(days=1)
    return None


def _bloques_historicos(ancla: date) -> tuple[tuple[date, date], ...]:
    fecha_minima = _restar_anios(ancla, 6)
    bloques: list[tuple[date, date]] = []
    for anio in range(ancla.year, fecha_minima.year - 1, -1):
        inicio = date(anio, 1, 1)
        fin = date(anio, 12, 31)
        if anio == ancla.year:
            fin = ancla
        if anio == fecha_minima.year:
            inicio = fecha_minima
        bloques.append((inicio, fin))
    return tuple(bloques)


def _periodo_completo(
    registros: tuple[RegistroPlanificacion, ...],
    periodo: PeriodoAutomatico,
) -> bool:
    for tipo_descarga, estado_comprobante in _FLUJOS:
        solicitudes = (
            item
            for item in registros
            if item.tipo_periodo == periodo.tipo_periodo.value
            and item.tipo_descarga == tipo_descarga
            and item.estado_comprobante == estado_comprobante
            and _misma_fecha(item.fecha_inicial, periodo.fecha_inicial)
            and _misma_fecha(item.fecha_final, periodo.fecha_final)
        )
        if not any(_solicitud_completa(item) for item in solicitudes):
            return False
    return True


def _solicitud_completa(item: RegistroPlanificacion) -> bool:
    if item.sin_resultados or item.estado == "COMPLETADA_SIN_RESULTADOS":
        return True
    if item.estado != "COMPLETADA":
        return False
    if item.total_paquetes == 0:
        return item.numero_cfdis == 0
    return item.paquetes_procesados == item.total_paquetes


def _restar_anios(value: date, cantidad: int) -> date:
    try:
        return value.replace(year=value.year - cantidad)
    except ValueError:
        # El 29 de febrero se convierte en el último día de febrero.
        return value.replace(year=value.year - cantidad, day=28)


def _inicio_dia(value: date) -> datetime:
    return datetime.combine(value, time.min)


def _fin_dia(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59))


def _misma_fecha(izquierda: datetime, derecha: datetime) -> bool:
    return izquierda.replace(tzinfo=None, microsecond=0) == derecha.replace(
        tzinfo=None,
        microsecond=0,
    )
