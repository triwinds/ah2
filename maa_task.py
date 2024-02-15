from Arknights.addons.contrib.maa import *
from common_config import common_config
from multiprocessing import Queue


def do_maa_tasks(q: Queue = None):
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


if __name__ == '__main__':
    do_maa_tasks()
