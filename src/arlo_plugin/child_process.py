import asyncio
import multiprocessing

import scrypted_arlo_go


HEARTBEAT_INTERVAL = 5


async def async_logging_thread(stdstream, logger, is_stderr, buffer_queue=None, binary_output=False):
    while True:
        try:
            line = await stdstream.readline()
            if not line:
                break
            if binary_output and not is_stderr:
                await buffer_queue.put(line)
            else:
                line = line.decode('utf-8')
                await asyncio.get_event_loop().run_in_executor(None, logger.Send, line)
        except Exception as e:
            logger.error(f"Error in logging thread: {e}")

async def multiprocess_main(name, logger, child_conn, exe, args, buffer_queue=None, binary_output=False):
    await asyncio.get_event_loop().run_in_executor(None, logger.Send, f"{name} starting\n")
    try:
        proc = await asyncio.create_subprocess_exec(exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        await asyncio.get_event_loop().run_in_executor(None, logger.Send, f"Failed to start {name}: {e}\n")
        return

    stdout_t = asyncio.create_task(async_logging_thread(proc.stdout, logger, False, buffer_queue, binary_output))
    stderr_t = asyncio.create_task(async_logging_thread(proc.stderr, logger, True))

    while True:
        has_data = await asyncio.get_event_loop().run_in_executor(None, child_conn.poll, HEARTBEAT_INTERVAL * 3)
        if not has_data:
            break

        if proc.returncode is not None:
            break

        keep_alive = await asyncio.get_event_loop().run_in_executor(None, child_conn.recv)
        if not keep_alive:
            break

    await asyncio.get_event_loop().run_in_executor(None, logger.Send, f"{name} exiting\n")

    if proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception as e:
                logger.error(f"Error killing {name}: {e}")
        except Exception as e:
            logger.error(f"Error terminating {name}: {e}")

    stdout_t.cancel()
    stderr_t.cancel()

    try:
        await stdout_t
    except asyncio.CancelledError:
        pass

    try:
        await stderr_t
    except asyncio.CancelledError:
        pass

    await asyncio.get_event_loop().run_in_executor(None, logger.Send, f"{name} exited\n")
    await asyncio.get_event_loop().run_in_executor(None, logger.Close)


class HeartbeatChildProcess:
    """Class to manage running a child process that gets cleaned up if the parent exits.

    When spawning subprocesses in Python, if the parent is forcibly killed (as is the case
    when Scrypted restarts plugins), subprocesses get orphaned. This approach uses parent-child
    heartbeats for the child to ensure that the parent process is still alive, and to cleanly
    exit the child if the parent has terminated.
    """

    def __init__(self, name, logger_port, exe, binary_output=False, *args):
        self.name = name
        self.logger = scrypted_arlo_go.NewTCPLogger(logger_port, "HeartbeatChildProcess")
        self.exe = exe
        self.args = args

        self.parent_conn, self.child_conn = multiprocessing.Pipe()
        self.buffer_queue = asyncio.Queue() if binary_output else None
        self.binary_output = binary_output
        self.buffer_output = []
        self._stop = False

    async def start(self):
        self.process_task = asyncio.create_task(multiprocess_main(self.name, self.logger, self.child_conn, self.exe, self.args, self.buffer_queue, self.binary_output))
        self.heartbeat_task = asyncio.create_task(self.heartbeat())

    async def stop(self):
        self._stop = True
        await asyncio.get_event_loop().run_in_executor(None, self.parent_conn.send, False)

    async def heartbeat(self):
        while not self._stop:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self.process_task.done():
                await self.stop()
                break
            await asyncio.get_event_loop().run_in_executor(None, self.parent_conn.send, True)

    async def buffer(self):
        if self.binary_output:
            await self.start()
            temp_buffer = b''
            found_start = False

            while True:
                try:
                    binary_data = await asyncio.wait_for(self.buffer_queue.get(), timeout=1)
                    temp_buffer += binary_data

                    if not found_start:
                        start = temp_buffer.find(b'\xFF\xD8')
                        if start != -1:
                            found_start = True
                            temp_buffer = temp_buffer[start:]
                    else:
                        end = temp_buffer.find(b'\xFF\xD9')
                        if end != -1:
                            end += 2
                            frame = temp_buffer[:end]
                            self.buffer_output.append(frame)
                            break
                except asyncio.TimeoutError:
                    continue

        await self.stop()
        return b''.join(self.buffer_output)