# Contributing

Bug reports and device reports are the most useful contribution: use the issue templates. For code, open an issue first for anything larger than a fix, so we can agree on the approach.

## Layout

- `custom_components/aoi_sig_mesh/mesh/` is the Bluetooth Mesh protocol. It does not import Home Assistant, and is checked against the sample data in the Bluetooth Mesh specification.
- The rest of `custom_components/aoi_sig_mesh/` is the Home Assistant side: config flow, coordinator, entities.
- `tests/mesh/` and `tests/release/` run without Home Assistant. `tests/components/aoi_sig_mesh/` runs inside a real Home Assistant against a simulated mesh (`tests/sim/`).

## Running the checks

The protocol and release tests, plus lint and formatting:

```sh
python3 -m venv .venv
.venv/bin/pip install pytest cryptography pyyaml ruff
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest -q
```

The Home Assistant tests need Python 3.14 and the versions in `.github/workflows/tests.yml`. CI
runs them on the oldest Home Assistant we support and on a recent one, and a test fails on any
call Home Assistant reports as deprecated. For the oldest:

```sh
python3.14 -m venv .venv-ha
.venv-ha/bin/pip install homeassistant==2026.5.4 pytest-homeassistant-custom-component==0.13.333 \
  aiousbwatcher==1.1.2 serialx==1.7.3 aioesphomeapi==44.24.1
.venv-ha/bin/pip install $(.venv-ha/bin/python tools/ci_ha_pins.py)
.venv-ha/bin/python -m pytest -c tests/components/aoi_sig_mesh/pytest.ini tests/components/aoi_sig_mesh -q
```

CI runs both on every push and pull request.

## Pull requests

- Add a test that fails without your change.
- Keep user-facing text in `strings.json` and `translations/en.json` the same, and say "Actions", not "services", as Home Assistant does.
- Never commit real keys, nRF Mesh exports, or a real device's MAC. Tests use made-up MACs (`AA:...`, `DC:23:50:00:00:...`), and `tests/release/test_docs.py` checks for others.
- Add a line to `CHANGELOG.md` under the next version.
