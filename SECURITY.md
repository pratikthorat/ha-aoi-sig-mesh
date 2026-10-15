# Security

This integration holds the keys of your Bluetooth mesh network. Anyone with those keys can control every device in it, so a bug that leaks them, or lets a nearby device take control, matters.

## Reporting a problem

Report it privately through [GitHub's private reporting](https://github.com/pratikthorat/ha-aoi-sig-mesh/security/advisories/new), not in a public issue. Include what you found, how to reproduce it, and what an attacker could do with it. You should hear back within a week.

Only the latest release gets fixes.

## What is in scope

- Keys or device MACs appearing in logs, diagnostics or anything else that leaves Home Assistant.
- Messages from outside the network being accepted: replays, forged or malformed packets.
- Provisioning that gives the network keys to a device the user did not choose.

Keys are kept in Home Assistant's own storage, like every integration's credentials. Protecting that storage, and the Bluetooth range around your home, is out of scope.
