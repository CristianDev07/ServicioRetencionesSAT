"""PASO 4: importar XML de retenciones en SQL Server.

Este archivo procesa los paquetes descargados, la bandeja manual o los ZIP SAT
archivados, según el modo elegido. La lectura XML y la persistencia detallada se
delegan a ``auxiliares/importador_archivos.py``.
"""

from collections.abc import Iterable
from time import perf_counter

from auxiliares.control_ejecucion import ContextoEjecucion, EmpresaEnProceso
from auxiliares.importador_archivos import ResultadoImportacion


def importar_retenciones(contexto: ContextoEjecucion) -> None:
    """Importa los lotes que correspondan al modo de ejecución."""

    contexto.logger.info("[PASO 4/4] IMPORTAR RETENCIONES")

    # Paso 4.A: recuperación controlada de paquetes SAT ya archivados.
    if contexto.opciones.recuperar_paquetes:
        _recuperar_paquetes(contexto)
        return

    # Paso 4.B: importar paquetes SAT descargados, empresa por empresa.
    if contexto.usa_flujo_sat:
        assert contexto.repositorio_solicitudes is not None
        for estado in contexto.empresas:
            if not estado.disponible:
                continue
            try:
                inicio = perf_counter()
                resultados = contexto.importador.procesar_paquetes_sat(
                    contexto.repositorio_solicitudes,
                    razon_social_id=estado.empresa.razon_social_id,
                )
                _acumular_resultados(
                    contexto,
                    resultados,
                    empresa_en_proceso=estado,
                )
                contexto.logger.info(
                    "[PASO 4/4][%s] paquetes SAT procesados=%d | "
                    "duracion=%.2fs",
                    estado.empresa.rfc,
                    len(resultados),
                    perf_counter() - inicio,
                )
            except Exception as exc:
                contexto.registrar_error_empresa(estado, "PASO 4/4", exc)

    # Paso 4.C: conservar el importador manual. En el flujo normal se revisa
    # al final; con --solo-importar-manuales es el único trabajo ejecutado.
    if contexto.opciones.continuar_pendientes:
        contexto.logger.info(
            "[PASO 4/4][MANUAL] Omitido durante el seguimiento de pendientes"
        )
        return

    try:
        inicio = perf_counter()
        resultados_manuales = contexto.importador.procesar_pendientes()
        _acumular_resultados(contexto, resultados_manuales)
        contexto.logger.info(
            "[PASO 4/4][MANUAL] lotes procesados=%d | duracion=%.2fs",
            len(resultados_manuales),
            perf_counter() - inicio,
        )
    except Exception:
        contexto.resumen.lotes_con_error += 1
        contexto.logger.exception("No fue posible procesar los ZIP manuales")


def _recuperar_paquetes(contexto: ContextoEjecucion) -> None:
    try:
        paquetes = contexto.importador.descubrir_paquetes_archivados(
            rfc=contexto.opciones.rfc
        )
    except Exception:
        contexto.logger.exception("No fue posible validar los paquetes archivados")
        contexto.codigo_salida_forzado = 2
        return

    if not paquetes:
        contexto.logger.error("No se encontraron paquetes SAT para recuperar")
        contexto.codigo_salida_forzado = 2
        return

    contexto.logger.info(
        "[RECUPERACIÓN/PLAN] paquetes=%d, XML=%d, RFC=%s",
        len(paquetes),
        sum(paquete.total_xml for paquete in paquetes),
        contexto.opciones.rfc or "TODOS",
    )
    for numero, paquete in enumerate(paquetes, start=1):
        contexto.logger.info(
            "[RECUPERACIÓN/PLAN %d/%d] %s | %s/%s | periodo=%s | XML=%d",
            numero,
            len(paquetes),
            paquete.rfc,
            paquete.tipo_descarga,
            paquete.estado_comprobante,
            paquete.periodo,
            paquete.total_xml,
        )

    if contexto.opciones.solo_validar_recuperacion:
        contexto.logger.info("Validación terminada; no se modificaron SQL ni ZIP")
        contexto.codigo_salida_forzado = 0
        return

    resultados = tuple(
        contexto.importador.procesar_paquete_archivado(paquete)
        for paquete in paquetes
    )
    _acumular_resultados(contexto, resultados)


def _acumular_resultados(
    contexto: ContextoEjecucion,
    resultados: Iterable[ResultadoImportacion],
    *,
    empresa_en_proceso: EmpresaEnProceso | None = None,
) -> None:
    items = tuple(resultados)
    contexto.resumen.lotes_procesados += len(items)
    contexto.resumen.lotes_con_error += sum(
        resultado.estado != "COMPLETADO" for resultado in items
    )
    contexto.resumen.archivos_xml += sum(
        resultado.total_archivos for resultado in items
    )
    contexto.resumen.archivos_importados += sum(
        resultado.archivos_importados for resultado in items
    )
    contexto.resumen.archivos_duplicados += sum(
        resultado.archivos_duplicados for resultado in items
    )
    contexto.resumen.archivos_con_conflicto += sum(
        resultado.archivos_con_conflicto for resultado in items
    )
    contexto.resumen.archivos_con_error += sum(
        resultado.archivos_con_error for resultado in items
    )
    if empresa_en_proceso is not None:
        resumen = empresa_en_proceso.resumen
        resumen.lotes_procesados += len(items)
        resumen.lotes_con_error += sum(
            resultado.estado != "COMPLETADO" for resultado in items
        )
        resumen.archivos_xml += sum(
            resultado.total_archivos for resultado in items
        )
        resumen.archivos_importados += sum(
            resultado.archivos_importados for resultado in items
        )
        resumen.archivos_duplicados += sum(
            resultado.archivos_duplicados for resultado in items
        )
        resumen.archivos_con_conflicto += sum(
            resultado.archivos_con_conflicto for resultado in items
        )
        resumen.archivos_con_error += sum(
            resultado.archivos_con_error for resultado in items
        )
    for resultado in items:
        contexto.logger.info(
            "[IMPORTACION] zip=%s | importacion_id=%s | lote=%s | "
            "estado=%s | XML=%d | importados=%d | duplicados=%d | "
            "conflictos=%d | errores=%d | paquete_sql_id=%s | mensaje=%s",
            resultado.ruta_zip.name,
            resultado.importacion_id or "N/A",
            resultado.lote_uuid or "N/A",
            resultado.estado,
            resultado.total_archivos,
            resultado.archivos_importados,
            resultado.archivos_duplicados,
            resultado.archivos_con_conflicto,
            resultado.archivos_con_error,
            resultado.solicitud_sat_paquete_id or "N/A",
            " ".join(str(resultado.mensaje or "N/A").split())[:500],
        )
