"""
Sensores y lógica de cálculo para CFE Forecast MX.

Este módulo implementa:
  - CFECoordinator: actualiza todos los datos cada N minutos.
  - Lógica de "cero virtual": captura lecturas iniciales y calcula deltas bimestrales.
  - Bolsa de energía: acumula excedentes netos por día (un depósito por día, no por ciclo).
  - Expiración de bolsa a 12 meses con alerta previa de 30 días.
  - Cálculo progresivo de costo según escalones CFE.
  - Proyección de costo al final del bimestre.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
    CONF_IMPORT_SENSOR,
    CONF_EXPORT_SENSOR,
    CONF_TARIFF,
    CONF_CUT_DAY,
    CONF_START_MONTH,
    CONF_INITIAL_BAG,
    CONF_BASIC_LIMIT,
    CONF_BASIC_PRICE,
    CONF_INTERMEDIATE_LIMIT,
    CONF_INTERMEDIATE_PRICE,
    CONF_EXCESS_PRICE,
    CONF_IVA,
    CONF_FIXED_CHARGE,
    BOLSA_EXPIRATION_MONTHS,
    BOLSA_EXPIRATION_ALERT_DAYS,
    DEFAULT_BASIC_LIMIT,
    DEFAULT_BASIC_PRICE,
    DEFAULT_INTERMEDIATE_LIMIT,
    DEFAULT_INTERMEDIATE_PRICE,
    DEFAULT_EXCESS_PRICE,
    DEFAULT_IVA,
    DEFAULT_FIXED_CHARGE,
)

_LOGGER = logging.getLogger(__name__)

UNIT_PESOS = "MXN"


# =============================================================================
# PUNTO DE ENTRADA DE LA PLATAFORMA
# =============================================================================

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Configura los sensores al cargar la integración.

    Solo crea entidades de tipo sensor. Los binary_sensor se crean
    en binary_sensor.py usando el mismo coordinador.
    """
    coordinator: CFECoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = [
        CFECostoActualSensor(coordinator, config_entry),
        CFEProyeccionSensor(coordinator, config_entry),
        CFEConsumoNetoBimestreSensor(coordinator, config_entry),
        CFEBolsaEnergiaSensor(coordinator, config_entry),
        CFEBaselineImportSensor(coordinator, config_entry),
        CFEBaselineExportSensor(coordinator, config_entry),
    ]

    async_add_entities(entities)


# =============================================================================
# COORDINATOR
# =============================================================================

