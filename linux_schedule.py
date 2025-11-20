import re
from typing import Optional

import requests
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone, timedelta
from util.msg_sender import send_by_tg_bot
import requests
from apscheduler.schedulers.blocking import BlockingScheduler

import app
import common_task
from Arknights.addons.contrib.only720.auto_chips import AutoChips
from Arknights.addons.contrib.only720.auto_jiaomie import AutoJiaomieAddOn
from Arknights.addons.stage_navigator import StageNavigator
from Arknights.configure_launcher import reconnect_helper, get_helper
from automator import BaseAutomator
from imgreco.itemdb import update_net
from Arknights.addons.contrib.maa import maa_rouge_like, shutdown_maa
from Arknights.addons.contrib.emulator_manager import restart_all, check_emulator_is_alive, close_emulator
from common_config import common_config
from web_admin import WebAdmin

logger = logging.getLogger(__file__)
helper: BaseAutomator = None
grab_red_ticket = False


def download_latest_apk():
    url = 'https://ak.hypergryph.com/downloads/android_lastest'


def do_jiaomie():
    if os.path.exists('common_task_cache.json'):
        with open('common_task_cache.json', 'r') as f:
            task_info = json.load(f)
            task_info.get('time')


def clear_sanity():
    now = datetime.now().astimezone(tz=timezone(timedelta(hours=4)))
    wd = now.weekday()
    logger.info(f'clear_sanity, weekday: {wd}, time: {now}')
    # items_day = {0, 2, 3, 4, 5, 6}
    # items_day = {2, 4}
    red_ticket_day = {0, 3, 5, 6}
    # Monday = 0, Sunday = 6
    if wd in red_ticket_day and grab_red_ticket:
        helper.addon(AutoChips).run()
        clear_sanity_by_red_ticket()
        clear_sanity_by_item(True)
    elif wd == 1:
        logger.info('clear_sanity_by_jiaomie')
        if not helper.addon(AutoJiaomieAddOn).run():
            # 剿灭刷完就刷材料
            clear_sanity_by_item()
    else:
        clear_sanity_by_item()


def clear_sanity_by_red_ticket():
    logger.info('clear_sanity_by_red_ticket')
    helper.addon(StageNavigator).navigate_and_combat('AP-5', 1000)


def clear_sanity_by_item(only_activity=False):
    logger.info('clear_sanity_by_item')
    # helper.addon(StageNavigator).navigate_and_combat('latest', 1000)
    # helper.addon(StageNavigator).navigate_and_combat('HE-7', 1000)
    # helper.addon(StageNavigator).navigate_and_combat('1-7', 1000)

    if sanity_mode == 'grass':
        from Arknights.addons.contrib.grass_on_aog import GrassAddOn
        if not helper.addon(GrassAddOn).run():
            helper.addon(AutoChips).run()
            helper.addon(StageNavigator).navigate_and_combat('1-7', 1000)
    elif sanity_mode == '1-7':
        helper.addon(AutoChips).run()
        helper.addon(StageNavigator).navigate_and_combat(sanity_mode, 1000)
    else:
        helper.addon(StageNavigator).navigate_and_combat(sanity_mode, 1000)


def escape_markdown(
    text: str, version=1, entity_type: Optional[str] = None
) -> str:
    if int(version) == 1:
        escape_chars = r"_*`["
    elif int(version) == 2:
        if entity_type in ["pre", "code"]:
            escape_chars = r"\`"
        elif entity_type in ["text_link", "custom_emoji"]:
            escape_chars = r"\)"
        else:
            escape_chars = r"\_*[]()~`>#+-=|{}.!"
    else:
        raise ValueError("Markdown version must be either 1 or 2!")

    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)


def send_summary(loots, stage_count, common_task_result):
    body = f'Task finished at: {escape_markdown(str(datetime.now()))}\n\n```log\n'
    if stage_count:
        body += '[Stages]\n'
        for stage in stage_count:
            body += f'{stage}: {stage_count[stage]}\n'
        body += '\n'
    if loots:
        body += '[Loots]\n'
        for loot in loots:
            body += f'{loot}: {loots[loot]}\n'
        body += '\n'
    body += '[Common tasks]\n'
    for task, result in common_task_result.items():
        if task == 'maa_result' and 'summary' in result:
            body += f'{task}:\n{result["summary"]}\n'
        else:
            body += f'{task}: {result}\n'
    body += '```'
    send_by_tg_bot('ah2', body)


def do_works():
    global helper
    shutdown_maa()
    update_cache()

    # 重启 adb server, 以免产生奇怪的 bug
    try:
        os.system('adb kill-server')
        if not check_emulator_is_alive():
            restart_all()
        reconnect_helper()
        helper = get_helper()
        helper.set_extra_delay(3)
        update_net()
        logger.info(f'run schedule at {datetime.now()}')
        clear_sanity()
        from Arknights.addons.combat import CombatAddon
        loots = helper.addon(CombatAddon).loots
        stage_count = helper.addon(CombatAddon).stage_count
        common_task_result = common_task.main()
        logger.info(f'finish at: {datetime.now()}')
        send_summary(loots, stage_count, common_task_result)
        time.sleep(60)
        from Arknights.addons.common import CommonAddon
        if common_config.rouge_like:
            helper.addon(CommonAddon).back_to_main()
            maa_rouge_like('Sami')
        else:
            try:
                helper.addon(CommonAddon).exit_game()
            finally:
                close_emulator()
    except Exception as e:
        from util.msg_sender import send_by_tg_bot
        send_by_tg_bot('arh-fail', traceback.format_exc())
        print(traceback.format_exc())
        close_emulator()


def recruit():
    from Arknights.addons.contrib.only720.auto_recruit import AutoRecruitAddOn
    addon = helper.addon(AutoRecruitAddOn)
    addon.hire_all()
    addon.auto_recruit(4)


def update_cache():
    update_net()
    from Arknights.addons.contrib.common_cache import check_game_data_version
    check_game_data_version()


def get_current_helper():
    """Helper function to get the current helper instance"""
    global helper
    return helper


def main():
    os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7890'
    os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7890'
    do_works()
    scheduler = BlockingScheduler(timezone='Asia/Shanghai')
    # scheduler.add_job(recruit, 'cron', day_of_week='0,1,2', hour='19', minute=0)
    scheduler.add_job(close_emulator, 'cron', day='*', hour=4, minute=5, id='close_emulator')
    scheduler.add_job(do_works, 'cron', hour='*/4', minute=15, id='do_works')
    
    # Start web admin interface
    web_admin = WebAdmin(scheduler, get_current_helper, port=8888)
    web_admin.start()
    logger.info('Web admin started at http://localhost:8888')
    
    scheduler.start()


if __name__ == '__main__':
    sanity_mode = input('sanity mode[grass/<stage_code>] default as grass: ')
    if not sanity_mode:
        sanity_mode = common_config.sanity_mode
    elif sanity_mode == '1':
        sanity_mode = '1-7'
    elif sanity_mode == 'g':
        sanity_mode = 'grass'
    elif sanity_mode == 'a':
        grab_red_ticket = True
        sanity_mode = '1-7'
    elif sanity_mode == 'l':
        sanity_mode = 'latest'
    main()
    # print(is_in_event())
