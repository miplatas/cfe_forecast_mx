"""
Plataforma binary_sensor para CFE Forecast MX.

Los sensores binarios están definidos en sensor.py para aprovechar el mismo
DataUpdateCoordinator. Este módulo actúa como punto de entrada de la plataforma
y re-exporta las clases de alertas.

Sensores binarios disponibles:
  - cfe_alerta_expiracion_bolsa: kWh de bolsa próximos a vencer (30 días).
  - cfe_riesgo_dac: Riesgo de reclasificación a tarifa DAC.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .sensor import (
    CFECoordinator,
    CFEAlertaExpiracionSensor,
    CFERiesgoDACBinarySensor,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Configura los sensores binarios al cargar la integración.
    
    Recupera el coordinador compartido y crea las entidades binary_sensor.
    Los sensores binarios usan el mismo coordinador que los sensores regulares,
    por lo que no hay lecturas adicionales a la red.
    
    Args:
        hass: Instancia de Home Assistant.
        config_entry: Entrada de configuración de esta integración.
        async_add_entities: Función para registrar las entidades en HA.
    """
    coordinator: CFECoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # Registrar los sensores binarios de alerta
    async_add_entities([
        CFEAlertaExpiracionSensor(coordinator, config_entry),
        CFERiesgoDACBinarySensor(coordinator, config_entry),
    ])

    _LOGGER.debug(
        "[CFE] Sensores binarios registrados para entry_id: %s",
        config_entry.entry_id,
    )
