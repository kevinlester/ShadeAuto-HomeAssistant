# Contributing

## Development setup

The integration targets Home Assistant 2026.10, which needs Python 3.14.
[uv](https://docs.astral.sh/uv/) is the quickest way to get a matching
environment:

```bash
uv venv --python 3.14
uv pip install -r requirements_test.txt
```

`requirements_test.txt` pins `pytest-homeassistant-custom-component`, which in
turn pins the matching `homeassistant` release and the pytest plugins the
Home Assistant test harness relies on.

## Checks

Run the same checks CI runs before pushing:

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m pytest
.venv/bin/bandit -c pyproject.toml -r custom_components tools -ll
```

`pytest` enforces a coverage floor (see `pyproject.toml`). CI additionally
runs hassfest, the HACS validator, CodeQL and zizmor.

## Supported Home Assistant versions

The integration supports **Home Assistant 2025.8 and newer**. The minimum is
declared in `hacs.json` (`homeassistant`), so HACS keeps users on older
releases at the last version that supported them.

**The rule:** support back to the oldest release that needs only
*import-level* shims, and raise the minimum when a Home Assistant
deprecation would force a *behavioural* fork. An import-level shim picks the
newer API when Home Assistant has it and the older one otherwise, with the
same behaviour either way. A behavioural fork is two real code paths, for
example an options flow that reloads itself on new releases but needs an
update listener on old ones. We don't keep those: Home Assistant removes
deprecated APIs 6-12 months after introducing their replacements, so an old
minimum turns shims into a permanent second stack, and tests and CI double
with it.

All shims live in `custom_components/shadeauto/compat.py`, and each names the
release that makes it removable:

| Shim | Delete when the minimum reaches |
|---|---|
| `vol`: probatio on 2026.10+, voluptuous before | 2026.10 |
| `get_device_by_identifier()`: `async_get_device_by_identifier` on 2026.8+ | 2026.8 |
| `link_to_hub()`: `via_device_id` on 2026.8+, `via_device` before | 2026.8 |

Before using a Home Assistant API, check that it exists in the minimum
release. The CI job "Tests on the minimum Home Assistant" runs the whole suite
on it, which catches most slips. If the API is newer, add an import-level
shim here, or raise the minimum if a shim would change behaviour.

When raising the minimum:

1. Check the share of installs it would drop on
   [analytics.home-assistant.io](https://analytics.home-assistant.io)
   (`current_data.json` has per-release counts). In October 2026, 2025.8 or
   newer covered 93 % of reporting installs.
2. Update `hacs.json`, the README's requirements, this section,
   `requirements_test_min.txt` (the `pytest-homeassistant-custom-component`
   release that pins the new minimum) and the `test-min` job in
   `.github/workflows/ci.yml` (Python version and `MIN_EXCLUDE_NEWER`, a date
   a day or two after that release).
3. Delete the shims the new minimum makes unnecessary, and say so in the
   changelog.

To run the suite on the minimum locally:

```bash
uv venv --python 3.13 .venv-min
uv pip install --python .venv-min/bin/python --exclude-newer 2025-08-24T00:00:00Z -r requirements_test_min.txt
.venv-min/bin/python -m pytest -p no:cacheprovider -o addopts=""
```

## Working against a real hub

`tools/hubwatch.py` is the instrument for everything hardware-related. It
needs only the standard library.

```bash
# print shade names, then a line for every position/target change
python3 tools/hubwatch.py --host HUB_IP --names

# send one command and watch what the hub does afterwards
python3 tools/hubwatch.py --host HUB_IP --control 12177 BottomRailPosition=0 MiddleRailPosition=100

# dump every /status document (for new fixtures)
python3 tools/hubwatch.py --host HUB_IP --raw --duration 5
```

Rules of thumb:

- Never send limit, calibration or pairing fields (`Set*Limit`, `Clean*`,
  `Calibration`, `CleanRfPairing`, ...). They reconfigure the motor.
- Record what you observe in `docs/HUB_PROTOCOL.md`, with timestamps. The
  integration's behaviour is justified by that file.
- New fixtures under `tests/fixtures/` must be real hub responses. Scrub only
  identity: `ThingName`, `GeoLoc`, `WiFiSSID`, and room names if you want.

## Tests

`tests/conftest.py` provides `FakeHub`, which serves the fixtures and applies
the hub's command rules. Extend it when the hub teaches us something new,
rather than patching methods in individual tests. See the *Testing notes* in
`docs/ARCHITECTURE.md` for the time-freezing rules.

## Pull request checklist

- [ ] Tests for the new behaviour, and the suite is green.
- [ ] `ruff`, `mypy` and `bandit` are clean.
- [ ] `docs/ARCHITECTURE.md` or `docs/HUB_PROTOCOL.md` updated if behaviour
      or protocol knowledge changed.
- [ ] `CHANGELOG.md` has an entry under *Unreleased*.
- [ ] For a release: bump `manifest.json`, move the changelog entry under the
      version, tag `vX.Y.Z` (the release workflow checks that the tag and the
      manifest agree and publishes the GitHub release that HACS installs).
