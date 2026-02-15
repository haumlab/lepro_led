import logging
import time
import asyncio
import aiohttp
import aiofiles
import os
import ssl
import json

from .const import LOGIN_URL, FAMILY_LIST_URL, USER_PROFILE_URL, DEVICE_LIST_URL

_LOGGER = logging.getLogger(__name__)

class LeproAPI:
    def __init__(self, account, password, mac, language="it", fcm_token=""):
        self.account = account
        self.password = password
        self.mac = mac
        self.language = language
        self.fcm_token = fcm_token
        self.token = None
        self.headers = {
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
            "User-Agent": "LE/1.0.9.202 (Custom Integration)",
        }

    def _get_headers(self):
        headers = self.headers.copy()
        timestamp = str(int(time.time()))
        headers["Timestamp"] = timestamp
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def login(self, session):
        timestamp = str(int(time.time()))
        payload = {
            "platform": "2",
            "account": self.account,
            "password": self.password,
            "mac": self.mac,
            "timestamp": timestamp,
            "language": self.language,
            "fcmToken": self.fcm_token,
        }

        login_headers = self.headers.copy()
        login_headers["Timestamp"] = timestamp

        try:
            async with session.post(LOGIN_URL, json=payload, headers=login_headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("Login failed with status %s", resp.status)
                    return False
                data = await resp.json()
                if data.get("code") != 0:
                    _LOGGER.error("Login failed with message: %s", data.get("msg"))
                    return False
                self.token = data.get("data", {}).get("token")
                return True
        except Exception as e:
            _LOGGER.error("Login exception: %s", e)
            return False

    async def get_user_profile(self, session):
        headers = self._get_headers()
        try:
            async with session.get(USER_PROFILE_URL, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("Failed to get user profile")
                    return None
                return await resp.json()
        except Exception as e:
            _LOGGER.error("Get user profile exception: %s", e)
            return None

    async def get_family_list(self, session):
        headers = self._get_headers()
        url = FAMILY_LIST_URL.format(timestamp=headers["Timestamp"])
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("Failed to get family list")
                    return None
                return await resp.json()
        except Exception as e:
            _LOGGER.error("Get family list exception: %s", e)
            return None

    async def get_device_list(self, session, fid):
        headers = self._get_headers()
        url = DEVICE_LIST_URL.format(fid=fid, timestamp=headers["Timestamp"])
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("Failed to get device list")
                    return None
                return await resp.json()
        except Exception as e:
            _LOGGER.error("Get device list exception: %s", e)
            return None

    async def download_file(self, session, url, path):
        headers = self._get_headers()
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to download {url}: {resp.status}")
                data = await resp.read()
                async with aiofiles.open(path, 'wb') as f:
                    await f.write(data)
        except Exception as e:
            _LOGGER.error("Download file exception: %s", e)
            raise

    async def download_certificates(self, session, mqtt_info, root_ca_path, client_cert_path):
        tasks = [
            self.download_file(session, mqtt_info["root"], root_ca_path),
            self.download_file(session, mqtt_info["cert"], client_cert_path)
        ]
        await asyncio.gather(*tasks)

def create_ssl_context(root_ca_path, client_cert_path, keyfile_path):
    """Create SSL context in a thread-safe manner."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=root_ca_path)
    context.load_cert_chain(certfile=client_cert_path, keyfile=keyfile_path)
    return context
