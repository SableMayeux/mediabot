"""Validate this homelab's existing storage and gateway deployment boundary."""

import argparse
import json
import sys


DATA_SOURCE = "/opt/stacks/mediabot/data"
LIFE_SOURCE = "/opt/stacks/life/vault/Life/Inbox"
SECRET_FILES = {
    "torrent_intake_token": "/opt/stacks/torrent/secrets/torrent_intake_token",
    "life_gateway_token": "/opt/stacks/life-workflows/secrets/life_gateway_token",
    "local_ai_token": "/opt/stacks/local-ai/secrets/local_ai_token",
}
GATEWAY_NETWORKS = {
    "torrent_intake": "dogginator_torrent_intake",
    "life_intake": "life_intake",
    "local_ai_frontend": "local-ai_frontend",
    "web_search_frontend": "mediabot_search_frontend",
}


class DeploymentBoundaryError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise DeploymentBoundaryError(message)


def validate_compose(document, version):
    services = document.get("services", {})
    require(set(services) == {"mediabot"}, "Expected only the existing MediaBot service")
    service = services["mediabot"]
    require(service.get("container_name") == "mediabot", "Existing container name changed")
    require(service.get("user") == "1000:1000" and service.get("read_only") is True,
            "Container user or read-only root changed")
    require(not service.get("ports") and not service.get("network_mode"), "Unexpected host exposure")
    mounts = {item.get("target"): item for item in service.get("volumes", [])}
    require(set(mounts) == {"/app/data", "/life-vault/Life/Inbox"}, "Unexpected application storage mounts")
    for target, source in (("/app/data", DATA_SOURCE), ("/life-vault/Life/Inbox", LIFE_SOURCE)):
        mount = mounts[target]
        require(mount.get("type") == "bind" and mount.get("source") == source
                and not mount.get("read_only"), "Existing bind storage was replaced or made read-only")
    require(not document.get("volumes"), "Named volumes are forbidden in the homelab deployment")
    require(set(service.get("networks", {})) == {"default", *GATEWAY_NETWORKS}, "Gateway network membership changed")
    for name, actual in GATEWAY_NETWORKS.items():
        configured = document.get("networks", {}).get(name, {})
        require(configured.get("external") is True and configured.get("name") == actual,
                "An existing gateway network was replaced")
    require({item.get("source") for item in service.get("secrets", [])} == set(SECRET_FILES),
            "Gateway secret selection changed")
    require(set(document.get("secrets", {})) == set(SECRET_FILES), "Unexpected secret definition")
    for name, path in SECRET_FILES.items():
        require(document["secrets"][name].get("file") == path, "Gateway secret file changed")
    health = service.get("healthcheck", {}).get("test", [])
    require("--version" in health and health[health.index("--version") + 1] == version,
            "Selected homelab Compose health version does not match the release")
    require(service.get("environment", {}).get("LIFE_CAPTURE_PATH") == "/life-vault/Life/Inbox",
            "Private capture storage changed")


def validate_container(document, *, upgrading=False):
    require(isinstance(document, list) and len(document) == 1, "Expected one existing container")
    container = document[0]
    require(container.get("Name") == "/mediabot", "Unexpected live container")
    mounts = {item.get("Destination"): item for item in container.get("Mounts", [])}
    for destination, source in (("/app/data", DATA_SOURCE), ("/life-vault/Life/Inbox", LIFE_SOURCE)):
        mount = mounts.get(destination, {})
        require(mount.get("Type") == "bind" and mount.get("Source") == source
                and mount.get("RW") is True, "Live bind storage does not match this homelab")
    for name, source in SECRET_FILES.items():
        mount = mounts.get("/run/secrets/" + name, {})
        require(mount.get("Type") == "bind" and mount.get("Source") == source
                and mount.get("RW") is False, "Live gateway secret mount changed")
    networks = set(container.get("NetworkSettings", {}).get("Networks", {}))
    required_networks = set(GATEWAY_NETWORKS.values())
    if upgrading:
        required_networks.discard("mediabot_search_frontend")
    require(required_networks <= networks, "Live gateway network membership changed")
    require(not networks.intersection({"local-ai_private", "nextcloud_backend", "nextcloud_frontend"}),
            "Live container reaches a private backend network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("compose", "container"), required=True)
    parser.add_argument("--version")
    parser.add_argument("--upgrading", action="store_true", help="Accept the prior live network layout before upgrading")
    args = parser.parse_args()
    try:
        document = json.load(sys.stdin)
        if args.kind == "compose":
            require(bool(args.version), "Release version is required")
            validate_compose(document, args.version)
        else:
            validate_container(document, upgrading=args.upgrading)
    except (DeploymentBoundaryError, ValueError, TypeError, KeyError, IndexError) as exc:
        detail = str(exc) if isinstance(exc, DeploymentBoundaryError) else "Invalid deployment metadata"
        print("Homelab deployment rejected: " + detail, file=sys.stderr)
        return 1
    print("HOMELAB_" + args.kind.upper() + "_BINDINGS ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
