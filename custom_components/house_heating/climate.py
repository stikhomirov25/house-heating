import asyncio
import logging
import math
import time
from datetime import timedelta, datetime, timezone

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode, HVACAction
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import Context, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .zones import Room, GROUPS, GROUP_NAMES, average, demands, outputs, HYSTERESIS_C

_LOG = logging.getLogger(__name__)
INVALID = {"unavailable", "unknown"}
FLOOR_TARGET_C = 33.0


async def async_setup_entry(hass, entry, async_add_entities):
    control = Controller(hass, entry.data, entry.entry_id)
    control.hysteresis = entry.options.get("hysteresis", HYSTERESIS_C)
    saved = await control.store.async_load() or {}
    control.targets.update(saved.get("targets", {}))
    control.pause_until = saved.get("pause_until", 0)
    control.active = saved.get("active", False) and saved.get("mapping") == control.mapping
    if control.active:
        control.requests.update({k: saved.get("requests", {}).get(k) is True for k in control.rooms})
    entities = [Thermostat(control, key) for key in GROUPS]
    control.entities = entities
    async_add_entities(entities)
    watched = list(control.outputs(dict.fromkeys(control.rooms, False)))
    watched.extend(s for room in control.rooms.values() for s in room.sensors)
    entry.async_on_unload(async_track_state_change_event(hass, watched, control.changed))
    entry.async_on_unload(async_track_time_interval(hass, control.tick, timedelta(seconds=5)))
    entry.async_on_unload(control.stop)


class Controller:
    def __init__(self, hass, config, entry_id):
        self.mapping = dict(config)
        self.rooms = {key: Room(**room) for key, room in config["rooms"].items()}
        self.floor = config["floor"]
        self.boiler = config["boiler"]
        self.protected = {self.boiler, self.floor, *(r.floor_climate for r in self.rooms.values() if r.floor_climate)}
        self.hass = hass
        self.store = Store(hass, 1, f"house_heating.targets.{entry_id}")
        self.targets = dict.fromkeys(self.rooms, 19.0)
        self.hysteresis = HYSTERESIS_C
        self.requests = dict.fromkeys(self.rooms, False)
        self.entities = []
        self.active = False
        self.pause_until = 0
        self.expected_off = {}
        self.last_command = {}
        self.task = None
        self.revision = 0
        self.closed = False

    def outputs(self, heat):
        return outputs(heat, self.rooms, self.floor, self.boiler)

    def snapshot(self):
        return {"mapping": self.mapping, "requests": dict(self.requests), "targets": dict(self.targets), "pause_until": self.pause_until, "active": self.active}

    def save(self):
        self.store.async_delay_save(self.snapshot, 0)

    def reading(self, key):
        values = []
        for sensor in self.rooms[key].sensors:
            state = self.hass.states.get(sensor)
            if state:
                try:
                    value = float(state.state)
                except ValueError:
                    continue
                if state.attributes.get("unit_of_measurement") == "°F":
                    value = (value - 32) * 5 / 9
                if math.isfinite(value):
                    values.append(value)
        return average(values)

    def heat(self):
        return demands(self.targets, {k: self.reading(k) for k in self.rooms}, self.requests, self.hysteresis)

    def update_requests(self):
        if not self.active:
            return
        readings = {k: self.reading(k) for k in self.rooms}
        next_requests = demands(self.targets, readings, self.requests, self.hysteresis)
        # Missing sensors suppress output. Keep the last latch through startup
        # so a sensor returning inside the band resumes its previous cycle.
        for key, temperature in readings.items():
            if temperature is None:
                next_requests[key] = self.requests[key]
        if self.requests != next_requests:
            self.requests = next_requests
            self.save()

    @property
    def paused(self):
        return time.time() < self.pause_until

    @callback
    def changed(self, event):
        old, new = (event.data.get(k) for k in ("old_state", "new_state"))
        if not new:
            return
        key = new.entity_id
        if old and (old.state != new.state or old.attributes.get("temperature") != new.attributes.get("temperature")):
            self.last_command.pop(key, None)
        if (self.active and key in self.protected and new.state == "off"
                and old and old.state not in INVALID | {"off"}):
            expected = self.expected_off.pop(key, None)
            own = expected and (new.context.id == expected[0] or
                                (not new.context.user_id and time.monotonic() < expected[1]))
            if not own:
                self.pause_until = time.time() + 1800
                self.save()
        self.update()

    @callback
    def tick(self, now):
        self.refresh()
        self.schedule()

    def refresh(self):
        self.update_requests()
        for entity in self.entities:
            if entity.hass is not None and entity.entity_id and entity.ready:
                entity.async_write_ha_state()

    def update(self):
        self.revision += 1
        self.refresh()
        self.schedule()

    def schedule(self):
        if not self.closed and (self.task is None or self.task.done()):
            self.task = self.hass.async_create_task(self.run())

    def stop(self):
        self.closed = True
        if self.task:
            self.task.cancel()

    async def run(self):
        while self.active and not self.paused and not self.closed:
            revision = self.revision
            desired = self.outputs(self.heat())
            # Stop the boiler before closing paths; open paths before starting it.
            order = list(desired) if desired[self.boiler] else [self.boiler, *(k for k in desired if k != self.boiler)]
            for key in order:
                if self.paused or self.closed:
                    return
                if revision != self.revision:
                    # Device acknowledgements can arrive during a blocking call.
                    # Restart only if the required outputs actually changed.
                    if desired != self.outputs(self.heat()):
                        break
                    revision = self.revision
                try:
                    await self.apply(key, desired[key])
                except (HomeAssistantError, ValueError, TypeError) as err:
                    _LOG.warning("Ошибка управления %s: %s", key, err)
            if revision == self.revision:
                return
            await asyncio.sleep(0)

    async def apply(self, key, on):
        state = self.hass.states.get(key)
        if not state or state.state in INVALID:
            return
        domain = key.split(".")[0]
        data = {"entity_id": key}
        if domain == "climate":
            modes = state.attributes.get("hvac_modes", ())
            radiator = key not in self.protected
            mode = "heat" if on or (radiator and "off" not in modes) else "off"
            if on and not radiator and "heat" not in modes and "heat_cool" in modes:
                mode = "heat_cool"
            if mode not in modes:
                raise ValueError(f"Устройство не поддерживает режим {mode}")
            floor_target = None
            if on and not radiator:
                # Climate service temperatures use Home Assistant's display unit.
                floor_target = FLOOR_TARGET_C
                if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
                    floor_target = round(FLOOR_TARGET_C * 9 / 5 + 32, 1)
                if not state.attributes.get("min_temp", -math.inf) <= floor_target <= state.attributes.get("max_temp", math.inf):
                    raise ValueError("33 °C вне допустимого диапазона уставок регулятора пола")
            if state.state != mode:
                service = "set_hvac_mode"
                data["hvac_mode"] = mode
            elif floor_target is not None and state.attributes.get("temperature") != floor_target:
                service = "set_temperature"
                data["temperature"] = floor_target
            elif radiator and mode == "heat":
                target = state.attributes.get("max_temp" if on else "min_temp")
                if target is None:
                    raise ValueError("Границы уставки термоголовки недоступны")
                if state.attributes.get("temperature") == target:
                    return
                service = "set_temperature"
                data["temperature"] = target
            else:
                return
        else:
            mode = "on" if on else "off"
            if state.state == mode:
                return
            service = "turn_" + mode
        signature = (service, tuple(data.items()))
        now = time.monotonic()
        previous = self.last_command.get(key)
        if previous and previous[0] == signature and now - previous[1] < 5:
            return
        self.last_command[key] = (signature, now)
        context = Context()
        if key in self.protected:
            if not on:
                self.expected_off[key] = (context.id, now + 30)
            else:
                self.expected_off.pop(key, None)
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True, context=context)
        except Exception:
            self.expected_off.pop(key, None)
            raise


