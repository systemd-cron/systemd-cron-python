#!/usr/bin/python3
import errno
import hashlib
import os
import pwd
import re
import stat
import string
import sys
from functools import reduce
from typing import Dict, List, Optional

envvar_re = re.compile(r'^([A-Za-z_0-9]+)\s*=\s*(.*)$')

MINUTES_SET = list(range(0, 60))
HOURS_SET = list(range(0, 24))
DAYS_SET = list(range(1, 32))
DOWS_SET = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHS_SET = list(range(1, 13))
TIME_UNITS_SET = ['daily', 'weekly', 'monthly', 'quarterly', 'semi-annually', 'yearly']

KSH_SHELLS = ['/bin/sh', '/bin/dash', '/bin/ksh', '/bin/bash', '/usr/bin/zsh']
REBOOT_FILE = '/run/crond.reboot'
RUN_PARTS_FLAG = '/run/systemd/use_run_parts'

USE_LOGLEVELMAX = "@use_loglevelmax@"
RANDOMIZED_DELAY = "@randomized_delay@" == "True"
USE_RUNPARTS = "@use_runparts@" == "True"
PERSISTENT = "@persistent@" == "True"
LIBDIR = "@libdir@"
STATEDIR = "@statedir@"

SELF = os.path.basename(sys.argv[0])
VALID_CHARS = "-_" + string.ascii_letters + string.digits

# this is dumb, but gets the job done
PART2TIMER = {
    'apt-compat': 'apt-daily',
    'dpkg': 'dpkg-db-backup',
    'plocate': 'plocate-updatedb',
    'sysstat': 'sysstat-summary',
}

CROND2TIMER = {
    'ntpsec': 'ntpsec-rotate-stats',
    'sysstat': 'sysstat-collect',
}

for pgm in ('/usr/sbin/sendmail', '/usr/lib/sendmail'):
    if os.path.exists(pgm):
        HAS_SENDMAIL = True
        break
else:
    HAS_SENDMAIL = False

class Persistent:
    yes, no, auto = range(3)

    @classmethod
    def parse(cls, value):
        value = value.strip().lower()
        if value in ['yes', 'true', '1']:
            return cls.yes
        elif value in ['auto', '']:
            return cls.auto
        else:
            return cls.no