class CFECoordinator(DataUpdateCoordinator):
    """
    Coordinador central de CFE Forecast MX.

    Responsabilidades:
      1. Leer los sensores de HA (import / export) cada N minutos.
      2. Calcular el delta respecto al inicio del bimestre (cero virtual).
      3. Mantener un acumulador DIARIO que evita duplicación en la bolsa:
         - Calcula delta diario (no acumulado del bimestre)
         - Al cambiar de día, traspa el acumulador anterior a la bolsa
         - Esto previene sumar dos veces lo mismo
      4. Calcular el costo progresivo segun escalones CFE.
      5. Persistir el estado en el Store de HA para sobrevivir reinicios.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        store,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.config_entry = config_entry
        self._store = store

        self._import_baseline: float = 0.0
        self._export_baseline: float = 0.0
        self._baseline_captured: bool = False
        self._bimestre_start: date | None = None

        # Lista de depositos FIFO. Cada elemento: {"kwh": float, "date": "YYYY-MM-DD"}
        # Solo existe UN deposito por fecha.
        self._bolsa_depositos: list[dict] = []

        self._last_import_reading: float | None = None
        self._last_export_reading: float | None = None
        self._last_reading_date: date | None = None

        # Acumulador diario: almacena el excedente (negativo) o deficit (positivo) del día actual.
        # Se usa para evitar duplicación al depositar en bolsa.
        # Cuando cambia el día, el acumulador se deposita en la bolsa y se reinicia.
        self._acumulador_diario: float = 0.0
        
        # Lecturas de inicio del día (se actualizan al cambiar de día)
        self._import_inicio_dia: float = 0.0
        self._export_inicio_dia: float = 0.0

        self.data: dict[str, Any] = self._empty_data()

    def _empty_data(self) -> dict[str, Any]:
        return {
            "consumo_neto_kwh": 0.0,
            "bolsa_total_kwh": 0.0,
            "costo_sin_iva": 0.0,
            "costo_con_iva": 0.0,
            "proyeccion_kwh": 0.0,
            "proyeccion_costo": 0.0,
            "dias_transcurridos": 0,
            "dias_restantes": 0,
            "bolsa_proxima_vencer_kwh": 0.0,
            "bolsa_proxima_vencer_fecha": None,
            "en_riesgo_dac": False,
            "alerta_expiracion": False,
            "alerta_expiracion_mensaje": "",
            "riesgo_dac_mensaje": "",
            "tariff": "1C",
        }

    # ── Persistencia ─────────────────────────────────────────────────────────

    async def async_load_state(self) -> None:
        stored = await self._store.async_load()
        if stored is None:
            _LOGGER.info("[CFE] No hay estado previo guardado. Iniciando desde cero.")
            return

        entry_id = self.config_entry.entry_id
        if entry_id not in stored:
            _LOGGER.info("[CFE] No hay datos para esta entrada. Iniciando desde cero.")
            return

        state = stored[entry_id]
        _LOGGER.debug("[CFE] Estado recuperado del Store: %s", state)

        self._import_baseline = state.get("import_baseline", 0.0)
        self._export_baseline = state.get("export_baseline", 0.0)
        self._baseline_captured = state.get("baseline_captured", False)

        bimestre_start_str = state.get("bimestre_start")
        if bimestre_start_str:
            self._bimestre_start = date.fromisoformat(bimestre_start_str)

        self._bolsa_depositos = state.get("bolsa_depositos", [])

        self._last_import_reading = state.get("last_import_reading")
        self._last_export_reading = state.get("last_export_reading")
        last_date_str = state.get("last_reading_date")
        if last_date_str:
            self._last_reading_date = date.fromisoformat(last_date_str)

        self._acumulador_diario = state.get("acumulador_diario", 0.0)
        self._import_inicio_dia = state.get("import_inicio_dia", 0.0)
        self._export_inicio_dia = state.get("export_inicio_dia", 0.0)

    async def async_save_state(self) -> None:
        stored = await self._store.async_load() or {}

        stored[self.config_entry.entry_id] = {
            "import_baseline": self._import_baseline,
            "export_baseline": self._export_baseline,
            "baseline_captured": self._baseline_captured,
            "bimestre_start": (
                self._bimestre_start.isoformat() if self._bimestre_start else None
            ),
            "bolsa_depositos": self._bolsa_depositos,
            "last_import_reading": self._last_import_reading,
            "last_export_reading": self._last_export_reading,
            "last_reading_date": (
                self._last_reading_date.isoformat() if self._last_reading_date else None
            ),
            "acumulador_diario": self._acumulador_diario,
            "import_inicio_dia": self._import_inicio_dia,
            "export_inicio_dia": self._export_inicio_dia,
        }

        await self._store.async_save(stored)
        _LOGGER.debug("[CFE] Estado guardado en Store.")

    # ── Deteccion del bimestre ────────────────────────────────────────────────

    def _calcular_inicio_bimestre(self) -> date:
        cfg = {**self.config_entry.data, **self.config_entry.options}
        cut_day: int = int(cfg.get(CONF_CUT_DAY, 1))
        start_month: int = int(cfg.get(CONF_START_MONTH, 1))

        today = date.today()
        meses_inicio = [(start_month + i * 2 - 1) % 12 + 1 for i in range(6)]

        mes_bimestre = today.month
        for mes in sorted(meses_inicio, reverse=True):
            if mes <= today.month:
                mes_bimestre = mes
                break
        else:
            mes_bimestre = meses_inicio[-1]

        try:
            inicio = date(today.year, mes_bimestre, cut_day)
        except ValueError:
            import calendar
            ultimo_dia = calendar.monthrange(today.year, mes_bimestre)[1]
            inicio = date(today.year, mes_bimestre, min(cut_day, ultimo_dia))

        if inicio > today:
            mes_anterior = mes_bimestre - 2
            anio = today.year
            if mes_anterior <= 0:
                mes_anterior += 12
                anio -= 1
            try:
                inicio = date(anio, mes_anterior, cut_day)
            except ValueError:
                import calendar
                ultimo_dia = calendar.monthrange(anio, mes_anterior)[1]
                inicio = date(anio, mes_anterior, min(cut_day, ultimo_dia))

        return inicio

    def _calcular_fin_bimestre(self, inicio: date) -> date:
        mes_fin = inicio.month + 2
        anio_fin = inicio.year
        if mes_fin > 12:
            mes_fin -= 12
            anio_fin += 1

        try:
            return date(anio_fin, mes_fin, inicio.day)
        except ValueError:
            import calendar
            ultimo_dia = calendar.monthrange(anio_fin, mes_fin)[1]
            return date(anio_fin, mes_fin, ultimo_dia)

    def _nuevo_bimestre_detectado(self) -> bool:
        inicio_actual = self._calcular_inicio_bimestre()
        if self._bimestre_start is None:
            return True
        return inicio_actual != self._bimestre_start

    # ── Bolsa de energia ─────────────────────────────────────────────────────

    def _limpiar_bolsa_expirada(self) -> None:
        limite_expiracion = date.today() - timedelta(days=BOLSA_EXPIRATION_MONTHS * 30)
        antes = sum(d["kwh"] for d in self._bolsa_depositos)

        self._bolsa_depositos = [
            d for d in self._bolsa_depositos
            if date.fromisoformat(d["date"]) > limite_expiracion
        ]

        despues = sum(d["kwh"] for d in self._bolsa_depositos)
        if antes > despues:
            _LOGGER.info("[CFE] Se expiraron %.2f kWh de la bolsa.", antes - despues)

    def _traspasar_acumulador_a_bolsa(self) -> None:
        """
        Traspasa el acumulador diario a la bolsa de energía como un depósito del día.
        
        Esto ocurre cuando:
        - El acumulador es negativo (hay excedente): se deposita en bolsa.
        - Se reinicia el acumulador para el nuevo día.
        """
        if self._acumulador_diario < -0.001:  # Solo si hay excedente
            excedente = abs(self._acumulador_diario)
            hoy_str = date.today().isoformat()
            
            # Buscar si ya existe un depósito de hoy y actualizarlo
            for deposito in self._bolsa_depositos:
                if deposito["date"] == hoy_str:
                    deposito["kwh"] = round(excedente, 3)
                    _LOGGER.debug(
                        "[CFE] Acumulador diario (%.3f kWh) trasvasado a bolsa de hoy.",
                        excedente,
                    )
                    self._acumulador_diario = 0.0
                    return
            
            # Si no existe, crear uno nuevo
            self._bolsa_depositos.append({"kwh": round(excedente, 3), "date": hoy_str})
            _LOGGER.debug(
                "[CFE] Nuevo depósito en bolsa por acumulador diario: %.3f kWh.",
                excedente,
            )
        
        self._acumulador_diario = 0.0

    def _consumir_de_bolsa(self, kwh_necesarios: float) -> float:
        """
        Consume kWh de la bolsa usando logica FIFO (mas antiguos primero).
        No toca el deposito del dia actual.

        Returns:
            kWh que no pudieron cubrirse con la bolsa (a cobrar por CFE).
        """
        remanente = kwh_necesarios
        hoy_str = date.today().isoformat()

        depositos_aplicables = sorted(
            [d for d in self._bolsa_depositos if d["date"] != hoy_str],
            key=lambda d: d["date"],
        )

        for deposito in depositos_aplicables:
            if remanente <= 0:
                break
            consumir = min(deposito["kwh"], remanente)
            deposito["kwh"] = round(deposito["kwh"] - consumir, 3)
            remanente = round(remanente - consumir, 3)

        self._bolsa_depositos = [
            d for d in self._bolsa_depositos
            if d["kwh"] > 0.001 or d["date"] == hoy_str
        ]

        return max(0.0, remanente)

    def _total_bolsa(self) -> float:
        return round(sum(d["kwh"] for d in self._bolsa_depositos), 3)

    def _info_proxima_expiracion(self) -> tuple[float, date | None]:
        depositos_validos = [d for d in self._bolsa_depositos if d["kwh"] > 0]
        if not depositos_validos:
            return 0.0, None

        mas_antiguo = min(depositos_validos, key=lambda d: d["date"])
        fecha_deposito = date.fromisoformat(mas_antiguo["date"])
        fecha_vencimiento = fecha_deposito + timedelta(days=BOLSA_EXPIRATION_MONTHS * 30)

        return mas_antiguo["kwh"], fecha_vencimiento

    # ── Calculo de costo ──────────────────────────────────────────────────────

    def _calcular_costo_progresivo(self, kwh_neto: float) -> float:
        if kwh_neto <= 0:
            return 0.0

        cfg = {**self.config_entry.data, **self.config_entry.options}
        basico_limite = float(cfg.get(CONF_BASIC_LIMIT, DEFAULT_BASIC_LIMIT))
        basico_precio = float(cfg.get(CONF_BASIC_PRICE, DEFAULT_BASIC_PRICE))
        intermedio_limite = float(cfg.get(CONF_INTERMEDIATE_LIMIT, DEFAULT_INTERMEDIATE_LIMIT))
        intermedio_precio = float(cfg.get(CONF_INTERMEDIATE_PRICE, DEFAULT_INTERMEDIATE_PRICE))
        excedente_precio = float(cfg.get(CONF_EXCESS_PRICE, DEFAULT_EXCESS_PRICE))

        costo = 0.0
        remanente = kwh_neto

        kwh_basico = min(remanente, basico_limite)
        costo += kwh_basico * basico_precio
        remanente -= kwh_basico

        if remanente > 0:
            kwh_intermedio = min(remanente, intermedio_limite)
            costo += kwh_intermedio * intermedio_precio
            remanente -= kwh_intermedio

        if remanente > 0:
            costo += remanente * excedente_precio

        return round(costo, 2)

    def _verificar_riesgo_dac(self, kwh_neto: float, dias_transcurridos: int) -> bool:
        if dias_transcurridos <= 0:
            return False

        cfg = {**self.config_entry.data, **self.config_entry.options}
        basico_limite = float(cfg.get(CONF_BASIC_LIMIT, DEFAULT_BASIC_LIMIT))
        intermedio_limite = float(cfg.get(CONF_INTERMEDIATE_LIMIT, DEFAULT_INTERMEDIATE_LIMIT))

        limite_dac_bimestre = (basico_limite + intermedio_limite) * 2
        consumo_diario_promedio = kwh_neto / dias_transcurridos
        consumo_proyectado = consumo_diario_promedio * 60

        return consumo_proyectado > limite_dac_bimestre

    # ── Lectura de sensores ───────────────────────────────────────────────────

    def _leer_estado_sensor(self, entity_id: str) -> float | None:
        if not entity_id:
            return None

        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            _LOGGER.warning(
                "[CFE] Sensor %s no disponible (estado: %s).",
                entity_id,
                state.state if state else "None",
            )
            return None

        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.error(
                "[CFE] No se pudo convertir el estado de %s a numero: %s",
                entity_id,
                state.state,
            )
            return None

    # ── Actualizacion principal ───────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """
        Metodo principal llamado por el DataUpdateCoordinator cada N minutos.

        Flujo:
          1. Leer sensores de HA.
          2. Detectar nuevo bimestre: reiniciar estado y capturar baseline.
          3. Primera captura de baseline si no se hizo todavia.
          4. Calcular delta bimestral (consumo real desde el inicio del ciclo).
          5. Detectar cambio de día y traspasar acumulador a bolsa.
          6. Calcular delta diario y acumular en acumulador diario.
          7. Gestionar consumo de bolsa basado en acumulador diario (evita duplicación).
          8. Calcular costo progresivo.
          9. Generar proyeccion al final del bimestre.
          10. Calcular alertas con mensajes descriptivos.
          11. Guardar estado.
        """
        cfg = {**self.config_entry.data, **self.config_entry.options}

        # 1. Leer sensores
        import_entity = cfg.get(CONF_IMPORT_SENSOR, "")
        export_entity = cfg.get(CONF_EXPORT_SENSOR, "")

        import_lectura = self._leer_estado_sensor(import_entity)
        export_lectura = self._leer_estado_sensor(export_entity) or 0.0

        if import_lectura is None:
            raise UpdateFailed(
                f"Sensor de importacion {import_entity} no disponible."
            )

        hoy = date.today()

        # 2. Detectar inicio de nuevo bimestre
        if self._nuevo_bimestre_detectado():
            nuevo_inicio = self._calcular_inicio_bimestre()
            _LOGGER.info(
                "[CFE] Nuevo bimestre detectado. Inicio: %s. "
                "Capturando lecturas base (import=%.3f, export=%.3f).",
                nuevo_inicio, import_lectura, export_lectura,
            )
            self._import_baseline = import_lectura
            self._export_baseline = export_lectura
            self._baseline_captured = True
            self._bimestre_start = nuevo_inicio

            self._limpiar_bolsa_expirada()

            inicial = float(cfg.get(CONF_INITIAL_BAG, 0.0))
            if inicial > 0 and not self._bolsa_depositos:
                self._bolsa_depositos.append({
                    "kwh": round(inicial, 3),
                    "date": nuevo_inicio.isoformat(),
                })
                _LOGGER.info("[CFE] Bolsa inicial de %.3f kWh anadida.", inicial)

        # 3. Primera captura de baseline
        if not self._baseline_captured:
            self._import_baseline = import_lectura
            self._export_baseline = export_lectura
            self._baseline_captured = True
            self._bimestre_start = self._calcular_inicio_bimestre()
            _LOGGER.info(
                "[CFE] Primera captura de baseline. Import: %.3f, Export: %.3f",
                self._import_baseline, self._export_baseline,
            )

            inicial = float(cfg.get(CONF_INITIAL_BAG, 0.0))
            if inicial > 0 and not self._bolsa_depositos:
                self._bolsa_depositos.append({
                    "kwh": round(inicial, 3),
                    "date": self._bimestre_start.isoformat(),
                })
                _LOGGER.info("[CFE] Bolsa inicial de %.3f kWh anadida.", inicial)

        # 4. Calcular delta bimestral (para mostrar consumo neto del bimestre)
        delta_import = max(0.0, import_lectura - self._import_baseline)
        delta_export = max(0.0, export_lectura - self._export_baseline)
        consumo_neto_bimestre = delta_import - delta_export

        # 5. Detectar cambio de día y gestionar acumulador diario
        # Si cambió el día, traspasar el acumulador del día anterior a la bolsa.
        if self._last_reading_date is not None and self._last_reading_date < hoy:
            self._traspasar_acumulador_a_bolsa()
            # Reiniciar las lecturas de inicio del nuevo día
            self._import_inicio_dia = import_lectura
            self._export_inicio_dia = export_lectura
            _LOGGER.debug(
                "[CFE] Nuevo día detectado. Inicio del día: import=%.3f, export=%.3f",
                self._import_inicio_dia,
                self._export_inicio_dia,
            )
        elif self._import_inicio_dia == 0.0 and self._export_inicio_dia == 0.0:
            # Primera ejecución del día
            self._import_inicio_dia = import_lectura
            self._export_inicio_dia = export_lectura
            _LOGGER.debug(
                "[CFE] Primera ejecución del día. Inicio: import=%.3f, export=%.3f",
                self._import_inicio_dia,
                self._export_inicio_dia,
            )

        # 6. Calcular delta diario (desde inicio del día hasta ahora)
        delta_import_hoy = max(0.0, import_lectura - self._import_inicio_dia)
        delta_export_hoy = max(0.0, export_lectura - self._export_inicio_dia)
        delta_neto_hoy = delta_import_hoy - delta_export_hoy

        # 7. Acumular el delta diario al acumulador
        self._acumulador_diario += delta_neto_hoy
        self._acumulador_diario = round(self._acumulador_diario, 3)

        # 8. Gestionar el consumo de la bolsa basado en el acumulador diario
        # Si el acumulador es positivo (consumo), consumir de la bolsa primero
        if self._acumulador_diario > 0.001:
            kwh_a_cobrar = self._consumir_de_bolsa(self._acumulador_diario)
            self._acumulador_diario = 0.0  # El consumo se procesó
            _LOGGER.debug(
                "[CFE] Acumulador diario consumido. kWh a cobrar: %.3f", kwh_a_cobrar
            )
        else:
            kwh_a_cobrar = 0.0

        self._last_import_reading = import_lectura
        self._last_export_reading = export_lectura
        self._last_reading_date = hoy

        # 9. Calcular costo progresivo
        iva = float(cfg.get(CONF_IVA, DEFAULT_IVA))
        cargo_fijo = float(cfg.get(CONF_FIXED_CHARGE, DEFAULT_FIXED_CHARGE))

        costo_sin_iva = self._calcular_costo_progresivo(kwh_a_cobrar)
        costo_con_iva = round((costo_sin_iva + cargo_fijo) * (1 + iva), 2)

        # 10. Proyeccion al final del bimestre
        inicio_bimestre = self._bimestre_start or hoy
        fin_bimestre = self._calcular_fin_bimestre(inicio_bimestre)
        dias_totales = max(1, (fin_bimestre - inicio_bimestre).days)
        dias_transcurridos = max(1, (hoy - inicio_bimestre).days)
        dias_restantes = max(0, (fin_bimestre - hoy).days)

        consumo_diario_promedio = consumo_neto_bimestre / dias_transcurridos
        consumo_proyectado = consumo_diario_promedio * dias_totales
        bolsa_disponible = self._total_bolsa()
        kwh_proyectado_a_cobrar = max(0.0, consumo_proyectado - bolsa_disponible)
        costo_proyectado_sin_iva = self._calcular_costo_progresivo(kwh_proyectado_a_cobrar)
        costo_proyectado = round((costo_proyectado_sin_iva + cargo_fijo) * (1 + iva), 2)

        # 11. Calcular alertas
        kwh_por_vencer, fecha_vencimiento = self._info_proxima_expiracion()

        alerta_expiracion = False
        alerta_expiracion_mensaje = "Sin kWh proximos a vencer."
        if fecha_vencimiento:
            dias_para_vencer = (fecha_vencimiento - hoy).days
            alerta_expiracion = dias_para_vencer <= BOLSA_EXPIRATION_ALERT_DAYS
            if alerta_expiracion:
                alerta_expiracion_mensaje = (
                    f"{round(kwh_por_vencer, 2)} kWh vencen el "
                    f"{fecha_vencimiento.strftime('%d/%m/%Y')} "
                    f"(en {dias_para_vencer} dias). Consúmalos antes de perderlos."
                )

        en_riesgo_dac = self._verificar_riesgo_dac(consumo_neto_bimestre, dias_transcurridos)
        riesgo_dac_mensaje = (
            "Consumo proyectado supera el limite DAC. "
            "Considere reducir el consumo para evitar tarifas mas altas."
            if en_riesgo_dac
            else "Consumo dentro de limites normales."
        )

        # 12. Guardar estado
        await self.async_save_state()

        resultado = {
            "import_baseline": self._import_baseline,
            "export_baseline": self._export_baseline,
            "consumo_neto_kwh": round(consumo_neto_bimestre, 3),
            "bolsa_total_kwh": self._total_bolsa(),
            "costo_sin_iva": round(costo_sin_iva, 2),
            "costo_con_iva": costo_con_iva,
            "proyeccion_kwh": round(consumo_proyectado, 3),
            "proyeccion_costo": costo_proyectado,
            "dias_transcurridos": dias_transcurridos,
            "dias_restantes": dias_restantes,
            "bolsa_proxima_vencer_kwh": round(kwh_por_vencer, 3),
            "bolsa_proxima_vencer_fecha": (
                fecha_vencimiento.isoformat() if fecha_vencimiento else None
            ),
            "en_riesgo_dac": en_riesgo_dac,
            "alerta_expiracion": alerta_expiracion,
            "alerta_expiracion_mensaje": alerta_expiracion_mensaje,
            "riesgo_dac_mensaje": riesgo_dac_mensaje,
            "tariff": cfg.get(CONF_TARIFF, "1C"),
            "bimestre_inicio": inicio_bimestre.isoformat(),
            "bimestre_fin": fin_bimestre.isoformat(),
        }

        _LOGGER.debug("[CFE] Actualizacion completada: %s", resultado)
        return resultado


# =============================================================================
# CLASE BASE
# =============================================================================

class CFEBaseSensor(CoordinatorEntity, SensorEntity):
    """Clase base compartida por todos los sensores de CFE Forecast MX."""

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
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "CFE Forecast MX",
            "manufacturer": "CFE (Comision Federal de Electricidad)",
            "model": f"Tarifa {self.coordinator.data.get('tariff', '?')}",
            "entry_type": "service",
        }


# =============================================================================
# SENSORES MONETARIOS
# =============================================================================

class CFECostoActualSensor(CFEBaseSensor):
    """Costo acumulado en el bimestre en curso (con IVA y cargo fijo)."""

    _attr_name = "Costo Actual del Bimestre"
    _attr_icon = "mdi:cash"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UNIT_PESOS

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "costo_actual")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            return self.coordinator.data.get("costo_con_iva")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "costo_sin_iva": data.get("costo_sin_iva"),
            "dias_transcurridos": data.get("dias_transcurridos"),
            "dias_restantes": data.get("dias_restantes"),
            "bimestre_inicio": data.get("bimestre_inicio"),
            "bimestre_fin": data.get("bimestre_fin"),
            "tarifa": data.get("tariff"),
        }


class CFEProyeccionSensor(CFEBaseSensor):
    """Proyeccion del costo del recibo al final del bimestre."""

    _attr_name = "Proyeccion del Recibo Final"
    _attr_icon = "mdi:cash-clock"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UNIT_PESOS

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "proyeccion_recibo")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            return self.coordinator.data.get("proyeccion_costo")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        dias = data.get("dias_transcurridos", 1) or 1
        return {
            "consumo_proyectado_kwh": data.get("proyeccion_kwh"),
            "dias_restantes": data.get("dias_restantes"),
            "promedio_diario_kwh": round(
                data.get("consumo_neto_kwh", 0.0) / dias, 3
            ),
        }


# =============================================================================
# SENSORES DE ENERGIA
# =============================================================================

class CFEConsumoNetoBimestreSensor(CFEBaseSensor):
    """
    Consumo neto del bimestre en kWh (importado menos exportado).
    Puede ser negativo si el usuario exporto mas de lo que consumio.
    """

    _attr_name = "Consumo Neto del Bimestre"
    _attr_icon = "mdi:lightning-bolt"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "consumo_neto_bimestre")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            return self.coordinator.data.get("consumo_neto_kwh")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "bimestre_inicio": data.get("bimestre_inicio"),
            "bimestre_fin": data.get("bimestre_fin"),
        }


class CFEBolsaEnergiaSensor(CFEBaseSensor):
    """
    kWh disponibles en la bolsa de energia.

    Acumula los excedentes de exportacion neta del bimestre.
    Se aplican antes de cobrar al usuario. Vencen a los 12 meses.
    """

    _attr_name = "Bolsa de Energia Disponible"
    _attr_icon = "mdi:battery-positive"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "bolsa_energia")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            return self.coordinator.data.get("bolsa_total_kwh")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "kwh_proximos_a_vencer": data.get("bolsa_proxima_vencer_kwh"),
            "fecha_proximo_vencimiento": data.get("bolsa_proxima_vencer_fecha"),
            "meses_vigencia": BOLSA_EXPIRATION_MONTHS,
        }


# =============================================================================
# SENSORES DE LECTURA BASE (CERO VIRTUAL)
# =============================================================================

class CFEBaselineImportSensor(CFEBaseSensor):
    """
    Lectura del medidor de importacion al inicio del bimestre.

    Este es el 'cero virtual' de importacion. El consumo neto del bimestre
    se calcula como: lectura_actual - este_valor.
    Se actualiza automaticamente al detectar un nuevo bimestre.
    """

    _attr_name = "Lectura Base de Importacion"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "baseline_import")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            return self.coordinator.data.get("import_baseline")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "bimestre_inicio": data.get("bimestre_inicio"),
            "descripcion": "Lectura del medidor al inicio del bimestre (punto cero de importacion).",
        }


class CFEBaselineExportSensor(CFEBaseSensor):
    """
    Lectura del medidor de exportacion al inicio del bimestre.

    Este es el 'cero virtual' de exportacion. El excedente neto del bimestre
    se calcula como: lectura_actual - este_valor.
    Se actualiza automaticamente al detectar un nuevo bimestre.
    """

    _attr_name = "Lectura Base de Exportacion"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "baseline_export")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            return self.coordinator.data.get("export_baseline")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "bimestre_inicio": data.get("bimestre_inicio"),
            "descripcion": "Lectura del medidor al inicio del bimestre (punto cero de exportacion).",
        }
