from Arknights.addons.contrib.maa import *
from multiprocessing import Queue
from sys import platform


def do_maa_tasks(q: Queue = None):
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
    from Arknights.addons.contrib.maa.linux_cli import init_maa_cli, run_all_tasks
    init_maa_cli()
    summary = run_all_tasks()
    q.put({'ok': True, 'summary': summary})


if __name__ == '__main__':
    do_maa_tasks()
