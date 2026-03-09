import time
from random import randint
from automator import AddonBase
from .common import CommonAddon


class InventoryAddon(AddonBase):
    def _wait_for_inventory_screen(self, max_retry=5):
        import imgreco.inventory

        for attempt in range(max_retry):
            screenshot = self.screenshot()
            if imgreco.inventory.get_all_item_img_in_screen(screenshot):
                return screenshot
            self.logger.info("仓库界面未稳定，等待后重试 %d/%d", attempt + 1, max_retry)
            time.sleep(1)
        raise RuntimeError("进入仓库后未识别到物品图标")

    def get_inventory_items(self, show_item_name=False, only_normal_items=True):
        import imgreco.inventory

        self.addon(CommonAddon).back_to_main()
        self.logger.info("进入仓库")
        self.tap_rect(imgreco.inventory.get_inventory_rect(self.viewport))

        items = []
        last_screen_items = None
        screenshot = self._wait_for_inventory_screen()
        extra_move = randint(self.viewport[0] // 5, self.viewport[0] // 4) \
            if self.control.device_config.screenshot_method == 'aah-agent' else 0
        item_ids = set()
        empty_screen_retry = 0
        while True:
            screen_items = imgreco.inventory.get_all_item_details_in_screen(
                screenshot, only_normal_items=only_normal_items)
            if not screen_items:
                empty_screen_retry += 1
                if last_screen_items is not None and empty_screen_retry >= 3:
                    self.logger.info("连续空页，视为仓库读取完毕")
                    break
                if empty_screen_retry > 3:
                    raise RuntimeError("仓库识别连续为空，可能仍在过渡画面")
                self.logger.info("当前仓库页未识别到物品，重试截图 %d/3", empty_screen_retry)
                time.sleep(1)
                screenshot = self.screenshot()
                continue
            empty_screen_retry = 0
            screen_item_ids = set([item['itemId'] for item in screen_items])
            screen_items_map = {item['itemId']: item['quantity'] for item in screen_items}
            if last_screen_items is not None and not screen_item_ids - last_screen_items:
                self.logger.info("读取完毕")
                break
            if show_item_name:
                name_map = {item['itemName']: item['quantity'] for item in screen_items}
                self.logger.info('name_map: %s' % name_map)
            else:
                self.logger.info('screen_items_map: %s' % screen_items_map)
            last_screen_items = screen_item_ids
            for item in screen_items:
                if item['quantity'] is None and item['itemId'] in item_ids:
                    continue
                item_ids.add(item['itemId'])
                items.append(item)
            move = -randint(self.viewport[0] // 4, self.viewport[0] // 3) - extra_move
            self.swipe_screen(move)
            screenshot = self.screenshot()
        if show_item_name:
            self.logger.info('items_map: %s' % {item['itemName']: item['quantity'] for item in items})
        return {item['itemId']: item['quantity'] for item in items}
