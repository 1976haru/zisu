"""Profile-scoped network preference without VPN control or location inference."""
from __future__ import annotations

from .youtube_api import _network_signature

DEFAULT = "default"
USER_MANAGED_VPN = "user_vpn"
MODES = (DEFAULT, USER_MANAGED_VPN)


def validate_mode(value: str) -> str:
    if value not in MODES:
        raise ValueError("Network mode must be 'default' or 'user_vpn'.")
    return value


def profile_mode(profile: dict | None) -> str:
    try:
        return validate_mode((profile or {}).get("network_mode", DEFAULT))
    except (TypeError, ValueError):
        return DEFAULT


def status(profile: dict | None, *, signature_provider=_network_signature) -> dict:
    """Separate the user's preference from facts this app can actually observe.

    A local interface signature only indicates that an interface fingerprint was
    available. It does not identify a VPN, public IP, country, or provider.
    """
    signature = signature_provider()
    return {
        "configured_mode": profile_mode(profile),
        "interface_observation": "available" if signature else "unknown",
        "vpn_connection": "unknown",
        "country": "unknown",
    }


def changed(before: str, after: str) -> bool:
    """Only compare non-empty ephemeral fingerprints; never persist either one."""
    return bool(before and after and before != after)
