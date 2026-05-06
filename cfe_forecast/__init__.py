"""
Módulo de inicialización para CFE Forecast MX.

Este archivo maneja el ciclo de vida completo de la integración:
  - async_setup_entry: Carga la integración al iniciar HA o al agregar la entrada.
  - async_unload_entry: Descarga limpiamente al eliminar la integración.
  - async_reload_entry: Recarga cuando se cambian las opciones.

La integración usa:
  - DataUpdateCoordinator para eficiencia (una sola actualización compartida).
  - helpers.storage.Store para persistir la bolsa de energía entre reinicios.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PLATFORMS, STORAGE_KEY, STORAGE_VERSION
from .sensor import CFECoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Configura la integración CFE Forecast MX a partir de una ConfigEntry.
    
    Este método se llama automáticamente cuando:
      - Home Assistant inicia y la integración ya estaba configurada.
      - El usuario agrega una nueva instancia desde la UI.
      - La integración se recarga (después de cambiar opciones).
    
    Flujo:
      1. Crear el Store para persistencia de datos.
      2. Crear e inicializar el Coordinador.
      3. Cargar el estado guardado (bolsa de energía, lecturas base).
      4. Realizar la primera actualización de datos.
      5. Registrar el coordinador en hass.data para que los sensores lo encuentren.
      6. Configurar las plataformas (sensor, binary_sensor).
    
    Args:
        hass: Instancia de Home Assistant.
        entry: Entrada de configuración de esta integración.
    
    Returns:
        True si la configuración fue exitosa, False en caso contrario.
    """
    _LOGGER.info(
        "[CFE] Iniciando CFE Forecast MX (entry_id: %s, tarifa: %s)",
        entry.entry_id,
        entry.data.get("tariff", "?"),
    )

    # ── Inicializar el namespace de datos para esta integración ──────────
    hass.data.setdefault(DOMAIN, {})

    # ── Crear el Store de persistencia ───────────────────────────────────
    # El Store guarda los datos en .storage/cfe_forecast_energy_store
    # y sobrevive reinicios de Home Assistant.
    store = Store(
        hass,
        version=STORAGE_VERSION,
        key=STORAGE_KEY,
    )

    # ── Crear el Coordinador ─────────────────────────────────────────────
    coordinator = CFECoordinator(hass, entry, store)

    # ── Cargar el estado persistido ──────────────────────────────────────
    # Recupera la bolsa de energía y las lecturas base guardadas previamente.
    # Si no hay datos guardados, el coordinador inicia desde cero.
    await coordinator.async_load_state()
    _LOGGER.debug("[CFE] Estado cargado del Store correctamente.")

    # ── Primera actualización de datos ───────────────────────────────────
    # Realiza la primera lectura de sensores y cálculos.
    # Si falla, la integración no se carga (el error se muestra en la UI).
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.info("[CFE] Primera actualización de datos completada.")

    # ── Registrar el coordinador en hass.data ────────────────────────────
    # Los sensores lo recuperarán usando: hass.data[DOMAIN][entry_id]
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # ── Configurar las plataformas ───────────────────────────────────────
    # Esto llama a async_setup_entry en sensor.py y binary_sensor.py
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("[CFE] Plataformas configuradas: %s", PLATFORMS)

    # ── Escuchar cambios de opciones ─────────────────────────────────────
    # Cuando el usuario edita las opciones, se recarga la integración automáticamente
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Descarga limpiamente la integración CFE Forecast MX.
    
    Se llama cuando el usuario elimina la integración desde la UI,
    o cuando HA necesita recargarla (por cambio de opciones).
    
    Flujo:
      1. Descargar todas las plataformas registradas.
      2. Eliminar el coordinador del namespace de datos.
    
    Args:
        hass: Instancia de Home Assistant.
        entry: Entrada de configuración a descargar.
    
    Returns:
        True si la descarga fue exitosa.
    """
    _LOGGER.info("[CFE] Descargando CFE Forecast MX (entry_id: %s)", entry.entry_id)

    # Descargar todas las entidades de las plataformas
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Limpiar el coordinador del namespace de datos
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.info("[CFE] Integración descargada correctamente.")
    else:
        _LOGGER.error("[CFE] Error al descargar las plataformas de la integración.")

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Recarga la integración cuando se cambian las opciones.
    
    Este método es llamado automáticamente por el listener registrado
    en async_setup_entry cuando el usuario guarda cambios en Options Flow.
    
    La recarga es transparente para el usuario: las entidades se
    actualizan con la nueva configuración sin perder el historial de HA.
    
    Args:
        hass: Instancia de Home Assistant.
        entry: Entrada de configuración que fue modificada.
    """
    _LOGGER.info(
        "[CFE] Recargando integración por cambio de opciones (entry_id: %s)",
        entry.entry_id,
    )
    await hass.config_entries.async_reload(entry.entry_id)
