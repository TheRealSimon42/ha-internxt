"""Storage sensors for Internxt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import InternxtConfigEntry
from .const import DOMAIN
from .coordinator import InternxtStorageCoordinator, StorageInfo

PARALLEL_UPDATES = 0  # all sensors read from one coordinator


@dataclass(frozen=True, kw_only=True)
class InternxtSensorDescription(SensorEntityDescription):
    """Describes an Internxt storage sensor."""

    value_fn: Callable[[StorageInfo], float]


def _bytes_sensor(
    key: str, value_fn: Callable[[StorageInfo], float]
) -> InternxtSensorDescription:
    return InternxtSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        value_fn=value_fn,
    )


SENSORS: tuple[InternxtSensorDescription, ...] = (
    _bytes_sensor("storage_used", lambda s: s.used),
    _bytes_sensor("storage_free", lambda s: s.free),
    _bytes_sensor("storage_total", lambda s: s.total),
    InternxtSensorDescription(
        key="storage_used_percent",
        translation_key="storage_used_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.used / s.total * 100, 2) if s.total else 0.0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InternxtConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the storage sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        InternxtStorageSensor(coordinator, description) for description in SENSORS
    )


class InternxtStorageSensor(
    CoordinatorEntity[InternxtStorageCoordinator], SensorEntity
):
    """One storage value of the Internxt account."""

    _attr_has_entity_name = True
    entity_description: InternxtSensorDescription

    def __init__(
        self,
        coordinator: InternxtStorageCoordinator,
        description: InternxtSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name="Internxt",
            manufacturer="Internxt",
            model="Drive",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://drive.internxt.com",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
