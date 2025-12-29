from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import config_validation as cv
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
import voluptuous as vol
from .const import DOMAIN

# Config entry only (no YAML)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up Lepro LED integration (not via YAML)."""
    
    async def handle_set_effect_config(call: ServiceCall):
        entity_ids = call.data.get("entity_id")
        effect_speed = call.data.get("effect_speed")
        effect_colors = call.data.get("effect_colors")
        
        service_data = {"entity_id": entity_ids}
        if effect_speed is not None:
            service_data["effect_speed"] = effect_speed
        if effect_colors is not None:
            service_data["effect_colors"] = effect_colors
        
        await hass.services.async_call(
            LIGHT_DOMAIN,
            "turn_on",
            service_data,
            blocking=True
        )
    
    hass.services.async_register(
        DOMAIN,
        "set_effect_config",
        handle_set_effect_config,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_ids,
            vol.Optional("effect_speed"): vol.All(vol.Coerce(int), vol.Range(min=25, max=150)),
            vol.Optional("effect_colors"): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
        })
    )
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lepro LED from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    await hass.config_entries.async_forward_entry_setups(entry, ["light"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["light"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
