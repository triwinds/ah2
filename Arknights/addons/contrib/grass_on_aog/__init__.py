import time

import app
from Arknights.addons.contrib.common_cache import load_inventory, load_game_data
from Arknights.addons.contrib.material_recommendation import (
    RecommendationError,
    get_t3_item_ids,
    load_stage_recommendations,
)
from Arknights.addons.stage_navigator import StageNavigator, custom_stage
from automator import AddonBase
from penguin_stats import arkplanner
import logging


logger = logging.getLogger(__name__)

# MAA's Linux fight integration only automates proxy battles. A material
# recommendation can point to a stage that exists in the global data but has
# not yet been cleared on this account, leaving the proxy button locked.
MAA_FALLBACK_STAGE = '1-7'

desc = f"""
{__file__}
==================================================================================================
长草时用的脚本, 检查库存中最少的蓝材料, 然后去推荐源给出的地图刷材料.
默认推荐源: 一图流，失败时回退到本地缓存或企鹅物流粗略推荐.

不想的刷的材料可以修改脚本中的 exclude_names.

cache_key 控制缓存的频率, 默认每周读取一次库存, 如果需要手动更新缓存, 
直接删除目录下的 material_recommendation_yituliu.json 和 inventory_items_cache.json 即可.

==================================================================================================
"""

# cache_key = '%Y-%m-%d'  # cache by day
cache_key = '%Y--%V'    # cache by week

inventory_cache_file = app.cache_path.joinpath('inventory_items_cache.json')


def get_activities():
    return load_game_data('activity_table')['basicInfo']


def get_available_activities(no_mini_story=True):
    activity_table = get_activities()
    cur_time = time.time()
    if no_mini_story:
        return [activity_table[aid] for aid in activity_table
                if activity_table[aid]['startTime'] < cur_time < activity_table[aid]['endTime']
                and activity_table[aid]['type'] != 'MINISTORY']
    else:
        return [activity_table[aid] for aid in activity_table
                if activity_table[aid]['startTime'] < cur_time < activity_table[aid]['endTime']]


def get_available_activity_stages(force_update=False):
    available_activities = get_available_activities()
    if not available_activities:
        logger.info('No available activities.')
        return []
    zones_table = load_game_data('zone_table')['zones']
    zone_ids = [zid for zid in zones_table]
    available_zone_ids = []
    for activity in available_activities:
        aid = activity['id']
        for zid in zone_ids:
            if zid.startswith(f'{aid}_'):
                available_zone_ids.append(zid)
    if not available_zone_ids:
        logger.debug('No available activity zones.')
        return []
    stage_table = load_game_data('stage_table')['stages']
    available_stages = []
    for zid in available_zone_ids:
        for sid in stage_table:
            stage = stage_table[sid]
            if stage['zoneId'] == zid:
                available_stages.append(stage)
    available_stage_codes = [stage['code'] for stage in available_stages]
    if available_stage_codes:
        logger.info(f'Available activities: {[activity["name"] for activity in available_activities]}')
    logger.debug(f'Available stages: {available_stage_codes}')
    return available_stage_codes


def filter_items_with_activity(t3_item_map, available_activity_stages):
    filtered_t3_items = {}
    for item_name in t3_item_map:
        item = t3_item_map[item_name]
        if item['stageCode'] in available_activity_stages:
            filtered_t3_items[item_name] = item
    logger.info(f'Filtered t3 items: {filtered_t3_items.keys()}')
    return filtered_t3_items


def choose_activity_t3_stage_by_inventory(my_items, available_activity_stages):
    from Arknights.addons.contrib.activity import get_stage_map, get_activity_info
    stage_code_map, _ = get_stage_map()
    try:
        t3_ids = get_t3_item_ids()
    except Exception as e:
        logger.warning('Failed to load T3 material IDs from game data: %s', e)
        return None

    item_stage_map = {}
    for stage_code in available_activity_stages:
        stage = stage_code_map.get(stage_code)
        if stage is None:
            continue
        rewards = (stage.get('stageDropInfo') or {}).get('displayDetailRewards')
        if not rewards:
            continue
        activity_info = get_activity_info(stage['zoneId'])
        if activity_info is None:
            continue
        for reward in rewards:
            if reward.get("type") == "MATERIAL" and reward.get("dropType") == "NORMAL" and reward.get("id") in t3_ids:
                item_id = reward["id"]
                item_stage = item_stage_map.get(item_id)
                start_time = activity_info['startTime']
                if item_stage is None or start_time > item_stage['startTime']:
                    item_stage_map[item_id] = {
                        'stage': stage,
                        'startTime': start_time,
                    }
    logger.debug(f'item_stage_map: {[(k, item_stage_map[k]["stage"]["code"]) for k in item_stage_map]}')
    if item_stage_map:
        for my_item in my_items:
            if my_item['itemId'] in item_stage_map:
                stage_code = item_stage_map[my_item['itemId']]['stage']['code']
                logger.info('活动 T3 材料候选: %s, owned: %s, stage: %s',
                            my_item['name'], my_item['count'], stage_code)
                return stage_code


