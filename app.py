import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from calendar_source import read
from occurrences import Window, collect_occurrences
from raw_store import RawStore


def _load_sources(sources):
    """读取每个来源的原文与组件，保留来源名用于隔离记录。"""
    loaded = []
    for source in sources:
        path = Path(source['path'])
        raw, rows = read(path)
        loaded.append({'name': source.get('name') or path.name, 'path': path,
                       'raw': raw, 'rows': rows,
                       'floating_timezone': source.get('floating_timezone')})
    return loaded


def collect(store, sources, window, *, floating_timezone=None):
    """把同一订阅的组件合并后展开为窗口内场次，原文按来源摘要留存。

    版本比较必须跨组件进行，所以先把所有来源的组件汇总，再交给
    ``collect_occurrences`` 按 UID 和 RECURRENCE-ID 选版本。``occurrence_id``
    只由 UID 与原始 recurrence 起点决定，同一订阅重跑不会产生重复场次。
    """
    observed_at = datetime.now(timezone.utc).isoformat()
    subscriptions = []
    all_rows = []
    for item in _load_sources(sources):
        digest = store.save(item['name'], item['raw'],
                            {'component_count': len(item['rows'])}, observed_at)
        all_rows.extend(item['rows'])
        subscriptions.append({'来源': item['name'], '组件数': len(item['rows']),
                              '原文摘要': digest})

    result = collect_occurrences(all_rows, window,
                                 floating_timezone=floating_timezone,
                                 window_zone=window.start.tzinfo)
    occurrences = result['occurrences']
    occurrences.sort(key=lambda entry: (entry['start'], entry['occurrence_id']))
    return {'订阅原文': subscriptions, '场次': occurrences,
            '隔离': result['quarantined']}


def demo(store):
    sources = [{'path': path} for path in sorted(Path('fixtures').glob('*.ics'))]
    window = Window.from_dates(datetime(2026, 10, 1).date(),
                               datetime(2026, 12, 31).date(), 'America/New_York')
    return collect(store, sources, window, floating_timezone='America/New_York')


def main():
    parser = argparse.ArgumentParser(description='本地原文采集与场次展开')
    parser.add_argument('command', choices=['demo'])
    parser.add_argument('--output', default='output/raw.sqlite3')
    args = parser.parse_args()
    store = RawStore(args.output)
    try:
        result = demo(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
