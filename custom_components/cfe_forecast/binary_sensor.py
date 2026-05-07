"""
Plataforma binary_sensor para CFE Forecast MX.

Sensores binarios disponibles:
  - Alerta Expiracion de Bolsa: kWh proximos a vencer (30 dias).
    El mensaje descriptivo se expone como atributo, no como sensor de texto.
  - Riesgo de Tarifa DAC: consumo proyectado supera el umbral DAC.
    El mensaje descriptivo se expone como atributo, no como sensor de texto.
"""

from __future__ import annotations

import logging
from datetime import date

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .sensor import CFECoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Configura los sensores binarios al cargar la integracion.

    Usa el mismo coordinador que sensor.py: no hay lecturas adicionales
    a la red. Se crean exactamente 2 binary_sensors, sin duplicados.
    """
    coordinator: CFECoordinator = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities([
        CFEAlertaExpiracionBinarySensor(coordinator, config_entry),
        CFERiesgoDACBinarySensor(coordinator, config_entry),
    ])

    _LOGGER.debug(
        "[CFE] Sensores binarios registrados para entry_id: %s",
        config_entry.entry_id,
    )


# =============================================================================
# CLASE BASE PARA BINARY SENSORS
# =============================================================================

class CFEBaseBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Clase base compartida por los binary sensors de CFE Forecast MX."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CFECoordinator,
        config_entry: ConfigEntry,
        unique_id_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{unique_id_suffix}"

    @property
    def device_info(self) -> dict:
        """Agrupa este sensor bajo el mismo dispositivo que los sensores regulares."""
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "CFE Forecast MX",
            "manufacturer": "CFE (Comision Federal de Electricidad)",
            "model": f"Tarifa {self.coordinator.data.get('tariff', '?')}",
            "entry_type": "service",
        }


# =============================================================================
# SENSOR BINARIO: ALERTA EXPIRACION DE BOLSA
# =============================================================================

class CFEAlertaExpiracionBinarySensor(CFEBaseBinarySensor):
    """
    Sensor Binario: Alerta por kWh de bolsa proximos a vencer.

    Se activa (ON) cuando hay kWh en la bolsa que venceran en los proximos
    30 dias. El mensaje con los detalles se expone como atributo 'mensaje'.

    Estado:
      OFF → Sin kWh proximos a vencer.
      ON  → Hay kWh que vencen pronto (ver atributo 'mensaje' para detalles).
    """

    _attr_name = "Alerta Expiracion de Bolsa"
    _attr_icon = "mdi:clock-alert"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: CFECoordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry, "alerta_expiracion")

    @property
    def is_on(self) -> bool | None:
        """True si hay kWh por vencer en los proximos 30 dias."""
        if self.coordinator.data:
            return self.coordinator.data.get("alerta_expiracion", False)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """
        Atributos con el detalle de la alerta.
        'mensaje' reemplaza cualquier sensor de texto independiente.
        """
        data = self.coordinator.data or {}
        fecha_str = data.get("bolsa_proxima_vencer_fecha")

        dias_para_vencer = None
        if fecha_str:
            try:
                dias_para_vencer = (date.fromisoformat(fecha_str) - date.today()).days
            except ValueError:
                pass

        return {
            "mensaje": data.get("alerta_expiracion_mensaje", ""),
            "kwh_por_vencer": data.get("bolsa_proxima_vencer_kwh"),
            "fecha_vencimiento": fecha_str,
            "dias_para_vencer": dias_para_vencer,
        }


# =============================================================================
# SENSOR BINARIO: RIESGO DAC
# =============================================================================

class CFERiesgoDACBinarySensor(CFEBaseBinarySensor):
    """
    Sensor Binario: Riesgo de cambio a tarifa DAC.

    Se activa (ON) cuando la proyeccion de consumo indica que el hogar
    podria ser reclasificado en tarifa DAC (Domestica de Alto Consumo).
    El mensaje explicativo se expone como atributo 'mensaje'.

    Estado:
      OFF → Consumo dentro de limites normales.
      ON  → Consumo proyectado supera el umbral DAC (ver atributo 'mensaje').
    """

    _attr_name = "Riesgo de Tarifa DAC"
    _attr_icon = "mdi:alert-circle"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: CFECoordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry, "riesgo_dac")

    @property
    def is_on(self) -> bool | None:
        """True si el consumo proyectado supera el umbral DAC."""
        if self.coordinator.data:
            return self.coordinator.data.get("en_riesgo_dac", False)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """
        Atributos con el detalle del riesgo DAC.
        'mensaje' reemplaza cualquier sensor de texto independiente.
        """
        data = self.coordinator.data or {}
        return {
            "mensaje": data.get("riesgo_dac_mensaje", ""),
            "consumo_actual_kwh": data.get("consumo_neto_kwh"),
            "consumo_proyectado_kwh": data.get("proyeccion_kwh"),
            "tarifa_actual": data.get("tariff"),
        }
