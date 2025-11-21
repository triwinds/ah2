from Arknights.addons.contrib.maa import *
from multiprocessing import Queue
from sys import platform


import logging
import logging.handlers
import sys

class StreamToLogger(object):
    """
    Fake file-like stream object that redirects writes to a logger instance.
    """
    def __init__(self, logger, log_level=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self.linebuf = ''

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            self.logger.log(self.log_level, line.rstrip())

    def flush(self):
        pass

def setup_process_logging(log_queue):
    if log_queue is not None:
        qh = logging.handlers.QueueHandler(log_queue)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(qh)
        
        # Redirect stdout and stderr to logger
        sys.stdout = StreamToLogger(logging.getLogger('MAA'), logging.INFO)
        sys.stderr = StreamToLogger(logging.getLogger('MAA'), logging.ERROR)

def do_maa_tasks(q: Queue = None, log_queue: Queue = None):
    setup_process_logging(log_queue)
    if platform == 'linux':
        maa_cli_tasks(q)
    else:
        maa_python_tasks(q)


def maa_python_tasks(q: Queue = None):
    try:
        asst = init_maa()
        maa_infrast(asst)
        maa_recruit(asst)
        maa_mall(asst)
        maa_award(asst)
        asst.start()
        wait_maa_task_finish()
        q.put({'ok': True})
    except Exception as e:
        if q is not None:
            q.put({'ok': False, 'error': str(e)})
        else:
            raise e


def maa_cli_tasks(q: Queue = None):
    from Arknights.addons.contrib.maa.maa_cli import init_maa_cli, run_all_tasks
    init_maa_cli()
    summary = run_all_tasks()
    q.put({'ok': False, 'summary': summary})


if __name__ == '__main__':
    do_maa_tasks()
