import asyncio
from typing import Optional

from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import (
    AsyncServiceBrowser,
    AsyncServiceInfo,
    AsyncZeroconf,
)

class AsyncListener:
    def __init__(self) -> None:
        super().__init__()
        self.services = {}
    
    def async_on_service_state_change(
        self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        asyncio.ensure_future(self.async_write_service_info(zeroconf, service_type, name))

    async def async_write_service_info(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        await info.async_request(zeroconf, 3000)
        
        if info:
            addresses = [addr for addr in info.parsed_scoped_addresses()]
            item = {
                'name': info.name,
                'type': info.type,
                'server': info.server[:-1],
                'address': addresses[0],
                'port': info.port,
                'deviceId': info.properties[b'deviceid'].decode("utf-8")
            }
            self.services.update({item['deviceId']:item})

class AsyncBrowser:
    def __init__(self) -> None:
        self.aiobrowser: Optional[AsyncServiceBrowser] = None
        self.aiozc: Optional[AsyncZeroconf] = None
        self.aiolistener: Optional[AsyncListener] = None
        self.services = {}

    async def async_run(self) -> None:
        self.aiozc = AsyncZeroconf()
        self.aiolistener = AsyncListener()
        services = ["_arlo-video._tcp.local."]
        self.aiobrowser = AsyncServiceBrowser(
            self.aiozc.zeroconf, services, handlers=[self.aiolistener.async_on_service_state_change]
        )
        await asyncio.sleep(1)
        self.services = self.aiolistener.services
        await self.async_close()

    async def async_close(self) -> None:
        assert self.aiozc is not None
        assert self.aiobrowser is not None
        await self.aiobrowser.async_cancel()
        await self.aiozc.async_close()