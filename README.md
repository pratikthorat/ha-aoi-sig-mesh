<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/pratikthorat/ha-aoi-sig-mesh/main/custom_components/aoi_sig_mesh/brand/dark_logo@2x.png">
  <img alt="AoI Bluetooth SIG Mesh" height="96" src="https://raw.githubusercontent.com/pratikthorat/ha-aoi-sig-mesh/main/custom_components/aoi_sig_mesh/brand/logo@2x.png">
</picture>

# AoI Bluetooth SIG Mesh

[![Release](https://img.shields.io/github/v/release/pratikthorat/ha-aoi-sig-mesh)](https://github.com/pratikthorat/ha-aoi-sig-mesh/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Tests](https://github.com/pratikthorat/ha-aoi-sig-mesh/actions/workflows/tests.yml/badge.svg)](https://github.com/pratikthorat/ha-aoi-sig-mesh/actions/workflows/tests.yml)
[![Validate](https://github.com/pratikthorat/ha-aoi-sig-mesh/actions/workflows/validate.yml/badge.svg)](https://github.com/pratikthorat/ha-aoi-sig-mesh/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Control Bluetooth SIG Mesh lights and plugs from Home Assistant. Everything runs locally: there is no vendor hub, no cloud and no account. By Arena of Innovation.

- **On/off plugs** (Generic OnOff) become switches.
- **Dimmable lights** (Light Lightness) become lights with brightness and transitions.
- **Tunable-white lights** (Light CTL) also get colour temperature, within the range the light reports.
- **One Bluetooth connection serves the whole network.** Commands to any device go through whichever mesh device is in range (a "proxy").
- **Devices report their own changes.** A change made at the device itself shows up in Home Assistant.

**Not in this version:** colour (RGB) lights, mesh groups and scenes (use Home Assistant groups instead), battery sensors and remotes, devices that need an OOB code to pair, more than one key per network, and Telink's proprietary mesh.

## Install

You need Home Assistant 2026.5 or newer with Bluetooth set up: a local adapter, or an ESPHome Bluetooth proxy with active connections enabled.

[![Open your Home Assistant and show this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=pratikthorat&repository=ha-aoi-sig-mesh&category=integration)

1. Select the button above to open this repository in HACS. Or, in HACS, open the menu, choose **Custom repositories**, and add `https://github.com/pratikthorat/ha-aoi-sig-mesh` as an **Integration**.
2. Download **AoI Bluetooth SIG Mesh** and restart Home Assistant.

## Create a network

[![Open your Home Assistant and add this integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aoi_sig_mesh)

Select the button above, or go to Settings > Devices & services > **Add integration** > AoI Bluetooth SIG Mesh. Choose **Create a new network** and give the network a name.

Home Assistant creates new random keys and keeps them in its own storage. Nothing is sent anywhere.

## Add a device

**From discovery:** factory-reset the light or plug (see its manual; it usually blinks fast once reset). Within a minute, Settings > Devices & services shows **Discovered**. Choose it, then pick the network to add it to (or name a new one) and the device type. **Automatic** works out the type from what the device reports.

**From the network:** on the network's entry, choose **Add device** and pick the device from the list.

Pairing usually takes 10–30 seconds, and up to a minute or two through a slow proxy. The first connection afterwards sets the device up, so its entity can take up to a minute to start working. A tap in that time may flip back once.

If pairing fails:
- **It stopped answering through an ESPHome Bluetooth proxy:** pairing through a proxy is fragile. Move the device near the Home Assistant machine's own Bluetooth adapter and try again.
- **It took the network's keys but did not confirm:** factory-reset it and add it again. It gets a new address, which is expected: the old one stays reserved in case the device still holds it.
- **It needs a code (OOB authentication):** this version supports only devices that pair without a code.

Devices already in one of your networks never show in the list. A device added to a mesh by another app (such as nRF Mesh) can still appear there, because Home Assistant remembers its earlier factory-reset adverts. Adding it fails cleanly. Import that app's network instead.

## Import from nRF Mesh

If your devices are already in a network made with the nRF Mesh app, bring that network in without resetting anything.

In nRF Mesh, open Settings > **Export**, export everything (including the device keys) as JSON, and paste the file's contents into **Import from nRF Mesh**.

The export holds your network keys, so delete it once the import is done. Home Assistant gets its own address in the network, so it never clashes with the phone. If the network is in the middle of an IV Update when you export it, the export loses that state. Home Assistant picks it up again from the network's beacon the first time it connects.

## Move from Tuya BLE Mesh

If a SIG Mesh device runs on the `tuya_ble_mesh` custom integration today, **Import from Tuya BLE Mesh** copies its keys and device across. Nothing is reset or re-paired.

Leave **Disable the old entry** on. The old entry is disabled, because the two integrations would fight over the device's Bluetooth connection. The new network connects only once the old entry has let go. If both stay enabled, the connection keeps passing between them, and a tap can hang for 15 seconds and then flip back.

`tuya_ble_mesh` has one entry per device, so import them one at a time. The first import creates the network; each later device from the same mesh joins it.

- **The device that creates the network** keeps its entity ids. Each old entity is renamed to `<entity_id>_tuya_ble_mesh` and the new one takes over its id, so dashboards, automations and voice assistants keep working.
- **A device that joins an existing network** gets new entity ids. Update the dashboards and automations that use it.

**To go back:**
- For the device that created the network: delete the new network entry (only while it holds no other devices), then enable the old entry again. The entity ids return to the old integration.
- For a device that joined: delete it from the network without resetting it (not **Reset and remove**, which wipes its keys), then enable its old entry again. Its old entity ids were never taken, so they work again as they were.

## Manage a device

On the device, choose **Manage device**:
- **Change device type:** for example, show a plug that drives a lamp as a light.
- **Run setup again:** repeat the setup for a device that was reset by hand or misbehaves. It keeps its type.
- **Reset and remove:** sends the device a factory reset, then removes it. If the device doesn't confirm (it is unplugged or out of range), nothing is deleted unless you tick **Delete it here even if it does not confirm**. Then factory-reset the device by hand before you use it again.

Deleting a device any other way removes it from Home Assistant but leaves it holding the network keys.

## Entities

- **Each device:** its light or switch, an **Identify** button, and a **Last seen** sensor (disabled by default).
  - Identify asks the device to show itself.
  - A light that ignores the request blinks three times and returns to how it was.
- **Each network:** **Signal strength** and **Connected via** (which proxy the network is using), and a **Reconnect** button.

## Options

- **Disconnect when idle:** minutes without commands before the Bluetooth connection is closed.
  - 0 (the default) keeps it open, so changes made at a device show up at once.
  - A higher value frees the Bluetooth slot. The next command then takes a moment longer.
- **Ask devices to report changes:** when a device is set up, its status reports are pointed at Home Assistant. This is on by default.

## Troubleshooting

- **Repairs.** These appear under Settings > Repairs:
  - **"No mesh proxy in range":** no device of the network has been heard for a while. Check power and distance, or add a Bluetooth proxy nearer the devices.
  - **"Keys rejected":** the network no longer accepts our keys, probably because it was reset or set up again with another app. Remove the network and import it again.
  - **"IV index jumped":** another controller moved the network forward by more than one step. The repair shows the new value and asks before adopting it.
  - **"Device data damaged":** a device's stored data can't be read, so it was skipped. Delete it from the network and import it again. If it was not imported, factory-reset it and add it again.
- **ESPHome Bluetooth proxies** work for everyday use. Pairing new devices is more reliable through a local adapter.
- **Range:** most mains-powered mesh devices relay messages for the others, so one device in range of Home Assistant is often enough for the whole network. If a far device stops answering, add a Bluetooth proxy nearer to it.
- **Diagnostics** (on the network's entry) never contain keys or device MACs.

## What is tested on hardware

The mesh code is checked against the sample data in the Bluetooth Mesh specification. The Home Assistant side is tested end to end against a simulated mesh. On real devices:

| What | Status |
|---|---|
| Tuya tunable-white bulb, moved from Tuya BLE Mesh | being checked by the author |
| On/off plugs | untested on hardware: reports welcome |
| Adding a factory-reset device | untested on hardware: reports welcome |
| nRF Mesh import | untested on hardware: reports welcome |

Reports go in the issue tracker at `https://github.com/pratikthorat/ha-aoi-sig-mesh/issues`.

## Credits

The mesh protocol code started from [tuya-ble-mesh](https://github.com/11z4t/tuya-ble-mesh) by 11z4t (MIT). Thank you.

Bug reports, device reports and code are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately, as [SECURITY.md](SECURITY.md) explains.

## Disclaimer

This is an independent project. It is not affiliated with, endorsed by or supported by the Bluetooth SIG, Tuya, Nordic Semiconductor, Home Assistant or the Open Home Foundation. Bluetooth® is a registered trademark of Bluetooth SIG, Inc. Other names are trademarks of their owners and are used only to say what this works with.

It switches mains-powered devices. It is provided as is, without warranty (see [LICENSE](LICENSE)): use it at your own risk, and do not rely on it where a device failing to switch could cause harm.

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
