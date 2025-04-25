import subprocess
import time
from imgreco.imgops import match_template
from PIL import Image
import os
import logging
from Arknights.addons.common import CommonAddon
from util import cvimage
from util.richlog import get_logger
from Arknights.configure_launcher import get_helper


file_root = os.path.realpath(os.path.dirname(__file__)) + '/'
start_img = Image.open(file_root + 'start.png').convert('L')
login_img = Image.open(file_root + 'login.png').convert('L')
start_stuck_img = Image.open(file_root + 'start_stuck.png').convert('L')
rich_logger = get_logger('emulator_manager')
logger = logging.getLogger(__name__)


def start_and_login_arknights(helper):
    helper.control.adb.shell('am start -n com.hypergryph.arknights/com.u8.sdk.U8UnityContext')
    time.sleep(35)
    retry_click_img(start_img, 'start')
    time.sleep(5)
    retry_click_img(login_img, 'login')
    time.sleep(30)
    if os.name != 'nt':
        time.sleep(30)


def screenshot():
    helper = get_helper()
    addon = helper.addon(CommonAddon)
    return addon.screenshot()


def click_window_img(pil_gray_img):
    helper = get_helper()
    addon = helper.addon(CommonAddon)
    st = time.time()
    screen = addon.screenshot()
    logger.info(f'screenshot time: {time.time() - st:.2f}s')
    factor = 720 / screen.size[1]
    if factor != 1:
        screen = screen.resize((int(factor * screen.width), int(factor * screen.height)))
    gray_screen = screen.convert('L')
    (x, y), p = match_template(gray_screen, pil_gray_img)
    if factor != 1:
        x = int(x / factor)
        y = int(y / factor)
    logger.info(f'adb click_window_img: {(x, y), p}')
    if p > 0.9:
        # click_window_pos(bluestacks_window, (x, y))
        addon.tap_point((x, y))
        return True


def check_is_stuck():
    screen = screenshot()
    gray_screen = screen.convert('L')
    (x, y), p = match_template(gray_screen, start_stuck_img)
    logger.info(f'adb check_is_stuck: {(x, y), p}')
    return p > 0.9


def retry_click_img(img, img_name):
    c = 0
    max_retry = 6
    network_retry_count = 0
    linux_stuck_count = 0
    logger.info(f'try to click [{img_name}].')
    while not click_window_img(img):
        time.sleep(20)
        c += 1
        if c > max_retry:
            screen = screenshot()
            rich_logger.logimage(screen)
            import imgreco.common
            dlgtype, ocrresult = imgreco.common.recognize_dialog(img)
            rich_logger.logtext(f'fail img_name: {img_name}, dlgtype: {dlgtype}, dialog ocr result: {ocrresult}')
            if dlgtype is None:
                raise RuntimeError(f'Fail to click [{img_name}].')
            else:
                if dlgtype == 'ok' and '获取网络配置失败' in ocrresult:
                    network_retry_count += 1
                    if network_retry_count < 6:
                        helper = get_helper()
                        addon = helper.addon(CommonAddon)
                        addon.tap_rect(imgreco.common.get_dialog_ok_button_rect(img))
                    else:
                        raise RuntimeError(f'Fail to click [{img_name}], dialog ocr result: {ocrresult}.')
                else:
                    raise RuntimeError(f'Fail to click [{img_name}], dialog ocr result: {ocrresult}.')
        else:
            logger.info(f'retry click [{img_name}]...')
            import imgreco.common
            dlgtype, ocrresult = imgreco.common.recognize_dialog(img)
            if dlgtype is not None:
                raise RuntimeError(f'Fail to click [{img_name}], dialog ocr result: {ocrresult}.')
            if img_name == 'start' and check_is_stuck():
                linux_stuck_count += 1
                if linux_stuck_count < 2:
                    logger.info('linux stuck, retry...')
                else:
                    raise RuntimeError(f'Stuck in [正在获取更新...] page.')


def start_bluestacks():
    subprocess.Popen(r'"C:\Program Files\BlueStacks_nxt\HD-Player.exe" --instance Nougat64')
    time.sleep(60)


def check_port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', 5555)) == 0


def check_emulator_is_alive():
    if os.name == 'nt':
        return check_bluestacks_is_alive()
    # return check_redroid_is_alive()
    from util.adb_utils import check_game_is_in_front
    return check_port_in_use(5555) and check_game_is_in_front(get_helper())


def check_redroid_is_alive():
    output = subprocess.run(['docker', 'ps', '-a', '--filter', 'name=redroid', '--format', '{{.Status}}'], capture_output=True)
    status = output.stdout.decode('utf-8').strip()
    return status.startswith('Up')


def check_bluestacks_is_alive():
    output = subprocess.run(['tasklist'], capture_output=True)
    # print(output.stdout.decode('gbk'))
    return 'HD-Player' in output.stdout.decode('gbk')


def close_bluestacks():
    if not check_bluestacks_is_alive():
        logger.info('bluestacks is not running.')
        return
    logger.info('stopping bluestacks...')
    ret = subprocess.run(['powershell', '$a = Get-Process HD-Player;', '$a.kill();'], capture_output=True)
    if ret.returncode != 0:
        logger.error('stdout:', ret.stdout.decode('gbk'))
        logger.error('stderr:', ret.stderr.decode('gbk'))
    else:
        logger.info('bluestacks stopped.')


def close_redroid():
    path = os.getcwd()
    os.chdir('/root/redroid/')
    logger.info('closing redroid...')
    os.system('docker-compose down')
    os.chdir(path)


def start_redroid():
    path = os.getcwd()
    os.chdir('/root/redroid/')
    logger.info('starting redroid...')
    os.system('docker-compose up -d')
    time.sleep(30)
    os.chdir(path)


def unlock_phone():
    os.system('adb kill-server')
    logger.info('unlocking phone...')
    helper = get_helper()
    helper.control.adb.shell('input keyevent 26')
    time.sleep(1)
    helper.control.adb.shell('input touchscreen swipe 930 880 930 280')


def close_arknights_and_lock_phone():
    logger.info('locking phone...')
    helper = get_helper()
    helper.control.adb.shell('am force-stop com.hypergryph.arknights')
    helper.control.adb.shell('input keyevent 26')
    time.sleep(1)


def close_emulator():
    if os.name == 'nt':
        close_bluestacks()
    else:
        # close_redroid()
        close_arknights_and_lock_phone()


def restart_all():
    from Arknights.configure_launcher import reconnect_helper, get_helper
    if os.name == 'nt':
        close_bluestacks()
        start_bluestacks()
    else:
        # close_redroid()
        # start_redroid()
        if not check_port_in_use(5555):
            raise RuntimeError('phone\'s adb is not connected.')
        unlock_phone()
        helper = get_helper()
        helper.control.adb.shell('am force-stop com.hypergryph.arknights')
    retry_count = 1 if os.name == 'nt' else 5
    while retry_count > 0:
        try:
            reconnect_helper()
            helper = get_helper()
            start_and_login_arknights(helper)
            break
        except RuntimeError as e:
            logger.error(e)
            logger.info('Closing arknights...')
            helper = get_helper()
            helper.control.adb.shell('am force-stop com.hypergryph.arknights')
            retry_count -= 1


if __name__ == '__main__':
    restart_all()
