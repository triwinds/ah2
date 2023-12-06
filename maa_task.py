from Arknights.addons.contrib.maa import *
from common_config import common_config


if __name__ == '__main__':
    asst = init_maa()
    maa_infrast(asst)
    maa_recruit(asst)
    maa_mall(asst)
    maa_award(asst)
    asst.start()
    wait_maa_task_finish()
