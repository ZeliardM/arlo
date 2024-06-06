import multiprocessing
import subprocess
import time
import threading

import scrypted_arlo_go


HEARTBEAT_INTERVAL = 5


def multiprocess_main(name, logger_port, child_conn, exe, args, queue=None, binary_output=False):
    logger = scrypted_arlo_go.NewTCPLogger(logger_port, "HeartbeatChildProcess")

    logger.Send(f"{name} starting\n")
    sp = subprocess.Popen([exe, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # pull stdout and stderr from the subprocess and forward it over to
    # our tcp logger
    def logging_thread(stdstream, is_stderr):
        while True:
            line = stdstream.readline()
            if not line:
                break
            if binary_output and not is_stderr:
                queue.put(line)  # push the binary output to the queue
            else:
                line = str(line, 'utf-8')
                logger.Send(line)
    stdout_t = threading.Thread(target=logging_thread, args=(sp.stdout, False))
    stderr_t = threading.Thread(target=logging_thread, args=(sp.stderr, True))
    stdout_t.start()
    stderr_t.start()

    while True:
        has_data = child_conn.poll(HEARTBEAT_INTERVAL * 3)
        if not has_data:
            break

        # check if the subprocess is still alive, if not then exit
        if sp.poll() is not None:
            break

        keep_alive = child_conn.recv()
        if not keep_alive:
            break

    logger.Send(f"{name} exiting\n")

    sp.terminate()
    sp.wait()

    stdout_t.join()
    stderr_t.join()

    logger.Send(f"{name} exited\n")
    logger.Close()


class HeartbeatChildProcess:
    """Class to manage running a child process that gets cleaned up if the parent exits.

    When spawining subprocesses in Python, if the parent is forcibly killed (as is the case
    when Scrypted restarts plugins), subprocesses get orphaned. This approach uses parent-child
    heartbeats for the child to ensure that the parent process is still alive, and to cleanly
    exit the child if the parent has terminated.
    """

    def __init__(self, name, logger_port, exe, binary_output=False, *args):
        self.name = name
        self.logger_port = logger_port
        self.exe = exe
        self.args = args

        self.parent_conn, self.child_conn = multiprocessing.Pipe()
        if binary_output:
            self.queue = multiprocessing.Queue()
            self.binary_output = binary_output
            self.output = []
            self.process = multiprocessing.Process(target=multiprocess_main, args=(name, logger_port, self.child_conn, exe, args, self.queue, binary_output))
        else:
            self.process = multiprocessing.Process(target=multiprocess_main, args=(name, logger_port, self.child_conn, exe, args))
        self.process.daemon = True
        self._stop = False

        self.thread = threading.Thread(target=self.heartbeat)

    def start(self):
        self.process.start()
        self.thread.start()

    def stop(self):
        self._stop = True
        self.parent_conn.send(False)

    def heartbeat(self):
        while not self._stop:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self.process.is_alive():
                self.stop()
                break
            self.parent_conn.send(True)

    def buffer(self):
        if self.binary_output:
            while not self.queue.empty():  # retrieve the output from the queue
                self.output.append(self.queue.get())
            return b''.join(self.output)