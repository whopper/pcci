import datetime
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import redis

import config


def signal_handler(signal, frame):
    print('Shutting down worker')
    r.decr('workers')
    sys.exit(0)


def setup_worker():
    #register as a worker
    workers = r.get('workers')
    if workers is None:
        r.set('workers', 0)
    else:
        print "found {0} workers".format(workers)
        if int(workers) >= config.workers:
            # we already have enough workers
            print "deactivating because we already have enough workers"
            sys.exit()
    r.incr('workers')


def write_log(work_item, response):
    path = config.logpath
    org, project, pr = work_item.split('/')
    if response['success'] == 0:
        succ = 'PASS'
    else:
        succ = 'FAIL'
    unix_seconds = datetime.datetime.utcnow().strftime('%s')
    filename =  "{0}+{1}+{2}+{3}+{4}".format(org,project,pr,unix_seconds,succ)
    if response['harness_failure']:
        filename = "harness_failures/" + filename

    with open(path + "/" + filename, 'w') as f:
        f.write("Test log\n")
        f.write("Test performed at {0} - {1}\n".format(unix_seconds, datetime.datetime.utcnow()))
        f.write("{0}/{1} PR # {2}\n".format(org, project, pr))
        f.write("Took {0} Seconds\n".format(response['time']))
        if response['success'] == 0:
            f.write("Tests passed\n")
        else:
            f.write("Tests failed\n")
        for line in response['gemout']:
            f.write(line)
        for line in response['gemerr']:
            f.write(line)
        for line in response['out']:
            f.write(line)
        for line in response['err']:
            f.write(line)
    f.closed
    return (filename)


def vagrant_kill(vagrant_run_dir):
    try:

        vagrant = subprocess.Popen(["vagrant", "destroy", "-f"], cwd=vagrant_run_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        vagrant.communicate()

    except OSError:
        pass


def clean_up(tempdir):
    time.sleep(120) # give vagrant time to self-kill
    jobdir = (tempdir + "/job/.vagrant/beaker_vagrant_files/default.yml")
    vagrant_kill(jobdir)

    clean_tempdir(tempdir)


def main_loop():
    print 'looping'
    if r.get('workers_run') is None:
        sys.exit()
    work_item = r.lpop('todo')
    if work_item == None:
        time.sleep(5)
        sys.exit()
    print "starting work on {0}".format(work_item)
    response = {}
    for run in range(3): # try 3 times
        tempdir = create_pr_env(work_item)
        response = run_beaker_rspec(tempdir)
        if response['success'] == 0:
            break
        if response['time']  > 1000:
            break
        clean_up(tempdir)

    if response['success'] == 0:
        print "Tests passed"
    else:
        print "Tests failed"
    log_path = write_log(work_item, response)
    print "log written to {0}".format(log_path)
    r.rpush('completed', log_path)

    print('Shutting down worker')
    r.decr('workers')
    sys.exit()


def create_pr_env(work_item):
    print "working on {0}".format(work_item)
    org, project, pr = work_item.split('/')
    tempdir = tempfile.mkdtemp()
    git_clone = subprocess.call(["git", "clone", "https://github.com/{0}/{1}".format(org, project), tempdir + "/job"])
    if pr != 'master':
        subprocess.Popen(["git", "fetch", "origin", "pull/{0}/head:pr_{0}".format(pr)], cwd=(tempdir + "/job")).communicate()
        subprocess.Popen(["git", "checkout", "pr_{0}".format(pr)], cwd=(tempdir + "/job")).communicate()
    return str(tempdir)


def run_beaker_rspec(tempdir):
    t1 = datetime.datetime.utcnow()
    jobdir = tempdir + "/job"
    print "running in {0}".format(jobdir)
    os.mkdir(jobdir + '/.pcci_gems')
    runenv = os.environ.copy()
    runenv["GEM_HOME"]=(jobdir + '/.pcci_gems')
    nodesets = os.listdir(jobdir + '/spec/acceptance/nodesets')
    print "found nodesets {0}".format(nodesets)
    for nodename in ['ubuntu-server-1404-x64']:
        if nodename + '.yml' in nodesets:
            print "using node {0}".format(nodename)
            runenv["BEAKER_set"] = nodename
            break
    gem = subprocess.Popen(["bundle", "install"], cwd=jobdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=runenv)
    gemout, gemerr = gem.communicate()
    beaker = subprocess.Popen(["bundle", "exec", "rspec", "spec/acceptance"], cwd=jobdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=runenv)
    out, err = beaker.communicate()
    t2 = datetime.datetime.utcnow()
    t_delta = t2 - t1
    response = { 'gemout'  : gemout,
                 'gemerr'  : gemerr,
                 'out'     : out,
                 'err'     : err,
                 'success' : int(beaker.returncode),
                 'time'    : int(t_delta.seconds),
                 'harness_failure': False,
                 }
    print "parsing output for likely failures"
    for line in out:
        if 'connection closed by remote host (Net::SSH::Disconnect)' in line:
            print "harness failure detected"
            response['harness_failure'] = True


    return response


def clean_tempdir(tempdir):
    shutil.rmtree(tempdir)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    r = redis.StrictRedis(host='localhost', port=6379, db=0)
    setup_worker()
    main_loop()