class Job:
    '''Job definition'''
    filename:str
    basename:str
    line:str
    parts:List[str]
    environment:Dict[str, str]
    shell:str
    random_delay:int
    # either period or timespec
    period:Optional[str]
    timespec_minute:List[str] # 0-60
    timespec_hour:List[str] # 0-24
    timespec_dom:List[str] # 0-31
    timespec_dow:List[str] # 0-7
    timespec_month:List[str] # 0-12
    sunday_is_seven:bool
    schedule:str
    boot_delay:int
    start_hour:int
    persistent:bool
    batch:bool
    jobid:str
    unit_name:Optional[str]
    user:str
    home:Optional[str]
    command:List[str]
    execstart:str
    scriptlet:str
    valid:bool
    run_parts:bool
    standardoutput:Optional[str]
    testremoved:Optional[str]

    def __init__(self, filename:str, line:str) -> None:
        self.filename = filename
        self.basename = os.path.basename(filename)
        self.line = line
        self.parts = line.split()
        self.environment = dict()
        self.shell = '/bin/sh'
        self.boot_delay = 0
        self.start_hour = 0
        self.random_delay = 0
        self.persistent = False
        self.user = 'root'
        self.command = []
        self.valid = False
        self.run_parts = False
        self.batch = False
        self.standardoutput = None
        self.testremoved = None
        self.period = None
        self.timespec_minute = []
        self.timespec_hour = []
        self.timespec_dow = []
        self.timespec_dom = []
        self.timespec_month = []
        self.sunday_is_seven = False

    def parse_anacrontab(self) -> None:
        if len(self.parts) < 4:
            return

        self.period, delay, self.jobid = self.parts[0:3]
        try:
            self.boot_delay = int(delay)
        except ValueError:
            log(4, 'invalid DELAY in %s: %s' % (self.filename, self.line))
        self.command = self.parts[3:]

    def parse_crontab_auto(self) -> None:
        '''crontab --translate <something>'''
        if self.line.startswith('@'):
            self.parse_crontab_at(False)
        else:
            self.parse_crontab_timespec(False)

        if self.command:
            maybe_user = self.command[0]
            try:
                pwd.getpwnam(maybe_user)
                self.user = maybe_user
                self.command = self.command[1:]
            except:
                self.user = os.getlogin()
            pgm = which(self.command[0])
            if pgm:
                self.command[0] = pgm
            self.execstart = ' '.join(self.command)

    def parse_crontab_at(self, withuser:bool) -> None:
        '''@daily (user) do something'''
        self.jobid = self.basename
        if len(self.parts) < (2 + int(withuser)):
            return

        self.period = self.parts[0]
        if withuser:
            self.user = self.parts[1]
            self.command = self.parts[2:]
        else:
            self.user = self.basename
            self.command = self.parts[1:]

    def parse_crontab_timespec(self, withuser:bool) -> None:
        '''6 2 * * * (user) do something'''
        self.jobid = self.basename
        if len(self.parts) < (6 + int(withuser)):
            return

        minutes, hours, days, months, dows = self.parts[0:5]
        self.timespec_minute = self.parse_time_unit(minutes, MINUTES_SET)
        self.timespec_hour = self.parse_time_unit(hours, HOURS_SET)
        self.timespec_dom = self.parse_time_unit(days, DAYS_SET)
        self.timespec_dow = self.parse_time_unit(dows, DOWS_SET, dow_map)
        self.sunday_is_seven = dows.endswith('7') or dows.title().endswith('Sun')
        self.timespec_month = self.parse_time_unit(months, MONTHS_SET, month_map)

        if withuser:
            self.user = self.parts[5]
            self.command = self.parts[6:]
        else:
            self.user = self.basename
            self.command = self.parts[5:]

    def parse_time_unit(self, value:str, values, mapping=int) -> List[str]:
        result:List[str]
        if value == '*':
            return ['*']
        try:
            base = min(values)
            # day of weeks
            if isinstance(base, str):
                base = 0
            result = sorted(reduce(lambda a, i: a.union(set(i)), list(map(values.__getitem__,
            list(map(parse_period(mapping, base), value.split(','))))), set()))
        except ValueError:
            result = []
        if not len(result):
            log(3, 'garbled time in %s [%s]: %s' % (self.filename, self.line, value))
        return result

    def decode(self) -> bool:
        '''decode & validate'''
        self.jobid = ''.join(c for c in self.jobid if c in VALID_CHARS)
        self.unit_name = None

        if not self.command:
            return False

        if 'SHELL' in self.environment:
            self.shell = self.environment['SHELL']

        self.decode_command()

        if self.period:
            self.period = self.period.lower().lstrip('@')
            self.period = {
                '1': 'daily',
                '7': 'weekly',
                '30': 'monthly',
                '31': 'monthly',
                'biannually': 'semi-annually',
                'bi-annually': 'semi-annually',
                'semiannually': 'semi-annually',
                'anually': 'yearly',
                'annually': 'yearly',
                '365': 'yearly',
            }.get(self.period, self.period)

        self.valid = True
        return True

    def decode_command(self) -> None:
        '''perform smart substitutions for known shells'''
        if self.shell not in KSH_SHELLS:
            return

        try:
            self.home = pwd.getpwnam(self.user).pw_dir
        except KeyError:
            pass
        if self.home and self.command[0].startswith('~/'):
            self.command[0] = self.home + self.command[0][2:]

        if (len(self.command) >= 3 and
            self.command[-2] == '>' and
            self.command[-1] == '/dev/null'):
            self.command = self.command[0:-2]
            self.standardoutput = '/dev/null'

        if (len(self.command) >= 2 and
            self.command[-1] == '>/dev/null'):
            self.command = self.command[0:-1]
            self.standardoutput = '/dev/null'

        if (len(self.command) == 6 and
            self.command[0] == '[' and
            self.command[1] in ['-x','-f','-e'] and
            self.command[2] == self.command[5] and
            self.command[3] == ']' and
            self.command[4] == '&&' ):
                self.testremoved = self.command[2]
                self.command = self.command[5:]

        if (len(self.command) == 5 and
            self.command[0] == 'test' and
            self.command[1] in ['-x','-f','-e'] and
            self.command[2] == self.command[4] and
            self.command[3] == '&&' ):
                self.testremoved = self.command[2]
                self.command = self.command[4:]

    def generate_scriptlet(self) -> Optional[str]:
        '''...only if needed'''
        if len(self.command) == 1:
            if os.path.isfile(self.command[0]):
                self.execstart = self.command[0]
                return None
            else:
                pgm = which(self.command[0])
                if pgm:
                    self.execstart = pgm
                    return None

        self.scriptlet = os.path.join(TARGET_DIR, '%s.sh' % self.unit_name)
        self.execstart = self.shell + ' ' + self.scriptlet
        return ' '.join(self.command)

    def generate_service(self) -> str:
        lines = list()
        lines.append('[Unit]')
        lines.append('Description=[Cron] "%s"' % self.line.replace('%', '%%'))
        lines.append('Documentation=man:systemd-crontab-generator(8)')
        if self.filename != '-':
            lines.append('SourcePath=%s' % self.filename)
        if 'MAILTO' in self.environment and not self.environment['MAILTO']:
            pass # mails explicitely disabled
        elif not HAS_SENDMAIL:
            pass # mails automaticaly disabled
        else:
            lines.append('OnFailure=cron-failure@%i.service')
        if self.user != 'root' or self.filename == os.path.join(STATEDIR, 'root'):
            lines.append('Requires=systemd-user-sessions.service')
            if self.home:
                lines.append('RequiresMountsFor=%s' % self.home)
        lines.append('')

        lines.append('[Service]')
        lines.append('Type=oneshot')
        lines.append('IgnoreSIGPIPE=false')
        lines.append('KillMode=process')
        if USE_LOGLEVELMAX != 'no':
            lines.append('LogLevelMax=%s' % USE_LOGLEVELMAX)
        if self.schedule and self.boot_delay:
            lines.append('ExecStartPre=-%s/systemd-cron/boot_delay %s' % (LIBDIR, self.boot_delay))
        lines.append('ExecStart=%s' % self.execstart)
        if self.environment:
             lines.append('Environment=%s' % environment_string(self.environment))
        lines.append('User=%s' % self.user)
        if self.standardoutput:
             lines.append('StandardOutput=%s' % self.standardoutput)
        if self.batch:
             lines.append('CPUSchedulingPolicy=idle')
             lines.append('IOSchedulingClass=idle')

        return '\n'.join(lines)

    def generate_timer(self) -> str:
        lines = list()
        lines.append('[Unit]')
        lines.append('Description=[Timer] "%s"' % self.line.replace('%', '%%'))
        lines.append('Documentation=man:systemd-crontab-generator(8)')
        lines.append('PartOf=cron.target')
        if self.filename != '-':
            lines.append('SourcePath=%s' % self.filename)
        if self.testremoved:
            lines.append('ConditionFileIsExecutable=%s' % self.testremoved)
        lines.append('')

        lines.append('[Timer]')
        if self.schedule:
            lines.append('OnCalendar=%s' % self.schedule)
        else:
            lines.append('OnBootSec=%sm' % self.boot_delay)
        if self.random_delay > 1:
            if RANDOMIZED_DELAY:
                lines.append('RandomizedDelaySec=%sm' % self.random_delay)
            else:
                lines.append('AccuracySec=%sm' % self.random_delay)
        if self.persistent:
            lines.append('Persistent=true')

        return '\n'.join(lines)


