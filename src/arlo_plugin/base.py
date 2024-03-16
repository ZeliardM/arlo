from __future__ import annotations

from typing import List, TYPE_CHECKING

from scrypted_sdk import ScryptedDeviceBase
from scrypted_sdk.types import Device

from .logging import ScryptedDeviceLoggerMixin
from .util import BackgroundTaskMixin

if TYPE_CHECKING:
    # https://adamj.eu/tech/2021/05/13/python-type-hints-how-to-fix-circular-imports/
    from .provider import ArloProvider


class ArloDeviceBase(ScryptedDeviceBase, ScryptedDeviceLoggerMixin, BackgroundTaskMixin):
    nativeId: str = None
    arlo_device: dict = None
    arlo_basestation: dict = None
    arlo_capabilities: dict = None
    arlo_properties: dict = None
    arlo_smartFeatures: dict = None
    provider: ArloProvider = None
    stop_subscriptions: bool = False

    def __init__(self, nativeId: str, arlo_device: dict, arlo_basestation: dict, arlo_properties: dict, provider: ArloProvider) -> None:
        super().__init__(nativeId=nativeId)

        self.logger_name = nativeId

        self.nativeId = nativeId
        self.arlo_device = arlo_device
        self.arlo_basestation = arlo_basestation
        self.arlo_properties = arlo_properties
        self.provider = provider
        self.logger.setLevel(self.provider.get_current_log_level())

        try:
            self.arlo_capabilities = {} if nativeId.endswith("smss") else self.provider.arlo.GetDeviceCapabilities(self.arlo_device)
            self.arlo_smartFeatures = {} if nativeId.endswith("smss") else self.provider.arlo.GetSmartFeatures(self.arlo_device)
        except Exception as e:
            self.logger.warning(f"Could not load device capabilities and smart features: {e}")
            self.arlo_capabilities = {}
            self.arlo_smartFeatures = {}
        self.create_task(self._do_delayed_init())

    async def _do_delayed_init(self) -> None:
        await self.provider.device_discovery_done()
        await self.delayed_init()

    async def delayed_init(self) -> None:
        """Override this function to perform initialization after device discovery is complete."""
        pass

    def __del__(self) -> None:
        self.stop_subscriptions = True
        self.cancel_pending_tasks()

    def get_applicable_interfaces(self) -> List[str]:
        """Returns the list of Scrypted interfaces that applies to this device."""
        return []

    def get_device_type(self) -> str:
        """Returns the Scrypted device type that applies to this device."""
        return ""

    def get_device_manifest(self) -> Device:
        """Returns the Scrypted device manifest representing this device."""
        parent = None
        if self.arlo_device.get("parentId") and self.arlo_device["parentId"] != self.arlo_device["deviceId"]:
            parent = self.arlo_device["parentId"]

        if parent in self.provider.hidden_device_ids:
            parent = None

        return {
            "info": {
                "model": f"{self.arlo_device['modelId']} {self.arlo_properties['hwVersion'].replace(self.arlo_device['modelId'], '').strip()}".strip(),
                "manufacturer": "Arlo",
                "serialNumber": self.arlo_device["deviceId"],
                "firmware": self.arlo_properties["swVersion"],
            },
            "nativeId": self.arlo_device["deviceId"],
            "name": self.arlo_device["deviceName"],
            "interfaces": self.get_applicable_interfaces(),
            "type": self.get_device_type(),
            "providerNativeId": parent,
        }

    def get_builtin_child_device_manifests(self) -> List[Device]:
        """Returns the list of child device manifests representing hardware features built into this device."""
        return []

    def has_feature(self, feature: str) -> bool:
        smartfeatures = self.arlo_smartFeatures.get("planFeatures", {})
        return smartfeatures.get(feature, False)
    
    def has_capability(self, capability: str, subCapability: str = None, subSubCapability: str = None) -> any:
        capabilities = self.arlo_capabilities.get("Capabilities", {})

        if subCapability:
            capabilities = capabilities.get(subCapability, {})
        if subSubCapability:
            capabilities = capabilities.get(subSubCapability, {})

        # Check if 'capability' exists in the dictionary
        return capability in capabilities
    
    def get_property(self, property: str, subProperty: str = None) -> any:
        if subProperty:
            return self.arlo_properties.get(subProperty, {}).get(property, None)
        return self.arlo_properties.get(property, None)
    
    def has_property(self, property: str, subProperty: str = None) -> bool:
        if subProperty:
            return property in self.arlo_properties.get(subProperty, {})
        return property in self.arlo_properties