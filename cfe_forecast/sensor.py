"""
Sensores y lógica de cálculo para CFE Forecast MX.

Este módulo implementa:
  - DataUpdateCoordinator: actualiza todos los datos cada N minutos.
  - Lógica de "cero virtual": captura lecturas iniciales y calcula deltas.
  - Bolsa de energía FIFO con expiración a 12 meses.
  - Cálculo progresivo de costo según escalones CFE.
  - Sensores monetarios, de energía, series temporales y binarios de alerta.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, CURRENCY_DOLLAR
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

# Unidad monetaria para México
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
    
    Recupera el coordinador registrado en __init__.py y crea todas
    las entidades asociadas a esta entrada de configuración.
    """
    coordinator: CFECoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # Lista completa de entidades que esta integración expone
    entities = [
        # ── Sensores Monetarios ──────────────────────────────────────────
        CFECostoActualSensor(coordinator, config_entry),
        CFEProyeccionSensor(coordinator, config_entry),

        # ── Sensores de Energía ──────────────────────────────────────────
        CFEConsumoNetoBimestreSensor(coordinator, config_entry),
        CFEBolsaEnergiaSensor(coordinator, config_entry),

        # ── Series Temporales (para gráficas de barras) ──────────────────
        CFEHistoricoImportSensor(coordinator, config_entry),
        CFEHistoricoExportSensor(coordinator, config_entry),
        CFEHistoricoNetoSensor(coordinator, config_entry),

        # ── Sensores Binarios de Alerta ──────────────────────────────────
        CFEAlertaExpiracionSensor(coordinator, config_entry),
        CFERiesgoDACBinarySensor(coordinator, config_entry),
    ]

    async_add_entities(entities)


# =============================================================================
# COORDINATOR: NÚCLEO DE ACTUALIZACIÓN
# =============================================================================

