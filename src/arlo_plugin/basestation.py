from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from typing import List, TYPE_CHECKING

from scrypted_sdk import ScryptedDeviceBase
from scrypted_sdk.types import Device, DeviceProvider, Setting, SettingValue, Settings, ScryptedInterface, ScryptedDeviceType

from .arlo.mdns import AsyncBrowser
from .base import ArloDeviceBase
from .vss import ArloSirenVirtualSecuritySystem

if TYPE_CHECKING:
    # https://adamj.eu/tech/2021/05/13/python-type-hints-how-to-fix-circular-imports/
    from .provider import ArloProvider


class ArloBasestation(ArloDeviceBase, DeviceProvider, Settings):

    vss: ArloSirenVirtualSecuritySystem = None
    reboot_time: datetime = datetime(1970, 1, 1)

    def __init__(self, nativeId: str, arlo_basestation: dict, arlo_properties: dict, provider: ArloProvider) -> None:
        super().__init__(nativeId=nativeId, arlo_device=arlo_basestation, arlo_basestation=arlo_basestation, arlo_properties=arlo_properties, provider=provider)

    async def delayed_init(self) -> None:
        self.state_change_timer = None
        self.start_state_subscription()
        self.start_ping_subscription()

        self.logger.debug("Checking if Certificates are created with Arlo")
        cert_registered = self.peer_cert
        if cert_registered:
            self.logger.debug("Certificates have been created with Arlo, skipping Certificate Creation")
        else:
            self.logger.debug("Certificates have not been created with Arlo, proceeding with Certificate Creation")

        if self.has_local_live_streaming and not cert_registered:
            self.createCertificates()

        if self.has_local_live_streaming:
            await self.mdns()

    def createCertificates(self) -> None:
        certificates = self.provider.arlo.CreateCertificate(self.arlo_basestation, "".join(self.provider.arlo_public_key[27:-25].splitlines()))
        if certificates:
            self.logger.debug("Certificates have been created with Arlo, parsing certificates")
            self.parseCertificates(certificates)
        else:
            self.logger.error("Failed to create Certificates with Arlo")

    def parseCertificates(self, certificates: dict) -> None:
        peerCert = certificates['certsData'][0]['peerCert']
        deviceCert = certificates['certsData'][0]['deviceCert']
        icaCert = certificates['icaCert']
        if peerCert and deviceCert and icaCert:
            self.logger.debug("Certificates have been parsed, storing certificates")
            self.storeCertificates(peerCert, deviceCert, icaCert)
        else:
            if not peerCert:
                self.logger.error("Failed to parse peer Certificate")
            if not deviceCert:
                self.logger.error("Failed to parse device Certificate")
            if not icaCert:
                self.logger.error("Failed to parse ICA Certificate")

    def storeCertificates(self, peerCert: str, deviceCert: str, icaCert: str) -> None:
        peerCert = f'-----BEGIN CERTIFICATE-----\n{chr(10).join([peerCert[idx:idx+64] for idx in range(len(peerCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----'
        deviceCert = f'-----BEGIN CERTIFICATE-----\n{chr(10).join([deviceCert[idx:idx+64] for idx in range(len(deviceCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----'
        icaCert = f'-----BEGIN CERTIFICATE-----\n{chr(10).join([icaCert[idx:idx+64] for idx in range(len(icaCert)) if idx % 64 == 0])}\n-----END CERTIFICATE-----'
        self.storage.setItem("peer_cert", peerCert)
        self.storage.setItem("device_cert", deviceCert)
        self.storage.setItem("ica_cert", icaCert)
        self.logger.debug("Certificates have been stored with Scrypted")

    def start_state_subscription(self) -> None:
        def callback(state):
            if self.arlo_properties.get('state', '') != state.get('state', ''):
                self.logger.debug(f"State is {state['state']}")
                self.arlo_properties['state'] = state['state']
            return self.stop_subscriptions

        self.register_task(
            self.provider.arlo.SubscribeToDeviceStateEvents(self.arlo_device, callback)
        )

    def start_ping_subscription(self) -> None:
        async def callback(state):
            if self.reboot_time:
                if datetime.now() - self.reboot_time < timedelta(seconds=30):
                    self.logger.debug("Reboot time is within the last 30 seconds, not resetting state to idle.")
                    return self.stop_subscriptions

            if state and self.arlo_properties.get('state', '') != 'idle':
                self.logger.debug(f"State is idle")
                await self.provider.mdns()
                if self.has_local_live_streaming:
                    await self.mdns()
                self.arlo_properties = await asyncio.wait_for(self.provider.arlo.TriggerProperties(self.arlo_device), timeout=10)
            return self.stop_subscriptions

        self.register_task(
            self.provider.arlo.SubscribeToDevicePingEvents(self.arlo_device, callback)
        )

    def start_state_change_check(self) -> None:
        task = asyncio.get_event_loop().create_task(self.state_change_check())
        self.register_task(task)

    async def state_change_check(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                state = self.arlo_properties.get('state', '')

                if not state or state != 'idle':
                    if state == 'rebooting':
                        raise ValueError("Device is rebooting, skipping TriggerProperties for state.")
                    else:
                        self.logger.debug(f"State is still {state} after 30 seconds, getting properties from Arlo.")
                        self.arlo_properties = await asyncio.wait_for(self.provider.arlo.TriggerProperties(self.arlo_device), timeout=10)
            except Exception as e:
                self.logger.error(f"Failed to get properties from Arlo: {e}")
                self.logger.debug(f"Retrying...")

    @property
    def has_siren(self) -> bool:
        return self.has_capability("Siren", "ResourceTypes")

    @property
    def has_local_live_streaming(self) -> bool:
        return self.has_capability("supported", "sipLiveStream") and self.provider.arlo_user_id == self.arlo_device["owner"]["ownerId"]

    @property
    def can_restart(self) -> bool:
        return self.provider.arlo_user_id == self.arlo_device["owner"]["ownerId"]

    @property
    def ip_addr(self) -> str:
        return self.storage.getItem("ip_addr")

    @property
    def hostname(self) -> str:
        return self.storage.getItem("hostname")

    @property
    def mdns_boolean(self) -> bool:
        return self.storage.getItem("mdns_boolean")

    async def mdns(self) -> None:
        self.storage.setItem("mdns_boolean", False)
        self.storage.setItem("ip_addr", "")
        self.storage.setItem("hostname", "")
        self.logger.debug("Checking if Basestation is found in mDNS.")
        self.storage.setItem("mdns_boolean", bool(self.provider.mdns_services.get(self.arlo_device['deviceId'])))
        if self.mdns_boolean == True:
            self.logger.debug("Basestation found in mDNS, setting IP Address and Hostname.")
            self.storage.setItem("ip_addr", self.provider.mdns_services[self.arlo_device['deviceId']].get('address'))
            self.storage.setItem("hostname", self.provider.mdns_services[self.arlo_device['deviceId']].get('server'))
        else:
            self.logger.error("Basestation not found in mDNS, manual input needed under basestation settings.")

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
                    "model": f"{self.arlo_device['modelId']} {self.arlo_properties.get('hwVersion', '').replace(self.arlo_device['modelId'], '').strip()}".strip(),
                    "manufacturer": "Arlo",
                    "serialNumber": self.arlo_device["deviceId"],
                    "firmware": self.arlo_properties.get("swVersion"),
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
            self.vss = ArloSirenVirtualSecuritySystem(vss_id, self.arlo_device, self.arlo_basestation, self.arlo_properties, self.provider, self)
        return self.vss

    async def getSettings(self) -> List[Setting]:
        result = []
        if self.has_local_live_streaming:
            result.append(
                {
                    "group": "General",
                    "key": "ip_addr",
                    "title": "IP Address",
                    "description": "This is the IP Address of the basestation pulled from mDNS. " + \
                                   "This will be used for cameras that support local streaming. " + \
                                   "Note that the basestation must be in the same network as Scrypted for this to work. " + \
                                   "If this is empty, it means mDNS failed to find your basestation, " + \
                                   "You will have to input the IP Address into this field manually.",
                    "value": self.ip_addr,
                    "readonly": self.mdns_boolean,
                },
            )
            result.append(
                {
                    "group": "General",
                    "key": "hostname",
                    "title": "Hostname",
                    "description": "This is the Hostname of the basestation pulled from mDNS. " + \
                                   "This will be used for cameras that support local streaming. " + \
                                   "Note that the basestation must be in the same network as Scrypted for this to work."
                                   "If this is empty, it means mDNS failed to find your basestation, " + \
                                   "You will have to input the Hostname into this field manually, " + \
                                   "Usually the model including the domain, i.e. ""VMB5000.local"" or ""VMB5000-2.local"" " + \
                                   "if you have mutliple of the same model basestation.",
                    "value": self.hostname,
                    "readonly": self.mdns_boolean,
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
        if self.can_restart:
            result.append(
                {
                    "group": "General",
                    "key": "restart_device",
                    "title": "Restart Device",
                    "description": "Restarts the Device.",
                    "type": "button",
                },
            )
        return result

    async def putSetting(self, key: str, value: SettingValue) -> None:
        if key == "print_debug":
            self.logger.info(f"Device Capabilities: {json.dumps(self.arlo_capabilities)}")
            self.logger.info(f"Smart Features: {json.dumps(self.arlo_smartFeatures)}")
            self.logger.info(f"Basestation Properties: {self.arlo_properties}")
            self.logger.debug(f'Peer Certificate:\n{self.peer_cert}')
            self.logger.debug(f'Device Certificate:\n{self.device_cert}')
            self.logger.debug(f'ICA Certificate:\n{self.ica_cert}')
            # self.logger.debug(f'Public Key:\n{self.provider.arlo_public_key}')
            # self.logger.debug(f'Private Key:\n{self.provider.arlo_private_key}')
        elif key == "restart_device":
            self.logger.info("Restarting Device")
            self.reboot_time = datetime.now()
            self.provider.arlo.RestartDevice(self.arlo_device["deviceId"])
        elif key in ["ip_addr", "hostname"]:
            self.storage.setItem(key, value)
        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)