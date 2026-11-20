"""Signed destination announcements, kept separate from network routing."""

import logging


def read_announce(raw, address=None):
    """Verify the packet signature and bind its public keys to its destination."""
    import RNS

    if not isinstance(raw, bytes) or not 1 <= len(raw) <= RNS.Reticulum.MTU:
        raise ValueError("Invalid announce packet size")
    packet = RNS.Packet(None, raw)
    if (
        not packet.unpack()
        or packet.packet_type != RNS.Packet.ANNOUNCE
        or packet.destination_type != RNS.Destination.SINGLE
    ):
        raise ValueError("Expected a SINGLE destination announce")
    if address is not None and packet.destination_hash.hex() != address:
        raise ValueError("Announce destination does not match the record")
    key_size = RNS.Identity.KEYSIZE // 8
    name_size = RNS.Identity.NAME_HASH_LENGTH // 8
    ratchet_size = (
        RNS.Identity.RATCHETSIZE // 8
        if packet.context_flag == RNS.Packet.FLAG_SET
        else 0
    )
    prefix_size = key_size + name_size + 10 + ratchet_size
    signature_size = RNS.Identity.SIGLENGTH // 8
    if len(packet.data) < prefix_size + signature_size:
        raise ValueError("Truncated announce")
    public_key = packet.data[:key_size]
    identity = RNS.Identity(create_keys=False)
    identity.load_public_key(public_key)
    expected = RNS.Identity.truncated_hash(
        packet.data[key_size : key_size + name_size] + identity.hash
    )
    if packet.destination_hash != expected:
        raise ValueError("Announce keys do not derive its destination")
    signed = (
        packet.destination_hash
        + packet.data[:prefix_size]
        + packet.data[prefix_size + signature_size :]
    )
    if not identity.validate(
        packet.data[prefix_size : prefix_size + signature_size], signed
    ):
        raise ValueError("Invalid announce signature")
    return packet


def announce_time(raw):
    """Read the source timestamp from an already validated announcement."""
    import RNS

    packet = RNS.Packet(None, raw)
    packet.unpack()
    offset = RNS.Identity.KEYSIZE // 8 + RNS.Identity.NAME_HASH_LENGTH // 8 + 5
    return int.from_bytes(packet.data[offset : offset + 5], "big")


def import_announce(raw, address):
    """Populate Reticulum's identity, app-data and ratchet stores without adding a route."""
    import RNS

    packet = read_announce(raw, address)
    if not RNS.Identity.validate_announce(packet):
        raise ConnectionError("Reticulum rejected the resolved announcement")


class AnnounceCollector:
    aspect_filter = None
    receive_path_responses = True

    def __init__(self, registry):
        """Keep announcements only for application destinations in this registry."""
        self.registry = registry

    def start(self):
        """Listen for broadcasts and path-response announcements."""
        import RNS

        RNS.Transport.register_announce_handler(self)
        return self

    def close(self):
        """Stop listening when the name service shuts down."""
        import RNS

        RNS.Transport.deregister_announce_handler(self)

    def received_announce(
        self, destination_hash, announced_identity, app_data, announce_packet_hash
    ):
        """Fetch the original signed packet rather than manufacturing unsigned metadata."""
        import RNS

        address = destination_hash.hex()
        if self.registry.lookup_address(address) is None:
            return
        packet = RNS.Transport.get_cached_packet(
            announce_packet_hash, packet_type="announce"
        )
        if packet is None:
            logging.warning(
                "Signed announce unavailable for registered destination %s", address
            )
            return
        try:
            self.registry.store_announce(address, packet.raw)
        except (ValueError, TypeError):
            logging.warning("Rejected an invalid stored announce for %s", address)
