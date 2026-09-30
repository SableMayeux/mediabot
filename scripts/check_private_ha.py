"""Manual external check of the owner's explicitly configured HA WAN boundary."""
from concurrent.futures import ThreadPoolExecutor
import errno
import ipaddress
import json
import os
import socket
import sys


PORTS = (443, 8123, 8444)


def probe(address, port):
    try:
        with socket.create_connection((str(address), port), timeout=6):
            reachable = True
    except OSError as error:
        reachable = False if isinstance(error, TimeoutError) or error.errno in (
            errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH
        ) else None
    # Do not publish the target WAN address or raw network errors.
    return {"ip_family": address.version, "port": port, "reachable": reachable}


def main():
    raw = json.loads(os.environ.get("PRIVATE_HA_AUDIT_TARGETS", "[]"))
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        raise ValueError("Configure the owner's explicit WAN targets before this manual audit")
    addresses = [ipaddress.ip_address(value) for value in raw]
    if not all(address.is_global for address in addresses):
        raise ValueError("External audit targets must be public IP addresses")
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda pair: probe(*pair),
                                    [(address, port) for address in addresses for port in PORTS]))
    print(json.dumps({"external_ha_port_checks": results}, sort_keys=True))
    if any(item["reachable"] is not False for item in results):
        print("A WAN listener is reachable or the route is inconclusive. Investigate before asserting HA is LAN-only.")
        return 1
    print("Checked WAN listeners were unreachable from this external runner.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
