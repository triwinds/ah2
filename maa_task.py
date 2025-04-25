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
    from Arknights.addons.contrib.maa.maa_cli import init_maa_cli, run_all_tasks
    init_maa_cli()
    retry_count = 3
    while retry_count > 0:
        summary = run_all_tasks()
        if 'Error' in summary:
            retry_count -= 1
            from util.adb_utils import check_game_is_in_front
            from Arknights.configure_launcher import get_helper
            helper = get_helper()
            if not check_game_is_in_front(get_helper()):
                logger.info('Game is not in front, restart game...')
                from Arknights.addons.contrib.emulator_manager import start_and_login_arknights
                start_and_login_arknights(helper)
            continue
        if retry_count != 3:
            summary += f'\nretry times:{3-retry_count}'
        q.put({'ok': True, 'summary': summary})
        break


if __name__ == '__main__':
    do_maa_tasks()
