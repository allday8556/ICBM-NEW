"""Network address helpers shared by configuration and the egress guard."""

import ipaddress


def is_loopback_host(host: str) -> bool:
    """True when ``host`` names this machine only (127.0.0.0/8, ::1 or localhost).

    Unspecified addresses such as ``0.0.0.0`` are deliberately *not* loopback.
    """
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name == "localhost" or name.endswith(".localhost"):
        return True
    name = name.split("%", 1)[0]
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False
