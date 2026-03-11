import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import app
from Arknights.addons.contrib.auto_credit_store import AutoCreditStoreAddOn
from Arknights.addons.contrib.only720.auto_clues import AutoClueAddOn
from Arknights.addons.contrib.only720.auto_recruit import AutoRecruitAddOn
from Arknights.addons.contrib.only720.auto_shift import AutoShiftAddOn
from Arknights.addons.contrib.common_cache import check_game_data_version
from Arknights.addons.quest import QuestAddon
from Arknights.addons.record import RecordAddon
from Arknights.configure_launcher import get_helper
from automator import BaseAutomator
from imgreco.itemdb import update_net
from Arknights.addons.contrib.emulator_manager import start_and_login_arknights

logger = logging.getLogger(__file__)
task_cache_path = app.cache_path.joinpath('common_task_cache.json')


def load_cache():
    task_cache = {
        'time': datetime.now().astimezone(tz=timezone(timedelta(hours=4))).strftime('%Y-%m-%d'),
        'get_credit': False,
        'auto_recruit': False,
        'auto_clue_time': 0
    }
    if os.path.exists(task_cache_path):
        with open(task_cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if data['time'] == task_cache['time']:
                task_cache = data
    return task_cache


def save_cache(task_cache):
    with open(task_cache_path, 'w', encoding='utf-8') as f:
        json.dump(task_cache, f, ensure_ascii=False)


def old_infrast_task(helper):
    logger.info('===基建收菜')
    helper.addon(RecordAddon).try_replay_record('get_building')
    logger.info('===清空无人机')
    helper.addon(AutoShiftAddOn).clear_drones('b302')
    logger.info('===基建换班')
    retry_count = 0
    while True:
        try:
            shift_addon = helper.addon(AutoShiftAddOn)
            shift_addon.run()
            break
        except Exception as e:
            retry_count += 1
            if retry_count > 3:
                raise e


def do_maa_tasks(queue, log_queue=None):
    from maa_task import do_maa_tasks
    do_maa_tasks(queue, log_queue)


def start_maa_process(helper: BaseAutomator):
    from multiprocessing import Process, Queue
    import logging.handlers
    
    retry_count = 0
    while retry_count < 3:
        queue = Queue()
        log_queue = Queue()
        
        # Setup log listener to forward logs from child process to main process logger
        root_logger = logging.getLogger()
        listener = logging.handlers.QueueListener(log_queue, *root_logger.handlers)
        listener.start()
        
        try:
            proc = Process(target=do_maa_tasks, args=(queue, log_queue))
            proc.start()
            proc.join(timeout=3600)
            
            if proc.is_alive():
                logger.warning('MAA任务超时，强制终止进程')
                proc.terminate()  # 先尝试正常终止
                proc.join(timeout=5)  # 等待5秒

                if proc.is_alive():  # 如果仍然存活
                    proc.kill()  # 强制杀死进程
                    proc.join()

            # 尝试获取结果（带超时保护）
            maa_result = None
            try:
                maa_result = queue.get(block=False)  # 非阻塞获取
            except Exception as e:
                retry_count += 1
                maa_result = f'maa 获取结果失败: {str(e)}'
                logger.warning(f'获取结果失败: {str(e)}')
            
            if isinstance(maa_result, str) and 'Error' in maa_result:
                retry_count += 1
                from util.adb_utils import check_game_is_in_front
                if not check_game_is_in_front(helper):
                    logger.info('Game is not in front, restart game...')
                    success, message = start_and_login_arknights(helper)
                    if not success:
                        logger.error(f'Failed to restart game: {message}')
                else:
                    logger.info('Game is in front, run maa startup...')
                    from Arknights.addons.contrib.maa.maa_cli import maa_startup
                    maa_startup()
                continue

            # 清理残留资源
            if proc.exitcode is None:
                proc.close()
            return maa_result
            
        finally:
            listener.stop()


def start_maa_direct(helper: BaseAutomator):
    """
    直接调用 MAA 任务，不使用 multiprocessing.Process
    这样可以避免进程通信导致的卡顿问题
    """
    from Arknights.addons.contrib.maa.maa_cli import init_maa_cli, run_all_tasks
    
    retry_count = 0
    while retry_count < 3:
        try:
            # 确保 MAA CLI 已初始化
            init_maa_cli()
            
            # 直接运行 MAA 任务
            logger.info('开始执行 MAA 任务...')
            summary = run_all_tasks()
            
            # 返回成功结果
            maa_result = {'ok': True, 'summary': summary}
            logger.info(f'MAA 任务完成: {summary}')
            return maa_result
            
        except Exception as e:
            retry_count += 1
            error_msg = str(e)
            logger.error(f'MAA 任务执行失败 (尝试 {retry_count}/3): {error_msg}')
            
            # 检查是否需要重启游戏
            from util.adb_utils import check_game_is_in_front
            if not check_game_is_in_front(helper):
                logger.info('检测到游戏未在前台，尝试重启游戏...')
                success, message = start_and_login_arknights(helper)
                if not success:
                    logger.error(f'重启游戏失败: {message}')
            else:
                logger.info('游戏在前台，尝试运行 MAA startup...')
                from Arknights.addons.contrib.maa.maa_cli import maa_startup
                try:
                    maa_startup()
                except Exception as startup_error:
                    logger.error(f'MAA startup 失败: {startup_error}')
            
            if retry_count >= 3:
                # 达到最大重试次数，返回错误
                return {'ok': False, 'error': error_msg}
    
    return {'ok': False, 'error': '未知错误'}


def main():
    print('do common task.')
    helper = get_helper()
    task_cache = load_cache()

    # 公招
    # helper.addon(AutoRecruitAddOn).hire_all()
    # if not task_cache['auto_recruit']:
    #     AutoRecruitAddOn(helper).auto_recruit(4)
    #     task_cache['auto_recruit'] = True
    # else:
    #     AutoRecruitAddOn(helper).clear_refresh()

    # helper.addon(QuestAddon).clear_task()
    # auto_clue_time = task_cache.get('auto_clue_time', 0)
    # if auto_clue_time + 3 * 3600 < time.time():
    #     logger.info('===收取并应用线索')
    #     helper.addon(AutoClueAddOn).run()
    #     task_cache['auto_clue_time'] = int(time.time())

    # old_infrast_task(helper)
    from Arknights.addons.common import CommonAddon
    helper.addon(CommonAddon).back_to_main()
    maa_result = start_maa_direct(helper)  # 使用直接调用方法，避免 Process 卡顿
    logger.info('maa tasks done, result: {}'.format(maa_result))

    # if datetime.now().hour > 20 or datetime.now().hour < 4:
    #     logger.info('===收取并使用信用点')
    #     helper.addon(AutoCreditStoreAddOn).run()
    #     task_cache['get_credit'] = True

    save_cache(task_cache)
    return {'maa_result': maa_result}


if __name__ == '__main__':
    main()
