import sys
import os
from unittest.mock import MagicMock
from types import ModuleType

# Add the current directory to sys.path so we can import custom_components
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock homeassistant and other missing modules
def mock_package(name):
    m = ModuleType(name)
    sys.modules[name] = m
    return m

ha = mock_package("homeassistant")
ha_core = mock_package("homeassistant.core")
ha_core.callback = lambda x: x
ha_core.HomeAssistant = MagicMock

ha_comp = mock_package("homeassistant.components")
ha_light = mock_package("homeassistant.components.light")
ha_helpers = mock_package("homeassistant.helpers")
ha_ent = mock_package("homeassistant.helpers.entity_platform")
ha_ent.AddEntitiesCallback = MagicMock
ha_typ = mock_package("homeassistant.helpers.typing")
ha_typ.ConfigType = MagicMock
ha_cfg = mock_package("homeassistant.config_entries")
ha_cfg.ConfigEntry = MagicMock
ha_cv = mock_package("homeassistant.helpers.config_validation")
ha_cv.config_entry_only_config_schema = MagicMock

ha_light.LightEntity = type("LightEntity", (), {"async_added_to_hass": MagicMock()})
ha_light.ColorMode = MagicMock()
ha_light.ATTR_BRIGHTNESS = "brightness"
ha_light.ATTR_HS_COLOR = "hs_color"
ha_light.ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"

# Other dependencies
sys.modules["aiomqtt"] = MagicMock()
sys.modules["aiofiles"] = MagicMock()
sys.modules["numpy"] = MagicMock()
sys.modules["aiohttp"] = MagicMock()

# Now import the class to test
from custom_components.lepro_led.light import LeproLedLight

def test_map_d4_to_kelvin():
    # d4 (0-1000) to Kelvin (2700-6500)
    # 2700 + (d4 / 1000.0) * (6500 - 2700)

    # Test 0 -> 2700
    assert LeproLedLight._map_d4_to_kelvin(0) == 2700

    # Test 1000 -> 6500
    assert LeproLedLight._map_d4_to_kelvin(1000) == 6500

    # Test 500 -> 4600
    assert LeproLedLight._map_d4_to_kelvin(500) == 4600

    # Test 250 -> 3650
    # 2700 + 0.25 * 3800 = 2700 + 950 = 3650
    assert LeproLedLight._map_d4_to_kelvin(250) == 3650

def test_map_kelvin_to_d4():
    # Kelvin (2700-6500) to d4 (0-1000)

    # Test 2700 -> 0
    assert LeproLedLight._map_kelvin_to_d4(2700) == 0

    # Test 6500 -> 1000
    assert LeproLedLight._map_kelvin_to_d4(6500) == 1000

    # Test 4600 -> 500
    assert LeproLedLight._map_kelvin_to_d4(4600) == 500

    # Out of range low
    assert LeproLedLight._map_kelvin_to_d4(2000) == 0

    # Out of range high
    assert LeproLedLight._map_kelvin_to_d4(7000) == 1000

def test_map_ha_to_lepro():
    # HA (0-255) to Lepro (10-1000)

    # Test 0 -> 10 (min is 10)
    assert LeproLedLight._map_ha_to_lepro(0) == 10

    # Test 255 -> 1000
    assert LeproLedLight._map_ha_to_lepro(255) == 1000

    # Test None -> 1000
    assert LeproLedLight._map_ha_to_lepro(None) == 1000

    # Test 127 -> 498
    # round(127 * 1000 / 255) = round(498.039) = 498
    assert LeproLedLight._map_ha_to_lepro(127) == 498

def test_map_lepro_to_ha():
    # Lepro (10-1000) to HA (0-255)

    # Test 10 -> 3
    # round(10 * 255 / 1000) = round(2.55) = 3
    assert LeproLedLight._map_lepro_to_ha(10) == 3

    # Test 1000 -> 255
    assert LeproLedLight._map_lepro_to_ha(1000) == 255

    # Test 0 -> 0
    assert LeproLedLight._map_lepro_to_ha(0) == 0

    # Test None -> 255
    assert LeproLedLight._map_lepro_to_ha(None) == 255

    # Test 500 -> 128
    # round(500 * 255 / 1000) = round(127.5) = 128
    assert LeproLedLight._map_lepro_to_ha(500) == 128
