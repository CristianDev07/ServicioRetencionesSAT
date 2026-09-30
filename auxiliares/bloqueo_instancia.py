"""Bloqueo exclusivo para evitar dos ejecuciones simultáneas.

Paso 1: abre el archivo de control dentro de la carpeta operativa.
Paso 2: intenta adquirir un bloqueo no bloqueante del sistema operativo.
Paso 3: libera el recurso al terminar, incluso cuando ocurre un error.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class InstanciaRetencionesActivaError(RuntimeError):
    """Indica que otro proceso ya conserva el bloqueo de ejecución."""


class BloqueoInstanciaRetenciones:
    """Bloqueo no bloqueante para impedir dos procesos simultáneos.

    La presencia del archivo no significa que el proceso siga activo: la
    exclusión depende únicamente del bloqueo del sistema operativo. Si el
    proceso se cierra de forma inesperada, Windows lo libera automáticamente.
    """

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta.resolve()
        self._archivo: BinaryIO | None = None

    def __enter__(self) -> BloqueoInstanciaRetenciones:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        archivo = self.ruta.open("a+b", buffering=0)
        try:
            _bloquear_archivo(archivo)
        except OSError as exc:
            archivo.close()
            raise InstanciaRetencionesActivaError(
                "Ya existe otra ejecución activa del proceso de retenciones."
            ) from exc

        self._archivo = archivo
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        archivo = self._archivo
        self._archivo = None
        if archivo is None:
            return
        try:
            _desbloquear_archivo(archivo)
        finally:
            archivo.close()


if os.name == "nt":
    import msvcrt

    def _bloquear_archivo(archivo: BinaryIO) -> None:
        archivo.seek(0)
        if archivo.read(1) == b"":
            archivo.seek(0)
            archivo.write(b"\0")
            archivo.flush()
        archivo.seek(0)
        msvcrt.locking(archivo.fileno(), msvcrt.LK_NBLCK, 1)

    def _desbloquear_archivo(archivo: BinaryIO) -> None:
        archivo.seek(0)
        msvcrt.locking(archivo.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _bloquear_archivo(archivo: BinaryIO) -> None:
        fcntl.flock(archivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _desbloquear_archivo(archivo: BinaryIO) -> None:
        fcntl.flock(archivo.fileno(), fcntl.LOCK_UN)