class CFECoordinator(DataUpdateCoordinator):
    """
    Coordinador central de CFE Forecast MX.
    
    Responsabilidades:
      1. Leer los sensores de HA (import / export) cada N minutos.
      2. Calcular el delta respecto al inicio del bimestre (cero virtual).
      3. Actualizar la bolsa de energía (FIFO, expiración 12 meses).
      4. Calcular el costo progresivo según escalones CFE.
      5. Mantener series temporales para gráficas.
      6. Persistir el estado en el Store de HA para sobrevivir reinicios.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        store,
    ) -> None:
        """
        Inicializa el coordinador.
        
        Args:
            hass: Instancia de Home Assistant.
            config_entry: Entrada de configuración de esta integración.
            store: Objeto Store de HA para persistencia de datos.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.config_entry = config_entry
        self._store = store

        # ── Estado persistido (se carga desde el Store al iniciar) ───────
        # Lectura del sensor de importación al inicio del bimestre
        self._import_baseline: float = 0.0
        # Lectura del sensor de exportación al inicio del bimestre
        self._export_baseline: float = 0.0
        # Indica si ya se capturaron las lecturas base del bimestre actual
        self._baseline_captured: bool = False
        # Fecha en que inició el bimestre actual (para calcular días transcurridos)
        self._bimestre_start: date | None = None

        # ── Bolsa de energía (lista de depósitos FIFO) ───────────────────
        # Cada elemento: {"kwh": float, "date": "YYYY-MM-DD"}
        self._bolsa_depositos: list[dict] = []

        # ── Series temporales (diccionarios fecha → kWh) ─────────────────
        self._daily_import: dict[str, float] = {}  # Importación por día
        self._daily_export: dict[str, float] = {}  # Exportación por día
        self._daily_net: dict[str, float] = {}     # Neto por día

        # ── Última lectura registrada (para calcular delta diario) ────────
        self._last_import_reading: float | None = None
        self._last_export_reading: float | None = None
        self._last_reading_date: date | None = None

        # ── Resultados calculados (expuestos a los sensores) ─────────────
        self.data: dict[str, Any] = self._empty_data()

    def _empty_data(self) -> dict[str, Any]:
        """Retorna un diccionario de datos vacío con valores seguros por defecto."""
        return {
            "consumo_neto_kwh": 0.0,       # kWh netos consumidos en el bimestre
            "bolsa_total_kwh": 0.0,         # kWh disponibles en la bolsa
            "costo_sin_iva": 0.0,           # Costo calculado antes de IVA
            "costo_con_iva": 0.0,           # Costo final con IVA y cargo fijo
            "proyeccion_kwh": 0.0,          # Proyección de kWh al final del bimestre
            "proyeccion_costo": 0.0,        # Proyección de costo final
            "dias_transcurridos": 0,        # Días desde inicio del bimestre
            "dias_restantes": 0,            # Días que faltan para el corte
            "daily_import": {},             # Serie temporal de importación
            "daily_export": {},             # Serie temporal de exportación
            "daily_net": {},                # Serie temporal de neto
            "bolsa_proxima_vencer_kwh": 0.0,  # kWh que vencen pronto
            "bolsa_proxima_vencer_fecha": None,  # Fecha del próximo vencimiento
            "en_riesgo_dac": False,         # ¿Supera límite para tarifa DAC?
            "alerta_expiracion": False,     # ¿Hay kWh por vencer en 30 días?
            "tariff": "1C",
        }

    # ────────────────────────────────────────────────────────────────────────
    # CARGA Y GUARDADO DE ESTADO PERSISTENTE
    # ────────────────────────────────────────────────────────────────────────

    async def async_load_state(self) -> None:
        """
        Carga el estado guardado desde el Store de Home Assistant.
        Esto garantiza que la bolsa de energía y las lecturas base
        no se pierdan al reiniciar HA.
        """
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

        # Restaurar lecturas base del bimestre
        self._import_baseline = state.get("import_baseline", 0.0)
        self._export_baseline = state.get("export_baseline", 0.0)
        self._baseline_captured = state.get("baseline_captured", False)

        # Restaurar fecha de inicio del bimestre
        bimestre_start_str = state.get("bimestre_start")
        if bimestre_start_str:
            self._bimestre_start = date.fromisoformat(bimestre_start_str)

        # Restaurar depósitos de la bolsa de energía
        self._bolsa_depositos = state.get("bolsa_depositos", [])

        # Restaurar series temporales
        self._daily_import = state.get("daily_import", {})
        self._daily_export = state.get("daily_export", {})
        self._daily_net = state.get("daily_net", {})

        # Restaurar última lectura para calcular delta diario correcto
        self._last_import_reading = state.get("last_import_reading")
        self._last_export_reading = state.get("last_export_reading")
        last_date_str = state.get("last_reading_date")
        if last_date_str:
            self._last_reading_date = date.fromisoformat(last_date_str)

    async def async_save_state(self) -> None:
        """
        Guarda el estado actual en el Store de Home Assistant.
        Se llama después de cada actualización exitosa.
        """
        # Leer el contenido actual del store para no sobreescribir otras entradas
        stored = await self._store.async_load() or {}

        # Serializar la fecha de bimestre como string ISO
        bimestre_start_str = (
            self._bimestre_start.isoformat() if self._bimestre_start else None
        )
        last_date_str = (
            self._last_reading_date.isoformat() if self._last_reading_date else None
        )

        stored[self.config_entry.entry_id] = {
            "import_baseline": self._import_baseline,
            "export_baseline": self._export_baseline,
            "baseline_captured": self._baseline_captured,
            "bimestre_start": bimestre_start_str,
            "bolsa_depositos": self._bolsa_depositos,
            "daily_import": self._daily_import,
            "daily_export": self._daily_export,
            "daily_net": self._daily_net,
            "last_import_reading": self._last_import_reading,
            "last_export_reading": self._last_export_reading,
            "last_reading_date": last_date_str,
        }

        await self._store.async_save(stored)
        _LOGGER.debug("[CFE] Estado guardado en Store.")

    # ────────────────────────────────────────────────────────────────────────
    # DETECCIÓN DEL BIMESTRE ACTUAL
    # ────────────────────────────────────────────────────────────────────────

    def _calcular_inicio_bimestre(self) -> date:
        """
        Calcula la fecha de inicio del bimestre actual basándose en:
          - El día de corte configurado (día del mes).
          - El mes de inicio del ciclo bimestral.
        
        La CFE factura en ciclos de 2 meses. Si el mes de inicio es Enero,
        los bimestres son: Ene-Feb, Mar-Abr, May-Jun, Jul-Ago, Sep-Oct, Nov-Dic.
        
        Returns:
            Fecha de inicio del bimestre en curso.
        """
        cfg = {**self.config_entry.data, **self.config_entry.options}
        cut_day: int = int(cfg.get(CONF_CUT_DAY, 1))
        start_month: int = int(cfg.get(CONF_START_MONTH, 1))

        today = date.today()

        # Calcular qué bimestre corresponde al mes actual
        # Los meses válidos de inicio de bimestre son cada 2 meses a partir de start_month
        meses_inicio = [(start_month + i * 2 - 1) % 12 + 1 for i in range(6)]

        # Buscar el mes de inicio del bimestre actual
        mes_bimestre = today.month
        for mes in sorted(meses_inicio, reverse=True):
            if mes <= today.month:
                mes_bimestre = mes
                break
        else:
            # Si ningún mes es ≤ hoy, el bimestre empezó en el año anterior
            mes_bimestre = meses_inicio[-1]

        # Construir la fecha de inicio con el día de corte
        try:
            inicio = date(today.year, mes_bimestre, cut_day)
        except ValueError:
            # Si el día de corte no existe en ese mes (ej: 31 en Febrero), usar el último día
            import calendar
            ultimo_dia = calendar.monthrange(today.year, mes_bimestre)[1]
            inicio = date(today.year, mes_bimestre, min(cut_day, ultimo_dia))

        # Si la fecha calculada es futura, retroceder 2 meses
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
        """
        Calcula la fecha de fin del bimestre sumando 2 meses al inicio.
        
        Args:
            inicio: Fecha de inicio del bimestre.
            
        Returns:
            Fecha de corte (fin) del bimestre.
        """
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
        """
        Verifica si hoy corresponde a un nuevo bimestre diferente al registrado.
        
        Returns:
            True si el bimestre actual es diferente al almacenado.
        """
        inicio_actual = self._calcular_inicio_bimestre()
        if self._bimestre_start is None:
            return True
        return inicio_actual != self._bimestre_start

    # ────────────────────────────────────────────────────────────────────────
    # BOLSA DE ENERGÍA (FIFO + EXPIRACIÓN)
    # ────────────────────────────────────────────────────────────────────────

    def _limpiar_bolsa_expirada(self) -> None:
        """
        Elimina de la bolsa los depósitos de kWh que tengan más de 12 meses
        de antigüedad. Implementa la lógica de expiración según política CFE.
        """
        limite_expiracion = date.today() - timedelta(days=BOLSA_EXPIRATION_MONTHS * 30)
        antes = sum(d["kwh"] for d in self._bolsa_depositos)

        self._bolsa_depositos = [
            deposito for deposito in self._bolsa_depositos
            if date.fromisoformat(deposito["date"]) > limite_expiracion
        ]

        despues = sum(d["kwh"] for d in self._bolsa_depositos)
        if antes > despues:
            _LOGGER.info(
                "[CFE] Se expiraron %.2f kWh de la bolsa de energía.", antes - despues
            )

    def _agregar_a_bolsa(self, kwh: float) -> None:
        """
        Añade un depósito de kWh a la bolsa con la fecha de hoy.
        
        Args:
            kwh: Cantidad de kWh a depositar (debe ser positivo).
        """
        if kwh <= 0:
            return
        self._bolsa_depositos.append({
            "kwh": round(kwh, 3),
            "date": date.today().isoformat(),
        })
        _LOGGER.debug("[CFE] Depositados %.3f kWh en bolsa.", kwh)

    def _consumir_de_bolsa(self, kwh_necesarios: float) -> float:
        """
        Consume kWh de la bolsa usando lógica FIFO (los más antiguos primero).
        
        Args:
            kwh_necesarios: kWh que se desean consumir de la bolsa.
            
        Returns:
            kWh que NO pudieron cubrirse con la bolsa (remanente a cobrar).
        """
        remanente = kwh_necesarios

        for deposito in self._bolsa_depositos:
            if remanente <= 0:
                break
            disponible = deposito["kwh"]
            consumir = min(disponible, remanente)
            deposito["kwh"] -= consumir
            remanente -= consumir

        # Limpiar depósitos agotados
        self._bolsa_depositos = [
            d for d in self._bolsa_depositos if d["kwh"] > 0.001
        ]

        return max(0.0, remanente)

    def _total_bolsa(self) -> float:
        """Retorna el total de kWh disponibles en la bolsa."""
        return round(sum(d["kwh"] for d in self._bolsa_depositos), 3)

    def _info_proxima_expiracion(self) -> tuple[float, date | None]:
        """
        Busca el depósito más antiguo de la bolsa para alertar sobre vencimiento.
        
        Returns:
            Tupla (kwh_por_vencer, fecha_vencimiento) del depósito más antiguo.
        """
        if not self._bolsa_depositos:
            return 0.0, None

        # Ordenar por fecha ascendente para encontrar el más antiguo
        depositos_ordenados = sorted(
            self._bolsa_depositos,
            key=lambda d: d["date"]
        )
        mas_antiguo = depositos_ordenados[0]
        fecha_deposito = date.fromisoformat(mas_antiguo["date"])
        fecha_vencimiento = fecha_deposito + timedelta(days=BOLSA_EXPIRATION_MONTHS * 30)

        return mas_antiguo["kwh"], fecha_vencimiento

    # ────────────────────────────────────────────────────────────────────────
    # CÁLCULO DE COSTO PROGRESIVO
    # ────────────────────────────────────────────────────────────────────────

    def _calcular_costo_progresivo(self, kwh_neto: float) -> float:
        """
        Aplica los escalones de precio CFE al consumo neto.
        
        Escalones:
          1. Básico:      primeros N kWh a precio básico.
          2. Intermedio:  siguientes M kWh a precio intermedio.
          3. Excedente:   todo lo que supere los escalones anteriores.
        
        Args:
            kwh_neto: kWh netos a cobrar (ya descontada la bolsa).
            
        Returns:
            Costo en pesos MXN sin IVA.
        """
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

        # ── Escalón Básico ───────────────────────────────────────────────
        kwh_basico = min(remanente, basico_limite)
        costo += kwh_basico * basico_precio
        remanente -= kwh_basico

        # ── Escalón Intermedio ───────────────────────────────────────────
        if remanente > 0:
            kwh_intermedio = min(remanente, intermedio_limite)
            costo += kwh_intermedio * intermedio_precio
            remanente -= kwh_intermedio

        # ── Escalón Excedente ────────────────────────────────────────────
        if remanente > 0:
            costo += remanente * excedente_precio

        return round(costo, 2)

    def _verificar_riesgo_dac(self, kwh_neto: float, dias_transcurridos: int) -> bool:
        """
        Verifica si el consumo actual proyecta superar el límite para tarifa DAC.
        
        La tarifa DAC en México aplica a hogares que consumen en promedio
        más de cierto límite mensual por 6 meses consecutivos. La lógica
        aproxima si el bimestre actual superará ese umbral.
        
        Args:
            kwh_neto: Consumo neto actual del bimestre.
            dias_transcurridos: Días desde inicio del bimestre.
            
        Returns:
            True si existe riesgo de entrar a tarifa DAC.
        """
        if dias_transcurridos <= 0:
            return False

        cfg = {**self.config_entry.data, **self.config_entry.options}
        basico_limite = float(cfg.get(CONF_BASIC_LIMIT, DEFAULT_BASIC_LIMIT))
        intermedio_limite = float(cfg.get(CONF_INTERMEDIATE_LIMIT, DEFAULT_INTERMEDIATE_LIMIT))

        # El límite DAC aproximado es 2x el límite básico + intermedio en el bimestre
        limite_dac_bimestre = (basico_limite + intermedio_limite) * 2

        # Proyectar consumo al final del bimestre (60 días)
        consumo_diario_promedio = kwh_neto / dias_transcurridos
        consumo_proyectado = consumo_diario_promedio * 60

        return consumo_proyectado > limite_dac_bimestre

    # ────────────────────────────────────────────────────────────────────────
    # LECTURA DE SENSORES Y ACTUALIZACIÓN PRINCIPAL
    # ────────────────────────────────────────────────────────────────────────

    def _leer_estado_sensor(self, entity_id: str) -> float | None:
        """
        Lee el estado actual de un sensor de HA y lo convierte a float.
        
        Args:
            entity_id: ID de la entidad a leer (ej: "sensor.mi_medidor").
            
        Returns:
            Valor numérico del sensor, o None si no está disponible.
        """
        if not entity_id:
            return None

        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            _LOGGER.warning("[CFE] Sensor %s no disponible (estado: %s).", entity_id, 
                           state.state if state else "None")
            return None

        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.error("[CFE] No se pudo convertir el estado de %s a número: %s",
                         entity_id, state.state)
            return None

    async def _async_update_data(self) -> dict[str, Any]:
        """
        Método principal de actualización llamado por el DataUpdateCoordinator.
        
        Flujo de ejecución:
          1. Leer sensores de HA.
          2. Detectar nuevo bimestre → capturar baseline.
          3. Calcular delta (consumo real del bimestre actual).
          4. Actualizar series temporales diarias.
          5. Gestionar bolsa de energía.
          6. Calcular costo progresivo.
          7. Generar proyección al final del bimestre.
          8. Calcular alertas.
          9. Guardar estado en el Store.
        
        Returns:
            Diccionario con todos los datos calculados para los sensores.
        """
        cfg = {**self.config_entry.data, **self.config_entry.options}

        # ── 1. Leer sensores ─────────────────────────────────────────────
        import_entity = cfg.get(CONF_IMPORT_SENSOR, "")
        export_entity = cfg.get(CONF_EXPORT_SENSOR, "")

        import_lectura = self._leer_estado_sensor(import_entity)
        export_lectura = self._leer_estado_sensor(export_entity) or 0.0

        if import_lectura is None:
            raise UpdateFailed(
                f"Sensor de importación {import_entity} no disponible."
            )

        hoy = date.today()

        # ── 2. Detectar inicio de nuevo bimestre ─────────────────────────
        if self._nuevo_bimestre_detectado():
            nuevo_inicio = self._calcular_inicio_bimestre()
            _LOGGER.info(
                "[CFE] Nuevo bimestre detectado. Inicio: %s. "
                "Capturando lecturas base (import=%.3f, export=%.3f).",
                nuevo_inicio, import_lectura, export_lectura
            )
            # Capturar las lecturas actuales como punto de referencia (cero virtual)
            self._import_baseline = import_lectura
            self._export_baseline = export_lectura
            self._baseline_captured = True
            self._bimestre_start = nuevo_inicio

            # Reiniciar series temporales para el nuevo bimestre
            self._daily_import = {}
            self._daily_export = {}
            self._daily_net = {}

            # Limpiar depósitos expirados al inicio del nuevo bimestre
            self._limpiar_bolsa_expirada()

            # Bolsa inicial si el usuario la configuró
            inicial = float(cfg.get(CONF_INITIAL_BAG, 0.0))
            if inicial > 0 and not self._bolsa_depositos:
                self._agregar_a_bolsa(inicial)

        # ── 3. Calcular delta del bimestre (cero virtual) ────────────────
        if not self._baseline_captured:
            # Primera ejecución: capturar baseline sin calcular consumo aún
            self._import_baseline = import_lectura
            self._export_baseline = export_lectura
            self._baseline_captured = True
            self._bimestre_start = self._calcular_inicio_bimestre()
            _LOGGER.info(
                "[CFE] Primera captura de baseline. Import: %.3f, Export: %.3f",
                self._import_baseline, self._export_baseline
            )

        # Delta = Lectura actual - Lectura al inicio del bimestre
        delta_import = max(0.0, import_lectura - self._import_baseline)
        delta_export = max(0.0, export_lectura - self._export_baseline)

        # Consumo neto = Lo que se tomó de la red - Lo que se inyectó
        consumo_neto = delta_import - delta_export

        # ── 4. Actualizar series temporales diarias ──────────────────────
        hoy_str = hoy.isoformat()

        if self._last_reading_date is not None and self._last_reading_date != hoy:
            # Nuevo día: calcular delta desde la última lectura
            delta_import_hoy = max(
                0.0, import_lectura - (self._last_import_reading or import_lectura)
            )
            delta_export_hoy = max(
                0.0, export_lectura - (self._last_export_reading or export_lectura)
            )
            delta_neto_hoy = delta_import_hoy - delta_export_hoy

            # Acumular en la fecha del día anterior (día que acaba de terminar)
            fecha_ayer = self._last_reading_date.isoformat()
            self._daily_import[fecha_ayer] = round(
                self._daily_import.get(fecha_ayer, 0.0) + delta_import_hoy, 3
            )
            self._daily_export[fecha_ayer] = round(
                self._daily_export.get(fecha_ayer, 0.0) + delta_export_hoy, 3
            )
            self._daily_net[fecha_ayer] = round(
                self._daily_net.get(fecha_ayer, 0.0) + delta_neto_hoy, 3
            )

        # Actualizar última lectura registrada
        self._last_import_reading = import_lectura
        self._last_export_reading = export_lectura
        self._last_reading_date = hoy

        # ── 5. Gestionar bolsa de energía ────────────────────────────────
        # Si el consumo neto es negativo, tenemos excedente → depositar en bolsa
        if consumo_neto < 0:
            kwh_excedente = abs(consumo_neto)
            # Limpiar depósitos anteriores del bimestre actual y reemplazar
            # (simplificación: el excedente neto del bimestre es un solo depósito)
            # Para mayor precisión se podría actualizar día a día
            self._agregar_a_bolsa(kwh_excedente)
            kwh_a_cobrar = 0.0
        else:
            # Consumo positivo: intentar cubrir con la bolsa primero
            kwh_a_cobrar = self._consumir_de_bolsa(consumo_neto)

        # ── 6. Calcular costo progresivo ─────────────────────────────────
        iva = float(cfg.get(CONF_IVA, DEFAULT_IVA))
        cargo_fijo = float(cfg.get(CONF_FIXED_CHARGE, DEFAULT_FIXED_CHARGE))

        costo_sin_iva = self._calcular_costo_progresivo(kwh_a_cobrar)
        costo_con_iva = round((costo_sin_iva + cargo_fijo) * (1 + iva), 2)

        # ── 7. Proyección al final del bimestre ──────────────────────────
        inicio_bimestre = self._bimestre_start or hoy
        fin_bimestre = self._calcular_fin_bimestre(inicio_bimestre)
        dias_totales = max(1, (fin_bimestre - inicio_bimestre).days)
        dias_transcurridos = max(1, (hoy - inicio_bimestre).days)
        dias_restantes = max(0, (fin_bimestre - hoy).days)

        # Proyección lineal basada en el promedio diario
        consumo_diario_promedio = consumo_neto / dias_transcurridos
        consumo_proyectado = consumo_diario_promedio * dias_totales
        kwh_proyectado_a_cobrar = self._consumir_de_bolsa_simulado(consumo_proyectado)
        costo_proyectado_sin_iva = self._calcular_costo_progresivo(kwh_proyectado_a_cobrar)
        costo_proyectado = round((costo_proyectado_sin_iva + cargo_fijo) * (1 + iva), 2)

        # ── 8. Calcular alertas ──────────────────────────────────────────
        kwh_por_vencer, fecha_vencimiento = self._info_proxima_expiracion()

        alerta_expiracion = False
        if fecha_vencimiento:
            dias_para_vencer = (fecha_vencimiento - hoy).days
            alerta_expiracion = dias_para_vencer <= BOLSA_EXPIRATION_ALERT_DAYS

        en_riesgo_dac = self._verificar_riesgo_dac(consumo_neto, dias_transcurridos)

        # ── 9. Guardar estado ────────────────────────────────────────────
        await self.async_save_state()

        # Retornar todos los datos calculados
        resultado = {
            "consumo_neto_kwh": round(consumo_neto, 3),
            "bolsa_total_kwh": self._total_bolsa(),
            "costo_sin_iva": round(costo_sin_iva, 2),
            "costo_con_iva": costo_con_iva,
            "proyeccion_kwh": round(consumo_proyectado, 3),
            "proyeccion_costo": costo_proyectado,
            "dias_transcurridos": dias_transcurridos,
            "dias_restantes": dias_restantes,
            "daily_import": dict(self._daily_import),
            "daily_export": dict(self._daily_export),
            "daily_net": dict(self._daily_net),
            "bolsa_proxima_vencer_kwh": round(kwh_por_vencer, 3),
            "bolsa_proxima_vencer_fecha": (
                fecha_vencimiento.isoformat() if fecha_vencimiento else None
            ),
            "en_riesgo_dac": en_riesgo_dac,
            "alerta_expiracion": alerta_expiracion,
            "tariff": cfg.get(CONF_TARIFF, "1C"),
            "bimestre_inicio": inicio_bimestre.isoformat(),
            "bimestre_fin": fin_bimestre.isoformat(),
        }

        _LOGGER.debug("[CFE] Actualización completada: %s", resultado)
        return resultado

    def _consumir_de_bolsa_simulado(self, kwh: float) -> float:
        """
        Simula el consumo de la bolsa para calcular proyecciones
        sin modificar el estado real de la bolsa.
        
        Args:
            kwh: kWh de consumo proyectado.
            
        Returns:
            kWh que habría que pagar después de usar la bolsa.
        """
        if kwh <= 0:
            return 0.0
        bolsa_disponible = self._total_bolsa()
        return max(0.0, kwh - bolsa_disponible)


