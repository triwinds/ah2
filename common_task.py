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
from imgreco.itemdb import update_net

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


def do_maa_tasks(queue):
    from maa_task import do_maa_tasks
    do_maa_tasks(queue)


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
    from multiprocessing import Process, Queue
    queue = Queue()
    proc = Process(target=do_maa_tasks, args=(queue,))
    proc.start()
    proc.join(timeout=3600)
    maa_result = queue.get()
    logger.info('maa tasks done, result: {}'.format(maa_result))

    # if datetime.now().hour > 20 or datetime.now().hour < 4:
    #     logger.info('===收取并使用信用点')
    #     helper.addon(AutoCreditStoreAddOn).run()
    #     task_cache['get_credit'] = True

    save_cache(task_cache)
    return {'maa_result': maa_result}


if __name__ == '__main__':
    main()
