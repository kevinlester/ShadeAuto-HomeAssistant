# ShadeAuto-HomeAssistant

Control Norman **ShadeAuto** shades locally via the hub’s undocumented HTTP API.  
✅ Multiple hubs • ✅ Covers (open/close/set position) • ✅ Battery **%** sensor per shade

---
> ⚠️ This uses **undocumented** local endpoints (`/NM/v1/*`). Firmware updates may change behavior. Requires access to each hub's TCP port **10123** for operation.
---

## Features
- **Auto-discover shades** from the hub.
- One **Cover** entity per shade: open/close/**set position** (0–100).
- One **Battery (%)** sensor per shade.
- Works with **multiple hubs** (add each hub by IP).

## Requirements
- ShadeAuto hub reachable on your LAN (port **10123**).
- Home Assistant 2024.6+ (tested).

## Installation

### Via HACS (custom repository)
1. HACS → *Integrations* → ⋯ → **Custom repositories** → add your GitHub repo URL, Category **Integration**.
2. Find **ShadeAuto (local)** → **Install** → **Restart** Home Assistant.
3. Settings → *Devices & Services* → **Add Integration** → **ShadeAuto (local)** → enter your **hub IP**.
4. Repeat step 3 for additional hubs.

### Manual
1. Copy `custom_components/shadeauto/` into your HA `config/custom_components/`.
2. Restart HA, then add the integration.

## Configuration
- **Host**: hub IP (e.g., `192.168.1.50`).
- **Options** → *Poll seconds*: default **30s**. (Faster polling increases traffic.)

## Entities
- `cover.<shade_name>`  
  - Supports **open**, **close**, **set position**.
  - Attributes:  
    - `shadeauto_uid`: internal hub UID  
    - `battery_raw`: raw value the hub returns (may be 0–100 or volts)

- `sensor.<shade_name>_battery`  
  - **device_class**: `battery`, **unit**: `%`  
  - If the hub returns volts instead of %, the integration maps **3.30–4.20 V → 0–100%**.

## Example use

**Open a shade to 40%**
```yaml
service: cover.set_cover_position
target: { entity_id: cover.deck_shade }
data: { position: 40 }
