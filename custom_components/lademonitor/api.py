"""API-Client für den Lademonitor-Server.

Kapselt das Bearer-Token-Auth-Modell des Servers (siehe backend/app/auth.py
im Lademonitor-Server-Repo) so, dass HA-seitig nie ein Token von Hand
gehandhabt werden muss: async_login() holt sich einmalig ein Token, jeder
Request hängt es automatisch an, und ein 401 (z.B. weil der Admin das Token
serverseitig widerrufen hat) löst einen einmaligen automatischen Re-Login
statt eines Fehlers beim Nutzer aus.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class LademonitorAuthError(Exception):
    """Login fehlgeschlagen - Nutzername/Passwort falsch oder Re-Login nicht möglich."""


class LademonitorConnectionError(Exception):
    """Server nicht erreichbar (Netzwerkfehler, falsche base_url, o.ä.)."""


async def _error_detail(resp: aiohttp.ClientResponse) -> str:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return resp.reason or ""
    if isinstance(body, dict) and body.get("detail"):
        return str(body["detail"])
    return resp.reason or ""


class LademonitorApiError(LademonitorConnectionError):
    """Server erreichbar, lehnt die Anfrage aber ab (HTTP 4xx/5xx)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_temporary(err: Exception) -> bool:
    """Lohnt sich ein spaeterer neuer Versuch?

    Ja bei Netzwerkfehlern, HTTP 5xx (z.B. Server laeuft, aber seine
    Datenbank nicht), 408/429 und Anmeldefehlern (nach einer Neuanmeldung in
    HA geht es wieder). Nein bei den uebrigen 4xx - ein unbekanntes Fahrzeug
    oder ungueltige Werte werden beim naechsten Versuch genauso abgelehnt."""
    if isinstance(err, LademonitorAuthError):
        return True
    if isinstance(err, LademonitorApiError):
        return err.status is None or err.status >= 500 or err.status in (408, 429)
    return isinstance(err, LademonitorConnectionError)


class LademonitorApiClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str,
        password: str,
        token: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self.token = token

    async def async_login(self) -> str:
        """Loggt sich ein und speichert das zurückgegebene Token. Tokens laufen
        serverseitig bewusst nicht ab (siehe Lademonitor-Server/CLAUDE.md) -
        ein erneuter Login ist daher nur nötig, wenn der Admin das Token
        widerrufen hat (Logout/Nutzer gelöscht)."""
        try:
            async with self._session.post(
                f"{self._base_url}/api/auth/login",
                json={"username": self._username, "password": self._password},
            ) as resp:
                if resp.status == 401:
                    raise LademonitorAuthError("Nutzername oder Passwort falsch")
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise LademonitorConnectionError(str(err)) from err

        self.token = data["token"]
        return self.token

    async def _request(self, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> Any:
        if not self.token:
            await self.async_login()
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with self._session.request(
                method, f"{self._base_url}{path}", headers=headers, **kwargs
            ) as resp:
                if resp.status == 401 and retry:
                    _LOGGER.debug("Token ungültig, versuche automatischen Re-Login")
                    await self.async_login()
                    return await self._request(method, path, retry=False, **kwargs)
                if resp.status == 401:
                    raise LademonitorAuthError("Auch Re-Login fehlgeschlagen - Zugangsdaten prüfen")
                if resp.status >= 400:
                    # Die Begruendung des Servers (FastAPI: {"detail": ...}),
                    # z.B. "Fahrzeug nicht gefunden", landet sonst nirgends -
                    # sie steht dann in der Fehler-Benachrichtigung.
                    raise LademonitorApiError(
                        f"HTTP {resp.status}: {await _error_detail(resp)}", resp.status
                    )
                if resp.status == 204:
                    return None
                return await resp.json()
        except aiohttp.ClientError as err:
            raise LademonitorConnectionError(str(err)) from err

    async def async_get_vehicles(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/vehicles")

    async def async_get_stats_summary(self, vehicle_id: str) -> dict[str, Any]:
        return await self._request("GET", "/api/stats/summary", params={"vehicle_id": vehicle_id})

    async def async_push_charging_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/sessions/auto", json=payload)
