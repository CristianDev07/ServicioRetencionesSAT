"""Punto de entrada: ejecuta los cuatro archivos principales en orden."""

from auxiliares.control_ejecucion import ContextoEjecucion, ejecutar_aplicacion
from consultar_retenciones import consultar_retenciones
from descargar_retenciones import descargar_retenciones
from importar_retenciones import importar_retenciones
from solicitar_retenciones import solicitar_retenciones


def ejecutar_flujo(contexto: ContextoEjecucion) -> None:
    """Llama cada paso; la lógica específica vive en su propio archivo."""

    solicitar_retenciones(contexto)   # Paso 1
    consultar_retenciones(contexto)   # Paso 2
    descargar_retenciones(contexto)   # Paso 3
    importar_retenciones(contexto)    # Paso 4


if __name__ == "__main__":
    raise SystemExit(ejecutar_aplicacion(ejecutar_flujo))
