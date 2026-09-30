"""Avisos breves del servicio sin intervenir en el flujo principal.

Paso 1: recibe un texto ya resumido por el controlador.
Paso 2: lo envía al chat configurado mediante la API de Telegram.
Paso 3: si Telegram falla, registra una advertencia y deja continuar al SAT.
"""

from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from auxiliares.configuracion import SatSettings


class NotificadorTelegram:
    def __init__(
        self,
        settings: SatSettings,
        logger: logging.Logger,
    ) -> None:
        self._enabled = settings.telegram_enabled
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._timeout = settings.telegram_timeout_seconds
        self._logger = logger

    @property
    def habilitado(self) -> bool:
        return self._enabled

    def enviar(self, mensaje: str) -> bool:
        if not self._enabled:
            return False

        texto = str(mensaje or "").strip()[:3900]
        if not texto:
            return False
        datos = urlencode(
            {"chat_id": self._chat_id, "text": texto}
        ).encode("utf-8")
        solicitud = Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=datos,
            method="POST",
        )
        try:
            with urlopen(solicitud, timeout=self._timeout) as respuesta:
                contenido = json.loads(respuesta.read().decode("utf-8"))
            if contenido.get("ok") is True:
                return True
            self._logger.warning(
                "Telegram rechazó el aviso del servicio: %s",
                contenido.get("description", "respuesta sin detalle"),
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            self._logger.warning(
                "No fue posible enviar el aviso a Telegram: %s",
                exc,
            )
        return False