class Thermostat(ClimateEntity):
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_target_temperature_step = 1.0
    _attr_precision = 0.1
    _attr_min_temp = 5
    _attr_max_temp = 30

    def __init__(self, control, key):
        self.control = control
        self.key = key
        self.members = GROUPS[key]
        self.entity_id = f"climate.house_heating_{key}"
        self._attr_unique_id = f"house_heating_{key}"
        self._attr_name = control.rooms[key].name if key in control.rooms else GROUP_NAMES[key]
        self.ready = False

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.ready = True

    @property
    def current_temperature(self):
        return average(self.control.reading(k) for k in self.members)

    @property
    def target_temperature(self):
        return average(self.control.targets[k] for k in self.members)

    @property
    def hvac_action(self):
        if not self.control.active or self.control.paused:
            return HVACAction.IDLE
        heat = self.control.heat()
        return HVACAction.HEATING if any(heat[k] for k in self.members) else HVACAction.IDLE

    @property
    def extra_state_attributes(self):
        until = self.control.pause_until
        return {"pause_until": datetime.fromtimestamp(until, timezone.utc).isoformat() if self.control.paused else None,
                "control_active": self.control.active, "hysteresis": self.control.hysteresis}

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode != HVACMode.HEAT:
            raise HomeAssistantError("Поддерживается только отопление")

    async def async_set_temperature(self, **kwargs):
        target = float(kwargs[ATTR_TEMPERATURE])
        if not self.min_temp <= target <= self.max_temp:
            raise HomeAssistantError("Допустимый диапазон 5–30 °C")
        if not self.control.active and self.key != "home":
            raise HomeAssistantError("Для первого запуска задайте температуру всего дома")
        for key in self.members:
            self.control.targets[key] = target
        self.control.active = True
        self.control.save()
        self.control.update()