def which(exe):
    '''TODO: we could use the PATH= variable from the crontab'''
    for path in os.environ.get('PATH', '/usr/bin:/bin').split(os.pathsep):
        try:
            abspath = os.path.join(path, exe)
            statbuf = os.stat(abspath)
        except:
            continue
        if stat.S_IMODE(statbuf.st_mode) & 0o111:
            return abspath

    return None

def files(dirname:str) -> List[str]:
    try:
        return list(filter(os.path.isfile, [os.path.join(dirname, f) for f in os.listdir(dirname)]))
    except OSError:
        return []

def expand_home_path(path:str, user:str) -> str:
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        return path

    parts = path.split(':')
    for i, part in enumerate(parts):
        if part.startswith('~/'):
            parts[i] = home + part[1:]
    return ':'.join(parts)

def environment_string(env:Dict[str, str]) -> str:
    line = []
    for k, v in env.items():
        if ' ' in v:
            line.append('"%s=%s"' % (k, v))
        else:
            line.append('%s=%s' % (k, v))
    return ' '.join(line)

def parse_crontab(filename:str,
                  withuser:bool=True,
                  monotonic:bool=False):
    '''parser shared with /usr/bin/crontab'''

    environment:Dict[str,str] = dict()
    random_delay:int = 1
    start_hour:int = 6
    boot_delay:int = 0
    persistent:int = Persistent.yes if monotonic else Persistent.auto
    batch:bool = False
    run_parts:bool = USE_RUNPARTS
    with open(filename, 'rb') as f:
        for rawline in f.readlines():
            rawline = rawline.strip()
            if not rawline or rawline.startswith(b'#'):
                continue

            line = rawline.decode('utf8')

            while '  ' in line:
                line = line.replace('  ', ' ')

            envvar = envvar_re.match(line)
            if envvar:
                value = envvar.group(2)
                value = value.strip("'").strip('"')
                if envvar.group(1) == 'RANDOM_DELAY':
                     try:
                         random_delay = int(value)
                     except ValueError:
                         log(4, 'invalid RANDOM_DELAY in %s: %s' % (filename, line))
                elif envvar.group(1) == 'START_HOURS_RANGE':
                     try:
                         start_hour = int(value.split('-')[0])
                     except ValueError:
                         log(4, 'invalid START_HOURS_RANGE in %s: %s' % (filename, line))
                elif envvar.group(1) == 'DELAY':
                     try:
                         boot_delay = int(value)
                     except ValueError:
                         log(4, 'invalid DELAY in %s: %s' % (filename, line))
                elif envvar.group(1) == 'PERSISTENT':
                     persistent = Persistent.parse(value)
                elif not withuser and envvar.group(1) == 'PATH':
                     basename:str = os.path.basename(filename)
                     environment['PATH'] = expand_home_path(value, basename)
                elif envvar.group(1) == 'BATCH':
                     batch = (value.strip().lower() in ['yes','true','1'])
                elif envvar.group(1) == 'RUN_PARTS':
                     run_parts = (value.strip().lower() in ['yes','true','1'])
                elif envvar.group(1) == 'MAILTO':
                     environment[envvar.group(1)] = value
                     if value and not HAS_SENDMAIL:
                         log(4, 'a MTA is not installed, but MAILTO is set in %s' % filename)
                else:
                     environment[envvar.group(1)] = value
                continue

            parts = line.split()
            line = ' '.join(parts)

            j = Job(filename, line)
            j.boot_delay = boot_delay
            j.batch = batch
            j.run_parts = run_parts
            j.random_delay = random_delay
            j.environment = environment
            j.start_hour = start_hour
            if monotonic:
                j.persistent = False if persistent == Persistent.no else True
                j.parse_anacrontab()
            elif line.startswith('@'):
                j.persistent = False if persistent == Persistent.no else True
                j.parse_crontab_at(withuser)
            else:
                j.persistent = True if persistent == Persistent.yes else False
                j.parse_crontab_timespec(withuser)
            j.decode()
            yield j


