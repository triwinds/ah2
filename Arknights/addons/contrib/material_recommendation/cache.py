import json
from datetime import datetime

import app


common_cache_file = app.cache_path.joinpath('common_cache.json')


def get_cache_path(cache_file_name):
    return app.cache_path.joinpath(cache_file_name)


def _load_common_cache():
    app.cache_path.mkdir(parents=True, exist_ok=True)
    if not common_cache_file.exists():
        with open(common_cache_file, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        return {}
    with open(common_cache_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_common_cache(data):
    app.cache_path.mkdir(parents=True, exist_ok=True)
    with open(common_cache_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def is_cache_expired(cache_name, cache_key):
    current_time = datetime.now().strftime(cache_key)
    common_cache = _load_common_cache()
    return common_cache.get(cache_name, current_time) != current_time


def mark_cache_updated(cache_name, cache_key):
    current_time = datetime.now().strftime(cache_key)
    common_cache = _load_common_cache()
    common_cache[cache_name] = current_time
    _save_common_cache(common_cache)


def load_json_cache(cache_file_name):
    with open(get_cache_path(cache_file_name), 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json_cache(cache_file_name, data):
    app.cache_path.mkdir(parents=True, exist_ok=True)
    with open(get_cache_path(cache_file_name), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
