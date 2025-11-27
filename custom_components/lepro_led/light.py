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
import colorsys  # Added for Color conversion
from .const import DOMAIN, LOGIN_URL, FAMILY_LIST_URL, USER_PROFILE_URL, DEVICE_LIST_URL, SWITCH_API_URL
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
    # Removed Effect Constants and D60 mappings as they are for LED Strips, not B1 Bulb.
    
    def __init__(self, device, mqtt_client, entry_id):
        self._device = device
        # self._attr_name = device["name"]
        self._attr_unique_id = str(device["did"])
        self._fid = device["fid"]
        self._mqtt_client = mqtt_client
        self._entry_id = entry_id
        self._did = str(device["did"])
        self._attr_has_entity_name = True
        self._attr_translation_key = "bulb" # Changed to bulb
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._did)},
            "name": device["name"],
            "manufacturer": "Lepro",
            "model": device.get("series", "Lepro B1"),
        }        
        
        # State variables for B1 Bulb
        # d1: On/Off (0/1)
        self._is_on = bool(device.get("d1", 0))
        
        # d2: Work Mode (1=White, 2=Color, 3=Scene)
        self._mode = device.get("d2", 1)
        
        # d3: White Brightness (0-1000)
        self._brightness = self._map_device_brightness(device.get("d3", 1000))
        
        # Initialize color from d5 (Hex string) if present
        self._attr_rgb_color = (255, 255, 255)
        if "d5" in device:
            self._parse_d5(device["d5"])
            
        # Entity attributes
        self._attr_supported_features = LightEntityFeature.EFFECT
        self._attr_color_mode = ColorMode.RGB
        self._attr_supported_color_modes = {ColorMode.RGB}
        
        # B1 specific effects (if any) can be added here, currently empty
        self._attr_effect_list = []
            
    def _map_device_brightness(self, device_brightness):
        """Map device brightness (0-1000) to HA brightness (0-255)"""
        # B1 uses 0-1000 range based on JADX
        return int(device_brightness * 255 / 1000)
    
    def _map_ha_brightness(self, ha_brightness):
        """Map HA brightness (0-255) to device brightness (0-1000)"""
        return int(ha_brightness * 1000 / 255)
    
    def _parse_d5(self, hex_str):
        """
        Parse d5 Hex String from B1 Protocol (AIotUtils.java)
        Format: HHHHSSSSVVVV (Hue 0-360, Sat 0-1000, Val 0-1000)
        """
        try:
            if len(hex_str) < 12:
                return
            h_int = int(hex_str[0:4], 16)
            s_int = int(hex_str[4:8], 16)
            v_int = int(hex_str[8:12], 16)
            
            # Convert HSV to RGB
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
        """Turn on the light with optional parameters."""
        payload = {}
        
        # Determine new values from kwargs
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness)
        
        # B1 Protocol always needs d1=1 for ON
        payload["d1"] = 1
        self._is_on = True
        
        # Add trace ID (required by app logic)
        payload["d30"] = "ha_cmd"
        
        # Handle Color Change
        if ATTR_RGB_COLOR in kwargs:
            rgb_color = kwargs[ATTR_RGB_COLOR]
            self._attr_rgb_color = rgb_color
            
            # Convert RGB to HSV
            r, g, b = rgb_color
            h, s, v = colorsys.rgb_to_hsv(r/255.0, g/255.0, b/255.0)
            
            # Use provided brightness for V if available, otherwise use current V
            if ATTR_BRIGHTNESS in kwargs:
                v = brightness / 255.0
                self._brightness = brightness
            
            # Format to Hex: HHHHSSSSVVVV
            h_val = int(h * 360)
            s_val = int(s * 1000)
            v_val = int(v * 1000)
            
            d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
            
            payload["d2"] = 2  # Color Mode
            payload["d5"] = d5_hex
            
        # Handle White Brightness Change (Only if no color change requested)
        elif ATTR_BRIGHTNESS in kwargs:
            self._brightness = brightness
            d3_val = self._map_ha_brightness(brightness)
            
            # If we are in color mode, we probably want to stay in color mode but dim it
            # But B1 separates White (d3) and Color (d5). 
            # Simple logic: If we are currently White (mode 1), send d3. 
            # If we are Color (mode 2), we should re-send d5 with lower V.
            
            if self._mode == 2:
                 # Re-calculate d5 with new brightness
                 r, g, b = self._attr_rgb_color
                 h, s, _ = colorsys.rgb_to_hsv(r/255.0, g/255.0, b/255.0)
                 v = brightness / 255.0
                 
                 h_val = int(h * 360)
                 s_val = int(s * 1000)
                 v_val = int(v * 1000)
                 d5_hex = f"{h_val:04X}{s_val:04X}{v_val:04X}"
                 payload["d5"] = d5_hex
            else:
                 payload["d2"] = 1 # White Mode
                 payload["d3"] = d3_val

        # Send command
        await self._send_mqtt_command(payload)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn off the light."""
        payload = {
            "d1": 0,
            "d30": "ha_cmd"
        }
        await self._send_mqtt_command(payload)
        self._is_on = False
        self.async_write_ha_state()

    async def _send_mqtt_command(self, payload: dict):
        """Send command via MQTT"""
        # B1 uses standard prp/set topic
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
        # Request B1 specific DPs: d1, d2, d3, d5
        payload = json.dumps({"d": ["d1", "d2", "d3", "d5", "d30", "online"]})
        try:
            await self._mqtt_client.publish(topic, payload)
            _LOGGER.debug("Requested state update for %s", self.name)
        except Exception as e:
            _LOGGER.error("Failed to request state update: %s", e)


# Removed LeproSegmentLight class as B1 is not a segment light


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
    
    # NOTE: If using the manual key file (client_key.pem), use it.
    # If the downloaded cert contains both, client_cert_path can be passed for both args.
    # The original code passed keyfile_path explicitly, so we keep that.
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
    
    # KEEPING THIS AS REQUESTED: Manual key file in the local directory
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
        # We pass keyfile_path here (the manual one) as requested
        ssl_context = await hass.async_add_executor_job(
            create_ssl_context, 
            root_ca_path, 
            client_cert_path, 
            keyfile_path
        )
    except Exception as e:
        _LOGGER.error("Failed to create SSL context: %s", e)
        # Fallback: If manual key failed, try using client_cert as key (since download often has both)
        try:
            _LOGGER.info("Trying fallback to combined cert/key from download...")
            ssl_context = await hass.async_add_executor_job(
                create_ssl_context, 
                root_ca_path, 
                client_cert_path, 
                client_cert_path
            )
        except Exception as e2:
             _LOGGER.error("Fallback SSL context failed: %s", e2)
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
    
    for device in devices:
        entity = LeproLedLight(device, mqtt_client, entry.entry_id)
        entities.append(entity)
        device_entity_map[str(device['did'])] = entity

    
    # 9) Message handler
    # Update the message handler to process B1 specific fields
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
                
                # Update basic state (d1 is On/Off)
                if 'd1' in data:
                    entity._is_on = bool(data['d1'])
                
                # Update mode (d2)
                if 'd2' in data:
                    entity._mode = data['d2']
                
                # Update white brightness (d3)
                if 'd3' in data:
                    entity._brightness = entity._map_device_brightness(data['d3'])
                
                # Update color (d5)
                if 'd5' in data:
                    entity._parse_d5(data['d5'])
                    
                # update state
                entity.async_write_ha_state()

                _LOGGER.debug("Updated state for %s: on=%s", entity.name, entity._is_on)
                    
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
