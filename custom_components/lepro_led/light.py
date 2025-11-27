import asyncio
import aiohttp
import logging
import time
import json
import random
import ssl
import os
import hashlib
import colorsys
from .const import DOMAIN, LOGIN_URL, FAMILY_LIST_URL, USER_PROFILE_URL, DEVICE_LIST_URL
from aiomqtt import Client, MqttError
import aiofiles

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_COLOR_TEMP_KELVIN,
    LightEntity,
    ColorMode,
    LightEntityFeature,
)

from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# --- MQTT Wrapper (Unchanged) ---
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
                clean_session=True,
                keepalive=10
            ) as client:
                self.client = client
                for topic in self._pending_subscriptions:
                    await client.subscribe(topic)
                self._pending_subscriptions = []
                for topic, payload in self._pending_messages:
                    await client.publish(topic, payload)
                self._pending_messages = []
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

# --- Login Helper (Unchanged) ---
async def async_login(session, account, password, mac, language="it", fcm_token=""):
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
        "Host": "api-eu-iot.lepro.com",
        "User-Agent": "LE/1.0.9.202 (Custom Integration)",
    }
    async with session.post(LOGIN_URL, json=payload, headers=headers) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        if data.get("code") != 0:
            return None
        return data.get("data", {}).get("token")

# --- MAIN ENTITY CLASS (REDEVELOPED FOR B1) ---
class LeproLedLight(LightEntity):
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
        
        # d5: Color Hex String
        self._attr_rgb_color = (255, 255, 255)
        if "d5" in device:
            self._parse_d5(device["d5"])
            
        # --- Capabilities ---
        self._attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
        
        # Determine current Color Mode for HA
        if self._mode == 1:
            self._attr_color_mode = ColorMode.RGB
        else:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            
        self._attr_min_color_temp_kelvin = 2700
        self._attr_max_color_temp_kelvin = 6500

    # --- Helpers ---
    def _map_ha_to_lepro(self, value):
        """Map 0-255 (HA) to 10-1000 (Lepro)"""
        if value is None: return 1000
        return max(10, int(value * 1000 / 255))

    def _map_lepro_to_ha(self, value):
        """Map 10-1000 (Lepro) to 0-255 (HA)"""
        if value is None: return 255
        return int(value * 255 / 1000)

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
            h_int = int(hex_str[0:4], 16) # Hue 0-360
            s_int = int(hex_str[4:8], 16) # Sat 0-1000
            v_int = int(hex_str[8:12], 16) # Val 0-1000
            
            # Convert to RGB
            h = h_int / 360.0
            s = s_int / 1000.0
            v = v_int / 1000.0
            r, g, b = colorsys.hsv_to_rgb(h, s, v)
            self._attr_rgb_color = (int(r*255), int(g*255), int(b*255))
        except Exception:
            pass

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
        
        # Brightness Handling
        new_brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness)
        self._brightness = new_brightness
        
        # 1. Color Change Requested (d2=1, d5=Hex)
        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            self._attr_rgb_color = rgb
            
            # Convert RGB -> HSV
            r, g, b = rgb
            h, s, v = colorsys.rgb_to_hsv(r/255.0, g/255.0, b/255.0)
            
            # Note: For Color mode, Brightness is part of d5 string (the V component)
            # If ATTR_BRIGHTNESS was sent, override V
            if ATTR_BRIGHTNESS in kwargs:
                v = new_brightness / 255.0
            
            h_val = int(h * 360)
            s_val = int(s * 1000)
            v_val = int(v * 1000)
            
            d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
            
            payload["d2"] = 1 # Color Mode
            payload["d5"] = d5_hex
            self._attr_color_mode = ColorMode.RGB

        # 2. Color Temp Change Requested (d2=0, d3=Bright, d4=Temp)
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            self._color_temp_kelvin = kelvin
            
            d3_val = self._map_ha_to_lepro(new_brightness)
            d4_val = self._map_kelvin_to_d4(kelvin)
            
            payload["d2"] = 0 # White Mode
            payload["d3"] = d3_val
            payload["d4"] = d4_val
            self._attr_color_mode = ColorMode.COLOR_TEMP

        # 3. Only Brightness Changed
        elif ATTR_BRIGHTNESS in kwargs:
            if self._mode == 1: # Current Color Mode
                # We need to re-send d5 with updated V component
                r, g, b = self._attr_rgb_color
                h, s, _ = colorsys.rgb_to_hsv(r/255.0, g/255.0, b/255.0)
                v = new_brightness / 255.0
                
                h_val = int(h * 360)
                s_val = int(s * 1000)
                v_val = int(v * 1000)
                d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
                payload["d5"] = d5_hex
            else: # Current White Mode
                payload["d3"] = self._map_ha_to_lepro(new_brightness)

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
            "d": payload
        }
        try:
            await self._mqtt_client.publish(topic, json.dumps(full_payload))
        except Exception as e:
            _LOGGER.error("Failed to send: %s", e)
            
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Request full status according to doc
        try:
            payload = json.dumps({"d": ["d1", "d2", "d3", "d4", "d5", "online"]})
            await self._mqtt_client.publish(f"le/{self._did}/prp/dsr/app/get", payload)
        except Exception:
            pass