def month_map(month:str) -> int:
    try:
        return int(month)
    except ValueError:
        return ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'nov', 'dec'].index(month.lower()[0:3]) + 1

def dow_map(dow:str) -> int:
    try:
        return ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'].index(dow[0:3].lower())
    except ValueError:
        return int(dow) #% 7

def parse_period(mapping=int, base=0):
    def parser(value:str):
        try:
            range, step = value.split('/')
        except ValueError:
            range = value
            step = '1'

        if range == '*':
            return slice(None, None, int(step))

        try:
            start, end = range.split('-')
        except ValueError:
            start = end = range

        return slice(mapping(start) - 1 + int(not(bool(base))), mapping(end) + int(not(bool(base))), int(step))

    return parser

def generate_timer_unit(job:Job, seq=None) -> Optional[str]:
    daemon_reload = os.path.isfile(REBOOT_FILE)

    if job.testremoved and not os.path.isfile(job.testremoved):
        log(3, '%s is removed, skipping job' % job.testremoved)
        return None

    if (len(job.command) == 6 and
        job.command[0] == '[' and
        job.command[1] in ['-d','-e'] and
        job.command[2] == '/run/systemd/system' and
        job.command[3] == ']' and
        job.command[4] == '||'):
            return None

    if (len(job.command) == 5 and
        job.command[0] == 'test' and
        job.command[1] in ['-d','-e'] and
        job.command[2] == '/run/systemd/system' and
        job.command[3] == '||'):
            return None

    if job.period:
        hour = job.start_hour

        if job.period == 'reboot':
            if daemon_reload:
                return None
            if job.boot_delay == 0:
                job.boot_delay = 1
            job.schedule = ''
            job.persistent = False
        elif job.period == 'minutely':
            job.schedule = job.period
            job.persistent = False
        elif job.period == 'hourly' and job.boot_delay == 0:
            job.schedule = 'hourly'
        elif job.period == 'hourly':
            job.schedule = '*-*-* *:%s:0' % job.boot_delay
            job.boot_delay = 0
        elif job.period == 'midnight' and job.boot_delay == 0:
            job.schedule = 'daily'
        elif job.period == 'midnight':
            job.schedule = '*-*-* 0:%s:0' % job.boot_delay
        elif job.period in TIME_UNITS_SET and hour == 0 and job.boot_delay == 0:
            job.schedule = job.period
        elif job.period == 'daily':
            job.schedule = '*-*-* %s:%s:0' % (hour, job.boot_delay)
        elif job.period == 'weekly':
            job.schedule = 'Mon *-*-* %s:%s:0' % (hour, job.boot_delay)
        elif job.period == 'monthly':
            job.schedule = '*-*-1 %s:%s:0' % (hour, job.boot_delay)
        elif job.period == 'quarterly':
            job.schedule = '*-1,4,7,10-1 %s:%s:0' % (hour, job.boot_delay)
        elif job.period == 'semi-annually':
            job.schedule = '*-1,7-1 %s:%s:0' % (hour, job.boot_delay)
        elif job.period == 'yearly':
            job.schedule = '*-1-1 %s:%s:0' % (hour, job.boot_delay)
        else:
            try:
               if int(job.period) > 31:
                    # workaround for anacrontab
                    job.schedule = '*-1/%s-1 %s:%s:0' % (int(round(int(job.period) / 30)), hour, job.boot_delay)
               else:
                    job.schedule = '*-*-1/%s %s:%s:0' % (int(job.period), hour, job.boot_delay)
            except ValueError:
                    log(3, 'unknown schedule in %s: %s' % (job.filename, job.line))
                    job.schedule = job.period

    else:
        if job.timespec_dow == ['*']:
            dows = ''
        else:
            dows_sorted = []
            for day in DOWS_SET[int(job.sunday_is_seven):]:
                if day in job.timespec_dow and not day in dows_sorted:
                    dows_sorted.append(day)
            dows = ','.join(dows_sorted) + ' '

        if '0' in job.timespec_month: job.timespec_month.remove('0')
        if '0' in job.timespec_dom: job.timespec_dom.remove('0')

        # 2023: I have no clue what this is for
        if (not len(job.timespec_month) or
           not len(job.timespec_dom) or
           not len(job.timespec_hour) or
           not len(job.timespec_minute)):
            log(3, 'unknown schedule in %s: %s' % (job.filename, job.line))
            return None

        job.schedule = '%s*-%s-%s %s:%s:00' % (
                      dows,
                      ','.join(map(str, job.timespec_month)),
                      ','.join(map(str, job.timespec_dom)),
                      ','.join(map(str, job.timespec_hour)),
                      ','.join(map(str, job.timespec_minute))
                   )

    if not job.unit_name:
        if not job.persistent:
            unit_id = next(seq)
        else:
            unit_id = hashlib.md5()
            unit_id.update(bytes('\0'.join([job.schedule, ' '.join(job.command)]), 'utf-8'))
            unit_id = unit_id.hexdigest()
        job.unit_name = "cron-%s-%s-%s" % (job.jobid, job.user, unit_id)


    code = job.generate_scriptlet()
    if code:
        with open(job.scriptlet, 'w', encoding='utf8') as f:
            f.write(code + '\n')


    timer = os.path.join(TARGET_DIR, '%s.timer' % job.unit_name)
    with open(timer, 'w', encoding='utf8') as f:
        f.write(job.generate_timer() + '\n')

    try:
        os.symlink(timer, os.path.join(TIMERS_DIR, '%s.timer' % job.unit_name))
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    service = os.path.join(TARGET_DIR, '%s.service' % job.unit_name)
    with open(service, 'w', encoding='utf8') as f:
        f.write(job.generate_service() + '\n')

    return timer

