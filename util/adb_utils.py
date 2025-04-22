from automator import BaseAutomator


def check_game_is_in_front(helper: BaseAutomator):
    res = helper.control.adb.shell('dumpsys window | grep mCurrentFocus')
    return 'com.hypergryph.arknights' in res.decode('utf-8')
