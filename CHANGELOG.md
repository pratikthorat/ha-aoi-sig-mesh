# Changelog

## 0.1.0

First release.

- Lights (on/off, brightness, colour temperature, transitions) and plugs over Bluetooth SIG Mesh, through one proxy connection per network.
- Create a network, import from nRF Mesh, or move from Tuya BLE Mesh without re-pairing.
- Add factory-reset devices from Bluetooth discovery or Add device. Manage device: change type, run setup again, reset and remove.
- Signal strength, connected-via and last-seen sensors; identify and reconnect buttons; repairs; diagnostics without keys.
- Tested on Home Assistant 2026.5 and 2026.10, with no deprecated calls.


## 0.1.1

- Add SIG Mesh HSL/RGB color control and preserving the selected color when adjusting brightness.
- Fix lights being incorrectly reported as on after a status poll.
- Add a quirk for AiDot Solar Spotlights [SL5C] (CID `0x0504`, PID `0x6212`) to send Generic OnOff OFF when turning the lights off, instead of setting brightness to zero and dimming them to minimum.
- Tested RGB color control with Otdair branded RGB flood lights which originally runs on the Tuya API as well with the AiDot Solar Spotlight


