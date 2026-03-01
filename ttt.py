import logging
logging.basicConfig(level=logging.DEBUG)

from Arknights.addons.contrib.maa.maa_cli import download_maa_cli


if __name__ == '__main__':
    download_maa_cli()