# --- Cert Helpers ---
async def download_cert_file(session, url, path, headers):
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200: return
        data = await resp.read()
        async with aiofiles.open(path, 'wb') as f:
            await f.write(data)

def create_ssl_context(root_ca_path, client_cert_path, keyfile_path):
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=root_ca_path)
    # Use manual key file if provided, otherwise assume cert contains key
    try:
        context.load_cert_chain(certfile=client_cert_path, keyfile=keyfile_path)
    except:
        context.load_cert_chain(certfile=client_cert_path, keyfile=client_cert_path)
    return context

# --- Setup Entry ---
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    config = hass.data["lepro_led"][entry.entry_id]
    
    # Files
    keyfile_path = os.path.join(os.path.dirname(__file__), "client_key.pem")
    cert_dir = os.path.join(hass.config.config_dir, ".lepro_led")
    if not os.path.exists(cert_dir):
        await hass.async_add_executor_job(os.makedirs, cert_dir)
    
    root_ca_path = os.path.join(cert_dir, f"{entry.entry_id}_root_ca.pem")
    client_cert_path = os.path.join(cert_dir, f"{entry.entry_id}_client_cert.pem")

    # API Login / Downloads
    async with aiohttp.ClientSession() as session:
        bearer_token = await async_login(session, config["account"], config["password"], config["persistent_mac"])
        if not bearer_token: return

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "App-Version": "1.0.9.202",
            "Device-Model": "custom_integration",
            "Host": "api-eu-iot.lepro.com",
            "User-Agent": "LE/1.0.9.202 (Custom Integration)",
        }

        async with session.get(USER_PROFILE_URL, headers=headers) as resp:
            if resp.status != 200: return
            user_data = await resp.json()
            mqtt_info = user_data["data"]["mqtt"]

        try:
            await download_cert_file(session, mqtt_info["root"], root_ca_path, headers)
            await download_cert_file(session, mqtt_info["cert"], client_cert_path, headers)
        except Exception: pass

        # Get Devices
        family_url = FAMILY_LIST_URL.format(timestamp=str(int(time.time())))
        async with session.get(family_url, headers=headers) as resp:
            family_data = await resp.json()
            fid = family_data["data"]["list"][0]["fid"]

        device_url = DEVICE_LIST_URL.format(fid=fid, timestamp=str(int(time.time())))
        async with session.get(device_url, headers=headers) as resp:
            device_data = await resp.json()
            devices = device_data.get("data", {}).get("list", [])

    # MQTT Connect
    try:
        ssl_context = await hass.async_add_executor_job(
            create_ssl_context, root_ca_path, client_cert_path, keyfile_path
        )
    except Exception: return

    client_id = f"lepro-app-{hashlib.sha256(entry.entry_id.encode()).hexdigest()[:32]}"
    mqtt_client = MQTTClientWrapper(hass, mqtt_info["host"], int(mqtt_info["port"]), ssl_context, client_id)
    
    try:
        await mqtt_client.connect()
    except Exception: return
    
    # Entity Creation
    entities = []
    device_entity_map = {}
    for device in devices:
        entity = LeproLedLight(device, mqtt_client, entry.entry_id)
        entities.append(entity)
        device_entity_map[str(device['did'])] = entity

    # Handler: Update State from MQTT
    async def handle_mqtt_message(message):
        try:
            payload = json.loads(message.payload.decode())
            did = message.topic.value.split('/')[1]
            entity = device_entity_map.get(did)
            
            if entity:
                data = payload.get('d', {})
                if 'd1' in data: entity._is_on = bool(data['d1'])
                if 'd2' in data: entity._mode = data['d2']
                if 'd3' in data: entity._brightness = entity._map_lepro_to_ha(data['d3'])
                if 'd4' in data: entity._color_temp_kelvin = entity._map_d4_to_kelvin(data['d4'])
                if 'd5' in data: entity._parse_d5(data['d5'])
                
                # Update HA mode based on d2
                if entity._mode == 1:
                    entity._attr_color_mode = ColorMode.RGB
                else:
                    entity._attr_color_mode = ColorMode.COLOR_TEMP
                    
                entity.async_write_ha_state()
        except Exception: pass
   
    mqtt_client.set_message_callback(handle_mqtt_message)
    for did in device_entity_map.keys():
        await mqtt_client.subscribe(f"le/{did}/prp/#")
    
    hass.data[DOMAIN][entry.entry_id] = {'mqtt_client': mqtt_client, 'entities': entities}
    async_add_entities(entities)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data:
        await data['mqtt_client'].disconnect()
        hass.data[DOMAIN].pop(entry.entry_id)
    return True
