import subprocess
import selectors
import atexit
import os
import shutil
from pathlib import Path
from util.msg_sender import send_by_tg_bot
import logging
import requests


logger = logging.getLogger(__name__)
maa_path = Path('/root/redroid/maa')
my_config_path = Path(os.path.realpath(os.path.dirname(__file__))).joinpath('cli_config/maa')
processes = []


def init_maa_cli():
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


def download_maa_cli():
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
    logger.info(f"maa-cli version: {subprocess.run([maa_path, 'version'], stdout=subprocess.PIPE).stdout.decode()}")


def close_all_processes():
    for p in processes:
        p.terminate()


atexit.register(close_all_processes)


def run_task(task_name: str):
    p = subprocess.Popen([maa_path, 'run', task_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    processes.append(p)
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    sel.register(p.stderr, selectors.EVENT_READ)
    log_item = ""
    summary_flag = False
    summary = ""
    ok = 0
    while ok < 2:
        for key, mask in sel.select():
            line = key.fileobj.readline().decode()
            if not line or line == "":
                ok += 1
                break
            if key.fileobj is p.stdout:
                # print("===", line, end='')
                if line.startswith('[INFO]'):
                    continue
                if line.startswith('Summary'):
                    summary_flag = True
                    continue
                if summary_flag and not line.startswith('-----------------'):
                    summary += line
            else:
                # print("---", line, end='')
                if line.startswith('[20'):
                    handle_log_item(log_item)
                    log_item = line
                else:
                    log_item += line
    if '高级资深干员' in summary:
        send_by_tg_bot('公招出 6 星了!', '公招出 6 星了!')
    return summary


def handle_log_item(log_item: str):
    if not log_item:
        return
    # print("==================================")
    # print(log_item)
    # print("==================================")
    # if 'UnknownSubTaskStart' in log_item:
    #     data_json = log_item.split('UnknownSubTaskStart: ')[1]
    #     data = json.loads(data_json)


def run_all_tasks():
    return run_task('my_tasks')


def update_maa():
    subprocess.run([maa_path, 'update'])


if __name__ == '__main__':
    init_maa_cli()
    run_all_tasks()
