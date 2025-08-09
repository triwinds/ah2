import subprocess
import selectors
import atexit
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict

from util.msg_sender import send_by_tg_bot
import logging
import requests
import time
import re


logger = logging.getLogger(__name__)
maa_path = Path(r'D:\software\maa_cli\maa.exe') if os.name == 'nt' else Path('/root/redroid/maa')
my_config_path = Path(os.path.realpath(os.path.dirname(__file__))).joinpath('cli_config/maa')
processes = []
inited = False


def init_maa_cli():
    global inited
    if not maa_path.exists():
        download_maa_cli()
    p = subprocess.Popen([maa_path, 'dir', 'config'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    maa_config_path = Path(p.stdout.read().decode().strip())
    if not maa_config_path.exists():
        maa_config_path.mkdir(parents=True)
    logger.info(f'maa config path: {maa_config_path}')
    shutil.copytree(my_config_path, maa_config_path, dirs_exist_ok=True)
    update_maa()
    log_maa_cli_version()
    inited = True


def download_maa_cli():
    if os.name == 'nt':
        raise Exception('Auto download maa-cli only support linux for now.')
    resp = requests.get('https://api.github.com/repos/MaaAssistantArknights/maa-cli/releases/latest')
    data = resp.json()
    logger.info(f'maa-cli latest release: {data["tag_name"]}')
    download_url = None
    filename = None
    for asset in data['assets']:
        if 'x86' in asset['name'] and '-linux' in asset['name'] and asset['name'].endswith('tar.gz'):
            download_url = asset['browser_download_url']
            filename = asset['name']
            break
    if download_url is None:
        raise Exception('Failed to find maa-cli download url')
#     requests download from download_url
    logger.info(f'maa-cli download url: {download_url}')
    resp = requests.get(download_url)
    with open(maa_path.parent.joinpath(filename), 'wb') as f:
        f.write(resp.content)
    # unzip
    import tarfile
    zip_file = maa_path.parent.joinpath(filename)
    tar = tarfile.open(zip_file)
    for member in tar.getmembers():
        if member.name.endswith('/maa'):
            tar.extract(member, maa_path.parent)
            shutil.move(maa_path.parent.joinpath(member.name), maa_path)
            shutil.rmtree(maa_path.parent.joinpath(member.name).parent)
            break
    tar.close()
    os.remove(zip_file)
    os.chmod(maa_path, 0o755)
    logger.info(f'maa-cli download to {maa_path}')
    subprocess.run([maa_path, 'install'])
    log_maa_cli_version()


def log_maa_cli_version():
    version = subprocess.run([maa_path, 'version'], stdout=subprocess.PIPE).stdout.decode().strip()
    version = version.replace('\n', ', ')
    logger.info(f"maa-cli version: {version}")


def close_all_processes():
    for p in processes:
        p.terminate()


atexit.register(close_all_processes)


def run_task(task_name: str, timeout: int = 3600):  # 默认超时时间设为1小时
    p = subprocess.Popen([maa_path, 'run', task_name, '-vvv'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start_time = time.time()  # Add this line to track start time
    processes.append(p)
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    sel.register(p.stderr, selectors.EVENT_READ)
    log_item = ""
    summary_flag = False
    summary = ""
    ok = True
    try:
        while ok:
            # 使用超时参数进行select
            for key, mask in sel.select(timeout=1.0):  # 每1秒检查一次超时
                line = key.fileobj.readline().decode()
                if key.fileobj is p.stdout and (not line or line == ""):
                    ok = False
                    break
                if key.fileobj is p.stdout:
                    print("===", line, end='')
                    if line.startswith('[INFO]'):
                        continue
                    if line.startswith('Summary'):
                        summary_flag = True
                        continue
                    if summary_flag and not line.startswith('-----------------'):
                        summary += line
                else:
                    print("---", line, end='')
                    if line.startswith('[20'):
                        handle_log_item(log_item.strip())
                        log_item = line
                    else:
                        log_item += line

            # 检查进程是否超时
            if p.poll() is None:  # 如果进程还在运行
                if timeout is not None and (time.time() - start_time) > timeout:  # Modified this line
                    p.terminate()
                    raise subprocess.TimeoutExpired(p.args, timeout)
    except subprocess.TimeoutExpired:
        logger.error(f"Task {task_name} timed out after {timeout} seconds")
        p.terminate()
        return f"Task timed out after {timeout} seconds"
    finally:
        sel.close()
        if p in processes:
            processes.remove(p)

    if '高级资深干员' in summary:
        send_by_tg_bot('公招出 6 星了!', '公招出 6 星了!')
    return summary.strip()


def handle_log_item(log_item: str):
    if not log_item:
        return
    # print("==================================")
    # print(log_item)
    # print("==================================")
    # if 'UnknownSubTaskStart' in log_item:
    #     data_json = log_item.split('UnknownSubTaskStart: ')[1]
    #     data = json.loads(data_json)
    if log_item.endswith('Start'):
        logger.info(log_item.split(' ] ')[1])


def run_all_tasks():
    return run_task('my_tasks')


def execute_maa_command(cmd: str|list, timeout: int = 3600):
    if isinstance(cmd, str):
        cmd = cmd.split(' ')
    logger.debug(f'execute maa command: {[maa_path, *cmd]}')
    process = subprocess.Popen([maa_path, *cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=timeout)  # 添加超时参数
    except subprocess.TimeoutExpired:
        logger.error('maa command execution timed out')
        process.terminate()  # 强制终止进程
        out, err = process.communicate()  # 获取剩余的输出
        logger.error(f'timeout maa output: {out.decode()}, stderr: {err.decode()}')
        raise RuntimeError('maa command execution timed out')
    out += err
    return out.decode()

stage_code_re = re.compile(r'^[a-zA-Z0-9-]+$')


def maa_fight(stage_code: str, times=None, expiring_medicine=None, timeout=3600):
    if not inited:
        init_maa_cli()
    if stage_code:
        stage_code = stage_code.upper()
    if stage_code and not stage_code_re.match(stage_code):
        raise ValueError('Invalid stage code')
    cmds = ['fight']
    if times is not None:
        cmds += ['--times', str(times)]
        if times >= 1:
            cmds += ['--series', '0']
    else:
        cmds += ['--series', '0']
    if expiring_medicine is None:
        now = datetime.now().astimezone(tz=timezone(timedelta(hours=4)))
        wd = now.weekday()
        if wd in {5, 6}:
            expiring_medicine = 100
    if expiring_medicine:
        cmds += ['--expiring-medicine', str(expiring_medicine)]
    if stage_code:
        cmds.append(stage_code)
    output = execute_maa_command(cmds, timeout)
    logger.debug(f'maa fight output: {output}')
    if times == 0:
        # wait 1 second for slow device
        time.sleep(1)
    return _parse_fight_log(output)


def maa_startup(timeout=120):
    if not inited:
        init_maa_cli()
    execute_maa_command('startup Official', timeout=timeout)


def _parse_fight_log(log: str) -> Dict:
    result = {
        "stage_code": "",
        "times": 0,
        "total_drops": [],
        'error': False
    }
    if 'error' in log.lower():
        result['error'] = True

    # 解析关卡名称和次数
    fight_match = re.search(r'Fight (\S+) (\d+) times', log)
    if fight_match:
        result["stage_code"] = fight_match.group(1)
        result["times"] = int(fight_match.group(2))

    # 解析total drops
    drops_section = re.search(r'total drops: (.*)', log)
    if drops_section:
        items = drops_section.group(1).split(', ')
        for item in items:
            # 处理带有特殊符号的物品名称（如“勇气”胸章）
            parts = item.split(' × ')
            if parts:
                result["total_drops"].append({
                    "name": parts[0],
                    "count": int(parts[1])
                })

    return result


def update_maa():
    process = subprocess.Popen([maa_path, 'update'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    out += err
    if 'Error' in out.decode():
        logger.error(f'update maa-cli failed: {out.decode()}')
        logger.info('trying to reinstall maa-cli and maa...')
        maa_path.unlink()
        download_maa_cli()


if __name__ == '__main__':
    # logging.getLogger().addHandler(logging.StreamHandler())
    # init_maa_cli()
    # # run_all_tasks()
    # maa_fight('1-7', 0)

    text = '''
    Summary
----------------------------------------
[Fight] 22:45:43 - 22:48:32 (2m 49s) Completed
Fight PR-B-2 4 times, drops:
1. 术师芯片组 × 1, 狙击芯片组 × 3, 龙门币 × 1728
total drops: 术师芯片组 × 1, 狙击芯片组 × 3, 龙门币 × 1728
    '''
    print(_parse_fight_log(text))
