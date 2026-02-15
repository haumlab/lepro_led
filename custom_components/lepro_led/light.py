import asyncio
import logging
import time
import json
import random
import os
import hashlib
import re
import numpy as np
import colorsys 
import aiohttp

from homeassistant.core import callback
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ATTR_COLOR_TEMP_KELVIN,
    LightEntity,
    ColorMode,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .lepro_api import LeproAPI, create_ssl_context
from .lepro_mqtt import LeproMQTTClient

_LOGGER = logging.getLogger(__name__)

class LeproLedLight(LightEntity):
    
    def __init__(self, device, mqtt_client, entry_id):
        self._device = device
        self._attr_unique_id = str(device["did"])
        self._fid = device["fid"]
        self._mqtt_client = mqtt_client
        self._entry_id = entry_id
        self._did = str(device["did"])
        self._attr_has_entity_name = True
        self._attr_translation_key = "strip"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._did)},
            "name": device["name"],
            "manufacturer": "Lepro",
            "model": device.get("series", "Lepro B1"),
        }
        
        self._is_on = bool(device.get("d1", 0))
        self._mode = device.get("d2", 0)
        
        self._brightness = self._map_lepro_to_ha(device.get("d3", 1000))
        
        self._color_temp_kelvin = self._map_d4_to_kelvin(device.get("d4", 0))
        
        self._attr_hs_color = (0.0, 100.0)  
        if "d5" in device:
            self._parse_d5(device["d5"])
            
        self._attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
        
        if self._mode == 1:
            self._attr_color_mode = ColorMode.HS
        else:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            
        self._attr_min_color_temp_kelvin = 2700
        self._attr_max_color_temp_kelvin = 6500

    def _map_ha_to_lepro(self, value):
        """Map 0-255 (HA) to 10-1000 (Lepro)"""
        if value is None: return 1000
        return max(10, round(value * 1000 / 255))

    def _map_lepro_to_ha(self, value):
        """Map 10-1000 (Lepro) to 0-255 (HA)"""
        if value is None: return 255
        return round(value * 255 / 1000)

    def _map_kelvin_to_d4(self, kelvin):
        """Map Kelvin (2700-6500) to d4 (0-1000)"""
        percentage = (kelvin - 2700) / (6500 - 2700)
        return max(0, min(1000, int(percentage * 1000)))

    def _map_d4_to_kelvin(self, d4):
        """Map d4 (0-1000) to Kelvin (2700-6500)"""
        return int(2700 + (d4 / 1000.0) * (6500 - 2700))

    def _parse_d5(self, hex_str):
        """Parse d5: HHHHSSSSBBBB (12 hex chars)"""
        try:
            if not hex_str or len(hex_str) < 12: return
            h_int = int(hex_str[0:4], 16)  
            s_int = int(hex_str[4:8], 16)  
            v_int = int(hex_str[8:12], 16) 
            
            self._attr_hs_color = (float(h_int), s_int / 10.0)
            
            self._brightness = max(1, round(v_int * 255 / 1000))
        except Exception as e:
            _LOGGER.error("Error parsing d5: %s", e)

    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        return self._brightness
        
    @property
    def color_temp_kelvin(self):
        return self._color_temp_kelvin

    async def async_turn_on(self, **kwargs):
        payload = {}
        payload["d1"] = 1
        self._is_on = True
        
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = max(1, kwargs[ATTR_BRIGHTNESS])
        
        if ATTR_HS_COLOR in kwargs:
            hs = kwargs[ATTR_HS_COLOR]
            self._attr_hs_color = hs
            
            h_val = int(hs[0])  
            s_val = int(hs[1] * 10) 
            v_val = self._map_ha_to_lepro(self._brightness) 
            
            d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
            
            payload["d2"] = 1  # Color Mode
            payload["d5"] = d5_hex
            self._attr_color_mode = ColorMode.HS
            self._mode = 1
            
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            self._color_temp_kelvin = kelvin
            
            d3_val = self._map_ha_to_lepro(self._brightness)
            d4_val = self._map_kelvin_to_d4(kelvin)
            
            payload["d2"] = 0  # White Mode
            payload["d3"] = d3_val
            payload["d4"] = d4_val
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._mode = 0

        elif ATTR_BRIGHTNESS in kwargs:
            if self._mode == 1:  
                hs = self._attr_hs_color
                h_val = int(hs[0])
                s_val = int(hs[1] * 10)
                v_val = self._map_ha_to_lepro(self._brightness)
                d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
                payload["d5"] = d5_hex
            else:  # White Mode
                payload["d3"] = self._map_ha_to_lepro(self._brightness)

        await self._send_mqtt_command(payload)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        payload = {"d1": 0}
        await self._send_mqtt_command(payload)
        self._is_on = False
        self.async_write_ha_state()

    async def _send_mqtt_command(self, payload: dict):
        topic = f"le/{self._did}/prp/set"
        full_payload = {
            "id": random.randint(0, 1000000000),
            "t": int(time.time()),
            "d": payload
        }
        try:
            await self._mqtt_client.publish(topic, json.dumps(full_payload))
            _LOGGER.debug("Sent MQTT command: %s - %s", topic, full_payload)
        except Exception as e:
            _LOGGER.error("Failed to send MQTT command: %s", e)
            
    async def async_added_to_hass(self):
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        await self._request_state_update()

    async def _request_state_update(self):
        """Request current state from device."""
        topic = f"le/{self._did}/prp/get"
        payload = json.dumps({"d": ["d1", "d2", "d3", "d4", "d5", "online"]})
        try:
            await self._mqtt_client.publish(topic, payload)
        except Exception as e:
            _LOGGER.error("Failed to request state update: %s", e)

    def process_update(self, data):
        """Process MQTT update."""
        if 'd1' in data:
            self._is_on = bool(data['d1'])

        if 'd2' in data:
            self._mode = data['d2']

        if 'd4' in data:
            self._color_temp_kelvin = self._map_d4_to_kelvin(data['d4'])

        if 'd5' in data:
            self._parse_d5(data['d5'])

        if 'd3' in data and self._mode != 1:
            self._brightness = self._map_lepro_to_ha(data['d3'])

        if self._mode == 1:
            self._attr_color_mode = ColorMode.HS
        else:
            self._attr_color_mode = ColorMode.COLOR_TEMP

        self.async_write_ha_state()

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    config = hass.data["lepro_led_b1"][entry.entry_id]
    account = config["account"]
    password = config["password"]
    
    config_data = dict(config)
    
    if "persistent_mac" not in config_data:
        mac_hash = hashlib.md5(config_data["account"].encode()).hexdigest()
        persistent_mac = f"02:{mac_hash[0:2]}:{mac_hash[2:4]}:{mac_hash[4:6]}:{mac_hash[6:8]}:{mac_hash[8:10]}"
        config_data["persistent_mac"] = persistent_mac
        hass.config_entries.async_update_entry(entry, data=config_data)
    
    mac = config_data["persistent_mac"]
    language = config_data.get("language", "it")
    fcm_token = config_data.get("fcm_token", "dfi8s76mRTCxRxm3UtNp2z:APA91bHWMEWKT9CgNfGJ961jot2qgfYdWePbO5sQLovSFDI7U_H-ulJiqIAB2dpZUUrhzUNWR3OE_eM83i9IDLk1a5ZRwHDxMA_TnGqdpE8H-0_JML8pBFA")
    
    hass.data["lepro_led_b1"][entry.entry_id] = config_data
    
    api = LeproAPI(account, password, mac, language, fcm_token)

    async with aiohttp.ClientSession() as session:
        if not await api.login(session):
            _LOGGER.error("Failed to login")
            return

        user_data = await api.get_user_profile(session)
        if not user_data:
            return

        try:
            uid = user_data["data"]["uid"]
            mqtt_info = user_data["data"]["mqtt"]
        except KeyError as e:
            _LOGGER.error("Invalid user profile data: %s", e)
            return

        cert_dir = os.path.join(hass.config.config_dir, ".lepro_led_b1")
        if not os.path.exists(cert_dir):
            await hass.async_add_executor_job(os.makedirs, cert_dir)

        root_ca_path = os.path.join(cert_dir, f"{entry.entry_id}_root_ca.pem")
        client_cert_path = os.path.join(cert_dir, f"{entry.entry_id}_client_cert.pem")
        keyfile_path = os.path.join(os.path.dirname(__file__), "client_key.pem")

        try:
            await api.download_certificates(session, mqtt_info, root_ca_path, client_cert_path)
        except Exception as e:
             _LOGGER.error("Failed to download certificates: %s", e)
             return

        family_data = await api.get_family_list(session)
        if not family_data:
            return

        try:
            fid = family_data["data"]["list"][0]["fid"]
        except (KeyError, IndexError):
            _LOGGER.error("No family found")
            return

        device_data = await api.get_device_list(session, fid)
        if not device_data:
            return

        devices = device_data.get("data", {}).get("list", [])
        if not devices:
            _LOGGER.warning("No devices found")
            return

    # SSL Context
    try:
        ssl_context = await hass.async_add_executor_job(
            create_ssl_context, 
            root_ca_path, 
            client_cert_path, 
            keyfile_path
        )
    except Exception as e:
        _LOGGER.error("Failed to create SSL context: %s", e)
        return

    # MQTT Client
    client_id_suffix = hashlib.sha256(entry.entry_id.encode()).hexdigest()[:32]
    client_id = f"lepro-app-{client_id_suffix}"
    
    mqtt_client = LeproMQTTClient(
        host=mqtt_info["host"],
        port=int(mqtt_info["port"]),
        ssl_context=ssl_context,
        client_id=client_id
    )
    
    device_entity_map = {}
    entities = []
    
    for device in devices:
        entity = LeproLedLight(device, mqtt_client, entry.entry_id)
        entities.append(entity)
        device_entity_map[str(device['did'])] = entity

    async def handle_mqtt_message(message):
        try:
            topic = message.topic.value
            payload = json.loads(message.payload.decode())
            _LOGGER.debug("MQTT: %s - %s", topic, payload)
            
            parts = topic.split('/')
            if len(parts) < 4 or parts[0] != "le":
                return

            did = parts[1]
            entity = device_entity_map.get(did)
            if entity and parts[3] in ["rpt", "set", "getr"]:
                entity.process_update(payload.get('d', {}))
                
        except Exception as e:
            _LOGGER.error("Error processing message: %s", e)

    mqtt_client.set_message_callback(handle_mqtt_message)
    
    await mqtt_client.connect()
    await mqtt_client.subscribe(f"le/{client_id_suffix}/act/app/exe")
    for did in device_entity_map.keys():
        await mqtt_client.subscribe(f"le/{did}/prp/#")

    hass.data[DOMAIN][entry.entry_id] = {
        'mqtt_client': mqtt_client,
        'entities': entities
    }
    
    # Register cleanup
    entry.async_on_unload(lambda: hass.async_create_task(mqtt_client.disconnect()))
    
    async_add_entities(entities)
