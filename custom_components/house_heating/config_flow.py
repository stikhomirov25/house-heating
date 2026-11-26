from copy import deepcopy
import json
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import entity_registry as er, selector

from .zones import SLOTS


def entities(domains, multiple=False):
    return selector.EntitySelector(selector.EntitySelectorConfig(
        domain=domains, multiple=multiple,
    ))


class HeatingFlow(config_entries.ConfigFlow, domain="house_heating"):
    VERSION = 1

    def __init__(self):
        self.settings = {"rooms": {}}
        self.edit_entry = None
        self.slot = next(iter(SLOTS))

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("house_heating")
        self._abort_if_unique_id_configured()
        return self.async_show_menu(step_id="user", menu_options=["shared", "import_map"])

    async def async_step_import_map(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                data = json.loads(user_input["mapping"])
                self.settings = self.validate_import(data)
            except (TypeError, KeyError, ValueError, AttributeError):
                errors["base"] = "invalid_map"
            else:
                return self.finish()
        return self.async_show_form(step_id="import_map", errors=errors, data_schema=vol.Schema({
            vol.Required("mapping"): selector.TextSelector(selector.TextSelectorConfig(multiline=True))
        }))

    def validate_import(self, data):
        def entity(key, domains):
            if not isinstance(key, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", key):
                raise ValueError
            if key.split(".")[0] not in domains or self.hass.states.get(key) is None:
                raise ValueError
            return key

        boiler = entity(data["boiler"], ("light", "switch"))
        floor = entity(data["floor"], ("light", "switch"))
        if boiler == floor or set(data["rooms"]) != set(SLOTS):
            raise ValueError
        rooms = {}
        assigned = {boiler, floor}
        registry = er.async_get(self.hass)
        for key, level in SLOTS.items():
            raw = data["rooms"][key]
            if raw["floor"] != level or not isinstance(raw["name"], str) or not raw["name"].strip():
                raise ValueError
            sensors = [entity(s, ("sensor",)) for s in raw["sensors"]]
            radiators = [entity(s, ("climate",)) for s in raw.get("radiators", [])]
            heater = raw.get("floor_climate", "")
            devices = list(radiators)
            if heater:
                devices.append(entity(heater, ("climate", "switch")))
            if not sensors or (level == 2 and not devices) or (level == 1 and heater):
                raise ValueError
            for device in devices:
                item = registry.async_get(device)
                if device in assigned or (item and item.platform == "house_heating"):
                    raise ValueError
                assigned.add(device)
            rooms[key] = dict(name=raw["name"].strip(), floor=level, sensors=sensors,
                              radiators=radiators, floor_climate=heater)
        return dict(boiler=boiler, floor=floor, rooms=rooms)

    async def async_step_reconfigure(self, user_input=None):
        self.edit_entry = self._get_reconfigure_entry()
        self.settings.update(deepcopy(dict(self.edit_entry.data)))
        if set(self.settings["rooms"]) != set(SLOTS):
            return await self.async_step_shared()
        choices = {"shared": "Котёл и общий пол"}
        choices.update({k: f'{r["floor"]} эт. · {r["name"]}' for k, r in self.settings["rooms"].items()})
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema({
                vol.Required("section"): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[{"value": k, "label": v} for k, v in choices.items()]
                ))
            }))
        section = user_input["section"]
        if section == "shared":
            return await self.async_step_shared()
        self.slot = section
        return await self.async_step_room()

    def assigned(self, skip=None):
        devices = set()
        for key, room in self.settings["rooms"].items():
            if key != skip:
                devices.update(room.get("radiators", []))
                devices.add(room.get("floor_climate", ""))
        return devices

    async def async_step_shared(self, user_input=None):
        errors = {}
        if user_input is not None:
            devices = list(user_input.values())
            if len(set(devices)) != 2 or set(devices) & self.assigned():
                errors["base"] = "duplicate"
            else:
                self.settings.update(user_input)
                if len(self.settings["rooms"]) == len(SLOTS):
                    return self.finish()
                return await self.async_step_room()
        schema = vol.Schema({
            vol.Required("boiler"): entities(["light", "switch"]),
            vol.Required("floor"): entities(["switch", "light"]),
        })
        return self.async_show_form(step_id="shared", errors=errors,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or self.settings))

    async def async_step_room(self, user_input=None):
        level = SLOTS[self.slot]
        errors = {}
        if user_input is not None:
            room = dict(user_input, floor=level)
            room["name"] = room["name"].strip()
            actuators = list(room.get("radiators", []))
            if room.get("floor_climate"):
                actuators.append(room["floor_climate"])
            reserved = self.assigned(self.slot) | {self.settings["boiler"], self.settings["floor"]}
            registry = er.async_get(self.hass)
            if not room["name"] or not room.get("sensors"):
                errors["base"] = "required_room"
            elif level == 2 and not actuators:
                errors["base"] = "no_heat"
            elif len(actuators) != len(set(actuators)) or reserved.intersection(actuators):
                errors["base"] = "duplicate"
            elif any((item := registry.async_get(k)) and item.platform == "house_heating" for k in actuators):
                errors["base"] = "recursive"
            else:
                self.settings["rooms"][self.slot] = room
                missing = [k for k in SLOTS if k not in self.settings["rooms"]]
                if not missing:
                    return self.finish()
                self.slot = missing[0]
                return await self.async_step_room()
        fields = {
            vol.Required("name"): str,
            vol.Required("sensors"): entities("sensor", multiple=True),
            vol.Optional("radiators"): entities("climate", multiple=True),
        }
        if level == 2:
            fields[vol.Optional("floor_climate")] = entities(["climate", "switch"])
        defaults = self.settings["rooms"].get(self.slot, {})
        return self.async_show_form(step_id="room", errors=errors,
            description_placeholders={"number": self.slot.split("_")[1], "floor": str(level)},
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), user_input or defaults))

    def finish(self):
        if self.edit_entry:
            return self.async_update_reload_and_abort(self.edit_entry, data_updates=self.settings)
        return self.async_create_entry(title="Климат дома", data=self.settings)
