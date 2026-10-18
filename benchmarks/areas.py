"""Published bidding-zone EIC identifiers, shared by the offline comparison clients."""

ENTSOE_AREAS = {
    "DK1": "10YDK-1--------W",
    "DK2": "10YDK-2--------M",
    "FI": "10YFI-1--------U",
    "NO1": "10YNO-1--------2",
    "NO2": "10YNO-2--------T",
    "NO3": "10YNO-3--------J",
    "NO4": "10YNO-4--------9",
    "NO5": "10Y1001A1001A48H",
    "SE1": "10Y1001A1001A44P",
    "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L",
    "SE4": "10Y1001A1001A47J",
}
ESETT_AREAS = {
    zone: code.replace("-", "_")
    if zone == "FI" or (zone.startswith("NO") and zone != "NO5")
    else code
    for zone, code in ENTSOE_AREAS.items()
}