def log(level:int, message:str) -> None:
    if len(sys.argv) == 4:
        with open('/dev/kmsg', 'w', encoding='utf8') as kmsg:
            kmsg.write('<%s>%s[%s]: %s\n' % (level, SELF, os.getpid(), message))
    else:
        sys.stderr.write('%s: %s\n' % (SELF, message))

seqs:Dict[str, int] = {}
def count():
    n = 0
    while True:
        yield n
        n += 1

def main() -> None:
    try:
        os.makedirs(TIMERS_DIR)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    run_parts = USE_RUNPARTS
    fallback_mailto = None

    if os.path.isfile('/etc/crontab'):
        for job in parse_crontab('/etc/crontab', withuser=True):
            run_parts = job.run_parts
            fallback_mailto = job.environment.get('MAILTO')
            if not job.valid:
                 log(3, 'truncated line in /etc/crontab: %s' % job.line)
                 continue
            if '/etc/cron.hourly'  in job.line: continue
            if '/etc/cron.daily'   in job.line: continue
            if '/etc/cron.weekly'  in job.line: continue
            if '/etc/cron.monthly' in job.line: continue
            generate_timer_unit(job,
                                seq=seqs.setdefault(
                                    job.jobid+job.user, count()
                                ))

    CRONTAB_FILES = files('/etc/cron.d')
    for filename in CRONTAB_FILES:
        basename = os.path.basename(filename)
        basename_distro = CROND2TIMER.get(basename, basename)
        masked = False
        for unit_file in ('/lib/systemd/system/%s.timer' % basename,
                          '/lib/systemd/system/%s.timer' % basename_distro,
                          '/etc/systemd/system/%s.timer' % basename,
                          '/run/systemd/system/%s.timer' % basename):
            if os.path.exists(unit_file):
                masked = True
                if os.path.realpath(unit_file) == '/dev/null':
                    log(5, 'ignoring %s because it is masked' % filename)
                else:
                    log(5, 'ignoring %s because native timer is present' % filename)
                break
        if masked:
            continue
        if basename.startswith('.'):
            continue
        if '.dpkg-' in basename:
            log(5, 'ignoring %s' % filename)
            continue
        if '~' in basename:
            log(5, 'ignoring %s' % filename)
            continue
        for job in parse_crontab(filename, withuser=True):
            if not job.valid:
                log(3, 'truncated line in %s: %s' % (filename, job.line))
                continue
            if fallback_mailto and 'MAILTO' not in job.environment:
                job.environment['MAILTO'] = fallback_mailto
            generate_timer_unit(job, seq=seqs.setdefault(job.jobid+job.user, count()))

    if run_parts:
        open(RUN_PARTS_FLAG, 'a').close()
    else:
        if os.path.exists(RUN_PARTS_FLAG):
            os.unlink(RUN_PARTS_FLAG)
        # https://github.com/systemd-cron/systemd-cron/issues/47
        i = 0
        for period in ['hourly', 'daily', 'weekly', 'monthly', 'yearly']:
            i = i + 1
            directory = '/etc/cron.' + period
            if not os.path.isdir(directory):
                continue
            CRONTAB_FILES = files('/etc/cron.' + period)
            for filename in CRONTAB_FILES:
                job = Job(filename, filename)
                job.persistent = PERSISTENT
                job.period = period
                job.boot_delay = i * 5
                job.command = [filename]
                basename = os.path.basename(filename)
                job.jobid = period + '-' + basename
                job.decode() # ensure clean jobid
                if fallback_mailto and 'MAILTO' not in job.environment:
                    job.environment['MAILTO'] = fallback_mailto
                basename_distro = PART2TIMER.get(basename, basename)
                if (os.path.exists('/lib/systemd/system/%s.timer' % basename)
                 or os.path.exists('/lib/systemd/system/%s.timer' % basename_distro)
                 or os.path.exists('/etc/systemd/system/%s.timer' % basename)):
                    log(5, 'ignoring %s because native timer is present' % filename)
                    continue
                elif basename.startswith('.'):
                    continue
                elif '.dpkg-' in basename:
                    log(5, 'ignoring %s' % filename)
                    continue
                else:
                    job.unit_name = 'cron-' + job.jobid
                    generate_timer_unit(job)

    if os.path.isfile('/etc/anacrontab'):
        for job in parse_crontab('/etc/anacrontab', monotonic=True):
            if not job.valid:
                 log(3, 'truncated line in /etc/anacrontab: %s' % job.line)
                 continue
            generate_timer_unit(job, seq=seqs.setdefault(job.jobid+job.user, count()))


    if os.path.isdir(STATEDIR):
        # /var is avaible
        USERCRONTAB_FILES = files(STATEDIR)
        for filename in USERCRONTAB_FILES:
            basename = os.path.basename(filename)
            if '.' in basename:
                continue
            else:
                for job in parse_crontab(filename, withuser=False):
                    generate_timer_unit(job, seq=seqs.setdefault(job.jobid+job.user, count()))
        try:
            open(REBOOT_FILE,'a').close()
        except:
            pass
    else:
        # schedule rerun
        with open('%s/cron-after-var.service' % TARGET_DIR, 'w') as f:
            f.write('[Unit]\n')
            f.write('Description=Rerun systemd-crontab-generator because /var is a separate mount\n')
            f.write('Documentation=man:systemd.cron(7)\n')
            f.write('After=cron.target\n')
            f.write('ConditionDirectoryNotEmpty=%s\n' % STATEDIR)

            f.write('\n[Service]\n')
            f.write('Type=oneshot\n')
            f.write('ExecStart=/bin/sh -c "systemctl daemon-reload ; systemctl try-restart cron.target"\n')

        MULTIUSER_DIR = os.path.join(TARGET_DIR, 'multi-user.target.wants')

        try:
           os.makedirs(MULTIUSER_DIR)
        except OSError as e:
           if e.errno != errno.EEXIST:
               raise

        try:
            os.symlink('%s/cron-after-var.service' % TARGET_DIR, '%s/cron-after-var.service' % MULTIUSER_DIR)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise


if __name__ == '__main__':
    if len(sys.argv) == 1 or not os.path.isdir(sys.argv[1]):
        sys.exit("Usage: %s <destination_folder>" % sys.argv[0])

    TARGET_DIR = sys.argv[1]
    TIMERS_DIR = os.path.join(TARGET_DIR, 'cron.target.wants')

    try:
        main()
    except Exception as e:
        if len(sys.argv) == 4:
            open('/dev/kmsg', 'w').write('<2> %s[%s]: global exception: %s\n' % (SELF, os.getpid(), e))
            exit(1)
        else:
            raise
