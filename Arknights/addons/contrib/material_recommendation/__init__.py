import logging

from .penguin import get_t3_item_ids, load_penguin_recommendations
from .yituliu import RecommendationError, load_yituliu_recommendations


logger = logging.getLogger(__name__)


def load_stage_recommendations(force_update=None, cache_key='%Y--%V'):
    try:
        return load_yituliu_recommendations(force_update=force_update, cache_key=cache_key)
    except RecommendationError as e:
        logger.warning('Failed to load Yituliu recommendations, fallback to Penguin data: %s', e)
    return load_penguin_recommendations(force_update=force_update, cache_key=cache_key)


def get_recommended_stage_codes(force_update=None, cache_key='%Y--%V'):
    recommendations = load_stage_recommendations(force_update=force_update, cache_key=cache_key)
    return {item['stageCode'] for item in recommendations.values() if item.get('stageCode')}


__all__ = [
    'RecommendationError',
    'get_recommended_stage_codes',
    'get_t3_item_ids',
    'load_stage_recommendations',
]
