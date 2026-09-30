"""PASO 1: solicitar retenciones al SAT.

Este archivo sólo prepara el cliente de cada empresa, calcula/reutiliza los
criterios del periodo y crea las solicitudes faltantes. No consulta estados,
no descarga paquetes y no importa XML.
"""

from auxiliares.cliente_sat import ClienteSatRetenciones
from auxiliares.control_ejecucion import ContextoEjecucion
from auxiliares.operaciones_sat import GestorSolicitudesRetenciones


def solicitar_retenciones(contexto: ContextoEjecucion) -> None:
    """Crea o reutiliza las solicitudes requeridas para cada empresa."""

    contexto.logger.info("[PASO 1/4] SOLICITAR RETENCIONES")
    if not contexto.usa_flujo_sat:
        contexto.logger.info("[PASO 1/4] Omitido por el modo seleccionado")
        return

    assert contexto.repositorio_empresas is not None
    assert contexto.repositorio_solicitudes is not None
    for numero, estado in enumerate(contexto.empresas, start=1):
        empresa = estado.empresa
        contexto.logger.info(
            "[PASO 1/4][EMPRESA %d/%d] %s (%s)",
            numero,
            len(contexto.empresas),
            empresa.razon_social,
            empresa.rfc,
        )
        try:
            # Paso 1.1: preparar una sesión SAT exclusiva para esta empresa.
            contrasena = contexto.repositorio_empresas.descifrar_contrasena(
                empresa.fiel
            )
            cliente = ClienteSatRetenciones(
                contexto.settings,
                empresa,
                contrasena,
                logger=contexto.logger,
            )
            del contrasena
            gestor = GestorSolicitudesRetenciones(
                contexto.repositorio_solicitudes,
                cliente,
            )
            estado.cliente = cliente
            estado.gestor_solicitudes = gestor

            # Paso 1.2: el modo de seguimiento prepara autenticación, pero no
            # vuelve a crear solicitudes SAT.
            if not contexto.crea_solicitudes:
                contexto.logger.info(
                    "[PASO 1/4][%s] Modo seguimiento: no se crearán "
                    "solicitudes nuevas",
                    empresa.rfc,
                )
                continue

            # Paso 1.3: cada empresa recibe un único periodo del planificador.
            # Las banderas de razón social pertenecen al servicio de CFDI y
            # no intervienen en este flujo de retenciones.
            assert estado.periodo is not None
            fecha_inicial, fecha_final, tipo_periodo = estado.periodo
            contexto.logger.info(
                "[PASO 1/4] servicio_sat=RETENCIONES | periodo=%s | "
                "desde=%s | hasta=%s | solicitudes_esperadas=3",
                tipo_periodo.value,
                fecha_inicial,
                fecha_final,
            )
            plan = gestor.planificar(
                fecha_inicial,
                fecha_final,
                tipo_periodo=tipo_periodo,
            )
            resultado = gestor.crear_faltantes(plan)
            contexto.resumen.solicitudes_creadas += resultado.creadas
            contexto.resumen.solicitudes_reutilizadas += resultado.reutilizadas
            contexto.resumen.solicitudes_rechazadas += resultado.rechazadas
            contexto.resumen.solicitudes_con_error += (
                resultado.errores_comunicacion
            )
            estado.resumen.solicitudes_creadas += resultado.creadas
            estado.resumen.solicitudes_reutilizadas += resultado.reutilizadas
            estado.resumen.solicitudes_rechazadas += resultado.rechazadas
            estado.resumen.solicitudes_con_error += (
                resultado.errores_comunicacion
            )
            for detalle in resultado.resultados:
                solicitud = detalle.solicitud
                contexto.logger.info(
                    "[SOLICITUD][%s][%s/%s] estado=%s | sql_id=%s | "
                    "sat_id=%s | servicio=%s | fecha_solicitud=%s | "
                    "padre_id=%s | turno_diario=%s | codigo=%s | "
                    "duracion=%.2fs | mensaje=%s",
                    empresa.rfc,
                    detalle.criterio.tipo_descarga.value,
                    detalle.criterio.estado_comprobante.value,
                    detalle.estado,
                    solicitud.id if solicitud else "N/A",
                    (
                        solicitud.id_solicitud_sat
                        if solicitud and solicitud.id_solicitud_sat
                        else "N/A"
                    ),
                    solicitud.servicio_sat if solicitud else "RETENCIONES",
                    (
                        solicitud.fecha_solicitud
                        if solicitud and solicitud.fecha_solicitud
                        else "N/A"
                    ),
                    (
                        solicitud.solicitud_padre_id
                        if solicitud and solicitud.solicitud_padre_id
                        else "N/A"
                    ),
                    (
                        solicitud.turno_diario
                        if solicitud and solicitud.turno_diario is not None
                        else "N/A"
                    ),
                    detalle.codigo_estatus or "N/A",
                    detalle.duracion_segundos,
                    _mensaje_log(detalle.mensaje),
                )
            contexto.logger.info(
                "[PASO 1/4] creadas=%d, reutilizadas=%d, rechazadas=%d, "
                "errores=%d",
                resultado.creadas,
                resultado.reutilizadas,
                resultado.rechazadas,
                resultado.errores_comunicacion,
            )
        except Exception as exc:
            contexto.registrar_error_empresa(estado, "PASO 1/4", exc)


def _mensaje_log(valor: object, limite: int = 500) -> str:
    texto = " ".join(str(valor or "").split())
    if len(texto) <= limite:
        return texto or "N/A"
    return f"{texto[:limite]}..."
