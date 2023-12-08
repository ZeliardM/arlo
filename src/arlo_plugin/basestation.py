from __future__ import annotations

import os

from typing import List, TYPE_CHECKING

from scrypted_sdk import ScryptedDeviceBase
from scrypted_sdk.types import Device, DeviceProvider, Setting, SettingValue, Settings, ScryptedInterface, ScryptedDeviceType

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

    FILE_STORAGE = os.path.join(os.environ['SCRYPTED_PLUGIN_VOLUME'], 'zip', 'unzipped', 'fs')

    vss: ArloSirenVirtualSecuritySystem = None

    def __init__(self, nativeId: str, arlo_basestation: dict, provider: ArloProvider) -> None:
        super().__init__(nativeId=nativeId, arlo_device=arlo_basestation, arlo_basestation=arlo_basestation, provider=provider)

        if self.has_local_live_streaming and not any(filename.endswith('.cer') for filename in os.listdir(ArloBasestation.FILE_STORAGE)):
            self.createCertificates()

    def createCertificates(self) -> None:
        certificates = self.provider.arlo.CreateCertificate(self.arlo_basestation, "".join(self.provider.arlo_public_key[27:-25].splitlines()))
        self.parseCertificates(certificates)

    def parseCertificates(self, certificates: dict) -> None:
        peerCert = certificates['certsData'][0]['peerCert']
        deviceCert = certificates['certsData'][0]['deviceCert']
        icaCert = certificates['icaCert']
        self.storeCertificates(peerCert, deviceCert, icaCert)

    def storeCertificates(self, peerCert: str, deviceCert: str, icaCert: str) -> None:
        peer = open(f'{ArloBasestation.FILE_STORAGE}/{self.provider._arlo.user_id}_{self.arlo_device["deviceId"]}.crt', "x")
        peer.write(f'-----BEGIN CERTIFICATE-----\n{chr(10).join([peerCert[idx:idx+64] for idx in range(len(peerCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----')
        peer.close()
        device = open(f'{ArloBasestation.FILE_STORAGE}/{self.arlo_device["deviceId"]}.crt', "x")
        device.write(f'-----BEGIN CERTIFICATE-----\n{chr(10).join([deviceCert[idx:idx+64] for idx in range(len(deviceCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----')
        device.close()
        ica = open(f'{ArloBasestation.FILE_STORAGE}/ica.crt', "x")
        ica.write(f'-----BEGIN CERTIFICATE-----\n{chr(10).join([icaCert[idx:idx+64] for idx in range(len(icaCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----')
        ica.close()

    @property
    def has_siren(self) -> bool:
        return any([self.arlo_device["modelId"].lower().startswith(model) for model in ArloBasestation.MODELS_WITH_SIRENS])

    @property
    def has_local_live_streaming(self) -> bool:
        return self.provider.arlo.GetDeviceCapabilities(self.arlo_device).get("Capabilities", {}).get("sipLiveStream", False)

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
        return [
            {
                "group": "General",
                "key": "print_debug",
                "title": "Debug Info",
                "description": "Prints information about this device to console.",
                "type": "button",
            }
        ]

    async def putSetting(self, key: str, value: SettingValue) -> None:
        if key == "print_debug":
            self.logger.info(f"Device Capabilities: {self.arlo_capabilities}")
        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)
