import json
import pathlib
import time

from Arknights.addons.contrib.maa.asst.asst import Asst
from Arknights.addons.contrib.maa.asst.utils import Message, Version, InstanceOptionType
from Arknights.addons.contrib.maa.asst.updater import Updater
from Arknights.addons.contrib.maa.asst.emulator import Bluestacks
import logging


logger = logging.getLogger(__name__)
asst: Asst | None = None


@Asst.CallBackType
def my_callback(msg, details, arg):
    m = Message(msg)
    d = json.loads(details.decode('utf-8'))

    print(m, d, arg)


path = pathlib.Path(r'D:\software\MeoAssistantArknights')


def init_maa():
    global asst
    if asst:
        return asst
    Updater(path, Version.Beta).update()
    Asst.load(path=path)
    port = Bluestacks.get_hyperv_port(r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf", "Nougat64")

    # 若需要获取详细执行信息，请传入 callback 参数
    # 例如 asst = Asst(callback=my_callback)
    asst = Asst()
    if asst.connect('adb.exe', f'127.0.0.1:{port}'):
        print('连接成功')
    else:
        print('连接失败')
        raise RuntimeError('maa 模拟器连接失败')
    return asst


def maa_infrast(timeout_seconds=1200, shutdown_maa_after_finish=True):
    logger.info('starting maa infrast task...')
    asst = init_maa()
    # 开发文档
    # https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/docs/3.1-%E9%9B%86%E6%88%90%E6%96%87%E6%A1%A3.md
    asst.append_task('Infrast', {
        'facility': [
            "Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm"
        ],
        # "_NotUse"、"Money"、"SyntheticJade"、"CombatRecord"、"PureGold"、"OriginStone"、"Chip"
        'drones': "Money",
        "replenish": True
    })
    asst.start()
    wait_maa_task_finish(timeout_seconds)
    if shutdown_maa_after_finish:
        shutdown_maa()


def wait_maa_task_finish(timeout_seconds):
    global asst
    st = time.time()
    if not asst:
        raise RuntimeError('maa instance not started.')
    if timeout_seconds > 0:
        while asst.running() and time.time() - st < timeout_seconds:
            time.sleep(0.5)
    else:
        while asst.running():
            time.sleep(0.5)
    asst.stop()


def maa_rouge_like(theme):
    logger.info('starting maa rouge like task...')
    asst = init_maa()
    asst.append_task('Roguelike', {
        "theme": theme
    })
    asst.start()
    # wait_maa_task_finish(-1)


def shutdown_maa():
    global asst
    if asst:
        asst.stop()
        logger.info('shutdown maa...')
        del asst
        asst = None


if __name__ == '__main__':
    import time
    maa_rouge_like('Sami')
    time.sleep(60)
    shutdown_maa()
    maa_infrast()
