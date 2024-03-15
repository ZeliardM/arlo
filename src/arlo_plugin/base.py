from __future__ import annotations

import asyncio
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

    def __init__(self, nativeId: str, arlo_device: dict, arlo_basestation: dict, provider: ArloProvider) -> None:
        super().__init__(nativeId=nativeId)

        self.logger_name = nativeId

        self.nativeId = nativeId
        self.arlo_device = arlo_device
        self.arlo_basestation = arlo_basestation
        self.provider = provider
        self.logger.setLevel(self.provider.get_current_log_level())

        try:
            self.arlo_capabilities = {} if nativeId.endswith("smss") else self.provider.arlo.GetDeviceCapabilities(self.arlo_device)
            self.arlo_properties = self.provider.arlo.TriggerProperties(self.arlo_basestation, self.arlo_device if self.arlo_device["deviceType"] != "basestation" else None)
            self.arlo_smartFeatures = self.provider.arlo.GetSmartFeatures(self.arlo_device)
        except Exception as e:
            self.logger.warning(f"Could not load device capabilities: {e}")
            self.arlo_capabilities = {}

        self.properties_loaded = asyncio.Event()
        self.create_task(self.load_device_properties())
        self.create_task(self._do_delayed_init())

    async def load_device_properties(self) -> None:
        try:
            self.logger.debug(f"Loading device properties for {self.nativeId}")
            self.arlo_properties = await self.provider.arlo.TriggerProperties(self.arlo_basestation, self.arlo_device if self.arlo_device["deviceType"] != "basestation" else None)
            self.logger.debug(f"Loaded device properties for {self.nativeId}")
            self.properties_loaded.set()  # Signal that properties have been loaded
        except Exception as e:
            self.logger.warning(f"Could not load device properties: {e}")

    async def _do_delayed_init(self) -> None:
        await self.properties_loaded.wait()
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

    async def get_device_manifest(self) -> Device:
        """Returns the Scrypted device manifest representing this device."""
        await self.properties_loaded.wait()
        parent = None
        if self.arlo_device.get("parentId") and self.arlo_device["parentId"] != self.arlo_device["deviceId"]:
            parent = self.arlo_device["parentId"]

        if parent in self.provider.hidden_device_ids:
            parent = None

        return {
            "info": {
                "model": f"{self.arlo_device['modelId']}",
                "manufacturer": "Arlo",
                "firmware": self.arlo_device["firmwareVersion"],
                "serialNumber": self.arlo_device["deviceId"],
                "version": self.arlo_properties["hwVersion"],
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

    def get_feature(self, feature: str) -> bool:
        smartfeatures = self.arlo_smartFeatures.get("planFeatures", {})
        return smartfeatures.get(feature, False)
    
    def has_capability(self, capability: str, subCapability: str = None, subSubCapability: str = None) -> bool:
        capabilities = self.arlo_capabilities.get("Capabilities", {})
        if subCapability:
            if subSubCapability:
                return capability in capabilities.get(subCapability, {}).get(subSubCapability, {})
            return capability in capabilities.get(subCapability, {})
        return capability in capabilities
    
    def get_capability(self, capability: str, subCapability: str, subSubCapability: str = None) -> any:
        capabilities = self.arlo_capabilities.get("Capabilities", {})
        if subSubCapability:
            return capabilities.get(subCapability, {}).get(subSubCapability, {}).get(capability, False)
        return capabilities.get(subCapability, {}).get(capability, False)
    
    def get_property(self, property: str, subProperty: str, subSubProperty: str = None) -> any:
        if subSubProperty:
            return self.arlo_properties.get(subProperty, {}).get(subSubProperty, {}).get(property, None)
        return self.arlo_properties.get(subProperty, {}).get(property, None)