filter_latest_activity_t3_item_stage = choose_activity_t3_stage_by_inventory


def get_stage(t3_item_map, my_items, prefer_activity=True):
    normal_action = app.config.grass_on_aog.normal_action
    if prefer_activity:
        available_activity_stages = set(get_available_activity_stages())
        if not available_activity_stages:
            logger.debug('No available activity stage, skip filtering.')
            return get_stage_with_action(normal_action, my_items, t3_item_map)
        else:
            filtered_t3_items = filter_items_with_activity(t3_item_map, available_activity_stages)
            if filtered_t3_items:
                return get_stage_with_action('auto_t3', my_items, filtered_t3_items)
            else:
                stage = choose_activity_t3_stage_by_inventory(my_items, available_activity_stages)
                if stage:
                    logger.info(f'没有在推荐源中找到活动关卡相关的材料, 尝试按库存刷活动 T3 材料关卡 [{stage}]')
                    return stage
            logger.info('没有在推荐源中找到活动关卡相关的材料, 这可能是因为推荐数据还没有更新, 或者这次活动关卡的效率还不如普通关卡.')
            logger.info('可以试试在一段时间后删除 cache/material_recommendation_yituliu.json 以强制刷新推荐缓存.')
            no_aog_data_action = app.config.grass_on_aog.no_aog_data_action
            logger.info(f'no_aog_data_action: {no_aog_data_action}.')
            return get_stage_with_action(no_aog_data_action, my_items, t3_item_map)
    return get_stage_with_action(normal_action, my_items, t3_item_map)


def get_stage_with_action(action, my_items, t3_item_map):
    if action == 'none' or action is None:
        logger.info('根据配置, 不执行任何操作.')
    elif action == 'auto_t3':
        for my_item in my_items:
            t3_item = t3_item_map.get(my_item['name'])
            if t3_item:
                logger.info('require item: %s, owned: %s' % (my_item['name'], my_item['count']))
                return t3_item['stageCode']
    else:
        logger.info(f'根据配置, 刷 [{action}].')
        return action


def get_t3_item_map_from_recommendation():
    return load_stage_recommendations(cache_key=cache_key)


class GrassAddOn(AddonBase):
    def choose_stage(self):
        exclude_names = app.config.grass_on_aog.exclude
        self.logger.info('不刷以下材料: %r', exclude_names)
        self.logger.info('加载库存信息...')
        try:
            t3_item_map = get_t3_item_map_from_recommendation()
        except RecommendationError as e:
            self.logger.warning('加载推荐源失败, 将按配置降级: %s', e)
            t3_item_map = {}

        my_items = load_inventory(self.helper, cache_key=cache_key)
        all_items = arkplanner.get_all_items()

        my_items_with_count = []
        for item in all_items:
            if item['itemType'] in ['MATERIAL'] and item['name'] not in exclude_names and item['rarity'] == 2 \
                    and len(item['itemId']) > 4:
                my_items_with_count.append({'name': item['name'],
                          'itemId': item['itemId'],
                          'count': my_items.get(item['itemId'], 0) or 0,
                          'rarity': item['rarity']})
        my_items_with_count = sorted(my_items_with_count, key=lambda x: x['count'])
        return get_stage(t3_item_map, my_items_with_count, prefer_activity=app.config.grass_on_aog.prefer_activity_stage)

    @custom_stage('grass', ignore_count=True, title='一键长草', description='检查库存中最少的蓝材料, 然后去推荐源给出的地图刷材料')
    def run(self, *args):
        stage = self.choose_stage()
        if stage:
            try:
                return self.addon(StageNavigator).navigate_and_combat(stage, 1000)
            except Exception as exc:
                from Arknights.addons.contrib.maa.maa_cli import MaaFightError

                if not isinstance(exc, MaaFightError) or stage.upper() == MAA_FALLBACK_STAGE:
                    raise
                self.logger.warning(
                    'MAA failed to proxy recommended stage %s; falling back to %s: %s',
                    stage,
                    MAA_FALLBACK_STAGE,
                    exc,
                )
                return self.addon(StageNavigator).navigate_and_combat(
                    MAA_FALLBACK_STAGE, 1000
                )


__all__ = ['GrassAddOn']


if __name__ == '__main__':
    # from Arknights.configure_launcher import helper
    # helper.addon(GrassAddOn).run()
    t3_item_map = get_t3_item_map_from_recommendation()
    for item_name in t3_item_map:
        print(item_name, t3_item_map[item_name]['stageCode'])
