# croncheck

**没跑、跑了但失败了、跑了但太慢——三件事要分开说。**

`croncheck` 只读检查定时任务的状态：解析 crontab 定义、扫描执行日志，
把每个任务的状态分成四类，并给出证据。

- 零依赖，Python 3.10+ 标准库即可运行
- 单文件，`croncheck.py` 拷走就能用
- **只读不触发**：从不执行任务，也不与 cron 服务交互

## 为什么需要它

踩过「定时任务突然不跑了」的坑：查下来是时区。
容器里按 UTC 排程，配置里写的是本地时间，差八小时，
于是任务每次都在凌晨两点被触发——但那时候没人看日志。

更麻烦的是另一类情况：任务**跑了，但失败了**，而面板上只显示「上次执行时间」。
你看不出它失败了，也就没人去修。

`croncheck` 把状态分成四类，每类都有各自的修法：

| 状态 | 含义 | 该查什么 |
|---|---|---|
| **未执行过** | 找不到任何执行记录 | 调度是否生效、时区对不对得上 |
| **执行失败** | 有记录，但标记为失败 | 命令本身、依赖、权限、退出码 |
| **耗时异常** | 执行了，但耗时超过阈值 | 是不是变慢了、是不是重复执行 |
| **执行正常** | 有成功记录 | 无 |

「未执行过」和「执行失败」混在一起是最要命的——修法完全不同。

## 用法

```bash
# 只看 crontab 里定义了什么
python croncheck.py --crontab ./crontab.txt

# 加上日志目录，才能判断执行情况
python croncheck.py --crontab ./crontab.txt --log-dir ./logs

# 调整耗时阈值（秒）
python croncheck.py --crontab ./crontab.txt --log-dir ./logs --slow 120

# 机器可读输出
python croncheck.py --crontab ./crontab.txt --log-dir ./logs --json
```

输出：

```
crontab：./crontab.txt
日志目录：./logs
任务数：3
  未执行过：1
  执行失败：1
  执行正常：1

[1] 未执行过  (0 2 * * *)  /usr/bin/backup.sh
    来源：./crontab.txt:2
    最后执行：
    退出码：
    耗时：
    证据：（没有日志目录，无法判断执行情况）

[2] 执行失败  (30 3 * * 1)  /usr/bin/weekly-report.sh
    来源：./crontab.txt:3
    最后执行：2026-10-20 03:30:02
    退出码：1
    耗时：4.210
    证据：./logs/weekly.log: 2026-10-20 03:30:06 weekly-report FAILED exit code 1

[3] 执行正常  (*/5 * * * *)  /usr/bin/healthcheck.sh
    来源：./crontab.txt:4
    最后执行：2026-10-26 09:55:01
    退出码：
    耗时：0.412
    证据：./logs/healthcheck.log: 2026-10-26 09:55:01 healthcheck OK

说明：
  「未执行过」与「执行失败」是两回事——修法完全不同：
    未执行过 → 查调度是否生效、时区是否对得上；
    执行失败 → 查命令本身、依赖、权限与退出码。
  本工具只读文件，从不触发任务，也不与 cron 服务交互。
```

## 日志是怎么匹配的

`croncheck` 用**命令的 basename**（`/usr/bin/backup.sh` → `backup.sh`）
作为关键词，在日志目录里找包含这个词的文件。
日志文件名不必与任务名一致，内容里出现关键词就会纳入。

从日志里读三类信息：

- **状态**：`OK`/`SUCCESS`/`完成` 算成功，`FAILED`/`FAIL`/`ERROR`/`exit code: N` 算失败
- **退出码**：从 `exit code 1` 这类文本里提取
- **耗时**：从 `耗时: 1.5s`、`duration 250ms`、`elapsed: 3` 里提取

**只要有一条失败记录，整个任务就判定为失败**——哪怕别的记录是成功的。

## 已知局限

- **不与 cron 服务交互**。它不查 `crontab -l`、不查 systemd timer、不查 crond 日志，
  只读你给的 crontab 文件。这意味着它看到的是**文件里的定义**，不是**实际生效的调度**。
- **不判断时区**。它会把 `CRON_TZ=` 这类环境变量解析出来但不做时区换算，
  「因为时区没跑」这类原因需要你自己对一遍。
- **日志匹配靠关键词**。命令没出现在日志里就判为「未执行过」——
  但也可能是日志被轮转掉了。
- **不读 systemd timer 的 `OnCalendar` 表达式**。只认 cron 五段格式与 `@reboot`/`@daily` 这类宏。
- **不检查命令是否存在**、是否有执行权限。
- **耗时阈值是固定的秒数**，不按任务类型区分。
- **不做并发扫描**，日志多时较慢。

## 跑测试

```bash
python test_croncheck.py
```

测试里有一条专门验证「工具不会执行 crontab 里的命令」：
往 crontab 里写一条 `touch` 命令，跑完断言目标文件没有被创建。

## 为什么还要做这个

现有的监控方案要么太重（要装 agent、要接 Prometheus），
要么太糙（只看一个「上次运行时间」字段）。

`croncheck` 站在中间：给它一个 crontab 文件和一个日志目录，它就告诉你
每个任务到底是**没跑**、**跑了但失败**、还是**跑得太慢**，并附上证据。

## 来源与参考

- crontab(5) 手册页：<https://man7.org/linux/man-pages/man5/crontab.5.html>
- Debian cron 文档：<https://wiki.debian.org/Cron>
- systemd.timer 手册（`OnCalendar` 语法参考）：<https://www.freedesktop.org/software/systemd/man/systemd.timer.html>

---

## 关于作者

tridy-worm 做定时任务状态只读检查。这个工具先把状态分清楚，再谈下一步。

- 个人主页：<https://tridy-worm.github.io>
- 仓库：<https://github.com/tridy-worm/croncheck>

## 许可

MIT，见 [LICENSE](LICENSE)。
