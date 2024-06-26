import asyncio
import multiprocessing
import queue
import subprocess
import threading

import scrypted_arlo_go


HEARTBEAT_INTERVAL = 5


def multiprocess_main(name, logger_port, child_conn, exe, args, buffer_queue=None, binary_output=False):
    logger = scrypted_arlo_go.NewTCPLogger(logger_port, "HeartbeatChildProcess")

    logger.Send(f"{name} starting\n")
    sp = subprocess.Popen([exe, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stop_event = threading.Event()

    def logging_thread(stdstream, is_stderr):
        while not stop_event.is_set():
            line = stdstream.readline()
            if not line:
                break
            if binary_output and not is_stderr:
                buffer_queue.put(line, block=True, timeout=HEARTBEAT_INTERVAL * 3)
            else:
                line = str(line, 'utf-8')
                logger.Send(line)

    stdout_t = threading.Thread(target=logging_thread, args=(sp.stdout, False))
    stderr_t = threading.Thread(target=logging_thread, args=(sp.stderr, True))

    stdout_t.start()
    stderr_t.start()

    try:
        while True:
            has_data = child_conn.poll(HEARTBEAT_INTERVAL * 3)
            if not has_data:
                break
            if sp.poll() is not None:
                break
            keep_alive = child_conn.recv()
            if not keep_alive:
                break
    finally:
        logger.Send(f"{name} exiting\n")
        sp.terminate()
        try:
            sp.wait(timeout=HEARTBEAT_INTERVAL)
        except subprocess.TimeoutExpired:
            sp.kill()
        stop_event.set()

        stdout_t.join()
        stderr_t.join()

        logger.Send(f"{name} exited\n")
        logger.Close()


class HeartbeatChildProcess:
    """Class to manage running a child process that gets cleaned up if the parent exits.

    When spawning subprocesses in Python, if the parent is forcibly killed (as is the case
    when Scrypted restarts plugins), subprocesses get orphaned. This approach uses parent-child
    heartbeats for the child to ensure that the parent process is still alive, and to cleanly
    exit the child if the parent has terminated.
    """

    def __init__(self, name, logger_port, exe, binary_output=False, *args):
        self.name = name
        self.logger_port = logger_port
        self.exe = exe
        self.binary_output = binary_output
        self.args = args

        self.parent_conn, self.child_conn = multiprocessing.Pipe()
        self.buffer_queue = multiprocessing.Queue() if self.binary_output else None
        
        if self.binary_output:
            self.buffer_output = []

        self.process = multiprocessing.Process(target=multiprocess_main, args=(self.name, self.logger_port, self.child_conn, self.exe, self.args, self.buffer_queue, self.binary_output))
        self.process.daemon = True
        self._stop = False

    async def start(self):
        self.process.start()
        self.heartbeat_task = asyncio.create_task(self.heartbeat())

    async def stop(self):
        self._stop = True
        if self.process.is_alive():
            self.parent_conn.send(False)
            self.process.join()
        self.parent_conn.close()
        self.child_conn.close()
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.binary_output:
            self.buffer_queue.close()
            self.buffer_queue.join_thread()

    async def heartbeat(self):
        try:
            while not self._stop:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self.process.is_alive():
                    await self.stop()
                    break
                self.parent_conn.send(True)
        except asyncio.CancelledError:
            pass

    async def buffer(self):
        if self.binary_output:
            await self.start()
            temp_buffer = b''
            found_start = False

            while True:
                try:
                    binary_data = self.buffer_queue.get_nowait()
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
                except queue.Empty:
                    try:
                        await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        continue
                    continue

        return b''.join(self.buffer_output)