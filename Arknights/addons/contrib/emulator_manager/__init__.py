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
from typing import Tuple, Optional
from util.cvimage import Image as CVImage


file_root = os.path.realpath(os.path.dirname(__file__)) + '/'
start_img = Image.open(file_root + 'start.png').convert('L')
login_img = Image.open(file_root + 'login.png').convert('L')
start_stuck_img = Image.open(file_root + 'start_stuck.png').convert('L')
rich_logger = get_logger('emulator_manager')
logger = logging.getLogger(__name__)


def start_and_login_arknights_adb(helper):
    helper.control.adb.shell('am start -n com.hypergryph.arknights/com.u8.sdk.U8UnityContext')
    time.sleep(50)
    retry_click_img(start_img, 'start')
    time.sleep(30)
    retry_click_img(login_img, 'login')
    time.sleep(50)
    if os.name != 'nt':
        time.sleep(30)


def screenshot() -> CVImage:
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
    try:
        from util.adb_utils import check_game_is_in_front
        return check_port_in_use(5555) and check_game_is_in_front(get_helper())
    except Exception as e:
        logger.error(e)
        return False


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

    # Check if redroid container exists and is running
    logger.info('checking if redroid container is running...')
    check_process = subprocess.run(['docker', 'compose', 'ps', '-q'], capture_output=True, text=True)
    if check_process.returncode != 0 or not check_process.stdout.strip():
        logger.info('redroid container is not running, skipping shutdown process')
        os.chdir(path)
        return

    logger.info('redroid container is running, proceeding with graceful shutdown')

    # Close Arknights game via ADB before stopping container
    logger.info('closing Arknights game via ADB...')
    try:
        result = subprocess.run(['adb', '-s', '127.0.0.1:5555', 'shell', 'am force-stop com.hypergryph.arknights'], capture_output=True, text=True)
        if result.returncode == 0:
            logger.info('successfully closed Arknights game')
        else:
            logger.warning(f'failed to close Arknights game via ADB: {result.stderr}')
    except Exception as e:
        logger.warning(f'failed to close Arknights game via ADB: {e}')

    # Stop the container instead of down to preserve data
    logger.info('stopping redroid container...')
    try:
        process = subprocess.run(['docker', 'compose', 'stop'], capture_output=True, text=True, timeout=30)
        output = process.stdout + process.stderr
        if process.returncode != 0:
            logger.error(f"docker compose stop failed with error: {output}")
        else:
            logger.info(f"docker compose stop completed successfully: {output}")
    except subprocess.TimeoutExpired:
        logger.error('docker compose stop timed out')
    finally:
        os.chdir(path)


def start_redroid():
    path = os.getcwd()
    os.chdir('/root/redroid/')
    logger.info('starting redroid...')

    # Check if container is already running
    logger.info('checking if redroid container is already running...')
    check_process = subprocess.run(['docker', 'compose', 'ps', '-q'], capture_output=True, text=True)
    if check_process.returncode == 0 and check_process.stdout.strip():
        logger.info('redroid container is already running, skipping startup')
        os.chdir(path)
        return

    logger.info('starting redroid container with docker compose...')
    try:
        process = subprocess.run(['docker', 'compose', 'up', '-d'], capture_output=True, text=True, timeout=60)
        output = process.stdout + process.stderr
        if process.returncode != 0:
            logger.error(f"docker compose up failed with error: {output}")
            raise RuntimeError(f"Failed to start redroid container: {output}")
        else:
            logger.info(f"docker compose up completed successfully: {output}")
    except subprocess.TimeoutExpired:
        logger.error('docker compose up timed out')
        raise RuntimeError('docker compose up timed out')

    logger.info('waiting for container to fully initialize...')
    time.sleep(30)
    os.chdir(path)

    # logger.info('stopping logd...')
    # helper = get_helper()
    # helper.control.adb.shell('setprop persist.logd.enable 0')
    # # needs root shell
    # helper.control.adb.shell('stop logd')


def unlock_phone():
    if not check_port_in_use(5555):
        raise RuntimeError('phone\'s adb is not connected.')
    os.system('adb kill-server')
    logger.info('unlocking phone...')
    helper = get_helper()
    helper.control.adb.shell('input keyevent 26')
    time.sleep(1)
    helper.control.adb.shell('input touchscreen swipe 930 880 930 280')
    helper = get_helper()
    helper.control.adb.shell('am force-stop com.hypergryph.arknights')


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
        close_redroid()
        # close_arknights_and_lock_phone()


def restart_all():
    from Arknights.configure_launcher import reconnect_helper, get_helper
    if os.name == 'nt':
        close_bluestacks()
        start_bluestacks()
    else:
        close_redroid()
        time.sleep(5)
        start_redroid()
        # unlock_phone()
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


