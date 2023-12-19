from __future__ import annotations

import asyncio

from typing import List, TYPE_CHECKING
import json

from scrypted_sdk import ScryptedDeviceBase
from scrypted_sdk.types import Device, DeviceProvider, Setting, SettingValue, Settings, ScryptedInterface, ScryptedDeviceType

from .arlo.mdns import AsyncBrowser
from .base import ArloDeviceBase
from .vss import ArloSirenVirtualSecuritySystem

if TYPE_CHECKING:
    # https://adamj.eu/tech/2021/05/13/python-type-hints-how-to-fix-circular-imports/
    from .provider import ArloProvider


class ArloBasestation(ArloDeviceBase, DeviceProvider, Settings):
    MODELS_WITH_SIRENS = [
        "vmb4000",
        "vmb4500"
    ]

    vss: ArloSirenVirtualSecuritySystem = None

    def __init__(self, nativeId: str, arlo_basestation: dict, provider: ArloProvider) -> None:
        super().__init__(nativeId=nativeId, arlo_device=arlo_basestation, arlo_basestation=arlo_basestation, provider=provider)
        self.create_task(self.delayed_init())

    async def delayed_init(self) -> None:
        iterations = 1
        while True:
            if iterations > 100:
                self.logger.error("Delayed init exceeded iteration limit, giving up")
                return

            try:
                cert_registered = self.peer_cert
                break
            except Exception as e:
                self.logger.debug(f"Delayed init failed, will try again: {e}")
                await asyncio.sleep(0.1)
            iterations += 1

        if self.has_local_live_streaming and not cert_registered:
            self.createCertificates()

        await self.mdns()

    def createCertificates(self) -> None:
        certificates = self.provider.arlo.CreateCertificate(self.arlo_basestation, "".join(self.provider.arlo_public_key[27:-25].splitlines()))
        self.parseCertificates(certificates)

    def parseCertificates(self, certificates: dict) -> None:
        peerCert = certificates['certsData'][0]['peerCert']
        deviceCert = certificates['certsData'][0]['deviceCert']
        icaCert = certificates['icaCert']
        self.storeCertificates(peerCert, deviceCert, icaCert)

    def storeCertificates(self, peerCert: str, deviceCert: str, icaCert: str) -> None:
        self.storage.setItem("peer_cert", f'-----BEGIN CERTIFICATE-----\n{chr(10).join([peerCert[idx:idx+64] for idx in range(len(peerCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----')
        self.storage.setItem("device_cert", f'-----BEGIN CERTIFICATE-----\n{chr(10).join([deviceCert[idx:idx+64] for idx in range(len(deviceCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----')
        self.storage.setItem("ica_cert", f'-----BEGIN CERTIFICATE-----\n{chr(10).join([icaCert[idx:idx+64] for idx in range(len(icaCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----')

    @property
    def has_siren(self) -> bool:
        return any([self.arlo_device["modelId"].lower().startswith(model) for model in ArloBasestation.MODELS_WITH_SIRENS])

    @property
    def has_local_live_streaming(self) -> bool:
        return self.provider.arlo.GetDeviceCapabilities(self.arlo_device).get("Capabilities", {}).get("sipLiveStream", False) and self.provider.arlo_user_id == self.arlo_device["owner"]["ownerId"]

    @property
    def ip_addr(self) -> str:
        return self.storage.getItem("ip_addr")

    async def mdns(self) -> None:
        mdns = AsyncBrowser()
        await mdns.async_run()
        self.storage.setItem("ip_addr", mdns.services.get(self.arlo_device['deviceId']))

    @property
    def peer_cert(self) -> str:
        return self.storage.getItem("peer_cert")

    @property
    def device_cert(self) -> str:
        return self.storage.getItem("device_cert")

    @property
    def ica_cert(self) -> str:
        return self.storage.getItem("ica_cert")

    def get_applicable_interfaces(self) -> List[str]:
        return [
            ScryptedInterface.DeviceProvider.value,
            ScryptedInterface.Settings.value,
        ]

    def get_device_type(self) -> str:
        return ScryptedDeviceType.DeviceProvider.value

    def get_builtin_child_device_manifests(self) -> List[Device]:
        if not self.has_siren:
            # this basestation has no builtin siren, so no manifests to return
            return []

        vss = self.get_or_create_vss()
        return [
            {
                "info": {
                    "model": f"{self.arlo_device['modelId']} {self.arlo_device['properties'].get('hwVersion', '')}".strip(),
                    "manufacturer": "Arlo",
                    "firmware": self.arlo_device.get("firmwareVersion"),
                    "serialNumber": self.arlo_device["deviceId"],
                },
                "nativeId": vss.nativeId,
                "name": f'{self.arlo_device["deviceName"]} Siren Virtual Security System',
                "interfaces": vss.get_applicable_interfaces(),
                "type": vss.get_device_type(),
                "providerNativeId": self.nativeId,
            },
        ] + vss.get_builtin_child_device_manifests()

    async def getDevice(self, nativeId: str) -> ScryptedDeviceBase:
        if not nativeId.startswith(self.nativeId):
            # must be a camera, so get it from the provider
            return await self.provider.getDevice(nativeId)
        if not nativeId.endswith("vss"):
            return None
        return self.get_or_create_vss()

    def get_or_create_vss(self) -> ArloSirenVirtualSecuritySystem:
        vss_id = f'{self.arlo_device["deviceId"]}.vss'
        if not self.vss:
            self.vss = ArloSirenVirtualSecuritySystem(vss_id, self.arlo_device, self.arlo_basestation, self.provider, self)
        return self.vss

    async def getSettings(self) -> List[Setting]:
        result = []
        if self.has_local_live_streaming:
            result.append(
                {
                    "group": "General",
                    "key": "ip_addr",
                    "title": "IP Address",
                    "description": "Add this to tell Scrypted the local IP address of the basestation. " + \
                                   "This will be used for cameras that support local streaming. " + \
                                   "Note that the basestation must be in the same network as Scrypted for this to work.",
                    "value": self.ip_addr,
                    "readonly": True,
                },
            )
        result.append(
            {
                "group": "General",
                "key": "print_debug",
                "title": "Debug Info",
                "description": "Prints information about this device to console.",
                "type": "button",
            },
        )
        return result

    async def putSetting(self, key: str, value: SettingValue) -> None:
        if key == "print_debug":
            self.logger.info(f"Device Capabilities: {json.dumps(self.arlo_capabilities)}")
        elif key in ["ip_addr"]:
            self.storage.setItem(key, value)
        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)