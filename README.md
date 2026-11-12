# 海外路演日历原文库

读取 UTF-8 日历、还原折行并列出组件属性，原文件按摘要留存。

## 运行

在 Python 3.11 或更新版本的 Linux 环境使用。原始内容保存在 `output/raw.sqlite3`，删除 `output/` 可回到干净状态。全部示例使用本地合成数据，不包含账号凭据或真实企业资料。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 app.py demo
```

`app.py demo` 直接执行当前读取路径并输出结构化摘要，不重放历史日志；正常执行不到 30 秒。`--output` 可以指定另一份原文库。

## 模块与数据

calendar_source.py 只负责词法读取，fixtures 中保存合成订阅与修订文件。

occurrences.py 在读取结果之上展开指定窗口内的场次，不读取 RRULE 文本给对接方。每场输出：

- `occurrence_id`：由 UID 和原始 recurrence 起点（系列时区的当地墙钟值）组成。改期和查询窗口都不改变它，重跑同一订阅不会重复。
- `start`/`end`：主办方实际举行的当地时刻（带 IANA 时区偏移）；全天活动输出本地日期，`end` 为排他日期，不按 UTC 午夜换算。
- `recurrence_key`、`all_day`、`moved`、`floating_assumed`、`summary`。

支持范围：DAILY/WEEKLY/MONTHLY 及 INTERVAL、COUNT、UNTIL、BYDAY、BYMONTHDAY、RDATE、EXDATE 和带 RECURRENCE-ID 的单场覆盖。COUNT 从系列起点计算，不从查询下界重新计数；窗口只裁剪输出，不改变身份。其余 RRULE 字段、非 IANA 时区、`RANGE=THISANDFUTURE`、`VALUE=PERIOD` 会按 UID 整组隔离到 `quarantined`，并给出 `code`、`message` 和 `detail`，不会近似展开。

版本处理：主组件先比 SEQUENCE，再比 DTSTAMP；单场覆盖按各自 RECURRENCE-ID 分组后同样比较。最高版本并列而内容不同时不按到达顺序取最后，而是隔离为 `version_conflict`。缺少一个覆盖组件不代表取消；只有 `STATUS:CANCELLED`（单场或整系列）或 EXDATE 才会移除场次。改期只保留新时刻，仍属于原 recurrence 身份，不会与恰好同一时刻的其他场次合并。

浮动时间必须由来源配置（`collect(..., floating_timezone=...)` 或来源的 `floating_timezone`）补足时区，未配置时隔离为 `missing_floating_timezone`。

`raw_store.py` 将原始字节按 SHA-256 保存，用来源和字节摘要区分观察。它只保存原文，不表达业务结果是否完整。同一内容重复观察保留一份对象并更新观察时间，SQLite 事务保证对象和观察同时提交。所有解析异常保持非零退出码，输入原件不改写。

## 环境

`requirements.txt` 登记精确依赖。环境准备阶段安装依赖，运行、构建及测试不执行下载。正式运行可以使用依赖准备后记录的镜像 ID；镜像和验证回执保存在项目外，不影响原始内容。
