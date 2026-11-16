"""One persisted source for the three execution concurrency controls."""
import json

CONFIG_KEY = 'execution_concurrency'
DEFAULT_CONFIG = {'flowConcurrency': 3, 'promptConcurrency': 6, 'imageConcurrency': 6}

def get_execution_config(store):
    raw = store.get_setting(CONFIG_KEY)
    values = json.loads(raw) if raw else {}
    return {key: values.get(key, default) for key, default in DEFAULT_CONFIG.items()}

def save_execution_config(store, values):
    if not isinstance(values, dict) or set(values) != set(DEFAULT_CONFIG):
        raise ValueError('请填写流程、提示词和生图三个并发参数。')
    for key, label in [('flowConcurrency','流程并发数'),('promptConcurrency','提示词并发数'),('imageConcurrency','生图并发数')]:
        if type(values[key]) is not int or not 1 <= values[key] <= 12:
            raise ValueError(label + '必须为1到12的整数。')
    store.set_setting(CONFIG_KEY, json.dumps(values))
    return get_execution_config(store)
