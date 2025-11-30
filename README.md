# Lepro B1 Controller (Home Assistant Custom Integration)

Monitor and control your **Lepro B1** devices from Home Assistant.

This custom integration is a fork of [Sanji78/lepro_led](https://github.com/Sanji78/lepro_led), modified to **fully support Lepro B1 lights**. It logs in to the **Lepro Cloud**, retrieves your specific B1 devices, and exposes them as fully controllable lights in HA with improved stability.

> ⚠️ **Note:** This is a third‑party project and is not affiliated with Lepro.

---

## ✨ Features

- **Full B1 Compatibility:** Specifically patched to ensure Lepro B1 lights work correctly.
- **Cloud Login:** Seamless integration using your existing **Lepro** account (email + password).
- **Auto-Discovery:** Automatically finds lights and strips associated with your account.
- **Device Control:**
  - Turn lights **on/off**.
  - Adjust **brightness** and **color temperature**.
  - Set **RGB colors** and dynamic **effects**.
- **Sensors & Status:**
  - Real-time connection state (Online/Offline).
  - Device info (Model, Firmware, MAC address).
- **Stability:** Includes automatic token renewal to keep your devices connected.

---

## 🔧 Installation

### Option A — HACS (Recommended)
1. Ensure [HACS](https://hacs.xyz/) is installed in Home Assistant.
2. Navigate to **HACS → Integrations**.
3. Click the **⋮ (three dots)** in the top right corner and select **Custom repositories**.
4. Add the following URL:
   `https://github.com/haumlab/lepro_led`
5. Select **Integration** as the category and click **Add**.
6. Find **Lepro B1 Controller** in the list and click **Download**.
7. **Restart** Home Assistant.

### Option B — Manual
1. Download this repository.
2. Copy the `custom_components/lepro_led_b1` folder into your Home Assistant configuration directory:
   - Path: `<config>/custom_components/lepro_led_b1`
3. **Restart** Home Assistant.

---

## ⚙️ Configuration

1. Go to **Settings → Devices & services**.
2. Click **Add Integration** in the bottom right.
3. Search for **Lepro LED**.
4. Enter your **Lepro email and password**.
5. Upon success, entities will be generated for your B1 devices.

---

## 🧪 Supported Versions
- **Home Assistant:** 2024.8 or newer.

---

## 🐞 Troubleshooting
- **Logs:** Check **Settings → System → Logs** and filter for `custom_components.lepro_led_b1` to see detailed errors.
- **Login Issues:** If authentication fails, verify your credentials by logging into the official Lepro mobile app first.
- **Connectivity:** If entities become unavailable, ensure your Home Assistant instance has an active internet connection to communicate with the Lepro Cloud.

---

## 🙌 Credits & Contributing
This project is heavily based on the work of **Sanji78**. Huge thanks to them for the original integration foundation.

Pull requests to improve B1 functionality further are welcome. Please open an issue if you encounter bugs.

---

## ❤️ Donate (to the original creator)
If this integration solved your B1 issues and you'd like to support the work, consider buying me a coffee:

**[PayPal](https://www.paypal.me/elenacapasso80)**

..and yes... 😊 the paypal account is correct. Thank you so much!

---

## 📜 License
[MIT](LICENSE.md)
