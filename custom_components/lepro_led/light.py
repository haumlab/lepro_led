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
from homeassistant.core import callback

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
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
        
        # Initialize state
        # d1 is 0/1 for On/Off
        self._is_on = bool(device.get("d1", 0))
        
        # d2 is Work Mode (1=White, 2=Color, 3=Scene)
        self._mode = device.get("d2", 1) 
        
        # d3 is White Brightness (0-1000)
        self._brightness = self._map_device_brightness(device.get("d3", 1000))
        
        # d5 is Color Data (Hex String)
        self._attr_rgb_color = (255, 255, 255)
        if "d5" in device:
            self._parse_d5(device["d5"])
        
        self._attr_supported_color_modes = {ColorMode.RGB}
        self._attr_color_mode = ColorMode.RGB

    def _map_device_brightness(self, device_brightness):
        """Map device brightness (0-1000) to HA brightness (0-255)"""
        return int(device_brightness * 255 / 1000)
    
    def _map_ha_brightness(self, ha_brightness):
        """Map HA brightness (0-255) to device brightness (0-1000)"""
        return int(ha_brightness * 1000 / 255)

    def _parse_d5(self, hex_str):
        """
        Parse d5 Hex String from AIotUtils.java
        Format: HHHHSSSSVVVV (Hue 0-360, Sat 0-1000, Val 0-1000)
        """
        try:
            if len(hex_str) < 12:
                return
            h_int = int(hex_str[0:4], 16)
            s_int = int(hex_str[4:8], 16)
            v_int = int(hex_str[8:12], 16)
            
            # Convert to HA RGB (0-255)
            # HSV to RGB
            h = h_int / 360.0
            s = s_int / 1000.0
            v = v_int / 1000.0
            r, g, b = colorsys.hsv_to_rgb(h, s, v)
            self._attr_rgb_color = (int(r * 255), int(g * 255), int(b * 255))
        except Exception as e:
            _LOGGER.error(f"Error parsing d5 color: {e}")

    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        return self._brightness

    async def async_turn_on(self, **kwargs):
        """Turn on the light using B1 Protocol."""
        payload = {}
        
        # Always send Trace ID (from your DpSwitchLedValue findings)
        payload["d30"] = "ha_command"

        # 1. Handle On/Off
        payload["d1"] = 1
        self._is_on = True

        # 2. Handle Color Change
        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            self._attr_rgb_color = rgb
            
            # Convert RGB to HSV for Lepro
            r, g, b = rgb
            h, s, v = colorsys.rgb_to_hsv(r/255.0, g/255.0, b/255.0)
            
            # Format: HHHHSSSSVVVV (Hex)
            h_val = int(h * 360)
            s_val = int(s * 1000)
            v_val = int(v * 1000)
            
            # Matches AIotUtils.java: String.format("%04X%04X%04X", hue, saturation, value)
            d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
            
            payload["d2"] = 2  # Switch to Color Mode
            payload["d5"] = d5_hex
            
            # If brightness is also provided, update the V part of HSV, but d5 handles it usually
            if ATTR_BRIGHTNESS in kwargs:
                self._brightness = kwargs[ATTR_BRIGHTNESS]

        # 3. Handle White Brightness Change (if no color change)
        elif ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
            d3_val = self._map_ha_brightness(self._brightness)
            
            payload["d2"] = 1 # Switch to White Mode
            payload["d3"] = d3_val

        # Send the command
        await self._send_mqtt_command(payload)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn off the light."""
        payload = {
            "d1": 0,
            "d30": "ha_command"
        }
        await self._send_mqtt_command(payload)
        self._is_on = False
        self.async_write_ha_state()

    async def _send_mqtt_command(self, payload: dict):
        """Send command via MQTT wrapped in standard envelope."""
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
        await super().async_added_to_hass()
        # Request initial state
        topic = f"le/{self._did}/prp/get"
        # Request standard B1 DPs
        payload = json.dumps({"d": ["d1", "d2", "d3", "d5", "d30", "online"]})
        try:
            await self._mqtt_client.publish(topic, payload)
        except Exception:
            pass

async def download_cert_file(session, url, path, headers):
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            raise Exception(f"Failed to download {url}: {resp.status}")
        data = await resp.read()
        async with aiofiles.open(path, 'wb') as f:
            await f.write(data)

def create_ssl_context(root_ca_path, client_cert_path):
    """
    Create SSL context. 
    NOTE: The Lepro 'cert' download contains BOTH the Certificate and Private Key.
    So we use the same file path for certfile and keyfile.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=root_ca_path)
    context.load_cert_chain(certfile=client_cert_path, keyfile=client_cert_path)
    return context

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    config = hass.data["lepro_led"][entry.entry_id]
    account = config["account"]
    password = config["password"]
    config_data = dict(config)
    
    # Generate persistent MAC
    if "persistent_mac" not in config_data:
        mac_hash = hashlib.md5(config_data["account"].encode()).hexdigest()
        persistent_mac = f"02:{mac_hash[0:2]}:{mac_hash[2:4]}:{mac_hash[4:6]}:{mac_hash[6:8]}:{mac_hash[8:10]}"
        config_data["persistent_mac"] = persistent_mac
        hass.config_entries.async_update_entry(entry, data=config_data)
    
    mac = config_data["persistent_mac"]
    language = config_data.get("language", "it")
    # This token is standard for the app
    fcm_token = "dfi8s76mRTCxRxm3UtNp2z:APA91bHWMEWKT9CgNfGJ961jot2qgfYdWePbO5sQLovSFDI7U_H-ulJiqIAB2dpZUUrhzUNWR3OE_eM83i9IDLk1a5ZRwHDxMA_TnGqdpE8H-0_JML8pBFA"
    
    hass.data["lepro_led"][entry.entry_id] = config_data
    
    # Setup Cert Directory
    cert_dir = os.path.join(hass.config.config_dir, ".lepro_led")
    if not os.path.exists(cert_dir):
        await hass.async_add_executor_job(os.makedirs, cert_dir)

    root_ca_path = os.path.join(cert_dir, f"{entry.entry_id}_root_ca.pem")
    # This file will hold BOTH cert and key
    client_cert_path = os.path.join(cert_dir, f"{entry.entry_id}_client_combined.pem")

    async with aiohttp.ClientSession() as session:
        # 1) Login
        bearer_token = await async_login(session, account, password, mac, language, fcm_token)
        if bearer_token is None:
            return

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept-Encoding": "gzip",
            "App-Version": "1.0.9.202",
            "Device-Model": "custom_integration",
            "Device-System": "custom",
            "Host": "api-eu-iot.lepro.com",
            "User-Agent": "LE/1.0.9.202 (Custom Integration)",
            "Timestamp": str(int(time.time()))
        }

        # 2) Get Profile
        async with session.get(USER_PROFILE_URL, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to get profile")
                return
            user_data = await resp.json()

        try:
            mqtt_info = user_data["data"]["mqtt"]
        except KeyError:
            return

        # 3) Download Certs
        try:
            await download_cert_file(session, mqtt_info["root"], root_ca_path, headers)
            # The 'cert' URL returns a file with BEGIN CERTIFICATE and BEGIN PRIVATE KEY
            await download_cert_file(session, mqtt_info["cert"], client_cert_path, headers)
        except Exception as e:
            _LOGGER.error("Cert download failed: %s", e)
            return

        # 4) Get Devices (Logic simplified to find FID automatically)
        family_url = FAMILY_LIST_URL.format(timestamp=headers["Timestamp"])
        async with session.get(family_url, headers=headers) as resp:
            family_data = await resp.json()
            fid = family_data["data"]["list"][0]["fid"]

        device_url = DEVICE_LIST_URL.format(fid=fid, timestamp=str(int(time.time())))
        headers["Timestamp"] = str(int(time.time()))
        async with session.get(device_url, headers=headers) as resp:
            device_data = await resp.json()
            devices = device_data.get("data", {}).get("list", [])

    # 5) Create SSL Context (Using combined file for key and cert)
    try:
        ssl_context = await hass.async_add_executor_job(
            create_ssl_context, 
            root_ca_path, 
            client_cert_path
        )
    except Exception as e:
        _LOGGER.error("SSL Context error: %s", e)
        return

    # 6) Connect MQTT
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
        _LOGGER.error("MQTT Connect failed: %s", e)
        return
    
    # 7) Setup Entities
    entities = []
    device_entity_map = {}
    
    for device in devices:
        entity = LeproLedLight(device, mqtt_client, entry.entry_id)
        entities.append(entity)
        device_entity_map[str(device['did'])] = entity

    # 8) Message Handler
    async def handle_mqtt_message(message):
        try:
            topic = message.topic.value
            payload = json.loads(message.payload.decode())
            
            parts = topic.split('/')
            if len(parts) < 4 or parts[0] != "le":
                return
                
            did = parts[1]
            entity = device_entity_map.get(did)
            if not entity:
                return

            data = payload.get('d', {})
            
            # d1: On/Off
            if 'd1' in data:
                entity._is_on = bool(data['d1'])
            
            # d2: Mode
            if 'd2' in data:
                entity._mode = data['d2']
            
            # d3: White Brightness
            if 'd3' in data:
                entity._brightness = entity._map_device_brightness(data['d3'])
            
            # d5: Color Data
            if 'd5' in data:
                entity._parse_d5(data['d5'])

            entity.async_write_ha_state()
                    
        except Exception:
            pass
   
    mqtt_client.set_message_callback(handle_mqtt_message)
    
    # Subscribe
    for did in device_entity_map.keys():
        await mqtt_client.subscribe(f"le/{did}/prp/#")
    
    hass.data[DOMAIN][entry.entry_id] = {
        'mqtt_client': mqtt_client,
        'entities': entities
    }
    
    async_add_entities(entities)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data:
        await data['mqtt_client'].disconnect()
        hass.data[DOMAIN].pop(entry.entry_id)
    return True
