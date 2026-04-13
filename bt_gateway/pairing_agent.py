"""BlueZ Agent1 implementation that auto-approves all pairing requests.

Registered as the default agent so the gateway can pair with devices and the
PLC without a human having to type a PIN or confirm a passkey.  All pairing
callbacks return success with the default PIN "0000" / passkey 0 and accept
every authorisation request.
"""

import logging

import dbus
import dbus.service

logger = logging.getLogger(__name__)

AGENT_IFACE = "org.bluez.Agent1"
AGENT_PATH = "/org/bluez/btgateway/pairing_agent"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
DEFAULT_PIN = "0000"


class PairingAgent(dbus.service.Object):
    """Auto-approving BlueZ pairing agent."""

    def __init__(self, bus, default_pin=DEFAULT_PIN):
        self._default_pin = default_pin
        super().__init__(bus, AGENT_PATH)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        logger.info("Pairing agent released by BlueZ")

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        logger.info("Auto-authorising service %s for %s", uuid, device)
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        logger.info("PIN requested for %s — providing default %s",
                    device, self._default_pin)
        return self._default_pin

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        logger.info("Passkey requested for %s — providing 0", device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        logger.info("DisplayPasskey for %s: %06u (entered %u)",
                    device, passkey, entered)

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        logger.info("DisplayPinCode for %s: %s", device, pincode)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        logger.info("Auto-confirming passkey %06u for %s", passkey, device)
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        logger.info("Auto-authorising pairing for %s", device)
        return

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        logger.info("Pairing cancelled")


def register_agent(bus, capability="NoInputNoOutput"):
    """Register the pairing agent as the default agent and return it.

    Capability ``NoInputNoOutput`` tells BlueZ we have no way to display a
    passkey or accept input, which makes BlueZ skip interactive flows and
    proceed with "Just Works" pairing whenever possible.
    """
    agent = PairingAgent(bus)
    try:
        manager = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez"),
            AGENT_MANAGER_IFACE,
        )
        manager.RegisterAgent(AGENT_PATH, capability)
        manager.RequestDefaultAgent(AGENT_PATH)
        logger.info("Pairing agent registered as default (%s)", capability)
    except dbus.DBusException as e:
        logger.error("Failed to register pairing agent: %s", e)
    return agent


def unregister_agent(bus):
    try:
        manager = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez"),
            AGENT_MANAGER_IFACE,
        )
        manager.UnregisterAgent(AGENT_PATH)
        logger.info("Pairing agent unregistered")
    except dbus.DBusException as e:
        logger.warning("Failed to unregister pairing agent: %s", e)
