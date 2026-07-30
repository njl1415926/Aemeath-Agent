from __future__ import annotations

import ipaddress


def validated_trusted_proxy_ips(raw_value: str | None, *, environment: str) -> str:
    if raw_value is None or not raw_value.strip():
        if environment == "prod":
            raise ValueError("AEMEATH_TRUSTED_PROXY_IPS is required in prod.")
        return "127.0.0.1"
    values = [value.strip() for value in raw_value.split(",") if value.strip()]
    if environment == "prod" and len(values) != 1:
        raise ValueError("Prod must trust exactly one fixed Caddy IP.")
    normalized: list[str] = []
    for value in values:
        if value == "*":
            raise ValueError("AEMEATH_TRUSTED_PROXY_IPS must not contain a wildcard.")
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("AEMEATH_TRUSTED_PROXY_IPS must contain fixed IP addresses.") from exc
        if address.is_unspecified or address.is_multicast:
            raise ValueError("AEMEATH_TRUSTED_PROXY_IPS contains an unsafe address.")
        normalized.append(address.compressed)
    return ",".join(normalized)
