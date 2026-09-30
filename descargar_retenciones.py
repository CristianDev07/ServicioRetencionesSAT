"""PASO 3: descargar los paquetes de retenciones disponibles.

Este archivo reserva cada paquete registrado en SQL, valida la respuesta ZIP y
la guarda en la bandeja SAT. No abre ni importa los XML.
"""

from auxiliares.control_ejecucion import ContextoEjecucion
from auxiliares.operaciones_sat import GestorDescargaPaquetesRetenciones


def descargar_retenciones(contexto: ContextoEjecucion) -> None:
    """Descarga paquetes pendientes de forma segura e idempotente."""

    contexto.logger.info("[PASO 3/4] DESCARGAR RETENCIONES")
    if not contexto.usa_flujo_sat:
        contexto.logger.info("[PASO 3/4] Omitido por el modo seleccionado")
        return

    assert contexto.repositorio_solicitudes is not None
    for estado in contexto.empresas:
        if not estado.disponible or estado.cliente is None:
            continue
        try:
            # Paso 3.1: elegir y reservar paquetes que aún no están cerrados.
            gestor = GestorDescargaPaquetesRetenciones(
                contexto.settings,
                contexto.repositorio_solicitudes,
                estado.cliente,
            )
            resultado = gestor.descargar_pendientes()

            # Paso 3.2: registrar el resultado; el ZIP todavía no se importa.
            contexto.resumen.paquetes_descargados += resultado.descargados
            contexto.resumen.paquetes_con_error += resultado.con_error
            bytes_disponibles = sum(
                item.paquete.tamanio_bytes or 0
                for item in resultado.resultados
                if item.paquete is not None
                and item.estado in {"DESCARGADO", "RECUPERADO_LOCAL"}
            )
            contexto.resumen.bytes_paquetes_disponibles += bytes_disponibles
            estado.resumen.paquetes_descargados += resultado.descargados
            estado.resumen.paquetes_con_error += resultado.con_error
            estado.resumen.bytes_zip += bytes_disponibles
            for detalle in resultado.resultados:
                paquete = detalle.paquete
                tamanio = paquete.tamanio_bytes if paquete else None
                contexto.logger.info(
                    "[DESCARGA][%s] paquete_sql_id=%s | sat_paquete=%s | "
                    "estado=%s | bytes=%s | mib=%s | sha256=%s | "
                    "ruta=%s | duracion=%.2fs | mensaje=%s",
                    estado.empresa.rfc,
                    detalle.paquete_id,
                    detalle.id_paquete_sat,
                    detalle.estado,
                    tamanio if tamanio is not None else "N/A",
                    f"{tamanio / (1024 * 1024):.3f}"
                    if tamanio is not None
                    else "N/A",
                    paquete.hash_sha256 if paquete and paquete.hash_sha256 else "N/A",
                    paquete.ruta_descarga if paquete and paquete.ruta_descarga else "N/A",
                    detalle.duracion_segundos,
                    _mensaje_log(detalle.mensaje),
                )
            contexto.logger.info(
                "[PASO 3/4][%s] total=%d, descargados=%d, recuperados=%d, "
                "con_error=%d, omitidos=%d, bytes=%d, mib=%.3f",
                estado.empresa.rfc,
                len(resultado.resultados),
                resultado.descargados,
                resultado.recuperados_localmente,
                resultado.con_error,
                resultado.omitidos,
                bytes_disponibles,
                bytes_disponibles / (1024 * 1024),
            )
        except Exception as exc:
            contexto.registrar_error_empresa(estado, "PASO 3/4", exc)


def _mensaje_log(valor: object, limite: int = 500) -> str:
    texto = " ".join(str(valor or "").split())
    if len(texto) <= limite:
        return texto or "N/A"
    return f"{texto[:limite]}..."