# =============================================================================
# CLASE BASE PARA TODOS LOS SENSORES
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
        """
        Inicializa el sensor base.
        
        Args:
            coordinator: Instancia del coordinador.
            config_entry: Entrada de configuración de la integración.
            unique_id_suffix: Sufijo único para distinguir este sensor.
        """
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{unique_id_suffix}"

    @property
    def device_info(self) -> dict:
        """Información del dispositivo virtual para agrupar entidades en HA."""
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "CFE Forecast MX",
            "manufacturer": "CFE (Comisión Federal de Electricidad)",
            "model": f"Tarifa {self.coordinator.data.get('tariff', '?')}",
            "entry_type": "service",
        }


# =============================================================================
# SENSORES MONETARIOS
# =============================================================================

class CFECostoActualSensor(CFEBaseSensor):
    """
    Sensor: Costo actual del recibo (con IVA y cargo fijo).
    
    Muestra el costo acumulado en el bimestre en curso,
    calculado según los escalones progresivos de la CFE.
    """

    _attr_name = "Costo Actual del Bimestre"
    _attr_icon = "mdi:cash"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UNIT_PESOS

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "costo_actual")

    @property
    def native_value(self) -> float | None:
        """Retorna el costo acumulado con IVA en pesos MXN."""
        if self.coordinator.data:
            return self.coordinator.data.get("costo_con_iva")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Atributos adicionales para información detallada del costo."""
        data = self.coordinator.data or {}
        return {
            "costo_sin_iva": data.get("costo_sin_iva"),
            "iva_aplicado": data.get("iva_porcentaje"),
            "cargo_fijo": data.get("cargo_fijo"),
            "dias_transcurridos": data.get("dias_transcurridos"),
            "dias_restantes": data.get("dias_restantes"),
            "bimestre_inicio": data.get("bimestre_inicio"),
            "bimestre_fin": data.get("bimestre_fin"),
            "tarifa": data.get("tariff"),
        }


class CFEProyeccionSensor(CFEBaseSensor):
    """
    Sensor: Proyección del recibo final.
    
    Extrapola el consumo actual al final del bimestre usando el
    promedio diario de consumo. Útil para anticipar el monto del recibo.
    """

    _attr_name = "Proyección del Recibo Final"
    _attr_icon = "mdi:cash-clock"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UNIT_PESOS

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "proyeccion_recibo")

    @property
    def native_value(self) -> float | None:
        """Retorna el costo proyectado al final del bimestre."""
        if self.coordinator.data:
            return self.coordinator.data.get("proyeccion_costo")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "consumo_proyectado_kwh": data.get("proyeccion_kwh"),
            "dias_restantes": data.get("dias_restantes"),
            "promedio_diario_kwh": (
                round(data.get("consumo_neto_kwh", 0) / max(1, data.get("dias_transcurridos", 1)), 3)
            ),
        }


# =============================================================================
# SENSORES DE ENERGÍA
# =============================================================================

class CFEConsumoNetoBimestreSensor(CFEBaseSensor):
    """
    Sensor: Consumo neto del bimestre en kWh.
    
    Representa el consumo real (importado - exportado) desde el inicio
    del ciclo de facturación actual. Puede ser negativo si se exportó más.
    """

    _attr_name = "Consumo Neto del Bimestre"
    _attr_icon = "mdi:lightning-bolt"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
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
    Sensor: Bolsa de energía disponible en kWh.
    
    Muestra los kWh acumulados a favor del usuario, generalmente
    por excedentes de paneles solares. Se aplican antes de cobrar.
    """

    _attr_name = "Bolsa de Energía Disponible"
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
            "meses_vigencia": 12,
        }


