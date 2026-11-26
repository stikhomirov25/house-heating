from homeassistant.const import Platform


async def async_setup_entry(hass, entry):
    if not entry.data.get("rooms"):
        return True  # Legacy installation: use Reconfigure to choose devices.
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.CLIMATE])
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    hass.data.setdefault("house_heating_loaded", {})[entry.entry_id] = True
    return True


async def async_unload_entry(hass, entry):
    if not hass.data.get("house_heating_loaded", {}).get(entry.entry_id):
        return True
    success = await hass.config_entries.async_unload_platforms(entry, [Platform.CLIMATE])
    if success:
        hass.data["house_heating_loaded"].pop(entry.entry_id, None)
    return success


async def async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)
