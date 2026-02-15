import sys
import os
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# Mock homeassistant package BEFORE importing custom_components
mock_ha = MagicMock()
sys.modules["homeassistant"] = mock_ha
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.typing"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.entity_platform"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.light"] = MagicMock()

# Add custom_components to path
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "custom_components"))

try:
    from custom_components.lepro_led.lepro_api import LeproAPI
    from custom_components.lepro_led.lepro_mqtt import LeproMQTTClient
except ImportError as e:
    print(f"Import failed: {e}")
    sys.exit(1)

@pytest.mark.asyncio
async def test_api_init():
    api = LeproAPI("user", "pass", "mac")
    assert api.account == "user"
    assert api.token is None
    # Check headers are initialized (but timestamp might not be set yet until _get_headers called)
    assert "Content-Type" in api.headers

@pytest.mark.asyncio
async def test_mqtt_init():
    client = LeproMQTTClient("host", 1234, None, "id")
    assert client._host == "host"
    assert client._port == 1234

@pytest.mark.asyncio
async def test_mqtt_subscribe():
    client = LeproMQTTClient("host", 1234, None, "id")
    await client.subscribe("test/topic")
    assert "test/topic" in client._subscriptions