def start_and_login_arknights_maa(helper=None) -> Tuple[bool, str]:
    """
    Simple startup function using MAA CLI

    Args:
        helper: Arknights helper instance (optional)

    Returns:
        Tuple[bool, str]: (success, message)
    """
    try:
        from Arknights.addons.contrib.maa.maa_cli import maa_startup
        if helper is None:
            helper = get_helper()
        maa_startup()
        return True, "MAA startup completed successfully"
    except Exception as e:
        logger.error(f"MAA startup failed: {str(e)}")
        # Fallback to original method
        if helper is None:
            helper = get_helper()
        start_and_login_arknights(helper)
        return True, "Fallback to original startup method completed"


def check_and_click_cache_repair() -> bool:
    """
    Check screen top-left for '清除缓存' and then '资源修复', click if found

    Returns:
        bool: True if both clicks were successful, False otherwise
    """
    from imgreco.ppocr_utils import detect_box, get_ppocr
    from util.cvimage import Image as CVImage

    # First screenshot to check for '清除缓存'
    screen = screenshot()
    # Crop to top-left portion of the screen (e.g., first 1/3 width and height)
    width, height = screen.size
    top_left_region = screen.crop((0, 0, width // 3, height // 3))

    # Convert to CVImage for detect_box
    pos, score = detect_box(top_left_region, '清除缓存')

    if pos is not None and score > 0.5:
        logger.info(f'Found "清除缓存" at {pos} with score {score:.3f}, clicking...')
        helper = get_helper()
        addon = helper.addon(CommonAddon)
        # Click at the detected position (relative to the cropped image)
        addon.tap_point(pos)

        # Wait 2 seconds after clicking
        time.sleep(2)

        # Second screenshot to check for '资源修复'
        screen = screenshot()
        pos2, score2 = detect_box(screen, '资源修复')

        if pos2 is not None and score2 > 0.5:
            logger.info(f'Found "资源修复" at {pos2} with score {score2:.3f}, clicking...')
            # Click at the detected position for resource repair
            addon.tap_point(pos2)
            time.sleep(2)
            import imgreco.common
            screen = screenshot()
            addon.tap_rect(imgreco.common.get_dialog_right_button_rect(screen))
            logger.info('等待修复中...')
            time.sleep(20)
            st = time.time()
            max_wait_time = 300
            fixed_flag = False
            while time.time() - st < max_wait_time:
                logger.info('等待修复中...')
                screen = screenshot()
                res = get_ppocr().detect_and_ocr(screen.array)
                flag = False
                for ocr_res in res:
                    if ocr_res.ocr_text.startswith('正在恢复'):
                        flag = True
                        break
                if flag:
                    time.sleep(3)
                else:
                    fixed_flag = True
                    break
            if fixed_flag:
                logger.info('修复完成')
            else:
                logger.warning(f'未能在 {max_wait_time} 秒内完成修复')
            return True
        else:
            logger.warning(f'Did not find "资源修复" after clicking "清除缓存" (score: {score2:.3f})')
            return False
    else:
        logger.info(f'Did not find "清除缓存" in top-left region (score: {score:.3f})')
        return False


def start_and_login_arknights(helper=None) -> Tuple[bool, str]:
    """
    Unified startup function that prioritizes MAA CLI for starting and logging into Arknights
    
    Args:
        helper: Arknights helper instance (optional)
    
    Returns:
        Tuple[bool, str]: (success, message)
    """
    if helper is None:
        helper = get_helper()
    
    # Try MAA CLI first with retry
    max_retry = 1
    retry_count = 0
    while retry_count < max_retry:
        try:
            logger.info(f'Attempting to start Arknights using MAA CLI (attempt {retry_count + 1}/{max_retry})')
            from Arknights.addons.contrib.maa.maa_cli import init_maa_cli, maa_startup
            
            # Initialize MAA CLI if not already done
            init_maa_cli()
            
            # Try to start the game using MAA CLI
            maa_startup(timeout=180)
            logger.info('MAA CLI startup completed successfully')
            return True, "MAA CLI startup completed successfully"
            
        except Exception as e:
            retry_count += 1
            logger.error(f'MAA CLI startup attempt {retry_count} failed: {str(e)}')
            if retry_count >= max_retry:
                logger.warning('All MAA CLI startup attempts failed, falling back to ADB method')
            else:
                logger.info(f'Retrying MAA CLI startup ({retry_count}/{max_retry})')
                time.sleep(5)  # Wait before retry
    logger.info('maa startup 失败, 尝试修复资源')
    if check_and_click_cache_repair():
        try:
            from Arknights.addons.contrib.maa.maa_cli import init_maa_cli, maa_startup
            maa_startup(timeout=180)
        except Exception as e:
            logger.error(f'修复资源后，maa startup 错误: {str(e)}')
    
    # Fallback to ADB method if MAA CLI failed
    try:
        logger.info('Falling back to ADB startup method')
        start_and_login_arknights_adb(helper)
        logger.info('ADB startup completed successfully')
        return True, "Fallback to ADB startup method completed successfully"
    except Exception as e:
        logger.error(f'ADB startup failed: {str(e)}')
        return False, f"Both MAA CLI and ADB startup failed. MAA error: {str(e)}, ADB error: {str(e)}"


if __name__ == '__main__':
    restart_all()
