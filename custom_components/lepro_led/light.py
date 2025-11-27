import asyncio
import aiohttp
import logging
import time
import json
import random
import ssl
import os
import hashlib
import re
import numpy as np
import colorsys # Required for B1 Color conversion
from .const import DOMAIN, LOGIN_URL, FAMILY_LIST_URL, USER_PROFILE_URL, DEVICE_LIST_URL, SWITCH_API_URL
from aiomqtt import Client, MqttError
import aiofiles
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

_LOGGER = logging.getLogger(__name__)

class MQTTClientWrapper:
    def __init__(self, hass, host, port, ssl_context, client_id):
        self.hass = hass
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.client_id = client_id
        self.client = None
        self._message_callback = None
        self._loop_task = None
        self._pending_subscriptions = []
        self._pending_messages = []

    async def _connect_and_run(self):
        try:
            async with Client(
                hostname=self.host,
                port=self.port,
                identifier=self.client_id,
                tls_context=self.ssl_context,
                clean_session=True
            ) as client:
                self.client = client
                
                # Process pending subscriptions
                for topic in self._pending_subscriptions:
                    await client.subscribe(topic)
                self._pending_subscriptions = []
                
                # Process pending messages
                for topic, payload in self._pending_messages:
                    await client.publish(topic, payload)
                self._pending_messages = []
                
                # Start message loop
                async for message in client.messages:
                    if self._message_callback:
                        await self._message_callback(message)
        except MqttError as e:
            _LOGGER.error("MQTT error: %s", e)
        finally:
            self.client = None

    async def connect(self):
        if self._loop_task and not self._loop_task.done():
            return
            
        self._pending_subscriptions = []
        self._pending_messages = []
        self._loop_task = asyncio.create_task(self._connect_and_run())

    async def subscribe(self, topic):
        if self.client:
            await self.client.subscribe(topic)
        else:
            self._pending_subscriptions.append(topic)
            if not self._loop_task or self._loop_task.done():
                await self.connect()

    async def publish(self, topic, payload):
        if self.client:
            await self.client.publish(topic, payload)
        else:
            self._pending_messages.append((topic, payload))
            if not self._loop_task or self._loop_task.done():
                await self.connect()

    def set_message_callback(self, callback):
        self._message_callback = callback

    async def disconnect(self):
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

async def async_login(session, account, password, mac, language="it", fcm_token=""):
    """Perform login and return bearer token."""
    timestamp = str(int(time.time()))
    payload = {
        "platform": "2",
        "account": account,
        "password": password,
        "mac": mac,
        "timestamp": timestamp,
        "language": language,
        "fcmToken": fcm_token,
    }
    headers = {
        "Content-Type": "application/json",
        "App-Version": "1.0.9.202",
        "Device-Model": "custom_integration",
        "Device-System": "custom",
        "GMT": "+0",
        "Host": "api-eu-iot.lepro.com",
        "Language": language,
        "Platform": "2",
        "Screen-Size": "1536*2048",
        "Slanguage": language,
        "Timestamp": timestamp,
        "User-Agent": "LE/1.0.9.202 (Custom Integration)",
    }

    async with session.post(LOGIN_URL, json=payload, headers=headers) as resp:
        import json
        if resp.status != 200:
            _LOGGER.error("Login failed with status %s", resp.status)
            return None
        data = await resp.json()
        if data.get("code") != 0:
            _LOGGER.error("Login failed with message: %s", data.get("msg"))
            return None
        token = data.get("data", {}).get("token")
        return token


