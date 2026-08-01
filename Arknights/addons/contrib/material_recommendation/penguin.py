import logging
from datetime import datetime, timezone

import requests

import app
from penguin_stats import arkplanner

from .cache import (
    get_cache_path,
    is_cache_expired,
    load_json_cache,
    mark_cache_updated,
    save_json_cache,
)
from .yituliu import RecommendationError


logger = logging.getLogger(__name__)

SOURCE = 'penguin'
CACHE_NAME = 'material_recommendation_penguin'
CACHE_FILE = f'{CACHE_NAME}.json'
MATRIX_CACHE_FILE = 'material_recommendation_penguin_matrix.json'
MATRIX_URL = 'https://penguin-stats.io/PenguinStats/api/v2/result/matrix'
REQUEST_TIMEOUT = 10
SAMPLE_SIZE = 300


def load_penguin_recommendations(force_update=None, cache_key='%Y--%V'):
    cache_path = get_cache_path(CACHE_FILE)
    if force_update is None:
        force_update = is_cache_expired(CACHE_NAME, cache_key) or not cache_path.exists()
    if not force_update and cache_path.exists():
        return load_json_cache(CACHE_FILE)['items']

    try:
        items = build_penguin_recommendations(force_update=bool(force_update))
        save_json_cache(CACHE_FILE, {
            'source': SOURCE,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'sample_size': SAMPLE_SIZE,
            'items': items,
        })
        mark_cache_updated(CACHE_NAME, cache_key)
        return items
    except Exception as e:
        if cache_path.exists():
            logger.warning('Using cached Penguin recommendations after update failure: %s', e)
            return load_json_cache(CACHE_FILE)['items']
        if isinstance(e, RecommendationError):
            raise
        raise RecommendationError(str(e)) from e


def build_penguin_recommendations(force_update=False):
    t3_items = _get_t3_items()
    stages = _load_stages()
    matrix = _load_matrix(force_update=force_update)
    if matrix:
        recommendations = build_matrix_recommendations(t3_items, stages, matrix)
        if recommendations:
            return recommendations
    recommendations = build_stage_info_recommendations(t3_items, stages)
    if not recommendations:
        raise RecommendationError('Penguin data has no usable T3 material recommendation')
    return recommendations


def get_t3_item_ids():
    return {item['itemId'] for item in _get_t3_items()}


def build_matrix_recommendations(t3_items, stages, matrix, source=SOURCE):
    fetched_at = datetime.now(timezone.utc).isoformat()
    stage_map = {stage['stageId']: stage for stage in stages if stage.get('stageId')}
    item_map = {item['itemId']: item for item in t3_items}
    best_by_item = {}
    for drop in matrix:
        item_id = drop.get('itemId') or drop.get('itemID')
        stage_id = drop.get('stageId') or drop.get('stageID')
        if item_id not in item_map or not stage_id:
            continue
        stage = stage_map.get(stage_id)
        if not _is_usable_stage(stage):
            continue
        times = _as_float(drop.get('times'))
        quantity = _as_float(drop.get('quantity'))
        ap_cost = _as_float(stage.get('apCost'))
        if times < SAMPLE_SIZE or quantity <= 0 or ap_cost <= 0:
            continue
        ap_expect = ap_cost / (quantity / times)
        current = best_by_item.get(item_id)
        if current is None or ap_expect < current['ap_expect']:
            best_by_item[item_id] = {
                'stage': stage,
                'ap_expect': ap_expect,
                'times': times,
                'quantity': quantity,
            }
    return {
        item_map[item_id]['name']: _normalize_penguin_item(
            item=item_map[item_id],
            stage=best['stage'],
            ap_expect=best['ap_expect'],
            fetched_at=fetched_at,
            source=source,
            sample_size=best['times'],
        )
        for item_id, best in best_by_item.items()
    }


def build_stage_info_recommendations(t3_items, stages):
    fetched_at = datetime.now(timezone.utc).isoformat()
    result = {}
    for item in t3_items:
        best_stage = None
        for stage in stages:
            if not _is_usable_stage(stage):
                continue
            if not _stage_has_normal_drop(stage, item['itemId']):
                continue
            if best_stage is None or _as_float(stage.get('apCost')) < _as_float(best_stage.get('apCost')):
                best_stage = stage
        if best_stage is not None:
            result[item['name']] = _normalize_penguin_item(
                item=item,
                stage=best_stage,
                ap_expect=_as_float(best_stage.get('apCost')),
                fetched_at=fetched_at,
                source='penguin-stage-info',
            )
    return result


def _get_t3_items():
    return [
        item for item in arkplanner.get_all_items()
        if item.get('itemType') == 'MATERIAL' and item.get('rarity') == 2 and len(item.get('itemId', '')) > 4
    ]


def _load_stages():
    return arkplanner.get_all_stages()


def _load_matrix(force_update=False):
    cache_path = get_cache_path(MATRIX_CACHE_FILE)
    if cache_path.exists() and not force_update:
        return _extract_matrix(load_json_cache(MATRIX_CACHE_FILE))
    try:
        resp = requests.get(
            MATRIX_URL,
            params={'server': 'CN', 'show_closed_zone': 'true'},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        save_json_cache(MATRIX_CACHE_FILE, data)
        return _extract_matrix(data)
    except Exception as e:
        if cache_path.exists():
            logger.warning('Using cached Penguin matrix after update failure: %s', e)
            return _extract_matrix(load_json_cache(MATRIX_CACHE_FILE))
        logger.warning('Penguin matrix is unavailable, fallback to stage drop metadata: %s', e)
        return []


def _extract_matrix(data):
    if isinstance(data, dict):
        data = data.get('matrix') or data.get('data') or []
    if not isinstance(data, list):
        return []
    return data


def _is_usable_stage(stage):
    if not stage:
        return False
    stage_type = stage.get('stageType')
    if stage_type in {'DAILY', 'ACTIVITY'}:
        return False
    code = _stage_code(stage)
    if not code or code.startswith(('PR-', 'LS-', 'CE-', 'AP-', 'SK-', 'CA-')):
        return False
    ap_cost = _as_float(stage.get('apCost'))
    if ap_cost <= 0:
        return False
    return _stage_exists_on_cn(stage)


def _stage_exists_on_cn(stage):
    existence = stage.get('existence') or {}
    cn = existence.get('CN') or {}
    return cn.get('exist', True)


def _stage_has_normal_drop(stage, item_id):
    normal_drop_types = {'NORMAL_DROP', 'NORMAL', 'REGULAR_DROP'}
    for drop in stage.get('dropInfos') or []:
        if drop.get('itemId') == item_id and drop.get('dropType') in normal_drop_types:
            return True
    return False


def _normalize_penguin_item(item, stage, ap_expect, fetched_at, source, sample_size=None):
    stage_code = _stage_code(stage)
    item_name = item['name']
    efficiency = 1 / ap_expect if ap_expect else 0
    return {
        'item_id': item['itemId'],
        'item_name': item_name,
        'itemId': item['itemId'],
        'itemName': item_name,
        'stage_id': stage.get('stageId'),
        'stageId': stage.get('stageId'),
        'stage_code': stage_code,
        'stageCode': stage_code,
        'source': source,
        'efficiency': efficiency,
        'stageEfficiency': efficiency,
        'ap_expect': ap_expect,
        'apExpect': ap_expect,
        'sampleSize': sample_size,
        'updated_at': fetched_at,
        'fetched_at': fetched_at,
    }


def _stage_code(stage):
    return stage.get('code') or (stage.get('code_i18n') or {}).get('zh')


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
