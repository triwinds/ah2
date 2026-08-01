import logging
from datetime import datetime, timezone

import requests

from .cache import (
    get_cache_path,
    is_cache_expired,
    load_json_cache,
    mark_cache_updated,
    save_json_cache,
)


logger = logging.getLogger(__name__)

SOURCE = 'yituliu'
CACHE_NAME = 'material_recommendation_yituliu'
CACHE_FILE = f'{CACHE_NAME}.json'
EXP_COEFFICIENT = 0.633
SAMPLE_SIZE = 300
REQUEST_TIMEOUT = 10
# The recommendation controllers were removed from backend.yituliu.cn when
# the site moved to client-side calculation.  The backend still publishes the
# current Penguin matrix to COS, and the web client uses the same mirror.
MATRIX_ENDPOINT = 'https://cos.yituliu.cn/arknights/stage-drop/matrix.json'
ENDPOINTS = (MATRIX_ENDPOINT,)


class RecommendationError(RuntimeError):
    pass


def load_yituliu_recommendations(force_update=None, cache_key='%Y--%V'):
    cache_path = get_cache_path(CACHE_FILE)
    if force_update is None:
        force_update = is_cache_expired(CACHE_NAME, cache_key) or not cache_path.exists()
    if not force_update and cache_path.exists():
        return load_json_cache(CACHE_FILE)['items']

    try:
        payload = request_yituliu_data()
        items = parse_yituliu_response(payload)
        save_json_cache(CACHE_FILE, {
            'source': SOURCE,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'exp_coefficient': EXP_COEFFICIENT,
            'sample_size': SAMPLE_SIZE,
            'items': items,
        })
        mark_cache_updated(CACHE_NAME, cache_key)
        return items
    except Exception as e:
        if cache_path.exists():
            logger.warning('Using cached Yituliu recommendations after update failure: %s', e)
            return load_json_cache(CACHE_FILE)['items']
        if isinstance(e, RecommendationError):
            raise
        raise RecommendationError(str(e)) from e


def request_yituliu_data():
    errors = []
    for url in ENDPOINTS:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                logger.warning('Yituliu endpoint returned HTTP 404: %s', url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            errors.append(f'{url}: {e}')
    raise RecommendationError('; '.join(errors))


def parse_yituliu_response(payload):
    fetched_at = datetime.now(timezone.utc).isoformat()
    if isinstance(payload, dict) and 'matrix' in payload:
        return _parse_matrix_response(payload['matrix'])

    data = payload.get('data') if isinstance(payload, dict) else payload
    if not data:
        raise RecommendationError('Yituliu response has no data')

    if isinstance(data, dict) and 'recommendedStageList' in data:
        return _parse_recommended_stage_list(data, fetched_at)
    if isinstance(data, list):
        return _parse_legacy_stage_list(data, fetched_at)
    raise RecommendationError('Unsupported Yituliu response schema')


def _parse_matrix_response(matrix, t3_items=None, stages=None):
    if not isinstance(matrix, list):
        raise RecommendationError('Yituliu matrix response is not a list')

    # The current Yituliu API exposes the raw matrix rather than the old
    # precomputed recommendation response.  Reuse the Penguin-compatible
    # stage/item metadata and selector so both sources have identical rules.
    from .penguin import _get_t3_items, _load_stages, build_matrix_recommendations

    if t3_items is None:
        t3_items = _get_t3_items()
    if stages is None:
        stages = _load_stages()

    recommendations = build_matrix_recommendations(
        t3_items,
        stages,
        matrix,
        source=SOURCE,
    )
    if not recommendations:
        raise RecommendationError('Yituliu matrix has no usable T3 recommendation')
    return recommendations


def _parse_recommended_stage_list(data, fetched_at):
    update_time = data.get('updateTime')
    result = {}
    for item_group in data.get('recommendedStageList') or []:
        item_name = item_group.get('itemType') or item_group.get('itemSeries') or item_group.get('itemName')
        item_id = item_group.get('itemTypeId') or item_group.get('itemSeriesId') or item_group.get('itemId')
        stage_list = item_group.get('stageResultList') or item_group.get('stages') or []
        stage = _best_stage(stage_list)
        if not item_name or not stage:
            continue
        result[item_name] = _normalize_item(
            item_name=item_name,
            item_id=item_id,
            stage=stage,
            fetched_at=fetched_at,
            updated_at=update_time,
        )
    if not result:
        raise RecommendationError('Yituliu recommendedStageList is empty or invalid')
    return result


def _parse_legacy_stage_list(data, fetched_at):
    result = {}
    for row in data:
        if isinstance(row, dict):
            stage_items = [row]
        else:
            stage_items = row
        for item in stage_items:
            if not isinstance(item, dict):
                continue
            item_name = item.get('itemName') or item.get('itemType') or item.get('itemSeries')
            stage_code = item.get('stageCode')
            if not item_name or not stage_code:
                continue
            current = result.get(item_name)
            candidate = _normalize_item(
                item_name=item_name,
                item_id=item.get('itemId') or item.get('itemTypeId') or item.get('itemSeriesId'),
                stage=item,
                fetched_at=fetched_at,
            )
            if current is None or _stage_score(candidate) > _stage_score(current):
                result[item_name] = candidate
    if not result:
        raise RecommendationError('Yituliu legacy stage list is empty or invalid')
    return result


def _best_stage(stage_list):
    valid_stages = [stage for stage in stage_list if isinstance(stage, dict) and stage.get('stageCode')]
    if not valid_stages:
        return None
    return max(valid_stages, key=_stage_score)


def _stage_score(stage):
    for key in ('stageEfficiency', 'efficiency', 'leT3Efficiency', 'leT4Efficiency'):
        value = stage.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return 0.0


def _normalize_item(item_name, item_id, stage, fetched_at, updated_at=None):
    efficiency = _stage_score(stage)
    stage_code = stage.get('stageCode')
    normalized = {
        'item_id': item_id,
        'item_name': item_name,
        'itemId': item_id,
        'itemName': item_name,
        'stage_code': stage_code,
        'stageCode': stage_code,
        'source': SOURCE,
        'efficiency': efficiency,
        'stageEfficiency': efficiency,
        'updated_at': updated_at or fetched_at,
        'fetched_at': fetched_at,
    }
    for key in ('stageId', 'apExpect', 'knockRating', 'leT3Efficiency', 'sampleSize', 'zoneName'):
        if key in stage:
            normalized[key] = stage[key]
    return normalized
