import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from raw_store import RawStore


def demo(store):
    observed_at = datetime.now(timezone.utc).isoformat()
    from calendar_source import read
    result = []
    for path in sorted(Path('fixtures').glob('*.ics')):
        raw, components = read(path)
        digest = store.save(path.name, raw, {'component_count': len(components)}, observed_at)
        result.append({'文件': path.name, '组件数': len(components), '原文摘要': digest})
    return {'订阅原文': result, '说明': '组件尚未展开为场次'}


def main():
    parser = argparse.ArgumentParser(description='本地原文采集演示')
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
