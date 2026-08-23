"""Sensor-Plattform für Lademonitor - ein HA-Device pro Fahrzeug, Werte aus
GET /api/stats/summary (siehe schemas.StatsSummary im Server-Repo)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LademonitorCoordinator


@dataclass(frozen=True, kw_only=True)
class LademonitorSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any] = lambda data: None


# Feldnamen entsprechen 1:1 schemas.StatsSummary im Server-Repo. Kein
# device_class=MONETARY-Unit-Zwang auf "€" - der Server kennt keine Währung,
# das Projekt ist auf den deutschsprachigen/EUR-Kontext des Nutzers
# zugeschnitten (siehe README des Server-Repos).
SENSOR_DESCRIPTIONS: tuple[LademonitorSensorDescription, ...] = (
    LademonitorSensorDescription(
        key="total_cost",
        translation_key="total_cost",
        native_unit_of_measurement="€",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("total_cost"),
    ),
    LademonitorSensorDescription(
        key="total_kwh",
        translation_key="total_kwh",
        native_unit_of_measurement="kWh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("total_kwh"),
    ),
    LademonitorSensorDescription(
        key="avg_price_per_kwh",
        translation_key="avg_price_per_kwh",
        native_unit_of_measurement="€/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("avg_price_per_kwh"),
    ),
    LademonitorSensorDescription(
        key="avg_consumption_kwh_per_100km",
        translation_key="avg_consumption_kwh_per_100km",
        native_unit_of_measurement="kWh/100km",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("avg_consumption_kwh_per_100km"),
    ),
    LademonitorSensorDescription(
        key="price_per_100km",
        translation_key="price_per_100km",
        native_unit_of_measurement="€/100km",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("price_per_100km"),
    ),
    LademonitorSensorDescription(
        key="total_km_driven",
        translation_key="total_km_driven",
        native_unit_of_measurement="km",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("total_km_driven"),
    ),
    LademonitorSensorDescription(
        key="ac_share_pct",
        translation_key="ac_share_pct",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("ac_share_pct"),
    ),
    LademonitorSensorDescription(
        key="dc_share_pct",
        translation_key="dc_share_pct",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("dc_share_pct"),
    ),
    LademonitorSensorDescription(
        key="total_sessions",
        translation_key="total_sessions",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("total_sessions"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: LademonitorCoordinator = stored["coordinator"]
    vehicles: list[dict[str, Any]] = stored["vehicles"]

    async_add_entities(
        LademonitorSensor(coordinator, vehicle, description)
        for vehicle in vehicles
        for description in SENSOR_DESCRIPTIONS
    )


class LademonitorSensor(CoordinatorEntity[LademonitorCoordinator], SensorEntity):
    entity_description: LademonitorSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LademonitorCoordinator,
        vehicle: dict[str, Any],
        description: LademonitorSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._vehicle_id = vehicle["id"]
        self._attr_unique_id = f"{vehicle['id']}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vehicle["id"])},
            name=vehicle["name"],
            manufacturer=vehicle.get("brand"),
            model=vehicle.get("model"),
        )

    @property
    def native_value(self) -> Any:
        vehicle_data = self.coordinator.data.get(self._vehicle_id)
        if not vehicle_data:
            return None
        return self.entity_description.value_fn(vehicle_data)
