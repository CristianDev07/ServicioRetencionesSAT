"""PASO 2: consultar solicitudes pendientes de retenciones.

Este archivo toma las sesiones preparadas en el paso 1, consulta una vez cada
solicitud activa y registra en SQL sus estados y paquetes disponibles.
"""

from auxiliares.control_ejecucion import ContextoEjecucion


def consultar_retenciones(contexto: ContextoEjecucion) -> None:
    """Consulta solicitudes activas sin volver a crearlas."""

    contexto.logger.info("[PASO 2/4] CONSULTAR RETENCIONES")
    if not contexto.usa_flujo_sat:
        contexto.logger.info("[PASO 2/4] Omitido por el modo seleccionado")
        return

    for estado in contexto.empresas:
        if not estado.disponible or estado.gestor_solicitudes is None:
            continue
        try:
            # Paso 2.1: traer de SQL únicamente solicitudes todavía activas.
            resultado = (
                estado.gestor_solicitudes.consultar_pendientes_una_vez()
            )

            # Paso 2.2: acumular el estado; los paquetes quedan registrados
            # por el repositorio dentro de la misma operación de consulta.
            contexto.resumen.solicitudes_consultadas += len(
                resultado.resultados
            )
            contexto.resumen.solicitudes_con_error += resultado.con_error
            estado.resumen.solicitudes_consultadas += len(
                resultado.resultados
            )
            estado.resumen.solicitudes_con_error += resultado.con_error
            for detalle in resultado.resultados:
                solicitud = detalle.solicitud
                contexto.logger.info(
                    "[CONSULTA][%s][%s/%s] estado=%s | sql_id=%s | "
                    "sat_id=%s | estado_sat=%s | codigo_estado=%s | "
                    "cfdis=%s | paquetes=%d | intento=%s | "
                    "duracion=%.2fs | mensaje=%s",
                    estado.empresa.rfc,
                    detalle.criterio.tipo_descarga.value,
                    detalle.criterio.estado_comprobante.value,
                    detalle.estado,
                    solicitud.id if solicitud else "N/A",
                    (
                        solicitud.id_solicitud_sat
                        if solicitud and solicitud.id_solicitud_sat
                        else "N/A"
                    ),
                    (
                        solicitud.estado_solicitud_sat
                        if solicitud
                        and solicitud.estado_solicitud_sat is not None
                        else "N/A"
                    ),
                    (
                        solicitud.codigo_estado_solicitud
                        if solicitud and solicitud.codigo_estado_solicitud
                        else "N/A"
                    ),
                    (
                        solicitud.numero_cfdis
                        if solicitud and solicitud.numero_cfdis is not None
                        else "N/A"
                    ),
                    len(solicitud.ids_paquetes) if solicitud else 0,
                    solicitud.intentos_consulta if solicitud else "N/A",
                    detalle.duracion_segundos,
                    _mensaje_log(detalle.mensaje),
                )
            contexto.logger.info(
                "[PASO 2/4][%s] total=%d, en_proceso=%d, completadas=%d, "
                "sin_resultados=%d, con_error=%d",
                estado.empresa.rfc,
                len(resultado.resultados),
                resultado.en_proceso,
                resultado.completadas,
                resultado.sin_resultados,
                resultado.con_error,
            )
        except Exception as exc:
            contexto.registrar_error_empresa(estado, "PASO 2/4", exc)


def _mensaje_log(valor: object, limite: int = 500) -> str:
    texto = " ".join(str(valor or "").split())
    if len(texto) <= limite:
        return texto or "N/A"
    return f"{texto[:limite]}..."
