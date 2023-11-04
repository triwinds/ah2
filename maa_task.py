from Arknights.addons.contrib.maa import maa_infrast
from common_config import common_config


if __name__ == '__main__':
    maa_infrast(shutdown_maa_after_finish=not common_config.rouge_like)