# =============================================================================
# SENSORES DE SERIES TEMPORALES (GRÁFICAS)
# =============================================================================

class CFEHistoricoImportSensor(CFEBaseSensor):
    """
    Sensor: Importación diaria de energía de la red.
    
    El estado muestra el total del bimestre. Los atributos contienen
    la serie temporal diaria para visualizar en gráficas de barras
    mediante la tarjeta 'apexcharts-card' u otras tarjetas compatibles.
    """

    _attr_name = "Importación Diaria (Serie)"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "historico_import")

    @property
    def native_value(self) -> float | None:
        """Total importado en el bimestre (suma de la serie temporal)."""
        if self.coordinator.data:
            serie = self.coordinator.data.get("daily_import", {})
            return round(sum(serie.values()), 3) if serie else 0.0
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """
        La clave 'serie_diaria' contiene una lista de objetos
        [{fecha, kwh}] lista que las tarjetas de gráficas pueden consumir.
        """
        data = self.coordinator.data or {}
        serie = data.get("daily_import", {})
        return {
            "serie_diaria": [
                {"fecha": fecha, "kwh": kwh}
                for fecha, kwh in sorted(serie.items())
            ],
            "total_kwh": round(sum(serie.values()), 3) if serie else 0.0,
        }


class CFEHistoricoExportSensor(CFEBaseSensor):
    """
    Sensor: Exportación diaria de energía (generación solar).
    
    Registra cuántos kWh se inyectaron a la red cada día del bimestre.
    """

    _attr_name = "Exportación Diaria (Serie)"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "historico_export")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            serie = self.coordinator.data.get("daily_export", {})
            return round(sum(serie.values()), 3) if serie else 0.0
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        serie = data.get("daily_export", {})
        return {
            "serie_diaria": [
                {"fecha": fecha, "kwh": kwh}
                for fecha, kwh in sorted(serie.items())
            ],
            "total_kwh": round(sum(serie.values()), 3) if serie else 0.0,
        }