class LeproLedLight(LightEntity):
    # This class has been rewritten for the B1 Protocol (d1, d2, d3, d4, d5)
    
    def __init__(self, device, mqtt_client, entry_id):
        self._device = device
        self._attr_unique_id = str(device["did"])
        self._fid = device["fid"]
        self._mqtt_client = mqtt_client
        self._entry_id = entry_id
        self._did = str(device["did"])
        self._attr_has_entity_name = True
        self._attr_translation_key = "bulb"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._did)},
            "name": device["name"],
            "manufacturer": "Lepro",
            "model": device.get("series", "Lepro B1"),
        }
        
        # --- Internal State ---
        # d1: 0=Off, 1=On
        self._is_on = bool(device.get("d1", 0))
        # d2: 0=White, 1=Color, 2=Scene
        self._mode = device.get("d2", 0)
        
        # d3: Brightness (10-1000)
        self._brightness = self._map_lepro_to_ha(device.get("d3", 1000))
        
        # d4: Color Temp (0-1000, 0=2700K, 1000=6500K)
        self._color_temp_kelvin = self._map_d4_to_kelvin(device.get("d4", 0))
        
        # d5: Color Hex String - stores Hue (0-360) and Saturation (0-100)
        self._attr_hs_color = (0.0, 100.0)  # Default red at full saturation
        if "d5" in device:
            self._parse_d5(device["d5"])
            
        # --- Capabilities ---
        self._attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
        
        # Determine current Color Mode for HA
        if self._mode == 1:
            self._attr_color_mode = ColorMode.HS
        else:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            
        self._attr_min_color_temp_kelvin = 2700
        self._attr_max_color_temp_kelvin = 6500

    # --- Helpers ---
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
        # 2700K = 0, 6500K = 1000
        percentage = (kelvin - 2700) / (6500 - 2700)
        return max(0, min(1000, int(percentage * 1000)))

    def _map_d4_to_kelvin(self, d4):
        """Map d4 (0-1000) to Kelvin (2700-6500)"""
        return int(2700 + (d4 / 1000.0) * (6500 - 2700))

    def _parse_d5(self, hex_str):
        """Parse d5: HHHHSSSSBBBB (12 hex chars)"""
        try:
            if not hex_str or len(hex_str) < 12: return
            h_int = int(hex_str[0:4], 16)  # Hue 0-360
            s_int = int(hex_str[4:8], 16)  # Sat 0-1000
            v_int = int(hex_str[8:12], 16) # Val/Brightness 0-1000
            
            # Store as HS color (Home Assistant format: H=0-360, S=0-100)
            self._attr_hs_color = (float(h_int), s_int / 10.0)
            
            # V component is brightness in color mode - use round() to avoid drift
            self._brightness = max(1, round(v_int * 255 / 1000))
            
            _LOGGER.debug("Parsed d5=%s -> HS=(%s, %s), brightness=%s", 
                         hex_str, h_int, s_int/10.0, self._brightness)
        except Exception as e:
            _LOGGER.error("Error parsing d5: %s", e)

    # --- Properties ---
    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        return self._brightness
        
    @property
    def color_temp_kelvin(self):
        return self._color_temp_kelvin

    # --- Control ---
    async def async_turn_on(self, **kwargs):
        payload = {}
        payload["d1"] = 1
        self._is_on = True
        
        # Get brightness - use provided or current, ensure minimum of 10 (HA scale)
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = max(10, kwargs[ATTR_BRIGHTNESS])
        
        # 1. Color (HS) Change Requested
        if ATTR_HS_COLOR in kwargs:
            hs = kwargs[ATTR_HS_COLOR]
            self._attr_hs_color = hs
            
            # Convert HA HS (H=0-360, S=0-100) to Lepro d5 format
            h_val = int(hs[0])  # Hue 0-360
            s_val = int(hs[1] * 10)  # Saturation: HA 0-100 -> Lepro 0-1000
            v_val = self._map_ha_to_lepro(self._brightness)  # Brightness as V
            
            d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
            
            payload["d2"] = 1  # Color Mode
            payload["d5"] = d5_hex
            self._attr_color_mode = ColorMode.HS
            self._mode = 1
            
            _LOGGER.debug("Color change: HS=%s, brightness=%s, d5=%s", hs, self._brightness, d5_hex)

        # 2. Color Temp Change Requested
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

        # 3. Only Brightness Changed (or just turn on)
        elif ATTR_BRIGHTNESS in kwargs:
            if self._mode == 1:  # Color Mode - update d5 with new brightness
                hs = self._attr_hs_color
                h_val = int(hs[0])
                s_val = int(hs[1] * 10)
                v_val = self._map_ha_to_lepro(self._brightness)
                d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
                payload["d5"] = d5_hex
                _LOGGER.debug("Brightness change in color mode: d5=%s", d5_hex)
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
        # Request initial state
        await self._request_state_update()

    async def _request_state_update(self):
        """Request current state from device."""
        topic = f"le/{self._did}/prp/get"
        # B1 payload for get
        payload = json.dumps({"d": ["d1", "d2", "d3", "d4", "d5", "online"]})
        try:
            await self._mqtt_client.publish(topic, payload)
            _LOGGER.debug("Requested state update for %s", self.name)
        except Exception as e:
            _LOGGER.error("Failed to request state update: %s", e)


