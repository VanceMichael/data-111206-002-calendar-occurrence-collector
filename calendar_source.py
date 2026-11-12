from pathlib import Path


def unfold(raw):
    text = raw.decode('utf-8-sig')
    output = []
    for line in text.replace('\r\n', '\n').splitlines():
        if line.startswith((' ', '\t')):
            if not output:
                raise ValueError('折行没有前置内容')
            output[-1] += line[1:]
        elif line:
            output.append(line)
    return output


def components(raw):
    lines = unfold(raw)
    if not lines or lines[0] != 'BEGIN:VCALENDAR' or lines[-1] != 'END:VCALENDAR':
        raise ValueError('日历边界不完整')
    records = []
    current = None
    for line in lines[1:-1]:
        if line == 'BEGIN:VEVENT':
            if current is not None:
                raise ValueError('活动嵌套')
            current = []
        elif line == 'END:VEVENT':
            if current is None:
                raise ValueError('缺少活动起点')
            records.append(current)
            current = None
        elif current is not None:
            if ':' not in line:
                raise ValueError('属性缺少值分隔符')
            key, value = line.split(':', 1)
            current.append({'property': key, 'value': value})
    if current is not None:
        raise ValueError('活动未结束')
    return records


def read(path):
    raw = Path(path).read_bytes()
    return raw, components(raw)
