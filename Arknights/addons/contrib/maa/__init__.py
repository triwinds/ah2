import json
import pathlib
from util.msg_sender import send_by_tg_bot
from typing import Dict, List
import time
from Arknights.addons.contrib.maa.asst.asst import Asst
from Arknights.addons.contrib.maa.asst.utils import Message, Version, InstanceOptionType
from Arknights.addons.contrib.maa.asst.updater import Updater
from Arknights.addons.contrib.maa.asst.emulator import Bluestacks
import logging


logger = logging.getLogger(__name__)
_asst: Asst | None = None
_callback_backward_map: Dict[str, List[Dict]] = {}


@Asst.CallBackType
def my_callback(msg, details, arg):
    m = Message(msg)
    d = json.loads(details.decode('utf-8'))
    # print(m, d, arg)
    if m == Message.TaskChainStart:
        logger.info(f'task {d.get("taskchain")} started.')
    elif m == Message.TaskChainCompleted:
        logger.info(f'task {d.get("taskchain")} finished.')
    print(m, d)
    handle_maa_callback_detail(m, d)


def _update_backward(detail: Dict):
    li = _callback_backward_map.get(detail.get('uuid'), [])
    li.append(detail)
    if len(li) > 5:
        li.pop(0)


def handle_maa_callback_detail(msg: Message, detail: Dict):
    _update_backward(detail)
    if detail.get('what') == 'RecruitResult':
        details = detail['details']
        if details['level'] == 6:
            tags_choose = details['result'][0]['tags']
            send_by_tg_bot('公招出 6 星了!', f'选择标签: {tags_choose}')


def init_maa():
    global _asst
    if _asst:
        return _asst
    import os
    if os.name == 'nt':
        path = pathlib.Path(r'D:\software\MeoAssistantArknights')
    else:
        path = pathlib.Path(r'/root/redroid/maa')
    os.environ['http_proxy'] = 'http://127.0.0.1:7890'
    os.environ['https_proxy'] = 'http://127.0.0.1:7890'
    Updater(path, Version.Beta).update()
    Asst.load(path=path)
    # port = Bluestacks.get_hyperv_port(r"C:\Program Files\BlueStacks_nxt\bluestacks.conf", "Pie64")
    port = 5555

    # 若需要获取详细执行信息，请传入 callback 参数
    # 例如 asst = Asst(callback=my_callback)
    _asst = Asst(callback=my_callback)
    _asst.set_instance_option(InstanceOptionType.touch_type, 'maatouch')
    print(port)
    if _asst.connect('adb', f'127.0.0.1:{port}'):
        print('连接成功')
    else:
        print('连接失败')
        raise RuntimeError('maa 模拟器连接失败')
    return _asst


def maa_infrast(asst: Asst):
    logger.info('add maa infrast task...')
    # 开发文档
    # https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/docs/3.1-%E9%9B%86%E6%88%90%E6%96%87%E6%A1%A3.md
    asst.append_task('Infrast', {
        'facility': [
            "Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm"
        ],
        # "_NotUse"、"Money"、"SyntheticJade"、"CombatRecord"、"PureGold"、"OriginStone"、"Chip"
        'drones': "SyntheticJade",
        "replenish": True
    })


def maa_award(asst: Asst):
    logger.info('add maa award task...')
    asst.append_task('Award')


def maa_mall(asst: Asst):
    logger.info('add maa Mall task...')
    asst.append_task('Mall', {
        "shopping": True,
        "buy_first": ["招聘许可", "龙门币"],
        "blacklist": ["加急许可", "家具零件"],
    })


def maa_recruit(asst: Asst):
    logger.info('add maa recruit task...')
    asst.append_task('Recruit', {
        "refresh": True,
        "select": [5, 4, 1],
        "confirm": [5, 4, 3, 1],
        "times": 4,
    })


def wait_maa_task_finish(timeout_seconds=1200):
    global _asst
    st = time.time()
    if not _asst:
        raise RuntimeError('maa instance not started.')
    if timeout_seconds > 0:
        while _asst.running() and time.time() - st < timeout_seconds:
            time.sleep(0.5)
    else:
        while _asst.running():
            time.sleep(0.5)
    _asst.stop()


def maa_rouge_like(theme):
    logger.info('starting maa rouge like task...')
    asst = init_maa()
    asst.append_task('Roguelike', {
        "theme": theme
    })
    asst.start()
    # wait_maa_task_finish(-1)


def shutdown_maa():
    global _asst
    if _asst:
        _asst.stop()
        logger.info('shutdown maa...')
        del _asst
        _asst = None


if __name__ == '__main__':
    import time
    maa_rouge_like('Sami')
    time.sleep(60)
    shutdown_maa()