async def download_cert_file(session, url, path, headers):
    """Download a certificate file asynchronously."""
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            raise Exception(f"Failed to download {url}: {resp.status}")
        data = await resp.read()
        async with aiofiles.open(path, 'wb') as f:
            await f.write(data)

def create_ssl_context(root_ca_path, client_cert_path, keyfile_path):
    """Create SSL context in a thread-safe manner."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=root_ca_path)
    context.load_cert_chain(certfile=client_cert_path, keyfile=keyfile_path)
    return context

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up Lepro LED lights from config entry."""
    config = hass.data["lepro_led"][entry.entry_id]
    account = config["account"]
    password = config["password"]
    
    # Create a mutable copy of the config
    config_data = dict(config)
    
    # Generate persistent MAC if not exists
    if "persistent_mac" not in config_data:
        # Create a hash of the account to generate a persistent MAC
        mac_hash = hashlib.md5(config_data["account"].encode()).hexdigest()
        persistent_mac = f"02:{mac_hash[0:2]}:{mac_hash[2:4]}:{mac_hash[4:6]}:{mac_hash[6:8]}:{mac_hash[8:10]}"
        config_data["persistent_mac"] = persistent_mac
        
        # Save updated config to the entry
        hass.config_entries.async_update_entry(entry, data=config_data)
        _LOGGER.info("Generated persistent MAC: %s", persistent_mac)
    
    # Use the persistent MAC from config_data
    mac = config_data["persistent_mac"]
    language = config_data.get("language", "it")
    fcm_token = config_data.get("fcm_token", "dfi8s76mRTCxRxm3UtNp2z:APA91bHWMEWKT9CgNfGJ961jot2qgfYdWePbO5sQLovSFDI7U_H-ulJiqIAB2dpZUUrhzUNWR3OE_eM83i9IDLk1a5ZRwHDxMA_TnGqdpE8H-0_JML8pBFA")
    
    # Update hass.data with the new config
    hass.data["lepro_led"][entry.entry_id] = config_data
    
    # ... rest of the setup code ...
    # 1) Create certificate directory
    cert_dir = os.path.join(hass.config.config_dir, ".lepro_led")
    if not os.path.exists(cert_dir):
        await hass.async_add_executor_job(os.makedirs, cert_dir)

    root_ca_path = os.path.join(cert_dir, f"{entry.entry_id}_root_ca.pem")
    client_cert_path = os.path.join(cert_dir, f"{entry.entry_id}_client_cert.pem")
    keyfile_path = os.path.join(os.path.dirname(__file__), "client_key.pem")

    async with aiohttp.ClientSession() as session:
        # 1) Login and get bearer token
        bearer_token = await async_login(session, account, password, mac, language, fcm_token)
        if bearer_token is None:
            _LOGGER.error("Failed to login to Lepro API")
            return

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept-Encoding": "gzip",
            "App-Version": "1.0.9.202",
            "Device-Model": "custom_integration",
            "Device-System": "custom",
            "GMT": "+0",
            "Host": "api-eu-iot.lepro.com",
            "Language": language,
            "Platform": "2",
            "Screen-Size": "1536*2048",
            "Slanguage": language,
            "User-Agent": "LE/1.0.9.202 (Custom Integration)",
        }

        # 2) Get user profile to find uid and MQTT info
        user_url = USER_PROFILE_URL
        timestamp = str(int(time.time()))
        headers["Timestamp"] = timestamp
        
        async with session.get(user_url, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to get user profile from Lepro API")
                return
            user_data = await resp.json()

        try:
            uid = user_data["data"]["uid"]
            mqtt_info = user_data["data"]["mqtt"]
        except KeyError as e:
            _LOGGER.error("Failed to parse user profile response: %s", e)
            return

        # 3) Download certificates within the same session
        try:
            await download_cert_file(session, mqtt_info["root"], root_ca_path, headers)
            await download_cert_file(session, mqtt_info["cert"], client_cert_path, headers)
        except Exception as e:
            _LOGGER.error("Certificate download failed: %s", e)
            return

        # 4) Get family list to find fid
        family_url = FAMILY_LIST_URL.format(timestamp=timestamp)
        async with session.get(family_url, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to get family list from Lepro API")
                return
            family_data = await resp.json()

        try:
            fid = family_data["data"]["list"][0]["fid"]
        except (KeyError, IndexError) as e:
            _LOGGER.error("Failed to parse fid from family list response: %s", e)
            return

        # 5) Get device list by fid
        timestamp = str(int(time.time()))
        device_url = DEVICE_LIST_URL.format(fid=fid, timestamp=timestamp)
        headers["Timestamp"] = timestamp

        async with session.get(device_url, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to get device list from Lepro API")
                return
            device_data = await resp.json()

        devices = device_data.get("data", {}).get("list", [])
        if not devices:
            _LOGGER.warning("No devices found in Lepro account")
            return

    # 6) Create SSL context in executor thread
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

    # 7) Create MQTT client
    client_id_suffix = hashlib.sha256(entry.entry_id.encode()).hexdigest()[:32]
    client_id = f"lepro-app-{client_id_suffix}"
    
    mqtt_client = MQTTClientWrapper(
        hass,
        host=mqtt_info["host"],
        port=int(mqtt_info["port"]),
        ssl_context=ssl_context,
        client_id=client_id
    )
    
    try:
        await mqtt_client.connect()
    except Exception as e:
        _LOGGER.error("MQTT connection failed: %s", e)
        return
    
    # 8) Create entities
    entities = []
    device_entity_map = {}
    
    # Cleaned up loop: NO SEGMENTS for B1
    for device in devices:
        entity = LeproLedLight(device, mqtt_client, entry.entry_id)
        entities.append(entity)
        device_entity_map[str(device['did'])] = entity
    
    # 9) Message handler
    # Update the message handler to process all relevant fields
    async def handle_mqtt_message(message):
        try:
            topic = message.topic.value
            payload = json.loads(message.payload.decode())
            _LOGGER.debug("Received MQTT message: %s - %s", topic, payload)
            
            parts = topic.split('/')
            if len(parts) < 4 or parts[0] != "le":
                return
                
            did = parts[1]
            message_type = parts[3]
            entity = device_entity_map.get(did)
            
            if not entity:
                return
                
            # Handle different message types
            if message_type in ["rpt", "set", "getr"]:
                data = payload.get('d', {})
                
                # B1 Protocol Handling
                if 'd1' in data:
                    entity._is_on = bool(data['d1'])
                
                if 'd2' in data:
                    entity._mode = data['d2']
                
                if 'd4' in data:
                    entity._color_temp_kelvin = entity._map_d4_to_kelvin(data['d4'])
                
                # Handle d5 first (contains brightness for color mode)
                if 'd5' in data:
                    entity._parse_d5(data['d5'])
                
                # d3 is brightness for white mode - only update if NOT in color mode
                # (in color mode, brightness comes from d5's V component)
                if 'd3' in data and entity._mode != 1:
                    entity._brightness = entity._map_lepro_to_ha(data['d3'])
                
                # Update HA State
                if entity._mode == 1:
                    entity._attr_color_mode = ColorMode.HS
                else:
                    entity._attr_color_mode = ColorMode.COLOR_TEMP

                entity.async_write_ha_state()

                _LOGGER.debug("Updated state for %s: on=%s, mode=%s, brightness=%s", entity.name, entity._is_on, entity._mode, entity._brightness)
                    
        except Exception as e:
            _LOGGER.error("Error processing MQTT message: %s", e)
   
    mqtt_client.set_message_callback(handle_mqtt_message)
    
    # 10) Subscribe and start
    await mqtt_client.subscribe(f"le/{client_id_suffix}/act/app/exe")
    for did in device_entity_map.keys():
        await mqtt_client.subscribe(f"le/{did}/prp/#")
    
    # Store for cleanup
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN][entry.entry_id] = {
        'mqtt_client': mqtt_client,
        'entities': entities
    }
    
    async_add_entities(entities)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload MQTT client and entities."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        return True
        
    # Disconnect MQTT client
    await data['mqtt_client'].disconnect()
    
    # Remove entities
    for entity in data['entities']:
        await entity.async_remove()
        
    hass.data[DOMAIN].pop(entry.entry_id)
    return True
