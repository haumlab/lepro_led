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
from .const import DOMAIN, LOGIN_URL, FAMILY_LIST_URL, USER_PROFILE_URL, DEVICE_LIST_URL
from aiomqtt import Client, MqttError
import aiofiles
from homeassistant.core import callback

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_EFFECT,
    LightEntity,
    ColorMode,
    LightEntityFeature,
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

class LeproLedLight(LightEntity):
    # B1 Bulb seems to use Strip Protocol (d50), so we treat it as a 1-segment strip.
    
    EFFECT_SOLID = "solid"
    
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
            "model": device.get("series", "Lepro B1 AI"),
        }
        
        # d1 = Switch (0/1)
        self._is_on = bool(device.get("d1", 0))
        # d2 = Mode (1=White/Static, 2=Dynamic/Scene)
        self._mode = device.get("d2", 1)
        
        # d52 = RGBIC Brightness (0-1000) - Not d3!
        self._brightness = self._map_device_brightness(device.get("d52", 1000))
        
        # d50 = RGBIC Data
        self._attr_rgb_color = (255, 255, 255)
        if "d50" in device:
            self._parse_d50(device["d50"])
            
        self._attr_supported_features = LightEntityFeature.EFFECT
        self._attr_color_mode = ColorMode.RGB
        self._attr_supported_color_modes = {ColorMode.RGB}
        self._attr_effect_list = [self.EFFECT_SOLID]

    def _map_device_brightness(self, device_brightness):
        return int(device_brightness * 255 / 1000)
    
    def _map_ha_brightness(self, ha_brightness):
        return int(ha_brightness * 1000 / 255)

    def _parse_d50(self, d50_str):
        """Parse d50 RGBIC String. Finds the first color and uses it."""
        try:
            # Regex to find the color Hex block after P1000...
            # It usually looks like P10001[RRGGBB]...
            match = re.search(r'P1000.*?([0-9A-F]{6})', d50_str)
            if match:
                hex_color = match.group(1)
                r = int(hex_color[0:2], 16)
                g = int(hex_color[2:4], 16)
                b = int(hex_color[4:6], 16)
                self._attr_rgb_color = (r, g, b)
        except Exception as e:
            _LOGGER.error(f"Error parsing d50: {e}")

    def _generate_d50(self, r, g, b):
        """
        Generates a d50 string for a single-segment bulb.
        Format: N01:P1000{count}{COLOR}F21000{count}{length}U3V3...
        We force it to 1 segment of the chosen color.
        """
        color_hex = f"{r:02X}{g:02X}{b:02X}"
        # 1 Group, Color, 1 Group, Length 25 (Bulb often emulates 25 LEDs internally)
        return f"N01:P10001{color_hex}F2100010019U3V3000640000E1;"

    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        return self._brightness

    async def async_turn_on(self, **kwargs):
        payload = {}
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness)
        
        # 1. Power
        payload["d1"] = 1
        payload["d30"] = "ha_cmd"
        self._is_on = True
        
        # 2. Color / d50 Logic
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            self._attr_rgb_color = (r, g, b)
            self._brightness = brightness
            
            payload["d2"] = 1 # Static/White Mode
            payload["d50"] = self._generate_d50(r, g, b)
            payload["d52"] = self._map_ha_brightness(brightness)
            
        elif ATTR_BRIGHTNESS in kwargs:
            self._brightness = brightness
            payload["d52"] = self._map_ha_brightness(brightness)

        await self._send_mqtt_command(payload)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        payload = {"d1": 0, "d30": "ha_cmd"}
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
        except Exception as e:
            _LOGGER.error("Failed to send MQTT command: %s", e)
            
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Request state. Note: requesting d50 and d52 specifically for B1 AI.
        payload = json.dumps({"d": ["d1", "d2", "d50", "d52", "d30", "online"]})
        try:
            await self._mqtt_client.publish(f"le/{self._did}/prp/get", payload)
        except Exception:
            pass

async def download_cert_file(session, url, path, headers):
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            return
        data = await resp.read()
        async with aiofiles.open(path, 'wb') as f:
            await f.write(data)

def create_ssl_context(root_ca_path, client_cert_path, keyfile_path):
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=root_ca_path)
    # If manual key exists, use it. If not, try using cert path for both (combined file).
    try:
        context.load_cert_chain(certfile=client_cert_path, keyfile=keyfile_path)
    except:
        context.load_cert_chain(certfile=client_cert_path, keyfile=client_cert_path)
    return context

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    config = hass.data["lepro_led"][entry.entry_id]
    
    # Manual key file support
    keyfile_path = os.path.join(os.path.dirname(__file__), "client_key.pem")
    cert_dir = os.path.join(hass.config.config_dir, ".lepro_led")
    if not os.path.exists(cert_dir):
        await hass.async_add_executor_job(os.makedirs, cert_dir)
    
    root_ca_path = os.path.join(cert_dir, f"{entry.entry_id}_root_ca.pem")
    client_cert_path = os.path.join(cert_dir, f"{entry.entry_id}_client_cert.pem")

    async with aiohttp.ClientSession() as session:
        bearer_token = await async_login(session, config["account"], config["password"], config["persistent_mac"])
        if not bearer_token:
            return

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
        except Exception:
            pass

        family_url = FAMILY_LIST_URL.format(timestamp=str(int(time.time())))
        async with session.get(family_url, headers=headers) as resp:
            family_data = await resp.json()
            fid = family_data["data"]["list"][0]["fid"]

        device_url = DEVICE_LIST_URL.format(fid=fid, timestamp=str(int(time.time())))
        async with session.get(device_url, headers=headers) as resp:
            device_data = await resp.json()
            devices = device_data.get("data", {}).get("list", [])

    try:
        ssl_context = await hass.async_add_executor_job(
            create_ssl_context, root_ca_path, client_cert_path, keyfile_path
        )
    except Exception:
        return

    client_id = f"lepro-app-{hashlib.sha256(entry.entry_id.encode()).hexdigest()[:32]}"
    mqtt_client = MQTTClientWrapper(hass, mqtt_info["host"], int(mqtt_info["port"]), ssl_context, client_id)
    
    try:
        await mqtt_client.connect()
    except Exception:
        return
    
    entities = []
    device_entity_map = {}
    for device in devices:
        entity = LeproLedLight(device, mqtt_client, entry.entry_id)
        entities.append(entity)
        device_entity_map[str(device['did'])] = entity

    async def handle_mqtt_message(message):
        try:
            payload = json.loads(message.payload.decode())
            did = message.topic.value.split('/')[1]
            entity = device_entity_map.get(did)
            if entity:
                data = payload.get('d', {})
                if 'd1' in data: entity._is_on = bool(data['d1'])
                if 'd52' in data: entity._brightness = entity._map_device_brightness(data['d52'])
                if 'd50' in data: entity._parse_d50(data['d50'])
                entity.async_write_ha_state()
        except Exception:
            pass
   
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
