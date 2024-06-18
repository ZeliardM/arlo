import asyncio

import scrypted_arlo_go

HEARTBEAT_INTERVAL = 5

async def sync_logger_send(logger, message):
    # Convert the synchronous logger send function to an async one
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, logger.Send, message)

async def multiprocess_main(name, logger_port, parent_to_child_queue, child_to_parent_queue, exe, args, parent_instance, queue=None, binary_output=False):
    logger = scrypted_arlo_go.NewTCPLogger(logger_port, "HeartbeatChildProcess")
    await sync_logger_send(logger, f"{name} starting\n")

    try:
        process = await asyncio.create_subprocess_exec(
            exe, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        parent_instance.process = process
    except Exception as e:
        await sync_logger_send(logger, f"Failed to start subprocess: {e}")
        return

    async def logging_thread(stdstream, is_stderr):
        while True:
            line = await stdstream.readline()
            if not line:
                break
            if binary_output and not is_stderr:
                await queue.put(line)
            else:
                line = line.decode('utf-8')
                await sync_logger_send(logger, line)

    await asyncio.gather(
        logging_thread(process.stdout, False),
        logging_thread(process.stderr, True))

    while True:
        try:
            keep_alive = await asyncio.wait_for(parent_to_child_queue.get(), timeout=HEARTBEAT_INTERVAL * 3)
        except asyncio.TimeoutError:
            break

        if process.returncode is not None:
            break

        if not keep_alive:
            break

    await sync_logger_send(logger, f"{name} exiting\n")

    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    await sync_logger_send(logger, f"{name} exited\n")

class HeartbeatChildProcess:
    """Class to manage running a child process that gets cleaned up if the parent exits."""

    def __init__(self, name, logger_port, exe, binary_output=False, *args):
        self.name = name
        self.logger_port = logger_port
        self.exe = exe
        self.args = args
        self.binary_output = binary_output
        self.output = []

        self.loop = asyncio.get_event_loop()
        self.parent_to_child_queue = asyncio.Queue()
        self.child_to_parent_queue = asyncio.Queue()
        self.queue = asyncio.Queue() if binary_output else None
        self.process_task = self.loop.create_task(multiprocess_main(name, logger_port, self.parent_to_child_queue, self.child_to_parent_queue, exe, args, self, self.queue, binary_output))

        self._stop = False
        self.process = None

    async def start(self):
        if self.process_task.done():
            # Restart the process if it has already completed
            self.process_task = self.loop.create_task(multiprocess_main(self.name, self.logger_port, self.parent_to_child_queue, self.child_to_parent_queue, self.exe, self.args, self, self.queue, self.binary_output))

    async def stop(self):
        self._stop = True
        await self.parent_to_child_queue.put(False)
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.process_task and not self.process_task.done():
            self.process_task.cancel()
            try:
                await self.process_task
            except asyncio.CancelledError:
                pass
        while not self.parent_to_child_queue.empty():
            await self.parent_to_child_queue.get()
        if self.binary_output:
            while not self.queue.empty():
                await self.queue.get()

    async def heartbeat(self):
        while not self._stop:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self.process_task.done():
                await self.stop()
                break
            await self.parent_to_child_queue.put(True)

    async def buffer(self):
        if self.binary_output:
            try:
                while True:
                    buf = await asyncio.wait_for(self.queue.get(), timeout=5)
                    self.output.append(buf)
            except asyncio.TimeoutError:
                pass
            return b''.join(self.output)