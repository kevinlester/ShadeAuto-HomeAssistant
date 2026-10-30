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