class CFEHistoricoNetoSensor(CFEBaseSensor):
    """
    Sensor: Consumo neto diario (import - export).
    
    Valores negativos indican días donde se generó más de lo que se consumió.
    Ideal para gráficas de barras con colores diferenciados.
    """

    _attr_name = "Neto Diario (Serie)"
    _attr_icon = "mdi:chart-bar"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry, "historico_neto")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            serie = self.coordinator.data.get("daily_net", {})
            return round(sum(serie.values()), 3) if serie else 0.0
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        serie = data.get("daily_net", {})
        return {
            "serie_diaria": [
                {"fecha": fecha, "kwh": kwh}
                for fecha, kwh in sorted(serie.items())
            ],
            "dias_positivos": sum(1 for v in serie.values() if v > 0),
            "dias_negativos": sum(1 for v in serie.values() if v < 0),
            "maximo_dia": max(serie.values(), default=0),
            "minimo_dia": min(serie.values(), default=0),
        }


# =============================================================================
# SENSORES BINARIOS DE ALERTA
# =============================================================================

class CFEAlertaExpiracionSensor(CoordinatorEntity, BinarySensorEntity):
    """
    Sensor Binario: Alerta por kWh de bolsa próximos a vencer.
    
    Se activa cuando hay kWh en la bolsa que vencerán en los próximos
    30 días. Permite al usuario consumirlos antes de perderlos.
    """

    _attr_has_entity_name = True
    _attr_name = "Alerta Expiración de Bolsa"
    _attr_icon = "mdi:clock-alert"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: CFECoordinator, config_entry: ConfigEntry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_alerta_expiracion"

    @property
    def is_on(self) -> bool | None:
        """True si hay kWh por vencer en los próximos 30 días."""
        if self.coordinator.data:
            return self.coordinator.data.get("alerta_expiracion", False)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "kwh_por_vencer": data.get("bolsa_proxima_vencer_kwh"),
            "fecha_vencimiento": data.get("bolsa_proxima_vencer_fecha"),
            "dias_para_vencer": (
                (
                    date.fromisoformat(data["bolsa_proxima_vencer_fecha"]) - date.today()
                ).days
                if data.get("bolsa_proxima_vencer_fecha")
                else None
            ),
        }

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "CFE Forecast MX",
        }


class CFERiesgoDACBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """
    Sensor Binario: Riesgo de cambio a tarifa DAC.
    
    Se activa cuando la proyección de consumo indica que el hogar
    podría ser reclasificado en tarifa DAC (Doméstica de Alto Consumo),
    que es significativamente más cara que las tarifas domésticas.
    """

    _attr_has_entity_name = True
    _attr_name = "Riesgo de Tarifa DAC"
    _attr_icon = "mdi:alert-circle"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: CFECoordinator, config_entry: ConfigEntry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_riesgo_dac"

    @property
    def is_on(self) -> bool | None:
        """True si el consumo proyectado supera el umbral DAC."""
        if self.coordinator.data:
            return self.coordinator.data.get("en_riesgo_dac", False)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "consumo_actual_kwh": data.get("consumo_neto_kwh"),
            "consumo_proyectado_kwh": data.get("proyeccion_kwh"),
            "tarifa_actual": data.get("tariff"),
            "descripcion": (
                "El consumo proyectado supera el límite para tarifa DAC. "
                "Considere reducir el consumo para evitar tarifas más altas."
                if data.get("en_riesgo_dac")
                else "Consumo dentro de límites normales."
            ),
        }

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "CFE Forecast MX",
        }
