"""DataUpdateCoordinator für Lademonitor - pollt GET /api/stats/summary pro
Fahrzeug (siehe backend/app/routers/stats.py im Server-Repo)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LademonitorApiClient, LademonitorAuthError, LademonitorConnectionError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class LademonitorCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Hält pro vehicle_id die zuletzt abgerufene StatsSummary."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: LademonitorApiClient,
        vehicle_ids: list[str],
        update_interval: timedelta,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self._client = client
        self._vehicle_ids = vehicle_ids

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return {
                vehicle_id: await self._client.async_get_stats_summary(vehicle_id)
                for vehicle_id in self._vehicle_ids
            }
        except LademonitorAuthError as err:
            # api.py hat bereits einen automatischen Re-Login versucht - das
            # schlägt nur fehl, wenn sich auch das gespeicherte Passwort nicht
            # mehr einloggen lässt. Löst den HA-Reauth-Flow aus (siehe
            # config_flow.py::async_step_reauth).
            raise ConfigEntryAuthFailed(str(err)) from err
        except LademonitorConnectionError as err:
            raise UpdateFailed(str(err)) from err
